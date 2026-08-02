"""Intent classifier ladder, stages 0 + 2 (spec §5.3). Stage 1 (embedding kNN
over labeled traffic) is deferred per §12 PoC scope until labeled traffic
exists.

Stage 2 asks the LLM only for *raw* entity mentions (verbatim brand/platform
strings, a raw time-window phrase) — never resolved values. Resolution
(month-window math, brand/platform canonicalization) always happens in
`core.classify.entities`, in code, per spec §5.3.
"""

import json
import time
from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.catalog.hashing import content_hash
from core.classify.compound import compound_directive_constraints
from core.classify.entities import resolve_brand, resolve_platform, resolve_window, scan_brand
from core.classify.rules import classify_stage0
from core.classify.schema import Entities, NormalizedAsk
from core.gateway import get_gateway, resolve_model

_LLM_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_type": {
            "type": "string",
            "enum": ["analytic_query", "pipeline", "scenario", "optimization", "ops_status", "unknown"],
        },
        "domain": {
            "type": ["string", "null"],
            "description": "null means the ask doesn't belong to any domain this system knows — "
            "out-of-domain, resolved deterministically in code (never guess a domain).",
        },
        "output_wanted": {"type": ["string", "null"]},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "ambiguities": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "entities": {
            "type": "object",
            "properties": {
                "brand": {"type": ["string", "null"]},
                "platform": {"type": ["string", "null"]},
                "window_text": {
                    "type": ["string", "null"],
                    "description": "verbatim natural-language time mention, e.g. 'last quarter', 'Jan-Jun 2026' — never resolved by you",
                },
                "price_delta_pct": {"type": ["number", "null"]},
                "budget": {"type": ["number", "null"]},
                "metric": {"type": ["string", "null"]},
            },
            "required": [],
        },
    },
    "required": ["task_type", "domain", "output_wanted", "constraints", "ambiguities", "confidence", "entities"],
}

_FEW_SHOT: list[tuple[str, dict[str, Any]]] = [
    (
        "How many SKUs made losses last quarter?",
        {
            "task_type": "analytic_query",
            "domain": "demand",
            "output_wanted": "answer",
            "constraints": [],
            "ambiguities": [],
            "confidence": 0.92,
            "entities": {
                "brand": None,
                "platform": None,
                "window_text": "last quarter",
                "price_delta_pct": None,
                "budget": None,
                "metric": "loss_making_skus",
            },
        },
    ),
    (
        "Run elasticity for Brand X for H1 and show what changed.",
        {
            "task_type": "pipeline",
            "domain": "demand",
            "output_wanted": "surface",
            "constraints": [],
            "ambiguities": [],
            "confidence": 0.85,
            "entities": {
                "brand": "brand x",
                "platform": None,
                "window_text": "H1",
                "price_delta_pct": None,
                "budget": None,
                "metric": None,
            },
        },
    ),
    (
        "How much volume do we lose if we cut discounts 10% on our top-20 SKUs on Blinkit?",
        {
            "task_type": "scenario",
            "domain": "demand",
            "output_wanted": "answer",
            "constraints": ["top-20 SKUs"],
            "ambiguities": [],
            "confidence": 0.8,
            "entities": {
                "brand": None,
                "platform": "Blinkit",
                "window_text": None,
                "price_delta_pct": -0.1,
                "budget": None,
                "metric": None,
            },
        },
    ),
    (
        "Build a promo plan with a budget of 5000 on Zepto for demo brand next month.",
        {
            "task_type": "optimization",
            "domain": "demand",
            "output_wanted": "plan",
            "constraints": [],
            "ambiguities": [],
            "confidence": 0.85,
            "entities": {
                "brand": "demo",
                "platform": "Zepto",
                "window_text": "next month",
                "price_delta_pct": None,
                "budget": 5000,
                "metric": None,
            },
        },
    ),
    (
        "Can you show me the most recent pipeline runs?",
        {
            "task_type": "ops_status",
            "domain": "demand",
            "output_wanted": "answer",
            "constraints": [],
            "ambiguities": [],
            "confidence": 0.85,
            "entities": {
                "brand": None,
                "platform": None,
                "window_text": None,
                "price_delta_pct": None,
                "budget": None,
                "metric": None,
            },
        },
    ),
    (
        "What happens to our volumes if we make everything 10% cheaper on blinkit for the demo brand?",
        {
            "task_type": "scenario",
            "domain": "demand",
            "output_wanted": "answer",
            "constraints": [],
            "ambiguities": [],
            "confidence": 0.8,
            "entities": {
                "brand": "demo",
                "platform": "blinkit",
                "window_text": None,
                "price_delta_pct": -0.1,
                "budget": None,
                "metric": None,
            },
        },
    ),
]

_SYSTEM_RULES = (
    "You are the intent classifier for GobbleCube's Demand Intelligence system. "
    "Turn the ask below into JSON matching the schema exactly — no prose, no markdown fence. "
    "Extract entities as the user said them: do not resolve dates, do not canonicalize brand or "
    "platform names — pass through the raw text (window_text verbatim, brand/platform as written). "
    "Resolution happens elsewhere, in code, not by you. "
    "If the ask has nothing to do with demand/pricing/elasticity/promos, set domain to null — "
    "do not guess a domain for an out-of-domain ask."
)

# Deterministic mapping for "the LLM told us this ask isn't in our domain at
# all" (signalled by domain: null) — code decides the resulting NormalizedAsk,
# never the model's own task_type/confidence/entities for an ask it just said
# it can't place (spec §5.3: "entity resolution happens in code"; same
# principle extended to out-of-domain routing).
_OUT_OF_DOMAIN_AMBIGUITY = "ask appears unrelated to Demand Intelligence (no domain detected)"
_OUT_OF_DOMAIN_CONFIDENCE = 0.0


def _render_stage2_prompt(text: str) -> str:
    examples = "\n\n".join(
        f'Ask: "{ask}"\nJSON: {json.dumps(expected, separators=(",", ":"))}' for ask, expected in _FEW_SHOT
    )
    return f"{_SYSTEM_RULES}\n\n{examples}\n\n" f'Ask: "{text}"\nJSON:'


def _resolve_entities(raw: dict[str, Any], text: str, reference_date: date) -> tuple[Entities, list[str]]:
    ambiguities: list[str] = []

    brand_raw = raw.get("brand")
    brand = resolve_brand(brand_raw)
    if brand is None:
        # The LLM sometimes drops the brand slot for descriptive phrasing
        # ("the demo brand", "our demo brand", "for demo") rather than a bare
        # proper noun — fall back to a deterministic scan of the raw ask text
        # (spec §5.3: entity resolution happens in code, not the LLM). This
        # only ever adds a match; it never overrides an LLM-resolved brand.
        brand = scan_brand(text)
    if brand_raw and resolve_brand(brand_raw) is None and brand is None:
        ambiguities.append(f"unknown brand: {brand_raw!r}")

    platform_raw = raw.get("platform")
    platform = resolve_platform(platform_raw)
    if platform_raw and platform is None:
        ambiguities.append(f"unknown platform: {platform_raw!r}")

    window_text = raw.get("window_text")
    window = resolve_window(window_text, reference_date)
    if window_text and window is None:
        ambiguities.append(f"could not resolve time window: {window_text!r}")

    entities = Entities(
        brand=brand,
        platform=platform,
        window=window,
        price_delta_pct=raw.get("price_delta_pct"),
        budget=raw.get("budget"),
        metric=raw.get("metric"),
    )
    return entities, ambiguities


def _classify_llm(session: Session, tenant_id: str | UUID, text: str, reference_date: date) -> NormalizedAsk:
    gateway = get_gateway()
    prompt = _render_stage2_prompt(text)
    raw = gateway.complete_structured(
        session,
        tenant_id,
        call_class="classify",
        prompt=prompt,
        schema=_LLM_EXTRACTION_SCHEMA,
        model=resolve_model("classify"),
        max_tokens=300,
    )

    if raw.get("domain") is None:
        return NormalizedAsk(
            task_type="unknown",
            domain="",
            entities=Entities(),
            output_wanted="",
            constraints=[],
            ambiguities=[_OUT_OF_DOMAIN_AMBIGUITY],
            confidence=_OUT_OF_DOMAIN_CONFIDENCE,
        )

    entities, resolution_ambiguities = _resolve_entities(raw.get("entities") or {}, text, reference_date)
    ambiguities = list(raw.get("ambiguities", [])) + resolution_ambiguities

    return NormalizedAsk(
        task_type=raw["task_type"],
        domain=raw["domain"],
        entities=entities,
        output_wanted=raw.get("output_wanted") or "",
        constraints=list(raw.get("constraints", [])),
        ambiguities=ambiguities,
        confidence=float(raw["confidence"]),
    )


def normalized_hash(ask: NormalizedAsk) -> str:
    """Excludes entity *values*, includes entity *keys* + task_type + domain
    (spec §5.3) — so "elasticity for demo, Jan-Jun" and "elasticity for
    nivea, Feb-May" share a plan template.

    Also includes `output_wanted` and the *shape* (count, not text) of
    `constraints` — a compound ask that changes what's being produced (e.g.
    "run elasticity for demo on blinkit for Jan-Feb **and promote the model
    to champion**") reports the same task_type/domain/entity_keys as the
    plain ask it's built from, but a different `output_wanted` ("report" vs
    "surface", confirmed against the live stage-2 classifier) — without this,
    the compound ask silently binds to the plain ask's cached plan template
    (cache poisoning: the promotion never happens, with no signal). Hashing
    `output_wanted` verbatim and `constraints` only by count keeps the same
    values-blind, shape-only philosophy as `entity_keys` above — paraphrases
    that don't change what's asked for still share a template."""
    entity_keys = sorted(key for key, value in ask.entities.model_dump(exclude_none=True).items())
    return content_hash(
        {
            "task_type": ask.task_type,
            "domain": ask.domain,
            "entity_keys": entity_keys,
            "output_wanted": ask.output_wanted,
            "constraint_count": len(ask.constraints),
        }
    )


class ClassifyResult(BaseModel):
    normalized: NormalizedAsk
    stage: Literal["rules", "llm"]
    normalized_hash: str
    latency_ms: int


def classify_ask(
    session: Session,
    tenant_id: str | UUID,
    text: str,
    *,
    reference_date: date | None = None,
) -> ClassifyResult:
    resolved_reference_date = reference_date or date.today()
    start = time.monotonic()

    stage0_result = classify_stage0(text, resolved_reference_date)
    if stage0_result is not None:
        # No compound-directive guard needed here: every stage-0 rule is
        # anchored end-to-end (core/classify/rules.py), so an ask with a
        # trailing directive its bounded shape doesn't account for already
        # failed to match and never reaches this branch — applying the guard
        # would be a no-op by construction (see evals/test_classifier.py's
        # stage-0 anchoring regression tests).
        latency_ms = int((time.monotonic() - start) * 1000)
        return ClassifyResult(
            normalized=stage0_result,
            stage="rules",
            normalized_hash=normalized_hash(stage0_result),
            latency_ms=latency_ms,
        )

    ask = _classify_llm(session, tenant_id, text, resolved_reference_date)
    ask = _apply_compound_directive_guard(ask, text)
    latency_ms = int((time.monotonic() - start) * 1000)
    return ClassifyResult(normalized=ask, stage="llm", normalized_hash=normalized_hash(ask), latency_ms=latency_ms)


def _apply_compound_directive_guard(ask: NormalizedAsk, raw_text: str) -> NormalizedAsk:
    """Deterministic backstop for stage 2: the LLM's own signal for a
    compound ask's extra directive (`output_wanted` differing from the plain
    shape) is not guaranteed deterministic between calls — see
    `core.classify.compound` for the confirmed repro. Forcing a matching
    constraint token here means `normalized_hash` (which counts constraints)
    separates a compound ask from the plain one it's built from regardless of
    what stage 2 happened to emit."""
    tokens = compound_directive_constraints(raw_text)
    if not tokens:
        return ask
    merged = list(ask.constraints)
    for token in tokens:
        if token not in merged:
            merged.append(token)
    return ask.model_copy(update={"constraints": merged})
