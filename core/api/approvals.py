"""Two-person approval flow for `mutate_state`/`external` plans (spec §4.2,
§8) — the mutate-class counterpart to the auto-run validate/execute path in
`core.api.app`. Kept in its own module so `create_ask`'s happy path stays
readable; every function here is plain code, fully audited, no LLM.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from core.api.answers import compose_answer
from core.db.models import Approval, Ask, AuditEvent, GateVerdict, Plan, Run
from core.db.session import tenant_session
from core.execute.runner import PlanRejectedError, execute_plan
from core.plan.plan_ir import PlanIR, ir_hash
from core.plan.validator import ValidationResult


def classify_approval_need(validation: ValidationResult | None, plan: PlanIR | None) -> list[str] | None:
    """The sorted node ids requiring approval if this plan should be routed
    into the pending-approval flow instead of straight to execute/reject, or
    `None` if it should follow the existing valid/invalid path unchanged.

    `validate_plan` populates `requires_approval` with every mutate_state/
    external node unconditionally (before it even checks `actor_roles`), so
    no re-validation is needed here: if every error the plan carries is
    `sideeffect_policy`, actor_roles=["approver"] is the *only* thing that
    check depends on — clearing those errors is guaranteed by construction,
    not something worth a second `validate_plan` round-trip to confirm.
    """
    if validation is None or plan is None or not validation.requires_approval:
        return None
    if validation.valid:
        return sorted(validation.requires_approval)
    if all(error.code == "sideeffect_policy" for error in validation.errors):
        return sorted(validation.requires_approval)
    return None


def _audit_blocked_self_approval(tenant_id: uuid.UUID, plan_id: uuid.UUID, approval_id: uuid.UUID, approver: str) -> None:
    """Writes the audit row in its own short-lived transaction so it survives
    regardless of what happens to the caller's request-scoped session next
    (it raises an `HTTPException` right after this, which would otherwise
    roll back anything added to that shared session — see `decide_approval`).
    """
    with tenant_session(tenant_id) as audit_session:
        audit_session.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor=approver,
                action="plan.approval_self_attempt_blocked",
                subject=str(plan_id),
                details={"approval_id": str(approval_id)},
            )
        )


def open_approval(
    session: Session,
    tenant_id: uuid.UUID,
    actor: str,
    raw_text: str,
    plan: PlanIR,
    nodes: list[str],
) -> Approval:
    """Persists Ask + Plan(status='proposed') + Approval(status='pending') +
    an audit event, all inside the caller's tenant-scoped session — mirrors
    the row shapes `execute_plan` itself writes on the invalid path, since
    this plan is deliberately never handed to `execute_plan` until approved.
    """
    ask_row = Ask(tenant_id=tenant_id, actor=actor, raw_text=raw_text)
    session.add(ask_row)
    session.flush()

    plan_row = Plan(
        tenant_id=tenant_id,
        ask_id=ask_row.id,
        ir=plan.model_dump(mode="json", by_alias=True),
        ir_hash=ir_hash(plan),
        status="proposed",
    )
    session.add(plan_row)
    session.flush()

    approval_row = Approval(
        tenant_id=tenant_id,
        plan_id=plan_row.id,
        requested_by=actor,
        status="pending",
    )
    session.add(approval_row)
    session.flush()

    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor=actor,
            action="plan.approval_requested",
            subject=str(plan_row.id),
            details={"approval_id": str(approval_row.id), "nodes": nodes},
        )
    )
    return approval_row


def pending_approval_answer(approval: Approval, nodes: list[str]) -> dict[str, Any]:
    text = (
        f"This plan needs approval before it can run: node(s) {', '.join(nodes)} perform a mutating action and "
        f"require a named approver who is not the requester. Submit a decision via "
        f"POST /api/approvals/{approval.id} (approval id {approval.id})."
    )
    return {
        "text": text,
        "numbers_provenance": {"plan_id": str(approval.plan_id), "approval_id": str(approval.id)},
    }


def _gate_verdicts_for(session: Session, run_id: uuid.UUID) -> list[dict[str, Any]]:
    verdicts = session.query(GateVerdict).filter_by(run_id=run_id).all()
    return [
        {"node_id": v.node_id, "check_suite": v.check_suite, "verdict": v.verdict, "details": v.details}
        for v in verdicts
    ]


def decide_approval(
    session: Session,
    tenant_id: uuid.UUID,
    approval_id: uuid.UUID,
    approver: str,
    decision: str,
    reason: str | None,
) -> dict[str, Any]:
    """Handles both `approve` and `reject`. Raises `HTTPException` for 404/409/403
    cases; FastAPI's exception handling applies regardless of call depth.

    Concurrency (two simultaneous decisions on the same approval): belt one
    is `with_for_update()`, which locks the row for the rest of this
    transaction — a second, concurrent call blocks on that same SELECT until
    this one commits, then re-reads the now-decided row and 409s at the
    `status != "pending"` check above. Belt two is the state transition
    itself: a single conditional `UPDATE ... WHERE status = 'pending'` whose
    matched-row count is checked, independent of the lock — defense in depth
    if the lock is ever weakened or bypassed elsewhere.
    """
    approval = session.query(Approval).filter(Approval.id == approval_id).with_for_update().one_or_none()
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail=f"approval {approval_id} already {approval.status}")
    if approver == approval.requested_by:
        _audit_blocked_self_approval(tenant_id, approval.plan_id, approval.id, approver)
        raise HTTPException(status_code=403, detail="requester cannot approve their own mutation")

    plan_row = session.query(Plan).filter_by(id=approval.plan_id).one_or_none()
    if plan_row is None:
        raise HTTPException(status_code=404, detail="plan not found")

    new_status = "rejected" if decision == "reject" else "approved"
    decided_at = datetime.now(UTC)
    result = session.execute(
        sa_update(Approval)
        .where(Approval.id == approval.id, Approval.status == "pending")
        .values(status=new_status, approver=approver, reason=reason, decided_at=decided_at)
    )
    if result.rowcount != 1:
        # Unreachable in practice given the row lock above — kept as an
        # explicit second guard around the transition itself.
        raise HTTPException(status_code=409, detail=f"approval {approval_id} already decided")
    approval.status = new_status
    approval.approver = approver
    approval.reason = reason
    approval.decided_at = decided_at

    if decision == "reject":
        session.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor=approver,
                action="plan.approval_rejected",
                subject=str(plan_row.id),
                reason=reason,
                details={"approval_id": str(approval.id)},
            )
        )
        return {
            "approval_id": str(approval.id),
            "status": "rejected",
            "plan_id": str(plan_row.id),
            "execute": None,
            "answer": {
                "text": f"Approval {approval.id} was rejected by {approver}.",
                "numbers_provenance": {"plan_id": str(plan_row.id)},
            },
        }

    plan_row.status = "approved"
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor=approver,
            action="plan.approved",
            subject=str(plan_row.id),
            details={"approval_id": str(approval.id)},
        )
    )
    session.flush()

    plan = PlanIR.model_validate(plan_row.ir)
    execute_stage: dict[str, Any] | None = None
    try:
        run_result = execute_plan(session, tenant_id, plan, actor=approval.requested_by, actor_roles=["approver"])
    except PlanRejectedError as exc:
        # The catalog moved underneath between proposal and decision — record
        # the approved-but-dead-on-arrival outcome durably (on the Approval
        # row and in the audit trail), not just in this response.
        approval.execution_outcome = "rejected"
        session.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor=approver,
                action="plan.approved.execution_rejected",
                subject=str(plan_row.id),
                details={
                    "approved_plan_id": str(plan_row.id),
                    "rejected_plan_id": str(exc.plan_id),
                    "validator_error_codes": sorted({error.code for error in exc.errors}),
                },
            )
        )
        answer = {
            "text": "The approved plan was rejected by the executor's own validation (fail-closed) — "
            "the catalog must have changed underneath since it was proposed.",
            "numbers_provenance": {"validator_error_codes": sorted({error.code for error in exc.errors})},
        }
    else:
        run_uuid = uuid.UUID(run_result.run_id)
        run_row = session.query(Run).filter_by(id=run_uuid).one()
        approval.executed_run_id = run_uuid
        approval.execution_outcome = "succeeded" if run_result.status == "succeeded" else "failed"
        gate_verdicts = _gate_verdicts_for(session, run_uuid)
        node_hashes = {
            node_id: {
                "input": run_result.input_hashes.get(node_id, {}),
                "output": run_result.output_hashes.get(node_id, {}),
            }
            for node_id in run_result.node_statuses
        }
        execute_stage = {
            "run_id": run_result.run_id,
            "status": run_result.status,
            "node_statuses": run_result.node_statuses,
            "node_hashes": node_hashes,
            "gate_verdicts": gate_verdicts,
        }
        session.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor=approver,
                action="plan.approved.executed",
                subject=str(plan_row.id),
                details={
                    "approved_plan_id": str(plan_row.id),
                    "executed_plan_id": str(run_row.plan_id),
                    "run_id": str(run_uuid),
                },
            )
        )
        classify_stub = SimpleNamespace(normalized=SimpleNamespace(entities={}))
        plan_outcome_stub = SimpleNamespace(plan=plan, validation=ValidationResult(valid=True))
        answer = compose_answer(
            classify_result=classify_stub,
            plan_outcome=plan_outcome_stub,
            run_result=run_result,
            gate_verdicts=gate_verdicts,
        )

    return {
        "approval_id": str(approval.id),
        "status": "approved",
        "plan_id": str(plan_row.id),
        "execute": execute_stage,
        "answer": answer,
    }
