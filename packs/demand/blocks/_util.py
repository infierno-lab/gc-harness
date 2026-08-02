"""Deterministic demo-math helpers shared by the Demand pack blocks.

No wall-clock, no unseeded randomness: every number here is a pure function of
its inputs so that re-running the same plan produces byte-identical outputs.
"""

import hashlib
import random
from datetime import date, timedelta


def seed_for(*parts: str) -> int:
    """A stable integer seed derived from the given strings, independent of
    PYTHONHASHSEED (unlike the builtin `hash()`)."""
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(digest[:8], 16)


def rng_for(*parts: str) -> random.Random:
    return random.Random(seed_for(*parts))


def weeks_between(from_month: str, to_month: str) -> list[str]:
    """Weekly (Monday-aligned) buckets from the first of `from_month` to the
    first of `to_month`, both `YYYY-MM`."""
    start = date.fromisoformat(f"{from_month}-01")
    end = date.fromisoformat(f"{to_month}-01")
    if end < start:
        raise ValueError(f"window.to ({to_month}) precedes window.from ({from_month})")
    weeks = []
    cur = start
    while cur <= end:
        weeks.append(cur.isoformat())
        cur += timedelta(weeks=1)
    return weeks


def skus_for_brand(brand: str, count: int) -> list[str]:
    return [f"SKU-{brand.upper()}-{i:03d}" for i in range(1, count + 1)]
