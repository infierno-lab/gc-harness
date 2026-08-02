import uuid
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.db.models._mixins import UUIDPKMixin

# block, block_version, contract, gate_binding, metric are global (catalog is
# shared across tenants — it's code, not tenant data). block_grant and
# knowledge_source carry tenant_id per spec §4.4 / §9.3.


class Block(UUIDPKMixin, Base):
    __tablename__ = "block"

    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    domain: Mapped[str] = mapped_column(String, nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default="{}")

    __table_args__ = (
        CheckConstraint(
            "kind in ('query','metric','data','feature','model','scenario',"
            "'optimizer','validation','reporting','promotion','pipeline')",
            name="ck_block_kind",
        ),
    )


class BlockVersion(UUIDPKMixin, Base):
    __tablename__ = "block_version"

    block_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("block.id"), nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    git_sha: Mapped[str] = mapped_column(String, nullable=False)
    image_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    entrypoint: Mapped[str] = mapped_column(String, nullable=False)
    params_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    cost_class: Mapped[str] = mapped_column(String, nullable=False)
    sideeffect_class: Mapped[str] = mapped_column(String, nullable=False)
    when_to_use: Mapped[str | None] = mapped_column(Text, nullable=True)
    when_not_to_use: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="active")

    __table_args__ = (
        UniqueConstraint("block_id", "version", name="uq_block_version_block_version"),
        CheckConstraint(
            "cost_class in ('instant','seconds','minutes','hours')", name="ck_block_version_cost_class"
        ),
        CheckConstraint(
            "sideeffect_class in ('read','compute','write_artifact','mutate_state','external')",
            name="ck_block_version_sideeffect_class",
        ),
        CheckConstraint("status in ('active','deprecated','retired')", name="ck_block_version_status"),
    )


class Contract(UUIDPKMixin, Base):
    __tablename__ = "contract"

    name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    json_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="active")

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_contract_name_version"),
        CheckConstraint("status in ('active','deprecated','retired')", name="ck_contract_status"),
    )


class BlockIO(Base):
    __tablename__ = "block_io"

    block_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("block_version.id"), primary_key=True
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contract.id"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String, primary_key=True)
    port_name: Mapped[str] = mapped_column(String, primary_key=True)

    __table_args__ = (CheckConstraint("role in ('consumes','produces')", name="ck_block_io_role"),)


class GateBinding(UUIDPKMixin, Base):
    __tablename__ = "gate_binding"

    block_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("block_version.id"), nullable=False
    )
    check_suite: Mapped[str] = mapped_column(String, nullable=False)
    when: Mapped[str] = mapped_column(String, nullable=False)
    policy: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        CheckConstraint('"when" in (\'pre\',\'post\')', name="ck_gate_binding_when"),
        CheckConstraint("policy in ('block','warn')", name="ck_gate_binding_policy"),
    )


class Metric(UUIDPKMixin, Base):
    __tablename__ = "metric"

    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    domain: Mapped[str] = mapped_column(String, nullable=False)
    definition_sql: Mapped[str] = mapped_column(Text, nullable=False)
    dimensions: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    grain: Mapped[str] = mapped_column(String, nullable=False)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="active")

    __table_args__ = (CheckConstraint("status in ('active','deprecated','retired')", name="ck_metric_status"),)


class BlockGrant(UUIDPKMixin, Base):
    __tablename__ = "block_grant"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    block_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("block_version.id"), nullable=False
    )
    params_constraints: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "block_version_id", name="uq_block_grant_tenant_block_version"),
    )


class KnowledgeSource(UUIDPKMixin, Base):
    __tablename__ = "knowledge_source"

    # nullable = shared/global corpus (spec §9.3); tenant-scoped sources set it.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    uri: Mapped[str] = mapped_column(String, nullable=False)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    version_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    freshness_policy: Mapped[str | None] = mapped_column(String, nullable=True)
