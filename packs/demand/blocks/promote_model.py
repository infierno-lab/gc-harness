"""promote_model@1.0 — kind `promotion`, sideeffect_class `mutate_state`.
Moves the champion/candidate registry alias for a (brand, platform) pair to
point at a new elasticity_surface@v1 handle. Demo stand-in for an MLflow
alias move: writes/updates a JSON alias record under run storage, keyed by
(tenant, brand, platform) so promotions are visible across runs and every
write carries `previous` for audit/rollback.

This is the harness's only `mutate_state` block — spec §4.2/§8 route it
through the two-person approval flow (core.api.approvals) before it ever
reaches this entrypoint; `execute_plan` itself still validates fail-closed.
"""

import json
from pathlib import Path
from typing import Any

from core.catalog.hashing import content_hash
from core.execute.adapter import BlockContext, build_envelope
from core.execute.envelope import Envelope

_REGISTRY_DIRNAME = "registry"


def _registry_path(ctx: BlockContext, brand: str, platform: str) -> Path:
    """Alias state lives one level above the per-run scratch dir (`ctx.run_dir`
    is `.gc_runs/<run_id>`) so it persists and is visible across runs for the
    same (tenant, brand, platform) — a real registry survives any one run.

    `brand`/`platform` are constrained by the catalog's params_schema (the
    contract is the whole truth per spec §4.3), but this is a filesystem
    write and gets its own defense-in-depth: the resolved path must land
    directly inside `registry_dir`, or this raises rather than writing
    anywhere else on disk.
    """
    registry_dir = (ctx.run_dir.parent / _REGISTRY_DIRNAME).resolve()
    registry_dir.mkdir(parents=True, exist_ok=True)
    candidate = (registry_dir / f"{ctx.tenant_id}__{brand}__{platform}.json").resolve()
    if candidate.parent != registry_dir:
        raise ValueError(
            f"refusing to write registry alias outside {registry_dir}: resolved to {candidate} "
            f"(brand={brand!r}, platform={platform!r})"
        )
    return candidate


def run(params: dict[str, Any], inputs: dict[str, Envelope], ctx: BlockContext) -> dict[str, Envelope]:
    brand = str(params["brand"])
    platform = str(params["platform"])
    alias = str(params.get("alias", "champion"))

    surface_env = inputs["surface"]
    surface_rows = ctx.storage.get(surface_env.ref)

    path = _registry_path(ctx, brand, platform)
    previous = json.loads(path.read_text()) if path.exists() else None

    record = {
        "alias": alias,
        "brand": brand,
        "platform": platform,
        "surface_ref": surface_env.ref,
        "surface_hash": content_hash(surface_rows),
        "promoted_by": ctx.actor or "unknown",
        "previous": previous,
    }
    path.write_text(json.dumps(record, indent=2, sort_keys=True))

    ref = ctx.storage.put("promotion", record)
    summary = f"promoted {brand}/{platform} surface {surface_env.ref} to alias {alias!r} (by {record['promoted_by']})"

    envelope = build_envelope(
        block_version=ctx.block_version,
        params=params,
        inputs=inputs,
        summary=summary,
        cols=["alias", "surface_ref", "surface_hash", "promoted_by"],
        sample=[record],
        ref=ref,
        rows=1,
    )
    return {"report": envelope}
