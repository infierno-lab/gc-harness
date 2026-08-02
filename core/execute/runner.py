"""In-process DAG runner (spec §3.2, §9, §10) — instant/seconds plans, no LLM
anywhere in this path. A failed node halts the run; diagnosis is a new ask.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from core.catalog.hashing import content_hash
from core.db.models import (
    Ask,
    AuditEvent,
    Block,
    BlockIO,
    BlockVersion,
    GateBinding,
    GateVerdict,
    NodeExecution,
    Plan,
    Run,
)
from core.execute.adapter import BlockContext, Storage, resolve_entrypoint
from core.execute.envelope import Envelope
from core.plan.plan_ir import Node, PlanIR, ir_hash
from core.plan.validator import ValidationError, validate_plan

RUNS_DIR = Path(".gc_runs")


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: Literal["succeeded", "failed"]
    node_statuses: dict[str, str]
    outputs: dict[str, Envelope]
    input_hashes: dict[str, dict[str, str]]
    output_hashes: dict[str, dict[str, str]]
    # Set only when the outer catch-all fired (a harness/runner bug, not a
    # domain failure like a block exception or a gate condemning its input) —
    # lets callers/monitoring branch on "the engine broke" vs "the plan/data
    # failed" without parsing NodeExecution.error strings.
    internal_error: str | None = None


class PlanRejectedError(Exception):
    """Raised when execute_plan's fail-closed validation gate (spec invariant
    #6, §11: never skip the plan validator) rejects a plan. No Run row is
    created — the rejection itself is fully audited via the Ask/Plan (status
    'invalid', validation_errors populated) and AuditEvent rows created before
    raising, all committed by the caller's session."""

    def __init__(self, plan_id: uuid.UUID, errors: list[ValidationError]) -> None:
        self.plan_id = plan_id
        self.errors = errors
        codes = sorted({error.code for error in errors})
        super().__init__(f"plan {plan_id} rejected by validator: {codes}")


def _topo_sort(nodes: list[Node]) -> list[Node]:
    """Small local topo sort over input edges — the plan validator owns the
    canonical version; this is a one-line duplication for the runner's own use."""
    by_id = {node.id: node for node in nodes}
    for node in nodes:
        for ref in node.inputs.values():
            dep_id = ref.split(".", 1)[0]
            if dep_id not in by_id:
                raise ValueError(f"node {node.id!r} references unknown node {dep_id!r} (input {ref!r})")

    ordered: list[Node] = []
    resolved: set[str] = set()
    visiting: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in resolved:
            return
        if node_id in visiting:
            raise ValueError(f"cycle detected in plan DAG at node {node_id!r}")
        visiting.add(node_id)
        for ref in by_id[node_id].inputs.values():
            visit(ref.split(".", 1)[0])
        visiting.discard(node_id)
        resolved.add(node_id)
        ordered.append(by_id[node_id])

    for node in nodes:
        visit(node.id)
    return ordered


def _resolve_block_version(session: Session, block_ref: str) -> tuple[Block, BlockVersion]:
    name, sep, version = block_ref.rpartition("@")
    if not sep or not name or not version:
        raise ValueError(f"invalid block reference {block_ref!r}, expected 'name@version'")
    block = session.query(Block).filter_by(name=name).one_or_none()
    if block is None:
        raise ValueError(f"unknown block {name!r} (from {block_ref!r})")
    block_version = session.query(BlockVersion).filter_by(block_id=block.id, version=version).one_or_none()
    if block_version is None:
        raise ValueError(f"unknown version {version!r} for block {name!r}")
    if block_version.status != "active":
        # Defense-in-depth (spec §4.3: contracts/blocks enforced twice) — the
        # plan validator already rejects inactive blocks before execute_plan
        # ever reaches this point.
        raise ValueError(f"block {block_ref!r} is not active (status={block_version.status!r})")
    return block, block_version


def _resolve_active_block_version(session: Session, block_name: str) -> tuple[Block, BlockVersion]:
    block = session.query(Block).filter_by(name=block_name).one_or_none()
    if block is None:
        raise ValueError(f"unknown check_suite block {block_name!r}")
    block_version = (
        session.query(BlockVersion)
        .filter_by(block_id=block.id, status="active")
        .order_by(BlockVersion.version.desc())
        .first()
    )
    if block_version is None:
        raise ValueError(f"no active version for check_suite block {block_name!r}")
    return block, block_version


def _port_mapping(session: Session, produces_bv_id: uuid.UUID, consumes_bv_id: uuid.UUID) -> dict[str, str]:
    """consume_port -> produce_port, matched by the contract shared between the
    gated node's outputs and the gate block's declared inputs."""
    produces = session.query(BlockIO).filter_by(block_version_id=produces_bv_id, role="produces").all()
    consumes = session.query(BlockIO).filter_by(block_version_id=consumes_bv_id, role="consumes").all()
    mapping: dict[str, str] = {}
    for consume in consumes:
        match = next((p for p in produces if p.contract_id == consume.contract_id), None)
        if match is not None:
            mapping[consume.port_name] = match.port_name
    return mapping


def _has_explicit_downstream_gate(plan: PlanIR, node_id: str, check_suite: str) -> bool:
    """True when the Plan IR itself already contains a validation node (by
    block name == check_suite) that directly consumes an output of `node_id`.
    When it does, that node IS the gate (its own node-level `gate` governs) —
    the catalog's auto-invoke path must defer to it rather than double-run
    the check suite."""
    for other in plan.nodes:
        if other.id == node_id:
            continue
        if other.block.rpartition("@")[0] != check_suite:
            continue
        if any(ref.split(".", 1)[0] == node_id for ref in other.inputs.values()):
            return True
    return False


def _extract_verdict_payload(storage: Storage, outputs: dict[str, Envelope]) -> dict[str, Any] | None:
    """The first stored output payload shaped like a validation_verdicts@v1
    handle (i.e. carrying an "overall" verdict), or None if this node's
    outputs aren't verdict-shaped."""
    for env in outputs.values():
        payload = storage.get(env.ref)
        if isinstance(payload, dict) and "overall" in payload:
            return payload
    return None


def _strictest_policy(policies: list[str]) -> str:
    return "block" if "block" in policies else "warn"


def execute_plan(
    session: Session,
    tenant_id: str | uuid.UUID,
    plan: PlanIR,
    *,
    actor: str,
    actor_roles: list[str] = (),
    budget_cost_class: str = "hours",
) -> RunResult:
    """Validate, then run, a Plan IR (spec §3.2, §9, §10) — never the reverse.

    Fails closed (spec invariant #6, §11 anti-pattern "skipping the plan
    validator"): `validate_plan` runs first; an invalid plan raises
    `PlanRejectedError` and creates no Run row at all (only Ask/Plan rows
    recording the rejection, plus an AuditEvent — no silent bypass, no flag
    to skip this).
    """
    tenant_uuid = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
    plan_dump = plan.model_dump(mode="json", by_alias=True)

    validation = validate_plan(session, tenant_uuid, plan, actor_roles=actor_roles, budget_cost_class=budget_cost_class)
    if not validation.valid:
        ask = Ask(tenant_id=tenant_uuid, actor=actor, raw_text=plan.intent_summary)
        session.add(ask)
        session.flush()

        plan_row = Plan(
            tenant_id=tenant_uuid,
            ask_id=ask.id,
            ir=plan_dump,
            ir_hash=ir_hash(plan),
            status="invalid",
            validation_errors={"errors": [error.model_dump() for error in validation.errors]},
        )
        session.add(plan_row)
        session.flush()

        session.add(
            AuditEvent(
                tenant_id=tenant_uuid,
                actor=actor,
                action="plan.rejected",
                subject=str(plan_row.id),
                details={"errors": [error.model_dump() for error in validation.errors]},
            )
        )
        raise PlanRejectedError(plan_row.id, validation.errors)

    ordered_nodes = _topo_sort(plan.nodes)

    ask = Ask(tenant_id=tenant_uuid, actor=actor, raw_text=plan.intent_summary)
    session.add(ask)
    session.flush()

    plan_row = Plan(
        tenant_id=tenant_uuid,
        ask_id=ask.id,
        ir=plan_dump,
        ir_hash=ir_hash(plan),
        status="running",
    )
    session.add(plan_row)
    session.flush()

    run = Run(
        tenant_id=tenant_uuid,
        plan_id=plan_row.id,
        status="running",
        started_at=datetime.now(UTC),
        params=plan_dump,
    )
    session.add(run)
    session.flush()
    run_id = run.id

    session.add(
        AuditEvent(
            tenant_id=tenant_uuid,
            actor=actor,
            action="run.start",
            subject=str(run_id),
            details={"plan_ir_hash": plan_row.ir_hash},
        )
    )

    run_dir = RUNS_DIR / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    storage = Storage(run_dir)

    node_statuses: dict[str, str] = {node.id: "pending" for node in plan.nodes}
    node_outputs: dict[str, dict[str, Envelope]] = {}
    resolved_block_versions: dict[str, BlockVersion] = {}
    all_input_hashes: dict[str, dict[str, str]] = {}
    all_output_hashes: dict[str, dict[str, str]] = {}

    run_status: Literal["succeeded", "failed"] = "succeeded"
    halted = False
    crashed_node: Node | None = None
    internal_error: str | None = None

    try:
        for node in ordered_nodes:
            node_exec_row: NodeExecution | None = None

            if halted:
                node_statuses[node.id] = "skipped"
                session.add(
                    NodeExecution(
                        run_id=run_id,
                        tenant_id=tenant_uuid,
                        node_id=node.id,
                        block_version=node.block,
                        status="skipped",
                    )
                )
                continue

            node_statuses[node.id] = "running"
            started_at = datetime.now(UTC)

            try:
                block, block_version = _resolve_block_version(session, node.block)
            except ValueError as exc:
                session.add(
                    NodeExecution(
                        run_id=run_id,
                        tenant_id=tenant_uuid,
                        node_id=node.id,
                        block_version=node.block,
                        status="failed",
                        started_at=started_at,
                        finished_at=datetime.now(UTC),
                        error=str(exc),
                    )
                )
                node_statuses[node.id] = "failed"
                run_status = "failed"
                halted = True
                continue

            resolved_inputs: dict[str, Envelope] = {}
            input_error: str | None = None
            for port, ref in node.inputs.items():
                dep_node_id, _, dep_port = ref.partition(".")
                dep_outputs = node_outputs.get(dep_node_id, {})
                if dep_port not in dep_outputs:
                    input_error = f"node {node.id!r} input {port!r} references missing output {ref!r}"
                    break
                resolved_inputs[port] = dep_outputs[dep_port]

            if input_error is not None:
                session.add(
                    NodeExecution(
                        run_id=run_id,
                        tenant_id=tenant_uuid,
                        node_id=node.id,
                        block_version=f"{block.name}@{block_version.version}",
                        status="failed",
                        started_at=started_at,
                        finished_at=datetime.now(UTC),
                        error=input_error,
                    )
                )
                node_statuses[node.id] = "failed"
                run_status = "failed"
                halted = True
                continue

            input_hashes = {port: content_hash(env) for port, env in resolved_inputs.items()}
            all_input_hashes[node.id] = input_hashes

            ctx = BlockContext(
                tenant_id=str(tenant_uuid),
                run_id=str(run_id),
                node_id=node.id,
                block_version=f"{block.name}@{block_version.version}",
                run_dir=run_dir,
                storage=storage,
                actor=actor,
            )

            try:
                fn = resolve_entrypoint(block_version.entrypoint)
                outputs = fn(node.params, resolved_inputs, ctx)
            except Exception as exc:  # spec §10: no retries, no improvisation — record verbatim and halt
                session.add(
                    NodeExecution(
                        run_id=run_id,
                        tenant_id=tenant_uuid,
                        node_id=node.id,
                        block_version=ctx.block_version,
                        status="failed",
                        input_hashes=input_hashes,
                        started_at=started_at,
                        finished_at=datetime.now(UTC),
                        error=repr(exc),
                    )
                )
                node_statuses[node.id] = "failed"
                run_status = "failed"
                halted = True
                continue

            output_hashes = {port: content_hash(env) for port, env in outputs.items()}
            all_output_hashes[node.id] = output_hashes
            node_outputs[node.id] = outputs
            resolved_block_versions[node.id] = block_version

            node_exec_row = NodeExecution(
                run_id=run_id,
                tenant_id=tenant_uuid,
                node_id=node.id,
                block_version=ctx.block_version,
                status="succeeded",
                input_hashes=input_hashes,
                output_hashes=output_hashes,
                envelope={port: env.model_dump(mode="json", by_alias=True) for port, env in outputs.items()},
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )
            session.add(node_exec_row)
            node_statuses[node.id] = "succeeded"

            gate_failed_here = False

            # Gate path A: this node IS an explicit gate (validation-kind, carries
            # its own `gate` per spec §5.5's worked example) — its own verdict
            # governs, merged with any matching catalog binding's policy (fail-closed:
            # strictest of the two wins).
            if node.gate is not None and block.kind == "validation":
                try:
                    verdict_payload = _extract_verdict_payload(storage, outputs)
                    if verdict_payload is not None:
                        overall = verdict_payload["overall"]
                        policies = [node.gate.policy]
                        for ref in node.inputs.values():
                            upstream_bv = resolved_block_versions.get(ref.split(".", 1)[0])
                            if upstream_bv is None:
                                continue
                            policies.extend(
                                binding.policy
                                for binding in session.query(GateBinding)
                                .filter_by(block_version_id=upstream_bv.id, when="post", check_suite=block.name)
                                .all()
                            )
                        effective_policy = _strictest_policy(policies)
                    else:
                        overall = None
                        effective_policy = None
                except Exception as exc:  # a broken gate must fail closed, never propagate uncaught
                    verdict_payload = {"error": repr(exc)}
                    overall = "fail"
                    effective_policy = "block"

                if overall is not None:
                    session.add(
                        GateVerdict(
                            run_id=run_id,
                            tenant_id=tenant_uuid,
                            node_id=node.id,
                            check_suite=block.name,
                            verdict=overall,
                            details=verdict_payload,
                        )
                    )

                    if effective_policy == "block" and overall == "fail":
                        node_exec_row.status = "gate_failed"
                        node_statuses[node.id] = "gate_failed"
                        node_outputs.pop(node.id, None)
                        run_status = "failed"
                        halted = True
                        gate_failed_here = True

            # Gate path B: catalog-driven defense-in-depth — auto-invoke any post
            # GateBinding declared on this node's own block_version, unless the plan
            # already wires an explicit downstream validation node for it (gate
            # path A on that node will run when its turn comes; no double-invoke).
            if not gate_failed_here:
                gate_bindings = (
                    session.query(GateBinding).filter_by(block_version_id=block_version.id, when="post").all()
                )
                for binding in gate_bindings:
                    if _has_explicit_downstream_gate(plan, node.id, binding.check_suite):
                        continue
                    policy = node.gate.policy if node.gate is not None else binding.policy
                    try:
                        gate_block, gate_block_version = _resolve_active_block_version(session, binding.check_suite)
                        port_map = _port_mapping(session, block_version.id, gate_block_version.id)
                        gate_inputs = {
                            consume_port: outputs[produce_port]
                            for consume_port, produce_port in port_map.items()
                            if produce_port in outputs
                        }
                        gate_ctx = BlockContext(
                            tenant_id=str(tenant_uuid),
                            run_id=str(run_id),
                            node_id=node.id,
                            block_version=f"{gate_block.name}@{gate_block_version.version}",
                            run_dir=run_dir,
                            storage=storage,
                            actor=actor,
                        )
                        gate_fn = resolve_entrypoint(gate_block_version.entrypoint)
                        gate_outputs = gate_fn({}, gate_inputs, gate_ctx)
                        verdict_payload = None
                        for env in gate_outputs.values():
                            verdict_payload = storage.get(env.ref)
                            break
                        overall = verdict_payload["overall"] if verdict_payload is not None else "fail"
                        details = verdict_payload
                    except Exception as exc:  # a broken gate must fail closed, never pass silently
                        overall = "fail"
                        details = {"error": repr(exc)}

                    session.add(
                        GateVerdict(
                            run_id=run_id,
                            tenant_id=tenant_uuid,
                            node_id=node.id,
                            check_suite=binding.check_suite,
                            verdict=overall,
                            details=details,
                        )
                    )

                    if policy == "block" and overall == "fail":
                        node_exec_row.status = "gate_failed"
                        node_statuses[node.id] = "gate_failed"
                        node_outputs.pop(node.id, None)
                        run_status = "failed"
                        halted = True
                        break
    except Exception as exc:
        # Runner-internal crash outside the per-node/gate guards above (e.g. a
        # DB error mid-gate-lookup, a bug in a helper like _has_explicit_
        # downstream_gate) — distinct from a domain failure (block exception,
        # gate condemning its input), both of which are already caught by the
        # inner try/excepts and never reach here. Never let this roll back the
        # audit trail: record it verbatim and let the caller's session commit
        # the failure — do NOT re-raise (would roll back) and do NOT commit
        # here (SET LOCAL tenant scoping is transaction-scoped; a mid-flight
        # commit would drop it for these very writes).
        run_status = "failed"
        internal_error = f"RUNNER_INTERNAL: {exc!r}"
        crashed_node = node
        if node_exec_row is not None:
            node_exec_row.status = "failed"
            node_exec_row.error = internal_error
            node_exec_row.finished_at = datetime.now(UTC)
        else:
            session.add(
                NodeExecution(
                    run_id=run_id,
                    tenant_id=tenant_uuid,
                    node_id=node.id,
                    block_version=node.block,
                    status="failed",
                    error=internal_error,
                    finished_at=datetime.now(UTC),
                )
            )
        node_statuses[node.id] = "failed"
        node_outputs.pop(node.id, None)

        # Relies on validate_plan's duplicate_node_id rejection to guarantee
        # every node in ordered_nodes has a distinct id (Node equality is by
        # value, not identity — a byte-identical duplicate elsewhere in the
        # list would make .index() resolve to the wrong position).
        crashed_index = ordered_nodes.index(crashed_node)
        for remaining in ordered_nodes[crashed_index + 1 :]:
            node_statuses[remaining.id] = "skipped"
            session.add(
                NodeExecution(
                    run_id=run_id,
                    tenant_id=tenant_uuid,
                    node_id=remaining.id,
                    block_version=remaining.block,
                    status="skipped",
                )
            )

    run.status = run_status
    run.finished_at = datetime.now(UTC)
    plan_row.status = run_status
    audit_action = "run.crashed" if internal_error is not None else f"run.{run_status}"
    session.add(AuditEvent(tenant_id=tenant_uuid, actor=actor, action=audit_action, subject=str(run_id)))

    plan_outputs: dict[str, Envelope] = {}
    if run_status == "succeeded":
        for out_name, ref in plan.outputs.items():
            node_id, _, port = ref.partition(".")
            plan_outputs[out_name] = node_outputs[node_id][port]

    return RunResult(
        run_id=str(run_id),
        status=run_status,
        node_statuses=node_statuses,
        outputs=plan_outputs,
        input_hashes=all_input_hashes,
        output_hashes=all_output_hashes,
        internal_error=internal_error,
    )
