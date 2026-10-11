"""Domain events v1: what a consumer may learn about a change of verifiable state.

An event is a statement of this service about its own append-only store. It is never an
authorization (every link re-authorizes) and never a guarantee that its conclusion is still
current when read. It carries no raw evidence, evidence identifiers, financial deltas,
credentials, local paths, principals or tenant identifier.

Serialization (schema_version 1.0):
* every field is always present; ``null`` means "not applicable" as documented per field,
  never "unknown"; there are no optional (omitted) fields in v1;
* times are RFC 3339 UTC with ``Z``; numbers are JSON integers only (no fractions);
* identifiers are the store's own (``operation_ref``, ``evaluation_id``, ``ir-<sha256>``);
  links are relative ``/v1`` paths of the existing read API;
* the stored payload is the UTF-8 of ``json.dumps(event, sort_keys=True,
  separators=(",", ":"), ensure_ascii=False)`` (the registry convention), hashed as
  ``payload_sha256``. ``tenant_seq`` and ``derived_at`` are record metadata, outside it.

Compatibility: a persisted event is never rewritten. A change of meaning or shape is a new
``type_version`` (or ``schema_version``), with its own model kept beside the old one;
readers select the model by (schema_version, type, type_version) and must reject an
unknown combination rather than guess.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, TypeAdapter

from invaria.contracts.base import (
    Contract,
    Identifier,
    NonEmptyText,
    Sha256Hex,
    UtcDatetime,
    VersionRef,
    check_strict_json,
)
from invaria.contracts.evaluation import FinancialResult, OperationState

SCHEMA_VERSION: Literal["1.0"] = "1.0"
EventId = Annotated[str, StringConstraints(pattern=r"^ev-[a-f0-9]{64}$")]
ReportRef = Annotated[str, StringConstraints(pattern=r"^ir-[a-f0-9]{64}$")]
SourceType = Literal["scope.invalidated", "evaluation.published", "integrity_report.registered"]
EventType = Literal[
    "operation.scope_reopened", "operation.evaluation_published", "integrity_report.registered"
]
Category = Literal[
    "currency_lost",
    "first_publication",
    "result_changed",
    "controls_changed",
    "cited_evidence_changed",
    "technical_errors_changed",
    "blocks_changed",
    "versions_changed",
    "comparison_incomplete",
]
DimensionStatus = Literal["changed", "unchanged", "not_comparable", "no_previous"]
NotComparableReason = Literal[
    "previous_unidentifiable",  # no unambiguous previous publication can be named
    "profile_changed",  # control sets may differ between profile versions
    "engine_blocked",  # never replayed
    "engine_without_diagnosis",  # not an instrumented engine
    "reconstruction_mismatch",  # the replay does not reproduce the stored result
    "diagnosis_unavailable",  # a pinned input needed by the diagnosis cannot be read
    "limit_reached",  # a configured derivation limit (detail names it)
    "diagnosis_error",  # the diagnosis raised; detail names the exception class
]


class Link(Contract):
    rel: Literal["evaluation", "previous_evaluation", "prior_evaluation", "timeline", "report"]
    href: NonEmptyText  # relative /v1 path; the request is authorized when it is made


class Provenance(Contract):
    """Where the event comes from and when the fact was recorded.

    ``fact_recorded_at`` is the store clock of the fact itself (epoch opened, publication
    committed, report registered), not the derivation time (record ``derived_at``) and not
    an economic time. ``source_payload``: ``pinned`` when the fact's transaction wrote the
    references this event names (current code); ``historical`` when the outbox row predates
    pinned references and those references were read back from append-only history."""

    source: Literal["outbox"]
    source_type: SourceType
    source_payload: Literal["pinned", "historical"]
    fact_recorded_at: UtcDatetime


class OperationSubject(Contract):
    kind: Literal["operation"]
    operation_ref: Identifier


class ReportSubject(Contract):
    kind: Literal["integrity_report"]
    report_id: ReportRef


class PublicationRef(Contract):
    epoch: int
    evaluation_id: Identifier


# ------------------------------------------------------------ operation.scope_reopened


class EvidenceCounts(Contract):
    observations: int
    coverage_certificates: int
    identity_links: int


class PriorPublication(Contract):
    """The latest publication when the epoch opened. ``current_until_reopening`` is true
    only if it belonged to the epoch just closed (otherwise it was already stale)."""

    epoch: int
    evaluation_id: Identifier
    current_until_reopening: bool


class ScopeReopenedData(Contract):
    epoch: int
    cause: Literal["evidence_appended", "profile_changed", "explicit"]
    profile_ref: VersionRef
    previous_profile_ref: VersionRef | None  # only for profile_changed
    evidence_counts: EvidenceCounts | None  # only for evidence_appended
    knowledge_floor: UtcDatetime | None  # latest recorded_at of that evidence (source time)
    # named: the publication below; none: nothing was published before; unidentifiable:
    # the fact's pinned reference contradicts the publication history (none is named and
    # no loss of currency is claimed).
    prior_publication_basis: Literal["named", "none", "unidentifiable"]
    prior_publication: PriorPublication | None


# ------------------------------------------------------ operation.evaluation_published


class SnapshotTimes(Contract):
    snapshot_id: Identifier
    valid_at: UtcDatetime  # economic time of the conclusion
    known_at: UtcDatetime  # knowledge horizon of the closed snapshot
    evaluation_clock: UtcDatetime


class Versions(Contract):
    profile_ref: VersionRef
    rules_ref: VersionRef
    engine_ref: VersionRef


class NotComparable(Contract):
    reason: NotComparableReason
    side: Literal["previous", "current", "both"] | None
    detail: NonEmptyText | None


class ResultChange(Contract):
    status: DimensionStatus
    previous: FinancialResult | None
    current: FinancialResult
    not_comparable: NotComparable | None


class ControlState(Contract):
    control_id: Identifier
    previous: NonEmptyText | None  # "STATUS/REASON_CODE"; null: absent on that side
    current: NonEmptyText | None


class ControlsChange(Contract):
    status: DimensionStatus
    changed: list[ControlState]
    not_comparable: NotComparable | None


class CitedEvidenceChange(Contract):
    """Evidence references cited by controls (``evidence_refs``), counted, never listed."""

    status: DimensionStatus
    added: int
    removed: int
    controls: list[Identifier]  # controls whose cited set changed (same profile only)
    not_comparable: NotComparable | None


class TechnicalErrorsChange(Contract):
    """Controls whose stored ``reason_code`` is ``EVALUATION_ERROR`` (contract: a technical
    error, always UNKNOWN). A change is reported as observed; it is never asserted to be a
    correction of the earlier error."""

    status: DimensionStatus
    previous_controls: list[Identifier] | None
    current_controls: list[Identifier]
    not_comparable: NotComparable | None


class BlockKey(Contract):
    """An engine-recorded block of an UNKNOWN control (the decisive check of its diagnosis)."""

    control_id: Identifier
    requirement_id: NonEmptyText
    category: NonEmptyText | None


class BlocksChange(Contract):
    status: DimensionStatus
    added: list[BlockKey]
    removed: list[BlockKey]
    not_comparable: NotComparable | None


class VersionsChange(Contract):
    status: DimensionStatus
    changed_fields: list[Literal["profile_ref", "rules_ref", "engine_ref"]]
    previous: Versions | None
    not_comparable: NotComparable | None


class ChangeView(Contract):
    """This publication against the previous publication of the same operation (the one
    of highest epoch below it). Stored documents only, except ``blocks`` (diagnosis replay)."""

    basis: Literal["previous_publication", "no_previous", "previous_unidentifiable"]
    result: ResultChange
    controls: ControlsChange
    cited_evidence: CitedEvidenceChange
    technical_errors: TechnicalErrorsChange
    blocks: BlocksChange
    versions: VersionsChange


class EvaluationPublishedData(Contract):
    """``result`` is the stored aggregate of the published evaluation (precedence of
    ``aggregate()``): MATCH only if every mandatory control passed with coverage; BREAK if
    a mandatory control failed; UNKNOWN otherwise, which is not non-payment and not a
    loss. It was current at its publication (compare-and-swap); it may be stale now."""

    epoch: int
    evaluation_id: Identifier
    result: FinancialResult
    operation_state: OperationState | None  # redemption lifecycle; null when not recorded
    snapshot: SnapshotTimes
    versions: Versions
    previous_publication: PublicationRef | None
    change: ChangeView


# --------------------------------------------------------- integrity_report.registered


class ReportRegisteredData(Contract):
    report_sha256: Sha256Hex
    report_format: NonEmptyText
    definition: NonEmptyText
    selection_policy: NonEmptyText
    evidence_horizon: UtcDatetime
    store_read_at: UtcDatetime | None


# ------------------------------------------------------------------------- envelopes


class ScopeReopenedEvent(Contract):
    schema_version: Literal["1.0"]
    event_id: EventId
    type: Literal["operation.scope_reopened"]
    type_version: Literal[1]
    subject: OperationSubject
    provenance: Provenance
    categories: list[Category]
    data: ScopeReopenedData
    links: list[Link]


class EvaluationPublishedEvent(Contract):
    schema_version: Literal["1.0"]
    event_id: EventId
    type: Literal["operation.evaluation_published"]
    type_version: Literal[1]
    subject: OperationSubject
    provenance: Provenance
    categories: list[Category]
    data: EvaluationPublishedData
    links: list[Link]


class ReportRegisteredEvent(Contract):
    schema_version: Literal["1.0"]
    event_id: EventId
    type: Literal["integrity_report.registered"]
    type_version: Literal[1]
    subject: ReportSubject
    provenance: Provenance
    categories: list[Category]
    data: ReportRegisteredData
    links: list[Link]


DomainEvent = Annotated[
    ScopeReopenedEvent | EvaluationPublishedEvent | ReportRegisteredEvent,
    Field(discriminator="type"),
]
AnyEvent = ScopeReopenedEvent | EvaluationPublishedEvent | ReportRegisteredEvent
_EVENT: TypeAdapter[AnyEvent] = TypeAdapter(DomainEvent)


class DomainEventRecord(Contract):
    """A persisted event: the payload plus record metadata the store assigned.
    ``tenant_seq`` is the confirmed derivation order within the tenant: not economic
    order, not the order of the facts, never global."""

    tenant_seq: int
    derived_at: UtcDatetime
    payload_sha256: Sha256Hex
    event: DomainEvent


def event_id(event_type: str, dedup_key: str) -> str:
    """Deterministic: depends only on the type and the outbox ``dedup_key`` of the fact
    (which names tenant, subject and epoch or report), never on ``event_seq``."""
    material = f"invaria-domain-event|{SCHEMA_VERSION}|{event_type}|{dedup_key}"
    return "ev-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def canonical_bytes(
    event: ScopeReopenedEvent | EvaluationPublishedEvent | ReportRegisteredEvent,
) -> bytes:
    text = json.dumps(
        event.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return text.encode("utf-8")


def parse_event(
    payload: bytes,
) -> ScopeReopenedEvent | EvaluationPublishedEvent | ReportRegisteredEvent:
    text = payload.decode("utf-8")
    check_strict_json(text)
    return _EVENT.validate_json(text)
