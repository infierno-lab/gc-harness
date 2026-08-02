import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

_DEFAULT_APP_URL = "postgresql+psycopg://gc_app:gc_app@localhost:5433/gc_intel"
_DEFAULT_ADMIN_URL = "postgresql+psycopg://gc_intel:gc_intel@localhost:5433/gc_intel"


def database_url() -> str:
    return os.environ.get("GC_DATABASE_URL", _DEFAULT_APP_URL)


def admin_database_url() -> str:
    return os.environ.get("GC_ADMIN_DATABASE_URL", _DEFAULT_ADMIN_URL)


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_admin_engine: Engine | None = None
_admin_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """The `gc_app` engine — every tenant-scoped session runs through this role, which
    RLS is enforced against."""
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(database_url(), pool_pre_ping=True)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_admin_engine() -> Engine:
    """The `gc_app`-owning role's engine — bypasses RLS. Migrations and seeds only."""
    global _admin_engine, _admin_session_factory
    if _admin_engine is None:
        _admin_engine = create_engine(admin_database_url(), pool_pre_ping=True)
        _admin_session_factory = sessionmaker(bind=_admin_engine, expire_on_commit=False)
    return _admin_engine


@contextmanager
def tenant_session(tenant_id: str | uuid.UUID) -> Iterator[Session]:
    """The ONLY sanctioned way to get a session for request/task handling.

    Opens a transaction on the `gc_app` role and sets `app.tenant_id` for the
    duration of that transaction only (`set_config(..., is_local=true)` is
    `SET LOCAL` semantics via a bind parameter, not string-built SQL), so
    every query in the block is RLS-scoped to this tenant.
    """
    validated_tenant_id = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
    get_engine()
    assert _session_factory is not None
    session = _session_factory()
    try:
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(validated_tenant_id)},
        )
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def admin_session() -> Iterator[Session]:
    """Bypasses tenant scoping entirely (runs as the RLS-exempt owning role).
    Reserved for migrations and seed scripts — never for request handling."""
    get_admin_engine()
    assert _admin_session_factory is not None
    session = _admin_session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
