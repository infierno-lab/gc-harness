"""Pure read helpers over the catalog (spec §4.4) — no caching, no writes.

Used by the plan validator and the capability digest renderer.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.db.models import Block, BlockGrant, BlockIO, BlockVersion, Contract, GateBinding


def resolve_block(session: Session, name_version: str) -> BlockVersion | None:
    """Resolve "name@version" to its BlockVersion row, or None if it doesn't exist.

    For convenience, the owning Block row is attached as a transient `.block`
    attribute on the returned instance (not a mapped relationship) so callers
    can read `bv.block.kind`, `.owner`, `.domain`, `.tags` without a second
    round trip.
    """
    name, _, version = name_version.rpartition("@")
    if not name or not version:
        return None

    row = session.execute(
        select(BlockVersion, Block)
        .join(Block, BlockVersion.block_id == Block.id)
        .where(Block.name == name, BlockVersion.version == version)
    ).first()
    if row is None:
        return None

    block_version, block = row
    block_version.block = block  # type: ignore[attr-defined]
    return block_version


def block_io_for(session: Session, block_version: BlockVersion) -> dict[str, dict[str, str]]:
    """{"consumes": {port: "contract_name@version"}, "produces": {port: "contract_name@version"}}"""
    rows = session.execute(
        select(BlockIO, Contract)
        .join(Contract, BlockIO.contract_id == Contract.id)
        .where(BlockIO.block_version_id == block_version.id)
    ).all()

    result: dict[str, dict[str, str]] = {"consumes": {}, "produces": {}}
    for block_io, contract in rows:
        result[block_io.role][block_io.port_name] = f"{contract.name}@{contract.version}"
    return result


def gates_for(session: Session, block_version: BlockVersion) -> list[GateBinding]:
    """Catalog-registered gate bindings for a block version, ordered deterministically."""
    return list(
        session.execute(
            select(GateBinding)
            .where(GateBinding.block_version_id == block_version.id)
            .order_by(GateBinding.check_suite, GateBinding.when)
        )
        .scalars()
        .all()
    )


def grants_for(session: Session, tenant_id: str | UUID) -> dict[UUID, dict[str, Any] | None]:
    """{block_version_id: params_constraints} for every block granted to this tenant."""
    tenant_uuid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    rows = session.execute(
        select(BlockGrant).where(BlockGrant.tenant_id == tenant_uuid)
    ).scalars().all()
    return {row.block_version_id: row.params_constraints for row in rows}
