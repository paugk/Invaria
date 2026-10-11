"""Pure construction of domain events v1. No I/O: callers pass pinned documents.

Two separate comparisons:
* stored documents (``compare_documents``): result, controls, cited evidence, technical
  errors and versions, read from the two pinned ``EvaluationResult`` documents. No engine.
* diagnostic reconstruction (``block_signature``): the blocks of UNKNOWN controls, from the
  requirement diagnosis of a replay with the recorded engine, only for instrumented engines
  (``DIAGNOSTIC_ENGINES``) and never for a blocked one; the replay must reproduce the stored
  result. Compatibility implementations of retired engines are never instrumented, so no
  historical reasoning is attributed to them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from invaria.contracts.evaluation import EvaluationResult
from invaria.contracts.events import (
    SCHEMA_VERSION,
    BlockKey,
    BlocksChange,
    Category,
    ChangeView,
    CitedEvidenceChange,
    ControlsChange,
    ControlState,
    DimensionStatus,
    EvaluationPublishedData,
    EvaluationPublishedEvent,
    EvidenceCounts,
    Link,
    NotComparable,
    NotComparableReason,
    OperationSubject,
    PriorPublication,
    Provenance,
    PublicationRef,
    ReportRegisteredData,
    ReportRegisteredEvent,
    ReportSubject,
    ResultChange,
    ScopeReopenedData,
    ScopeReopenedEvent,
    SnapshotTimes,
    TechnicalErrorsChange,
    Versions,
    VersionsChange,
    event_id,
)
from invaria.engine.common import Evaluation, EvaluationInputs
from invaria.engine.versions import DIAGNOSTIC_ENGINES, engine_status
from invaria.query.diagnostics import diagnose

TECHNICAL_ERROR = "EVALUATION_ERROR"


@dataclass(frozen=True)
class Limits:
    """Operational limits of one derivation run (measured)."""

    max_replays_per_run: int = 200
    max_unknown_controls: int = 50
    statement_timeout_ms: int = 5000
    # Waiting for a lock held by another session is contention, not a policy limit: it
    # fails fast as a transient error. 0 disables it (lock waits then count against the
    # statement timeout). Must stay below statement_timeout_ms to keep the distinction.
    lock_timeout_ms: int = 1000

    def __post_init__(self) -> None:
        # A comparison needs up to two replays: a budget of one could never finish one.
        if self.max_replays_per_run != 0 and not 2 <= self.max_replays_per_run <= 100_000:
            raise ValueError("max_replays_per_run must be 0 (disabled) or 2..100000")
        if not 1 <= self.max_unknown_controls <= 10_000:
            raise ValueError("max_unknown_controls must be 1..10000")
        if not 100 <= self.statement_timeout_ms <= 600_000:
            raise ValueError("statement_timeout_ms must be 100..600000")
        if self.lock_timeout_ms != 0 and not 10 <= self.lock_timeout_ms < self.statement_timeout_ms:
            raise ValueError("lock_timeout_ms must be 0 or 10..statement_timeout_ms - 1")


@dataclass
class Budget:
    limits: Limits
    replays: int = 0
    limited: int = field(default=0)


class DiagnosisLimit(Exception):
    """A configured limit stopped a diagnostic load (e.g. the statement timeout)."""


class BudgetExhausted(Exception):
    """The run's replay budget is spent: the run stops and the source stays pending, so
    whether an event is comparable never depends on the size of a batch."""


Loader = Callable[[EvaluationResult], tuple[EvaluationInputs, Evaluation]]


def _versions(doc: EvaluationResult) -> Versions:
    v = doc.versions
    return Versions(profile_ref=v.profile_ref, rules_ref=v.rules_ref, engine_ref=v.engine_ref)


def _nc(
    reason: NotComparableReason, side: str | None = None, detail: str | None = None
) -> NotComparable:
    return NotComparable(reason=reason, side=side, detail=detail)  # type: ignore[arg-type]


def _status(changed: bool) -> DimensionStatus:
    return "changed" if changed else "unchanged"


def _state(doc: EvaluationResult) -> dict[str, str]:
    return {c.control_id: f"{c.status}/{c.reason_code or '-'}" for c in doc.controls}


def _cited(doc: EvaluationResult) -> dict[str, frozenset[str]]:
    return {c.control_id: frozenset(c.evidence_refs) for c in doc.controls}


def _technical(doc: EvaluationResult) -> list[str]:
    return sorted(c.control_id for c in doc.controls if c.reason_code == TECHNICAL_ERROR)


# ------------------------------------------- blocks (requirement-diagnosis replay)


def block_signature(
    doc: EvaluationResult, load: Loader, budget: Budget
) -> frozenset[tuple[str, str, str | None]] | NotComparable:
    """The blocks of a stored evaluation's UNKNOWN controls, or why they are unknown."""
    unknown = [c for c in doc.controls if c.status == "UNKNOWN"]
    if not unknown:
        return frozenset()  # nothing undetermined: no replay needed
    ref = doc.versions.engine_ref
    if engine_status(ref) == "blocked":
        return _nc("engine_blocked", detail=ref)
    if ref not in DIAGNOSTIC_ENGINES:
        return _nc("engine_without_diagnosis", detail=ref)
    if len(unknown) > budget.limits.max_unknown_controls:
        budget.limited += 1
        return _nc("limit_reached", detail="max_unknown_controls")
    if budget.limits.max_replays_per_run == 0:  # block comparison explicitly disabled
        budget.limited += 1
        return _nc("limit_reached", detail="max_replays_per_run=0")
    if budget.replays >= budget.limits.max_replays_per_run:
        raise BudgetExhausted()
    budget.replays += 1
    try:
        inputs, replayed = load(doc)
    except DiagnosisLimit as limit:
        budget.limited += 1
        return _nc("limit_reached", detail=str(limit))
    except KeyError:
        return _nc("diagnosis_unavailable", detail="a pinned input is not stored")
    keys: set[tuple[str, str, str | None]] = set()
    for c in unknown:
        try:
            view = diagnose(doc, inputs, replayed, c.control_id)
        except Exception as error:  # a diagnosis defect never blocks the event
            return _nc("diagnosis_error", detail=type(error).__name__)
        if view.diagnosis == "reconstruction_mismatch":
            return _nc("reconstruction_mismatch", detail=c.control_id)
        if view.diagnosis != "reconstructed":
            return _nc("diagnosis_unavailable", detail=view.diagnosis)
        for r in view.requirements:
            if r.determined_result:
                keys.add((c.control_id, r.requirement_id, r.category))
    return frozenset(keys)


def _blocks(
    previous: EvaluationResult, current: EvaluationResult, load: Loader, budget: Budget
) -> BlocksChange:
    def unavailable(nc: NotComparable, side: str) -> BlocksChange:
        return BlocksChange(
            status="not_comparable",
            added=[],
            removed=[],
            not_comparable=nc.model_copy(update={"side": side}),
        )

    if previous.versions.profile_ref != current.versions.profile_ref:
        return unavailable(_nc("profile_changed"), "both")
    before = block_signature(previous, load, budget)
    if isinstance(before, NotComparable):
        return unavailable(before, "previous")
    after = block_signature(current, load, budget)
    if isinstance(after, NotComparable):
        return unavailable(after, "current")

    def keys(items: frozenset[tuple[str, str, str | None]]) -> list[BlockKey]:
        return [
            BlockKey(control_id=c, requirement_id=r, category=k)
            for c, r, k in sorted(items, key=lambda x: (x[0], x[1], x[2] or ""))
        ]

    return BlocksChange(
        status=_status(before != after),
        added=keys(after - before),
        removed=keys(before - after),
        not_comparable=None,
    )


# ---------------------------------------------------------- stored-document comparison


def compare(
    previous: EvaluationResult | None,
    current: EvaluationResult,
    load: Loader,
    budget: Budget,
    *,
    unidentifiable: str | None = None,
) -> ChangeView:
    """``unidentifiable``: why no previous publication can be named (then every dimension
    is not comparable; nothing is presented as unchanged)."""
    if unidentifiable is not None or previous is None:
        status: DimensionStatus = "no_previous" if unidentifiable is None else "not_comparable"
        nc = (
            None
            if unidentifiable is None
            else _nc("previous_unidentifiable", "previous", unidentifiable)
        )
        return ChangeView(
            basis="no_previous" if unidentifiable is None else "previous_unidentifiable",
            result=ResultChange(
                status=status, previous=None, current=current.result, not_comparable=nc
            ),
            controls=ControlsChange(status=status, changed=[], not_comparable=nc),
            cited_evidence=CitedEvidenceChange(
                status=status, added=0, removed=0, controls=[], not_comparable=nc
            ),
            technical_errors=TechnicalErrorsChange(
                status=status,
                previous_controls=None,
                current_controls=_technical(current),
                not_comparable=nc,
            ),
            blocks=BlocksChange(status=status, added=[], removed=[], not_comparable=nc),
            versions=VersionsChange(
                status=status, changed_fields=[], previous=None, not_comparable=nc
            ),
        )
    same_profile = previous.versions.profile_ref == current.versions.profile_ref
    profile_nc = _nc("profile_changed", "both")
    if same_profile:
        before, after = _state(previous), _state(current)
        changed = [
            ControlState(control_id=c, previous=before.get(c), current=after.get(c))
            for c in sorted(set(before) | set(after))
            if before.get(c) != after.get(c)
        ]
        controls = ControlsChange(
            status=_status(bool(changed)), changed=changed, not_comparable=None
        )
        tech_before, tech_after = _technical(previous), _technical(current)
        technical = TechnicalErrorsChange(
            status=_status(tech_before != tech_after),
            previous_controls=tech_before,
            current_controls=tech_after,
            not_comparable=None,
        )
    else:
        controls = ControlsChange(status="not_comparable", changed=[], not_comparable=profile_nc)
        technical = TechnicalErrorsChange(
            status="not_comparable",
            previous_controls=_technical(previous),
            current_controls=_technical(current),
            not_comparable=profile_nc,
        )
    cited_before, cited_after = _cited(previous), _cited(current)
    union_before = frozenset().union(*cited_before.values())
    union_after = frozenset().union(*cited_after.values())
    moved = (
        sorted(
            c
            for c in set(cited_before) | set(cited_after)
            if cited_before.get(c, frozenset()) != cited_after.get(c, frozenset())
        )
        if same_profile
        else []
    )
    cited = CitedEvidenceChange(
        status=_status(union_before != union_after or bool(moved)),
        added=len(union_after - union_before),
        removed=len(union_before - union_after),
        controls=moved,
        not_comparable=None,
    )
    fields = [
        f
        for f in ("profile_ref", "rules_ref", "engine_ref")
        if getattr(previous.versions, f) != getattr(current.versions, f)
    ]
    return ChangeView(
        basis="previous_publication",
        result=ResultChange(
            status=_status(previous.result != current.result),
            previous=previous.result,
            current=current.result,
            not_comparable=None,
        ),
        controls=controls,
        cited_evidence=cited,
        technical_errors=technical,
        blocks=_blocks(previous, current, load, budget),
        versions=VersionsChange(
            status=_status(bool(fields)),
            changed_fields=fields,  # type: ignore[arg-type]
            previous=_versions(previous),
            not_comparable=None,
        ),
    )


def categories(change: ChangeView) -> list[Category]:
    if change.basis == "no_previous":
        return ["first_publication"]
    found: list[Category] = []
    for name, category in (
        ("result", "result_changed"),
        ("controls", "controls_changed"),
        ("cited_evidence", "cited_evidence_changed"),
        ("technical_errors", "technical_errors_changed"),
        ("blocks", "blocks_changed"),
        ("versions", "versions_changed"),
    ):
        if getattr(change, name).status == "changed":
            found.append(category)  # type: ignore[arg-type]
    if any(
        getattr(change, n).status == "not_comparable"
        for n in ("result", "controls", "cited_evidence", "technical_errors", "blocks", "versions")
    ):
        found.append("comparison_incomplete")
    return found


# ------------------------------------------------------------------------- builders


def _provenance(source_type: str, pinned: bool, at: object) -> Provenance:
    return Provenance(
        source="outbox",
        source_type=source_type,  # type: ignore[arg-type]
        source_payload="pinned" if pinned else "historical",
        fact_recorded_at=at,  # type: ignore[arg-type]
    )


def scope_reopened(
    *,
    dedup_key: str,
    operation_ref: str,
    epoch: int,
    cause: str,
    profile_ref: str,
    previous_profile_ref: str | None,
    evidence_counts: tuple[int, int, int] | None,
    knowledge_floor: object,
    prior: PublicationRef | None,
    pinned: bool,
    reopened_at: object,
    unidentifiable: bool = False,
) -> ScopeReopenedEvent:
    prior_view = (
        None
        if prior is None or unidentifiable
        else PriorPublication(
            epoch=prior.epoch,
            evaluation_id=prior.evaluation_id,
            current_until_reopening=prior.epoch == epoch - 1,
        )
    )
    links = [Link(rel="timeline", href=f"/v1/operations/{operation_ref}/timeline")]
    if prior is not None:
        links.append(Link(rel="prior_evaluation", href=f"/v1/evaluations/{prior.evaluation_id}"))
    return ScopeReopenedEvent(
        schema_version=SCHEMA_VERSION,
        event_id=event_id("operation.scope_reopened", dedup_key),
        type="operation.scope_reopened",
        type_version=1,
        subject=OperationSubject(kind="operation", operation_ref=operation_ref),
        provenance=_provenance("scope.invalidated", pinned, reopened_at),
        categories=["currency_lost"] if prior_view and prior_view.current_until_reopening else [],
        data=ScopeReopenedData(
            epoch=epoch,
            cause=cause,  # type: ignore[arg-type]
            profile_ref=profile_ref,
            previous_profile_ref=previous_profile_ref,
            evidence_counts=None
            if evidence_counts is None
            else EvidenceCounts(
                observations=evidence_counts[0],
                coverage_certificates=evidence_counts[1],
                identity_links=evidence_counts[2],
            ),
            knowledge_floor=knowledge_floor,  # type: ignore[arg-type]
            prior_publication_basis="unidentifiable"
            if unidentifiable
            else ("none" if prior_view is None else "named"),
            prior_publication=prior_view,
        ),
        links=links,
    )


def evaluation_published(
    *,
    dedup_key: str,
    epoch: int,
    current: EvaluationResult,
    snapshot: SnapshotTimes,
    previous: PublicationRef | None,
    change: ChangeView,
    pinned: bool,
    published_at: object,
) -> EvaluationPublishedEvent:
    op = current.operation_ref
    links = [
        Link(rel="evaluation", href=f"/v1/evaluations/{current.evaluation_id}"),
        Link(rel="timeline", href=f"/v1/operations/{op}/timeline"),
    ]
    if previous is not None:
        links.insert(
            1, Link(rel="previous_evaluation", href=f"/v1/evaluations/{previous.evaluation_id}")
        )
    return EvaluationPublishedEvent(
        schema_version=SCHEMA_VERSION,
        event_id=event_id("operation.evaluation_published", dedup_key),
        type="operation.evaluation_published",
        type_version=1,
        subject=OperationSubject(kind="operation", operation_ref=op),
        provenance=_provenance("evaluation.published", pinned, published_at),
        categories=categories(change),
        data=EvaluationPublishedData(
            epoch=epoch,
            evaluation_id=current.evaluation_id,
            result=current.result,
            operation_state=current.operation_state,
            snapshot=snapshot,
            versions=_versions(current),
            previous_publication=previous,
            change=change,
        ),
        links=links,
    )


def report_registered(
    *,
    dedup_key: str,
    report_id: str,
    report_sha256: str,
    report_format: str,
    definition: str,
    selection_policy: str,
    evidence_horizon: object,
    store_read_at: object,
    registered_at: object,
) -> ReportRegisteredEvent:
    return ReportRegisteredEvent(
        schema_version=SCHEMA_VERSION,
        event_id=event_id("integrity_report.registered", dedup_key),
        type="integrity_report.registered",
        type_version=1,
        subject=ReportSubject(kind="integrity_report", report_id=report_id),
        provenance=_provenance("integrity_report.registered", True, registered_at),
        categories=[],
        data=ReportRegisteredData(
            report_sha256=report_sha256,
            report_format=report_format,
            definition=definition,
            selection_policy=selection_policy,
            evidence_horizon=evidence_horizon,  # type: ignore[arg-type]
            store_read_at=store_read_at,  # type: ignore[arg-type]
        ),
        links=[Link(rel="report", href=f"/v1/integrity-reports/{report_id}")],
    )
