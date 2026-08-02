import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.db.models._mixins import CreatedAtMixin, UUIDPKMixin


class Tenant(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "tenant"

    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="active")

    __table_args__ = (
        CheckConstraint("status in ('active','suspended','deleted')", name="ck_tenant_status"),
    )


class AppUser(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "app_user"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    roles: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="active")

    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_app_user_tenant_email"),
        CheckConstraint("status in ('active','disabled')", name="ck_app_user_status"),
    )
