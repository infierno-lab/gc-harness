"""Capability digest (spec §5.2 Tier 1 / §3.3): one line per granted active
block, byte-stable across renders (no timestamps, deterministic ordering).
"""

from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.catalog.hashing import content_hash
from core.catalog.store import block_io_for, grants_for
from core.db.models import Block, BlockVersion, Metric


class DigestResult(BaseModel):
    text: str
    digest_hash: str


def _format_ports(ports: dict[str, str]) -> str:
    if not ports:
        return "none"
    return ", ".join(f"{port}={contract}" for port, contract in sorted(ports.items()))


def _block_line(block: Block, block_version: BlockVersion, io: dict[str, dict[str, str]]) -> str:
    when_to_use = block_version.when_to_use or ""
    consumes = _format_ports(io["consumes"])
    produces = _format_ports(io["produces"])
    return (
        f"{block.name}@{block_version.version} "
        f"[{block.kind}/{block_version.cost_class}/{block_version.sideeffect_class}] "
        f"— {when_to_use} (consumes: {consumes}; produces: {produces})"
    )


def _metric_line(metric: Metric) -> str:
    dims = ", ".join(sorted(metric.dimensions))
    return f"metric: {metric.name} [{metric.domain}] grain={metric.grain} dims=[{dims}]"


def render_digest(session: Session, tenant_id: str | UUID) -> DigestResult:
    grants = grants_for(session, tenant_id)

    rows: list[tuple[Block, BlockVersion]] = []
    if grants:
        rows = list(
            session.execute(
                select(Block, BlockVersion)
                .join(BlockVersion, BlockVersion.block_id == Block.id)
                .where(BlockVersion.id.in_(grants.keys()), BlockVersion.status == "active")
            ).all()
        )
    rows.sort(key=lambda pair: (pair[0].name, pair[1].version))

    lines = [_block_line(block, block_version, block_io_for(session, block_version)) for block, block_version in rows]

    metrics = list(
        session.execute(select(Metric).where(Metric.status == "active").order_by(Metric.name)).scalars().all()
    )
    lines.extend(_metric_line(metric) for metric in metrics)

    text = "\n".join(lines)
    return DigestResult(text=text, digest_hash=content_hash(text))
