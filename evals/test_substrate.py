"""Substrate smoke tests (Phase 0 exit criteria): migration applies cleanly,
RLS actually isolates tenants, contracts round-trip, the example Plan IR parses.
"""

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from core.catalog.contracts import ColumnSpec, DmlPanelV1, contract_json_schema, get_contract
from core.catalog.hashing import content_hash
from core.plan.plan_ir import ir_hash, parse_plan_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

_DEFAULT_TEST_ADMIN_URL = "postgresql+psycopg://gc_intel:gc_intel@localhost:5433/gc_intel_test"
_DEFAULT_TEST_APP_URL = "postgresql+psycopg://gc_app:gc_app@localhost:5433/gc_intel_test"


def _test_admin_url() -> str:
    return os.environ.get("GC_TEST_ADMIN_DATABASE_URL", _DEFAULT_TEST_ADMIN_URL)


def _test_app_url() -> str:
    return os.environ.get("GC_TEST_DATABASE_URL", _DEFAULT_TEST_APP_URL)


def _db_reachable(url: str) -> bool:
    try:
        engine = create_engine(url)
    except Exception:
        return False
    try:
        with engine.connect():
            return True
    except Exception:
        return False
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def migrated_test_db() -> str:
    admin_url = _test_admin_url()
    app_url = _test_app_url()
    if not _db_reachable(admin_url):
        pytest.skip(
            f"test database unreachable at {admin_url} — start it with `make db-up` "
            "(or docker compose -f deploy/docker-compose.yml up -d)"
        )

    os.environ["GC_ADMIN_DATABASE_URL"] = admin_url
    os.environ["GC_DATABASE_URL"] = app_url
    os.environ["GC_ALEMBIC_DATABASE_URL"] = admin_url

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")
    return app_url


def test_migration_applies_cleanly(migrated_test_db: str) -> None:
    # migrated_test_db already ran the migration; re-running head must be a no-op.
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")


def test_rls_tenant_isolation(migrated_test_db: str) -> None:
    from core.db.models import Ask, Tenant
    from core.db.session import admin_session, get_engine, tenant_session

    tenant_a_id = uuid.uuid4()
    tenant_b_id = uuid.uuid4()

    with admin_session() as session:
        session.add(Tenant(id=tenant_a_id, name=f"tenant-a-{tenant_a_id}"))
        session.add(Tenant(id=tenant_b_id, name=f"tenant-b-{tenant_b_id}"))

    with tenant_session(tenant_a_id) as session:
        session.add(Ask(tenant_id=tenant_a_id, actor="tester", raw_text="a's ask"))

    with tenant_session(tenant_b_id) as session:
        session.add(Ask(tenant_id=tenant_b_id, actor="tester", raw_text="b's ask"))

    with tenant_session(tenant_a_id) as session:
        rows = session.query(Ask).all()
        assert [row.raw_text for row in rows] == ["a's ask"]

    with tenant_session(tenant_b_id) as session:
        rows = session.query(Ask).all()
        assert [row.raw_text for row in rows] == ["b's ask"]

    # gc_app with no app.tenant_id set at all must fail closed (error, not leak).
    with get_engine().connect() as conn, pytest.raises(Exception):
        conn.execute(text("SELECT * FROM ask")).fetchall()


def test_contracts_round_trip_and_hash_is_stable() -> None:
    schema = contract_json_schema("dml_panel@v1")
    assert schema["title"] == "DmlPanelV1"
    assert "ref" in schema["properties"]
    assert get_contract("dml_panel@v1") is DmlPanelV1

    same_content_different_order_a = {"x": 1, "y": {"b": 2, "a": 1}}
    same_content_different_order_b = {"y": {"a": 1, "b": 2}, "x": 1}
    assert content_hash(same_content_different_order_a) == content_hash(same_content_different_order_b)

    panel = DmlPanelV1(ref="res_01H9")
    assert content_hash(panel) == content_hash(panel.model_dump(mode="json", by_alias=True))


def test_hash_canonicalizes_nested_models_numbers_and_collections() -> None:
    # A BaseModel nested inside a dict (not just at the top level) must
    # canonicalize identically to its own manual model_dump.
    nested = {"a": ColumnSpec(name="sku", dtype="string"), "b": [1, 2, 3]}
    equivalent = {
        "a": {"name": "sku", "dtype": "string", "nullable": False, "unit": None, "description": None},
        "b": [1, 2, 3],
    }
    assert content_hash(nested) == content_hash(equivalent)

    # int and float representations of the same numeric value hash identically.
    assert content_hash({"x": 1}) == content_hash({"x": 1.0})
    assert content_hash([1, 2.0, 3]) != content_hash([1, 2.5, 3])

    # tuples hash like their list equivalent.
    assert content_hash({"t": (1, 2, 3)}) == content_hash({"t": [1, 2, 3]})

    # sets hash like their sorted list equivalent (set iteration order isn't stable).
    assert content_hash({"s": {3, 1, 2}}) == content_hash({"s": [1, 2, 3]})


def test_example_plan_ir_parses() -> None:
    plan = parse_plan_yaml(REPO_ROOT / "evals" / "fixtures" / "example_plan_ir.yaml")

    assert plan.plan_ir_version == 1
    assert [node.id for node in plan.nodes] == ["n1", "n2", "n3"]
    assert plan.nodes[0].block == "panel_builder@2.3"
    assert plan.nodes[1].inputs == {"panel": "n1.panel"}
    assert plan.nodes[2].gate is not None
    assert plan.nodes[2].gate.policy == "block"
    assert plan.outputs == {"surface": "n2.surface"}
    assert plan.estimated_cost.class_ == "minutes"

    digest = ir_hash(plan)
    assert isinstance(digest, str)
    assert len(digest) == 64
