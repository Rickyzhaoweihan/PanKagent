"""Bounded public request schema. Physical SQL names are never client input."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filters(StrictModel):
    source_file_id: str | None = Field(default=None, max_length=200)
    collection_id: str | None = Field(default=None, max_length=200)
    context_id: str | None = Field(default=None, max_length=200)
    context_kind: str | None = Field(default=None, max_length=200)
    condition: str | None = Field(default=None, max_length=500)
    cell_type_id: str | None = Field(default=None, max_length=200)
    record_status: Literal["source_reported", "validated", "quarantined"] | None = None


class RecordSearch(Filters):
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def explicit_scope(self):
        if not any(value and value.strip() for value in (self.collection_id, self.source_file_id, self.context_id)):
            raise ValueError("Record search requires collection_id, source_file_id or context_id")
        return self


class GeneSearch(StrictModel):
    query: str | None = Field(default=None, min_length=1, max_length=200)
    chromosome: str | None = Field(default=None, pattern=r"^(?:[1-9]|1[0-9]|2[0-2]|X|Y|MT)$")
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=1)
    assembly: str | None = Field(default=None, min_length=1, max_length=100)
    limit: int = Field(default=25, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def complete_region(self):
        region = [self.chromosome, self.start, self.end, self.assembly]
        if any(v is not None for v in region) and (
                any(v is None for v in region) or self.end <= self.start):
            raise ValueError("Region requires chromosome, start, end, assembly and end > start")
        if not (self.query and self.query.strip()) and self.chromosome is None:
            raise ValueError("Supply nonblank query text or a complete genomic region")
        return self


class QueryRequest(StrictModel):
    cypher: str | None = Field(default=None, min_length=1, max_length=20_000)
    parameters: dict[str, Any] = Field(default_factory=dict)
    postgres_search: GeneSearch | None = None
    mode: Literal["brief", "detail"] = "brief"
    snapshot_id: str | None = Field(default=None, max_length=200)
    filters: Filters = Field(default_factory=Filters)
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.cypher is None) == (self.postgres_search is None):
            raise ValueError("Supply exactly one of cypher or postgres_search")
        if self.postgres_search is not None and self.parameters:
            raise ValueError("Parameters apply only to Cypher")
        if self.mode == "brief" and (self.offset or any(v is not None for v in self.filters.model_dump().values())):
            raise ValueError("Evidence filters and outer offsets require detail mode")
        return self
