"""Effort-tiered model routing (spec §3.3: "Haiku-class: classify/extract ·
Sonnet-class: plan · top tier: escalation only"). `resolve_model` is the one
place a call site turns `call_class` (+ an optional escalation hint) into a
concrete model id — nobody else should hardcode a model string.

Three tiers, each an env var with a Claude-lineup default:
  light    -> GC_MODEL_LIGHT    (default: claude-haiku-4-5-20251001)
  standard -> GC_MODEL_STANDARD (default: claude-sonnet-5)
  heavy    -> GC_MODEL_HEAVY    (default: claude-opus-4-8; escalation only)

Every tier is provider-agnostic: pointing GC_MODEL_HEAVY (or any tier) at a
Bifrost-routed model id (e.g. "kimi-k2.5") just works, since the gateway's
transport layer (not this module) is what talks to Bifrost/Moonshot/etc.

Static call_class -> default-tier map (data, easy to extend); anything
unlisted falls back to "standard":
  classify -> light, plan -> standard, escalate -> heavy

`effort`, when it names a real tier, overrides the call_class map for that
one call — the hook a future dynamic-escalation path (e.g. the planner's
repair round asking one tier up, or spec §5.3's "Low confidence -> Sonnet
one-shot") plugs into. No escalation behavior is wired up yet; only the
kwarg exists.

Per-call-class override env vars (`GC_MODEL_OVERRIDE_CLASSIFY`,
`GC_MODEL_OVERRIDE_PLAN`, ...) win over BOTH tier routing and `effort` —
precedence is per-class > tier > default. (The OVERRIDE_ prefix keeps the
namespace clear of the tier vars themselves.)
"""

import os

Tier = str  # "light" | "standard" | "heavy" — kept as str, not Literal, so a
# caller can pass through an unrecognized/future tier name without a type error;
# resolve_model itself falls back to "standard" for anything it doesn't know.

_TIER_ENV_VARS: dict[Tier, str] = {
    "light": "GC_MODEL_LIGHT",
    "standard": "GC_MODEL_STANDARD",
    "heavy": "GC_MODEL_HEAVY",
}

_TIER_DEFAULTS: dict[Tier, str] = {
    "light": "claude-haiku-4-5-20251001",
    "standard": "claude-sonnet-5",
    "heavy": "claude-opus-4-8",
}

_CALL_CLASS_TIERS: dict[str, Tier] = {
    "classify": "light",
    "plan": "standard",
    "chat": "light",
    "escalate": "heavy",
}


def resolve_model(call_class: str, *, effort: Tier | None = None) -> str:
    override = os.environ.get(f"GC_MODEL_OVERRIDE_{call_class.upper()}")
    if override:
        return override

    tier = effort if effort in _TIER_DEFAULTS else _CALL_CLASS_TIERS.get(call_class, "standard")
    env_var = _TIER_ENV_VARS[tier]
    return os.environ.get(env_var, _TIER_DEFAULTS[tier])
