"""Snapshot reference, control results and evaluation results.

Result precedence (profile ``BREAK>UNKNOWN>MATCH``), over mandatory applicable controls:
any FAIL -> BREAK; otherwise all PASS -> MATCH; otherwise UNKNOWN. A technical error is
an UNKNOWN reason, never a PASS. A global BREAK keeps its UNKNOWN controls visible.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from invaria.contracts.base import (
    Contract,
    Identifier,
    NonEmptyText,
    SchemaVersion,
    UtcDatetime,
    VersionRef,
)
from invaria.contracts.quantity import Quantity

FinancialResult = Literal["MATCH", "BREAK", "UNKNOWN"]
ControlStatus = Literal["PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE"]

ReasonCode = Literal[
    "EXACT_MATCH",
    "CASH_AMOUNT_MISMATCH",
    "UNITS_MISMATCH",
    "ORDER_TERMS_INCONSISTENT",
    "MISSING_EVIDENCE",
    "INSUFFICIENT_COVERAGE",
    "LOSS_OF_SUPPORT",
    "SOURCE_CONFLICT",
    "AMBIGUOUS_MATCH",
    "QUARANTINED_INPUT",
    "UNSUPPORTED_CAPABILITY",
    "EVALUATION_ERROR",
    "NOT_APPLICABLE_BY_PROFILE",
]

REASONS_BY_STATUS: dict[str, frozenset[str]] = {
    "PASS": frozenset({"EXACT_MATCH"}),
    "FAIL": frozenset({"CASH_AMOUNT_MISMATCH", "UNITS_MISMATCH", "ORDER_TERMS_INCONSISTENT"}),
    "UNKNOWN": frozenset(
        {
            "MISSING_EVIDENCE",
            "INSUFFICIENT_COVERAGE",
            "LOSS_OF_SUPPORT",
            "SOURCE_CONFLICT",
            "AMBIGUOUS_MATCH",
            "QUARANTINED_INPUT",
            "UNSUPPORTED_CAPABILITY",
            "EVALUATION_ERROR",
        }
    ),
    "NOT_APPLICABLE": frozenset({"NOT_APPLICABLE_BY_PROFILE"}),
}

SortedIds = Annotated[list[Identifier], Field(max_length=10_000)]


def _require_sorted_unique(values: list[str], name: str) -> None:
    if values != sorted(set(values)):
        raise ValueError(f"{name} must be sorted and unique")


class SnapshotRef(Contract):
    """Closed snapshot: explicit membership, two times and an explicit evaluation clock.

    Replay uses this membership, never "everything recorded before known_at" re-queried
    later, and never the wall clock.
    """

    schema_version: SchemaVersion
    snapshot_id: Identifier
    tenant_id: Identifier
    operation_ref: Identifier
    valid_at: UtcDatetime
    known_at: UtcDatetime
    evaluation_clock: UtcDatetime
    observation_ids: SortedIds
    coverage_ids: SortedIds
    identity_link_ids: SortedIds
    profile_ref: VersionRef
    rules_ref: VersionRef
    mapping_refs: Annotated[list[VersionRef], Field(min_length=1)]

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        _require_sorted_unique(self.observation_ids, "observation_ids")
        _require_sorted_unique(self.coverage_ids, "coverage_ids")
        _require_sorted_unique(self.identity_link_ids, "identity_link_ids")
        _require_sorted_unique(list(self.mapping_refs), "mapping_refs")
        if self.evaluation_clock < self.known_at:
            raise ValueError("evaluation_clock cannot precede known_at")
        return self


class ControlOutcome(Contract):
    control_id: Identifier
    mandatory: bool
    status: ControlStatus
    reason_code: ReasonCode
    delta: Quantity | None

    @model_validator(mode="after")
    def _reason_matches_status(self) -> Self:
        if self.reason_code not in REASONS_BY_STATUS[self.status]:
            raise ValueError(f"reason_code {self.reason_code} is not valid for {self.status}")
        if self.delta is not None and self.status != "FAIL":
            raise ValueError("delta is only reported for FAIL")
        return self


def aggregate(controls: Iterable[ControlOutcome]) -> FinancialResult:
    """Apply BREAK>UNKNOWN>MATCH over mandatory, applicable controls."""
    statuses = [c.status for c in controls if c.mandatory and c.status != "NOT_APPLICABLE"]
    if "FAIL" in statuses:
        return "BREAK"
    if statuses and all(status == "PASS" for status in statuses):
        return "MATCH"
    return "UNKNOWN"


def _check_controls(controls: list[ControlOutcome], result: FinancialResult) -> None:
    ids = [c.control_id for c in controls]
    if len(set(ids)) != len(ids):
        raise ValueError("control_id must be unique")
    expected = aggregate(controls)
    if expected != result:
        raise ValueError(f"result {result} contradicts control precedence ({expected})")


class ControlResult(ControlOutcome):
    reason: NonEmptyText
    evidence_refs: Annotated[list[Identifier], Field(max_length=1000)]

    @model_validator(mode="after")
    def _evidence(self) -> Self:
        if self.status in {"PASS", "FAIL"} and not self.evidence_refs:
            raise ValueError("PASS and FAIL must cite evidence")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("evidence_refs must be unique")
        return self


class EvaluationVersions(Contract):
    casm: Literal["1.0"]
    profile_ref: VersionRef
    rules_ref: VersionRef
    mapping_refs: Annotated[list[VersionRef], Field(min_length=1)]
    engine_ref: VersionRef


class EvaluationResult(Contract):
    """Immutable evaluation artifact. Currency (current/stale/superseded) is a separate
    projection and never edits this record."""

    schema_version: SchemaVersion
    evaluation_id: Identifier
    snapshot_id: Identifier
    operation_ref: Identifier
    result: FinancialResult
    controls: Annotated[list[ControlResult], Field(min_length=1)]
    versions: EvaluationVersions
    evaluation_clock: UtcDatetime
    assumptions: list[NonEmptyText]

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        _check_controls(list(self.controls), self.result)
        return self


class ExpectedOutcome(Contract):
    """Expected result for a corpus scenario, written before any evaluator exists."""

    result: FinancialResult
    controls: Annotated[list[ControlOutcome], Field(min_length=1)]
    effective_observation_ids: SortedIds

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        _check_controls(list(self.controls), self.result)
        _require_sorted_unique(self.effective_observation_ids, "effective_observation_ids")
        return self


class Scenario(Contract):
    scenario_id: Identifier
    title: NonEmptyText
    timeline_id: Identifier
    query_mode: Literal["as_known", "as_known_now"]
    snapshot: SnapshotRef
    expected: ExpectedOutcome
    rationale: NonEmptyText
    acceptance_refs: list[Identifier]


class ScenarioCatalog(Contract):
    schema_version: SchemaVersion
    corpus_id: Identifier
    synthetic: Literal[True]
    profile_ref: VersionRef
    scenarios: Annotated[list[Scenario], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [s.scenario_id for s in self.scenarios]
        if len(set(ids)) != len(ids):
            raise ValueError("scenario_id must be unique")
        snapshot_ids = [s.snapshot.snapshot_id for s in self.scenarios]
        if len(set(snapshot_ids)) != len(snapshot_ids):
            raise ValueError("snapshot_id must be unique")
        return self
