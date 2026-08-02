"""Plan validator tests (spec §5.6) — one failing case per error code, plus a
fully-valid plan and a cross-tenant RLS sanity check.
"""

import uuid

import pytest
from sqlalchemy import select

from core.catalog.seed import apply_seed, grant_all_blocks
from core.catalog.store import resolve_block
from core.db.models import BlockGrant
from core.db.session import admin_session, tenant_session
from core.plan.plan_ir import PlanIR
from core.plan.validator import validate_plan
from evals.fixtures.catalog_blocks import demo_blocks


@pytest.fixture(scope="session", autouse=True)
def _seed_catalog(migrated_test_db: str) -> None:
    with admin_session() as session:
        apply_seed(session, demo_blocks())


def _grant_all(tid: uuid.UUID) -> None:
    with admin_session() as session:
        grant_all_blocks(session, tid)


def _set_constraint(tid: uuid.UUID, block_name_version: str, constraints: dict) -> None:
    with admin_session() as session:
        block_version = resolve_block(session, block_name_version)
        assert block_version is not None
        grant = session.execute(
            select(BlockGrant).where(
                BlockGrant.tenant_id == tid, BlockGrant.block_version_id == block_version.id
            )
        ).scalar_one()
        grant.params_constraints = constraints


def _valid_plan_dict() -> dict:
    return {
        "plan_ir_version": 1,
        "intent_summary": "valid demo plan",
        "nodes": [
            {"id": "n1", "block": "panel_builder@2.3", "params": {"brand": "demo"}},
            {"id": "n2", "block": "elasticity_dml@1.4", "inputs": {"panel": "n1.panel"}},
            {
                "id": "n3",
                "block": "post_model_checks@1.1",
                "inputs": {"surface": "n2.surface"},
                "gate": {"policy": "block"},
            },
        ],
        "outputs": {"surface": "n2.surface"},
        "assumptions": [],
        "estimated_cost": {"class": "minutes"},
    }


def _plan(nodes: list[dict], outputs: dict | None = None) -> PlanIR:
    return PlanIR.model_validate(
        {
            "plan_ir_version": 1,
            "intent_summary": "test plan",
            "nodes": nodes,
            "outputs": outputs or {},
            "assumptions": [],
            "estimated_cost": {"class": "hours"},
        }
    )


def test_valid_plan_passes(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant_all(tenant_id)
    plan = PlanIR.model_validate(_valid_plan_dict())

    with tenant_session(tenant_id) as session:
        result = validate_plan(session, tenant_id, plan)

    assert result.errors == []
    assert result.valid is True
    assert result.requires_approval == []


def test_not_granted_rls_sanity(migrated_test_db: str, other_tenant_id: uuid.UUID) -> None:
    # other_tenant_id is never granted anything — plan is otherwise valid.
    plan = PlanIR.model_validate(_valid_plan_dict())

    with tenant_session(other_tenant_id) as session:
        result = validate_plan(session, other_tenant_id, plan)

    assert result.valid is False
    codes = {err.code for err in result.errors}
    assert "not_granted" in codes
    not_granted_nodes = {err.node_id for err in result.errors if err.code == "not_granted"}
    assert not_granted_nodes == {"n1", "n2", "n3"}


def test_unknown_block(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant_all(tenant_id)
    plan = _plan([{"id": "n1", "block": "does_not_exist@v1", "params": {}}])

    with tenant_session(tenant_id) as session:
        result = validate_plan(session, tenant_id, plan)

    assert result.valid is False
    assert any(err.code == "unknown_block" and err.node_id == "n1" for err in result.errors)


def test_inactive_block(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant_all(tenant_id)
    plan = _plan([{"id": "n1", "block": "legacy_block@1.0", "params": {}}])

    with tenant_session(tenant_id) as session:
        result = validate_plan(session, tenant_id, plan)

    assert result.valid is False
    assert any(err.code == "inactive_block" and err.node_id == "n1" for err in result.errors)


def test_duplicate_node_id(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant_all(tenant_id)
    plan = _plan(
        [
            {"id": "n1", "block": "panel_builder@2.3", "params": {"brand": "demo"}},
            {"id": "n1", "block": "panel_builder@2.3", "params": {"brand": "demo"}},
        ]
    )

    with tenant_session(tenant_id) as session:
        result = validate_plan(session, tenant_id, plan)

    assert result.valid is False
    assert any(err.code == "duplicate_node_id" and err.node_id == "n1" for err in result.errors)


def test_unbound_input(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant_all(tenant_id)
    plan = _plan(
        [
            {"id": "n1", "block": "panel_builder@2.3", "params": {"brand": "demo"}},
            {"id": "n2", "block": "elasticity_dml@1.4", "inputs": {"panel": "n1.nope"}},
        ]
    )

    with tenant_session(tenant_id) as session:
        result = validate_plan(session, tenant_id, plan)

    assert result.valid is False
    assert any(err.code == "unbound_input" and err.node_id == "n2" for err in result.errors)


def test_dangling_output(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant_all(tenant_id)
    plan = _plan(
        [{"id": "n1", "block": "panel_builder@2.3", "params": {"brand": "demo"}}],
        outputs={"result": "n1.wrongport"},
    )

    with tenant_session(tenant_id) as session:
        result = validate_plan(session, tenant_id, plan)

    assert result.valid is False
    assert any(err.code == "dangling_output" for err in result.errors)


def test_cycle(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant_all(tenant_id)
    plan = _plan(
        [
            {"id": "n1", "block": "panel_builder@2.3", "params": {"brand": "demo"}, "inputs": {"x": "n2.y"}},
            {"id": "n2", "block": "panel_builder@2.3", "params": {"brand": "demo"}, "inputs": {"x": "n1.y"}},
        ]
    )

    with tenant_session(tenant_id) as session:
        result = validate_plan(session, tenant_id, plan)

    assert result.valid is False
    assert any(err.code == "cycle" for err in result.errors)


def test_contract_mismatch(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant_all(tenant_id)
    plan = _plan(
        [
            {"id": "n1", "block": "panel_builder@2.3", "params": {"brand": "demo"}},
            {"id": "n2", "block": "bad_consumer@1.0", "inputs": {"panel": "n1.panel"}},
        ]
    )

    with tenant_session(tenant_id) as session:
        result = validate_plan(session, tenant_id, plan)

    assert result.valid is False
    assert any(err.code == "contract_mismatch" and err.node_id == "n2" for err in result.errors)


def test_params_invalid(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant_all(tenant_id)
    plan = _plan([{"id": "n1", "block": "panel_builder@2.3", "params": {}}])

    with tenant_session(tenant_id) as session:
        result = validate_plan(session, tenant_id, plan)

    assert result.valid is False
    param_errors = [err for err in result.errors if err.code == "params_invalid"]
    assert param_errors
    assert param_errors[0].node_id == "n1"
    assert "pointer" in param_errors[0].details


def test_params_constraint_violation_mismatched(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant_all(tenant_id)
    _set_constraint(tenant_id, "panel_builder@2.3", {"brand": "demo"})
    plan = _plan([{"id": "n1", "block": "panel_builder@2.3", "params": {"brand": "other"}}])

    with tenant_session(tenant_id) as session:
        result = validate_plan(session, tenant_id, plan)

    assert result.valid is False
    codes = {err.code for err in result.errors}
    assert "params_constraint_violation" in codes
    assert "not_granted" not in codes
    violations = [
        err for err in result.errors if err.code == "params_constraint_violation" and err.node_id == "n1"
    ]
    assert len(violations) == 1
    assert violations[0].details == {"param": "brand", "expected": "demo", "actual": "other", "reason": "mismatched"}


def test_params_constraint_violation_omitted_param_evades_nothing(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    # elasticity_dml@1.4's params_schema has no required fields, so an empty
    # params dict is otherwise schema-valid — omitting a grant-constrained key
    # must still be caught, not silently pass through to a block-side default.
    _grant_all(tenant_id)
    _set_constraint(tenant_id, "elasticity_dml@1.4", {"mode": "champion"})
    plan = _plan([{"id": "n1", "block": "elasticity_dml@1.4", "params": {}}])

    with tenant_session(tenant_id) as session:
        result = validate_plan(session, tenant_id, plan)

    assert result.valid is False
    violations = [
        err for err in result.errors if err.code == "params_constraint_violation" and err.node_id == "n1"
    ]
    assert len(violations) == 1
    assert violations[0].details == {"param": "mode", "expected": "champion", "reason": "missing"}


def test_sideeffect_policy_requires_approver(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant_all(tenant_id)
    plan = _plan([{"id": "n1", "block": "promote_model@1.0", "params": {}}])

    with tenant_session(tenant_id) as session:
        without_approver = validate_plan(session, tenant_id, plan, actor_roles=[])
    assert without_approver.valid is False
    assert any(err.code == "sideeffect_policy" and err.node_id == "n1" for err in without_approver.errors)
    assert without_approver.requires_approval == ["n1"]

    with tenant_session(tenant_id) as session:
        with_approver = validate_plan(session, tenant_id, plan, actor_roles=["approver"])
    assert not any(err.code == "sideeffect_policy" for err in with_approver.errors)
    # still flagged even though the role satisfies the policy.
    assert with_approver.requires_approval == ["n1"]


def test_missing_gate(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant_all(tenant_id)
    plan = _plan(
        [
            {"id": "n1", "block": "panel_builder@2.3", "params": {"brand": "demo"}},
            {"id": "n2", "block": "elasticity_dml@1.4", "inputs": {"panel": "n1.panel"}},
        ]
    )

    with tenant_session(tenant_id) as session:
        result = validate_plan(session, tenant_id, plan)

    assert result.valid is False
    assert any(err.code == "missing_gate" and err.node_id == "n2" for err in result.errors)


def test_cost_budget(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant_all(tenant_id)
    plan = _plan([{"id": "n1", "block": "heavy_query@1.0", "params": {}}])

    with tenant_session(tenant_id) as session:
        result = validate_plan(session, tenant_id, plan, budget_cost_class="minutes")

    assert result.valid is False
    assert any(err.code == "cost_budget" and err.node_id == "n1" for err in result.errors)


def test_all_errors_are_collected_not_short_circuited(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant_all(tenant_id)
    plan = _plan(
        [
            {"id": "n1", "block": "does_not_exist@v1", "params": {}},
            {"id": "n1", "block": "legacy_block@1.0", "params": {}},
        ]
    )

    with tenant_session(tenant_id) as session:
        result = validate_plan(session, tenant_id, plan)

    codes = [err.code for err in result.errors]
    assert "duplicate_node_id" in codes
    # both node definitions under the duplicated id are still checked.
    assert codes.count("unknown_block") + codes.count("inactive_block") >= 1
