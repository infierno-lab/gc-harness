"""Anthropic SDK transport — used automatically when ANTHROPIC_API_KEY is set
(spec §3.3). Points at the Anthropic API directly, or at Bifrost (the L0
gateway of record, spec §3.1) when GC_LLM_BASE_URL / ANTHROPIC_BASE_URL is
set — Bifrost exposes an Anthropic-compatible surface, so the SDK is the
client either way.
"""

import os
import time

import anthropic

from core.gateway.errors import GatewayTransportError
from core.gateway.transport import TransportResult


class AnthropicTransport:
    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise GatewayTransportError("ANTHROPIC_API_KEY is not set; cannot use the SDK transport")
        resolved_base = (
            base_url
            or os.environ.get("GC_LLM_BASE_URL")
            or os.environ.get("ANTHROPIC_BASE_URL")
        )
        kwargs: dict = {"api_key": resolved_key}
        if resolved_base:
            kwargs["base_url"] = resolved_base
        self._client = anthropic.Anthropic(**kwargs)

    def complete(self, prompt: str, *, model: str, max_tokens: int, timeout_s: float) -> TransportResult:
        start = time.monotonic()
        try:
            message = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                timeout=timeout_s,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # anthropic raises its own APIError hierarchy
            raise GatewayTransportError(f"anthropic SDK call failed: {exc}") from exc
        latency_ms = int((time.monotonic() - start) * 1000)

        text = "".join(block.text for block in message.content if getattr(block, "type", None) == "text")
        usage = message.usage
        return TransportResult(
            text=text,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", None),
            latency_ms=latency_ms,
        )
