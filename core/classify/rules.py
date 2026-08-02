"""Stage 0 (spec §5.3): regex rules for unambiguous shapes, no LLM. Each rule
maps a matched prefix straight to a NormalizedAsk; entities are still resolved
in code from whatever the raw text contains (spec's "entity resolution
happens in code" applies identically whether the shape came from a rule or
the LLM).

Every pattern is anchored end-to-end (`^...$`) with bounded entity slots — a
rule may only claim an ask that is FULLY described by its shape. This is a
correctness requirement, not cosmetic: an unanchored/greedy pattern (e.g. the
old `^run\\s+elasticity\\b` with no end anchor) would swallow a *compound*
ask like "run elasticity for demo on blinkit for Jan-Feb and promote the
model to champion" into the plain pipeline shape, silently dropping the
trailing directive — and because `normalized_hash` only sees task_type/
domain/entity-keys, the truncated ask would then bind to whatever plan
template the plain shape already cached. Any extra clause the bounded slots
below don't account for (`" and ..."`, `" then ..."`, etc.) simply fails the
`$` anchor and falls through to stage 2, where the LLM sees the whole ask."""

import re
from dataclasses import dataclass
from datetime import date

from core.classify.entities import (
    _MONTH_ALT,
    resolve_brand,
    resolve_platform,
    resolve_window,
    scan_brand,
    scan_budget,
    scan_platform,
    scan_price_delta_pct,
)
from core.classify.schema import Entities, NormalizedAsk, TaskType


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str]
    task_type: TaskType
    domain: str
    output_wanted: str
    metric: str | None = None


# A single free-text entity token (brand/platform names in the PoC catalog are
# single words, e.g. "demo", "blinkit" — see entities.KNOWN_BRANDS/PLATFORMS).
# Bounded to word characters so it can never swallow a following literal like
# " on " or " for " that the rule requires next.
_ENTITY_WORD = r"[A-Za-z][\w-]*"
# Up to 3 words, for brand mentions that may be more than one token.
_ENTITY_PHRASE = _ENTITY_WORD + r"(?:\s+" + _ENTITY_WORD + r"){0,2}"

# The exact window shapes `core.classify.entities.resolve_window` knows how to
# resolve (ISO range, month range, half, quarter, relative quarter) — bounded
# alternation, not a generic "any words" slot, so a rule can't be satisfied by
# an unrelated trailing clause that merely happens to contain word characters.
_WINDOW_SHAPE = "".join(
    [
        r"(?:\d{4}-\d{2}\s*(?:-|–|to|through)\s*\d{4}-\d{2}",
        r"|(?:", _MONTH_ALT, r")\.?\s*(?:-|–|to|through)\s*(?:", _MONTH_ALT, r")\.?(?:\s+\d{4})?",
        r"|H[12](?:\s+\d{4})?",
        r"|Q[1-4](?:\s+\d{4})?",
        r"|(?:last|this|next)\s+quarter)",
    ]
)

# Optional trailing punctuation a fully-described ask may still end with.
_TRAILING_PUNCT = r"\s*[.?!]?\s*$"

_RULES: list[_Rule] = [
    _Rule(
        pattern=re.compile(
            r"^\s*run\s+elasticity\s+for\s+" + _ENTITY_PHRASE
            + r"\s+on\s+" + _ENTITY_WORD
            + r"\s+for\s+" + _WINDOW_SHAPE
            + _TRAILING_PUNCT,
            re.IGNORECASE,
        ),
        task_type="pipeline",
        domain="demand",
        output_wanted="surface",
    ),
    _Rule(
        pattern=re.compile(
            r"^\s*how\s+many\b.*?\bloss\w*\b"
            r"(?:\s+" + _WINDOW_SHAPE + r")?"
            r"(?:\s+for\s+" + _ENTITY_WORD + r"\s+on\s+" + _ENTITY_WORD + r")?"
            + _TRAILING_PUNCT,
            re.IGNORECASE,
        ),
        task_type="analytic_query",
        domain="demand",
        output_wanted="answer",
        metric="loss_making_skus",
    ),
    _Rule(
        pattern=re.compile(
            r"^\s*show\s+(?:me\s+)?recent\s+runs(?:\s+please)?" + _TRAILING_PUNCT,
            re.IGNORECASE,
        ),
        task_type="ops_status",
        domain="demand",
        output_wanted="answer",
    ),
]


def _build_entities_from_text(text: str, reference_date: date, *, metric: str | None) -> tuple[Entities, list[str]]:
    ambiguities: list[str] = []

    brand_raw = scan_brand(text)
    brand = resolve_brand(brand_raw)

    platform_raw = scan_platform(text)
    platform = resolve_platform(platform_raw)

    window = resolve_window(text, reference_date)

    entities = Entities(
        brand=brand,
        platform=platform,
        window=window,
        price_delta_pct=scan_price_delta_pct(text),
        budget=scan_budget(text),
        metric=metric,
    )
    return entities, ambiguities


def classify_stage0(text: str, reference_date: date) -> NormalizedAsk | None:
    """A direct rules-ladder hit, or None to fall through to stage 2."""
    stripped = text.strip()
    for rule in _RULES:
        if rule.pattern.match(stripped):
            entities, ambiguities = _build_entities_from_text(stripped, reference_date, metric=rule.metric)
            return NormalizedAsk(
                task_type=rule.task_type,
                domain=rule.domain,
                entities=entities,
                output_wanted=rule.output_wanted,
                constraints=[],
                ambiguities=ambiguities,
                confidence=0.95,
            )
    return None
