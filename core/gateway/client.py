"""The Gateway (spec §3.3 L0, §5.5) — the ONE module every structured-output
LLM call in this repo goes through. Nothing outside `core/gateway/` imports a
model SDK or spawns the `claude` subprocess; every attempt is audited via an
`LlmCall` row (fail-open observation would violate spec invariant #6 in
spirit — here it's simpler: audit is unconditional, on every attempt).
"""

import json
import os
import re
import time
from typing import Any
from uuid import UUID

import jsonschema
from sqlalchemy.orm import Session

from core.catalog.hashing import content_hash
from core.db.models import LlmCall
from core.gateway.bifrost_transport import BifrostTransport
from core.gateway.cli_transport import ClaudeCliTransport
from core.gateway.errors import GatewayError, GatewayTransportError
from core.gateway.sdk_transport import AnthropicTransport
from core.gateway.timeouts import resolve_timeout
from core.gateway.transport import Transport, TransportResult

__all__ = [
    "AnthropicTransport",
    "BifrostTransport",
    "ClaudeCliTransport",
    "Gateway",
    "GatewayError",
    "GatewayTransportError",
    "Transport",
    "TransportResult",
    "get_gateway",
]

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    return match.group(1).strip() if match else stripped


class Gateway:
    def __init__(self, transport: Transport, *, timeout_s: float | None = None) -> None:
        self._transport = transport
        # None (the default) means "resolve per call_class via
        # core.gateway.timeouts.resolve_timeout" — an explicit value here
        # overrides that resolution for every call this Gateway makes
        # (mainly a test/back-compat hook; no production call site sets it).
        self._timeout_s_override = timeout_s

    def complete_structured(
        self,
        session: Session,
        tenant_id: str | UUID,
        *,
        call_class: str,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        max_tokens: int = 1500,
        retries: int = 1,
    ) -> dict[str, Any]:
        """Calls the transport, strips markdown fences, parses JSON, and
        validates against `schema`. On a malformed/invalid response, retries
        once with the error appended to the prompt. Still bad -> `GatewayError`
        (fail closed, no silent fallback). Every attempt — success or failure
        — is recorded as an `LlmCall` row before this method returns or raises.
        """
        attempt_prompt = prompt
        last_error: str | None = None
        timeout_s = self._timeout_s_override if self._timeout_s_override is not None else resolve_timeout(call_class)

        for attempt in range(retries + 1):
            prompt_hash = content_hash(attempt_prompt)
            response_text: str | None = None
            input_tokens = output_tokens = cache_read_tokens = latency_ms = None

            try:
                result = self._transport.complete(
                    attempt_prompt, model=model, max_tokens=max_tokens, timeout_s=timeout_s
                )
                response_text = result.text
                input_tokens = result.input_tokens
                output_tokens = result.output_tokens
                cache_read_tokens = result.cache_read_tokens
                latency_ms = result.latency_ms

                parsed = json.loads(_strip_fences(response_text))
                jsonschema.validate(parsed, schema)
            except Exception as exc:
                last_error = str(exc)
                response_hash = content_hash(response_text) if response_text is not None else None
                self._record_call(
                    session,
                    tenant_id,
                    call_class=call_class,
                    model=model,
                    prompt_hash=prompt_hash,
                    response_hash=response_hash,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    latency_ms=latency_ms,
                )
                if attempt < retries:
                    attempt_prompt = (
                        f"{prompt}\n\n"
                        f"Your previous reply was invalid: {last_error}\n"
                        "Reply with ONLY valid JSON matching the schema — no prose, no markdown fence."
                    )
                    continue
                raise GatewayError(
                    f"LLM did not produce valid structured output after {retries + 1} attempt(s): {last_error}"
                ) from exc
            else:
                self._record_call(
                    session,
                    tenant_id,
                    call_class=call_class,
                    model=model,
                    prompt_hash=prompt_hash,
                    response_hash=content_hash(response_text),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    latency_ms=latency_ms,
                )
                return parsed

        raise GatewayError("unreachable")  # pragma: no cover

    def _record_call(
        self,
        session: Session,
        tenant_id: str | UUID,
        *,
        call_class: str,
        model: str,
        prompt_hash: str,
        response_hash: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None,
        latency_ms: int | None,
    ) -> None:
        tenant_uuid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
        session.add(
            LlmCall(
                tenant_id=tenant_uuid,
                call_class=call_class,
                model=model,
                prompt_hash=prompt_hash,
                response_hash=response_hash,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                cache_read_tokens=cache_read_tokens,
            )
        )
        session.flush()


_gateway: Gateway | None = None


def get_gateway() -> Gateway:
    """Singleton, transport chosen by env: `GC_LLM_TRANSPORT`
    ("bifrost"|"sdk"|"cli") overrides if set; otherwise, in order: "bifrost"
    if `GC_BIFROST_BASE_URL` is set, else "sdk" if `ANTHROPIC_API_KEY` is set,
    else "cli" (the authenticated `claude` CLI, dev default)."""
    global _gateway
    if _gateway is None:
        choice = os.environ.get("GC_LLM_TRANSPORT")
        if choice is None:
            if os.environ.get("GC_BIFROST_BASE_URL"):
                choice = "bifrost"
            elif os.environ.get("ANTHROPIC_API_KEY"):
                choice = "sdk"
            else:
                choice = "cli"
        if choice == "bifrost":
            transport: Transport = BifrostTransport()
        elif choice == "sdk":
            transport = AnthropicTransport()
        elif choice == "cli":
            transport = ClaudeCliTransport()
        else:
            raise GatewayError(f"unknown GC_LLM_TRANSPORT: {choice!r}")
        _gateway = Gateway(transport)
    return _gateway
