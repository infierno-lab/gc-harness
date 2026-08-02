"""Gateway tests (spec §3.3 L0, §5.5): structured-output parsing/retry/audit
over a FakeTransport — no network in the default suite. `ClaudeCliTransport`
is exercised with a mocked `subprocess.run` (still no network); the one real
network call lives in evals/test_classifier.py's `@pytest.mark.live` smoke.
"""

import json
import uuid
from unittest.mock import MagicMock

import httpx
import pytest

import core.gateway.client as client_module
from core.db.models import LlmCall
from core.db.session import tenant_session
from core.gateway.bifrost_transport import BifrostTransport
from core.gateway.cli_transport import ClaudeCliTransport
from core.gateway.client import Gateway
from core.gateway.errors import GatewayError, GatewayTransportError
from core.gateway.model_router import resolve_model
from core.gateway.sdk_transport import AnthropicTransport
from core.gateway.transport import TransportResult

_SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}


class FakeTransport:
    """Returns queued `TransportResult`s (or raises queued exceptions) in
    order — one entry consumed per `.complete()` call."""

    def __init__(self, responses: list[TransportResult | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def complete(self, prompt: str, *, model: str, max_tokens: int, timeout_s: float) -> TransportResult:
        self.calls.append(prompt)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _llm_calls(tenant_id: uuid.UUID) -> list[LlmCall]:
    with tenant_session(tenant_id) as session:
        return list(
            session.query(LlmCall).filter_by(tenant_id=tenant_id).order_by(LlmCall.created_at).all()
        )


def test_complete_structured_strips_markdown_fence(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    transport = FakeTransport(
        [TransportResult(text='```json\n{"x": 1}\n```', input_tokens=10, output_tokens=5, cache_read_tokens=0, latency_ms=120)]
    )
    gateway = Gateway(transport)

    with tenant_session(tenant_id) as session:
        result = gateway.complete_structured(
            session, tenant_id, call_class="classify", prompt="p", schema=_SCHEMA, model="m"
        )

    assert result == {"x": 1}
    assert len(transport.calls) == 1

    calls = _llm_calls(tenant_id)
    assert len(calls) == 1
    assert calls[0].call_class == "classify"
    assert calls[0].model == "m"
    assert calls[0].input_tokens == 10
    assert calls[0].output_tokens == 5
    assert calls[0].latency_ms == 120
    assert calls[0].response_hash is not None
    assert calls[0].prompt_hash is not None


def test_complete_structured_retries_once_on_malformed_json_then_succeeds(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    transport = FakeTransport(
        [
            TransportResult(text="not json at all", input_tokens=5, output_tokens=5, cache_read_tokens=0, latency_ms=50),
            TransportResult(text='{"x": 2}', input_tokens=5, output_tokens=5, cache_read_tokens=0, latency_ms=50),
        ]
    )
    gateway = Gateway(transport)

    with tenant_session(tenant_id) as session:
        result = gateway.complete_structured(
            session, tenant_id, call_class="classify", prompt="p", schema=_SCHEMA, model="m", retries=1
        )

    assert result == {"x": 2}
    assert len(transport.calls) == 2
    # the retry prompt carries the original prompt plus the error, so the model
    # sees it wasn't a repeat of the exact same request.
    assert transport.calls[1] != transport.calls[0]
    assert "p" in transport.calls[1]

    calls = _llm_calls(tenant_id)
    assert len(calls) == 2
    assert calls[0].response_hash is not None  # "not json at all" still hashed even though unparseable
    assert calls[1].response_hash is not None


def test_complete_structured_fails_closed_after_retries_exhausted(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    transport = FakeTransport(
        [
            TransportResult(text="still not json", input_tokens=1, output_tokens=1, cache_read_tokens=0, latency_ms=10),
            TransportResult(text="nope", input_tokens=1, output_tokens=1, cache_read_tokens=0, latency_ms=10),
        ]
    )
    gateway = Gateway(transport)

    with tenant_session(tenant_id) as session:
        with pytest.raises(GatewayError):
            gateway.complete_structured(
                session, tenant_id, call_class="classify", prompt="p", schema=_SCHEMA, model="m", retries=1
            )

    assert len(transport.calls) == 2
    calls = _llm_calls(tenant_id)
    assert len(calls) == 2


def test_complete_structured_retries_on_schema_violation(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    transport = FakeTransport(
        [
            TransportResult(text='{"x": "not-an-int"}', input_tokens=1, output_tokens=1, cache_read_tokens=0, latency_ms=10),
            TransportResult(text='{"x": 3}', input_tokens=1, output_tokens=1, cache_read_tokens=0, latency_ms=10),
        ]
    )
    gateway = Gateway(transport)

    with tenant_session(tenant_id) as session:
        result = gateway.complete_structured(
            session, tenant_id, call_class="plan", prompt="p", schema=_SCHEMA, model="m", retries=1
        )

    assert result == {"x": 3}
    assert len(transport.calls) == 2


def test_complete_structured_records_llm_call_even_on_transport_failure(
    migrated_test_db: str, tenant_id: uuid.UUID
) -> None:
    transport = FakeTransport([GatewayTransportError("boom"), GatewayTransportError("boom again")])
    gateway = Gateway(transport)

    with tenant_session(tenant_id) as session:
        with pytest.raises(GatewayError):
            gateway.complete_structured(
                session, tenant_id, call_class="classify", prompt="p", schema=_SCHEMA, model="m", retries=1
            )

    calls = _llm_calls(tenant_id)
    assert len(calls) == 2
    assert all(call.response_hash is None for call in calls)


def test_no_retries_means_single_attempt(migrated_test_db: str, tenant_id: uuid.UUID) -> None:
    transport = FakeTransport([TransportResult(text="garbage", input_tokens=1, output_tokens=1, cache_read_tokens=0, latency_ms=1)])
    gateway = Gateway(transport)

    with tenant_session(tenant_id) as session:
        with pytest.raises(GatewayError):
            gateway.complete_structured(
                session, tenant_id, call_class="classify", prompt="p", schema=_SCHEMA, model="m", retries=0
            )

    assert len(transport.calls) == 1
    assert len(_llm_calls(tenant_id)) == 1


# --- ClaudeCliTransport: subprocess mocked, no network -----------------------


def _fake_completed_process(*, returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_cli_transport_parses_result_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = (
        '{"result": "{\\"x\\": 1}", "is_error": false, "duration_ms": 999, '
        '"usage": {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 2}}'
    )
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return _fake_completed_process(returncode=0, stdout=envelope)

    monkeypatch.setattr("subprocess.run", fake_run)

    transport = ClaudeCliTransport()
    result = transport.complete("hello", model="claude-haiku-4-5-20251001", max_tokens=100, timeout_s=5.0)

    assert result.text == '{"x": 1}'
    assert result.input_tokens == 7
    assert result.output_tokens == 3
    assert result.cache_read_tokens == 2
    assert result.latency_ms == 999
    assert "hello" in captured_cmd
    assert "--strict-mcp-config" in captured_cmd


def test_cli_transport_raises_on_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = '{"result": "boom", "is_error": true, "duration_ms": 10, "usage": {}}'
    monkeypatch.setattr("subprocess.run", lambda cmd, **kwargs: _fake_completed_process(returncode=0, stdout=envelope))

    transport = ClaudeCliTransport()
    with pytest.raises(GatewayTransportError):
        transport.complete("hello", model="m", max_tokens=100, timeout_s=5.0)


def test_cli_transport_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "subprocess.run", lambda cmd, **kwargs: _fake_completed_process(returncode=1, stderr="model not found")
    )

    transport = ClaudeCliTransport()
    with pytest.raises(GatewayTransportError):
        transport.complete("hello", model="does-not-exist", max_tokens=100, timeout_s=5.0)


def test_cli_transport_falls_back_when_strict_mcp_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = '{"result": "ok", "is_error": false, "duration_ms": 5, "usage": {}}'
    call_args: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        call_args.append(list(cmd))
        if "--strict-mcp-config" in cmd:
            return _fake_completed_process(returncode=1, stderr="error: unknown option '--strict-mcp-config'")
        return _fake_completed_process(returncode=0, stdout=envelope)

    monkeypatch.setattr("subprocess.run", fake_run)

    transport = ClaudeCliTransport()
    result = transport.complete("hello", model="m", max_tokens=100, timeout_s=5.0)

    assert result.text == "ok"
    assert len(call_args) == 2
    assert "--strict-mcp-config" not in call_args[1]


# --- BifrostTransport: httpx mocked, no network ------------------------------


class _FakeHttpxResponse:
    def __init__(self, *, status_code: int, json_body: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text or (json.dumps(json_body) if json_body is not None else "")

    def json(self) -> dict:
        if self._json_body is None:
            raise ValueError("no JSON body")
        return self._json_body


def _bifrost_body(*, content: str = "hi", prompt_tokens: int = 10, completion_tokens: int = 5, cached_tokens: int | None = None) -> dict:
    usage: dict = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    if cached_tokens is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return {"choices": [{"message": {"content": content}}], "usage": usage}


def test_bifrost_transport_happy_path_prefixes_bare_model(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeHttpxResponse(status_code=200, json_body=_bifrost_body(content='{"x": 1}', cached_tokens=4))

    monkeypatch.setattr("httpx.post", fake_post)

    transport = BifrostTransport(base_url="http://bifrost:8080/v1", api_key="bf-key", provider="anthropic")
    result = transport.complete("hello", model="claude-sonnet-5", max_tokens=100, timeout_s=5.0)

    assert result.text == '{"x": 1}'
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.cache_read_tokens == 4
    assert captured["url"] == "http://bifrost:8080/v1/chat/completions"
    assert captured["json"]["model"] == "anthropic/claude-sonnet-5"
    assert captured["headers"]["Authorization"] == "Bearer bf-key"
    assert captured["timeout"] == 5.0


def test_bifrost_transport_passes_through_already_prefixed_model(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_post(url, *, json, headers, timeout):
        captured["model"] = json["model"]
        return _FakeHttpxResponse(status_code=200, json_body=_bifrost_body())

    monkeypatch.setattr("httpx.post", fake_post)

    transport = BifrostTransport(base_url="http://bifrost:8080/v1", api_key="bf-key")
    transport.complete("hello", model="openai/gpt-5", max_tokens=100, timeout_s=5.0)

    assert captured["model"] == "openai/gpt-5"


def test_bifrost_transport_no_usage_details_means_no_cache_read_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("httpx.post", lambda url, **kwargs: _FakeHttpxResponse(status_code=200, json_body=_bifrost_body()))

    transport = BifrostTransport(base_url="http://bifrost:8080/v1", api_key="bf-key")
    result = transport.complete("hello", model="claude-sonnet-5", max_tokens=100, timeout_s=5.0)

    assert result.cache_read_tokens is None


def test_bifrost_transport_raises_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("httpx.post", lambda url, **kwargs: _FakeHttpxResponse(status_code=500, text="server error"))

    transport = BifrostTransport(base_url="http://bifrost:8080/v1", api_key="bf-key")
    with pytest.raises(GatewayTransportError):
        transport.complete("hello", model="claude-sonnet-5", max_tokens=100, timeout_s=5.0)


def test_bifrost_transport_raises_on_malformed_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "httpx.post", lambda url, **kwargs: _FakeHttpxResponse(status_code=200, json_body={"unexpected": "shape"})
    )

    transport = BifrostTransport(base_url="http://bifrost:8080/v1", api_key="bf-key")
    with pytest.raises(GatewayTransportError):
        transport.complete("hello", model="claude-sonnet-5", max_tokens=100, timeout_s=5.0)


def test_bifrost_transport_raises_on_httpx_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr("httpx.post", fake_post)

    transport = BifrostTransport(base_url="http://bifrost:8080/v1", api_key="bf-key")
    with pytest.raises(GatewayTransportError):
        transport.complete("hello", model="claude-sonnet-5", max_tokens=100, timeout_s=5.0)


def test_bifrost_transport_requires_base_url_and_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GC_BIFROST_BASE_URL", raising=False)
    monkeypatch.delenv("GC_BIFROST_API_KEY", raising=False)

    with pytest.raises(GatewayTransportError):
        BifrostTransport()
    with pytest.raises(GatewayTransportError):
        BifrostTransport(base_url="http://bifrost:8080/v1")  # no key, env unset either


# --- get_gateway(): transport selection precedence ---------------------------


def _reset_gateway_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "_gateway", None)


def test_get_gateway_defaults_to_cli_with_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_gateway_singleton(monkeypatch)
    for var in ("GC_LLM_TRANSPORT", "GC_BIFROST_BASE_URL", "GC_BIFROST_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    gateway = client_module.get_gateway()
    assert isinstance(gateway._transport, ClaudeCliTransport)


def test_get_gateway_prefers_sdk_when_api_key_set_and_no_bifrost(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_gateway_singleton(monkeypatch)
    monkeypatch.delenv("GC_LLM_TRANSPORT", raising=False)
    monkeypatch.delenv("GC_BIFROST_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

    gateway = client_module.get_gateway()
    assert isinstance(gateway._transport, AnthropicTransport)


def test_get_gateway_prefers_bifrost_over_sdk_when_bifrost_base_url_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_gateway_singleton(monkeypatch)
    monkeypatch.delenv("GC_LLM_TRANSPORT", raising=False)
    monkeypatch.setenv("GC_BIFROST_BASE_URL", "http://bifrost:8080/v1")
    monkeypatch.setenv("GC_BIFROST_API_KEY", "bf-fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")  # present, but bifrost still wins

    gateway = client_module.get_gateway()
    assert isinstance(gateway._transport, BifrostTransport)


def test_get_gateway_explicit_transport_override_beats_bifrost_auto_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_gateway_singleton(monkeypatch)
    monkeypatch.setenv("GC_BIFROST_BASE_URL", "http://bifrost:8080/v1")
    monkeypatch.setenv("GC_BIFROST_API_KEY", "bf-fake")
    monkeypatch.setenv("GC_LLM_TRANSPORT", "cli")

    gateway = client_module.get_gateway()
    assert isinstance(gateway._transport, ClaudeCliTransport)


# --- resolve_model(): effort-tiered model router -----------------------------

_MODEL_ENV_VARS = (
    "GC_MODEL_LIGHT",
    "GC_MODEL_STANDARD",
    "GC_MODEL_HEAVY",
    "GC_MODEL_OVERRIDE_CLASSIFY",
    "GC_MODEL_OVERRIDE_PLAN",
)


def _clear_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _MODEL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_resolve_model_defaults_per_call_class(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    assert resolve_model("classify") == "claude-haiku-4-5-20251001"
    assert resolve_model("plan") == "claude-sonnet-5"
    assert resolve_model("some_future_call_class") == "claude-sonnet-5"  # unlisted -> standard tier


def test_resolve_model_tier_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("GC_MODEL_LIGHT", "moonshot/kimi-k2.5")
    assert resolve_model("classify") == "moonshot/kimi-k2.5"
    # unaffected tier stays at its default.
    assert resolve_model("plan") == "claude-sonnet-5"


def test_resolve_model_effort_selects_tier_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    # a low-confidence classify call escalating to the standard tier
    # (spec §5.3: "Low confidence -> Sonnet one-shot").
    assert resolve_model("classify", effort="standard") == "claude-sonnet-5"
    assert resolve_model("classify", effort="heavy") == "claude-opus-4-8"
    # an unrecognized effort value is ignored, falling back to the call_class default.
    assert resolve_model("classify", effort="not-a-real-tier") == "claude-haiku-4-5-20251001"


def test_resolve_model_per_call_class_override_wins_over_tier_and_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("GC_MODEL_OVERRIDE_CLASSIFY", "moonshot/kimi-k2.5")
    monkeypatch.setenv("GC_MODEL_HEAVY", "claude-opus-4-8")

    assert resolve_model("classify") == "moonshot/kimi-k2.5"
    assert resolve_model("classify", effort="heavy") == "moonshot/kimi-k2.5"
    # the override is scoped to its own call_class only.
    assert resolve_model("plan") == "claude-sonnet-5"


# --- resolve_timeout(): per-call-class timeout resolution (spec §10 failure
# model — "planner timeout -> queue + notify", never a bare 500) ------------

_TIMEOUT_ENV_VARS = ("GC_LLM_TIMEOUT_CLASSIFY", "GC_LLM_TIMEOUT_PLAN")


def _clear_timeout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _TIMEOUT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_resolve_timeout_defaults_per_call_class(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.gateway.timeouts import resolve_timeout

    _clear_timeout_env(monkeypatch)

    assert resolve_timeout("classify") == 30.0
    assert resolve_timeout("plan") == 120.0
    # unlisted call class falls back to the shorter, safer classify default —
    # not the plan default — for anything this map doesn't yet know about.
    assert resolve_timeout("some_future_call_class") == 30.0


def test_resolve_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.gateway.timeouts import resolve_timeout

    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("GC_LLM_TIMEOUT_CLASSIFY", "45")
    monkeypatch.setenv("GC_LLM_TIMEOUT_PLAN", "180")

    assert resolve_timeout("classify") == 45.0
    assert resolve_timeout("plan") == 180.0


class _TimeoutCapturingTransport:
    """Records the `timeout_s` each `.complete()` call receives — used to
    confirm the resolved per-call-class timeout actually reaches the
    transport, not just that `resolve_timeout()` returns the right number in
    isolation."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def complete(self, prompt: str, *, model: str, max_tokens: int, timeout_s: float) -> TransportResult:
        self.timeouts.append(timeout_s)
        return TransportResult(text='{"x": 1}', input_tokens=1, output_tokens=1, cache_read_tokens=0, latency_ms=1)


def test_complete_structured_threads_resolved_timeout_to_transport(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("GC_LLM_TIMEOUT_PLAN", "180")

    transport = _TimeoutCapturingTransport()
    gateway = Gateway(transport)

    with tenant_session(tenant_id) as session:
        gateway.complete_structured(session, tenant_id, call_class="plan", prompt="p", schema=_SCHEMA, model="m")
        gateway.complete_structured(session, tenant_id, call_class="classify", prompt="p", schema=_SCHEMA, model="m")

    assert transport.timeouts == [180.0, 30.0]


def test_gateway_explicit_timeout_override_beats_call_class_resolution(
    migrated_test_db: str, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("GC_LLM_TIMEOUT_PLAN", "180")

    transport = _TimeoutCapturingTransport()
    gateway = Gateway(transport, timeout_s=5.0)

    with tenant_session(tenant_id) as session:
        gateway.complete_structured(session, tenant_id, call_class="plan", prompt="p", schema=_SCHEMA, model="m")

    assert transport.timeouts == [5.0]
