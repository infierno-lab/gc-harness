"""runs_history@1.0 — kind `query`. Tenant-scoped read over the control
plane's own run ledger (spec §4.2: `runs_history` example query block).
Opens its own RLS-scoped session — the block context carries no session, only
tenant_id."""

from typing import Any

from core.db.models import Run
from core.db.session import tenant_session
from core.execute.adapter import BlockContext, build_envelope
from core.execute.envelope import Envelope


def run(params: dict[str, Any], inputs: dict[str, Envelope], ctx: BlockContext) -> dict[str, Envelope]:
    limit = int(params.get("limit", 10))

    with tenant_session(ctx.tenant_id) as session:
        rows = session.query(Run).order_by(Run.started_at.desc().nulls_last()).limit(limit).all()
        history = [
            {
                "run_id": str(row.id),
                "status": row.status,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            }
            for row in rows
        ]

    ref = ctx.storage.put("history", history)
    summary = f"{len(history)} most recent runs for tenant {ctx.tenant_id}"

    envelope = build_envelope(
        block_version=ctx.block_version,
        params=params,
        inputs=inputs,
        summary=summary,
        cols=["run_id", "status", "started_at", "finished_at"],
        sample=history[:5],
        ref=ref,
        rows=len(history),
    )
    return {"history": envelope}
