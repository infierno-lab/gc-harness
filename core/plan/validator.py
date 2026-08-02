"""Plan validator (spec §5.6) — pure code, the actual product. Runs every
check over (catalog, plan) and collects every failure; never auto-fixes,
never loops.
"""

from typing import Any
from uuid import UUID

import jsonschema
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.catalog.store import block_io_for, grants_for, resolve_block
from core.db.models import BlockVersion
from core.plan.plan_ir import Node, PlanIR

_COST_RANK = {"instant": 0, "seconds": 1, "minutes": 2, "hours": 3}


class ValidationError(BaseModel):
    code: str
    node_id: str | None = None
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    valid: bool
    errors: list[ValidationError] = Field(default_factory=list)
    # node ids whose block requires a named approver distinct from the
    # requester (spec §8), flagged even when actor_roles already satisfy it.
    requires_approval: list[str] = Field(default_factory=list)


class PlanCycleError(Exception):
    def __init__(self, cycle_nodes: list[str]) -> None:
        self.cycle_nodes = cycle_nodes
        super().__init__(f"cycle detected among nodes: {cycle_nodes}")


def _parse_ref(ref: str) -> tuple[str, str] | None:
    node_id, sep, port = ref.partition(".")
    if not sep or not node_id or not port:
        return None
    return node_id, port


def topo_order(plan: PlanIR) -> list[str]:
    """Deterministic topological order of node ids over declared input edges.

    Malformed/dangling input refs are ignored here — the validator reports
    those separately (`unbound_input`); this function only walks edges that
    resolve to a real node id, and raises `PlanCycleError` if that graph has
    a cycle. Exported so the executor can reuse it for dispatch order.
    """
    node_ids = [node.id for node in plan.nodes]
    id_set = set(node_ids)
    deps: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for node in plan.nodes:
        for ref in node.inputs.values():
            parsed = _parse_ref(ref)
            if parsed is None:
                continue
            src_id, _ = parsed
            if src_id in id_set and src_id != node.id:
                deps[node.id].add(src_id)

    order: list[str] = []
    state: dict[str, int] = {}  # 0=unvisited (absent), 1=in-progress, 2=done

    def visit(node_id: str, stack: list[str]) -> None:
        if state.get(node_id) == 2:
            return
        if state.get(node_id) == 1:
            cycle_start = stack.index(node_id)
            raise PlanCycleError(stack[cycle_start:] + [node_id])
        state[node_id] = 1
        stack.append(node_id)
        for dep in sorted(deps[node_id]):
            visit(dep, stack)
        stack.pop()
        state[node_id] = 2
        order.append(node_id)

    for node_id in sorted(node_ids):
        if state.get(node_id) != 2:
            visit(node_id, [])
    return order


def _json_pointer(path: Any) -> str:
    return "/" + "/".join(str(part) for part in path)


def validate_plan(
    session: Session,
    tenant_id: str | UUID,
    plan: PlanIR,
    *,
    actor_roles: list[str] = (),
    budget_cost_class: str = "hours",
) -> ValidationResult:
    errors: list[ValidationError] = []
    requires_approval: list[str] = []
    actor_roles = list(actor_roles)

    node_by_id: dict[str, Node] = {}
    for node in plan.nodes:
        if node.id in node_by_id:
            errors.append(
                ValidationError(
                    code="duplicate_node_id", node_id=node.id, message=f"duplicate node id {node.id!r}"
                )
            )
        node_by_id[node.id] = node

    resolved: dict[str, BlockVersion] = {}
    io_by_node: dict[str, dict[str, dict[str, str]]] = {}
    grants = grants_for(session, tenant_id)

    for node in plan.nodes:
        block_version = resolve_block(session, node.block)
        if block_version is None:
            errors.append(
                ValidationError(
                    code="unknown_block",
                    node_id=node.id,
                    message=f"block {node.block!r} not found in catalog",
                )
            )
            continue
        if block_version.status != "active":
            errors.append(
                ValidationError(
                    code="inactive_block",
                    node_id=node.id,
                    message=f"block {node.block!r} is {block_version.status}, not active",
                )
            )
            continue
        resolved[node.id] = block_version
        io_by_node[node.id] = block_io_for(session, block_version)

    # --- input binding + contract type-check ---------------------------------
    for node in plan.nodes:
        for port_name, ref in node.inputs.items():
            parsed = _parse_ref(ref)
            if parsed is None:
                errors.append(
                    ValidationError(
                        code="unbound_input",
                        node_id=node.id,
                        message=f"malformed input ref {ref!r} for port {port_name!r}",
                        details={"port": port_name, "ref": ref},
                    )
                )
                continue
            src_id, src_port = parsed
            if src_id not in node_by_id:
                errors.append(
                    ValidationError(
                        code="unbound_input",
                        node_id=node.id,
                        message=f"input {port_name!r} references unknown node {src_id!r}",
                        details={"port": port_name, "ref": ref},
                    )
                )
                continue
            if src_id not in resolved or node.id not in resolved:
                # source or this node's own block failed to resolve — already
                # reported above; don't cascade a second error onto the edge.
                continue

            src_io = io_by_node[src_id]
            if src_port not in src_io["produces"]:
                errors.append(
                    ValidationError(
                        code="unbound_input",
                        node_id=node.id,
                        message=f"input {port_name!r} references {ref!r}, "
                        f"but {src_id!r} does not produce port {src_port!r}",
                        details={"port": port_name, "ref": ref},
                    )
                )
                continue

            consumer_io = io_by_node[node.id]
            if port_name not in consumer_io["consumes"]:
                errors.append(
                    ValidationError(
                        code="unbound_input",
                        node_id=node.id,
                        message=f"{node.block!r} does not declare a consume port named {port_name!r}",
                        details={"port": port_name},
                    )
                )
                continue

            producer_contract = src_io["produces"][src_port]
            consumer_contract = consumer_io["consumes"][port_name]
            if producer_contract != consumer_contract:
                errors.append(
                    ValidationError(
                        code="contract_mismatch",
                        node_id=node.id,
                        message=f"port {port_name!r} expects {consumer_contract!r}, "
                        f"but {ref!r} produces {producer_contract!r}",
                        details={
                            "port": port_name,
                            "ref": ref,
                            "producer_contract": producer_contract,
                            "consumer_contract": consumer_contract,
                        },
                    )
                )

    # --- dangling outputs -----------------------------------------------------
    for output_name, ref in plan.outputs.items():
        parsed = _parse_ref(ref)
        if parsed is None:
            errors.append(
                ValidationError(
                    code="dangling_output",
                    node_id=None,
                    message=f"malformed output ref {ref!r} for output {output_name!r}",
                    details={"output": output_name, "ref": ref},
                )
            )
            continue
        src_id, src_port = parsed
        if src_id not in node_by_id:
            errors.append(
                ValidationError(
                    code="dangling_output",
                    node_id=None,
                    message=f"output {output_name!r} references unknown node {src_id!r}",
                    details={"output": output_name, "ref": ref},
                )
            )
            continue
        if src_id not in resolved:
            continue  # already reported via unknown_block/inactive_block
        if src_port not in io_by_node[src_id]["produces"]:
            errors.append(
                ValidationError(
                    code="dangling_output",
                    node_id=None,
                    message=f"output {output_name!r} references {ref!r}, "
                    f"but {src_id!r} does not produce port {src_port!r}",
                    details={"output": output_name, "ref": ref},
                )
            )

    # --- cycle detection --------------------------------------------------
    try:
        topo_order(plan)
    except PlanCycleError as exc:
        errors.append(
            ValidationError(
                code="cycle",
                node_id=None,
                message=str(exc),
                details={"nodes": exc.cycle_nodes},
            )
        )

    # --- params validate against params_schema -------------------------------
    for node in plan.nodes:
        block_version = resolved.get(node.id)
        if block_version is None:
            continue
        validator_cls = jsonschema.validators.validator_for(block_version.params_schema)
        validator = validator_cls(block_version.params_schema)
        for err in validator.iter_errors(node.params):
            errors.append(
                ValidationError(
                    code="params_invalid",
                    node_id=node.id,
                    message=err.message,
                    details={"pointer": _json_pointer(err.absolute_path)},
                )
            )

    # --- grants + params constraints + side-effect policy --------------------
    for node in plan.nodes:
        block_version = resolved.get(node.id)
        if block_version is None:
            continue

        if block_version.id not in grants:
            errors.append(
                ValidationError(
                    code="not_granted",
                    node_id=node.id,
                    message=f"tenant is not granted {node.block!r}",
                )
            )
        else:
            constraints = grants[block_version.id]
            if constraints:
                for key, expected in constraints.items():
                    if key not in node.params:
                        errors.append(
                            ValidationError(
                                code="params_constraint_violation",
                                node_id=node.id,
                                message=f"param {key!r} is required by grant constraint "
                                f"{expected!r} but is missing from node params",
                                details={"param": key, "expected": expected, "reason": "missing"},
                            )
                        )
                    elif node.params[key] != expected:
                        errors.append(
                            ValidationError(
                                code="params_constraint_violation",
                                node_id=node.id,
                                message=f"param {key!r}={node.params[key]!r} violates grant "
                                f"constraint {expected!r}",
                                details={
                                    "param": key,
                                    "expected": expected,
                                    "actual": node.params[key],
                                    "reason": "mismatched",
                                },
                            )
                        )

        sideeffect_class = block_version.sideeffect_class
        if sideeffect_class in ("mutate_state", "external"):
            requires_approval.append(node.id)
            if "approver" not in actor_roles:
                errors.append(
                    ValidationError(
                        code="sideeffect_policy",
                        node_id=node.id,
                        message=f"{node.block!r} is {sideeffect_class!r} and requires an approver role",
                        details={"sideeffect_class": sideeffect_class},
                    )
                )

    # --- mandatory gates: model nodes need a downstream blocking validation --
    forward: dict[str, set[str]] = {node_id: set() for node_id in node_by_id}
    for node in plan.nodes:
        for ref in node.inputs.values():
            parsed = _parse_ref(ref)
            if parsed is None:
                continue
            src_id, _ = parsed
            if src_id in forward:
                forward[src_id].add(node.id)

    def _reachable(start: str) -> set[str]:
        seen: set[str] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            for nxt in forward.get(current, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    for node in plan.nodes:
        block_version = resolved.get(node.id)
        if block_version is None or block_version.block.kind != "model":
            continue
        downstream = _reachable(node.id)
        has_blocking_validation = any(
            d in resolved
            and resolved[d].block.kind == "validation"
            and node_by_id[d].gate is not None
            and node_by_id[d].gate.policy == "block"
            for d in downstream
        )
        if not has_blocking_validation:
            errors.append(
                ValidationError(
                    code="missing_gate",
                    node_id=node.id,
                    message=f"model node {node.id!r} has no downstream blocking validation gate",
                )
            )

    # --- cost budget ----------------------------------------------------------
    budget_rank = _COST_RANK[budget_cost_class]
    for node in plan.nodes:
        block_version = resolved.get(node.id)
        if block_version is None:
            continue
        if _COST_RANK[block_version.cost_class] > budget_rank:
            errors.append(
                ValidationError(
                    code="cost_budget",
                    node_id=node.id,
                    message=f"node {node.id!r} cost class {block_version.cost_class!r} "
                    f"exceeds budget {budget_cost_class!r}",
                    details={"cost_class": block_version.cost_class, "budget": budget_cost_class},
                )
            )

    # --- entity checks (spec §5.6 "entities exist and are permitted") -------
    # TODO: out of PoC scope (deferred per stage 2 spec) — entity resolution
    # against tenant-scoped dimension tables isn't implemented yet. No errors
    # are ever raised by this check in the current PoC.

    return ValidationResult(
        valid=len(errors) == 0, errors=errors, requires_approval=sorted(set(requires_approval))
    )
