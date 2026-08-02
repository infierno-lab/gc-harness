"""Unit tests for the deterministic compound-directive guard
(core/classify/compound.py) — the code-level backstop for stage 2's
non-deterministic signaling of a compound ask's extra directive. See
evals/test_classifier.py for the full-pipeline regression proving this closes
the reachable cache-poisoning hole even when the LLM under-reports.
"""

import pytest

from core.classify.compound import compound_directive_constraints


def test_plain_ask_gains_no_constraint_tokens() -> None:
    assert compound_directive_constraints("run elasticity for demo on blinkit for Jan-Jun 2026") == []
    assert compound_directive_constraints("How many SKUs made losses last quarter?") == []
    assert compound_directive_constraints("show recent runs") == []


def test_plain_asks_primary_verb_is_not_mistaken_for_a_compound_directive() -> None:
    # "run" is itself in the catalogued-action verb list -- it must only ever
    # fire when preceded by a conjunction ("and run"/"then run"), never for an
    # ask's own leading action.
    assert compound_directive_constraints("run elasticity for demo on blinkit") == []
    assert compound_directive_constraints("build a promo plan for demo on zepto") == []


@pytest.mark.parametrize(
    ("text", "expected_token"),
    [
        ("run elasticity for demo on blinkit for Jan to Feb and promote the model to champion", "compound:promote"),
        ("run elasticity for demo on blinkit for Jan-Jun 2026 then optimize the promo budget", "compound:optimize"),
        ("show recent runs and then push the results to slack", "compound:push"),
        ("run elasticity for demo and approve the surface", "compound:approve"),
        ("compute elasticity for demo on zepto AND PROMOTE it", "compound:promote"),  # case-insensitive
        ("build a promo plan for demo and deploy it", "compound:deploy"),
        ("run elasticity for demo and archive the old run", "compound:archive"),
    ],
)
def test_compound_directive_detected(text: str, expected_token: str) -> None:
    assert expected_token in compound_directive_constraints(text)


def test_compound_directive_dedupes_repeated_verb() -> None:
    text = "run elasticity for demo and promote it, and then promote it again"
    assert compound_directive_constraints(text) == ["compound:promote"]


def test_compound_directive_preserves_first_appearance_order_across_distinct_verbs() -> None:
    text = "run elasticity for demo and optimize the budget and then promote the model"
    assert compound_directive_constraints(text) == ["compound:optimize", "compound:promote"]


def test_ask_with_unrelated_trailing_clause_gains_no_token() -> None:
    # "and" followed by something that isn't a catalogued action verb must
    # not fire -- this is a hash-separation guard, not a blanket "any 'and'".
    assert compound_directive_constraints("run elasticity for demo on blinkit and show the surface") == []
