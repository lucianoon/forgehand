"""Bounded, declarative product demos; model output is data, never executable code."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, Field, model_validator

ShortText = Annotated[str, Field(min_length=1, max_length=300)]
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")]


class ProductIdea(BaseModel):
    project_id: str = Field(min_length=1, max_length=100)
    idea: str = Field(min_length=15, max_length=4000)
    audience: str = Field(min_length=3, max_length=300)
    max_cost_usd: float = Field(default=0.5, ge=0.01, le=5, allow_inf_nan=False)
    idempotency_key: str = Field(min_length=8, max_length=100)


class ProductBrief(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    audience: ShortText
    outcome: ShortText
    features: list[ShortText] = Field(min_length=1, max_length=8)
    backlog: list[ShortText] = Field(min_length=1, max_length=12)
    acceptance_criteria: list[ShortText] = Field(min_length=1, max_length=8)
    out_of_scope: list[ShortText] = Field(min_length=1, max_length=8)


class DemoField(BaseModel):
    id: Identifier
    label: str = Field(min_length=1, max_length=80)
    kind: Literal["text", "number", "date", "time", "select"]
    required: bool
    options: list[ShortText] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def valid_options(self) -> DemoField:
        if self.kind == "select" and not self.options:
            raise ValueError("select requires options")
        return self


class DemoRecord(BaseModel):
    values: list[str] = Field(max_length=8)

    @model_validator(mode="after")
    def bounded_values(self) -> DemoRecord:
        if any(len(value) > 300 for value in self.values):
            raise ValueError("record value too long")
        return self


class DemoEntity(BaseModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=80)
    fields: list[DemoField] = Field(min_length=1, max_length=8)
    records: list[DemoRecord] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def valid_records(self) -> DemoEntity:
        if len({field.id for field in self.fields}) != len(self.fields):
            raise ValueError("duplicate field id")
        if any(len(row.values) != len(self.fields) for row in self.records):
            raise ValueError("record values must follow field order")
        return self


class DemoApp(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: ShortText
    theme: Literal["forest", "ocean", "plum"]
    entities: list[DemoEntity] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def unique_entities(self) -> DemoApp:
        if len({entity.id for entity in self.entities}) != len(self.entities):
            raise ValueError("duplicate entity id")
        return self
