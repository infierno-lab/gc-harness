"""Shaped envelope (spec §4.5) — the summary+sample+handle form every adapter returns.

Summaries are computed by code, never by a model.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SchemaInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cols: list[str]


class FullSize(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: int | None = None


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block: str
    inputs_hash: str


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    summary: str
    schema_info: SchemaInfo = Field(alias="schema")
    sample: list[Any] = Field(default_factory=list)
    ref: str
    full_size: FullSize
    provenance: Provenance
