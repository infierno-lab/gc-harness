"""panel_builder@1.0 — kind `data`. Synthesizes a sku x week x platform panel
(spec §12: first node of the DML elasticity pipeline) and materializes it via
`ctx.storage`. Deterministic: same params always produce the same rows."""

from typing import Any

from core.execute.adapter import BlockContext, build_envelope
from core.execute.envelope import Envelope
from packs.demand.blocks._util import rng_for, seed_for, skus_for_brand, weeks_between
from packs.demand.blocks.elasticity_real import REAL_BRANDS

N_SKUS = 12


def run(params: dict[str, Any], inputs: dict[str, Envelope], ctx: BlockContext) -> dict[str, Envelope]:
    brand = str(params["brand"])
    platform = str(params["platform"])
    if brand.lower() in REAL_BRANDS:
        # Fail loud, not silently-wrong (see elasticity_real.REAL_BRANDS): a
        # stale/mis-bound template cache is the only legitimate way this
        # branch is reached — every fresh plan_ask call routes a real brand to
        # elasticity_real via the planner's few-shot instead.
        raise ValueError(
            f"brand {brand!r} has a real ds-models run — use elasticity_real, not the synthetic "
            "panel_builder/elasticity_dml demo pipeline"
        )
    window = params["window"]
    weeks = weeks_between(window["from"], window["to"])
    skus = skus_for_brand(brand, N_SKUS)

    rows: list[dict[str, Any]] = []
    for sku in skus:
        base_seed = seed_for(brand, platform, sku)
        base_price = 50.0 + (base_seed % 4_500) / 100.0
        base_units = 200 + (base_seed % 300)
        rng = rng_for(brand, platform, sku)
        for week in weeks:
            price = round(base_price * (1 + rng.uniform(-0.05, 0.05)), 2)
            units = max(0, int(base_units * (1 + rng.uniform(-0.15, 0.15))))
            rows.append({"sku": sku, "week": week, "platform": platform, "price": price, "units": units})

    ref = ctx.storage.put("panel", rows)
    n_nulls = sum(1 for row in rows if row["price"] is None or row["units"] is None)
    null_pct = round(100 * n_nulls / len(rows), 1) if rows else 0.0
    summary = f"{len(skus)} SKUs x {len(weeks)} weeks on {platform}; {len(rows)} rows; {null_pct}% null"

    envelope = build_envelope(
        block_version=ctx.block_version,
        params=params,
        inputs=inputs,
        summary=summary,
        cols=["sku", "week", "platform", "price", "units"],
        sample=rows[:5],
        ref=ref,
        rows=len(rows),
    )
    return {"panel": envelope}
