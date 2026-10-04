"""Versioned, declarative CSV mapping. A mapping is data, never code: no expressions,
no eval, no free transformations. Interpretation choices (scale, unit, status meaning)
are explicit and change only by publishing a new mapping version."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from invaria.contracts.base import Contract, Identifier, SchemaVersion, VersionRef
from invaria.contracts.quantity import MAX_SCALE, Unit

ColumnName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
StatusValue = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,31}$")]
CsvFactType = Literal["order_accepted", "cash_settled", "units_registered"]

REQUIRED_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "order_accepted": frozenset({"account_ref", "units", "cash_amount", "price_per_unit"}),
    "cash_settled": frozenset({"account_ref", "payment_ref", "amount"}),
    "units_registered": frozenset({"account_ref", "units"}),
}
QUANTITY_FIELDS = frozenset({"units", "cash_amount", "price_per_unit", "amount"})


class QuantityField(Contract):
    kind: Literal["quantity"]
    column: ColumnName
    unit: Unit
    unit_column: ColumnName | None
    scale: Annotated[int, Field(ge=0, le=MAX_SCALE)]
    decimals: Literal["exact", "up_to_scale"]
    sign: Literal["non_negative"]


class IdentifierField(Contract):
    kind: Literal["identifier"]
    column: ColumnName


PayloadField = Annotated[QuantityField | IdentifierField, Field(discriminator="kind")]


class StatusSpec(Contract):
    column: ColumnName
    assertion_values: Annotated[list[StatusValue], Field(min_length=1)]
    retraction_values: list[StatusValue]


class CsvLimits(Contract):
    max_bytes: Annotated[int, Field(ge=1, le=100_000_000)]
    max_rows: Annotated[int, Field(ge=1, le=1_000_000)]


class CsvMapping(Contract):
    schema_version: SchemaVersion
    mapping_ref: VersionRef
    parser_ref: VersionRef
    source_id: Identifier
    fact_type: CsvFactType
    synthetic: bool
    encoding: Literal["utf-8"]
    delimiter: Literal[",", ";"]
    columns: Annotated[list[ColumnName], Field(min_length=1, max_length=256)]
    limits: CsvLimits
    record_key_column: ColumnName
    revision_column: ColumnName | None
    operation_ref_column: ColumnName
    valid_time_column: ColumnName
    valid_time_format: Literal["iso8601_utc_z"]
    status: StatusSpec | None
    payload: dict[str, PayloadField]

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("columns must be unique")
        referenced = [
            self.record_key_column,
            self.operation_ref_column,
            self.valid_time_column,
            *([self.revision_column] if self.revision_column else []),
            *([self.status.column] if self.status else []),
        ]
        for spec in self.payload.values():
            referenced.append(spec.column)
            if isinstance(spec, QuantityField) and spec.unit_column:
                referenced.append(spec.unit_column)
        missing = [column for column in referenced if column not in self.columns]
        if missing:
            raise ValueError(f"mapping references columns absent from header: {missing}")
        required = REQUIRED_PAYLOAD_FIELDS[self.fact_type]
        if set(self.payload) != required:
            raise ValueError(f"{self.fact_type} payload needs exactly {sorted(required)}")
        for name, spec in self.payload.items():
            expected = "quantity" if name in QUANTITY_FIELDS else "identifier"
            if spec.kind != expected:
                raise ValueError(f"payload field {name} must be a {expected}")
        if self.status is not None:
            overlap = set(self.status.assertion_values) & set(self.status.retraction_values)
            if overlap:
                raise ValueError(f"status values both assertion and retraction: {overlap}")
            if self.status.retraction_values and self.revision_column is None:
                raise ValueError("retractions need a revision column")
        return self
