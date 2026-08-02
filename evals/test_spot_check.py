"""Reproducibility spot-check tests (spec §9, §5.4) — over the Demand pack's
demo blocks, same DAG shape as evals/test_executor.py's canonical plan.

Catalog rows are seeded directly via ORM here (get-or-create, not via
core.catalog.seed.apply_seed) rather than imported from evals/test_executor.py,
to keep this file's tests fully decoupled from a concurrently-changing sibling
module — block/contract tables are global (not tenant-scoped) so this
naturally coexists with whatever seeding other test modules do into the same
catalog.
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
    NodeExecution,
    Run,
)
from core.db.session import admin_session, tenant_session
from core.execute.runner import execute_plan
from core.execute.spot_check import run_spot_check
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


@pytest.fixture(scope="session", autouse=True)
def seeded_spot_check_catalog(migrated_test_db: str) -> None:
    with admin_session() as session:
        needed_contracts = {io["contract"] for block in BLOCKS for io in block["consumes"] + block["produces"]}
        contracts = {name_version: _get_or_create_contract(session, name_version) for name_version in needed_contracts}
        for block_dict in BLOCKS:
            _get_or_create_block_version(session, block_dict, contracts)


def _grant(tid: uuid.UUID) -> None:
    with admin_session() as session:
        grant_all_blocks(session, tid)


def _canonical_plan(*, force_positive: bool = False) -> PlanIR:
    """panel_builder -> elasticity_dml -> post_model_checks, an explicit gate
    node with policy=block — identical shape to test_executor.py's canonical
    plan, defined locally to keep this module decoupled."""
    return PlanIR.model_validate(
        {
            "plan_ir_version": 1,
            "intent_summary": "spot-check test: canonical pipeline",
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
                {
                    "id": "n3",
                    "block": "post_model_checks@1.0",
                    "inputs": {"surface": "n2.surface"},
                    "gate": {"policy": "block"},
                },
            ],
            "outputs": {"surface": "n2.surface"},
            "assumptions": [],
            "estimated_cost": {"class": "seconds"},
        }
    )


def test_spot_check_all_match_creates_replay_runs_and_audit_event(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    _grant(tenant_id)
    plan = _canonical_plan()

    with tenant_session(tenant_id) as session:
        original = execute_plan(session, tenant_id, plan, actor="tester")
    assert original.status == "succeeded"

    with tenant_session(tenant_id) as session:
        report = run_spot_check(session, tenant_id, sample_size=3, actor="spot-check")

    assert len(report.checked) == 1
    result = report.checked[0]
    assert result.original_run_id == original.run_id
    assert result.replay_run_id != original.run_id
    assert result.match is True
    assert result.mismatches == []
    assert report.all_match is True
    assert report.skipped == []

    with tenant_session(tenant_id) as session:
        # the replay is a real, separately audited run — not a dry-run.
        replay_run = session.query(Run).filter_by(id=uuid.UUID(result.replay_run_id)).one()
        assert replay_run.status == "succeeded"

        audit_actions = [ev.action for ev in session.query(AuditEvent).all() if ev.tenant_id == tenant_id]
        assert "spot_check.completed" in audit_actions
        assert "spot_check.MISMATCH" not in audit_actions


def test_spot_check_detects_tampered_original_hash_as_mismatch(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    _grant(tenant_id)
    plan = _canonical_plan()

    with tenant_session(tenant_id) as session:
        original = execute_plan(session, tenant_id, plan, actor="tester")
    assert original.status == "succeeded"

    # Simulate drift: overwrite the recorded output hash on the original run's
    # n2 (elasticity_dml) node execution row directly in the DB.
    with tenant_session(tenant_id) as session:
        node_exec = (
            session.query(NodeExecution)
            .filter_by(run_id=uuid.UUID(original.run_id), node_id="n2")
            .one()
        )
        real_hash = node_exec.output_hashes["surface"]
        tampered_hash = "0" * len(real_hash)
        node_exec.output_hashes = {**node_exec.output_hashes, "surface": tampered_hash}

    with tenant_session(tenant_id) as session:
        report = run_spot_check(session, tenant_id, sample_size=3, actor="spot-check")

    assert len(report.checked) == 1
    result = report.checked[0]
    assert result.match is False
    assert report.all_match is False
    assert len(result.mismatches) == 1
    mismatch = result.mismatches[0]
    assert mismatch.node_id == "n2"
    assert mismatch.port == "surface"
    assert mismatch.original_hash == tampered_hash
    assert mismatch.replay_hash == real_hash

    with tenant_session(tenant_id) as session:
        audit_actions = [ev.action for ev in session.query(AuditEvent).all() if ev.tenant_id == tenant_id]
        assert "spot_check.MISMATCH" in audit_actions
        assert "spot_check.completed" not in audit_actions


def test_spot_check_dedupes_by_plan_ir_hash(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    _grant(tenant_id)
    plan = _canonical_plan()

    with tenant_session(tenant_id) as session:
        first = execute_plan(session, tenant_id, plan, actor="tester")
    with tenant_session(tenant_id) as session:
        second = execute_plan(session, tenant_id, plan, actor="tester")
    assert first.status == second.status == "succeeded"

    with tenant_session(tenant_id) as session:
        report = run_spot_check(session, tenant_id, sample_size=3, actor="spot-check")

    # Same plan (identical ir_hash) ran twice originally — checked once, not twice.
    assert len(report.checked) == 1
    assert report.checked[0].original_run_id == second.run_id  # most recent first
    assert report.all_match is True


def test_spot_check_empty_tenant_reports_nothing_to_check_without_crashing(
    migrated_test_db: str, other_tenant_id: uuid.UUID
) -> None:
    with tenant_session(other_tenant_id) as session:
        report = run_spot_check(session, other_tenant_id, sample_size=3, actor="spot-check")

    assert report.checked == []
    assert report.skipped == []
    assert report.all_match is True

    with tenant_session(other_tenant_id) as session:
        audit_actions = [ev.action for ev in session.query(AuditEvent).all() if ev.tenant_id == other_tenant_id]
        assert "spot_check.completed" in audit_actions
