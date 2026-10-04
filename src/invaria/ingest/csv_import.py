"""Deterministic CSV import: bytes + versioned mapping + explicit context -> observations.

Pure function of its inputs: no clock, no network, no filesystem. Nothing is guessed:
anything the mapping does not pin down exactly is quarantined with a reason code.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, ValidationError

from invaria.contracts.base import Contract, Identifier, UtcDatetime
from invaria.contracts.bundle import RelativePath
from invaria.contracts.coverage import CoverageCertificate, CoverageLevel, TimeInterval
from invaria.contracts.mapping import CsvMapping, IdentifierField, QuantityField
from invaria.contracts.observation import (
    FactType,
    Observation,
    Payload,
    Provenance,
    SourceRecord,
)
from invaria.contracts.quantity import Quantity

PARSER_REF = "csv-synthetic-parser@1.0.0"

FileReason = Literal[
    "PARSER_MISMATCH", "LIMIT_EXCEEDED", "ENCODING_ERROR", "MALFORMED_FILE", "SCHEMA_DRIFT"
]
RowReason = Literal[
    "MALFORMED_ROW",
    "MISSING_REQUIRED",
    "REVISION_FORMAT",
    "UNKNOWN_STATUS",
    "TIME_FORMAT",
    "QUANTITY_FORMAT",
    "NEGATIVE_QUANTITY",
    "UNIT_MISMATCH",
    "RETRACTION_WITH_VALUES",
    "CONTRACT_VIOLATION",
]

_UTC_Z = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_REVISION = re.compile(r"^[1-9][0-9]{0,8}$")
_IDENTIFIER: TypeAdapter[str] = TypeAdapter(Identifier)
_PAYLOAD: TypeAdapter[Payload] = TypeAdapter(Payload)


class CoverageClaim(Contract):
    """What the caller asserts about the export as a whole (period and how it was checked)."""

    coverage_id: Identifier
    interval: TimeInterval
    level: CoverageLevel
    method: Annotated[str, Field(min_length=1, max_length=2000)]


class ImportContext(Contract):
    tenant_id: Identifier
    instrument_id: Identifier
    raw_path: RelativePath
    recorded_at: UtcDatetime
    coverage: CoverageClaim | None


@dataclass(frozen=True)
class QuarantinedRow:
    row: int
    reason: RowReason
    detail: str


@dataclass(frozen=True)
class Candidate:
    """A parsed row: an Observation still missing only its ``supersedes`` resolution."""

    row: int
    observation_id: str
    tenant_id: str
    instrument_id: str
    fact_type: FactType
    kind: Literal["assertion", "retraction"]
    operation_ref: str
    source: SourceRecord
    valid_time: datetime
    recorded_at: datetime
    provenance: Provenance
    payload: Payload | None
    synthetic: bool

    def content(self) -> tuple[Any, ...]:
        return (
            self.kind,
            self.fact_type,
            self.instrument_id,
            None,
            self.operation_ref,
            self.valid_time,
            self.payload,
        )

    def to_observation(self, supersedes: str | None) -> Observation:
        return Observation.model_validate(
            {
                "schema_version": "1.0",
                "observation_id": self.observation_id,
                "tenant_id": self.tenant_id,
                "kind": self.kind,
                "fact_type": self.fact_type,
                "instrument_id": self.instrument_id,
                "representation_id": None,
                "operation_ref": self.operation_ref,
                "source": self.source,
                "valid_time": self.valid_time,
                "recorded_at": self.recorded_at,
                "provenance": self.provenance,
                "supersedes": supersedes,
                "payload": self.payload,
                "synthetic": self.synthetic,
            }
        )


@dataclass(frozen=True)
class ParseResult:
    raw_sha256: str
    schema_fingerprint: str | None
    file_reason: FileReason | None
    file_detail: str
    rows_received: int
    candidates: tuple[Candidate, ...]
    quarantined: tuple[QuarantinedRow, ...]

    @property
    def accepted(self) -> bool:
        return self.file_reason is None


class _RowRejected(Exception):
    def __init__(self, reason: RowReason, detail: str) -> None:
        super().__init__(detail)
        self.reason: RowReason = reason
        self.detail = detail


def schema_fingerprint(delimiter: str, header: list[str]) -> str:
    canonical = json.dumps({"delimiter": delimiter, "columns": header}, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _file_failure(
    sha: str, reason: FileReason, detail: str, rows: int = 0, fingerprint: str | None = None
) -> ParseResult:
    return ParseResult(sha, fingerprint, reason, detail, rows, (), ())


def parse_csv(raw: bytes, mapping: CsvMapping, context: ImportContext) -> ParseResult:
    sha = hashlib.sha256(raw).hexdigest()
    if mapping.parser_ref != PARSER_REF:
        return _file_failure(sha, "PARSER_MISMATCH", f"parser is {PARSER_REF}")
    if len(raw) > mapping.limits.max_bytes:
        return _file_failure(sha, "LIMIT_EXCEEDED", f"{len(raw)} bytes")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        return _file_failure(sha, "ENCODING_ERROR", str(error))
    try:
        rows = list(
            csv.reader(io.StringIO(text, newline=""), delimiter=mapping.delimiter, strict=True)
        )
    except csv.Error as error:
        return _file_failure(sha, "MALFORMED_FILE", str(error))
    if not rows:
        return _file_failure(sha, "SCHEMA_DRIFT", "empty file")
    header, data = rows[0], rows[1:]
    fingerprint = schema_fingerprint(mapping.delimiter, header)
    if header != mapping.columns:
        return _file_failure(
            sha, "SCHEMA_DRIFT", f"header {header!r} != mapping columns", len(data), fingerprint
        )
    if len(data) > mapping.limits.max_rows:
        return _file_failure(sha, "LIMIT_EXCEEDED", f"{len(data)} rows", len(data), fingerprint)

    candidates: list[Candidate] = []
    quarantined: list[QuarantinedRow] = []
    for number, row in enumerate(data, start=1):
        try:
            candidates.append(_parse_row(number, row, sha, mapping, context))
        except _RowRejected as rejected:
            quarantined.append(QuarantinedRow(number, rejected.reason, rejected.detail))
    return ParseResult(sha, fingerprint, None, "", len(data), tuple(candidates), tuple(quarantined))


def _identifier(value: str, column: str) -> str:
    if value == "":
        raise _RowRejected("MISSING_REQUIRED", f"{column} is empty")
    try:
        return _IDENTIFIER.validate_python(value)
    except ValidationError as error:
        raise _RowRejected("CONTRACT_VIOLATION", f"{column}: invalid identifier") from error


def _quantity(value: str, spec: QuantityField, values: dict[str, str]) -> Quantity:
    if value == "":
        raise _RowRejected("MISSING_REQUIRED", f"{spec.column} is empty")
    if spec.unit_column is not None and values[spec.unit_column] != spec.unit:
        raise _RowRejected(
            "UNIT_MISMATCH",
            f"{spec.unit_column}={values[spec.unit_column]!r}, expected {spec.unit}",
        )
    try:
        quantity = Quantity.from_decimal_text(value, scale=spec.scale, unit=spec.unit)
    except ValueError as error:
        raise _RowRejected("QUANTITY_FORMAT", f"{spec.column}: {error}") from error
    if spec.decimals == "exact":
        fraction = value.partition(".")[2]
        if len(fraction) != spec.scale:
            raise _RowRejected(
                "QUANTITY_FORMAT",
                f"{spec.column}={value!r} must have exactly {spec.scale} decimals",
            )
    if quantity.atoms.startswith("-"):
        raise _RowRejected("NEGATIVE_QUANTITY", f"{spec.column}={value!r}")
    return quantity


def _parse_row(
    number: int, row: list[str], sha: str, mapping: CsvMapping, context: ImportContext
) -> Candidate:
    if len(row) != len(mapping.columns):
        raise _RowRejected("MALFORMED_ROW", f"{len(row)} fields, expected {len(mapping.columns)}")
    values = dict(zip(mapping.columns, row, strict=True))

    record_key = _identifier(values[mapping.record_key_column], mapping.record_key_column)
    revision = 1
    if mapping.revision_column is not None:
        text = values[mapping.revision_column]
        if not _REVISION.fullmatch(text):
            raise _RowRejected("REVISION_FORMAT", f"revision={text!r}")
        revision = int(text)

    kind: Literal["assertion", "retraction"] = "assertion"
    if mapping.status is not None:
        status = values[mapping.status.column]
        if status in mapping.status.retraction_values:
            kind = "retraction"
        elif status not in mapping.status.assertion_values:
            raise _RowRejected("UNKNOWN_STATUS", f"{mapping.status.column}={status!r}")

    time_text = values[mapping.valid_time_column]
    if not _UTC_Z.fullmatch(time_text):
        raise _RowRejected("TIME_FORMAT", f"{mapping.valid_time_column}={time_text!r}")
    try:
        valid_time = datetime.fromisoformat(time_text)
    except ValueError as error:
        raise _RowRejected("TIME_FORMAT", str(error)) from error

    operation_ref = _identifier(values[mapping.operation_ref_column], mapping.operation_ref_column)

    payload: Payload | None = None
    if kind == "retraction":
        for spec in mapping.payload.values():
            if isinstance(spec, QuantityField) and values[spec.column] != "":
                raise _RowRejected("RETRACTION_WITH_VALUES", f"{spec.column} must be empty")
    else:
        fields: dict[str, Any] = {"payload_type": mapping.fact_type}
        for name, field_spec in mapping.payload.items():
            value = values[field_spec.column]
            if isinstance(field_spec, IdentifierField):
                fields[name] = _identifier(value, field_spec.column)
            else:
                fields[name] = _quantity(value, field_spec, values)
        try:
            payload = _PAYLOAD.validate_python(fields)
        except ValidationError as error:
            raise _RowRejected("CONTRACT_VIOLATION", str(error.errors()[0]["msg"])) from error

    identity = f"{mapping.source_id}|{record_key}|{revision}|{sha}|{number}"
    return Candidate(
        row=number,
        observation_id="obs-" + hashlib.sha256(identity.encode()).hexdigest()[:32],
        tenant_id=context.tenant_id,
        instrument_id=context.instrument_id,
        fact_type=mapping.fact_type,
        kind=kind,
        operation_ref=operation_ref,
        source=SourceRecord(source_id=mapping.source_id, record_key=record_key, revision=revision),
        valid_time=valid_time,
        recorded_at=context.recorded_at,
        provenance=Provenance(
            raw_sha256=sha,
            raw_locator=f"{context.raw_path}#row={number}",
            parser_ref=mapping.parser_ref,
            mapping_ref=mapping.mapping_ref,
        ),
        payload=payload,
        synthetic=mapping.synthetic,
    )


def coverage_for(
    parsed: ParseResult, mapping: CsvMapping, context: ImportContext, rejected_rows: int
) -> CoverageCertificate | None:
    """Coverage only for an accepted file; a quarantined file covers nothing."""
    claim = context.coverage
    if claim is None or not parsed.accepted:
        return None
    return CoverageCertificate.model_validate(
        {
            "schema_version": "1.0",
            "coverage_id": claim.coverage_id,
            "tenant_id": context.tenant_id,
            "source_id": mapping.source_id,
            "fact_types": [mapping.fact_type],
            "instrument_id": context.instrument_id,
            "interval": claim.interval,
            "ledger_range": None,
            "level": claim.level,
            "method": claim.method,
            "records_received": parsed.rows_received,
            "records_quarantined": len(parsed.quarantined) + rejected_rows,
            "gaps": [],
            "raw_sha256": [parsed.raw_sha256],
            "recorded_at": context.recorded_at,
            "synthetic": mapping.synthetic,
        }
    )
