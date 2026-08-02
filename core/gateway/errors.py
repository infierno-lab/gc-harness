"""Gateway error taxonomy (spec §3.3 L0, §5.2): every failure here is
fail-closed — a caller never receives a silently-degraded result."""


class GatewayError(Exception):
    """The gateway could not produce a valid, schema-conformant structured
    result after all retries. Fail closed — never a silent fallback."""


class GatewayTransportError(GatewayError):
    """The underlying transport itself failed (subprocess exit, API error,
    timeout, unparseable envelope) — distinct from a validly-transported but
    schema-invalid response."""
