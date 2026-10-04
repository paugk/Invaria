"""Coverage certificate: what a source delivered, for which interval, and its gaps.

Absence of a fact can only be argued inside a closed, declared coverage. A hash of what
was received says nothing about what was never received.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from invaria.contracts.base import (
    Contract,
    Identifier,
    NonEmptyText,
    SchemaVersion,
    Sha256Hex,
    UtcDatetime,
)
from invaria.contracts.observation import FactType

CoverageLevel = Literal["provider_claimed", "internally_checked", "independently_verified"]


class TimeInterval(Contract):
    """Half-open interval [start, end) in UTC."""

    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("interval end must be after start")
        return self


class LedgerRange(Contract):
    first: Annotated[int, Field(ge=1)]
    last: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.last < self.first:
            raise ValueError("ledger range last must be >= first")
        return self


class CoverageCertificate(Contract):
    schema_version: SchemaVersion
    coverage_id: Identifier
    tenant_id: Identifier
    source_id: Identifier
    fact_types: Annotated[list[FactType], Field(min_length=1)]
    instrument_id: Identifier
    interval: TimeInterval
    ledger_range: LedgerRange | None
    level: CoverageLevel
    method: NonEmptyText
    records_received: Annotated[int, Field(ge=0)]
    records_quarantined: Annotated[int, Field(ge=0)]
    gaps: list[TimeInterval]
    raw_sha256: list[Sha256Hex]
    recorded_at: UtcDatetime
    synthetic: bool

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if len(set(self.fact_types)) != len(self.fact_types):
            raise ValueError("fact_types must be unique")
        for gap in self.gaps:
            if gap.start < self.interval.start or gap.end > self.interval.end:
                raise ValueError("gaps must lie inside the covered interval")
        if self.records_quarantined > self.records_received:
            raise ValueError("cannot quarantine more records than received")
        return self

    @property
    def is_gap_free(self) -> bool:
        return not self.gaps and self.records_quarantined == 0
