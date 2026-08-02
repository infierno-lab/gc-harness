"""Tests for core.api.app — the FastAPI front door (spec §3.2, §12).

core.classify.classifier and core.plan.planner (a sibling's concurrently-built
modules) do not need to exist on disk for this suite: `_load_classify_ask`/
`_load_plan_ask` are monkeypatched directly as attributes on `core.api.app`,
so nothing here imports the real sibling modules. Execution, however, is
real — `execute_plan` runs the actual 3-node elasticity plan from
evals/fixtures/elasticity_e2e_plan.yaml against the test DB, reusing the
seeding pattern from evals/test_phase1_exit.py.
"""

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from core.api import app as api_app
from core.catalog.seed import apply_seed
from core.db.session import admin_session
from core.plan.plan_ir import PlanIR, parse_plan_yaml
from core.plan.validator import ValidationError, ValidationResult, validate_plan
from packs.demand.catalog_seed import BLOCKS, METRICS

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "elasticity_e2e_plan.yaml"


class FakeNormalizedAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_type: str
    domain: str
    entities: dict[str, Any] = Field(default_factory=dict)
    output_wanted: str = "surface"
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
        validation: ValidationResult | None,
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


def _elasticity_plan() -> PlanIR:
    return parse_plan_yaml(FIXTURE_PATH)


def test_valid_ask_runs_full_pipeline_and_returns_staged_trace(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    plan = _elasticity_plan()
    normalized = FakeNormalizedAsk(
        task_type="elasticity",
        domain="demand",
        entities={"brand": "demo", "platform": "blinkit", "window": {"from": "2026-01", "to": "2026-02"}},
        output_wanted="surface",
    )

    def fake_classify_ask(session: Any, tenant_id: Any, text: str, *, reference_date: Any = None) -> FakeClassifyResult:
        return FakeClassifyResult(normalized)

    def fake_plan_ask(session: Any, tenant_id: Any, classify: Any, raw_text: str) -> FakePlanOutcome:
        validation = validate_plan(session, tenant_id, plan)
        return FakePlanOutcome(plan, validation)

    monkeypatch.setattr(api_app, "_load_classify_ask", lambda: fake_classify_ask)
    monkeypatch.setattr(api_app, "_load_plan_ask", lambda: fake_plan_ask)

    resp = client.post("/api/asks", json={"text": "Run elasticity for demo brand on blinkit for Jan to Feb"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["ask_text"] == "Run elasticity for demo brand on blinkit for Jan to Feb"
    stages = body["stages"]

    assert stages["normalize"]["result"]["task_type"] == "elasticity"
    assert stages["normalize"]["stage"] == "rules"
    assert stages["normalize"]["hash"] == "deadbeef" * 4

    assert stages["plan"]["ir"]["plan_ir_version"] == 1
    assert stages["plan"]["source"] == "template_cache"
    assert stages["plan"]["attempts"] == 1

    assert stages["validate"]["valid"] is True
    assert stages["validate"]["errors"] == []

    assert stages["execute"] is not None
    assert stages["execute"]["status"] == "succeeded"
    assert set(stages["execute"]["node_statuses"]) == {"n1", "n2", "n3"}
    assert stages["execute"]["node_hashes"]["n2"]["input"] == stages["execute"]["node_hashes"]["n1"]["output"]
    assert len(stages["execute"]["gate_verdicts"]) == 1
    assert stages["execute"]["gate_verdicts"][0]["verdict"] == "pass"
    assert stages["execute"]["gate_verdicts"][0]["check_suite"] == "post_model_checks"

    assert "demo" in stages["answer"]["text"]
    assert "surface" in stages["answer"]["numbers_provenance"]

    assert body["llm"]["calls"] == 0
    assert body["llm"]["input_tokens"] == 0
    assert body["llm"]["output_tokens"] == 0
    assert body["total_latency_ms"] >= 0

    # ledger drill-down
    run_id = stages["execute"]["run_id"]
    detail_resp = client.get(f"/api/runs/{run_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["run_id"] == run_id
    assert detail["status"] == "succeeded"
    assert {node["node_id"] for node in detail["nodes"]} == {"n1", "n2", "n3"}
    assert len(detail["gate_verdicts"]) == 1
    assert any(event["action"] == "run.succeeded" for event in detail["audit_events"])


def test_unknown_run_id_returns_404(client: TestClient) -> None:
    resp = client.get(f"/api/runs/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_rejected_plan_renders_validator_errors_as_the_answer(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    plan = _elasticity_plan()
    normalized = FakeNormalizedAsk(task_type="elasticity", domain="demand", entities={})

    rejection = ValidationResult(
        valid=False,
        errors=[
            ValidationError(
                code="not_granted", node_id="n1", message="tenant is not granted panel_builder@1.0"
            )
        ],
    )

    def fake_classify_ask(session: Any, tenant_id: Any, text: str, *, reference_date: Any = None) -> FakeClassifyResult:
        return FakeClassifyResult(normalized)

    def fake_plan_ask(session: Any, tenant_id: Any, classify: Any, raw_text: str) -> FakePlanOutcome:
        return FakePlanOutcome(plan, rejection)

    monkeypatch.setattr(api_app, "_load_classify_ask", lambda: fake_classify_ask)
    monkeypatch.setattr(api_app, "_load_plan_ask", lambda: fake_plan_ask)

    resp = client.post("/api/asks", json={"text": "Run elasticity for demo brand on blinkit for Jan to Feb"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["stages"]["validate"]["valid"] is False
    assert body["stages"]["validate"]["errors"][0]["code"] == "not_granted"
    assert body["stages"]["execute"] is None
    assert "not_granted" in body["stages"]["answer"]["text"]
    assert body["stages"]["answer"]["numbers_provenance"]["validator_error_codes"] == ["not_granted"]


def test_no_plan_produced_renders_a_clarifying_answer(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    normalized = FakeNormalizedAsk(
        task_type="unknown", domain="demand", entities={}, ambiguities=["which brand?"]
    )

    def fake_classify_ask(session: Any, tenant_id: Any, text: str, *, reference_date: Any = None) -> FakeClassifyResult:
        return FakeClassifyResult(normalized)

    def fake_plan_ask(session: Any, tenant_id: Any, classify: Any, raw_text: str) -> FakePlanOutcome:
        return FakePlanOutcome(None, None)

    monkeypatch.setattr(api_app, "_load_classify_ask", lambda: fake_classify_ask)
    monkeypatch.setattr(api_app, "_load_plan_ask", lambda: fake_plan_ask)

    resp = client.post("/api/asks", json={"text": "do something vague"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["stages"]["plan"]["ir"] is None
    assert body["stages"]["execute"] is None
    assert "which brand?" in body["stages"]["answer"]["text"]


def test_index_returns_html(migrated_test_db: str) -> None:
    with TestClient(api_app.app) as test_client:
        resp = test_client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "GobbleCube Core Intelligence" in resp.text


# --- graceful degradation on a gateway timeout (spec §10 failure model) -----
# "planner timeout -> queue + notify", never an uncaught 500. The gateway
# already ran its one retry before raising `GatewayError`; the API here must
# turn that into an honest 200 trace, not propagate it.


def test_classify_timeout_degrades_to_200_with_audited_ask(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    from core.db.models import Ask, AuditEvent
    from core.db.session import tenant_session
    from core.gateway import GatewayError

    def fake_classify_ask(session: Any, tenant_id: Any, text: str, *, reference_date: Any = None) -> FakeClassifyResult:
        raise GatewayError("claude CLI timed out after 30.0s")

    monkeypatch.setattr(api_app, "_load_classify_ask", lambda: fake_classify_ask)

    ask_text = f"a novel compound ask {uuid.uuid4()}"  # unique per run — this demo tenant persists across test runs
    resp = client.post("/api/asks", json={"text": ask_text, "actor": "alice"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["stages"]["normalize"]["degraded"] is True
    assert "timed out" in body["stages"]["normalize"]["error"]
    assert body["stages"]["plan"] is None
    assert body["stages"]["validate"] is None
    assert body["stages"]["execute"] is None
    assert body["stages"]["approval"] is None
    assert "recorded" in body["stages"]["answer"]["text"].lower()
    assert body["stages"]["answer"]["numbers_provenance"]["degraded_stage"] == "classify"

    with tenant_session(api_app.app.state.demo_tenant_id) as session:
        ask_row = session.query(Ask).filter_by(actor="alice", raw_text=ask_text).one()
        audit = (
            session.query(AuditEvent)
            .filter_by(subject=str(ask_row.id), action="ask.degraded")
            .one()
        )
        assert audit.details["stage"] == "classify"
        assert "timed out" in audit.details["error"]


def test_plan_timeout_degrades_to_200_with_audited_ask(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    from core.db.models import Ask, AuditEvent
    from core.db.session import tenant_session
    from core.gateway import GatewayError

    normalized = FakeNormalizedAsk(
        task_type="elasticity", domain="demand", entities={"brand": "demo", "platform": "blinkit"}
    )

    def fake_classify_ask(session: Any, tenant_id: Any, text: str, *, reference_date: Any = None) -> FakeClassifyResult:
        return FakeClassifyResult(normalized)

    def fake_plan_ask(session: Any, tenant_id: Any, classify: Any, raw_text: str) -> FakePlanOutcome:
        raise GatewayError("claude CLI timed out after 120.0s")

    monkeypatch.setattr(api_app, "_load_classify_ask", lambda: fake_classify_ask)
    monkeypatch.setattr(api_app, "_load_plan_ask", lambda: fake_plan_ask)

    ask_text = f"a novel compound ask {uuid.uuid4()}"  # unique per run — this demo tenant persists across test runs
    resp = client.post("/api/asks", json={"text": ask_text, "actor": "bob"})
    assert resp.status_code == 200
    body = resp.json()

    # normalize succeeded — only plan onward is degraded.
    assert body["stages"]["normalize"]["result"]["task_type"] == "elasticity"
    assert body["stages"]["plan"]["degraded"] is True
    assert "timed out" in body["stages"]["plan"]["error"]
    assert body["stages"]["validate"] is None
    assert body["stages"]["execute"] is None
    assert body["stages"]["approval"] is None
    answer_text = body["stages"]["answer"]["text"]
    assert "didn't respond in time" in answer_text
    assert "retry" in answer_text.lower()
    assert body["stages"]["answer"]["numbers_provenance"]["degraded_stage"] == "plan"

    with tenant_session(api_app.app.state.demo_tenant_id) as session:
        ask_row = session.query(Ask).filter_by(actor="bob", raw_text=ask_text).one()
        audit = (
            session.query(AuditEvent)
            .filter_by(subject=str(ask_row.id), action="ask.degraded")
            .one()
        )
        assert audit.details["stage"] == "plan"
        assert "timed out" in audit.details["error"]
