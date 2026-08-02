"""Golden-ask regression suite (spec §9 Evals, §12 PoC scope "golden-ask
suite (~30)") — THE gate for any change to prompts, digest, catalog
descriptions, or model pins.

Classifier LLM calls (and, for the `plan_expect` entries, planner calls)
replay canned responses from `evals/fixtures/golden_recordings.json` by
default — zero network, fully deterministic. A missing recording fails the
test with a message naming exactly which ask needs refreshing; refresh with
`pytest -m live_record`, which runs `test_refresh_golden_recordings` against
the real `claude` CLI transport and rewrites the recordings file (see
`evals/recording.py`).
"""

import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

import core.classify.classifier as classifier_module
import core.plan.planner as planner_module
from core.catalog.seed import apply_seed, grant_all_blocks
from core.classify.classifier import ClassifyResult, classify_ask
from core.classify.schema import WindowSpec
from core.db.session import admin_session, tenant_session
from core.gateway.cli_transport import ClaudeCliTransport
from core.gateway.client import Gateway
from core.plan.planner import plan_ask
from core.plan.validator import topo_order
from evals.recording import RecordingTransport
from packs.demand.catalog_seed import BLOCKS, METRICS

REFERENCE_DATE = date(2026, 7, 31)
CORPUS_PATH = Path(__file__).resolve().parent / "golden_asks.yaml"


def _load_corpus() -> list[dict[str, Any]]:
    return yaml.safe_load(CORPUS_PATH.read_text())


CORPUS = _load_corpus()
PLAN_ENTRIES = [entry for entry in CORPUS if "plan_expect" in entry]


@pytest.fixture(scope="session", autouse=True)
def _seed_demand_catalog(migrated_test_db: str) -> None:
    with admin_session() as session:
        apply_seed(session, BLOCKS, METRICS)


def _assert_entities_subset(actual, expected: dict[str, Any]) -> None:
    for key, value in expected.items():
        actual_value = getattr(actual, key)
        if key == "window" and value is not None:
            assert actual_value == WindowSpec(**value), f"window: expected {value}, got {actual_value}"
        else:
            assert actual_value == value, f"entity {key!r}: expected {value!r}, got {actual_value!r}"


def _assert_classify_expectations(result: ClassifyResult, expect: dict[str, Any]) -> None:
    if "task_type" in expect:
        assert result.normalized.task_type == expect["task_type"]
    if "stage" in expect:
        assert result.stage == expect["stage"]
    if "min_confidence" in expect:
        assert result.normalized.confidence >= expect["min_confidence"]
    if "max_confidence" in expect:
        assert result.normalized.confidence <= expect["max_confidence"]
    if "entities" in expect:
        _assert_entities_subset(result.normalized.entities, expect["entities"])
    if "ambiguities_contains" in expect:
        for needle in expect["ambiguities_contains"]:
            assert any(needle.lower() in ambiguity.lower() for ambiguity in result.normalized.ambiguities), (
                f"expected an ambiguity containing {needle!r}, got {result.normalized.ambiguities!r}"
            )


def _boom_gateway() -> Gateway:
    raise AssertionError("plan_ask should have short-circuited for an out-of-domain ask without touching the gateway")


@pytest.mark.parametrize("entry", CORPUS, ids=[entry["id"] for entry in CORPUS])
def test_golden_ask_classify(
    entry: dict[str, Any], migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = RecordingTransport(label=f"{entry['id']} classify")
    monkeypatch.setattr(classifier_module, "get_gateway", lambda: Gateway(transport))
    expect = entry["expect"]

    with tenant_session(tenant_id) as session:
        result = classify_ask(session, tenant_id, entry["text"], reference_date=REFERENCE_DATE)

    _assert_classify_expectations(result, expect)

    if entry.get("plan_skip_check"):
        # Out-of-domain asks (task_type="unknown") must never reach the
        # planner's LLM call — verified by making the gateway itself raise if
        # touched, not just by inspecting PlanOutcome fields.
        monkeypatch.setattr(planner_module, "get_gateway", _boom_gateway)
        with tenant_session(tenant_id) as session:
            outcome = plan_ask(session, tenant_id, result, entry["text"])
        assert outcome.source == "skipped"
        assert outcome.plan is None
        assert outcome.validation is None


@pytest.mark.parametrize("entry", PLAN_ENTRIES, ids=[entry["id"] for entry in PLAN_ENTRIES])
def test_golden_ask_plan(
    entry: dict[str, Any], migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    with admin_session() as session:
        grant_all_blocks(session, tenant_id)

    classify_transport = RecordingTransport(label=f"{entry['id']} classify")
    monkeypatch.setattr(classifier_module, "get_gateway", lambda: Gateway(classify_transport))
    with tenant_session(tenant_id) as session:
        classify_result = classify_ask(session, tenant_id, entry["text"], reference_date=REFERENCE_DATE)

    plan_transport = RecordingTransport(label=f"{entry['id']} plan")
    monkeypatch.setattr(planner_module, "get_gateway", lambda: Gateway(plan_transport))
    with tenant_session(tenant_id) as session:
        outcome = plan_ask(session, tenant_id, classify_result, entry["text"])

    plan_expect = entry["plan_expect"]
    assert outcome.validation is not None
    assert outcome.validation.valid == plan_expect["valid"], (
        f"expected validation.valid={plan_expect['valid']}, got errors={outcome.validation.errors!r}"
    )
    # Checked whenever `blocks` is declared, independent of `valid`: a plan
    # containing a mutate-class node (e.g. promote_model) is CORRECTLY
    # `valid: false` (sideeffect_policy — plan_ask never holds an approver
    # role, that's the two-person approval flow's job downstream) while still
    # fully composing the requested pipeline. `valid` and "did it compose the
    # right blocks" are independent questions; conflating them would make it
    # impossible to golden-test a compound ask that legitimately requires
    # approval (see regression_compound_elasticity_and_promote).
    if "blocks" in plan_expect:
        assert outcome.plan is not None, "plan_expect declares expected blocks but no plan was produced"
        order = topo_order(outcome.plan)
        node_by_id = {node.id: node for node in outcome.plan.nodes}
        block_names = [node_by_id[node_id].block.split("@")[0] for node_id in order]
        assert block_names == plan_expect["blocks"]


# --- recording refresh (excluded by default; run with `pytest -m live_record`) ---


@pytest.mark.live_record
def test_refresh_golden_recordings(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs the full corpus against the real `claude` CLI transport and
    rewrites `golden_recordings.json` in place. Continues past a single ask's
    failure so partial progress is never lost — failures are collected and
    reported together at the end."""
    with admin_session() as session:
        grant_all_blocks(session, tenant_id)

    failures: list[str] = []

    for entry in CORPUS:
        try:
            classify_transport = RecordingTransport(
                label=f"{entry['id']} classify", real_transport=ClaudeCliTransport(), record=True
            )
            monkeypatch.setattr(classifier_module, "get_gateway", lambda t=classify_transport: Gateway(t))
            with tenant_session(tenant_id) as session:
                classify_result = classify_ask(session, tenant_id, entry["text"], reference_date=REFERENCE_DATE)

            if "plan_expect" in entry:
                plan_transport = RecordingTransport(
                    label=f"{entry['id']} plan", real_transport=ClaudeCliTransport(), record=True
                )
                monkeypatch.setattr(planner_module, "get_gateway", lambda t=plan_transport: Gateway(t))
                with tenant_session(tenant_id) as session:
                    plan_ask(session, tenant_id, classify_result, entry["text"])
        except Exception as exc:  # noqa: BLE001 - collect and keep recording the rest
            failures.append(f"{entry['id']}: {exc}")

    assert not failures, "some asks failed to record:\n" + "\n".join(failures)
