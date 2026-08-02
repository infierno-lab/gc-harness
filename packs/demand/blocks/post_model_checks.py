"""post_model_checks@1.0 — kind `validation`. Real checks over an
elasticity_surface@v1 handle: sign sanity (elasticities must be negative),
coverage (at least one SKU), no nulls. Bound as the mandatory post-gate on
elasticity_dml (spec §5.6: a model node without a downstream blocking
validation is invalid by construction)."""

from typing import Any

from core.execute.adapter import BlockContext, build_envelope
from core.execute.envelope import Envelope

CHECK_SUITE = "post_model_checks"


def run(params: dict[str, Any], inputs: dict[str, Envelope], ctx: BlockContext) -> dict[str, Envelope]:
    surface_env = inputs["surface"]
    rows = ctx.storage.get(surface_env.ref)

    n_skus = len(rows)
    verdicts: list[dict[str, Any]] = [
        {"check_name": "coverage", "verdict": "pass" if n_skus > 0 else "fail", "details": {"n_skus": n_skus}}
    ]

    positive_skus = [row["sku"] for row in rows if row.get("elasticity") is not None and row["elasticity"] >= 0]
    verdicts.append(
        {
            "check_name": "sign_sanity",
            "verdict": "fail" if positive_skus else "pass",
            "details": {"positive_skus": positive_skus},
        }
    )

    null_skus = [row["sku"] for row in rows if row.get("elasticity") is None]
    verdicts.append(
        {"check_name": "nulls", "verdict": "fail" if null_skus else "pass", "details": {"null_skus": null_skus}}
    )

    overall = "fail" if any(v["verdict"] == "fail" for v in verdicts) else "pass"
    payload = {"check_suite": CHECK_SUITE, "verdicts": verdicts, "overall": overall}
    ref = ctx.storage.put("verdicts", payload)
    summary = f"{CHECK_SUITE}: {overall} ({len(verdicts)} checks over {n_skus} SKUs)"

    envelope = build_envelope(
        block_version=ctx.block_version,
        params=params,
        inputs=inputs,
        summary=summary,
        cols=["check_name", "verdict"],
        sample=verdicts,
        ref=ref,
        rows=len(verdicts),
    )
    return {"verdicts": envelope}
