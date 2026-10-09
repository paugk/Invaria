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


# ------------------------------------------------------------- control diagnosis

CheckStatus = Literal[
    "satisfied", "contradicted", "undetermined", "not_applicable", "not_evaluated"
]
BlockCategory = Literal[
    "missing_evidence",
    "withdrawn_evidence",
    "source_conflict",
    "inconsistent_evidence",
    "invalid_evidence",
    "insufficient_coverage",
    "quarantined_input",
    "unattributed",
    "unsupported_capability",
    "not_yet_due",
    "technical_error",
]
NextStepKind = Literal[
    "provide_observation",
    "obtain_coverage",
    "resolve_source_conflict",
    "review_inconsistent_evidence",
    "resolve_quarantine",
    "provide_approved_link",
    "await_due_time",
    "correct_technical_error",
    "outside_declared_capabilities",
]
DiagnosisBasis = Literal["reconstructed", "unavailable_for_engine", "reconstruction_mismatch"]


class NextStepView(Contract):
    """A deterministic suggestion derived from the block; never a promise of a result."""

    kind: NextStepKind
    description: NonEmptyText
    fact_type: FactType | None
    authoritative_source: Identifier | None
    mapping_ref: VersionRef | None
    min_coverage_level: CoverageLevel | None
    must_cover: NonEmptyText | None
    caveat: NonEmptyText


class RequirementCheckView(Contract):
    requirement_id: Annotated[str, Field(pattern=r"^[a-z_]+(:[a-z_]+)?$", max_length=64)]
    description: NonEmptyText
    applies_when: NonEmptyText
    status: CheckStatus
    # True for the one check whose outcome fixed the control's result.
    determined_result: bool
    category: BlockCategory | None
    reason_code: ReasonCode | None
    explanation: NonEmptyText
    admitted_evidence: Ids
    questioned_evidence: Ids
    set_aside_evidence: Ids
    coverage_ids: Ids
    left: Quantity | None
    right: Quantity | None
    delta: Quantity | None
    next_step: NextStepView | None


class ControlDiagnosisView(Contract):
    """What the engine checked for one control of a stored evaluation, and where it
    stopped; reconstructed by replaying the recorded engine on the closed snapshot."""

    schema_version: Literal["1.0"]
    evaluation_id: Identifier
    operation_ref: Identifier
    snapshot: SnapshotView
    profile_ref: VersionRef
    rules_ref: VersionRef
    engine_ref: VersionRef
    engine_status: Literal["current", "retired", "blocked", "unknown"]
    control_id: Identifier
    mandatory: bool
    status: ControlStatus
    reason_code: ReasonCode
    # PASS or FAIL; UNKNOWN and NOT_APPLICABLE conclude nothing about the obligation.
    concluded: bool
    diagnosis: DiagnosisBasis
    replay_consistent: bool
    requirements: list[RequirementCheckView]
    limitations: list[NonEmptyText]


# ------------------------------------------------------------ operation obligations

ObligationRole = Literal["performance", "timeliness", "term", "applicability_condition"]
ApplicabilityCondition = Literal["always", "unless_cancelled", "cancellation"]
ApplicabilityState = Literal["applies", "not_applicable", "mixed", "not_determined"]
CatalogueStatus = Literal["available", "unavailable_for_profile", "mismatch_with_profile"]
ProfilePath = Annotated[
    str, Field(pattern=r"^[a-z_]+(\[[a-z0-9_.]+\])?(\.[a-z_]+)*$", max_length=128)
]


class ProfileBasisView(Contract):
    """A rule of the profile, read from the evaluation's own profile: what it requires."""

    path: ProfilePath
    value: NonEmptyText


class ObligationControlLink(Contract):
    """How one control informs one obligation; status and reason are the stored ones."""

    control_id: Identifier
    role: ObligationRole
    relation: NonEmptyText
    status: ControlStatus
    reason_code: ReasonCode
    concluded: bool


class ApplicabilityCheckView(Contract):
    """The engine's own check of the obligation's condition in one control;
    status is None when no diagnosis is available."""

    control_id: Identifier
    requirement_id: Annotated[str, Field(pattern=r"^[a-z_]+$", max_length=64)]
    status: CheckStatus | None
    reason_code: ReasonCode | None
    explanation: NonEmptyText


class ApplicabilityView(Contract):
    condition: ApplicabilityCondition
    declared_by: list[ProfileBasisView]
    # Presentation summary of the checks below, never a financial result.
    state: ApplicabilityState
    checks: list[ApplicabilityCheckView]


class ObservedComparisonView(Contract):
    """A comparison the engine recorded. ``observed_only``: it did not fix the control's
    result, so it is not a conclusion about the obligation."""

    control_id: Identifier
    status: CheckStatus
    scope: Literal["decided", "observed_only"]
    left: Quantity | None
    right: Quantity | None
    delta: Quantity | None


class BlockView(Contract):
    """The undetermined check that fixed a linked control's result."""

    control_id: Identifier
    role: ObligationRole
    requirement_id: Annotated[str, Field(pattern=r"^[a-z_]+(:[a-z_]+)?$", max_length=64)]
    category: BlockCategory | None
    reason_code: ReasonCode | None
    explanation: NonEmptyText
    next_step: NextStepView | None


class ObligationView(Contract):
    obligation_id: Annotated[str, Field(pattern=r"^[a-z]+\.[a-z_]+$", max_length=64)]
    description: NonEmptyText
    basis: Annotated[list[ProfileBasisView], Field(min_length=1)]
    applicability: ApplicabilityView
    controls: Annotated[list[ObligationControlLink], Field(min_length=1)]
    observed: list[ObservedComparisonView]
    blocks: list[BlockView]
    evidence_refs: Ids
    coverage_ids: Ids
    not_derivable: list[NonEmptyText]
    limitations: list[NonEmptyText]


class ObligationControlView(Contract):
    """One control of the evaluation, once, with its stored result and requirement diagnosis."""

    control_id: Identifier
    mandatory: bool
    status: ControlStatus
    reason_code: ReasonCode
    concluded: bool
    evidence_refs: Ids
    obligations: list[Annotated[str, Field(pattern=r"^[a-z]+\.[a-z_]+$", max_length=64)]]
    diagnosis: ControlDiagnosisView


class NotProjectedView(Contract):
    """A possible obligation the catalogue leaves out, and why."""

    description: NonEmptyText
    reason: NonEmptyText


class OperationObligationsView(Contract):
    """The obligations a catalogued profile sets for an operation, projected from the
    profile, the closed snapshot and the stored evaluation. Not a second result."""

    schema_version: Literal["1.0"]
    operation_ref: Identifier
    evaluation_id: Identifier
    result: FinancialResult
    operation_state: OperationState | None
    snapshot: SnapshotView
    currency: CurrencyView | None
    versions: VersionsView
    catalogue_ref: VersionRef | None
    catalogue: CatalogueStatus
    diagnosis: DiagnosisBasis
    replay_consistent: bool
    obligations: list[ObligationView]
    controls: list[ObligationControlView]
    not_projected: list[NotProjectedView]
    limitations: list[NonEmptyText]
