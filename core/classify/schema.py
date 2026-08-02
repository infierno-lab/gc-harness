"""NormalizedAsk (spec §5.3) — the typed output of the classifier ladder.
Strict: no field the classifier didn't populate ever silently appears."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TaskType = Literal["analytic_query", "pipeline", "scenario", "optimization", "ops_status", "unknown"]


class WindowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from", description="YYYY-MM")
    to: str = Field(description="YYYY-MM")


class Entities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand: str | None = None
    platform: str | None = None
    window: WindowSpec | None = None
    price_delta_pct: float | None = None
    budget: float | None = None
    metric: str | None = None


class NormalizedAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_type: TaskType
    domain: str
    entities: Entities = Field(default_factory=Entities)
    output_wanted: str
    constraints: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    confidence: float
