"""Reproducibility spot-check (spec §9, §5.4) — "Scheduled reproducibility
spot-check: re-execute a sample of yesterday's plans, diff output hashes."

Takes a sample of a tenant's most recently succeeded Runs, re-executes each
one's Plan IR through the same `execute_plan` path used for a live ask, and
diffs the freshly produced node output hashes against the hashes durably
recorded on the *original* run's NodeExecution rows. Any divergence is a loud,
audited event (`spot_check.MISMATCH`) — never a silently swallowed detail.

Honest boundary (spec §5.4): today's demo blocks (panel_builder,
elasticity_dml, ...) are pure functions of their `params` — no read from a
live mart — so replay-equality is expected and byte-identical hashes are the
correct outcome. Once a block reads from a live data mart, a naive replay
would legitimately diverge as the underlying data moves forward in time, and
"mismatch" would stop meaning "reproducibility broke" and start meaning
"the world changed since yesterday". At that point replay must pin the
data-version stamp the original run captured (spec §5.4) and pass it back in
so the block reads the *same* mart snapshot — this module is where that
pinning will be threaded through once such a stamp exists; there is no stamp
to pin yet, so this is a deliberate gap, not an oversight.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.db.models import AuditEvent, NodeExecution, Plan, Run
from core.execute.runner import PlanRejectedError, execute_plan
from core.plan.plan_ir import PlanIR

# Candidate runs are fetched with a generous multiplier over sample_size
# before plan-hash dedup narrows them down, so a tenant whose recent history
# is dominated by repeats of one plan doesn't starve the sample — but the
# fetch itself still stays bounded rather than scanning the tenant's entire
# run history.
_CANDIDATE_FETCH_MULTIPLIER = 20
_MIN_CANDIDATE_FETCH = 50


class HashMismatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    port: str
    original_hash: str | None
    replay_hash: str | None


class SpotCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_run_id: str
    replay_run_id: str
    plan_ir_hash: str
    match: bool
    mismatches: list[HashMismatch]


class SkippedRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    reason: str


class SpotCheckReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checked: list[SpotCheckResult]
    all_match: bool
    skipped: list[SkippedRun]


def _diff_output_hashes(
    original: dict[str, dict[str, str]], replay: dict[str, dict[str, str]]
) -> list[HashMismatch]:
    mismatches: list[HashMismatch] = []
    for node_id in sorted(set(original) | set(replay)):
        original_ports = original.get(node_id, {})
        replay_ports = replay.get(node_id, {})
        for port in sorted(set(original_ports) | set(replay_ports)):
            original_hash = original_ports.get(port)
            replay_hash = replay_ports.get(port)
            if original_hash != replay_hash:
                mismatches.append(
                    HashMismatch(
                        node_id=node_id,
                        port=port,
                        original_hash=original_hash,
                        replay_hash=replay_hash,
                    )
                )
    return mismatches


def run_spot_check(
    session: Session,
    tenant_id: str | uuid.UUID,
    *,
    sample_size: int = 3,
    since: datetime | None = None,
    actor: str = "spot-check",
) -> SpotCheckReport:
    """Re-execute up to `sample_size` distinct-plan succeeded Runs (most
    recent first) for `tenant_id` and diff replay output hashes against the
    hashes recorded on the original run. `since`, if given, restricts the
    candidate pool to runs started at or after that timestamp.

    Never raises for a single bad candidate — a missing Plan row, an
    unparseable Plan IR, or a validator rejection on replay is recorded in
    `skipped` and the sweep continues. Writes exactly one AuditEvent
    summarizing the whole sweep.
    """
    tenant_uuid = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))

    fetch_limit = max(sample_size * _CANDIDATE_FETCH_MULTIPLIER, _MIN_CANDIDATE_FETCH)
    candidate_runs = (
        session.execute(
            select(Run)
            .where(Run.tenant_id == tenant_uuid, Run.status == "succeeded")
            .order_by(Run.started_at.desc().nullslast())
            .limit(fetch_limit)
        )
        .scalars()
        .all()
    )

    checked: list[SpotCheckResult] = []
    skipped: list[SkippedRun] = []
    seen_plan_ir_hashes: set[str] = set()

    for run in candidate_runs:
        if len(checked) >= sample_size:
            break

        plan_row = session.query(Plan).filter_by(id=run.plan_id).one_or_none()
        if plan_row is None:
            skipped.append(SkippedRun(run_id=str(run.id), reason="plan row missing"))
            continue

        if plan_row.ir_hash in seen_plan_ir_hashes:
            continue

        try:
            plan = PlanIR.model_validate(plan_row.ir)
        except Exception as exc:  # a corrupt/unparseable stored IR must never crash the sweep
            skipped.append(SkippedRun(run_id=str(run.id), reason=f"plan IR failed to parse: {exc!r}"))
            continue

        original_output_hashes = {
            node_exec.node_id: node_exec.output_hashes
            for node_exec in session.execute(
                select(NodeExecution).where(NodeExecution.run_id == run.id)
            )
            .scalars()
            .all()
            if node_exec.output_hashes
        }

        seen_plan_ir_hashes.add(plan_row.ir_hash)

        try:
            # actor_roles=["approver"]: the spot-check re-runs already-approved
            # work — a plan that originally required a named approver to run
            # once must not be blocked from replay just because this sweep's
            # actor isn't that approver.
            replay = execute_plan(session, tenant_uuid, plan, actor=actor, actor_roles=["approver"])
        except PlanRejectedError as exc:
            codes = sorted({error.code for error in exc.errors})
            skipped.append(SkippedRun(run_id=str(run.id), reason=f"replay rejected by validator: {codes}"))
            continue

        mismatches = _diff_output_hashes(original_output_hashes, replay.output_hashes)
        checked.append(
            SpotCheckResult(
                original_run_id=str(run.id),
                replay_run_id=replay.run_id,
                plan_ir_hash=plan_row.ir_hash,
                match=not mismatches,
                mismatches=mismatches,
            )
        )

    all_match = all(result.match for result in checked)
    report = SpotCheckReport(checked=checked, all_match=all_match, skipped=skipped)

    action: Literal["spot_check.completed", "spot_check.MISMATCH"] = (
        "spot_check.completed" if all_match else "spot_check.MISMATCH"
    )
    session.add(
        AuditEvent(
            tenant_id=tenant_uuid,
            actor=actor,
            action=action,
            details=report.model_dump(mode="json"),
        )
    )

    return report
