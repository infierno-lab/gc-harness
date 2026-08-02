"""Idempotent seed framework for the catalog (spec §4.4): re-running with
identical content is a no-op; changed content updates the existing row in
place. Seeds are the only sanctioned way catalog rows are written — no ad-hoc
inserts elsewhere.
"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.catalog.contracts import contract_json_schema, get_contract
from core.db.models import Block, BlockGrant, BlockIO, BlockVersion, Contract, GateBinding

# NOTE: block/block_version carry no updated_at column in the stage-1 schema
# (BlockVersion/Block use only UUIDPKMixin), so there is nothing to bump on
# update — content is compared and overwritten in place instead.


class SeedReport(BaseModel):
    blocks_created: list[str] = Field(default_factory=list)
    blocks_updated: list[str] = Field(default_factory=list)
    blocks_unchanged: list[str] = Field(default_factory=list)
    contracts_created: list[str] = Field(default_factory=list)
    contracts_updated: list[str] = Field(default_factory=list)
    contracts_unchanged: list[str] = Field(default_factory=list)
    metrics_created: list[str] = Field(default_factory=list)
    metrics_updated: list[str] = Field(default_factory=list)
    metrics_unchanged: list[str] = Field(default_factory=list)


def _split_name_version(name_version: str) -> tuple[str, str]:
    name, _, version = name_version.rpartition("@")
    if not name or not version:
        raise ValueError(f"expected 'name@version', got {name_version!r}")
    return name, version


def _upsert_contract(session: Session, name_version: str, report: SeedReport) -> Contract:
    name, version = _split_name_version(name_version)
    json_schema = contract_json_schema(name_version)
    get_contract(name_version)  # raises KeyError if not registered

    existing = session.execute(
        select(Contract).where(Contract.name == name, Contract.version == version)
    ).scalar_one_or_none()

    if existing is None:
        contract = Contract(name=name, version=version, json_schema=json_schema, status="active")
        session.add(contract)
        session.flush()
        report.contracts_created.append(name_version)
        return contract

    if existing.json_schema != json_schema:
        existing.json_schema = json_schema
        report.contracts_updated.append(name_version)
    else:
        report.contracts_unchanged.append(name_version)
    return existing


def _sync_block_io(
    session: Session,
    block_version: BlockVersion,
    consumes: list[dict[str, str]],
    produces: list[dict[str, str]],
    contracts_by_name_version: dict[str, Contract],
) -> bool:
    """Sync block_io rows to exactly the declared consumes/produces set. Returns
    True if anything changed."""
    desired: set[tuple[UUID, str, str]] = set()
    for role, entries in (("consumes", consumes), ("produces", produces)):
        for entry in entries:
            contract = contracts_by_name_version[entry["contract"]]
            desired.add((contract.id, role, entry["port"]))

    existing_rows = list(
        session.execute(
            select(BlockIO).where(BlockIO.block_version_id == block_version.id)
        ).scalars().all()
    )
    existing = {(row.contract_id, row.role, row.port_name): row for row in existing_rows}

    changed = False
    for key, row in existing.items():
        if key not in desired:
            session.delete(row)
            changed = True
    for contract_id, role, port_name in desired:
        if (contract_id, role, port_name) not in existing:
            session.add(
                BlockIO(
                    block_version_id=block_version.id,
                    contract_id=contract_id,
                    role=role,
                    port_name=port_name,
                )
            )
            changed = True
    return changed


def _sync_gates(session: Session, block_version: BlockVersion, gates: list[dict[str, str]]) -> bool:
    desired: set[tuple[str, str, str]] = {
        (gate["check_suite"], gate["when"], gate["policy"]) for gate in gates
    }
    existing_rows = list(
        session.execute(
            select(GateBinding).where(GateBinding.block_version_id == block_version.id)
        ).scalars().all()
    )
    existing = {(row.check_suite, row.when, row.policy): row for row in existing_rows}

    changed = False
    for key, row in existing.items():
        if key not in desired:
            session.delete(row)
            changed = True
    for check_suite, when, policy in desired:
        if (check_suite, when, policy) not in existing:
            session.add(
                GateBinding(
                    block_version_id=block_version.id,
                    check_suite=check_suite,
                    when=when,
                    policy=policy,
                )
            )
            changed = True
    return changed


def _upsert_block(session: Session, spec: dict[str, Any], report: SeedReport) -> None:
    name_version = f"{spec['name']}@{spec['version']}"

    block = session.execute(select(Block).where(Block.name == spec["name"])).scalar_one_or_none()
    block_changed = False
    if block is None:
        block = Block(
            name=spec["name"],
            kind=spec["kind"],
            owner=spec["owner"],
            domain=spec["domain"],
            tags=list(spec["tags"]),
        )
        session.add(block)
        session.flush()
        block_changed = True
    else:
        for field, value in (
            ("kind", spec["kind"]),
            ("owner", spec["owner"]),
            ("domain", spec["domain"]),
        ):
            if getattr(block, field) != value:
                setattr(block, field, value)
                block_changed = True
        if list(block.tags) != list(spec["tags"]):
            block.tags = list(spec["tags"])
            block_changed = True

    contracts_by_name_version: dict[str, Contract] = {}
    for entry in [*spec["consumes"], *spec["produces"]]:
        contract_name_version = entry["contract"]
        if contract_name_version not in contracts_by_name_version:
            contracts_by_name_version[contract_name_version] = _upsert_contract(
                session, contract_name_version, report
            )

    block_version = session.execute(
        select(BlockVersion).where(
            BlockVersion.block_id == block.id, BlockVersion.version == spec["version"]
        )
    ).scalar_one_or_none()

    version_fields = {
        "git_sha": spec["git_sha"],
        "image_ref": spec.get("image_ref"),
        "entrypoint": spec["entrypoint"],
        "params_schema": spec["params_schema"],
        "cost_class": spec["cost_class"],
        "sideeffect_class": spec["sideeffect_class"],
        "when_to_use": spec.get("when_to_use"),
        "when_not_to_use": spec.get("when_not_to_use"),
        "status": spec.get("status", "active"),
    }

    version_created = False
    version_changed = False
    if block_version is None:
        block_version = BlockVersion(block_id=block.id, version=spec["version"], **version_fields)
        session.add(block_version)
        session.flush()
        version_created = True
    else:
        for field, value in version_fields.items():
            if getattr(block_version, field) != value:
                setattr(block_version, field, value)
                version_changed = True

    io_changed = _sync_block_io(
        session, block_version, spec["consumes"], spec["produces"], contracts_by_name_version
    )
    gates_changed = _sync_gates(session, block_version, spec.get("gates", []))

    if version_created:
        report.blocks_created.append(name_version)
    elif block_changed or version_changed or io_changed or gates_changed:
        report.blocks_updated.append(name_version)
    else:
        report.blocks_unchanged.append(name_version)


def _upsert_metric(session: Session, spec: dict[str, Any], report: SeedReport) -> None:
    from core.db.models import Metric

    metric = session.execute(select(Metric).where(Metric.name == spec["name"])).scalar_one_or_none()
    fields = {
        "domain": spec["domain"],
        "definition_sql": spec["definition_sql"],
        "dimensions": list(spec.get("dimensions", [])),
        "grain": spec["grain"],
        "owner": spec["owner"],
        "status": spec.get("status", "active"),
    }

    if metric is None:
        session.add(Metric(name=spec["name"], **fields))
        report.metrics_created.append(spec["name"])
        return

    changed = False
    for field, value in fields.items():
        if getattr(metric, field) != value:
            setattr(metric, field, value)
            changed = True
    if changed:
        report.metrics_updated.append(spec["name"])
    else:
        report.metrics_unchanged.append(spec["name"])


def apply_seed(
    session: Session, blocks: list[dict[str, Any]], metrics: list[dict[str, Any]] = ()
) -> SeedReport:
    report = SeedReport()
    for block_spec in blocks:
        _upsert_block(session, block_spec, report)
    for metric_spec in metrics:
        _upsert_metric(session, metric_spec, report)
    return report


def grant_all_blocks(session: Session, tenant_id: str | UUID) -> None:
    """Dev/test helper: grant a tenant every active block version, unconstrained."""
    tenant_uuid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    active_versions = session.execute(
        select(BlockVersion).where(BlockVersion.status == "active")
    ).scalars().all()

    existing = {
        row.block_version_id
        for row in session.execute(
            select(BlockGrant).where(BlockGrant.tenant_id == tenant_uuid)
        ).scalars().all()
    }
    for block_version in active_versions:
        if block_version.id not in existing:
            session.add(
                BlockGrant(tenant_id=tenant_uuid, block_version_id=block_version.id, params_constraints=None)
            )
