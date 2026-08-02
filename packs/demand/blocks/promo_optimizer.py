"""promo_optimizer@1.0 — kind `optimizer`. Greedy discount allocation over an
elasticity_surface@v1 handle under a budget param, shaped as a promo_plan@v1
handle. SKUs with the steepest (most negative) elasticity get funded first —
they return the most volume lift per discount dollar spent."""

from typing import Any

from core.execute.adapter import BlockContext, build_envelope
from core.execute.envelope import Envelope
from packs.demand.blocks._util import seed_for


def run(params: dict[str, Any], inputs: dict[str, Envelope], ctx: BlockContext) -> dict[str, Envelope]:
    surface_env = inputs["surface"]
    surface_rows = ctx.storage.get(surface_env.ref)

    platform = str(params["platform"])
    week = str(params["week"])
    budget = float(params["budget"])
    max_discount_pct = float(params.get("max_discount_pct", 0.3))

    candidates = []
    for row in surface_rows:
        sku = row["sku"]
        elasticity = row["elasticity"]
        base_seed = seed_for(sku, platform, "baseline")
        base_price = 50.0 + (base_seed % 4_500) / 100.0
        base_units = 200 + (base_seed % 300)
        candidates.append({"sku": sku, "elasticity": elasticity, "base_price": base_price, "base_units": base_units})

    # Steepest elasticity (most negative) first — most volume response per discount dollar.
    candidates.sort(key=lambda c: c["elasticity"])

    remaining_budget = budget
    plan_rows: list[dict[str, Any]] = []
    for cand in candidates:
        cost_at_max = cand["base_price"] * cand["base_units"] * max_discount_pct
        discount_pct = max_discount_pct if cost_at_max <= remaining_budget else max(remaining_budget, 0.0) / (
            cand["base_price"] * cand["base_units"]
        )
        discount_pct = max(0.0, min(discount_pct, max_discount_pct))
        cost = cand["base_price"] * cand["base_units"] * discount_pct
        remaining_budget = max(0.0, remaining_budget - cost)

        recommended_price = round(cand["base_price"] * (1 - discount_pct), 2)
        expected_lift = round(-cand["elasticity"] * discount_pct * cand["base_units"], 1) if discount_pct > 0 else 0.0

        plan_rows.append(
            {
                "sku": cand["sku"],
                "week": week,
                "platform": platform,
                "recommended_price": recommended_price,
                "expected_lift": expected_lift,
            }
        )

    ref = ctx.storage.put("plan", plan_rows)
    funded = sum(1 for row in plan_rows if row["expected_lift"] > 0)
    total_lift = round(sum(row["expected_lift"] for row in plan_rows), 1)
    summary = f"{funded}/{len(plan_rows)} SKUs funded within budget={budget}; total lift={total_lift}"

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
