"""Repeat-ask latency probe (spec §5.9, §12 Phase-3 exit metric): times the
LLM-free hot path — `classify_ask` (stage-0 rules) -> `plan_ask` (L2
template-cache hit) -> `execute_plan` — for a repeat of an ask the system has
already seen once, against the ~1.5 s message-to-execution-start SLO.

Every stage in this path is required to be LLM-free by construction: the
probe raises `AssertionError` if the ask falls through to the stage-2
classifier LLM, or if the plan misses the template cache, rather than
silently measuring a slower path and reporting a misleading number.
"""

import time
from datetime import date
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.classify.classifier import ClassifyResult, classify_ask
from core.db.models import PlanTemplate
from core.execute.runner import execute_plan
from core.plan.planner import plan_ask

SLO_MS = 1500

# A stage-0-classifiable ask (matches the anchored "how many...loss" rule in
# core/classify/rules.py — "SKUs made losses", not just any sentence
# containing the word "loss", per that rule's bounded end-to-end shape) over
# the demo pack's single-node `loss_making_skus` block — the simplest
# LLM-free, gate-free analytic plan available.
DEFAULT_ASK_TEXT = "How many SKUs made losses for demo on blinkit?"


def _default_template_ir() -> dict[str, Any]:
    return {
        "plan_ir_version": 1,
        "intent_summary": "loss making skus for demo on blinkit",
        "nodes": [
            {
                "id": "n1",
                "block": "loss_making_skus@1.0",
                "params": {"brand": "$slot:brand", "platform": "$slot:platform"},
            }
        ],
        "outputs": {},
        "assumptions": [],
        "estimated_cost": {"class": "instant"},
    }


def seed_plan_template(
    session: Session,
    tenant_id: str | UUID,
    ask_text: str = DEFAULT_ASK_TEXT,
    *,
    reference_date: date | None = None,
) -> ClassifyResult:
    """Classifies `ask_text` (must land on the stage-0 rules ladder) and
    stores a matching `PlanTemplate` row directly, so a subsequent `plan_ask`
    for the same ask is a template-cache hit with zero gateway calls. Caller
    is responsible for granting the tenant the blocks the template
    references (e.g. via `core.catalog.seed.grant_all_blocks`)."""
    tenant_uuid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))

    classify_result = classify_ask(session, tenant_uuid, ask_text, reference_date=reference_date)
    if classify_result.stage != "rules":
        raise AssertionError(
            f"seed ask must be stage-0-classifiable (rules ladder); got stage={classify_result.stage!r} — "
            "pick an ask that matches a rule in core/classify/rules.py"
        )

    existing = session.execute(
        select(PlanTemplate).where(
            PlanTemplate.tenant_id == tenant_uuid,
            PlanTemplate.normalized_hash == classify_result.normalized_hash,
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            PlanTemplate(
                tenant_id=tenant_uuid,
                normalized_hash=classify_result.normalized_hash,
                ir=_default_template_ir(),
                hit_count=0,
                status="active",
            )
        )
        session.flush()
    return classify_result


class ProbeDecomposition(BaseModel):
    classify_p50: float
    plan_p50: float
    execute_p50: float


class ProbeReport(BaseModel):
    p50_ms: float
    p95_ms: float
    samples: list[float]
    decomposition: ProbeDecomposition
    slo_ms: int = SLO_MS
    within_slo: bool


def _percentile(sorted_values: list[float], pct: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * pct
    lower = int(k)
    upper = min(lower + 1, len(sorted_values) - 1)
    frac = k - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * frac


def probe_repeat_ask(
    session: Session,
    tenant_id: str | UUID,
    ask_text: str,
    *,
    n: int = 20,
    reference_date: date | None = None,
    actor: str = "latency-probe",
) -> ProbeReport:
    """Runs `n` repeats of classify -> plan -> execute for `ask_text`, timing
    each stage, and returns a `ProbeReport` decomposed by stage. Every repeat
    must classify via the rules ladder and hit the plan-template cache — a
    real "repeat ask" never touches the gateway anywhere in this path; if it
    does, that's a probe setup bug (missing template) or a mischosen ask
    (doesn't match a stage-0 rule), and it's reported as an `AssertionError`
    rather than silently measured."""
    totals: list[float] = []
    classify_samples: list[float] = []
    plan_samples: list[float] = []
    execute_samples: list[float] = []

    for _ in range(n):
        iteration_start = time.monotonic()

        classify_start = time.monotonic()
        classify_result = classify_ask(session, tenant_id, ask_text, reference_date=reference_date)
        classify_ms = (time.monotonic() - classify_start) * 1000
        if classify_result.stage != "rules":
            raise AssertionError(
                f"probe ask must be stage-0-classifiable (rules ladder); got stage={classify_result.stage!r} — "
                "this probe characterizes the LLM-free repeat-ask path, not the LLM path"
            )

        plan_start = time.monotonic()
        plan_outcome = plan_ask(session, tenant_id, classify_result, ask_text)
        plan_ms = (time.monotonic() - plan_start) * 1000
        if plan_outcome.source != "template_cache":
            raise AssertionError(
                f"probe ask must hit the plan-template cache (L2); got source={plan_outcome.source!r} — "
                "pre-seed a PlanTemplate (see seed_plan_template) before probing"
            )
        if plan_outcome.plan is None or plan_outcome.validation is None or not plan_outcome.validation.valid:
            errors = plan_outcome.validation.errors if plan_outcome.validation is not None else []
            raise AssertionError(
                f"probe ask's cached plan failed validation: {errors} — check the tenant's block grants"
            )

        execute_start = time.monotonic()
        execute_plan(session, tenant_id, plan_outcome.plan, actor=actor)
        execute_ms = (time.monotonic() - execute_start) * 1000

        total_ms = (time.monotonic() - iteration_start) * 1000
        totals.append(total_ms)
        classify_samples.append(classify_ms)
        plan_samples.append(plan_ms)
        execute_samples.append(execute_ms)

    p50 = _percentile(sorted(totals), 0.50)
    p95 = _percentile(sorted(totals), 0.95)

    return ProbeReport(
        p50_ms=p50,
        p95_ms=p95,
        samples=totals,
        decomposition=ProbeDecomposition(
            classify_p50=_percentile(sorted(classify_samples), 0.50),
            plan_p50=_percentile(sorted(plan_samples), 0.50),
            execute_p50=_percentile(sorted(execute_samples), 0.50),
        ),
        within_slo=p50 <= SLO_MS,
    )
