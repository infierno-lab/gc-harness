"""Per-call-class LLM timeouts (spec §10 failure model: "planner timeout ->
queue + notify", never a symptom masked by a one-size-fits-all deadline). A
single fixed timeout treats a slow *plan* call — measured 30-90s for a
successful CLI planner call, and up to ~2x that across a repair round — the
same as a stuck *classify* call, which should fail fast. Scaling per call
class lets each degrade at its own natural latency instead.

  classify -> GC_LLM_TIMEOUT_CLASSIFY (default 30s)
  plan     -> GC_LLM_TIMEOUT_PLAN     (default 120s)
  chat     -> GC_LLM_TIMEOUT_CHAT     (default 60s)

Anything unlisted falls back to the classify default (the repo-wide
constant this replaces) — the safest, shortest prior behavior.
"""

import os

_DEFAULT_TIMEOUT_S = 30.0

_CALL_CLASS_ENV_VARS: dict[str, str] = {
    "classify": "GC_LLM_TIMEOUT_CLASSIFY",
    "plan": "GC_LLM_TIMEOUT_PLAN",
    "chat": "GC_LLM_TIMEOUT_CHAT",
}

_CALL_CLASS_DEFAULTS: dict[str, float] = {
    "classify": 30.0,
    "plan": 120.0,
    "chat": 60.0,
}


def resolve_timeout(call_class: str) -> float:
    """The timeout (seconds) for one `complete_structured` attempt at this
    call class. Per-call-class env var wins; an unlisted call_class falls
    back to the classify default, not to the plan default — fail toward the
    shorter, safer timeout for anything this map doesn't yet know about."""
    env_var = _CALL_CLASS_ENV_VARS.get(call_class)
    default = _CALL_CLASS_DEFAULTS.get(call_class, _DEFAULT_TIMEOUT_S)
    if env_var is None:
        return default
    raw = os.environ.get(env_var)
    return default if raw is None else float(raw)
