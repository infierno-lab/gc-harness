import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.db.models._mixins import CreatedAtMixin, UUIDPKMixin


class Ask(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "ask"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    normalized_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    task_type: Mapped[str | None] = mapped_column(String, nullable=True)


class Plan(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "plan"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    ask_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ask.id"), nullable=False)
    ir: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ir_hash: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="proposed")
    validation_errors: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status in ('proposed','valid','invalid','approved','running','succeeded','failed')",
            name="ck_plan_status",
        ),
    )


class PlanTemplate(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "plan_template"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    normalized_hash: Mapped[str] = mapped_column(String, nullable=False)
    ir: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="active")

    __table_args__ = (
        CheckConstraint("status in ('active','deprecated','retired')", name="ck_plan_template_status"),
    )


class Approval(UUIDPKMixin, CreatedAtMixin, Base):
    """Two-person approval for a `mutate_state`/`external` plan (spec §4.2,
    §8): requested at ask time, decided by a named approver distinct from the
    requester before `execute_plan` ever runs the plan.

    `executed_run_id`/`execution_outcome` are populated only after an
    'approved' decision actually runs `execute_plan` — they make the
    approved-but-dead-on-arrival case (the catalog moved underneath between
    proposal and decision, so the post-approval re-validation itself fails)
    durably visible to a DB-only investigator, not just in the API response.
    """

    __tablename__ = "approval"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("plan.id"), nullable=False)
    requested_by: Mapped[str] = mapped_column(String, nullable=False)
    approver: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="pending")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("run.id"), nullable=True)
    execution_outcome: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint("status in ('pending','approved','rejected')", name="ck_approval_status"),
        CheckConstraint(
            "execution_outcome is null or execution_outcome in ('succeeded','failed','rejected')",
            name="ck_approval_execution_outcome",
        ),
    )


class Run(UUIDPKMixin, Base):
    __tablename__ = "run"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("plan.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','running','succeeded','failed','cancelled')", name="ck_run_status"
        ),
    )


class NodeExecution(UUIDPKMixin, Base):
    __tablename__ = "node_execution"

    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("run.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    node_id: Mapped[str] = mapped_column(String, nullable=False)
    block_version: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="pending")
    input_hashes: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output_hashes: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    envelope: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','running','succeeded','failed','skipped','gate_failed')",
            name="ck_node_execution_status",
        ),
    )


class GateVerdict(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "gate_verdict"

    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("run.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    node_id: Mapped[str] = mapped_column(String, nullable=False)
    check_suite: Mapped[str] = mapped_column(String, nullable=False)
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (CheckConstraint("verdict in ('pass','warn','fail')", name="ck_gate_verdict_verdict"),)


class LlmCall(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "llm_call"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    call_class: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String, nullable=False)
    response_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AuditEvent(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "audit_event"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
