"""loss_making_skus@1.0 — kind `metric`. Deterministic demo stand-in for a
governed margin mart: hash-derived per-SKU price/cost/units, flags SKUs whose
margin falls below `margin_threshold`."""

from typing import Any

from core.execute.adapter import BlockContext, build_envelope
from core.execute.envelope import Envelope
from packs.demand.blocks._util import seed_for, skus_for_brand

N_SKUS = 12


def run(params: dict[str, Any], inputs: dict[str, Envelope], ctx: BlockContext) -> dict[str, Envelope]:
    brand = str(params["brand"])
    platform = str(params["platform"])
    margin_threshold = float(params.get("margin_threshold", 0.0))

    rows: list[dict[str, Any]] = []
    for sku in skus_for_brand(brand, N_SKUS):
        base_seed = seed_for(brand, platform, sku, "margin")
        price = round(50.0 + (base_seed % 4_500) / 100.0, 2)
        cost = round(price * (0.6 + (base_seed % 50) / 100.0), 2)  # cost = 60-109% of price
        units = 200 + (base_seed % 300)
        margin = round(price - cost, 2)
        rows.append(
            {
                "sku": sku,
                "price": price,
                "cost": cost,
                "units": units,
                "margin": margin,
                "loss_making": margin < margin_threshold,
            }
        )

    loss_making = [row for row in rows if row["loss_making"]]
    ref = ctx.storage.put("loss_making_skus", rows)
    summary = f"{len(loss_making)}/{len(rows)} SKUs loss-making (margin < {margin_threshold}) on {platform}"

    envelope = build_envelope(
        block_version=ctx.block_version,
        params=params,
        inputs=inputs,
        summary=summary,
        cols=["sku", "price", "cost", "units", "margin", "loss_making"],
        sample=loss_making[:5],
        ref=ref,
        rows=len(rows),
    )
    return {"skus": envelope}
