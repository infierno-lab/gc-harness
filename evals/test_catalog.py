"""Seed idempotency and capability digest byte-stability (spec §4.4, §5.2)."""

import uuid

from core.catalog.digest import render_digest
from core.catalog.seed import apply_seed, grant_all_blocks
from core.db.session import admin_session, tenant_session
from evals.fixtures.catalog_blocks import demo_blocks, demo_metrics


def test_apply_seed_is_idempotent(migrated_test_db: str) -> None:
    blocks = demo_blocks()
    metrics = demo_metrics()
    expected_block_names = {f"{b['name']}@{b['version']}" for b in blocks}
    expected_metric_names = {m["name"] for m in metrics}

    # The test DB persists across runs, so the first call here may itself be a
    # no-op if a prior run already seeded identical content — that's exactly
    # the idempotency this test is checking. What must always hold is that a
    # *second* run right after is a pure no-op reporting everything unchanged.
    with admin_session() as session:
        apply_seed(session, blocks, metrics)

    with admin_session() as session:
        second = apply_seed(session, blocks, metrics)

    assert second.blocks_created == []
    assert second.blocks_updated == []
    assert set(second.blocks_unchanged) == expected_block_names
    assert second.metrics_created == []
    assert second.metrics_updated == []
    assert set(second.metrics_unchanged) == expected_metric_names


def test_apply_seed_updates_changed_content(migrated_test_db: str) -> None:
    with admin_session() as session:
        apply_seed(session, demo_blocks(), demo_metrics())

    changed = demo_blocks()
    changed[0]["when_to_use"] = "Updated description."

    with admin_session() as session:
        report = apply_seed(session, changed, demo_metrics())

    assert "panel_builder@2.3" in report.blocks_updated
    assert "panel_builder@2.3" not in report.blocks_unchanged


def test_digest_is_byte_stable_and_ordered(migrated_test_db: str) -> None:
    with admin_session() as session:
        apply_seed(session, demo_blocks(), demo_metrics())

    new_tenant_id = uuid.uuid4()
    from core.db.models import Tenant

    with admin_session() as session:
        session.add(Tenant(id=new_tenant_id, name=f"digest-tenant-{new_tenant_id}"))
        grant_all_blocks(session, new_tenant_id)

    with tenant_session(new_tenant_id) as session:
        first = render_digest(session, new_tenant_id)

    with tenant_session(new_tenant_id) as session:
        second = render_digest(session, new_tenant_id)

    assert first.text == second.text
    assert first.digest_hash == second.digest_hash

    lines = first.text.splitlines()
    block_lines = [line for line in lines if not line.startswith("metric:")]
    assert block_lines == sorted(block_lines)
    assert any(line.startswith("elasticity_dml@1.4 ") for line in block_lines)
    assert any("consumes: panel=dml_panel@v1" in line for line in block_lines)
    assert any(line.startswith("metric: loss_making_skus ") for line in lines)
    # deprecated versions never appear in the digest even when granted.
    assert not any(line.startswith("legacy_block@") for line in lines)


def test_digest_empty_for_ungranted_tenant(migrated_test_db: str) -> None:
    with admin_session() as session:
        apply_seed(session, demo_blocks(), demo_metrics())

    from core.db.models import Tenant

    ungranted_tenant_id = uuid.uuid4()
    with admin_session() as session:
        session.add(Tenant(id=ungranted_tenant_id, name=f"ungranted-{ungranted_tenant_id}"))

    with tenant_session(ungranted_tenant_id) as session:
        result = render_digest(session, ungranted_tenant_id)

    block_lines = [line for line in result.text.splitlines() if not line.startswith("metric:")]
    assert block_lines == []
