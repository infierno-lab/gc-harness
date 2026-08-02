"""Pydantic value objects for the Demand pack's contracts (spec §4.3).

Per spec's "handles, not payloads" invariant, blocks never exchange dataframes:
a contract instance carries a reference to the materialized data plus the
metadata a validator/executor can cheaply check on that reference (columns,
dtypes, grain, nullability) — never the rows themselves.
"""

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field


class ColumnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    dtype: Literal["string", "int", "float", "bool", "date", "datetime", "category"]
    nullable: bool = False
    unit: str | None = None
    description: str | None = None


class DmlPanelV1(BaseModel):
    """dml_panel@v1 — sku x week x platform panel reference."""

    model_config = ConfigDict(extra="forbid")
    contract_name: ClassVar[str] = "dml_panel@v1"

    ref: str
    grain: list[str] = Field(default_factory=lambda: ["sku", "week", "platform"])
    columns: list[ColumnSpec] = Field(
        default_factory=lambda: [
            ColumnSpec(name="sku", dtype="string", nullable=False),
            ColumnSpec(name="week", dtype="date", nullable=False),
            ColumnSpec(name="platform", dtype="string", nullable=False),
            ColumnSpec(name="price", dtype="float", nullable=False, unit="currency"),
            ColumnSpec(name="units", dtype="int", nullable=False),
        ]
    )
    row_count: int | None = None


class ElasticitySurfaceV1(BaseModel):
    """elasticity_surface@v1 — per-sku price elasticity estimates from a model block."""

    model_config = ConfigDict(extra="forbid")
    contract_name: ClassVar[str] = "elasticity_surface@v1"

    ref: str
    grain: list[str] = Field(default_factory=lambda: ["sku"])
    columns: list[ColumnSpec] = Field(
        default_factory=lambda: [
            ColumnSpec(name="sku", dtype="string", nullable=False),
            ColumnSpec(name="elasticity", dtype="float", nullable=False),
            ColumnSpec(name="ci_lower", dtype="float", nullable=True),
            ColumnSpec(name="ci_upper", dtype="float", nullable=True),
            ColumnSpec(name="method", dtype="string", nullable=False),
        ]
    )
    row_count: int | None = None
    model_version: str | None = None


class PromoPlanV1(BaseModel):
    """promo_plan@v1 — sku x week x platform recommended promo plan from an optimizer."""

    model_config = ConfigDict(extra="forbid")
    contract_name: ClassVar[str] = "promo_plan@v1"

    ref: str
    grain: list[str] = Field(default_factory=lambda: ["sku", "week", "platform"])
    columns: list[ColumnSpec] = Field(
        default_factory=lambda: [
            ColumnSpec(name="sku", dtype="string", nullable=False),
            ColumnSpec(name="week", dtype="date", nullable=False),
            ColumnSpec(name="platform", dtype="string", nullable=False),
            ColumnSpec(name="recommended_price", dtype="float", nullable=False, unit="currency"),
            ColumnSpec(name="expected_lift", dtype="float", nullable=True),
        ]
    )
    row_count: int | None = None


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_name: str
    verdict: Literal["pass", "warn", "fail"]
    details: dict[str, Any] = Field(default_factory=dict)


class ValidationVerdictsV1(BaseModel):
    """validation_verdicts@v1 — output of a validation-kind block; not tabular."""

    model_config = ConfigDict(extra="forbid")
    contract_name: ClassVar[str] = "validation_verdicts@v1"

    check_suite: str
    verdicts: list[Verdict]
    overall: Literal["pass", "warn", "fail"]


class ReportRefV1(BaseModel):
    """report_ref@v1 — a reference to a rendered, human-facing deliverable."""

    model_config = ConfigDict(extra="forbid")
    contract_name: ClassVar[str] = "report_ref@v1"

    ref: str
    uri: str
    kind: Literal["workbook", "deck", "pdf", "html"]
    format: str


CONTRACT_REGISTRY: dict[str, type[BaseModel]] = {
    DmlPanelV1.contract_name: DmlPanelV1,
    ElasticitySurfaceV1.contract_name: ElasticitySurfaceV1,
    PromoPlanV1.contract_name: PromoPlanV1,
    ValidationVerdictsV1.contract_name: ValidationVerdictsV1,
    ReportRefV1.contract_name: ReportRefV1,
}


def get_contract(name_version: str) -> type[BaseModel]:
    try:
        return CONTRACT_REGISTRY[name_version]
    except KeyError:
        raise KeyError(f"unknown contract: {name_version!r}") from None


def contract_json_schema(name_version: str) -> dict[str, Any]:
    return get_contract(name_version).model_json_schema()
