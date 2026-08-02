"""Planner tests (spec §5.4-5.5): L2 template-cache hit (zero transport
calls), and an LLM path that needs exactly one repair round. No network in
the default suite — FakeTransport stands in for the gateway.
"""

import json
import uuid

import pytest
from sqlalchemy import select

import core.plan.planner as planner_module
from core.catalog.seed import apply_seed, grant_all_blocks
from core.classify.classifier import ClassifyResult, normalized_hash
from core.classify.schema import Entities, NormalizedAsk, WindowSpec
from core.db.models import PlanTemplate
from core.db.session import admin_session, tenant_session
from core.gateway.client import Gateway
from core.gateway.transport import TransportResult
from core.plan.planner import plan_ask
from evals.test_gateway import FakeTransport
from packs.demand.catalog_seed import BLOCKS, METRICS


@pytest.fixture(scope="session", autouse=True)
def _seed_demand_catalog(migrated_test_db: str) -> None:
    with admin_session() as session:
        apply_seed(session, BLOCKS, METRICS)


def _grant_all(tid: uuid.UUID) -> None:
    with admin_session() as session:
        grant_all_blocks(session, tid)


def _classify_result(entities: Entities) -> ClassifyResult:
    normalized = NormalizedAsk(
        task_type="analytic_query", domain="demand", entities=entities, output_wanted="answer", confidence=0.9
    )
    return ClassifyResult(normalized=normalized, stage="rules", normalized_hash=normalized_hash(normalized), latency_ms=1)


def _compound_classify_result(entities: Entities, *, constraints: list[str]) -> ClassifyResult:
    normalized = NormalizedAsk(
        task_type="pipeline",
        domain="demand",
        entities=entities,
        output_wanted="report",
        constraints=constraints,
        confidence=0.9,
    )
    return ClassifyResult(normalized=normalized, stage="llm", normalized_hash=normalized_hash(normalized), latency_ms=1)


def test_plan_ask_template_cache_hit_binds_params_and_skips_llm(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grant_all(tenant_id)
    classify_result = _classify_result(Entities(brand="nivea", platform="zepto", metric="loss_making_skus"))

    template_ir = {
        "plan_ir_version": 1,
        "intent_summary": "loss making skus lookup",
        "nodes": [
            {
                "id": "n1",
                "block": "loss_making_skus@1.0",
                "params": {"brand": "$slot:brand", "platform": "$slot:platform"},
            }
        ],
        "outputs": {},
        "assumptions": [],
        "estimated_cost": {"class": "instant"},
    }
    with admin_session() as session:
        session.add(
            PlanTemplate(
                tenant_id=tenant_id,
                normalized_hash=classify_result.normalized_hash,
                ir=template_ir,
                hit_count=0,
                status="active",
            )
        )

    def _boom() -> Gateway:
        raise AssertionError("template cache hit must never call the gateway")

    monkeypatch.setattr(planner_module, "get_gateway", _boom)

    with tenant_session(tenant_id) as session:
        outcome = plan_ask(session, tenant_id, classify_result, "how many loss making skus for nivea on zepto")

    assert outcome.source == "template_cache"
    assert outcome.attempts == 0
    assert outcome.plan is not None
    assert outcome.plan.nodes[0].params == {"brand": "nivea", "platform": "zepto"}
    assert outcome.validation is not None
    assert outcome.validation.valid is True

    with tenant_session(tenant_id) as session:
        template = session.execute(
            select(PlanTemplate).where(
                PlanTemplate.tenant_id == tenant_id, PlanTemplate.normalized_hash == classify_result.normalized_hash
            )
        ).scalar_one()
        assert template.hit_count == 1


def test_plan_ask_llm_miss_then_repair_round_then_stores_template(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grant_all(tenant_id)
    classify_result = _classify_result(Entities(brand="demo", platform="blinkit", metric="loss_making_skus"))

    invalid_plan = {
        "plan_ir_version": 1,
        "intent_summary": "loss making skus for demo",
        "nodes": [{"id": "n1", "block": "loss_making_skus@1.0", "params": {"brand": "demo"}}],  # missing platform
        "outputs": {},
        "assumptions": [],
        "estimated_cost": {"class": "instant"},
    }
    valid_plan = {
        "plan_ir_version": 1,
        "intent_summary": "loss making skus for demo",
        "nodes": [{"id": "n1", "block": "loss_making_skus@1.0", "params": {"brand": "demo", "platform": "blinkit"}}],
        "outputs": {},
        "assumptions": [],
        "estimated_cost": {"class": "instant"},
    }
    transport = FakeTransport(
        [
            TransportResult(text=json.dumps(invalid_plan), input_tokens=100, output_tokens=80, cache_read_tokens=0, latency_ms=2000),
            TransportResult(text=json.dumps(valid_plan), input_tokens=100, output_tokens=80, cache_read_tokens=0, latency_ms=2000),
        ]
    )
    monkeypatch.setattr(planner_module, "get_gateway", lambda: Gateway(transport))

    with tenant_session(tenant_id) as session:
        outcome = plan_ask(session, tenant_id, classify_result, "how many loss making skus for demo on blinkit")

    assert outcome.source == "llm"
    assert outcome.attempts == 2
    assert outcome.validation is not None
    assert outcome.validation.valid is True
    assert outcome.plan is not None
    assert len(transport.calls) == 2
    # the repair round carries the FIRST attempt's verbatim validator errors.
    assert "params_invalid" in transport.calls[1]

    with tenant_session(tenant_id) as session:
        template = session.execute(
            select(PlanTemplate).where(
                PlanTemplate.tenant_id == tenant_id, PlanTemplate.normalized_hash == classify_result.normalized_hash
            )
        ).scalar_one()
        assert template.ir["nodes"][0]["params"] == {"brand": "$slot:brand", "platform": "$slot:platform"}


def test_plan_ask_llm_still_invalid_after_repair_returns_failure_without_caching(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grant_all(tenant_id)
    classify_result = _classify_result(Entities(brand="demo", metric="loss_making_skus"))

    still_invalid_plan = {
        "plan_ir_version": 1,
        "intent_summary": "loss making skus for demo",
        "nodes": [{"id": "n1", "block": "loss_making_skus@1.0", "params": {"brand": "demo"}}],
        "outputs": {},
        "assumptions": [],
        "estimated_cost": {"class": "instant"},
    }
    transport = FakeTransport(
        [
            TransportResult(text=json.dumps(still_invalid_plan), input_tokens=10, output_tokens=10, cache_read_tokens=0, latency_ms=100),
            TransportResult(text=json.dumps(still_invalid_plan), input_tokens=10, output_tokens=10, cache_read_tokens=0, latency_ms=100),
        ]
    )
    monkeypatch.setattr(planner_module, "get_gateway", lambda: Gateway(transport))

    with tenant_session(tenant_id) as session:
        outcome = plan_ask(session, tenant_id, classify_result, "how many loss making skus for demo")

    assert outcome.source == "llm"
    assert outcome.attempts == 2
    assert outcome.validation is not None
    assert outcome.validation.valid is False

    with tenant_session(tenant_id) as session:
        template = session.execute(
            select(PlanTemplate).where(
                PlanTemplate.tenant_id == tenant_id, PlanTemplate.normalized_hash == classify_result.normalized_hash
            )
        ).scalar_one_or_none()
        assert template is None


# --- intent-coverage guard (third layer of the compound-ask bug) -------------
#
# LIVE BUG (verified via the API): classification and hashing were fixed, but
# the planner itself still silently dropped a compound ask's promotion intent
# via "repair by amputation" -- the first LLM attempt's promote_model node
# tripped `sideeffect_policy` (mutate_state nodes always do without an
# approver role, which plan_ask never has -- that's the two-person approval
# flow's job downstream), and the repair round "fixed" the error by deleting
# the node instead of leaving it in place for approval. attempts:2 produced a
# fully valid 3-node plan that silently under-served the ask.

_COMPOUND_ASK_TEXT = "run elasticity for demo on blinkit for Jan to Feb and promote the model to champion"


def _elasticity_window_entities() -> Entities:
    return Entities(brand="demo", platform="blinkit", window=WindowSpec(**{"from": "2026-01", "to": "2026-02"}))


def _three_node_plan() -> dict:
    """A genuinely VALID plan (no mutate node -> no sideeffect_policy issue)
    that nonetheless drops the compound ask's promotion intent entirely --
    exactly the "repair by amputation" shape observed live."""
    return {
        "plan_ir_version": 1,
        "intent_summary": "elasticity for demo on blinkit, Jan-Feb",
        "nodes": [
            {
                "id": "n1",
                "block": "panel_builder@1.0",
                "params": {"brand": "demo", "platform": "blinkit", "window": {"from": "2026-01", "to": "2026-02"}},
            },
            {"id": "n2", "block": "elasticity_dml@1.0", "inputs": {"panel": "n1.panel"}},
            {
                "id": "n3",
                "block": "post_model_checks@1.0",
                "inputs": {"surface": "n2.surface"},
                "gate": {"policy": "block"},
            },
        ],
        "outputs": {"surface": "n2.surface"},
        "assumptions": [],
        "estimated_cost": {"class": "seconds"},
    }


def _four_node_plan_with_promotion() -> dict:
    plan = _three_node_plan()
    plan["nodes"].append(
        {
            "id": "n4",
            "block": "promote_model@1.0",
            "inputs": {"surface": "n2.surface"},
            "params": {"brand": "demo", "platform": "blinkit", "alias": "champion"},
        }
    )
    plan["outputs"]["report"] = "n4.report"
    return plan


def test_plan_ask_intent_coverage_repair_adds_missing_promote_node_then_succeeds(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grant_all(tenant_id)
    classify_result = _compound_classify_result(_elasticity_window_entities(), constraints=["compound:promote"])

    transport = FakeTransport(
        [
            TransportResult(
                text=json.dumps(_three_node_plan()), input_tokens=100, output_tokens=80, cache_read_tokens=0, latency_ms=1000
            ),
            TransportResult(
                text=json.dumps(_four_node_plan_with_promotion()),
                input_tokens=100,
                output_tokens=80,
                cache_read_tokens=0,
                latency_ms=1000,
            ),
        ]
    )
    monkeypatch.setattr(planner_module, "get_gateway", lambda: Gateway(transport))

    with tenant_session(tenant_id) as session:
        outcome = plan_ask(session, tenant_id, classify_result, _COMPOUND_ASK_TEXT)

    assert outcome.source == "llm"
    assert outcome.attempts == 2  # attempt 1 (valid, but missing coverage) + 1 coverage-repair round
    assert outcome.plan is not None
    block_names = [node.block.split("@")[0] for node in outcome.plan.nodes]
    assert "promote_model" in block_names
    assert outcome.validation is not None
    assert all(error.code != "intent_unserved" for error in outcome.validation.errors)

    # the coverage-repair prompt carried the explicit "requires a <kind>-kind
    # node" wording naming the missing kind and the triggering constraint.
    assert len(transport.calls) == 2
    assert "requires a promotion-kind node" in transport.calls[1]
    assert "compound:promote" in transport.calls[1]


def test_plan_ask_intent_coverage_still_unserved_after_repair_fails_honestly(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grant_all(tenant_id)
    classify_result = _compound_classify_result(_elasticity_window_entities(), constraints=["compound:promote"])

    transport = FakeTransport(
        [
            TransportResult(
                text=json.dumps(_three_node_plan()), input_tokens=100, output_tokens=80, cache_read_tokens=0, latency_ms=1000
            ),
            TransportResult(
                text=json.dumps(_three_node_plan()), input_tokens=100, output_tokens=80, cache_read_tokens=0, latency_ms=1000
            ),
        ]
    )
    monkeypatch.setattr(planner_module, "get_gateway", lambda: Gateway(transport))

    with tenant_session(tenant_id) as session:
        outcome = plan_ask(session, tenant_id, classify_result, _COMPOUND_ASK_TEXT)

    assert outcome.source == "llm"
    assert outcome.attempts == 2
    assert outcome.plan is None
    assert outcome.validation is not None
    assert outcome.validation.valid is False
    error = next(e for e in outcome.validation.errors if e.code == "intent_unserved")
    assert error.details["token"] == "compound:promote"
    assert error.details["kind"] == "promotion"

    # never cached -- plan is None, so the existing "store only if valid and
    # plan is not None" guard never fires.
    with tenant_session(tenant_id) as session:
        template = session.execute(
            select(PlanTemplate).where(
                PlanTemplate.tenant_id == tenant_id, PlanTemplate.normalized_hash == classify_result.normalized_hash
            )
        ).scalar_one_or_none()
        assert template is None


def test_plan_ask_intent_coverage_is_noop_for_non_compound_ask(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grant_all(tenant_id)
    classify_result = _classify_result(Entities(brand="demo", platform="blinkit", metric="loss_making_skus"))

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
        outcome = plan_ask(session, tenant_id, classify_result, "how many loss making skus for demo on blinkit")

    # no compound constraint tokens -> the coverage guard never fires: exactly
    # the pre-existing 1-attempt success path, unchanged.
    assert outcome.source == "llm"
    assert outcome.attempts == 1
    assert outcome.validation is not None
    assert outcome.validation.valid is True
    assert len(transport.calls) == 1


def test_plan_ask_template_cache_hit_missing_required_kind_falls_through_to_llm(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive extension of the same guard to the template-cache branch: a
    cached template can only ever have been stored while fully valid, which
    is impossible for any plan containing a mutate-class node -- so a stored
    template can never cover a `promotion` requirement. Since
    `normalized_hash` separates compound asks only by constraint COUNT (not
    identity), a template cached under a different compound verb sharing the
    same shape could otherwise be served here. It must be treated as a cache
    miss instead."""
    _grant_all(tenant_id)
    classify_result = _compound_classify_result(_elasticity_window_entities(), constraints=["compound:promote"])

    with admin_session() as session:
        session.add(
            PlanTemplate(
                tenant_id=tenant_id,
                normalized_hash=classify_result.normalized_hash,
                ir=_three_node_plan(),
                hit_count=0,
                status="active",
            )
        )

    # The freshly-planned 4-node plan covers the compound intent, but a
    # promote_model node ALWAYS trips `sideeffect_policy` here (no approver
    # role at plan time) -- that's the existing, unrelated ordinary-repair
    # trigger, so the LLM is asked (and here, simply repeats the same,
    # already-correct plan) once before the coverage check even runs.
    four_node_response = TransportResult(
        text=json.dumps(_four_node_plan_with_promotion()),
        input_tokens=10,
        output_tokens=10,
        cache_read_tokens=0,
        latency_ms=5,
    )
    transport = FakeTransport([four_node_response, four_node_response])
    monkeypatch.setattr(planner_module, "get_gateway", lambda: Gateway(transport))

    with tenant_session(tenant_id) as session:
        outcome = plan_ask(session, tenant_id, classify_result, _COMPOUND_ASK_TEXT)

    assert outcome.source == "llm"  # NOT template_cache -- the stale template was discarded
    assert outcome.plan is not None
    block_names = [node.block.split("@")[0] for node in outcome.plan.nodes]
    assert "promote_model" in block_names

    with tenant_session(tenant_id) as session:
        template = session.execute(
            select(PlanTemplate).where(
                PlanTemplate.tenant_id == tenant_id, PlanTemplate.normalized_hash == classify_result.normalized_hash
            )
        ).scalar_one()
        assert template.hit_count == 0  # never bumped for a hit that was discarded


def test_required_kinds_for_constraints_maps_known_compound_tokens_only() -> None:
    assert planner_module._required_kinds_for_constraints([]) == set()
    assert planner_module._required_kinds_for_constraints(["compound:promote"]) == {"promotion"}
    assert planner_module._required_kinds_for_constraints(["compound:optimize"]) == {"optimizer"}
    assert planner_module._required_kinds_for_constraints(["compound:optimise"]) == {"optimizer"}
    # an unrecognized compound token implies no structural requirement.
    assert planner_module._required_kinds_for_constraints(["compound:archive"]) == set()
