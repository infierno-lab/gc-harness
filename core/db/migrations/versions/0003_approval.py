"""approval: two-person approval flow for mutate_state/external plans

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-02

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "approval",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("plan.id"), nullable=False),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("approver", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("run.id"), nullable=True),
        sa.Column("execution_outcome", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status in ('pending','approved','rejected')", name="ck_approval_status"),
        sa.CheckConstraint(
            "execution_outcome is null or execution_outcome in ('succeeded','failed','rejected')",
            name="ck_approval_execution_outcome",
        ),
    )
    op.create_index("ix_approval_tenant_id", "approval", ["tenant_id"])
    op.create_index("ix_approval_plan_id", "approval", ["plan_id"])

    # --- Row-level security, same pattern as 0001 --------------------------
    op.execute("ALTER TABLE approval ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE approval FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON approval
        USING (tenant_id = current_setting('app.tenant_id')::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid)
        """
    )

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON approval TO gc_app")


def downgrade() -> None:
    op.execute("REVOKE ALL PRIVILEGES ON approval FROM gc_app")
    op.drop_table("approval")
