"""Latency-probe tests (spec §5.9, §12 Phase-3 exit metric): runs the probe
itself against the test DB, with a seeded catalog, a granted tenant, and a
pre-seeded plan template — small `n` for CI speed. Only a smoke ceiling is
asserted, never the SLO itself (machines vary too much for a hard SLO gate in
CI); the real dev-DB numbers are reported by `scripts/latency_probe.py`.
"""

import json
import uuid

import pytest

import core.classify.classifier as classifier_module
import core.plan.planner as planner_module
from core.catalog.seed import apply_seed, grant_all_blocks
from core.db.session import admin_session, tenant_session
from core.gateway.client import Gateway
from core.gateway.transport import TransportResult
from evals.latency_probe import DEFAULT_ASK_TEXT, ProbeReport, probe_repeat_ask, seed_plan_template
from evals.test_gateway import FakeTransport
from packs.demand.catalog_seed import BLOCKS, METRICS

_SMOKE_CEILING_MS = 10_000


@pytest.fixture(scope="module", autouse=True)
def _seed_demand_catalog(migrated_test_db: str) -> None:
    with admin_session() as session:
        apply_seed(session, BLOCKS, METRICS)


def _grant_all(tenant_id: uuid.UUID) -> None:
    with admin_session() as session:
        grant_all_blocks(session, tenant_id)


def test_probe_repeat_ask_reports_shape_and_sane_decomposition(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    _grant_all(tenant_id)

    with tenant_session(tenant_id) as session:
        seed_plan_template(session, tenant_id, DEFAULT_ASK_TEXT)
        report = probe_repeat_ask(session, tenant_id, DEFAULT_ASK_TEXT, n=5)

    assert isinstance(report, ProbeReport)
    assert len(report.samples) == 5
    assert report.p50_ms > 0
    assert report.p95_ms >= report.p50_ms
    assert report.slo_ms == 1500
    assert isinstance(report.within_slo, bool)

    decomposition = report.decomposition
    assert decomposition.classify_p50 >= 0
    assert decomposition.plan_p50 >= 0
    assert decomposition.execute_p50 >= 0
    # the decomposed stages must roughly compose the total -- a generous
    # ceiling (p50-of-parts isn't p50-of-sums in general) just guarding
    # against a decomposition wildly disconnected from the observed total.
    stage_sum = decomposition.classify_p50 + decomposition.plan_p50 + decomposition.execute_p50
    assert stage_sum <= report.p95_ms + 50

    # smoke ceiling only -- do NOT hard-assert the SLO passes in CI, machines
    # vary; scripts/latency_probe.py against the dev DB reports the real
    # exit-metric numbers.
    assert report.p50_ms < _SMOKE_CEILING_MS

    print(
        f"\nlatency probe (n=5): p50={report.p50_ms:.1f}ms p95={report.p95_ms:.1f}ms "
        f"classify_p50={decomposition.classify_p50:.1f}ms plan_p50={decomposition.plan_p50:.1f}ms "
        f"execute_p50={decomposition.execute_p50:.1f}ms"
    )


def test_probe_repeat_ask_rejects_ask_that_falls_through_to_llm_classifier(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grant_all(tenant_id)
    llm_response = json.dumps(
        {
            "task_type": "analytic_query",
            "domain": "demand",
            "output_wanted": "answer",
            "constraints": [],
            "ambiguities": [],
            "confidence": 0.9,
            "entities": {
                "brand": None,
                "platform": None,
                "window_text": None,
                "price_delta_pct": None,
                "budget": None,
                "metric": None,
            },
        }
    )
    transport = FakeTransport(
        [TransportResult(text=llm_response, input_tokens=10, output_tokens=10, cache_read_tokens=0, latency_ms=5)]
    )
    monkeypatch.setattr(classifier_module, "get_gateway", lambda: Gateway(transport))

    with tenant_session(tenant_id) as session:
        with pytest.raises(AssertionError):
            # not a stage-0 rule shape -- falls through to the (faked) LLM.
            probe_repeat_ask(session, tenant_id, "What is the elasticity of SKU 123?", n=1)


def test_probe_repeat_ask_rejects_ask_whose_plan_misses_the_template_cache(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grant_all(tenant_id)
    valid_plan = {
        "plan_ir_version": 1,
        "intent_summary": "loss making skus for demo on blinkit",
        "nodes": [{"id": "n1", "block": "loss_making_skus@1.0", "params": {"brand": "demo", "platform": "blinkit"}}],
        "outputs": {},
        "assumptions": [],
        "estimated_cost": {"class": "instant"},
    }
    transport = FakeTransport(
        [TransportResult(text=json.dumps(valid_plan), input_tokens=10, output_tokens=10, cache_read_tokens=0, latency_ms=5)]
    )
    monkeypatch.setattr(planner_module, "get_gateway", lambda: Gateway(transport))

    with tenant_session(tenant_id) as session:
        # no PlanTemplate seeded -- plan_ask must miss the cache and go to
        # the (faked) LLM, which the probe must reject.
        with pytest.raises(AssertionError):
            probe_repeat_ask(session, tenant_id, DEFAULT_ASK_TEXT, n=1)
