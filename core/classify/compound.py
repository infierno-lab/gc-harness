"""Deterministic compound-directive guard (spec §5.3 "entity resolution
happens in code," extended here to intent-*shape* resolution).

Stage 0 can never see a compound ask ("run elasticity for demo on blinkit for
Jan-Feb **and promote the model to champion**") land in this module at all —
every stage-0 rule (`core.classify.rules`) is anchored end-to-end, so an ask
with a trailing directive its bounded shape doesn't account for simply fails
to match and falls through to stage 2 (see `evals/test_classifier.py`'s
stage-0 anchoring regression tests).

Stage 2, however, is asked to *report* a compound ask's extra directive via
`output_wanted`/`constraints`, and an LLM's classification of that nuance is
NOT guaranteed deterministic between calls — confirmed empirically: the
identical ask above returned `output_wanted="report"` on one live call and
`output_wanted="surface"` (with `constraints=[]` either time) on another. Left
alone, that's a reachable cache-poisoning hole: an under-reported compound ask
collides with the plain ask's `normalized_hash`, binds to the plain ask's
cached plan template, and silently drops the extra directive.

This module closes that hole deterministically, in code, never touching the
LLM's parse: it scans the RAW ask text for conjunction + catalogued-action-
verb patterns and returns a stable constraint token per hit. Since
`normalized_hash` (`core.classify.classifier`) includes `len(constraints)`,
merging even one such token forces hash separation from the plain ask
regardless of what stage 2 emitted.

This is a hash-separation guard, not NLU: false positives are safe — they
only ever prevent template *sharing* (the planner still composes a plan
fresh from the ask text, entities, and catalog on a miss); false negatives
are the failure mode to avoid, so the verb list below errs broad, covering
every kind of catalogued action (spec §4.2 block taxonomy: pipeline/model/
optimizer/mutate-class actions) a trailing directive plausibly names.
"""

import re

_COMPOUND_ACTION_VERBS = (
    "promote",
    "approve",
    "reject",
    "publish",
    "deploy",
    "apply",
    "optimize",
    "optimise",
    "build",
    "push",
    "run",
    "execute",
    "schedule",
    "notify",
    "alert",
    "email",
    "send",
    "archive",
    "delete",
    "cancel",
    "rollback",
    "revert",
)

# Longest-first so e.g. "optimise" isn't shadowed by a shorter alternative.
_VERB_ALT = "|".join(sorted(_COMPOUND_ACTION_VERBS, key=len, reverse=True))

# A conjunction/sequencing word ("and", "then", or "and then") immediately
# followed by a catalogued-action verb, anywhere in the ask -- e.g. "and
# promote", "then optimize", "and then push to production".
_COMPOUND_DIRECTIVE_RE = re.compile(
    rf"\b(?:and\s+then|and|then)\s+({_VERB_ALT})\b",
    re.IGNORECASE,
)


def compound_directive_constraints(raw_text: str) -> list[str]:
    """Deterministic constraint tokens (e.g. `"compound:promote"`) for every
    conjunction+action-verb hit in `raw_text`, in first-appearance order and
    de-duplicated by verb. Empty for the overwhelming majority of asks, which
    name a single action."""
    tokens: list[str] = []
    seen: set[str] = set()
    for match in _COMPOUND_DIRECTIVE_RE.finditer(raw_text):
        token = f"compound:{match.group(1).lower()}"
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens
