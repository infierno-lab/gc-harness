"""Prompt byte-stability tests (spec §5.2): Tiers 0-2 of prompt assembly
(system rules, few-shots, capability digest) must be byte-stable across
renders — same hash, no timestamps, no dict-order nondeterminism — and
prompt-cache-safe across tenants: two tenants with identical grants must see
a byte-identical shared prefix (spec §8). Exercises the classifier's stage-2
prompt and the planner's prompt directly via their pure assembly functions —
no LLM calls, no network.
"""

import hashlib
import uuid
from datetime import date

import pytest

from core.catalog.digest import render_digest
from core.catalog.seed import apply_seed, grant_all_blocks
from core.classify.classifier import ClassifyResult, _render_stage2_prompt, normalized_hash
from core.classify.schema import Entities, NormalizedAsk
from core.db.models import Tenant
from core.db.session import admin_session, tenant_session
from core.plan.planner import _render_planner_prompt
from packs.demand.catalog_seed import BLOCKS, METRICS


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module", autouse=True)
def _seed_demand_catalog(migrated_test_db: str) -> None:
    with admin_session() as session:
        apply_seed(session, BLOCKS, METRICS)


def _new_tenant(name: str) -> uuid.UUID:
    with admin_session() as session:
        tenant = Tenant(name=name)
        session.add(tenant)
        session.flush()
        return tenant.id


def _grant_all(tenant_id: uuid.UUID) -> None:
    with admin_session() as session:
        grant_all_blocks(session, tenant_id)


def _classify_result(entities: Entities) -> ClassifyResult:
    normalized = NormalizedAsk(
        task_type="analytic_query", domain="demand", entities=entities, output_wanted="answer", confidence=0.9
    )
    return ClassifyResult(normalized=normalized, stage="rules", normalized_hash=normalized_hash(normalized), latency_ms=1)


# --- (a) classifier stage-2 prompt: byte-stable across renders ---------------


def test_classify_prompt_is_byte_stable_across_renders() -> None:
    text = "Run elasticity for Brand X for H1 and show what changed."
    first = _render_stage2_prompt(text)
    second = _render_stage2_prompt(text)
    assert first == second
    assert _sha256(first) == _sha256(second)


# --- (b) planner prompt: byte-stable across renders ---------------------------


def test_planner_prompt_is_byte_stable_across_renders(migrated_test_db: str) -> None:
    tenant_id = _new_tenant(f"stability-tenant-{uuid.uuid4()}")
    _grant_all(tenant_id)
    classify = _classify_result(Entities(brand="demo", platform="blinkit", metric="loss_making_skus"))
    raw_text = "how many loss making skus for demo on blinkit"

    with tenant_session(tenant_id) as session:
        digest = render_digest(session, tenant_id)

    first = _render_planner_prompt(digest.text, classify, raw_text)
    second = _render_planner_prompt(digest.text, classify, raw_text)
    assert first == second
    assert _sha256(first) == _sha256(second)


# --- (c) the digest portion is byte-stable at the prompt level ---------------


def test_planner_prompt_digest_section_is_byte_stable_across_digest_renders(migrated_test_db: str) -> None:
    tenant_id = _new_tenant(f"stability-tenant-{uuid.uuid4()}")
    _grant_all(tenant_id)
    classify = _classify_result(Entities(brand="demo", platform="blinkit", metric="loss_making_skus"))
    raw_text = "how many loss making skus for demo on blinkit"

    with tenant_session(tenant_id) as session:
        digest_first = render_digest(session, tenant_id)
    with tenant_session(tenant_id) as session:
        digest_second = render_digest(session, tenant_id)

    assert digest_first.text == digest_second.text
    assert digest_first.digest_hash == digest_second.digest_hash

    prompt_first = _render_planner_prompt(digest_first.text, classify, raw_text)
    prompt_second = _render_planner_prompt(digest_second.text, classify, raw_text)
    assert prompt_first == prompt_second
    # the digest text itself must appear verbatim, unchanged, inside both renders.
    assert digest_first.text in prompt_first
    assert digest_second.text in prompt_second


# --- (d) cross-tenant contamination: identical grants -> identical shared prefix


def test_classify_prompt_shared_verbatim_across_tenants() -> None:
    # The classifier's stage-2 prompt takes no tenant/digest input at all — it
    # is a pure function of the ask text (spec §8 prompt-cache-safe tenancy:
    # Tiers 0-2 "contain nothing tenant-specific"). Two tenants asking the
    # same thing must therefore produce a byte-identical prompt, full stop.
    text = "Build a promo plan with a budget of 8000 on Zepto for demo brand next month."
    assert _render_stage2_prompt(text) == _render_stage2_prompt(text)


def test_planner_prompt_shared_prefix_identical_across_tenants_with_identical_grants(
    migrated_test_db: str,
) -> None:
    tenant_a = _new_tenant(f"stability-tenant-a-{uuid.uuid4()}")
    tenant_b = _new_tenant(f"stability-tenant-b-{uuid.uuid4()}")
    # identical grants for both tenants (every active block version) --
    # this is the precondition spec §8 requires for a shared cache prefix.
    _grant_all(tenant_a)
    _grant_all(tenant_b)

    classify = _classify_result(Entities(brand="demo", platform="blinkit", metric="loss_making_skus"))
    raw_text = "how many loss making skus for demo on blinkit"

    with tenant_session(tenant_a) as session:
        digest_a = render_digest(session, tenant_a)
    with tenant_session(tenant_b) as session:
        digest_b = render_digest(session, tenant_b)

    # same grants -> byte-identical digest text: nothing tenant-specific (no
    # tenant_id, no per-tenant ordering) leaks into the digest content itself.
    assert digest_a.text == digest_b.text
    assert digest_a.digest_hash == digest_b.digest_hash

    prompt_a = _render_planner_prompt(digest_a.text, classify, raw_text)
    prompt_b = _render_planner_prompt(digest_b.text, classify, raw_text)
    assert prompt_a == prompt_b

    # the shared prefix (rules + capability digest, i.e. everything before the
    # few-shot examples) must be byte-identical regardless of tenant -- this is
    # the literal portion the prompt cache would key on.
    prefix_a = prompt_a.split("\n\nExample (multi-node pipeline):", 1)[0]
    prefix_b = prompt_b.split("\n\nExample (multi-node pipeline):", 1)[0]
    assert prefix_a == prefix_b
    assert len(prefix_a) > 0


def test_planner_prompt_differs_only_in_tenant_scoped_parts_when_grants_diverge(
    migrated_test_db: str,
) -> None:
    """The counterpart check: when grants genuinely differ, the digest (and
    therefore the prompt) MUST differ -- confirming the previous test's
    byte-equality isn't a false positive from a digest that ignores grants
    entirely."""
    tenant_full = _new_tenant(f"stability-tenant-full-{uuid.uuid4()}")
    tenant_ungranted = _new_tenant(f"stability-tenant-empty-{uuid.uuid4()}")
    _grant_all(tenant_full)
    # tenant_ungranted gets nothing.

    classify = _classify_result(Entities(brand="demo", platform="blinkit", metric="loss_making_skus"))
    raw_text = "how many loss making skus for demo on blinkit"

    with tenant_session(tenant_full) as session:
        digest_full = render_digest(session, tenant_full)
    with tenant_session(tenant_ungranted) as session:
        digest_empty = render_digest(session, tenant_ungranted)

    assert digest_full.text != digest_empty.text

    prompt_full = _render_planner_prompt(digest_full.text, classify, raw_text)
    prompt_empty = _render_planner_prompt(digest_empty.text, classify, raw_text)
    assert prompt_full != prompt_empty

    # but the rules text itself (the very first line, identity/hard-rules
    # Tier 0) is still shared verbatim regardless of grants.
    rules_line_full = prompt_full.split("\n", 1)[0]
    rules_line_empty = prompt_empty.split("\n", 1)[0]
    assert rules_line_full == rules_line_empty


# --- (e) no volatile tokens: no wall-clock leakage into the prompt -----------


def test_classify_prompt_has_no_wall_clock_leakage() -> None:
    text = "How many SKUs made losses last quarter?"  # no ISO datestamp in the ask itself
    prompt = _render_stage2_prompt(text)
    today_iso = date.today().isoformat()
    assert today_iso not in text  # sanity: the ask carries no date literal
    assert today_iso not in prompt  # so any match here would be a leaked wall-clock stamp


def test_planner_prompt_has_no_wall_clock_leakage(migrated_test_db: str) -> None:
    tenant_id = _new_tenant(f"stability-tenant-{uuid.uuid4()}")
    _grant_all(tenant_id)
    classify = _classify_result(Entities(brand="demo", platform="blinkit", metric="loss_making_skus"))
    raw_text = "how many loss making skus for demo on blinkit"  # no ISO datestamp in the ask

    with tenant_session(tenant_id) as session:
        digest = render_digest(session, tenant_id)

    prompt = _render_planner_prompt(digest.text, classify, raw_text)
    today_iso = date.today().isoformat()
    assert today_iso not in raw_text
    assert today_iso not in digest.text
    assert today_iso not in prompt
