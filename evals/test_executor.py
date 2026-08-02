"""Executor tests (spec §3.2, §9, §10) — the in-process DAG runner over the
Demand pack's demo blocks. No LLM anywhere in this path.

Catalog rows for the Demand pack blocks are seeded directly via ORM (not via
core.catalog.seed.apply_seed, which is owned by a sibling build) using
get-or-create semantics: block/contract tables are global (not tenant-scoped),
so this must coexist with whatever the other test modules seed into the same
catalog. `core.catalog.seed.grant_all_blocks` IS used here (test-only) since
execute_plan now enforces grants via validate_plan — the runner itself never
imports the seed module.

execute_plan fails closed (spec invariant #6, §11: never skip the plan
validator): it runs validate_plan internally before touching the DAG, so
every test plan below must be independently valid (granted, active blocks,
gated model nodes) unless the test is specifically exercising rejection.
"""

import uuid

import pytest

from core.catalog.contracts import contract_json_schema
from core.catalog.seed import grant_all_blocks
from core.db.models import (
    AuditEvent,
    Block,
    BlockIO,
    BlockVersion,
    Contract,
    GateBinding,
    GateVerdict,
    NodeExecution,
    Plan,
    Run,
)
from core.db.session import admin_session, tenant_session
from core.execute.runner import PlanRejectedError, execute_plan
from core.plan.plan_ir import PlanIR
from packs.demand.catalog_seed import BLOCKS


def _get_or_create_contract(session, name_version: str) -> Contract:
    name, version = name_version.rsplit("@", 1)
    existing = session.query(Contract).filter_by(name=name, version=version).one_or_none()
    if existing is not None:
        return existing
    contract = Contract(name=name, version=version, json_schema=contract_json_schema(name_version), status="active")
    session.add(contract)
    session.flush()
    return contract


def _get_or_create_block_version(session, block_dict: dict, contracts: dict[str, Contract]) -> BlockVersion:
    block = session.query(Block).filter_by(name=block_dict["name"]).one_or_none()
    if block is None:
        block = Block(
            name=block_dict["name"],
            kind=block_dict["kind"],
            owner=block_dict["owner"],
            domain=block_dict["domain"],
            tags=block_dict["tags"],
        )
        session.add(block)
        session.flush()

    block_version = (
        session.query(BlockVersion).filter_by(block_id=block.id, version=block_dict["version"]).one_or_none()
    )
    if block_version is not None:
        return block_version

    block_version = BlockVersion(
        block_id=block.id,
        version=block_dict["version"],
        git_sha=block_dict["git_sha"],
        image_ref=block_dict["image_ref"],
        entrypoint=block_dict["entrypoint"],
        params_schema=block_dict["params_schema"],
        cost_class=block_dict["cost_class"],
        sideeffect_class=block_dict["sideeffect_class"],
        when_to_use=block_dict["when_to_use"],
        when_not_to_use=block_dict["when_not_to_use"],
        status=block_dict["status"],
    )
    session.add(block_version)
    session.flush()

    for io in block_dict["consumes"]:
        session.add(
            BlockIO(
                block_version_id=block_version.id,
                contract_id=contracts[io["contract"]].id,
                role="consumes",
                port_name=io["port"],
            )
        )
    for io in block_dict["produces"]:
        session.add(
            BlockIO(
                block_version_id=block_version.id,
                contract_id=contracts[io["contract"]].id,
                role="produces",
                port_name=io["port"],
            )
        )
    for gate in block_dict["gates"]:
        session.add(
            GateBinding(
                block_version_id=block_version.id,
                check_suite=gate["check_suite"],
                when=gate["when"],
                policy=gate["policy"],
            )
        )
    return block_version


# Test-only catalog variants, seeded alongside the real pack export but never
# touching packs/demand/catalog_seed.py:
#  - panel_builder@1.1: same block as @1.0, but carries a synthetic post
#    GateBinding to post_model_checks. panel_builder is `kind: data`, not
#    `model`, so the validator's mandatory-gate check never requires an
#    explicit downstream validation node for it — this variant exercises the
#    catalog auto-invoke (gate path B) defense-in-depth mechanism in
#    isolation, without perturbing @1.0 (used by every other test below).
#  - panel_builder@0.9: same block, status=deprecated — for the inactive
#    block rejection test.
_panel_builder_base = next(b for b in BLOCKS if b["name"] == "panel_builder")

_PANEL_BUILDER_WITH_TEST_GATE = dict(_panel_builder_base)
_PANEL_BUILDER_WITH_TEST_GATE["version"] = "1.1"
_PANEL_BUILDER_WITH_TEST_GATE["gates"] = [{"check_suite": "post_model_checks", "when": "post", "policy": "block"}]

_DEPRECATED_PANEL_BUILDER = dict(_panel_builder_base)
_DEPRECATED_PANEL_BUILDER["version"] = "0.9"
_DEPRECATED_PANEL_BUILDER["status"] = "deprecated"


@pytest.fixture(scope="session", autouse=True)
def seeded_executor_catalog(migrated_test_db: str) -> None:
    all_block_dicts = [*BLOCKS, _PANEL_BUILDER_WITH_TEST_GATE, _DEPRECATED_PANEL_BUILDER]
    with admin_session() as session:
        needed_contracts = {
            io["contract"] for block in all_block_dicts for io in block["consumes"] + block["produces"]
        }
        contracts = {name_version: _get_or_create_contract(session, name_version) for name_version in needed_contracts}
        for block_dict in all_block_dicts:
            _get_or_create_block_version(session, block_dict, contracts)


def _grant(tid: uuid.UUID) -> None:
    with admin_session() as session:
        grant_all_blocks(session, tid)


def _canonical_plan(*, force_positive: bool = False, n3_gate_policy: str | None = "block") -> PlanIR:
    """panel_builder -> elasticity_dml -> post_model_checks, the explicit-node
    shape from evals/fixtures/example_plan_ir.yaml. post_model_checks is a
    first-class plan node carrying its own `gate: {policy: block}` — the
    catalog's auto-invoke path must defer to it (no double execution)."""
    n3: dict = {
        "id": "n3",
        "block": "post_model_checks@1.0",
        "inputs": {"surface": "n2.surface"},
    }
    if n3_gate_policy is not None:
        n3["gate"] = {"policy": n3_gate_policy}
    return PlanIR.model_validate(
        {
            "plan_ir_version": 1,
            "intent_summary": "executor test: canonical explicit-gate-node pipeline",
            "nodes": [
                {
                    "id": "n1",
                    "block": "panel_builder@1.0",
                    "params": {"brand": "demo", "platform": "blinkit", "window": {"from": "2026-01", "to": "2026-03"}},
                },
                {
                    "id": "n2",
                    "block": "elasticity_dml@1.0",
                    "params": {"force_positive_elasticities": force_positive},
                    "inputs": {"panel": "n1.panel"},
                },
                n3,
            ],
            "outputs": {"surface": "n2.surface"},
            "assumptions": [],
            "estimated_cost": {"class": "seconds"},
        }
    )


def _canonical_plan_with_bad_window() -> PlanIR:
    """Same DAG as _canonical_plan, but n1's window is a schema-valid string
    that isn't a real date — passes params_schema (jsonschema only checks
    type), crashes inside panel_builder's own run() at execution time."""
    plan_dict = _canonical_plan().model_dump(mode="json", by_alias=True)
    for node in plan_dict["nodes"]:
        if node["id"] == "n1":
            node["params"]["window"]["from"] = "not-a-date"
    return PlanIR.model_validate(plan_dict)


def _panel_only_plan(*, block_ref: str, gate_override: str | None = None) -> PlanIR:
    node: dict = {
        "id": "n1",
        "block": block_ref,
        "params": {"brand": "demo", "platform": "blinkit", "window": {"from": "2026-01", "to": "2026-02"}},
    }
    if gate_override is not None:
        node["gate"] = {"policy": gate_override}
    return PlanIR.model_validate(
        {
            "plan_ir_version": 1,
            "intent_summary": "executor test: path-B defense-in-depth via synthetic panel_builder post-gate",
            "nodes": [node],
            "outputs": {},
            "assumptions": [],
            "estimated_cost": {"class": "seconds"},
        }
    )


def test_explicit_gate_node_executes_once_and_passes(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant(tenant_id)
    plan = _canonical_plan()

    with tenant_session(tenant_id) as session:
        result = execute_plan(session, tenant_id, plan, actor="tester")

    assert result.status == "succeeded"
    assert result.node_statuses == {"n1": "succeeded", "n2": "succeeded", "n3": "succeeded"}
    assert "surface" in result.outputs

    with tenant_session(tenant_id) as session:
        node_execs = {
            ne.node_id: ne
            for ne in session.query(NodeExecution).filter_by(run_id=uuid.UUID(result.run_id)).all()
        }
        assert set(node_execs) == {"n1", "n2", "n3"}
        for node_id in ("n1", "n2", "n3"):
            assert node_execs[node_id].status == "succeeded"

        # exactly one gate verdict — the auto-invoke path must have deferred
        # to the explicit n3 node, not double-run post_model_checks.
        verdicts = session.query(GateVerdict).filter_by(run_id=uuid.UUID(result.run_id)).all()
        assert len(verdicts) == 1
        assert verdicts[0].node_id == "n3"
        assert verdicts[0].check_suite == "post_model_checks"
        assert verdicts[0].verdict == "pass"

        run_row = session.query(Run).filter_by(id=uuid.UUID(result.run_id)).one()
        assert run_row.status == "succeeded"


def test_determinism_same_plan_twice_yields_identical_output_hashes(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    _grant(tenant_id)
    plan = _canonical_plan()

    with tenant_session(tenant_id) as session:
        first = execute_plan(session, tenant_id, plan, actor="tester")
    with tenant_session(tenant_id) as session:
        second = execute_plan(session, tenant_id, plan, actor="tester")

    assert first.status == second.status == "succeeded"
    assert first.output_hashes == second.output_hashes
    assert first.input_hashes == second.input_hashes


def test_explicit_gate_node_failure_halts_run_and_persists_gate_failed(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    _grant(tenant_id)
    plan = _canonical_plan(force_positive=True)

    with tenant_session(tenant_id) as session:
        result = execute_plan(session, tenant_id, plan, actor="tester")

    assert result.status == "failed"
    assert result.node_statuses["n1"] == "succeeded"
    assert result.node_statuses["n2"] == "succeeded"
    assert result.node_statuses["n3"] == "gate_failed"
    assert result.outputs == {}

    with tenant_session(tenant_id) as session:
        node_execs = {
            ne.node_id: ne
            for ne in session.query(NodeExecution).filter_by(run_id=uuid.UUID(result.run_id)).all()
        }
        assert node_execs["n1"].status == "succeeded"
        assert node_execs["n2"].status == "succeeded"
        # durable in the DB — the validation node ran fine, its own gate condemned it.
        assert node_execs["n3"].status == "gate_failed"

        verdicts = session.query(GateVerdict).filter_by(run_id=uuid.UUID(result.run_id)).all()
        assert len(verdicts) == 1
        assert verdicts[0].node_id == "n3"
        assert verdicts[0].check_suite == "post_model_checks"
        assert verdicts[0].verdict == "fail"

        run_row = session.query(Run).filter_by(id=uuid.UUID(result.run_id)).one()
        assert run_row.status == "failed"


def test_node_exception_halts_run_with_verbatim_error_and_skips_downstream(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    _grant(tenant_id)
    plan = _canonical_plan_with_bad_window()

    with tenant_session(tenant_id) as session:
        result = execute_plan(session, tenant_id, plan, actor="tester")

    assert result.status == "failed"
    assert result.node_statuses["n1"] == "failed"
    assert result.node_statuses["n2"] == "skipped"
    assert result.node_statuses["n3"] == "skipped"
    assert result.outputs == {}
    # a block exception is a DOMAIN failure, caught by the per-node guard —
    # never classified as a runner-internal crash.
    assert result.internal_error is None

    with tenant_session(tenant_id) as session:
        node_execs = {
            ne.node_id: ne
            for ne in session.query(NodeExecution).filter_by(run_id=uuid.UUID(result.run_id)).all()
        }
        assert node_execs["n1"].status == "failed"
        assert node_execs["n1"].error is not None
        assert "isoformat" in node_execs["n1"].error.lower() or "invalid" in node_execs["n1"].error.lower()
        assert not node_execs["n1"].error.startswith("RUNNER_INTERNAL:")
        assert node_execs["n2"].status == "skipped"
        assert node_execs["n3"].status == "skipped"

        run_row = session.query(Run).filter_by(id=uuid.UUID(result.run_id)).one()
        assert run_row.status == "failed"

        audit_actions = {ev.action for ev in session.query(AuditEvent).filter_by(subject=result.run_id).all()}
        assert "run.failed" in audit_actions
        assert "run.crashed" not in audit_actions


def test_runner_internal_crash_is_distinguishable_from_domain_failure(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates a harness defect (not domain behavior) by monkeypatching a
    runner-internal helper — called directly inside gate path B's loop,
    outside any of its own try/excepts — to raise. Proves the outer catch-all
    classifies this distinctly from a block exception or a gate condemning
    its input."""
    _grant(tenant_id)
    plan = _panel_only_plan(block_ref="panel_builder@1.1", gate_override=None)

    def _broken(*args, **kwargs):
        raise RuntimeError("simulated runner defect")

    monkeypatch.setattr("core.execute.runner._has_explicit_downstream_gate", _broken)

    with tenant_session(tenant_id) as session:
        result = execute_plan(session, tenant_id, plan, actor="tester")

    assert result.status == "failed"
    assert result.internal_error is not None
    assert result.internal_error.startswith("RUNNER_INTERNAL:")
    assert "simulated runner defect" in result.internal_error
    assert result.node_statuses["n1"] == "failed"

    with tenant_session(tenant_id) as session:
        node_execs = {
            ne.node_id: ne
            for ne in session.query(NodeExecution).filter_by(run_id=uuid.UUID(result.run_id)).all()
        }
        assert node_execs["n1"].status == "failed"
        assert node_execs["n1"].error is not None
        assert node_execs["n1"].error.startswith("RUNNER_INTERNAL:")

        audit_actions = {ev.action for ev in session.query(AuditEvent).filter_by(subject=result.run_id).all()}
        assert "run.crashed" in audit_actions
        assert "run.failed" not in audit_actions

        run_row = session.query(Run).filter_by(id=uuid.UUID(result.run_id)).one()
        assert run_row.status == "failed"


def test_auto_gate_defense_in_depth_warn_policy_records_and_continues(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    """No explicit downstream validation node at all — the catalog's post
    GateBinding on panel_builder@1.1 auto-invokes post_model_checks (path B).
    The contract mismatch (dml_panel@v1 vs post_model_checks' expected
    elasticity_surface@v1) makes it fail every time; the node-level `gate`
    override downgrades the catalog's block policy to warn, so the run still
    succeeds — proving path B fires and 'warn' truly just records."""
    _grant(tenant_id)
    plan = _panel_only_plan(block_ref="panel_builder@1.1", gate_override="warn")

    with tenant_session(tenant_id) as session:
        result = execute_plan(session, tenant_id, plan, actor="tester")

    assert result.status == "succeeded"
    assert result.node_statuses["n1"] == "succeeded"

    with tenant_session(tenant_id) as session:
        verdicts = session.query(GateVerdict).filter_by(run_id=uuid.UUID(result.run_id)).all()
        assert len(verdicts) == 1
        assert verdicts[0].node_id == "n1"
        assert verdicts[0].check_suite == "post_model_checks"
        assert verdicts[0].verdict == "fail"

        run_row = session.query(Run).filter_by(id=uuid.UUID(result.run_id)).one()
        assert run_row.status == "succeeded"


def test_auto_gate_defense_in_depth_block_policy_halts(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    """Same synthetic binding, no node-level override — the catalog's
    policy=block on panel_builder@1.1 applies, halting the run durably."""
    _grant(tenant_id)
    plan = _panel_only_plan(block_ref="panel_builder@1.1", gate_override=None)

    with tenant_session(tenant_id) as session:
        result = execute_plan(session, tenant_id, plan, actor="tester")

    assert result.status == "failed"
    assert result.node_statuses["n1"] == "gate_failed"

    with tenant_session(tenant_id) as session:
        node_execs = {
            ne.node_id: ne
            for ne in session.query(NodeExecution).filter_by(run_id=uuid.UUID(result.run_id)).all()
        }
        assert node_execs["n1"].status == "gate_failed"

        verdicts = session.query(GateVerdict).filter_by(run_id=uuid.UUID(result.run_id)).all()
        assert len(verdicts) == 1
        assert verdicts[0].verdict == "fail"

        run_row = session.query(Run).filter_by(id=uuid.UUID(result.run_id)).one()
        assert run_row.status == "failed"


def test_ungranted_tenant_rejected_with_not_granted(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    """No grants at all for this tenant — execute_plan must fail closed before
    touching the DAG: no Run row, a Plan row recording the rejection."""
    plan = _canonical_plan()

    with tenant_session(tenant_id) as session:
        with pytest.raises(PlanRejectedError) as exc_info:
            execute_plan(session, tenant_id, plan, actor="tester")

    codes = {error.code for error in exc_info.value.errors}
    assert "not_granted" in codes

    with tenant_session(tenant_id) as session:
        plan_row = session.query(Plan).filter_by(id=exc_info.value.plan_id).one()
        assert plan_row.status == "invalid"
        assert plan_row.validation_errors is not None
        assert session.query(Run).filter_by(plan_id=plan_row.id).count() == 0


def test_inactive_block_rejected(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    plan = _panel_only_plan(block_ref="panel_builder@0.9")

    with tenant_session(tenant_id) as session:
        with pytest.raises(PlanRejectedError) as exc_info:
            execute_plan(session, tenant_id, plan, actor="tester")

    codes = {error.code for error in exc_info.value.errors}
    assert "inactive_block" in codes

    with tenant_session(tenant_id) as session:
        plan_row = session.query(Plan).filter_by(id=exc_info.value.plan_id).one()
        assert plan_row.status == "invalid"
        assert session.query(Run).filter_by(plan_id=plan_row.id).count() == 0


def test_downstream_node_without_gate_policy_rejected_by_missing_gate(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    """Closes the hole a reviewer found live: a plan wiring post_model_checks
    downstream of elasticity_dml BY NAME but without gate.policy=block would
    previously let gate path A never fire (requires node.gate) while gate path
    B deferred to it by name match alone — silently publishing an ungated
    model output. Now validate_plan rejects such a plan outright; execute_plan
    never reaches the DAG at all."""
    _grant(tenant_id)
    plan = _canonical_plan(n3_gate_policy=None)

    with tenant_session(tenant_id) as session:
        with pytest.raises(PlanRejectedError) as exc_info:
            execute_plan(session, tenant_id, plan, actor="tester")

    codes = {error.code for error in exc_info.value.errors}
    assert "missing_gate" in codes
    assert any(error.node_id == "n2" for error in exc_info.value.errors if error.code == "missing_gate")

    with tenant_session(tenant_id) as session:
        plan_row = session.query(Plan).filter_by(id=exc_info.value.plan_id).one()
        assert plan_row.status == "invalid"
        assert session.query(Run).filter_by(plan_id=plan_row.id).count() == 0
