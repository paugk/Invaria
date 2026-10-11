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
    Sha256Hex,
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
from invaria.contracts.events import DomainEventRecord
from invaria.contracts.observation import FactType, Observation
from invaria.contracts.quantity import Quantity

# reports:read and reports:register: registered integrity reports.
# events:read: read the tenant's event feed. It reveals the feed's positions and
# volume (every tenant_seq, readable or not), even when the principal cannot read the
# content of some types; each type's content still needs its resource scope.
# disclosures:issue, disclosures:revoke and disclosures:read: issuing a
# view of a report to a third party is its own permission (reading the report does not grant
# it); revoking is another; a recipient reads only views issued to it, and disclosures:read
# grants no other scope.
Scope = Literal[
    "operations:read",
    "evidence:read",
    "reports:read",
    "reports:register",
    "events:read",
    "disclosures:issue",
    "disclosures:revoke",
    "disclosures:read",
]
# A disclosure policy of the code catalogue, name@version. Unknown names are
# accepted here and refused where an output needs them: the refusal is the route's.
PolicyRef = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,62}@[1-9][0-9]{0,5}$")]
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
    # Disclosure policies this principal may receive. Explicit: an output that
    # passes the disclosure boundary is refused when none of these policies covers it.
    # Never chosen by the client; a policy grants no scope.
    disclosure_policies: list[PolicyRef] = Field(default_factory=list, max_length=16)


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
    code: Literal[
        "FORBIDDEN",
        "NOT_FOUND",
        "VALIDATION_ERROR",
        "SOURCE_UNAVAILABLE",
        "REPORT_INCONSISTENT_WITH_STORE",
        "INTEGRITY_ERROR",
        "UNSUPPORTED_REPORT_VERSION",
        "CURSOR_RESET",
        "CURSOR_BEYOND_FEED",
        "UNSUPPORTED_EVENT_VERSION",
        "EVENT_EXCEEDS_MAX_BYTES",
        "DISCLOSURE_POLICY_UNAVAILABLE",
    ]
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


# ------------------------------------------------- operational integrity analytics

MemberStatus = Literal[
    "admitted",
    "published_after_cut",
    "engine_not_admissible",
    # current-publication@2: a retired engine's publication keeps its stored result as
    # history but needs a re-evaluation before it counts as a current conclusion.
    "engine_retired_requires_reevaluation",
    "stale_publication",
    "stored_not_published",
    "registered_not_evaluated",
    "unregistered",
]
EngineStatusName = Literal["current", "retired", "blocked", "unknown"]
UndefinedReason = Literal["denominator_zero", "incomplete"]
RunStop = Literal[
    "earlier_publication_not_fail",
    "profile_changed",
    "engine_changed",
    "unpublished_epoch_gap",
    "earlier_publication_after_cut",
    "no_earlier_publication",
]
CaseReasonKind = Literal[
    "status",
    "result",
    "technical_error",
    "discrepancy",
    "block",
    "not_evaluated",
    "diagnosis_unavailable",
    "reconstruction_mismatch",
    "not_processed",
]
AgeBasis = Literal["published_run_start", "contiguous_fail_run_start"]


def _absent(value: object) -> bool:
    return value is None


FollowUpQuery = Literal[
    "get_timeline", "get_operation_obligations", "diagnose_control", "get_evidence"
]


class PublishedRef(Contract):
    epoch: int
    evaluation_id: Identifier
    evaluation_sha256: Sha256Hex
    known_at: UtcDatetime


class SelectedEvaluation(Contract):
    """The publication the selection policy picked for an operation (admitted or not)."""

    evaluation_id: Identifier
    evaluation_sha256: Sha256Hex
    snapshot_id: Identifier
    published_epoch: int
    valid_at: UtcDatetime
    known_at: UtcDatetime
    evaluation_clock: UtcDatetime
    result: FinancialResult
    profile_ref: VersionRef
    engine_ref: VersionRef
    engine_status: EngineStatusName


class IntegrityMember(Contract):
    """One included operation and the facts of its selection, pinned at the read."""

    operation_ref: Identifier
    status: MemberStatus
    status_detail: list[NonEmptyText]
    profile_ref: VersionRef | None
    scope_epoch: int | None
    stored_evaluations: int
    latest_stored_evaluation_id: Identifier | None
    selected: SelectedEvaluation | None
    published_history: list[PublishedRef]
    epochs_without_publication: list[int]


class MetricView(Contract):
    """A count, or a ratio with both terms; never a bare percentage."""

    metric_id: NonEmptyText
    kind: Literal["count", "ratio"]
    description: NonEmptyText
    numerator: int
    numerator_definition: NonEmptyText
    denominator: int | None
    denominator_definition: NonEmptyText | None
    # Percentage with 2 decimals, ROUND_HALF_EVEN; None when undefined or a count.
    value_percent: str | None
    undefined_reason: UndefinedReason | None
    # UNKNOWN admitted operations left out of this denominator, shown beside it.
    unknown_excluded: int | None
    cases_key: NonEmptyText


class DistributionView(Contract):
    included: int
    admitted: int
    match_count: int
    break_count: int
    unknown_count: int
    technical_error: int
    technical_error_operations: list[Identifier]
    statuses: dict[str, int]
    stored_evaluations: int
    evaluations_not_selected: int
    metrics: list[MetricView]


class SegmentView(Contract):
    profile_ref: VersionRef
    engine_ref: VersionRef
    engine_status: EngineStatusName
    admitted: int
    match_count: int
    break_count: int
    unknown_count: int


class AgeView(Contract):
    """Knowledge-time distance from the snapshot known_at of the run's first publication to
    the report's evidence horizon. Not time since publication, not the age of the evidence,
    not time open. ``published_run_start`` (definition 1.0.0) crossed unpublished epochs;
    ``contiguous_fail_run_start`` (1.1.0) stops at them and at a change of engine."""

    basis: AgeBasis
    origin_evaluation_id: Identifier
    origin_known_at: UtcDatetime
    # None when the origin is after the reference (never a negative age); see the reason.
    age_seconds: int | None
    publications_in_run: int
    epochs_without_publication_in_run: list[int]
    run_stopped_by: RunStop
    age_reference: Literal["evidence_horizon"] | None = Field(default=None, exclude_if=_absent)
    unpublished_epochs_before_run: list[int] | None = Field(default=None, exclude_if=_absent)
    age_undetermined_reason: NonEmptyText | None = Field(default=None, exclude_if=_absent)


class DiscrepancyIncident(Contract):
    operation_ref: Identifier
    evaluation_id: Identifier
    profile_ref: VersionRef
    control_id: Identifier
    mandatory: bool
    reason_code: ReasonCode
    age: AgeView


class ReasonCount(Contract):
    reason_code: ReasonCode
    incidents: int


class DiscrepancyGroup(Contract):
    profile_ref: VersionRef
    control_id: Identifier
    mandatory: bool
    incidents: int
    operations: int
    reason_codes: list[ReasonCount]
    oldest_age_seconds: int | None
    cases_key: NonEmptyText


class DiscrepanciesView(Contract):
    operations_with_discrepancy: int
    control_discrepancies: int
    groups: list[DiscrepancyGroup]
    incidents: list[DiscrepancyIncident]
    metrics: list[MetricView]


class BlockRecord(Contract):
    """The undetermined check that fixed an UNKNOWN control's result."""

    operation_ref: Identifier
    evaluation_id: Identifier
    control_id: Identifier
    requirement_id: NonEmptyText
    category: BlockCategory | None
    reason_code: ReasonCode | None
    fact_type: FactType | None
    # The authoritative source whose evidence the requirement names; not a cause.
    required_from_source: Identifier | None
    evidence_refs: Ids
    coverage_ids: Ids


class ControlRef(Contract):
    operation_ref: Identifier
    evaluation_id: Identifier
    control_id: Identifier
    requirement_id: NonEmptyText | None


class BlockGroup(Contract):
    category: NonEmptyText
    operations: int
    requirements: int
    cases_key: NonEmptyText


class BlockersView(Contract):
    complete: bool
    max_replays: int
    evaluations_requiring_diagnosis: int
    replayed: int
    not_processed: list[Identifier]
    operations_with_block: int
    blocked_requirements: int
    groups: list[BlockGroup]
    blocks: list[BlockRecord]
    not_evaluated: list[ControlRef]
    diagnosis_unavailable: list[ControlRef]
    reconstruction_mismatch: list[ControlRef]
    metrics: list[MetricView]


class ExclusionView(Contract):
    reason: NonEmptyText
    operations: int
    description: NonEmptyText


class ReportScopeView(Contract):
    tenant_id: Identifier
    principal_id: Identifier
    access_scopes: list[Scope]
    population: Literal["registered_accessible_operations"]
    isolation: NonEmptyText


class StoreReadView(Contract):
    isolation: Literal["repeatable_read_read_only"]
    # Store clock, informative only: never compared with known_at, valid_at or as_of.
    read_at: UtcDatetime | None
    closed_knowledge_cut: UtcDatetime | None
    as_of_within_closed_cut: bool


class TemporalView(Contract):
    """What each time of a report means (report format 1.1)."""

    evidence_horizon: UtcDatetime
    evidence_horizon_applies_to: Literal["snapshot_known_at"]
    publication_state: Literal["present_at_store_read"]
    publication_state_read_at: UtcDatetime | None
    publication_state_reconstructed_at_horizon: Literal[False]
    economic_time: Literal["not_filtered"]
    economic_time_reason: NonEmptyText
    age_reference: Literal["evidence_horizon"]
    notes: list[NonEmptyText]


class IntegrityReport(Contract):
    """Reproducible operational integrity report. Not a verified financial bundle.

    ``report_sha256`` is computed over this same content: it detects edits made without a
    new hash, but it does not authenticate the issuer, and anyone can edit the report and
    recompute it. Access never comes from the file: details and reproduction re-check every
    reference against the store of the authenticated scope."""

    schema_version: Literal["1.0"]
    report_format: Literal["invaria-integrity-report@1.0", "invaria-integrity-report@1.1"]
    definition: Literal["integrity-analytics@1.0.0", "integrity-analytics@1.1.0"]
    selection_policy: Literal["current-publication@1", "current-publication@2"]
    selection_description: NonEmptyText
    scope: ReportScopeView
    # The evidence horizon (snapshot known_at <= as_of); an alias of temporal.evidence_horizon
    # in format 1.1. It never means "the publication state at as_of".
    as_of: UtcDatetime
    store_read: StoreReadView
    profile_filter: list[VersionRef] | None
    max_replays: int
    diagnostic_engines: list[VersionRef]
    members: list[IntegrityMember]
    distribution: DistributionView
    segments: list[SegmentView]
    discrepancies: DiscrepanciesView
    blockers: BlockersView
    exclusions: list[ExclusionView]
    limitations: list[NonEmptyText]
    report_sha256: Sha256Hex
    temporal: TemporalView | None = Field(default=None, exclude_if=_absent)


class CaseReason(Contract):
    kind: CaseReasonKind
    control_id: Identifier | None
    requirement_id: NonEmptyText | None
    status: NonEmptyText | None
    reason_code: ReasonCode | None
    category: BlockCategory | None
    evidence_refs: Ids
    coverage_ids: Ids
    # The control's delta as stored; never summed, never a loss.
    delta: Quantity | None
    age: AgeView | None
    required_from_source: Identifier | None


class FollowUpView(Contract):
    query: FollowUpQuery
    args: dict[str, str]


class CaseView(Contract):
    operation_ref: Identifier
    status: MemberStatus
    result: FinancialResult | None
    selected: SelectedEvaluation | None
    criterion: NonEmptyText
    reasons: list[CaseReason]
    follow_up: list[FollowUpView]


class CasesView(Contract):
    report_sha256: Sha256Hex
    cases_key: NonEmptyText
    total_operations: int
    offset: int
    limit: int
    cases: list[CaseView]
    limitations: list[NonEmptyText]
    # Opaque cursor of the next page of a registered report; omitted when none.
    next_cursor: NonEmptyText | None = Field(default=None, exclude_if=_absent)


class ReproductionView(Contract):
    report_sha256: Sha256Hex
    status: Literal["REPRODUCED", "MISMATCH"]
    checked_evaluations: int
    checked_publications: int
    differences: list[NonEmptyText]
    limitations: list[NonEmptyText]


# ------------------------------------------------ registered integrity reports

ReportId = Annotated[str, Field(pattern=r"^ir-[a-f0-9]{64}$")]


class RegistrationView(Contract):
    """Outcome of registering a report computed by this service (never a client file)."""

    report_id: ReportId
    report_sha256: Sha256Hex
    created: bool
    # Store clock at registration; distinct from the evidence horizon and the store read.
    registered_at: UtcDatetime
    document_bytes: int
    limitations: list[NonEmptyText]


class IntegrityReportListItem(Contract):
    report_id: ReportId
    report_sha256: Sha256Hex
    report_format: NonEmptyText
    definition: NonEmptyText
    selection_policy: NonEmptyText
    evidence_horizon: UtcDatetime
    store_read_at: UtcDatetime | None
    registered_at: UtcDatetime


class IntegrityReportListView(Contract):
    """Registered reports of the access profile's tenant, newest registration first. Built
    from registry columns: a listed report is verified only when it is read."""

    schema_version: Literal["1.0"]
    items: list[IntegrityReportListItem]
    next_cursor: NonEmptyText | None
    limitations: list[NonEmptyText]


class DistributionSummary(Contract):
    included: int
    admitted: int
    match_count: int
    break_count: int
    unknown_count: int
    technical_error: int
    statuses: dict[str, int]
    stored_evaluations: int
    evaluations_not_selected: int
    metrics: list[MetricView]


class DiscrepanciesSummary(Contract):
    operations_with_discrepancy: int
    control_discrepancies: int
    groups: list[DiscrepancyGroup]
    metrics: list[MetricView]


class BlockersSummary(Contract):
    complete: bool
    max_replays: int
    evaluations_requiring_diagnosis: int
    replayed: int
    not_processed: int
    operations_with_block: int
    blocked_requirements: int
    groups: list[BlockGroup]
    not_evaluated: int
    diagnosis_unavailable: int
    reconstruction_mismatch: int
    metrics: list[MetricView]


class IntegrityReportSummaryView(Contract):
    """A registered report without its cases: figures with numerator, denominator and cases
    key, versions and times. No member lists, raw evidence, local paths or access-profile
    data (principal, scopes). Projected from the stored document; never recomputed."""

    schema_version: Literal["1.0"]
    report_id: ReportId
    report_sha256: Sha256Hex
    report_format: NonEmptyText
    definition: NonEmptyText
    selection_policy: NonEmptyText
    selection_description: NonEmptyText
    population: Literal["registered_accessible_operations"]
    isolation: NonEmptyText
    evidence_horizon: UtcDatetime
    store_read: StoreReadView
    temporal: TemporalView | None
    registered_at: UtcDatetime
    profile_filter: list[VersionRef] | None
    max_replays: int
    diagnostic_engines: list[VersionRef]
    members: int
    distribution: DistributionSummary
    segments: list[SegmentView]
    discrepancies: DiscrepanciesSummary
    blockers: BlockersSummary
    exclusions: list[ExclusionView]
    case_keys: list[NonEmptyText]
    report_limitations: list[NonEmptyText]
    registry_limitations: list[NonEmptyText]


# ------------------------------------------------------- event feed by cursor

StopReason = Literal["end_observed", "work_limit", "item_limit", "byte_limit", "start_now"]


class EventNotice(Contract):
    """A change of readable types since the cursor was issued (the cursor is authenticated,
    so this compares with what this server issued). ``widened``: events of these types up
    to ``examined_without_delivery_through`` were examined without being delivered; to
    read them, start again from the beginning with ``types`` set to them."""

    kind: Literal["readable_types_widened", "readable_types_narrowed"]
    types: list[NonEmptyText]
    examined_without_delivery_through: int | None


class EventPageView(Contract):
    """One page of the tenant's event feed, in tenant_seq order (derivation order).

    ``examined`` positions run from ``examined_after`` (exclusive) to
    ``continuation_position`` (inclusive): each was delivered or is excluded by the filter
    or by permissions. ``stop_reason``: ``end_observed`` (the head of this read), a work
    limit (``work_limit``: more may follow, even if the page is empty), the item or byte
    limit (the next authorized event was not delivered and is not passed over), or
    ``start_now``. ``observed_head`` reveals the feed's volume (accepted for events:read)."""

    schema_version: Literal["1.0"]
    generation_id: NonEmptyText
    items: list[DomainEventRecord]
    examined_after: int
    continuation_position: int
    examined: int
    delivered: int
    observed_head: int
    stop_reason: StopReason
    caught_up: bool
    next_cursor: NonEmptyText
    filter_types: list[NonEmptyText]
    readable_types: list[NonEmptyText]
    notices: list[EventNotice]
    limitations: list[NonEmptyText]


# ------------------------------------------- controlled disclosure

ViewId = Annotated[str, Field(pattern=r"^dv-[0-9a-f]{32}$")]
# Fixed categories a policy never discloses. The same list is shown for every view of the
# policy: it describes the policy, never the content of a particular report.
WithheldCategory = Literal[
    "source_report_identifiers_and_hashes",
    "access_scope_tenant_and_principals",
    "operations_and_evaluations",
    "financial_profiles_and_engines",
    "assets_amounts_addresses_and_evidence",
    "dates_times_and_ages",
    "filters_and_segments",
    "case_keys_and_links",
    "other_report_sections",
]


class DisclosurePolicyName(Contract):
    policy_id: NonEmptyText
    version: Annotated[int, Field(ge=1)]


class DisclosedOutcomes(Contract):
    """MATCH, BREAK and UNKNOWN of the original report's admitted conclusions, copied."""

    match_count: Annotated[int, Field(ge=0)]
    break_count: Annotated[int, Field(ge=0)]
    unknown_count: Annotated[int, Field(ge=0)]
    denominator: Annotated[int, Field(ge=0)]
    denominator_definition: NonEmptyText


class DisclosedOutside(Contract):
    """Included operations without an admitted conclusion: not a result."""

    operations: Annotated[int, Field(ge=0)]
    definition: NonEmptyText
    denominator: Annotated[int, Field(ge=0)]
    denominator_definition: NonEmptyText


class AggregateDisclosureView(Contract):
    """A restricted projection of a registered report's summary under a disclosure policy.

    A new artifact, not the report: it carries no identifier, hash, date, scope, profile,
    engine, filter, segment or case key of the original. ``view_id`` is random and opaque;
    it neither authenticates the view nor grants access to it."""

    view_format: Literal["invaria-disclosure-view@1"]
    view_id: ViewId
    policy: DisclosurePolicyName
    resource_kind: Literal["integrity_report_summary"]
    modality: Literal["selected_aggregates"]
    counting_definition: NonEmptyText
    included_operations: Annotated[int, Field(ge=0)]
    included_definition: NonEmptyText
    outcomes: DisclosedOutcomes
    outside_outcomes: DisclosedOutside
    withheld: list[WithheldCategory]
    withheld_meaning: NonEmptyText
    statements: list[NonEmptyText]


class DisclosureIssuanceView(Contract):
    """For the issuing operator only (trusted environment): what was bound and the exact view
    the recipient will read."""

    view_id: ViewId
    policy_ref: PolicyRef
    recipient_principal: Identifier
    report_id: ReportId
    issued_at: UtcDatetime
    preview: AggregateDisclosureView
    limitations: list[NonEmptyText]


class DisclosureRevocationView(Contract):
    view_id: ViewId
    revoked_now: bool
    revoked_at: UtcDatetime
    limitations: list[NonEmptyText]


class DisclosureListItem(Contract):
    view_id: ViewId
    policy_ref: PolicyRef


class DisclosureListView(Contract):
    """Views issued to the principal of this access profile, not revoked, newest first."""

    schema_version: Literal["1.0"]
    items: list[DisclosureListItem]
    truncated: bool
    limitations: list[NonEmptyText]
