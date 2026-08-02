"""Tests for the mutate-class approval flow (spec §4.2, §8) — core.api.approvals
plus the `/api/asks` and `/api/approvals` endpoints in core.api.app.

Follows evals/test_api.py's pattern: `core.classify.classifier`/`core.plan.planner`
are monkeypatched fakes (no LLM recordings needed); `validate_plan`/`execute_plan`
and the demo tenant are real, against the test Postgres DB.
"""

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text as sa_text

from core.api import app as api_app
from core.catalog.seed import apply_seed
from core.db.models import Approval, Ask, AuditEvent, Plan, Run
from core.db.session import admin_session, tenant_session
from core.plan.plan_ir import PlanIR
from core.plan.validator import validate_plan
from packs.demand.catalog_seed import BLOCKS, METRICS


class FakeNormalizedAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_type: str
    domain: str
    entities: dict[str, Any] = Field(default_factory=dict)
    output_wanted: str = "promotion"
    constraints: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    confidence: float = 0.95


class FakeClassifyResult:
    def __init__(
        self,
        normalized: FakeNormalizedAsk,
        stage: str = "rules",
        normalized_hash: str = "deadbeef" * 4,
        latency_ms: int = 4,
    ) -> None:
        self.normalized = normalized
        self.stage = stage
        self.normalized_hash = normalized_hash
        self.latency_ms = latency_ms


class FakePlanOutcome:
    def __init__(
        self,
        plan: PlanIR | None,
        validation: Any,
        source: str = "template_cache",
        attempts: int = 1,
        latency_ms: int = 8,
    ) -> None:
        self.plan = plan
        self.validation = validation
        self.source = source
        self.attempts = attempts
        self.latency_ms = latency_ms


@pytest.fixture(scope="session", autouse=True)
def _seed_real_demand_pack(migrated_test_db: str) -> None:
    with admin_session() as session:
        apply_seed(session, BLOCKS, METRICS)


@pytest.fixture()
def client(migrated_test_db: str):
    with TestClient(api_app.app) as test_client:
        yield test_client


def _promotion_plan() -> PlanIR:
    """panel_builder -> elasticity_dml -> post_model_checks (gated) ->
    promote_model, the only mutate_state block in the harness."""
    return PlanIR.model_validate(
        {
            "plan_ir_version": 1,
            "intent_summary": "estimate + promote demo brand on blinkit, gated",
            "nodes": [
                {
                    "id": "n1",
                    "block": "panel_builder@1.0",
                    "params": {
                        "brand": "demo",
                        "platform": "blinkit",
                        "window": {"from": "2026-01", "to": "2026-02"},
                    },
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
                    "params": {"brand": "demo", "platform": "blinkit", "alias": "champion"},
                    "inputs": {"surface": "n2.surface"},
                },
            ],
            "outputs": {"report": "n4.report"},
            "assumptions": [],
            "estimated_cost": {"class": "seconds"},
        }
    )


def _patch_ask(monkeypatch: pytest.MonkeyPatch, plan: PlanIR) -> None:
    normalized = FakeNormalizedAsk(
        task_type="promotion", domain="demand", entities={"brand": "demo", "platform": "blinkit"}
    )

    def fake_classify_ask(session: Any, tenant_id: Any, text: str, *, reference_date: Any = None) -> FakeClassifyResult:
        return FakeClassifyResult(normalized)

    def fake_plan_ask(session: Any, tenant_id: Any, classify: Any, raw_text: str) -> FakePlanOutcome:
        # No actor_roles — the demo ask actor never carries "approver" itself;
        # the mutate_state node comes back with a sideeffect_policy error.
        validation = validate_plan(session, tenant_id, plan)
        return FakePlanOutcome(plan, validation)

    monkeypatch.setattr(api_app, "_load_classify_ask", lambda: fake_classify_ask)
    monkeypatch.setattr(api_app, "_load_plan_ask", lambda: fake_plan_ask)


def _ask(client: TestClient, actor: str) -> dict[str, Any]:
    resp = client.post("/api/asks", json={"text": "promote demo brand on blinkit", "actor": actor})
    assert resp.status_code == 200
    return resp.json()


def test_pending_ask_returns_approval_trace_and_does_not_execute(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    plan = _promotion_plan()
    _patch_ask(monkeypatch, plan)

    body = _ask(client, "alice")

    assert body["stages"]["execute"] is None
    approval_stage = body["stages"]["approval"]
    assert approval_stage["required"] is True
    assert approval_stage["nodes"] == ["n4"]
    assert approval_stage["status"] == "pending"
    approval_id = approval_stage["approval_id"]

    answer = body["stages"]["answer"]
    assert approval_id in answer["text"]
    assert answer["numbers_provenance"]["approval_id"] == approval_id

    with tenant_session(api_app.app.state.demo_tenant_id) as session:
        approval_row = session.query(Approval).filter_by(id=uuid.UUID(approval_id)).one()
        assert approval_row.status == "pending"
        assert approval_row.requested_by == "alice"
        plan_row = session.query(Plan).filter_by(id=approval_row.plan_id).one()
        assert plan_row.status == "proposed"
        assert session.query(Run).filter_by(plan_id=plan_row.id).count() == 0
        audit_actions = {e.action for e in session.query(AuditEvent).filter_by(subject=str(plan_row.id)).all()}
        assert "plan.approval_requested" in audit_actions


def test_self_approval_is_blocked_and_audited(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    plan = _promotion_plan()
    _patch_ask(monkeypatch, plan)

    approval_id = _ask(client, "alice")["stages"]["approval"]["approval_id"]

    decide_resp = client.post(f"/api/approvals/{approval_id}", json={"approver": "alice", "decision": "approve"})
    assert decide_resp.status_code == 403
    assert "requester cannot approve" in decide_resp.json()["detail"]

    with tenant_session(api_app.app.state.demo_tenant_id) as session:
        approval_row = session.query(Approval).filter_by(id=uuid.UUID(approval_id)).one()
        assert approval_row.status == "pending"  # unchanged by the blocked attempt
        audit_actions = {
            e.action for e in session.query(AuditEvent).filter_by(subject=str(approval_row.plan_id)).all()
        }
        assert "plan.approval_self_attempt_blocked" in audit_actions


def test_reject_leaves_plan_unexecuted_with_no_run(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    plan = _promotion_plan()
    _patch_ask(monkeypatch, plan)

    approval_id = _ask(client, "alice")["stages"]["approval"]["approval_id"]

    decide_resp = client.post(
        f"/api/approvals/{approval_id}", json={"approver": "bob", "decision": "reject", "reason": "not ready"}
    )
    assert decide_resp.status_code == 200
    body = decide_resp.json()
    assert body["status"] == "rejected"
    assert body["execute"] is None

    with tenant_session(api_app.app.state.demo_tenant_id) as session:
        approval_row = session.query(Approval).filter_by(id=uuid.UUID(approval_id)).one()
        assert approval_row.status == "rejected"
        assert approval_row.approver == "bob"
        assert approval_row.reason == "not ready"
        plan_row = session.query(Plan).filter_by(id=approval_row.plan_id).one()
        assert plan_row.status == "proposed"  # never advanced to approved/running
        assert session.query(Run).filter_by(plan_id=plan_row.id).count() == 0
        audit_actions = {e.action for e in session.query(AuditEvent).filter_by(subject=str(plan_row.id)).all()}
        assert "plan.approval_rejected" in audit_actions


def test_approve_by_different_actor_executes_and_writes_promotion_record(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    plan = _promotion_plan()
    _patch_ask(monkeypatch, plan)

    approval_id = _ask(client, "alice")["stages"]["approval"]["approval_id"]

    decide_resp = client.post(f"/api/approvals/{approval_id}", json={"approver": "bob", "decision": "approve"})
    assert decide_resp.status_code == 200
    body = decide_resp.json()

    assert body["status"] == "approved"
    assert body["execute"] is not None
    assert body["execute"]["status"] == "succeeded"
    assert set(body["execute"]["node_statuses"]) == {"n1", "n2", "n3", "n4"}
    assert body["answer"]["text"]

    run_id = uuid.UUID(body["execute"]["run_id"])

    with tenant_session(api_app.app.state.demo_tenant_id) as session:
        approval_row = session.query(Approval).filter_by(id=uuid.UUID(approval_id)).one()
        assert approval_row.status == "approved"
        assert approval_row.approver == "bob"
        # The plan row tied to the approval request goes 'proposed' -> 'approved'
        # and stops there — `execute_plan` (unmodified, per spec) always mints
        # its own fresh Ask/Plan/Run rows for the actual run rather than
        # updating an existing plan by id.
        approved_plan_row = session.query(Plan).filter_by(id=approval_row.plan_id).one()
        assert approved_plan_row.status == "approved"
        audit_actions = {
            e.action for e in session.query(AuditEvent).filter_by(subject=str(approved_plan_row.id)).all()
        }
        assert "plan.approved" in audit_actions

        run_row = session.query(Run).filter_by(id=run_id).one()
        assert run_row.status == "succeeded"
        executed_plan_row = session.query(Plan).filter_by(id=run_row.plan_id).one()
        assert executed_plan_row.status == "succeeded"
        run_audit_actions = {e.action for e in session.query(AuditEvent).filter_by(subject=str(run_id)).all()}
        assert any(action.startswith("run.") for action in run_audit_actions)

        # the approval->execution link is durable, not just in the response
        assert approval_row.executed_run_id == run_id
        assert approval_row.execution_outcome == "succeeded"
        assert "plan.approved.executed" in audit_actions
        executed_audit = next(
            e
            for e in session.query(AuditEvent).filter_by(subject=str(approved_plan_row.id)).all()
            if e.action == "plan.approved.executed"
        )
        assert executed_audit.details["approved_plan_id"] == str(approved_plan_row.id)
        assert executed_audit.details["executed_plan_id"] == str(run_row.plan_id)
        assert executed_audit.details["run_id"] == str(run_id)

    # promote_model wrote its alias record; execute_plan runs as requested_by
    # ("alice"), not the approver ("bob") — spec §8's two-person rule governs
    # who may authorize the mutation, not who performs it.
    tenant_id_str = str(api_app.app.state.demo_tenant_id)
    registry_path = Path(".gc_runs") / "registry" / f"{tenant_id_str}__demo__blinkit.json"
    assert registry_path.exists()
    record = json.loads(registry_path.read_text())
    assert record["alias"] == "champion"
    assert record["promoted_by"] == "alice"


def test_second_decision_after_approve_conflicts(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    plan = _promotion_plan()
    _patch_ask(monkeypatch, plan)
    approval_id = _ask(client, "alice")["stages"]["approval"]["approval_id"]

    first = client.post(f"/api/approvals/{approval_id}", json={"approver": "bob", "decision": "approve"})
    assert first.status_code == 200

    second = client.post(f"/api/approvals/{approval_id}", json={"approver": "carol", "decision": "reject"})
    assert second.status_code == 409

    with tenant_session(api_app.app.state.demo_tenant_id) as session:
        approval_row = session.query(Approval).filter_by(id=uuid.UUID(approval_id)).one()
        assert approval_row.status == "approved"  # unchanged by the conflicting second decision
        assert approval_row.approver == "bob"


def test_second_decision_after_reject_conflicts(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    plan = _promotion_plan()
    _patch_ask(monkeypatch, plan)
    approval_id = _ask(client, "alice")["stages"]["approval"]["approval_id"]

    first = client.post(f"/api/approvals/{approval_id}", json={"approver": "bob", "decision": "reject"})
    assert first.status_code == 200

    second = client.post(f"/api/approvals/{approval_id}", json={"approver": "carol", "decision": "approve"})
    assert second.status_code == 409

    with tenant_session(api_app.app.state.demo_tenant_id) as session:
        approval_row = session.query(Approval).filter_by(id=uuid.UUID(approval_id)).one()
        assert approval_row.status == "rejected"  # unchanged by the conflicting second decision
        assert approval_row.approver == "bob"


def test_decide_approval_locks_the_row_with_select_for_update(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Confirms the actual SQL `decide_approval` issues includes a `FOR UPDATE`
    clause against the approval table — the concurrency guard's first belt —
    by capturing statements the real engine executes during a live decision,
    rather than re-deriving the same query construct in isolation."""
    from sqlalchemy import event

    from core.db.session import get_engine

    plan = _promotion_plan()
    _patch_ask(monkeypatch, plan)
    approval_id = _ask(client, "alice")["stages"]["approval"]["approval_id"]

    captured: list[str] = []
    engine = get_engine()

    def _capture(conn, cursor, statement, parameters, context, executemany) -> None:
        captured.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        resp = client.post(f"/api/approvals/{approval_id}", json={"approver": "bob", "decision": "reject"})
        assert resp.status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert any("for update" in stmt.lower() and "approval" in stmt.lower() for stmt in captured), captured


def test_post_approval_execution_rejection_is_durably_recorded(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Simulates the catalog moving underneath between proposal and decision
    (here: the promote_model grant is revoked after the approval is opened)
    so execute_plan's own fail-closed re-validation rejects the approved
    plan — the approved-but-dead-on-arrival outcome must be durably visible
    on the Approval row and in the audit trail, not just in the response."""
    from core.catalog.seed import grant_all_blocks
    from core.db.models import Block, BlockGrant, BlockVersion

    plan = _promotion_plan()
    _patch_ask(monkeypatch, plan)
    approval_id = _ask(client, "alice")["stages"]["approval"]["approval_id"]

    tenant_id = api_app.app.state.demo_tenant_id
    with tenant_session(tenant_id) as session:
        block = session.query(Block).filter_by(name="promote_model").one()
        block_version = session.query(BlockVersion).filter_by(block_id=block.id, version="1.0").one()
        grant = (
            session.query(BlockGrant)
            .filter_by(tenant_id=tenant_id, block_version_id=block_version.id)
            .one()
        )
        session.delete(grant)

    try:
        decide_resp = client.post(f"/api/approvals/{approval_id}", json={"approver": "bob", "decision": "approve"})
        assert decide_resp.status_code == 200
        body = decide_resp.json()
        assert body["status"] == "approved"
        assert body["execute"] is None
        assert "rejected" in body["answer"]["text"].lower()

        with tenant_session(tenant_id) as session:
            approval_row = session.query(Approval).filter_by(id=uuid.UUID(approval_id)).one()
            assert approval_row.status == "approved"
            assert approval_row.executed_run_id is None
            assert approval_row.execution_outcome == "rejected"
            audit_actions = {
                e.action for e in session.query(AuditEvent).filter_by(subject=str(approval_row.plan_id)).all()
            }
            assert "plan.approved.execution_rejected" in audit_actions
    finally:
        # The next test's `client` fixture re-bootstraps (and re-grants) the
        # demo tenant on its own via lifespan startup, but restore explicitly
        # here so this test doesn't leave a load-bearing side effect for
        # whatever runs next within the *same* client/test.
        with admin_session() as session:
            grant_all_blocks(session, tenant_id)


def test_list_approvals_is_tenant_scoped_and_filters_by_status(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    plan = _promotion_plan()
    _patch_ask(monkeypatch, plan)

    approval_id = _ask(client, "carol")["stages"]["approval"]["approval_id"]

    list_resp = client.get("/api/approvals", params={"status": "pending"})
    assert list_resp.status_code == 200
    pending_ids = {a["id"] for a in list_resp.json()["approvals"]}
    assert approval_id in pending_ids


def test_rls_other_tenant_sees_no_approvals(
    migrated_test_db: str, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    plan = _promotion_plan()

    with tenant_session(tenant_id) as session:
        ask_row = Ask(tenant_id=tenant_id, actor="alice", raw_text="promote demo brand")
        session.add(ask_row)
        session.flush()
        plan_row = Plan(
            tenant_id=tenant_id,
            ask_id=ask_row.id,
            ir=plan.model_dump(mode="json", by_alias=True),
            ir_hash="deadbeefdeadbeef",
            status="proposed",
        )
        session.add(plan_row)
        session.flush()
        session.add(Approval(tenant_id=tenant_id, plan_id=plan_row.id, requested_by="alice", status="pending"))

    with tenant_session(tenant_id) as session:
        assert session.query(Approval).count() == 1

    with tenant_session(other_tenant_id) as session:
        assert session.query(Approval).count() == 0


def test_hostile_brand_param_fails_schema_validation(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    """params_schema is the first layer of defense against path traversal via
    promote_model's brand/platform params (spec §4.3: the contract is the
    whole truth) — a plan carrying a hostile brand must never validate."""
    plan_dict = _promotion_plan().model_dump(mode="json", by_alias=True)
    for node in plan_dict["nodes"]:
        if node["id"] == "n4":
            node["params"]["brand"] = "../../../../tmp/evil"
    hostile_plan = PlanIR.model_validate(plan_dict)

    with tenant_session(tenant_id) as session:
        result = validate_plan(session, tenant_id, hostile_plan, actor_roles=["approver"])

    assert result.valid is False
    assert any(error.code == "params_invalid" and error.node_id == "n4" for error in result.errors)


def test_registry_path_rejects_path_traversal(tmp_path: Path) -> None:
    """Second layer of defense, independent of the schema: _registry_path
    itself refuses to resolve outside its own registry directory."""
    from core.execute.adapter import BlockContext, Storage
    from packs.demand.blocks.promote_model import _registry_path

    run_dir = tmp_path / "runs" / "some-run-id"
    run_dir.mkdir(parents=True)
    ctx = BlockContext(
        tenant_id="tenant1",
        run_id="some-run-id",
        node_id="n4",
        block_version="promote_model@1.0",
        run_dir=run_dir,
        storage=Storage(run_dir),
    )

    with pytest.raises(ValueError, match="escapes|refusing"):
        _registry_path(ctx, "../../../../tmp/evil", "blinkit")

    # a well-formed pair still resolves cleanly, inside the registry dir
    clean_path = _registry_path(ctx, "demo", "blinkit")
    assert clean_path.parent == (run_dir.parent / "registry").resolve()


def test_migration_0003_creates_approval_table_with_rls(migrated_test_db: str) -> None:
    with admin_session() as session:
        exists = session.execute(sa_text("SELECT to_regclass('public.approval')")).scalar()
        assert exists == "approval"
        rls_enabled = session.execute(
            sa_text("SELECT relrowsecurity FROM pg_class WHERE relname = 'approval'")
        ).scalar()
        assert rls_enabled is True
