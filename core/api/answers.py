"""Deterministic answer composer (spec §3.2, §4.5, invariant #2): the reply a
consumer sees is assembled here — from block-computed envelope summaries, run
status, and gate verdicts already produced by code — never phrased or
computed by an LLM. A rejected plan or a halted run is rendered honestly, not
hidden: fail-closed by construction is a feature, not an error page.
"""

from typing import Any

# Block name -> answer family. Keyed by the *producing block*, not the
# classifier's task_type vocabulary (which isn't ours to assume), so this
# stays correct regardless of exactly which strings the planner's task_type
# ends up using.
_ELASTICITY_BLOCKS = {"elasticity_dml", "elasticity_real"}
_PLAN_BLOCKS = {"scenario_projection", "promo_optimizer"}
_METRIC_BLOCKS = {"loss_making_skus"}
_OPS_BLOCKS = {"runs_history"}


def _format_window(window: Any) -> str:
    if isinstance(window, dict):
        start, end = window.get("from"), window.get("to")
        if start and end:
            return f"{start} to {end}"
    if isinstance(window, str) and window:
        return window
    return "the requested window"


def _entity(entities: dict[str, Any], key: str, default: str) -> str:
    value = entities.get(key)
    return str(value) if value not in (None, "") else default


def _normalize_entities(entities: Any) -> dict[str, Any]:
    """Accepts either a plain dict (test fakes) or the real `Entities` pydantic
    model (spec: `core.classify.schema.Entities`) and returns a plain,
    JSON-shaped dict either way — `model_dump(mode="json", by_alias=True)`
    turns `WindowSpec` into `{"from": ..., "to": ...}`, matching what
    `_format_window` expects."""
    if entities is None:
        return {}
    if hasattr(entities, "model_dump"):
        return entities.model_dump(mode="json", by_alias=True)
    return dict(entities)


def _hash_prefix(ref: str, length: int = 12) -> str:
    return ref.removeprefix("res_")[:length]


def _gate_summary(gate_verdicts: list[dict[str, Any]]) -> str:
    if not gate_verdicts:
        return ""
    parts = [f"{verdict['check_suite']}: {verdict['verdict'].upper()}" for verdict in gate_verdicts]
    return "Gated by " + "; ".join(parts) + ". "


def _no_plan_answer(normalized: Any) -> dict[str, Any]:
    ambiguities = list(getattr(normalized, "ambiguities", None) or [])
    if ambiguities:
        text = "I need a bit more detail before I can compose a plan: " + "; ".join(ambiguities)
    else:
        text = "No plan could be composed for this ask."
    return {"text": text, "numbers_provenance": {}}


def _rejected_answer(errors: list[Any]) -> dict[str, Any]:
    if not errors:
        return {"text": "The proposed plan was rejected by the validator.", "numbers_provenance": {}}
    lines = [
        f"{error.code}" + (f" on node {error.node_id!r}" if error.node_id else "") + f": {error.message}"
        for error in errors
    ]
    text = "The plan was rejected by the validator before anything ran:\n" + "\n".join(lines)
    return {"text": text, "numbers_provenance": {"validator_error_codes": [error.code for error in errors]}}


def _halted_answer(run_result: Any, gate_verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    gate_failed_node = next(
        (node_id for node_id, status in run_result.node_statuses.items() if status == "gate_failed"), None
    )
    if gate_failed_node is not None:
        failing_verdict = next((v for v in gate_verdicts if v["node_id"] == gate_failed_node), None)
        detail = f" ({failing_verdict['check_suite']}: {failing_verdict['verdict']})" if failing_verdict else ""
        text = (
            f"Run halted by gate at node {gate_failed_node!r}{detail} — the output failed validation and was "
            f"never published. Run {run_result.run_id}."
        )
    else:
        failed_node = next(
            (node_id for node_id, status in run_result.node_statuses.items() if status == "failed"), None
        )
        text = f"Run {run_result.run_id} failed" + (f" at node {failed_node!r}" if failed_node else "") + "."
    return {"text": text, "numbers_provenance": {"run_id": run_result.run_id, "status": run_result.status}}


def _elasticity_answer(
    entities: dict[str, Any], name: str, env: Any, run_result: Any, gate_verdicts: list[dict[str, Any]]
) -> dict[str, Any]:
    brand = _entity(entities, "brand", "the brand")
    platform = _entity(entities, "platform", "the platform")
    window = _format_window(entities.get("window"))
    text = (
        f"Estimated elasticities for {brand} on {platform} ({window}): {env.summary}. "
        f"{_gate_summary(gate_verdicts)}"
        f"Provenance: run {run_result.run_id}, surface hash {_hash_prefix(env.ref)}…"
    )
    return {
        "text": text,
        "numbers_provenance": {"run_id": run_result.run_id, name: {"ref": env.ref, "block": env.provenance.block}},
    }


def _plan_output_answer(
    entities: dict[str, Any], name: str, env: Any, run_result: Any, gate_verdicts: list[dict[str, Any]]
) -> dict[str, Any]:
    brand = _entity(entities, "brand", "the brand")
    platform = _entity(entities, "platform", "the platform")
    text = (
        f"Plan for {brand} on {platform}: {env.summary}. "
        f"{_gate_summary(gate_verdicts)}"
        f"Provenance: run {run_result.run_id}, plan hash {_hash_prefix(env.ref)}…"
    )
    return {
        "text": text,
        "numbers_provenance": {"run_id": run_result.run_id, name: {"ref": env.ref, "block": env.provenance.block}},
    }


def _metric_answer(
    entities: dict[str, Any], name: str, env: Any, run_result: Any, gate_verdicts: list[dict[str, Any]]
) -> dict[str, Any]:
    text = f"{env.summary}. {_gate_summary(gate_verdicts)}Provenance: run {run_result.run_id}, ref {env.ref}."
    return {
        "text": text,
        "numbers_provenance": {"run_id": run_result.run_id, name: {"ref": env.ref, "block": env.provenance.block}},
    }


def _ops_answer(name: str, env: Any, run_result: Any) -> dict[str, Any]:
    text = f"{env.summary}. Provenance: run {run_result.run_id}, ref {env.ref}."
    return {
        "text": text,
        "numbers_provenance": {"run_id": run_result.run_id, name: {"ref": env.ref, "block": env.provenance.block}},
    }


def _raw_listing_answer(run_result: Any, gate_verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    if not run_result.outputs:
        return {
            "text": f"Run {run_result.run_id} succeeded with no published outputs.",
            "numbers_provenance": {"run_id": run_result.run_id},
        }
    lines = [
        f"{output_name}: {env.summary} (block {env.provenance.block}, ref {env.ref})"
        for output_name, env in run_result.outputs.items()
    ]
    text = f"{_gate_summary(gate_verdicts)}" + "; ".join(lines) + f". Provenance: run {run_result.run_id}."
    return {
        "text": text,
        "numbers_provenance": {
            "run_id": run_result.run_id,
            "outputs": {output_name: env.ref for output_name, env in run_result.outputs.items()},
        },
    }


def _success_answer(entities: dict[str, Any], run_result: Any, gate_verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    for output_name, env in run_result.outputs.items():
        block_name = env.provenance.block.split("@")[0]
        if block_name in _ELASTICITY_BLOCKS:
            return _elasticity_answer(entities, output_name, env, run_result, gate_verdicts)
        if block_name in _PLAN_BLOCKS:
            return _plan_output_answer(entities, output_name, env, run_result, gate_verdicts)
        if block_name in _METRIC_BLOCKS:
            return _metric_answer(entities, output_name, env, run_result, gate_verdicts)
        if block_name in _OPS_BLOCKS:
            return _ops_answer(output_name, env, run_result)
    return _raw_listing_answer(run_result, gate_verdicts)


def compose_answer(
    *,
    classify_result: Any,
    plan_outcome: Any,
    run_result: Any | None,
    gate_verdicts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The single place a client-visible answer is assembled. Every branch
    returns `{text, numbers_provenance}` — `text` is always built from code
    (never an LLM), `numbers_provenance` always cites the run/gate/block that
    produced any number mentioned."""
    gate_verdicts = gate_verdicts or []
    normalized = classify_result.normalized

    if plan_outcome.plan is None:
        return _no_plan_answer(normalized)

    validation = plan_outcome.validation
    if validation is None or not validation.valid:
        errors = list(validation.errors) if validation is not None else []
        return _rejected_answer(errors)

    if run_result is None:
        return {"text": "The plan validated but was not executed.", "numbers_provenance": {}}

    if run_result.status != "succeeded":
        return _halted_answer(run_result, gate_verdicts)

    entities = _normalize_entities(getattr(normalized, "entities", None))
    return _success_answer(entities, run_result, gate_verdicts)
