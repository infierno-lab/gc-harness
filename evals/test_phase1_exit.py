"""Phase 1 exit test (roadmap §Phase 1, spec §12 milestones): a hand-written
Plan IR YAML for the elasticity e2e validates, compiles, runs, gates fire, and
the run ledger has hashes on every edge — no LLM involved anywhere.

This is the first REAL integration of the two stage-2 halves (catalog+seed+
validator vs. executor+demand pack), which so far were only exercised against
their own private fixtures (evals/fixtures/catalog_blocks.py pins
panel_builder@2.3/elasticity_dml@1.4/post_model_checks@1.1; the executor
tests seed the real pack via raw ORM get-or-create). Here the real seed
framework (core.catalog.seed.apply_seed) consumes the real pack export
(packs.demand.catalog_seed.BLOCKS, pinned at @1.0) and the real validator
gates a hand-written YAML plan before the real executor runs it.

Block/block_version/contract tables are global (not tenant-scoped) and get
reused across every test module in this suite; get-or-create/upsert semantics
in apply_seed mean this coexists fine with whatever other test files seed
into the same catalog (versions differ, so no collisions). Tenant-scoped
state (grants, runs) always uses a fresh tenant fixture per test.
"""

import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from core.catalog.digest import render_digest
from core.catalog.seed import apply_seed, grant_all_blocks
from core.db.models import Ask, AuditEvent, GateVerdict, LlmCall, NodeExecution, Plan, Run
from core.db.session import admin_session, tenant_session
from core.execute.runner import execute_plan
from core.plan.plan_ir import PlanIR, ir_hash, parse_plan_yaml
from core.plan.validator import validate_plan
from packs.demand.catalog_seed import BLOCKS, METRICS

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "elasticity_e2e_plan.yaml"


@pytest.fixture(scope="session", autouse=True)
def _seed_real_demand_pack(migrated_test_db: str) -> None:
    """Seeds the actual demand-pack catalog export via the real seed
    framework, once per test session. Individual tests below re-invoke
    apply_seed themselves where idempotency is the thing under test."""
    with admin_session() as session:
        apply_seed(session, BLOCKS, METRICS)


def _grant(tid: uuid.UUID) -> None:
    with admin_session() as session:
        grant_all_blocks(session, tid)


def _load_plan() -> PlanIR:
    return parse_plan_yaml(FIXTURE_PATH)


def _plan_with_forced_positive_elasticities(base_plan: PlanIR) -> PlanIR:
    """Same DAG shape, but flips elasticity_dml's demo/test knob so
    post_model_checks fails its sign_sanity check through the genuine
    pipeline — no monkeypatching."""
    mutated = base_plan.model_copy(deep=True)
    for node in mutated.nodes:
        if node.id == "n2":
            node.params["force_positive_elasticities"] = True
    return mutated


# --- 1. seed integration --------------------------------------------------


def test_seed_integration_apply_seed_consumes_real_pack_export_idempotently(
    migrated_test_db: str,
) -> None:
    expected_block_names = {f"{b['name']}@{b['version']}" for b in BLOCKS}

    # The session-scoped autouse fixture may have already applied this seed
    # once — that's fine, and itself already a form of idempotency coverage.
    # What must always hold is that running it again is a pure no-op.
    with admin_session() as session:
        apply_seed(session, BLOCKS, METRICS)

    with admin_session() as session:
        second = apply_seed(session, BLOCKS, METRICS)

    assert second.blocks_created == []
    assert second.blocks_updated == []
    assert set(second.blocks_unchanged) == expected_block_names
    assert second.metrics_created == []
    assert second.metrics_updated == []
    assert second.metrics_unchanged == []


# --- 2. digest -------------------------------------------------------------


def test_digest_lists_every_active_granted_pack_block_byte_stable(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    _grant(tenant_id)

    with tenant_session(tenant_id) as session:
        first = render_digest(session, tenant_id)
    with tenant_session(tenant_id) as session:
        second = render_digest(session, tenant_id)

    assert first.text == second.text
    assert first.digest_hash == second.digest_hash

    lines = first.text.splitlines()
    for block in BLOCKS:
        name_version = f"{block['name']}@{block['version']}"
        assert any(line.startswith(f"{name_version} ") for line in lines), (
            f"missing digest line for granted active block {name_version!r}"
        )


# --- 3. the exit flow --------------------------------------------------


def test_exit_flow_hand_written_plan_validates_runs_gates_fire_with_hashes(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    _grant(tenant_id)
    plan = _load_plan()

    # Snapshot before the flow: other test files legitimately audit gateway
    # calls into the shared DB; the guarantee here is that *this* flow adds none.
    with admin_session() as session:
        llm_calls_before = session.query(LlmCall).count()

    with tenant_session(tenant_id) as session:
        validation = validate_plan(session, tenant_id, plan)
    assert validation.errors == []
    assert validation.valid is True

    with tenant_session(tenant_id) as session:
        result = execute_plan(session, tenant_id, plan, actor="exit-test")

    assert result.status == "succeeded"
    assert result.node_statuses == {"n1": "succeeded", "n2": "succeeded", "n3": "succeeded"}

    run_uuid = uuid.UUID(result.run_id)
    with tenant_session(tenant_id) as session:
        node_execs = {ne.node_id: ne for ne in session.query(NodeExecution).filter_by(run_id=run_uuid).all()}
        verdicts = session.query(GateVerdict).filter_by(run_id=run_uuid).all()
        audit_actions = {ev.action for ev in session.query(AuditEvent).filter_by(subject=result.run_id).all()}
        run_row = session.query(Run).filter_by(id=run_uuid).one()
        plan_row = session.query(Plan).filter_by(id=run_row.plan_id).one()
        ask_row = session.query(Ask).filter_by(id=plan_row.ask_id).one()

    # hashes on every edge: every node has non-empty output_hashes; every node
    # with declared inputs has non-empty input_hashes.
    assert set(node_execs) == {"n1", "n2", "n3"}
    for node_id in ("n1", "n2", "n3"):
        assert node_execs[node_id].status == "succeeded"
        assert node_execs[node_id].output_hashes
    assert not node_execs["n1"].input_hashes  # n1 declares no inputs
    assert node_execs["n2"].input_hashes == {"panel": node_execs["n1"].output_hashes["panel"]}
    assert node_execs["n3"].input_hashes == {"surface": node_execs["n2"].output_hashes["surface"]}

    # exactly one gate verdict, pass, for the explicit post_model_checks node.
    assert len(verdicts) == 1
    assert verdicts[0].node_id == "n3"
    assert verdicts[0].check_suite == "post_model_checks"
    assert verdicts[0].verdict == "pass"

    # plan outputs published with a real ref.
    assert "surface" in result.outputs
    surface_env = result.outputs["surface"]
    assert surface_env.ref.startswith("res_")
    assert surface_env.summary

    # audit events for run start + terminal.
    assert "run.start" in audit_actions
    assert "run.succeeded" in audit_actions

    # Ask/Plan rows created with ir_hash matching ir_hash(plan).
    assert plan_row.ir_hash == ir_hash(plan)
    assert plan_row.status == "succeeded"
    assert ask_row.raw_text == plan.intent_summary
    assert run_row.status == "succeeded"

    # no-LLM guarantee: nothing in this flow ever writes an LlmCall row.
    with admin_session() as session:
        assert session.query(LlmCall).count() == llm_calls_before


# --- 4. determinism ---------------------------------------------------


def test_determinism_same_plan_twice_yields_identical_output_hashes(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    _grant(tenant_id)
    plan = _load_plan()

    with tenant_session(tenant_id) as session:
        first = execute_plan(session, tenant_id, plan, actor="exit-test")
    with tenant_session(tenant_id) as session:
        second = execute_plan(session, tenant_id, plan, actor="exit-test")

    assert first.status == second.status == "succeeded"
    assert first.output_hashes == second.output_hashes
    assert first.input_hashes == second.input_hashes


# --- 5. gate failure e2e through the real pack -------------------------


def test_gate_failure_e2e_through_real_pack(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant(tenant_id)
    plan = _plan_with_forced_positive_elasticities(_load_plan())

    with tenant_session(tenant_id) as session:
        validation = validate_plan(session, tenant_id, plan)
    # structurally identical DAG — only the runtime output is broken.
    assert validation.valid is True

    with tenant_session(tenant_id) as session:
        result = execute_plan(session, tenant_id, plan, actor="exit-test")

    assert result.status == "failed"
    assert result.node_statuses == {"n1": "succeeded", "n2": "succeeded", "n3": "gate_failed"}
    assert result.outputs == {}

    run_uuid = uuid.UUID(result.run_id)
    with tenant_session(tenant_id) as session:
        node_execs = {ne.node_id: ne for ne in session.query(NodeExecution).filter_by(run_id=run_uuid).all()}
        verdicts = session.query(GateVerdict).filter_by(run_id=run_uuid).all()
        run_row = session.query(Run).filter_by(id=run_uuid).one()

    assert node_execs["n1"].status == "succeeded"
    assert node_execs["n2"].status == "succeeded"  # the model ran fine; the downstream gate condemned it
    assert node_execs["n3"].status == "gate_failed"

    assert len(verdicts) == 1
    assert verdicts[0].node_id == "n3"
    assert verdicts[0].check_suite == "post_model_checks"
    assert verdicts[0].verdict == "fail"
    failed_checks = {v["check_name"] for v in verdicts[0].details["verdicts"] if v["verdict"] == "fail"}
    assert "sign_sanity" in failed_checks
    assert run_row.status == "failed"


# --- 6. cross-tenant negative -------------------------------------------


def test_cross_tenant_negative_not_granted_and_rls_hides_runs(
    migrated_test_db: str, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    _grant(tenant_id)  # tenant A granted; other_tenant_id (tenant B) granted nothing
    plan = _load_plan()

    with tenant_session(other_tenant_id) as session:
        validation = validate_plan(session, other_tenant_id, plan)
    assert validation.valid is False
    codes = {err.code for err in validation.errors}
    assert codes == {"not_granted"}
    not_granted_nodes = {err.node_id for err in validation.errors}
    assert not_granted_nodes == {"n1", "n2", "n3"}

    with tenant_session(tenant_id) as session:
        result = execute_plan(session, tenant_id, plan, actor="exit-test")
    assert result.status == "succeeded"
    run_uuid = uuid.UUID(result.run_id)

    with tenant_session(other_tenant_id) as session:
        visible_runs = session.query(Run).filter_by(id=run_uuid).all()
        visible_node_execs = session.query(NodeExecution).filter_by(run_id=run_uuid).all()
    assert visible_runs == []
    assert visible_node_execs == []


# --- 7. no-LLM guarantee -------------------------------------------------


def test_execution_spine_writes_no_llm_call_rows(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    """The plan-then-execute spine never talks to a model: a full
    validate+execute flow adds zero llm_call rows. (The gateway used by the
    classifier/planner DOES audit rows into the shared test DB from other
    test files — the guarantee is scoped to execution, not the whole table.)"""
    _grant(tenant_id)
    plan = _load_plan()

    with admin_session() as session:
        before = session.query(LlmCall).count()

    with tenant_session(tenant_id) as session:
        validation = validate_plan(session, tenant_id, plan)
        assert validation.valid is True
        result = execute_plan(session, tenant_id, plan, actor="exit-test")
    assert result.status == "succeeded"

    with admin_session() as session:
        assert session.query(LlmCall).count() == before


def test_no_llm_sdk_modules_imported_by_execution_or_validation() -> None:
    """Run in a subprocess: this process's sys.modules is polluted by test
    files that legitimately import the gateway (which imports the SDK)."""
    code = (
        "import sys; "
        "import core.catalog, core.execute, core.plan.validator; "
        "leaked = {'anthropic', 'openai'} & set(sys.modules); "
        "sys.exit(f'leaked: {leaked}' if leaked else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"LLM SDK modules imported by the execution/validation spine: "
        f"{proc.stdout}{proc.stderr}"
    )
