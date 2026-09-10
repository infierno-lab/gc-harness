"""Regression test for `elasticity_real` (packs/demand/blocks/elasticity_real.py)
against the actual ds-models workbooks vendored under packs/demand/data/real/.

No DB, no LLM, no BlockContext — the golden-ask suite exercises classify/plan
composition but never executes a real block, so a workbook-layout regression
here (e.g. the header-row-index bug this test was written to catch) would
otherwise pass the whole non-live suite while failing every live ask for a
real brand.
"""

from packs.demand.blocks.elasticity_real import REAL_BRANDS, _load_own_brand_rows


def test_real_brands_registered() -> None:
    assert REAL_BRANDS == {"gozero", "haleon"}


def test_gozero_rows_are_real_sku_elasticities() -> None:
    rows = _load_own_brand_rows("gozero")
    assert len(rows) == 9
    assert all(row["sku"].isdigit() for row in rows)
    assert all(isinstance(row["elasticity"], float) for row in rows)
    # Sign sanity (post_model_checks' own check, verified here independent of
    # any gate): a normal good's price elasticity of demand is negative.
    assert all(row["elasticity"] < 0 for row in rows)
    assert all(row["ci_lower"] <= row["elasticity"] <= row["ci_upper"] for row in rows)
    assert {row["method"] for row in rows} == {"ds_models_pooled_dml_bayes_causal_forest_psm_did_calibrated"}


def test_haleon_rows_are_real_sku_elasticities() -> None:
    rows = _load_own_brand_rows("haleon")
    assert len(rows) == 25
    assert all(row["elasticity"] < 0 for row in rows)
    skus = {row["sku"] for row in rows}
    assert len(skus) == len(rows)  # no duplicate/mis-filtered rows


def test_unknown_brand_raises() -> None:
    try:
        _load_own_brand_rows("nivea")
    except ValueError as exc:
        assert "nivea" in str(exc)
    else:
        raise AssertionError("expected ValueError for an unregistered brand")
