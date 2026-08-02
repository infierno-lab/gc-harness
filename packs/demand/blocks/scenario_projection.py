"""scenario_projection@1.0 — kind `scenario`. Deterministic price-delta x
elasticity volume math over an elasticity_surface@v1 handle, shaped as a
promo_plan@v1 handle (one row per SKU at the requested week/platform).

Baseline price/units are demo-synthetic (hash-derived per SKU, same scheme as
panel_builder) since this block only consumes the elasticity surface, per
spec — there's no panel handle in its inputs.
"""

from typing import Any

from core.execute.adapter import BlockContext, build_envelope
from core.execute.envelope import Envelope
from packs.demand.blocks._util import seed_for


def run(params: dict[str, Any], inputs: dict[str, Envelope], ctx: BlockContext) -> dict[str, Envelope]:
    surface_env = inputs["surface"]
    surface_rows = ctx.storage.get(surface_env.ref)

    platform = str(params["platform"])
    week = str(params["week"])
    price_delta_pct = float(params["price_delta_pct"])

    plan_rows: list[dict[str, Any]] = []
    for row in surface_rows:
        sku = row["sku"]
        elasticity = row["elasticity"]
        base_seed = seed_for(sku, platform, "baseline")
        base_price = 50.0 + (base_seed % 4_500) / 100.0
        base_units = 200 + (base_seed % 300)

        recommended_price = round(base_price * (1 + price_delta_pct), 2)
        volume_multiplier = (1 + price_delta_pct) ** elasticity
        expected_lift = round(base_units * (volume_multiplier - 1), 1)

        plan_rows.append(
            {
                "sku": sku,
                "week": week,
                "platform": platform,
                "recommended_price": recommended_price,
                "expected_lift": expected_lift,
            }
        )

    ref = ctx.storage.put("plan", plan_rows)
    total_lift = round(sum(row["expected_lift"] for row in plan_rows), 1)
    summary = f"{len(plan_rows)} SKU projections at {price_delta_pct:+.0%} price on {platform}; total lift={total_lift}"

    envelope = build_envelope(
        block_version=ctx.block_version,
        params=params,
        inputs=inputs,
        summary=summary,
        cols=["sku", "week", "platform", "recommended_price", "expected_lift"],
        sample=plan_rows[:5],
        ref=ref,
        rows=len(plan_rows),
    )
    return {"plan": envelope}
