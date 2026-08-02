"""Shared fixtures for the intelligence-harness test suite."""

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from core.db.models import Tenant
from core.db.session import admin_session

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


@pytest.fixture()
def tenant_id(migrated_test_db: str) -> uuid.UUID:
    """A fresh tenant row for tests that need tenant-scoped state (grants, RLS)."""
    new_id = uuid.uuid4()
    with admin_session() as session:
        session.add(Tenant(id=new_id, name=f"tenant-{new_id}"))
    return new_id


@pytest.fixture()
def other_tenant_id(migrated_test_db: str) -> uuid.UUID:
    """A second tenant, granted nothing — for cross-tenant isolation checks."""
    new_id = uuid.uuid4()
    with admin_session() as session:
        session.add(Tenant(id=new_id, name=f"tenant-{new_id}"))
    return new_id
