"""Classifier tests (spec §5.3): stage-0 rules, entity/window resolution
pinned to a fixed reference date, normalized_hash shape-invariance, and the
stage-2 LLM path over a FakeTransport. One live smoke test (`-m live`) hits
the real `claude` CLI.
"""

import json
import uuid
from datetime import date

import pytest

from core.classify.classifier import ClassifyResult, classify_ask, normalized_hash
import core.classify.classifier as classifier_module
from core.classify.entities import resolve_brand, resolve_platform, resolve_window
from core.classify.rules import classify_stage0
from core.classify.schema import Entities, NormalizedAsk, WindowSpec
from core.db.session import tenant_session
from core.gateway.client import Gateway
from evals.test_gateway import FakeTransport
from core.gateway.transport import TransportResult

REFERENCE_DATE = date(2026, 7, 31)


# --- stage 0 -----------------------------------------------------------------


def test_stage0_run_elasticity() -> None:
    ask = classify_stage0("Run elasticity for demo on blinkit for Jan-Jun 2026", REFERENCE_DATE)
    assert ask is not None
    assert ask.task_type == "pipeline"
    assert ask.entities.brand == "demo"
    assert ask.entities.platform == "blinkit"
    assert ask.entities.window == WindowSpec(**{"from": "2026-01", "to": "2026-06"})


def test_stage0_how_many_losses() -> None:
    ask = classify_stage0("How many SKUs made losses last quarter?", REFERENCE_DATE)
    assert ask is not None
    assert ask.task_type == "analytic_query"
    assert ask.entities.metric == "loss_making_skus"
    assert ask.entities.window == WindowSpec(**{"from": "2026-04", "to": "2026-06"})


def test_stage0_show_recent_runs() -> None:
    ask = classify_stage0("show recent runs", REFERENCE_DATE)
    assert ask is not None
    assert ask.task_type == "ops_status"


def test_stage0_no_match_falls_through() -> None:
    assert classify_stage0("What is the elasticity of SKU 123?", REFERENCE_DATE) is None


# --- stage-0 anchoring regression ---------------------------------------------
#
# LIVE BUG (confirmed via the API): "run elasticity for demo on blinkit for
# Jan to Feb and promote the model to champion" matched the old, unanchored
# `^run\s+elasticity\b` rule -> classified as the plain pipeline shape ->
# normalized_hash collided with the plain elasticity ask -> the plan-template
# cache bound the OLD 3-node plan -> executed, silently DROPPING the
# promotion intent. Every stage-0 rule must claim an ask ONLY when the whole
# ask is described by its shape; anything else must fall through to stage 2.

_STAGE0_CANONICAL_ASKS = [
    "Run elasticity for demo on blinkit for Jan-Jun 2026",
    "Run elasticity for demo on zepto for H1 2026",
    "How many SKUs made losses last quarter?",
    "How many SKUs made losses last quarter for demo on blinkit?",
    "show recent runs",
    "show me recent runs",
    "show recent runs please",
]


@pytest.mark.parametrize("canonical", _STAGE0_CANONICAL_ASKS)
def test_stage0_canonical_asks_still_match_after_anchoring(canonical: str) -> None:
    assert classify_stage0(canonical, REFERENCE_DATE) is not None


@pytest.mark.parametrize("canonical", _STAGE0_CANONICAL_ASKS)
def test_stage0_rejects_canonical_shape_plus_trailing_and_clause(canonical: str) -> None:
    # A stage-0 rule may only claim an ask that is FULLY described by its
    # shape (principled full-match via end-to-end anchoring, not a blocklist
    # of extra verbs) -- this proves it structurally for every existing rule:
    # tack an unrelated directive onto each canonical ask and confirm it now
    # falls through to stage 2 instead of being silently truncated.
    compound = canonical.rstrip("?") + " and do something else entirely"
    assert classify_stage0(compound, REFERENCE_DATE) is None


def test_stage0_run_elasticity_rejects_the_live_bug_ask() -> None:
    # The exact ask from the reported incident.
    assert (
        classify_stage0(
            "run elasticity for demo on blinkit for Jan to Feb and promote the model to champion",
            REFERENCE_DATE,
        )
        is None
    )


def test_stage0_how_many_losses_rejects_trailing_directive() -> None:
    assert classify_stage0("How many SKUs made losses last quarter and email the CFO", REFERENCE_DATE) is None


def test_stage0_show_recent_runs_rejects_trailing_directive() -> None:
    assert classify_stage0("show recent runs and archive the old ones", REFERENCE_DATE) is None


# --- window / entity resolution ----------------------------------------------


def test_resolve_window_last_quarter_is_2026_q2() -> None:
    window = resolve_window("last quarter", REFERENCE_DATE)
    assert window == WindowSpec(**{"from": "2026-04", "to": "2026-06"})


def test_resolve_window_this_and_next_quarter() -> None:
    assert resolve_window("this quarter", REFERENCE_DATE) == WindowSpec(**{"from": "2026-07", "to": "2026-09"})
    assert resolve_window("next quarter", REFERENCE_DATE) == WindowSpec(**{"from": "2026-10", "to": "2026-12"})


def test_resolve_window_year_boundary() -> None:
    # last quarter of Jan 2026 (Q1) is Q4 of the PRIOR year.
    assert resolve_window("last quarter", date(2026, 1, 15)) == WindowSpec(**{"from": "2025-10", "to": "2025-12"})


def test_resolve_window_h1_h2() -> None:
    assert resolve_window("H1", REFERENCE_DATE) == WindowSpec(**{"from": "2026-01", "to": "2026-06"})
    assert resolve_window("H2 2027", REFERENCE_DATE) == WindowSpec(**{"from": "2027-07", "to": "2027-12"})


def test_resolve_window_month_range_and_iso_range() -> None:
    assert resolve_window("jan-feb", REFERENCE_DATE) == WindowSpec(**{"from": "2026-01", "to": "2026-02"})
    assert resolve_window("January to March 2025", REFERENCE_DATE) == WindowSpec(
        **{"from": "2025-01", "to": "2025-03"}
    )
    assert resolve_window("2026-01 to 2026-06", REFERENCE_DATE) == WindowSpec(**{"from": "2026-01", "to": "2026-06"})


def test_resolve_window_none_when_unrecognized() -> None:
    assert resolve_window("sometime soon", REFERENCE_DATE) is None
    assert resolve_window(None, REFERENCE_DATE) is None


def test_resolve_brand_and_platform_allowlist() -> None:
    assert resolve_brand("Demo") == "demo"
    assert resolve_brand("nivea") is None
    assert resolve_brand(None) is None
    assert resolve_platform("Blinkit") == "blinkit"
    assert resolve_platform("bigbasket") is None


# --- normalized_hash shape-invariance -----------------------------------------


def _ask(**entity_overrides) -> NormalizedAsk:
    entities = Entities(brand="demo", platform="blinkit", window=WindowSpec(**{"from": "2026-01", "to": "2026-06"}))
    entities = entities.model_copy(update=entity_overrides)
    return NormalizedAsk(
        task_type="pipeline", domain="demand", entities=entities, output_wanted="surface", confidence=0.9
    )


def test_normalized_hash_ignores_entity_values() -> None:
    a = _ask(brand="demo")
    b = _ask(brand="nivea", window=WindowSpec(**{"from": "2026-02", "to": "2026-05"}))
    assert normalized_hash(a) == normalized_hash(b)


def test_normalized_hash_changes_with_entity_keys() -> None:
    a = _ask()
    b = _ask(price_delta_pct=-0.1)  # a NEW entity key present, not just a new value
    assert normalized_hash(a) != normalized_hash(b)


def test_normalized_hash_changes_with_task_type_or_domain() -> None:
    a = _ask()
    b = a.model_copy(update={"task_type": "scenario"})
    c = a.model_copy(update={"domain": "finance"})
    assert normalized_hash(a) != normalized_hash(b)
    assert normalized_hash(a) != normalized_hash(c)


def test_normalized_hash_changes_with_output_wanted() -> None:
    # Regression: a compound ask ("...and promote the model to champion")
    # reports the SAME task_type/domain/entity_keys as the plain ask it's
    # built from, but a different output_wanted ("report" vs "surface",
    # confirmed against the live stage-2 classifier) -- the hash must
    # distinguish them on that basis alone, or the plan-template cache binds
    # the compound ask to the plain ask's template (cache poisoning).
    a = _ask()  # output_wanted="surface"
    b = a.model_copy(update={"output_wanted": "report"})
    assert normalized_hash(a) != normalized_hash(b)


def test_normalized_hash_changes_with_constraint_count() -> None:
    a = _ask()  # constraints=[]
    b = a.model_copy(update={"constraints": ["top-20 SKUs"]})
    assert normalized_hash(a) != normalized_hash(b)


def test_normalized_hash_still_ignores_constraint_text() -> None:
    # consistent with entity_keys' values-blindness: same COUNT of
    # constraints, different text, must still share a template.
    a = _ask().model_copy(update={"constraints": ["top-20 SKUs"]})
    b = _ask().model_copy(update={"constraints": ["bottom-10 SKUs"]})
    assert normalized_hash(a) == normalized_hash(b)


# --- stage 2 (LLM path, FakeTransport — no network) --------------------------


def test_classify_ask_stage2_uses_llm_and_resolves_entities_in_code(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_json = (
        '{"task_type":"scenario","domain":"demand","output_wanted":"answer",'
        '"constraints":["top-20 SKUs"],"ambiguities":[],"confidence":0.8,'
        '"entities":{"brand":null,"platform":"Blinkit","window_text":"last quarter",'
        '"price_delta_pct":-0.1,"budget":null,"metric":null}}'
    )
    transport = FakeTransport(
        [TransportResult(text=raw_json, input_tokens=50, output_tokens=40, cache_read_tokens=0, latency_ms=800)]
    )
    fake_gateway = Gateway(transport)
    monkeypatch.setattr(classifier_module, "get_gateway", lambda: fake_gateway)

    with tenant_session(tenant_id) as session:
        result = classify_ask(
            session,
            tenant_id,
            "How much volume do we lose if we cut discounts 10% on our top-20 SKUs on Blinkit?",
            reference_date=REFERENCE_DATE,
        )

    assert isinstance(result, ClassifyResult)
    assert result.stage == "llm"
    assert result.normalized.task_type == "scenario"
    # the LLM's raw "Blinkit"/"last quarter" were resolved IN CODE, not trusted verbatim.
    assert result.normalized.entities.platform == "blinkit"
    assert result.normalized.entities.window == WindowSpec(**{"from": "2026-04", "to": "2026-06"})
    assert result.normalized.entities.price_delta_pct == -0.1
    assert len(transport.calls) == 1


def test_classify_ask_stage2_flags_unresolvable_entities_as_ambiguities(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_json = (
        '{"task_type":"pipeline","domain":"demand","output_wanted":"surface",'
        '"constraints":[],"ambiguities":[],"confidence":0.7,'
        '"entities":{"brand":"nivea","platform":"bigbasket","window_text":null,'
        '"price_delta_pct":null,"budget":null,"metric":null}}'
    )
    transport = FakeTransport(
        [TransportResult(text=raw_json, input_tokens=10, output_tokens=10, cache_read_tokens=0, latency_ms=500)]
    )
    monkeypatch.setattr(classifier_module, "get_gateway", lambda: Gateway(transport))

    with tenant_session(tenant_id) as session:
        result = classify_ask(
            session, tenant_id, "Please compute elasticity for nivea on bigbasket", reference_date=REFERENCE_DATE
        )

    assert result.normalized.entities.brand is None
    assert result.normalized.entities.platform is None
    assert any("nivea" in a for a in result.normalized.ambiguities)
    assert any("bigbasket" in a for a in result.normalized.ambiguities)


def test_classify_ask_prefers_stage0_over_llm(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom():
        raise AssertionError("stage 0 should have handled this ask without calling the gateway")

    monkeypatch.setattr(classifier_module, "get_gateway", _boom)

    with tenant_session(tenant_id) as session:
        result = classify_ask(session, tenant_id, "show recent runs", reference_date=REFERENCE_DATE)

    assert result.stage == "rules"


def test_classify_ask_compound_elasticity_ask_reaches_stage2_and_distinguishes_hash(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full regression for the reported live bug, both defects together:
    (1) the compound ask must fall through the anchored stage-0 rule to
    stage 2 (never silently truncated, and never touching the gateway for the
    plain ask, which stage 0 still legitimately handles), and (2) once there,
    its normalized_hash must differ from the plain ask's -- verified against
    the ACTUAL stage-2 output shape for the compound ask (checked live
    against the real `claude` CLI transport: `output_wanted` comes back
    "surface" for the plain ask and "report" for the compound one;
    `constraints` is empty for both, so output_wanted is what must carry the
    distinction here)."""
    compound_response = json.dumps(
        {
            "task_type": "pipeline",
            "domain": "demand",
            "output_wanted": "report",
            "constraints": [],
            "ambiguities": [],
            "confidence": 0.88,
            "entities": {
                "brand": "demo",
                "platform": "blinkit",
                "window_text": "Jan to Feb",
                "price_delta_pct": None,
                "budget": None,
                "metric": None,
            },
        }
    )
    transport = FakeTransport(
        [TransportResult(text=compound_response, input_tokens=10, output_tokens=10, cache_read_tokens=0, latency_ms=5)]
    )
    monkeypatch.setattr(classifier_module, "get_gateway", lambda: Gateway(transport))

    plain_text = "run elasticity for demo on blinkit for Jan to Feb"
    compound_text = "run elasticity for demo on blinkit for Jan to Feb and promote the model to champion"

    with tenant_session(tenant_id) as session:
        # the plain ask is a legitimate, fully-described stage-0 shape -- it
        # must stay on the rules ladder (fast path) and never touch the gateway.
        plain_result = classify_ask(session, tenant_id, plain_text, reference_date=REFERENCE_DATE)
        # the compound ask is NOT fully described by that shape -- it must
        # fall through to the (faked) stage-2 LLM instead of being truncated.
        compound_result = classify_ask(session, tenant_id, compound_text, reference_date=REFERENCE_DATE)

    assert plain_result.stage == "rules"
    assert compound_result.stage == "llm"
    assert len(transport.calls) == 1  # only the compound ask ever reached the gateway

    # both share task_type/domain/entity_keys (same brand/platform/window) --
    # the OLD hash would have collided them here.
    assert plain_result.normalized.task_type == compound_result.normalized.task_type == "pipeline"
    assert plain_result.normalized.entities.brand == compound_result.normalized.entities.brand == "demo"
    assert plain_result.normalized.entities.platform == compound_result.normalized.entities.platform == "blinkit"

    # (2) output_wanted differs, and the hash now reflects that.
    assert plain_result.normalized.output_wanted == "surface"
    assert compound_result.normalized.output_wanted == "report"
    assert plain_result.normalized_hash != compound_result.normalized_hash


def test_classify_ask_compound_directive_guard_separates_hash_even_when_llm_under_reports(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE regression for the reachable cache-poisoning hole: stage 2's own
    signal for a compound ask is not guaranteed deterministic (confirmed:
    Haiku returned output_wanted="surface" -- identical to the plain ask --
    for a compound ask on one live call). This reproduces exactly that
    under-report (same output_wanted, empty constraints, for BOTH asks) and
    proves the code-level compound-directive guard
    (core/classify/compound.py) still separates their normalized_hash by
    scanning the raw ask text itself, independent of what the LLM said."""
    colliding_response = json.dumps(
        {
            "task_type": "pipeline",
            "domain": "demand",
            "output_wanted": "surface",  # deliberately IDENTICAL for both asks -- an under-report
            "constraints": [],  # deliberately empty -- the LLM signals nothing extra
            "ambiguities": [],
            "confidence": 0.9,
            "entities": {
                "brand": "demo",
                "platform": "blinkit",
                "window_text": "Jan to Feb",
                "price_delta_pct": None,
                "budget": None,
                "metric": None,
            },
        }
    )
    transport = FakeTransport(
        [
            TransportResult(text=colliding_response, input_tokens=10, output_tokens=10, cache_read_tokens=0, latency_ms=5),
            TransportResult(text=colliding_response, input_tokens=10, output_tokens=10, cache_read_tokens=0, latency_ms=5),
        ]
    )
    monkeypatch.setattr(classifier_module, "get_gateway", lambda: Gateway(transport))

    # Phrased so NEITHER ask matches a stage-0 rule (no "run elasticity for
    # <brand> on <platform> for <window>" prefix) -- both must reach the
    # (faked) stage-2 LLM to exercise the guard.
    plain_text = "Please compute elasticity for demo across blinkit for Jan to Feb"
    compound_text = plain_text + " and promote the model to champion"

    with tenant_session(tenant_id) as session:
        plain_result = classify_ask(session, tenant_id, plain_text, reference_date=REFERENCE_DATE)
        compound_result = classify_ask(session, tenant_id, compound_text, reference_date=REFERENCE_DATE)

    assert plain_result.stage == "llm"
    assert compound_result.stage == "llm"

    # the LLM reported IDENTICAL output_wanted/constraints for both -- this is
    # the under-report case, not the lucky one from the earlier test.
    assert plain_result.normalized.output_wanted == compound_result.normalized.output_wanted == "surface"
    assert plain_result.normalized.constraints == []

    # the code-level guard added a deterministic token derived from the RAW
    # ask text -- never from the LLM's parse of it.
    assert compound_result.normalized.constraints == ["compound:promote"]

    # normalized_hash is therefore still separated, closing the hole.
    assert plain_result.normalized_hash != compound_result.normalized_hash


def test_classify_ask_plain_llm_ask_gains_no_compound_constraint_token(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_json = (
        '{"task_type":"scenario","domain":"demand","output_wanted":"answer",'
        '"constraints":[],"ambiguities":[],"confidence":0.8,'
        '"entities":{"brand":"demo","platform":"zepto","window_text":null,'
        '"price_delta_pct":0.05,"budget":null,"metric":null}}'
    )
    transport = FakeTransport(
        [TransportResult(text=raw_json, input_tokens=10, output_tokens=10, cache_read_tokens=0, latency_ms=5)]
    )
    monkeypatch.setattr(classifier_module, "get_gateway", lambda: Gateway(transport))

    with tenant_session(tenant_id) as session:
        result = classify_ask(
            session, tenant_id, "What if we raise prices 5% for the demo brand on Zepto?", reference_date=REFERENCE_DATE
        )

    assert result.normalized.constraints == []


# --- live smoke (excluded by default; run with `pytest -m live`) ------------


@pytest.mark.live
def test_classify_ask_live_smoke(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    # Phrased to bypass every stage-0 rule (no "run elasticity" / "how many
    # ... loss" / "show recent runs" prefix) so this genuinely exercises the
    # real `claude` CLI, not a rules-ladder shortcut.
    with tenant_session(tenant_id) as session:
        result = classify_ask(
            session,
            tenant_id,
            "What's the elasticity trend for the demo brand on blinkit over H1?",
            reference_date=REFERENCE_DATE,
        )
    assert result.stage == "llm"
    assert result.normalized.confidence >= 0.0
