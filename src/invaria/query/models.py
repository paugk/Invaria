"""Typed answers of the consultative interface. Every conclusion carries its snapshot,
currency and coverage; source content is labelled untrusted."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from invaria.contracts.base import (
    Contract,
    Identifier,
    NonEmptyText,
    SchemaVersion,
    UtcDatetime,
    VersionRef,
)
from invaria.contracts.coverage import CoverageCertificate, CoverageLevel, TimeInterval
from invaria.contracts.evaluation import (
    ControlStatus,
    FinancialResult,
    OperationState,
    ReasonCode,
)
from invaria.contracts.observation import FactType, Observation
from invaria.contracts.quantity import Quantity

Scope = Literal["operations:read", "evidence:read"]
Currency = Literal["current", "stale", "superseded", "not_published"]
Ids = Annotated[list[Identifier], Field(max_length=1000)]

LIMITATION_STORED_ONLY = (
    "Answers come from stored, closed snapshots and evaluations; no source was queried."
)


class AccessProfile(Contract):
    """Who is asking and what they may read. Chosen by the operator, never by the client."""

    schema_version: SchemaVersion
    principal_id: Identifier
    tenant_id: Identifier
    scopes: Annotated[list[Scope], Field(min_length=1)]
    max_items: Annotated[int, Field(ge=1, le=500)]


class SnapshotView(Contract):
    snapshot_id: Identifier
    valid_at: UtcDatetime
    known_at: UtcDatetime
    evaluation_clock: UtcDatetime


class CurrencyView(Contract):
    currency: Currency
    scope_epoch: int | None
    published_epoch: int | None


class ControlView(Contract):
    control_id: Identifier
    mandatory: bool
    status: ControlStatus
    reason_code: ReasonCode
    reason: NonEmptyText
    delta: Quantity | None
    evidence_refs: Ids


class CoverageView(Contract):
    coverage_id: Identifier
    source_id: Identifier
    fact_types: list[FactType]
    level: CoverageLevel
    required_level: CoverageLevel | None
    meets_required_level: bool | None
    interval: TimeInterval
    gaps: int
    records_received: int
    records_quarantined: int


class VersionsView(Contract):
    profile_ref: VersionRef
    rules_ref: VersionRef
    engine_ref: VersionRef
    # current / retired / blocked / unknown under the local policy
    engine_status: Literal["current", "retired", "blocked", "unknown"]


class ConclusionView(Contract):
    evaluation_id: Identifier
    operation_ref: Identifier
    result: FinancialResult
    operation_state: OperationState | None
    snapshot: SnapshotView
    currency: CurrencyView
    controls: list[ControlView]
    coverage: list[CoverageView]
    versions: VersionsView
    limitations: list[NonEmptyText]


class HistoryItem(Contract):
    evaluation_id: Identifier
    result: FinancialResult
    known_at: UtcDatetime
    currency: Currency


class EpochItem(Contract):
    epoch: int
    cause: Literal["registered", "evidence_appended", "profile_changed", "explicit"]
    evidence_ids: Ids


class LegView(Contract):
    fact_type: FactType
    authoritative_source: Identifier
    observation_ids: Ids


class TraceView(Contract):
    operation_ref: Identifier
    watched: bool
    current: ConclusionView | None
    legs: list[LegView]
    history: list[HistoryItem]
    epochs: list[EpochItem]
    truncated: bool
    limitations: list[NonEmptyText]


class Requirement(Contract):
    fact_type: FactType
    authoritative_source: Identifier
    mapping_ref: VersionRef
    min_coverage_level: CoverageLevel
    must_cover: NonEmptyText
    gaps_allowed: bool


class MissingEvidenceView(Contract):
    evaluation_id: Identifier
    control_id: Identifier
    status: ControlStatus
    reason_code: ReasonCode
    missing: bool
    requirements: list[Requirement]
    note: NonEmptyText


class DiscrepancyView(Contract):
    evaluation_id: Identifier
    control_id: Identifier
    status: ControlStatus
    reason_code: ReasonCode
    left_definition: NonEmptyText
    right_definition: NonEmptyText
    left: Quantity | None
    right: Quantity | None
    delta: Quantity | None
    evidence_refs: Ids
    replay_consistent: bool
    snapshot: SnapshotView
    currency: CurrencyView


class ControlChangeView(Contract):
    control_id: Identifier
    before: NonEmptyText
    after: NonEmptyText


class ConclusionChangeView(Contract):
    old_evaluation_id: Identifier
    new_evaluation_id: Identifier
    from_result: FinancialResult
    to_result: FinancialResult
    old_known_at: UtcDatetime
    new_known_at: UtcDatetime
    changed_controls: list[ControlChangeView]
    added_evidence: Ids
    removed_evidence: Ids
    replay_consistent: bool
    limitations: list[NonEmptyText]


class EvidenceView(Contract):
    kind: Literal["observation", "coverage"]
    content_trust: Literal["untrusted_source_data"]
    observation: Observation | None
    coverage: CoverageCertificate | None
    limitations: list[NonEmptyText]


class CoverageReport(Contract):
    operation_ref: Identifier
    evaluation_id: Identifier
    snapshot: SnapshotView
    currency: CurrencyView
    certificates: list[CoverageView]
    unmet: list[NonEmptyText]
    limitations: list[NonEmptyText]


class OperationItem(Contract):
    operation_ref: Identifier
    watched: bool
    evaluation_id: Identifier | None
    result: FinancialResult | None
    currency: Currency | None
    known_at: UtcDatetime | None
    evaluations: int


class OperationsView(Contract):
    items: list[OperationItem]
    result_filter: FinancialResult | None
    truncated: bool
    limitations: list[NonEmptyText]


class EvidenceEntry(Contract):
    """A revision epoch: evidence (or a registration) that invalidated the conclusion."""

    kind: Literal["evidence"]
    at: UtcDatetime | None
    epoch: int
    cause: Literal["registered", "evidence_appended", "profile_changed", "explicit"]
    evidence_ids: Ids


class EvaluationEntry(Contract):
    """A stored evaluation, placed at the knowledge time of its snapshot."""

    kind: Literal["evaluation"]
    at: UtcDatetime
    evaluation_id: Identifier
    result: FinancialResult
    currency: Currency
    valid_at: UtcDatetime
    previous_evaluation_id: Identifier | None
    engine_ref: VersionRef  # evaluations by different engine versions never merge


TimelineEntry = Annotated[EvidenceEntry | EvaluationEntry, Field(discriminator="kind")]


class TimelineView(Contract):
    operation_ref: Identifier
    entries: list[TimelineEntry]
    truncated: bool
    limitations: list[NonEmptyText]


class ControlComparison(Contract):
    control_id: Identifier
    mandatory: bool
    left_status: ControlStatus | None
    right_status: ControlStatus | None
    changed: bool


class ComparisonView(Contract):
    left: ConclusionView
    right: ConclusionView
    controls: list[ControlComparison]
    change: ConclusionChangeView


class QueryErrorView(Contract):
    code: Literal["FORBIDDEN", "NOT_FOUND", "VALIDATION_ERROR", "SOURCE_UNAVAILABLE"]
    message: NonEmptyText
    retryable: bool
