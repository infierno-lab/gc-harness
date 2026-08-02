"""`claude` CLI transport — used when no ANTHROPIC_API_KEY is configured
(spec §3.3, dev/PoC default). Verified live envelope shape (2.1.212):

    {"result": "...", "is_error": false, "duration_ms": 1234,
     "usage": {"input_tokens": N, "output_tokens": N, "cache_read_input_tokens": N, ...}}

The model's text lives in `.result` and may be wrapped in a ```json fence —
stripping that is `Gateway`'s job (client.py), not the transport's.
"""

import json
import subprocess
import tempfile
import time
from typing import Any

from core.gateway.errors import GatewayTransportError
from core.gateway.transport import TransportResult

# Keeps the CLI's own MCP surface empty and skips MCP discovery overhead —
# verified supported live; if a future CLI drops it, _run falls back to
# calling without these args (see _looks_like_unsupported_flag).
_STRICT_MCP_ARGS = ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']


def _looks_like_unsupported_flag(stderr: str) -> bool:
    lowered = stderr.lower()
    return "unknown option" in lowered or "unrecognized option" in lowered or "unknown argument" in lowered


class ClaudeCliTransport:
    """Subprocess transport: `claude -p <prompt> --model <model> --output-format json`.

    The prompt is passed as a single argv element (no shell interpolation);
    `cwd` is a neutral temp directory so the CLI never ingests this repo's
    CLAUDE.md/git context for what should be a stateless model call.
    """

    def __init__(self, claude_bin: str = "claude") -> None:
        self._claude_bin = claude_bin

    def complete(self, prompt: str, *, model: str, max_tokens: int, timeout_s: float) -> TransportResult:
        # max_tokens: the CLI exposes no equivalent flag (verified: `claude
        # --help` has no --max-tokens for -p mode) — accepted for protocol
        # conformance and enforced only by the SDK transport.
        return self._run(prompt, model=model, timeout_s=timeout_s, extra_args=_STRICT_MCP_ARGS)

    def _run(
        self, prompt: str, *, model: str, timeout_s: float, extra_args: list[str]
    ) -> TransportResult:
        cmd = [self._claude_bin, "-p", prompt, "--model", model, "--output-format", "json", *extra_args]
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=tempfile.gettempdir(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GatewayTransportError(f"claude CLI timed out after {timeout_s}s") from exc
        except OSError as exc:
            raise GatewayTransportError(f"failed to launch claude CLI: {exc}") from exc

        wall_latency_ms = int((time.monotonic() - start) * 1000)

        if proc.returncode != 0:
            if extra_args and _looks_like_unsupported_flag(proc.stderr):
                return self._run(prompt, model=model, timeout_s=timeout_s, extra_args=[])
            detail = (proc.stderr or proc.stdout or "").strip()[:2000]
            raise GatewayTransportError(f"claude CLI exited {proc.returncode}: {detail}")

        try:
            envelope: dict[str, Any] = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise GatewayTransportError(
                f"claude CLI returned non-JSON stdout: {proc.stdout[:500]!r}"
            ) from exc

        if envelope.get("is_error"):
            raise GatewayTransportError(f"claude CLI reported an error: {envelope.get('result')!r}")

        usage = envelope.get("usage") or {}
        return TransportResult(
            text=envelope.get("result", ""),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            latency_ms=envelope.get("duration_ms", wall_latency_ms),
        )
