"""initial schema: tenancy, catalog, control plane, RLS, gc_app role

Revision ID: 0001
Revises:
Create Date: 2026-07-31

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables carrying tenant_id, in the same order they're created below — every
# one of these gets RLS enabled+forced with a tenant_id-scoped policy.
_TENANT_SCOPED_TABLES = [
    "app_user",
    "block_grant",
    "ask",
    "plan",
    "plan_template",
    "run",
    "node_execution",
    "gate_verdict",
    "llm_call",
    "audit_event",
]

# knowledge_source has a nullable tenant_id (NULL = shared/global corpus, per
# spec §9.3) so its policy admits NULL rows for every tenant.
_NULLABLE_TENANT_TABLE = "knowledge_source"


def upgrade() -> None:
    op.create_table(
        "tenant",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status in ('active','suspended','deleted')", name="ck_tenant_status"),
        sa.UniqueConstraint("name", name="uq_tenant_name"),
    )

    op.create_table(
        "app_user",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("roles", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status in ('active','disabled')", name="ck_app_user_status"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_app_user_tenant_email"),
    )
    op.create_index("ix_app_user_tenant_id", "app_user", ["tenant_id"])

    op.create_table(
        "block",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("domain", sa.String(), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.CheckConstraint(
            "kind in ('query','metric','data','feature','model','scenario',"
            "'optimizer','validation','reporting','promotion','pipeline')",
            name="ck_block_kind",
        ),
        sa.UniqueConstraint("name", name="uq_block_name"),
    )

    op.create_table(
        "block_version",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("block_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("block.id"), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("git_sha", sa.String(), nullable=False),
        sa.Column("image_ref", sa.String(), nullable=True),
        sa.Column("entrypoint", sa.String(), nullable=False),
        sa.Column("params_schema", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("cost_class", sa.String(), nullable=False),
        sa.Column("sideeffect_class", sa.String(), nullable=False),
        sa.Column("when_to_use", sa.Text(), nullable=True),
        sa.Column("when_not_to_use", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.CheckConstraint("cost_class in ('instant','seconds','minutes','hours')", name="ck_block_version_cost_class"),
        sa.CheckConstraint(
            "sideeffect_class in ('read','compute','write_artifact','mutate_state','external')",
            name="ck_block_version_sideeffect_class",
        ),
        sa.CheckConstraint("status in ('active','deprecated','retired')", name="ck_block_version_status"),
        sa.UniqueConstraint("block_id", "version", name="uq_block_version_block_version"),
    )
    op.create_index("ix_block_version_block_id", "block_version", ["block_id"])

    op.create_table(
        "contract",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("json_schema", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.CheckConstraint("status in ('active','deprecated','retired')", name="ck_contract_status"),
        sa.UniqueConstraint("name", "version", name="uq_contract_name_version"),
    )

    op.create_table(
        "block_io",
        sa.Column("block_version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("block_version.id"), primary_key=True),
        sa.Column("contract_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contract.id"), primary_key=True),
        sa.Column("role", sa.String(), primary_key=True),
        sa.Column("port_name", sa.String(), primary_key=True),
        sa.CheckConstraint("role in ('consumes','produces')", name="ck_block_io_role"),
    )

    op.create_table(
        "gate_binding",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("block_version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("block_version.id"), nullable=False),
        sa.Column("check_suite", sa.String(), nullable=False),
        sa.Column("when", sa.String(), nullable=False),
        sa.Column("policy", sa.String(), nullable=False),
        sa.CheckConstraint('"when" in (\'pre\',\'post\')', name="ck_gate_binding_when"),
        sa.CheckConstraint("policy in ('block','warn')", name="ck_gate_binding_policy"),
    )
    op.create_index("ix_gate_binding_block_version_id", "gate_binding", ["block_version_id"])

    op.create_table(
        "metric",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("domain", sa.String(), nullable=False),
        sa.Column("definition_sql", sa.Text(), nullable=False),
        sa.Column("dimensions", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("grain", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.CheckConstraint("status in ('active','deprecated','retired')", name="ck_metric_status"),
        sa.UniqueConstraint("name", name="uq_metric_name"),
    )

    op.create_table(
        "block_grant",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("block_version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("block_version.id"), nullable=False),
        sa.Column("params_constraints", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint("tenant_id", "block_version_id", name="uq_block_grant_tenant_block_version"),
    )
    op.create_index("ix_block_grant_tenant_id", "block_grant", ["tenant_id"])

    op.create_table(
        "knowledge_source",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("uri", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("version_hash", sa.String(), nullable=True),
        sa.Column("freshness_policy", sa.String(), nullable=True),
    )
    op.create_index("ix_knowledge_source_tenant_id", "knowledge_source", ["tenant_id"])

    op.create_table(
        "ask",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("normalized", postgresql.JSONB(), nullable=True),
        sa.Column("normalized_hash", sa.String(), nullable=True),
        sa.Column("task_type", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_ask_tenant_id", "ask", ["tenant_id"])

    op.create_table(
        "plan",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("ask_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ask.id"), nullable=False),
        sa.Column("ir", postgresql.JSONB(), nullable=False),
        sa.Column("ir_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="proposed"),
        sa.Column("validation_errors", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status in ('proposed','valid','invalid','approved','running','succeeded','failed')",
            name="ck_plan_status",
        ),
    )
    op.create_index("ix_plan_tenant_id", "plan", ["tenant_id"])
    op.create_index("ix_plan_ask_id", "plan", ["ask_id"])

    op.create_table(
        "plan_template",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("normalized_hash", sa.String(), nullable=False),
        sa.Column("ir", postgresql.JSONB(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status in ('active','deprecated','retired')", name="ck_plan_template_status"),
    )
    op.create_index("ix_plan_template_tenant_id", "plan_template", ["tenant_id"])
    op.create_index("ix_plan_template_normalized_hash", "plan_template", ["normalized_hash"])

    op.create_table(
        "run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("plan.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("params", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(
            "status in ('pending','running','succeeded','failed','cancelled')", name="ck_run_status"
        ),
    )
    op.create_index("ix_run_tenant_id", "run", ["tenant_id"])
    op.create_index("ix_run_plan_id", "run", ["plan_id"])

    op.create_table(
        "node_execution",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("block_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("input_hashes", postgresql.JSONB(), nullable=True),
        sa.Column("output_hashes", postgresql.JSONB(), nullable=True),
        sa.Column("envelope", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status in ('pending','running','succeeded','failed','skipped')",
            name="ck_node_execution_status",
        ),
    )
    op.create_index("ix_node_execution_run_id", "node_execution", ["run_id"])
    op.create_index("ix_node_execution_tenant_id", "node_execution", ["tenant_id"])

    op.create_table(
        "gate_verdict",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("check_suite", sa.String(), nullable=False),
        sa.Column("verdict", sa.String(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("verdict in ('pass','warn','fail')", name="ck_gate_verdict_verdict"),
    )
    op.create_index("ix_gate_verdict_run_id", "gate_verdict", ["run_id"])
    op.create_index("ix_gate_verdict_tenant_id", "gate_verdict", ["tenant_id"])

    op.create_table(
        "llm_call",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("call_class", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("prompt_hash", sa.String(), nullable=False),
        sa.Column("response_hash", sa.String(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_llm_call_tenant_id", "llm_call", ["tenant_id"])

    op.create_table(
        "audit_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_audit_event_tenant_id", "audit_event", ["tenant_id"])

    # --- Row-level security -------------------------------------------------
    # Every table with a tenant_id column is isolated at the DB layer; the API
    # sets `SET LOCAL app.tenant_id` per request (core.db.session.tenant_session)
    # and RLS enforces the boundary regardless of application-code discipline.
    for table_name in _TENANT_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table_name}
            USING (tenant_id = current_setting('app.tenant_id')::uuid)
            WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid)
            """
        )

    op.execute(f"ALTER TABLE {_NULLABLE_TENANT_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_NULLABLE_TENANT_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {_NULLABLE_TENANT_TABLE}
        USING (tenant_id IS NULL OR tenant_id = current_setting('app.tenant_id')::uuid)
        WITH CHECK (tenant_id IS NULL OR tenant_id = current_setting('app.tenant_id')::uuid)
        """
    )

    # --- gc_app: the narrowest role the harness ever connects as ------------
    # Idempotent so this migration is safe to run against any database that
    # already has the role (roles are cluster-wide, not per-database).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'gc_app') THEN
                CREATE ROLE gc_app LOGIN PASSWORD 'gc_app';
            END IF;
        END
        $$;
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO gc_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO gc_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO gc_app")


def downgrade() -> None:
    op.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM gc_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM gc_app")

    op.drop_table("audit_event")
    op.drop_table("llm_call")
    op.drop_table("gate_verdict")
    op.drop_table("node_execution")
    op.drop_table("run")
    op.drop_table("plan_template")
    op.drop_table("plan")
    op.drop_table("ask")
    op.drop_table("knowledge_source")
    op.drop_table("block_grant")
    op.drop_table("metric")
    op.drop_table("gate_binding")
    op.drop_table("block_io")
    op.drop_table("contract")
    op.drop_table("block_version")
    op.drop_table("block")
    op.drop_table("app_user")
    op.drop_table("tenant")
