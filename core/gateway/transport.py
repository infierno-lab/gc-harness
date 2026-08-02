"""The `Transport` protocol (spec §3.3 L0) — the shape every model transport
must satisfy. `Gateway` (client.py) is the only thing that calls `.complete`;
nothing outside `core/gateway/` ever imports a model SDK or spawns `claude`.
"""

from typing import Protocol

from pydantic import BaseModel


class TransportResult(BaseModel):
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    latency_ms: int | None = None


class Transport(Protocol):
    def complete(
        self, prompt: str, *, model: str, max_tokens: int, timeout_s: float
    ) -> TransportResult: ...
