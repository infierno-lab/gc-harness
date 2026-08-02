"""Entity resolution — deterministic, in code (spec §5.3: "Entity resolution
happens in code"). The classifier (stage 0 or stage 2) only ever supplies raw
mentions (a brand/platform string as written, a natural-language time phrase);
everything here turns those into canonical values or flags them as ambiguous.
Nothing in this module ever calls a model.
"""

import re
from datetime import date

from core.classify.schema import WindowSpec

# PoC-scope allowlists (spec §5.3: "fuzzy match against tenant-scoped
# dimension tables" — a single demo tenant with a fixed catalog stands in for
# that here; unknown values fall through to `ambiguities`, never silently
# guessed).
KNOWN_BRANDS = {"demo"}
KNOWN_PLATFORMS = {"blinkit", "zepto", "swiggy"}

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

_ISO_RANGE_RE = re.compile(r"\b(\d{4}-\d{2})\s*(?:-|–|to|through)\s*(\d{4}-\d{2})\b")
_MONTH_RANGE_RE = re.compile(
    rf"\b({_MONTH_ALT})\.?\s*(?:-|–|to|through)\s*({_MONTH_ALT})\.?(?:\s+(\d{{4}}))?\b",
    re.IGNORECASE,
)
_HALF_RE = re.compile(r"\bH([12])\b(?:\s+(\d{4}))?", re.IGNORECASE)
_RELATIVE_QUARTER_RE = re.compile(r"\b(last|this|next)\s+quarter\b", re.IGNORECASE)
_QUARTER_RE = re.compile(r"\bQ([1-4])\b(?:\s+(\d{4}))?", re.IGNORECASE)

_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_NEGATIVE_WORDS = ("cut", "reduce", "lower", "drop", "discount", "decrease")
_POSITIVE_WORDS = ("increase", "raise", "hike")
_BUDGET_RE = re.compile(r"budget[^\d]{0,10}([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_MONEY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")


def _quarter_window(year: int, quarter: int) -> WindowSpec:
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    return WindowSpec(**{"from": f"{year:04d}-{start_month:02d}", "to": f"{year:04d}-{end_month:02d}"})


def resolve_window(raw: str | None, reference_date: date) -> WindowSpec | None:
    """Resolves a raw natural-language (or explicit ISO) window mention
    against `reference_date`, e.g. "last quarter" given a 2026-07-31
    reference resolves to {from: 2026-04, to: 2026-06} (2026-Q2). Returns
    None when nothing recognizable is present — the caller decides whether
    that's an ambiguity worth flagging."""
    if not raw:
        return None

    match = _ISO_RANGE_RE.search(raw)
    if match:
        return WindowSpec(**{"from": match.group(1), "to": match.group(2)})

    match = _MONTH_RANGE_RE.search(raw)
    if match:
        start_name, end_name, year_str = match.groups()
        year = int(year_str) if year_str else reference_date.year
        start_month = _MONTHS[start_name.lower()]
        end_month = _MONTHS[end_name.lower()]
        return WindowSpec(**{"from": f"{year:04d}-{start_month:02d}", "to": f"{year:04d}-{end_month:02d}"})

    match = _HALF_RE.search(raw)
    if match:
        half, year_str = match.groups()
        year = int(year_str) if year_str else reference_date.year
        if half == "1":
            return WindowSpec(**{"from": f"{year:04d}-01", "to": f"{year:04d}-06"})
        return WindowSpec(**{"from": f"{year:04d}-07", "to": f"{year:04d}-12"})

    match = _RELATIVE_QUARTER_RE.search(raw)
    if match:
        which = match.group(1).lower()
        current_quarter = (reference_date.month - 1) // 3 + 1
        year = reference_date.year
        if which == "last":
            quarter = current_quarter - 1
            if quarter == 0:
                quarter, year = 4, year - 1
        elif which == "next":
            quarter = current_quarter + 1
            if quarter == 5:
                quarter, year = 1, year + 1
        else:
            quarter = current_quarter
        return _quarter_window(year, quarter)

    match = _QUARTER_RE.search(raw)
    if match:
        quarter_str, year_str = match.groups()
        year = int(year_str) if year_str else reference_date.year
        return _quarter_window(year, int(quarter_str))

    return None


def resolve_brand(raw: str | None) -> str | None:
    """Canonical brand slug, or None if `raw` doesn't match the allowlist
    (including when `raw` is None)."""
    if not raw:
        return None
    normalized = raw.strip().lower()
    return normalized if normalized in KNOWN_BRANDS else None


def resolve_platform(raw: str | None) -> str | None:
    """Canonical platform slug, or None if `raw` doesn't match the allowlist
    (including when `raw` is None)."""
    if not raw:
        return None
    normalized = raw.strip().lower()
    return normalized if normalized in KNOWN_PLATFORMS else None


def scan_brand(text: str) -> str | None:
    """Best-effort raw brand mention lifted straight from free text (stage 0,
    no LLM available to extract a candidate string first)."""
    lowered = text.lower()
    for brand in KNOWN_BRANDS:
        if brand in lowered:
            return brand
    return None


def scan_platform(text: str) -> str | None:
    """Best-effort raw platform mention lifted straight from free text."""
    lowered = text.lower()
    for platform in KNOWN_PLATFORMS:
        if platform in lowered:
            return platform
    return None


def scan_price_delta_pct(text: str) -> float | None:
    match = _PCT_RE.search(text)
    if not match:
        return None
    value = float(match.group(1)) / 100.0
    lowered = text.lower()
    if any(word in lowered for word in _NEGATIVE_WORDS):
        return -value
    if any(word in lowered for word in _POSITIVE_WORDS):
        return value
    return value


def scan_budget(text: str) -> float | None:
    match = _BUDGET_RE.search(text) or _MONEY_RE.search(text)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))
