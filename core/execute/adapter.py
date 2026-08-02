"""Adapter runtime contract (spec §4.5) — every block entrypoint is a plain
python callable `run(params, inputs, ctx) -> dict[str, Envelope]`.

Envelope summaries are computed by code inside adapters, never free text from
outside. `BlockContext` carries the per-run scratch directory and a tiny
JSON-file-backed storage helper so blocks exchange references, never payloads.
"""

import importlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.catalog.hashing import canonical_json, content_hash
from core.execute.envelope import Envelope

AdapterFn = Callable[[dict[str, Any], dict[str, Envelope], "BlockContext"], dict[str, Envelope]]


class Storage:
    """JSON-file-backed content store scoped to a run directory. Refs are opaque
    strings (`res_<hash-prefix>`) derived from the payload's content hash —
    identical payloads reuse the same ref."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    def put(self, name: str, payload: Any) -> str:
        ref = f"res_{content_hash(payload)[:16]}"
        path = self.run_dir / f"{ref}__{name}.json"
        if not path.exists():
            path.write_text(canonical_json(payload))
        return ref

    def get(self, ref: str) -> Any:
        matches = sorted(self.run_dir.glob(f"{ref}__*.json"))
        if not matches:
            raise FileNotFoundError(f"no stored payload for ref {ref!r} under {self.run_dir}")
        return json.loads(matches[0].read_text())


@dataclass
class BlockContext:
    tenant_id: str
    run_id: str
    node_id: str
    block_version: str  # "name@version", for provenance
    run_dir: Path
    storage: Storage
    # The human/service actor `execute_plan` is running as (spec §8: mutating
    # actions carry a named actor). Optional/defaulted so direct BlockContext
    # construction elsewhere (tests, future callers) doesn't break.
    actor: str | None = None


def resolve_entrypoint(entrypoint: str) -> AdapterFn:
    """Load a block's `run` callable from its `module.path:callable` entrypoint string."""
    module_name, sep, func_name = entrypoint.partition(":")
    if not sep or not module_name or not func_name:
        raise ValueError(f"invalid entrypoint {entrypoint!r}, expected 'module.path:callable'")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ImportError(f"entrypoint module not found: {module_name!r} (from {entrypoint!r})") from exc
    try:
        return getattr(module, func_name)
    except AttributeError as exc:
        raise AttributeError(
            f"entrypoint callable {func_name!r} not found in {module_name!r} (from {entrypoint!r})"
        ) from exc


def provenance_hash(params: dict[str, Any], inputs: dict[str, Envelope]) -> str:
    """Single combined hash for an envelope's `provenance.inputs_hash` — covers
    both the block's params and the content of every input envelope it consumed."""
    payload = {
        "params": params,
        "inputs": {port: env.model_dump(mode="json", by_alias=True) for port, env in inputs.items()},
    }
    return content_hash(payload)


def build_envelope(
    *,
    block_version: str,
    params: dict[str, Any],
    inputs: dict[str, Envelope],
    summary: str,
    cols: list[str],
    sample: list[Any],
    ref: str,
    rows: int | None,
) -> Envelope:
    """Shared envelope constructor (spec §4.5) — keeps summary/schema/provenance
    shaping identical across every adapter."""
    return Envelope(
        summary=summary,
        schema={"cols": cols},
        sample=sample,
        ref=ref,
        full_size={"rows": rows},
        provenance={"block": block_version, "inputs_hash": provenance_hash(params, inputs)},
    )
