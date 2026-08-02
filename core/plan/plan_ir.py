"""Plan IR (spec §5.5) — the typed DAG the planner proposes and the executor runs."""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from core.catalog.hashing import content_hash


class GateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: Literal["block", "warn"]


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    block: str
    params: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, str] = Field(default_factory=dict)
    gate: GateSpec | None = None


class EstimatedCost(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    class_: Literal["instant", "seconds", "minutes", "hours"] = Field(alias="class")


class PlanIR(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    plan_ir_version: int
    intent_summary: str
    nodes: list[Node]
    outputs: dict[str, str] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    estimated_cost: EstimatedCost


def parse_plan_yaml(source: str | Path) -> PlanIR:
    """Parse a Plan IR from either a YAML file path or a raw YAML string."""
    try:
        is_file = Path(source).is_file()
    except (OSError, ValueError):  # e.g. a multi-line YAML string exceeding NAME_MAX
        is_file = False
    raw = Path(source).read_text() if is_file else str(source)
    data = yaml.safe_load(raw)
    return PlanIR.model_validate(data)


def ir_hash(plan: PlanIR) -> str:
    return content_hash(plan.model_dump(mode="json", by_alias=True))
