"""elasticity_dml@1.0 — kind `model`. Deterministic pseudo-elasticities per SKU
from a dml_panel@v1 handle, in the -2.5..-0.5 range (demo stand-in for the real
double-ML estimator).

`force_positive_elasticities` is an explicit demo/test knob (not a hidden
default) that flips the sign so the post_model_checks gate can be exercised
against a genuinely broken model output.
"""

from typing import Any

from core.execute.adapter import BlockContext, build_envelope
from core.execute.envelope import Envelope
from packs.demand.blocks._util import rng_for, seed_for


def run(params: dict[str, Any], inputs: dict[str, Envelope], ctx: BlockContext) -> dict[str, Envelope]:
    panel_env = inputs["panel"]
    panel_rows = ctx.storage.get(panel_env.ref)
    force_positive = bool(params.get("force_positive_elasticities", False))

    by_sku: dict[str, list[dict[str, Any]]] = {}
    for row in panel_rows:
        by_sku.setdefault(row["sku"], []).append(row)

    surface_rows: list[dict[str, Any]] = []
    for sku in sorted(by_sku):
        base_seed = seed_for(sku, "elasticity_dml")
        magnitude = 0.5 + (base_seed % 200) / 100.0  # 0.5..2.5
        elasticity = magnitude if force_positive else -magnitude
        spread = round(0.1 + rng_for(sku, "elasticity_ci").uniform(0.0, 0.1), 4)
        surface_rows.append(
            {
                "sku": sku,
                "elasticity": round(elasticity, 4),
                "ci_lower": round(elasticity - spread, 4),
                "ci_upper": round(elasticity + spread, 4),
                "method": "dml_demo",
            }
        )

    ref = ctx.storage.put("surface", surface_rows)
    mean_elasticity = (
        round(sum(row["elasticity"] for row in surface_rows) / len(surface_rows), 3) if surface_rows else 0.0
    )
    summary = f"{len(surface_rows)} SKU elasticities (dml_demo); mean={mean_elasticity}"

    envelope = build_envelope(
        block_version=ctx.block_version,
        params=params,
        inputs=inputs,
        summary=summary,
        cols=["sku", "elasticity", "ci_lower", "ci_upper", "method"],
        sample=surface_rows[:5],
        ref=ref,
        rows=len(surface_rows),
    )
    return {"surface": envelope}
