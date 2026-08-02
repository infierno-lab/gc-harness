"""Canonical hashing for everything recorded as a content hash: plan IRs, node
input/output edges, prompt/response pairs.

Canonicalization rules (every recorded hash depends on these — change them and
every previously-recorded hash stops matching):

- dict key order, and BaseModel-vs-its-own-dict-dump, never affect the hash.
- lists, tuples, sets, and frozensets of equal *content* hash the same; sets
  are hashed as their elements sorted by canonical JSON text, since Python
  set iteration order is not itself deterministic.
- BaseModel instances are canonicalized wherever they appear — at the top
  level or nested inside a dict/list/etc — via `model_dump(mode="json")`.
- numeric identity rule: any float that is mathematically integral (e.g.
  `1.0`) normalizes to the equivalent int (`1`) before hashing, so int and
  float representations of the same value hash identically. Non-integral
  floats hash as Python's own JSON float representation.
"""

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def _sort_key(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return _to_jsonable(obj.model_dump(mode="json", by_alias=True))
    if isinstance(obj, dict):
        return {str(key): _to_jsonable(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(value) for value in obj]
    if isinstance(obj, (set, frozenset)):
        items = [_to_jsonable(value) for value in obj]
        return sorted(items, key=_sort_key)
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    return obj


def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialization: sorted keys, no incidental whitespace —
    stable regardless of the input dict's insertion order."""
    return json.dumps(_to_jsonable(obj), sort_keys=True, separators=(",", ":"), default=str)


def content_hash(obj: Any) -> str:
    """Used everywhere a hash is recorded: plan IRs, node input/output edges, prompts, responses."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
