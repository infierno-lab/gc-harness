"""Bifrost transport (spec §3.1 L0) — Bifrost is a model-agnostic gateway
exposed as an OpenAI-compatible unified API, NOT an Anthropic-compatible
proxy: `POST {base_url}/chat/completions` with an OpenAI chat body, provider-
prefixed model names (e.g. "anthropic/claude-sonnet-5"). This is the real
Bifrost integration; `AnthropicTransport`'s base_url override is a separate,
unrelated knob for pointing the SDK at a custom Anthropic-compatible endpoint.
"""

import os
import time
from typing import Any

import httpx

from core.gateway.errors import GatewayTransportError
from core.gateway.transport import TransportResult


class BifrostTransport:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, *, provider: str | None = None) -> None:
        resolved_base = base_url or os.environ.get("GC_BIFROST_BASE_URL")
        if not resolved_base:
            raise GatewayTransportError("GC_BIFROST_BASE_URL is not set; cannot use the Bifrost transport")
        resolved_key = api_key or os.environ.get("GC_BIFROST_API_KEY")
        if not resolved_key:
            raise GatewayTransportError("GC_BIFROST_API_KEY is not set; cannot use the Bifrost transport")

        self._base_url = resolved_base.rstrip("/")
        self._api_key = resolved_key
        # The provider prefix applied to any model id that doesn't already
        # carry one (spec: "anthropic/claude-sonnet-5") — the model-agnostic
        # knob; a caller passing e.g. "openai/gpt-5" passes through untouched.
        # Absent GC_BIFROST_PROVIDER defaults to "anthropic"; an EXPLICIT empty
        # string (constructor arg or env var) disables prefixing entirely —
        # needed for providers like Moonshot that expect bare model ids
        # ("kimi-k2.5", not "anthropic/kimi-k2.5"). `is not None` (not a
        # truthiness check) is what makes an explicit "" distinguishable from
        # "not passed".
        self._provider = provider if provider is not None else os.environ.get("GC_BIFROST_PROVIDER", "anthropic")

    def _qualify_model(self, model: str) -> str:
        if "/" in model or not self._provider:
            return model
        return f"{self._provider}/{model}"

    def complete(self, prompt: str, *, model: str, max_tokens: int, timeout_s: float) -> TransportResult:
        body = {
            "model": self._qualify_model(model),
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        start = time.monotonic()
        try:
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=timeout_s,
            )
        except httpx.HTTPError as exc:
            raise GatewayTransportError(f"Bifrost request failed: {exc}") from exc
        latency_ms = int((time.monotonic() - start) * 1000)

        if response.status_code != 200:
            raise GatewayTransportError(f"Bifrost returned HTTP {response.status_code}: {response.text[:2000]}")

        try:
            payload: dict[str, Any] = response.json()
            text = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise GatewayTransportError(f"Bifrost returned an unexpected body: {response.text[:500]!r}") from exc

        usage = payload.get("usage") or {}
        cache_read_tokens = None
        prompt_tokens_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_tokens_details, dict):
            cache_read_tokens = prompt_tokens_details.get("cached_tokens")

        return TransportResult(
            text=text,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cache_read_tokens=cache_read_tokens,
            latency_ms=latency_ms,
        )
