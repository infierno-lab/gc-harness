"""FastAPI front door (spec §3.2, §12 api layout) — the presentable entry
point. `POST /api/asks` runs classify -> plan (validation is inside plan_ask)
-> execute -> answer, all inside one tenant_session, and returns the full
staged trace the UI renders as a pipeline timeline.

No auth for this PoC demo (OIDC middleware is the Phase-2 follow-up); a
single hardcoded demo tenant is bootstrapped server-side on startup — the ask
text is the only client input.

`core.classify.classifier` and `core.plan.planner` are a sibling's modules,
built concurrently and possibly not on disk yet. They're imported lazily
through `_load_classify_ask`/`_load_plan_ask` below so importing this module
never requires them to exist, and calling either loader for real raises a
clear ImportError if they're still missing. Tests monkeypatch the loaders
themselves (module attributes on `core.api.app`), so the test suite never
needs the sibling's real modules on disk either.
"""

import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select

from core.api.answers import compose_answer
from core.api.approvals import classify_approval_need, decide_approval, open_approval, pending_approval_answer
from core.api.ui import INDEX_HTML
from core.catalog.seed import apply_seed, grant_all_blocks
from core.db.models import Approval, Ask, AuditEvent, GateVerdict, LlmCall, NodeExecution, Run, Tenant
from core.db.session import admin_session, tenant_session
from core.execute.runner import PlanRejectedError, execute_plan
from core.gateway import GatewayError
from packs.demand.catalog_seed import BLOCKS, METRICS

DEMO_TENANT_NAME = "demo-tenant"
DEMO_ACTOR = "demo-ui"


def _load_classify_ask() -> Callable[..., Any]:
    """Lazy import of the sibling's classifier — raises a clear ImportError
    if `core.classify.classifier` hasn't landed yet."""
    from core.classify.classifier import classify_ask

    return classify_ask


def _load_plan_ask() -> Callable[..., Any]:
    """Lazy import of the sibling's planner — see `_load_classify_ask`."""
    from core.plan.planner import plan_ask

    return plan_ask


def _bootstrap_demo_tenant() -> uuid.UUID:
    """Idempotent demo bootstrap (mirrors scripts/demo.py): get-or-create the
    demo tenant, seed the demand pack catalog, grant every active block."""
    with admin_session() as session:
        tenant = session.execute(select(Tenant).where(Tenant.name == DEMO_TENANT_NAME)).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(name=DEMO_TENANT_NAME, status="active")
            session.add(tenant)
            session.flush()
        apply_seed(session, BLOCKS, METRICS)
        grant_all_blocks(session, tenant.id)
        return tenant.id


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.demo_tenant_id = _bootstrap_demo_tenant()
    yield


app = FastAPI(title="GobbleCube Core Intelligence", lifespan=lifespan)


class AskRequest(BaseModel):
    text: str
    # No auth for this PoC demo — `actor` is a named string the client
    # supplies directly so a requester and an approver can be distinct people
    # in the demo. An OIDC/JWT claims mapper (spec §8) replaces this with a
    # verified identity later; nothing downstream assumes otherwise.
    actor: str = DEMO_ACTOR


class ApprovalDecisionRequest(BaseModel):
    approver: str
    decision: Literal["approve", "reject"]
    reason: str | None = None


def _gate_verdicts_for(session: Any, run_id: uuid.UUID) -> list[dict[str, Any]]:
    verdicts = session.query(GateVerdict).filter_by(run_id=run_id).all()
    return [
        {"node_id": v.node_id, "check_suite": v.check_suite, "verdict": v.verdict, "details": v.details}
        for v in verdicts
    ]


_DEGRADED_MESSAGES = {
    "classify": "The classifier didn't respond in time for this ask. Your ask was recorded — please retry.",
    "plan": "The planner didn't respond in time for this novel ask. Your ask was recorded — please retry; "
    "repeat asks of known shapes are unaffected.",
}


def _degraded_answer(stage: str, error: Exception) -> dict[str, Any]:
    return {
        "text": _DEGRADED_MESSAGES.get(stage, f"The {stage} stage didn't respond in time. Your ask was recorded."),
        "numbers_provenance": {"degraded_stage": stage, "error": str(error)},
    }


def _record_degraded_ask(session: Any, tenant_id: uuid.UUID, actor: str, raw_text: str, *, stage: str, error: Exception) -> None:
    """Fail-open observation, fail-closed gates (spec invariant #6): a gateway
    timeout never reaches the caller as a bare 500 — it degrades to an honest
    200 trace, but the degradation itself is never silent. Records an `Ask`
    row (classify_ask/plan_ask don't persist one on the timeout path — there
    was nothing to normalize/plan) plus an audited `ask.degraded` event."""
    ask_row = Ask(tenant_id=tenant_id, actor=actor, raw_text=raw_text)
    session.add(ask_row)
    session.flush()
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor=actor,
            action="ask.degraded",
            subject=str(ask_row.id),
            details={"stage": stage, "error": str(error)},
        )
    )


@app.post("/api/asks")
def create_ask(payload: AskRequest) -> dict[str, Any]:
    tenant_id = app.state.demo_tenant_id
    actor = payload.actor
    request_started_at = datetime.now(UTC)
    wall_clock_start = time.perf_counter()

    with tenant_session(tenant_id) as session:
        classify_ask = _load_classify_ask()
        classify_t0 = time.perf_counter()
        try:
            classify_result = classify_ask(session, tenant_id, payload.text)
        except GatewayError as exc:
            # Spec §10 failure model: a gateway timeout degrades gracefully —
            # never an uncaught 500. The gateway already ran its one retry;
            # the API never retries again on top of that.
            _record_degraded_ask(session, tenant_id, actor, payload.text, stage="classify", error=exc)
            stages: dict[str, Any] = {
                "normalize": {"error": str(exc), "degraded": True},
                "plan": None,
                "validate": None,
                "execute": None,
                "approval": None,
                "answer": _degraded_answer("classify", exc),
            }
        else:
            classify_latency_ms = getattr(classify_result, "latency_ms", None)
            if classify_latency_ms is None:
                classify_latency_ms = int((time.perf_counter() - classify_t0) * 1000)

            stages = {
                "normalize": {
                    "result": classify_result.normalized.model_dump(mode="json", by_alias=True),
                    "stage": classify_result.stage,
                    "hash": classify_result.normalized_hash,
                    "latency_ms": classify_latency_ms,
                }
            }

            plan_ask = _load_plan_ask()
            try:
                plan_outcome = plan_ask(session, tenant_id, classify_result, payload.text)
            except GatewayError as exc:
                _record_degraded_ask(session, tenant_id, actor, payload.text, stage="plan", error=exc)
                stages["plan"] = {"error": str(exc), "degraded": True}
                stages["validate"] = None
                stages["execute"] = None
                stages["approval"] = None
                stages["answer"] = _degraded_answer("plan", exc)
            else:
                stages["plan"] = {
                    "ir": (
                        plan_outcome.plan.model_dump(mode="json", by_alias=True)
                        if plan_outcome.plan is not None
                        else None
                    ),
                    "source": plan_outcome.source,
                    "attempts": plan_outcome.attempts,
                    "latency_ms": plan_outcome.latency_ms,
                }

                validation = plan_outcome.validation
                stages["validate"] = {
                    "valid": validation.valid if validation is not None else False,
                    "errors": [error.model_dump() for error in validation.errors] if validation is not None else [],
                    "requires_approval": list(validation.requires_approval) if validation is not None else [],
                }

                approval_nodes = classify_approval_need(validation, plan_outcome.plan)

                run_result = None
                execute_stage: dict[str, Any] | None = None
                approval_stage: dict[str, Any] | None = None
                answer: dict[str, Any] | None = None

                if approval_nodes is not None:
                    # Mutate-class node(s) present (spec §4.2/§8): never execute
                    # here — persist the proposal and hand off to the
                    # two-person approval flow.
                    approval_row = open_approval(
                        session, tenant_id, actor, payload.text, plan_outcome.plan, approval_nodes
                    )
                    approval_stage = {
                        "required": True,
                        "approval_id": str(approval_row.id),
                        "nodes": approval_nodes,
                        "status": "pending",
                    }
                    answer = pending_approval_answer(approval_row, approval_nodes)
                elif validation is not None and validation.valid and plan_outcome.plan is not None:
                    execute_t0 = time.perf_counter()
                    try:
                        run_result = execute_plan(session, tenant_id, plan_outcome.plan, actor=actor)
                    except PlanRejectedError as exc:
                        # Defensive only: plan_ask's own validation already
                        # said this plan was valid. execute_plan re-validates
                        # independently (spec invariant #6 — never skip the
                        # validator) and fails closed if the catalog moved
                        # underneath between the two calls. Surface it exactly
                        # like any other rejected plan.
                        stages["validate"] = {
                            "valid": False,
                            "errors": [error.model_dump() for error in exc.errors],
                            "requires_approval": [],
                        }
                    else:
                        run_uuid = uuid.UUID(run_result.run_id)
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
                            "latency_ms": int((time.perf_counter() - execute_t0) * 1000),
                        }
                stages["execute"] = execute_stage
                stages["approval"] = approval_stage

                if answer is None:
                    answer = compose_answer(
                        classify_result=classify_result,
                        plan_outcome=plan_outcome,
                        run_result=run_result,
                        gate_verdicts=execute_stage["gate_verdicts"] if execute_stage else None,
                    )
                stages["answer"] = answer

        llm_calls = (
            session.query(LlmCall)
            .filter(LlmCall.tenant_id == tenant_id, LlmCall.created_at >= request_started_at)
            .all()
        )

    llm_tally = {
        "calls": len(llm_calls),
        "input_tokens": sum(call.input_tokens or 0 for call in llm_calls),
        "output_tokens": sum(call.output_tokens or 0 for call in llm_calls),
    }

    return {
        "ask_text": payload.text,
        "stages": stages,
        "llm": llm_tally,
        "total_latency_ms": int((time.perf_counter() - wall_clock_start) * 1000),
    }


@app.post("/api/approvals/{approval_id}")
def submit_approval_decision(approval_id: str, payload: ApprovalDecisionRequest) -> dict[str, Any]:
    tenant_id = app.state.demo_tenant_id
    try:
        approval_uuid = uuid.UUID(approval_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid approval_id") from exc

    with tenant_session(tenant_id) as session:
        return decide_approval(
            session, tenant_id, approval_uuid, payload.approver, payload.decision, payload.reason
        )


@app.get("/api/approvals")
def list_approvals(status: str | None = None) -> dict[str, Any]:
    tenant_id = app.state.demo_tenant_id
    with tenant_session(tenant_id) as session:
        query = session.query(Approval)
        if status is not None:
            query = query.filter(Approval.status == status)
        rows = query.order_by(Approval.created_at.desc()).all()
        return {
            "approvals": [
                {
                    "id": str(row.id),
                    "plan_id": str(row.plan_id),
                    "requested_by": row.requested_by,
                    "approver": row.approver,
                    "status": row.status,
                    "reason": row.reason,
                    "created_at": row.created_at.isoformat(),
                    "decided_at": row.decided_at.isoformat() if row.decided_at else None,
                }
                for row in rows
            ]
        }


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    tenant_id = app.state.demo_tenant_id
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid run_id") from exc

    with tenant_session(tenant_id) as session:
        run = session.query(Run).filter_by(id=run_uuid).one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")

        node_execs = (
            session.query(NodeExecution).filter_by(run_id=run_uuid).order_by(NodeExecution.started_at).all()
        )
        audit_events = session.query(AuditEvent).filter_by(subject=run_id).order_by(AuditEvent.created_at).all()

        return {
            "run_id": run_id,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "nodes": [
                {
                    "node_id": node_exec.node_id,
                    "block_version": node_exec.block_version,
                    "status": node_exec.status,
                    "input_hashes": node_exec.input_hashes,
                    "output_hashes": node_exec.output_hashes,
                    "error": node_exec.error,
                }
                for node_exec in node_execs
            ],
            "gate_verdicts": _gate_verdicts_for(session, run_uuid),
            "audit_events": [
                {
                    "action": event.action,
                    "actor": event.actor,
                    "created_at": event.created_at.isoformat(),
                    "details": event.details,
                }
                for event in audit_events
            ],
        }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML
