"""node_execution: allow durable 'gate_failed' status

A blocking gate that condemns a node's output is a distinct outcome from a
runtime exception ('failed') — the block itself ran fine, the gate blocked
it. The executor now persists this status; widen the CHECK constraint.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-31

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_STATUSES = "'pending','running','succeeded','failed','skipped'"
_NEW_STATUSES = "'pending','running','succeeded','failed','skipped','gate_failed'"


def upgrade() -> None:
    op.drop_constraint("ck_node_execution_status", "node_execution", type_="check")
    op.create_check_constraint("ck_node_execution_status", "node_execution", f"status in ({_NEW_STATUSES})")


def downgrade() -> None:
    op.drop_constraint("ck_node_execution_status", "node_execution", type_="check")
    op.create_check_constraint("ck_node_execution_status", "node_execution", f"status in ({_OLD_STATUSES})")
