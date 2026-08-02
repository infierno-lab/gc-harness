"""The planner (spec §5.4-5.5): L2 plan-template cache first, LLM (Sonnet)
only on a miss, one repair round on a validation failure, never a loop.

Nothing here executes anything — `plan_ask` only ever returns a `PlanIR`
(possibly `None` on unrepairable failure) plus its `ValidationResult`; the
caller decides whether to approve/execute.
"""

import json
import time
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.catalog.digest import render_digest
from core.catalog.store import resolve_block
from core.classify.classifier import ClassifyResult
from core.classify.schema import Entities
from core.db.models import PlanTemplate
from core.gateway import get_gateway, resolve_model
from core.plan.plan_ir import PlanIR
from core.plan.validator import ValidationError as PlanValidationError, ValidationResult, validate_plan

_PLAN_IR_JSON_SCHEMA: dict[str, Any] = PlanIR.model_json_schema()

# Third and final layer of the compound-ask bug (see core/classify/compound.py
# for the first two): classification and hashing are fixed, but the planner
# itself can still silently under-serve a compound ask via "repair by
# amputation" — verified live: a genuine "elasticity ... and promote to
# champion" ask produced a 3-node plan (no promote_model) that validated and
# executed, because the first LLM attempt's promote_model node tripped
# `sideeffect_policy` (mutate_state nodes always do here — plan_ask never
# passes actor_roles=["approver"], that's the two-person approval flow's job,
# not the planner's) and the repair round "fixed" that by deleting the node
# instead of leaving it in place for approval.
#
# This maps each `compound:<verb>` constraint token (core.classify.compound)
# to the catalog block *kind* the ask therefore requires — kept intentionally
# small: only verbs with an unambiguous, existing catalog kind get an entry;
# every other compound token implies no structural requirement here.
_COMPOUND_TOKEN_REQUIRED_KIND: dict[str, str] = {
    "compound:promote": "promotion",
    "compound:optimize": "optimizer",
    "compound:optimise": "optimizer",
}

# The exact slot-marker prefix templated into a stored PlanTemplate's params in
# place of a recognized entity *value* (spec §5.4: "template the exact entity
# keys you extract" — kept to a flat string substitution, not a mini DSL).
_SLOT_PREFIX = "$slot:"

_FEW_SHOT_ELASTICITY_E2E = {
    "plan_ir_version": 1,
    "intent_summary": "DML elasticity e2e — demo/blinkit Jan-Jun 2026, gated, no promotion",
    "nodes": [
        {
            "id": "n1",
            "block": "panel_builder@1.0",
            "params": {"brand": "demo", "platform": "blinkit", "window": {"from": "2026-01", "to": "2026-06"}},
        },
        {"id": "n2", "block": "elasticity_dml@1.0", "inputs": {"panel": "n1.panel"}},
        {
            "id": "n3",
            "block": "post_model_checks@1.0",
            "inputs": {"surface": "n2.surface"},
            "gate": {"policy": "block"},
        },
    ],
    "outputs": {"surface": "n2.surface"},
    "assumptions": ["read-only run; champion untouched"],
    "estimated_cost": {"class": "seconds"},
}

_FEW_SHOT_ANALYTIC = {
    "plan_ir_version": 1,
    "intent_summary": "Loss-making SKU count for demo/blinkit",
    "nodes": [
        {"id": "n1", "block": "loss_making_skus@1.0", "params": {"brand": "demo", "platform": "blinkit"}},
    ],
    "outputs": {},
    "assumptions": ["read-only analytic lookup"],
    "estimated_cost": {"class": "instant"},
}

# The constructive half of the intent-coverage fix (the coverage-check-and-
# repair guard above is the net under it): a compound ask that asks to
# promote the result gets its own worked example, so the planner doesn't have
# to infer the promote_model node's wiring cold under repair pressure.
_FEW_SHOT_ELASTICITY_AND_PROMOTE = {
    "plan_ir_version": 1,
    "intent_summary": "DML elasticity for demo/blinkit Jan-Feb, gated, and promote to champion",
    "nodes": [
        {
            "id": "n1",
            "block": "panel_builder@1.0",
            "params": {"brand": "demo", "platform": "blinkit", "window": {"from": "2026-01", "to": "2026-02"}},
        },
        {"id": "n2", "block": "elasticity_dml@1.0", "inputs": {"panel": "n1.panel"}},
        {
            "id": "n3",
            "block": "post_model_checks@1.0",
            "inputs": {"surface": "n2.surface"},
            "gate": {"policy": "block"},
        },
        {
            "id": "n4",
            "block": "promote_model@1.0",
            "inputs": {"surface": "n2.surface"},
            "params": {"brand": "demo", "platform": "blinkit", "alias": "champion"},
        },
    ],
    "outputs": {"surface": "n2.surface", "report": "n4.report"},
    "assumptions": ["promote_model is mutate_state; it is left in the plan and gated for approval, never omitted"],
    "estimated_cost": {"class": "seconds"},
}

_PLANNER_RULES = (
    "You are the planner for GobbleCube's Demand Intelligence system. Compose a Plan IR "
    "(a typed DAG of catalog blocks) that fulfills the ask below.\n"
    "Hard rules:\n"
    "- Use ONLY blocks listed in the capability digest below, referenced as name@version. "
    "Never invent a block or a version.\n"
    "- Every `model`-kind node MUST have a downstream node whose block is post_model_checks "
    "with gate: {policy: block} — a model node without this is invalid by construction.\n"
    "- Node params must match each block's params_schema (shown per-block in the digest via "
    "when_to_use/when_not_to_use plus its declared ports).\n"
    "- Output ONLY the Plan IR as JSON — no prose, no markdown fence."
)


def _render_planner_prompt(digest_text: str, classify: ClassifyResult, raw_text: str) -> str:
    normalized_json = classify.normalized.model_dump_json()
    examples = (
        f"Example (multi-node pipeline):\n{json.dumps(_FEW_SHOT_ELASTICITY_E2E, separators=(',', ':'))}\n\n"
        f"Example (single-node analytic):\n{json.dumps(_FEW_SHOT_ANALYTIC, separators=(',', ':'))}\n\n"
        f"Example (pipeline + promotion — never drop the promote_model node just because it needs "
        f"approval; leave it in the plan, gated):\n"
        f"{json.dumps(_FEW_SHOT_ELASTICITY_AND_PROMOTE, separators=(',', ':'))}"
    )
    return (
        f"{_PLANNER_RULES}\n\n"
        f"Capability digest:\n{digest_text}\n\n"
        f"{examples}\n\n"
        f"Normalized ask: {normalized_json}\n"
        f'Raw ask text: "{raw_text}"\n'
        "Plan IR JSON:"
    )


def _render_repair_section(errors: list[PlanValidationError]) -> str:
    error_lines = "\n".join(f"- [{error.code}] node={error.node_id}: {error.message}" for error in errors)
    return (
        "Your previous Plan IR failed validation with these errors:\n"
        f"{error_lines}\n\n"
        "Emit a corrected Plan IR that fixes every error above. Output ONLY the JSON, nothing else."
    )


def _required_kinds_for_constraints(constraints: list[str]) -> set[str]:
    """The set of catalog block *kinds* a compound ask structurally requires,
    derived from its `compound:<verb>` constraint tokens (see
    core.classify.compound) — empty for the overwhelming majority of asks,
    which name no compound directive at all."""
    return {
        _COMPOUND_TOKEN_REQUIRED_KIND[token]
        for token in constraints
        if token in _COMPOUND_TOKEN_REQUIRED_KIND
    }


def _plan_kinds(session: Session, plan: PlanIR) -> set[str]:
    kinds: set[str] = set()
    for node in plan.nodes:
        block_version = resolve_block(session, node.block)
        if block_version is not None:
            kinds.add(block_version.block.kind)
    return kinds


def _missing_required_kinds(session: Session, plan: PlanIR, required_kinds: set[str]) -> set[str]:
    """Which of `required_kinds` the plan has NO node of — empty means the
    plan structurally covers every compound directive the ask named
    (independent of `ValidationResult.valid`: a plan whose only failure is
    `sideeffect_policy` on the node satisfying a requirement, e.g. a
    `promote_model` node pending the two-person approval flow, still
    COVERS the intent — that's a legitimate gated-pending state, not an
    under-served ask)."""
    if not required_kinds:
        return set()
    return required_kinds - _plan_kinds(session, plan)


def _render_intent_coverage_repair_section(constraints: list[str], missing_kinds: set[str]) -> str:
    missing_lines = "\n".join(
        f"- the ask requires a {kind}-kind node (constraint {token}); the plan does not include one — "
        "add it, correctly wired"
        for token in sorted(constraints)
        if token in _COMPOUND_TOKEN_REQUIRED_KIND
        for kind in (_COMPOUND_TOKEN_REQUIRED_KIND[token],)
        if kind in missing_kinds
    )
    return (
        "Your previous Plan IR does not fully serve the ask:\n"
        f"{missing_lines}\n\n"
        "Emit a corrected Plan IR that adds the missing node(s) above, wired to the appropriate "
        "upstream output — do not drop any node already present. Output ONLY the JSON, nothing else."
    )


def _intent_unserved_validation(constraints: list[str], missing_kinds: set[str]) -> ValidationResult:
    errors = [
        PlanValidationError(
            code="intent_unserved",
            message=f"the ask requires a {kind}-kind node (constraint {token}) that the plan does not include",
            details={"token": token, "kind": kind},
        )
        for token in sorted(constraints)
        if token in _COMPOUND_TOKEN_REQUIRED_KIND
        for kind in (_COMPOUND_TOKEN_REQUIRED_KIND[token],)
        if kind in missing_kinds
    ]
    return ValidationResult(valid=False, errors=errors)


def _try_validate(session: Session, tenant_id: UUID, parsed: dict[str, Any]) -> tuple[PlanIR | None, ValidationResult]:
    try:
        plan = PlanIR.model_validate(parsed)
    except PydanticValidationError as exc:
        return None, ValidationResult(
            valid=False,
            errors=[PlanValidationError(code="malformed_plan_ir", message=str(exc))],
        )
    return plan, validate_plan(session, tenant_id, plan)


def _entity_value_map(entities: Entities) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if entities.brand is not None:
        values["brand"] = entities.brand
    if entities.platform is not None:
        values["platform"] = entities.platform
    if entities.window is not None:
        values["window.from"] = entities.window.from_
        values["window.to"] = entities.window.to
    if entities.price_delta_pct is not None:
        values["price_delta_pct"] = entities.price_delta_pct
    if entities.budget is not None:
        values["budget"] = entities.budget
    if entities.metric is not None:
        values["metric"] = entities.metric
    return values


def _templatize_value(value: Any, value_to_slot: dict[Any, str]) -> Any:
    if isinstance(value, dict):
        return {key: _templatize_value(inner, value_to_slot) for key, inner in value.items()}
    if isinstance(value, list):
        return [_templatize_value(inner, value_to_slot) for inner in value]
    return value_to_slot.get(value, value)


def templatize_plan(plan: PlanIR, entities: Entities) -> dict[str, Any]:
    """Straight value->slot substitution over node params for every
    recognized entity value (spec §5.4) — the template stored is otherwise
    the validated plan verbatim."""
    entity_values = _entity_value_map(entities)
    value_to_slot = {value: f"{_SLOT_PREFIX}{key}" for key, value in entity_values.items()}

    dumped = plan.model_dump(mode="json", by_alias=True)
    for node in dumped["nodes"]:
        node["params"] = _templatize_value(node.get("params", {}), value_to_slot)
    return dumped


def _bind_value(value: Any, entity_values: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _bind_value(inner, entity_values) for key, inner in value.items()}
    if isinstance(value, list):
        return [_bind_value(inner, entity_values) for inner in value]
    if isinstance(value, str) and value.startswith(_SLOT_PREFIX):
        key = value[len(_SLOT_PREFIX):]
        return entity_values.get(key, value)
    return value


def bind_template(template_ir: dict[str, Any], entities: Entities) -> PlanIR:
    """The reverse of `templatize_plan`: slot markers bound to this ask's
    current entity values (spec §5.4: "straight substitution of the param
    slots you templated at store time"). A slot with no matching current
    entity is left as-is — the subsequent `validate_plan` call will surface
    that cleanly (e.g. params_invalid) rather than silently guessing."""
    entity_values = _entity_value_map(entities)
    nodes = [
        {**node, "params": _bind_value(node.get("params", {}), entity_values)} for node in template_ir["nodes"]
    ]
    return PlanIR.model_validate({**template_ir, "nodes": nodes})


def _lookup_template(session: Session, tenant_id: UUID, normalized_hash_value: str) -> PlanTemplate | None:
    return session.execute(
        select(PlanTemplate).where(
            PlanTemplate.tenant_id == tenant_id,
            PlanTemplate.normalized_hash == normalized_hash_value,
            PlanTemplate.status == "active",
        )
    ).scalar_one_or_none()


def _store_template(
    session: Session, tenant_id: UUID, normalized_hash_value: str, plan: PlanIR, entities: Entities
) -> None:
    templated_ir = templatize_plan(plan, entities)
    existing = _lookup_template(session, tenant_id, normalized_hash_value)
    if existing is not None:
        existing.ir = templated_ir
    else:
        session.add(
            PlanTemplate(
                tenant_id=tenant_id,
                normalized_hash=normalized_hash_value,
                ir=templated_ir,
                hit_count=0,
                status="active",
            )
        )
    session.flush()


class PlanOutcome(BaseModel):
    plan: PlanIR | None
    validation: ValidationResult | None
    source: Literal["template_cache", "llm", "skipped"]
    attempts: int
    latency_ms: int


def plan_ask(
    session: Session,
    tenant_id: str | UUID,
    classify: ClassifyResult,
    raw_text: str,
) -> PlanOutcome:
    start = time.monotonic()
    tenant_uuid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))

    if classify.normalized.task_type == "unknown":
        # The classifier already told us this ask doesn't map to anything we
        # can do (spec §5.3 out-of-domain routing) — never spend a planner
        # LLM call composing a plan for it. `compose_answer`'s no-plan branch
        # renders this gracefully from `classify.normalized.ambiguities`.
        return PlanOutcome(
            plan=None, validation=None, source="skipped", attempts=0, latency_ms=int((time.monotonic() - start) * 1000)
        )

    required_kinds = _required_kinds_for_constraints(classify.normalized.constraints)

    template = _lookup_template(session, tenant_uuid, classify.normalized_hash)
    if template is not None:
        plan = bind_template(template.ir, classify.normalized.entities)
        validation = validate_plan(session, tenant_uuid, plan)
        if not _missing_required_kinds(session, plan, required_kinds):
            template.hit_count += 1
            session.flush()
            latency_ms = int((time.monotonic() - start) * 1000)
            return PlanOutcome(
                plan=plan, validation=validation, source="template_cache", attempts=0, latency_ms=latency_ms
            )
        # A cached template can only ever have been stored while fully valid
        # (below), which is impossible for any plan containing a mutate-class
        # node (sideeffect_policy always fires without an approver role) — so
        # a stored template NEVER covers a `promotion`-kind requirement, and
        # `normalized_hash` separates compound asks only by constraint COUNT,
        # not identity, so a different compound verb sharing the same shape
        # could otherwise hit a template that structurally can't serve it.
        # Treat this exactly like a cache miss (no hit_count bump) and fall
        # through to plan fresh below, rather than silently under-serving.

    gateway = get_gateway()
    digest = render_digest(session, tenant_uuid)
    prompt = _render_planner_prompt(digest.text, classify, raw_text)
    model = resolve_model("plan")

    attempts = 0
    parsed = gateway.complete_structured(
        session,
        tenant_uuid,
        call_class="plan",
        prompt=prompt,
        schema=_PLAN_IR_JSON_SCHEMA,
        model=model,
        max_tokens=1500,
    )
    attempts += 1
    plan, validation = _try_validate(session, tenant_uuid, parsed)

    if not validation.valid:
        repair_prompt = f"{prompt}\n\n{_render_repair_section(validation.errors)}"
        parsed = gateway.complete_structured(
            session,
            tenant_uuid,
            call_class="plan",
            prompt=repair_prompt,
            schema=_PLAN_IR_JSON_SCHEMA,
            model=model,
            max_tokens=1500,
        )
        attempts += 1
        plan, validation = _try_validate(session, tenant_uuid, parsed)

    # Intent-coverage guard (third layer of the compound-ask bug, see the
    # module docstring above): independent of `validation.valid` — a plan can
    # be structurally sound but still under-serve a compound ask (or vice
    # versa: correctly cover it while merely pending mutate-node approval).
    # Only ever spends ONE extra repair round, reusing the same mechanism.
    # Skipped entirely when `plan is None` (the existing repair budget was
    # already exhausted on an unrelated, already-reported failure — e.g.
    # malformed JSON — piling on an intent-coverage repair for that case
    # would misattribute the real error as "intent_unserved").
    missing_kinds = _missing_required_kinds(session, plan, required_kinds) if plan is not None else set()
    if missing_kinds:
        coverage_prompt = f"{prompt}\n\n{_render_intent_coverage_repair_section(classify.normalized.constraints, missing_kinds)}"
        parsed = gateway.complete_structured(
            session,
            tenant_uuid,
            call_class="plan",
            prompt=coverage_prompt,
            schema=_PLAN_IR_JSON_SCHEMA,
            model=model,
            max_tokens=1500,
        )
        attempts += 1
        plan, validation = _try_validate(session, tenant_uuid, parsed)
        missing_kinds = _missing_required_kinds(session, plan, required_kinds) if plan is not None else required_kinds

        if missing_kinds:
            # Still unserved: never hand back a plan (however "valid" by
            # validate_plan's own rules) that silently drops what was asked
            # for — fail honestly instead (spec §5.6 "never silently loop,
            # never auto-fix"; the API's existing no-plan branch renders this
            # as a plain-text failure to compose a full plan).
            latency_ms = int((time.monotonic() - start) * 1000)
            return PlanOutcome(
                plan=None,
                validation=_intent_unserved_validation(classify.normalized.constraints, missing_kinds),
                source="llm",
                attempts=attempts,
                latency_ms=latency_ms,
            )

    if validation.valid and plan is not None:
        _store_template(session, tenant_uuid, classify.normalized_hash, plan, classify.normalized.entities)

    latency_ms = int((time.monotonic() - start) * 1000)
    return PlanOutcome(plan=plan, validation=validation, source="llm", attempts=attempts, latency_ms=latency_ms)
