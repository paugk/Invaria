"""Operational integrity analytics. Pure: no I/O.

Counts what the store already holds: the registered operations, their stored evaluations,
the publications that make one of them current, and the checks the engine
records. It never evaluates evidence again, never decides a result and never
infers a cause from free text. Every figure carries its numerator, denominator and cases.

Two stages, so that a report can be reproduced from its pinned references:
1. ``select_members``: the population and the selection policy, over rows read in one
   consistent transaction (facts pinned in the report);
2. ``compute_sections``: distribution, discrepancies and blocks, from the pinned members
   and the immutable stored evaluations (and one replay per evaluation for the blocks).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import BaseModel

from invaria.contracts.evaluation import EvaluationResult
from invaria.engine.common import Evaluation, EvaluationInputs
from invaria.engine.versions import DIAGNOSTIC_ENGINES, engine_status
from invaria.query.diagnostics import diagnose
from invaria.query.models import (
    LIMITATION_STORED_ONLY,
    AgeBasis,
    AgeView,
    BlockersView,
    BlockGroup,
    BlockRecord,
    CaseReason,
    CasesView,
    CaseView,
    ControlRef,
    DiscrepanciesView,
    DiscrepancyGroup,
    DiscrepancyIncident,
    DistributionView,
    ExclusionView,
    FollowUpView,
    IntegrityMember,
    IntegrityReport,
    MemberStatus,
    MetricView,
    PublishedRef,
    ReasonCount,
    ReportScopeView,
    RunStop,
    SegmentView,
    SelectedEvaluation,
    StoreReadView,
    TemporalView,
)

STATUSES_V1: tuple[MemberStatus, ...] = (
    "admitted",
    "published_after_cut",
    "engine_not_admissible",
    "stale_publication",
    "stored_not_published",
    "registered_not_evaluated",
    "unregistered",
)
STATUSES: tuple[MemberStatus, ...] = (
    "admitted",
    "published_after_cut",
    "engine_not_admissible",
    "engine_retired_requires_reevaluation",
    "stale_publication",
    "stored_not_published",
    "registered_not_evaluated",
    "unregistered",
)


@dataclass(frozen=True)
class Rules:
    """The definition, selection policy and report format a report was produced with.
    New reports use ``CURRENT``; a pinned report is always recomputed with its own rules."""

    definition: str
    selection_policy: str
    report_format: str
    selection_description: str
    statuses: tuple[MemberStatus, ...]
    # Status descriptions are part of the definition: a pinned report keeps its own wording.
    meaning_overrides: tuple[tuple[str, str], ...]
    retired_admitted: bool
    run_breaks_on_gap_and_engine: bool
    age_basis: AgeBasis


RULES_V1 = Rules(
    definition="integrity-analytics@1.0.0",
    selection_policy="current-publication@1",
    report_format="invaria-integrity-report@1.0",
    selection_description=(
        "Per operation, the latest publication (highest epoch) of its registered scope "
        "(epochs). It is admitted to the current aggregate only if its epoch is the scope's "
        "current epoch, its engine is current or retired (never blocked or unknown) and its "
        "snapshot known_at is not after as_of. Stale publications, unpublished evaluations and "
        "later unpublished re-evaluations are never counted as current conclusions, and no "
        "earlier evaluation is substituted for one that is not admitted."
    ),
    statuses=STATUSES_V1,
    meaning_overrides=(
        (
            "published_after_cut",
            "its current publication knows evidence recorded after as_of; not replaced by an "
            "earlier one",
        ),
    ),
    retired_admitted=True,
    run_breaks_on_gap_and_engine=False,
    age_basis="published_run_start",
)
RULES_V2 = Rules(
    definition="integrity-analytics@1.1.0",
    selection_policy="current-publication@2",
    report_format="invaria-integrity-report@1.1",
    selection_description=(
        "Per operation, the latest publication (highest epoch) of its registered scope "
        "(epochs), read from the present store state. It is admitted to the current aggregate "
        "only if its epoch is the scope's current epoch, its engine is current, and its snapshot "
        "known_at is within the evidence horizon. A publication of a retired engine keeps its "
        "stored result as history and is counted apart as requiring re-evaluation (nothing is "
        "re-evaluated); blocked or unknown engines are not admissible. Stale publications, "
        "unpublished evaluations and later unpublished re-evaluations are never counted as "
        "current conclusions, and no earlier evaluation is substituted for one that is not "
        "admitted."
    ),
    statuses=STATUSES,
    meaning_overrides=(),
    retired_admitted=False,
    run_breaks_on_gap_and_engine=True,
    age_basis="contiguous_fail_run_start",
)
CURRENT = RULES_V2
DEFINITION = CURRENT.definition
REPORT_FORMAT = CURRENT.report_format
SELECTION_POLICY = CURRENT.selection_policy


def rules_of(report: IntegrityReport) -> Rules:
    for rules in (RULES_V1, RULES_V2):
        if (report.definition, report.selection_policy, report.report_format) == (
            rules.definition,
            rules.selection_policy,
            rules.report_format,
        ):
            return rules
    raise ValueError(
        f"unsupported combination {report.definition}, {report.selection_policy}, "
        f"{report.report_format}"
    )


STATUS_MEANING: dict[str, str] = {
    "admitted": "a current publication admitted to the aggregate",
    "published_after_cut": (
        "its current publication's snapshot knows evidence beyond the evidence horizon; not "
        "replaced by an earlier one"
    ),
    "engine_not_admissible": "its current publication names a blocked or unknown engine",
    "engine_retired_requires_reevaluation": (
        "its current publication was recorded with an engine retired since: the stored result "
        "is history and needs a re-evaluation before it counts (never re-evaluated here, never "
        "an UNKNOWN or a technical error)"
    ),
    "stale_publication": (
        "its latest publication belongs to an earlier epoch: the scope was reopened since; "
        "the stored result is history, not a current conclusion"
    ),
    "stored_not_published": "evaluations are stored but none was published",
    "registered_not_evaluated": (
        "registered scope with no stored evaluation; no execution state is recorded, so it "
        "is not reported as pending"
    ),
    "unregistered": "snapshots are stored but no scope is registered, so nothing publishes",
}
DEFAULT_MAX_REPLAYS = 100
ECONOMIC_TIME_REASON = (
    "The operation registry stores no economic date of the operation; filtering by the "
    "selected evaluation's valid_at would silently drop operations without an evaluation."
)
TEMPORAL_NOTES = [
    "Evidence horizon: a publication is admitted only if its snapshot known_at is within it; "
    "it is knowledge time, compared only with known_at, never with valid_at or a store clock.",
    "Publication state: the present one, read in one transaction at publication_state_read_at "
    "(store clock, informative). It is not reconstructed at the horizon, and an admitted "
    "publication is not claimed to have existed at the horizon.",
    "Evaluation and publication times: snapshot known_at, evaluation_clock and valid_at are "
    "shown per case; publication store times are not used by any figure.",
    "Ages: from the snapshot known_at of the first publication of a contiguous FAIL run to the "
    "evidence horizon, both knowledge time; never negative (undetermined with a reason).",
]
ISOLATION = (
    "Every read is filtered by the access profile's tenant before counting; counts, members "
    "and details never include operations of another tenant. No new isolation model."
)
LIMITATIONS = [
    LIMITATION_STORED_ONLY,
    "Population: registered accessible operations (registered scopes or stored snapshots of "
    "this tenant). It is not all real activity: an operation never registered is not here.",
    "Publication state is the present one, read at store_read.read_at (store clock); as_of is "
    "only the evidence horizon applied to each snapshot's known_at. The report does not "
    "reconstruct which conclusion was published at as_of, and does not claim that an admitted "
    "publication already existed then.",
    "report_sha256 is computed over the report's own content: it detects edits made without a "
    "new hash, but it does not authenticate the issuer, and the report can be edited and "
    "re-hashed. Details and reproduction re-check every reference against the store of the "
    "authenticated scope; a coherent subset of a legitimate report cannot be told apart from "
    "another legitimate report without an external anchor.",
    "UNKNOWN means not determined from the snapshot, never a breach or a missing payment; "
    "BREAK is a mandatory control failing under the profile, not an economic loss; MATCH is "
    "agreement under the controls applied, not a general guarantee.",
    "Counts only: no amounts are summed, converted or called loss, exposure or blocked funds.",
    "This JSON is a reproducible query report, not a verified financial bundle.",
]


@dataclass(frozen=True)
class EpochRow:
    operation_ref: str
    epoch: int
    profile_ref: str
    cause: str


@dataclass(frozen=True)
class SnapshotRow:
    operation_ref: str
    snapshot_id: str
    profile_ref: str
    known_at: datetime
    evaluation_clock: datetime


@dataclass(frozen=True)
class EvaluationRow:
    evaluation_id: str
    operation_ref: str
    snapshot_id: str
    profile_ref: str
    valid_at: datetime
    known_at: datetime
    evaluation_clock: datetime
    result: str
    engine_ref: str


@dataclass(frozen=True)
class PublicationRow:
    operation_ref: str
    epoch: int
    evaluation_id: str


@dataclass(frozen=True)
class AttemptRow:
    evaluation_id: str
    epoch: int
    outcome: str


@dataclass(frozen=True)
class IntegrityState:
    """What one consistent read of a tenant's store returns. ``documents`` holds every
    published evaluation (selected ones and their earlier publications)."""

    tenant_id: str
    epochs: Sequence[EpochRow]
    snapshots: Sequence[SnapshotRow]
    evaluations: Sequence[EvaluationRow]
    publications: Sequence[PublicationRow]
    attempts: Sequence[AttemptRow]
    documents: Mapping[str, EvaluationResult]
    read_at: datetime | None
    closed_cut: datetime | None


Diagnoser = Callable[[EvaluationResult], tuple[EvaluationInputs, Evaluation]]


def content_sha256(model: BaseModel) -> str:
    """sha256 of the compact, key-sorted JSON of a model (integrity, not a signature)."""
    text = json.dumps(
        model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def report_sha256(report: IntegrityReport | dict[str, Any]) -> str:
    document = report.model_dump(mode="json") if isinstance(report, BaseModel) else dict(report)
    document.pop("report_sha256", None)
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------- stage 1: selection


def select_members(
    state: IntegrityState,
    as_of: datetime,
    profile_refs: Iterable[str] | None,
    rules: Rules = CURRENT,
) -> tuple[list[IntegrityMember], list[ExclusionView]]:
    """Every operation of the tenant once, with its selection pinned. ``as_of`` is the
    evidence horizon, compared only with snapshot known_at."""
    wanted = None if profile_refs is None else set(profile_refs)
    epochs: dict[str, list[EpochRow]] = defaultdict(list)
    for e in state.epochs:
        epochs[e.operation_ref].append(e)
    snapshots: dict[str, list[SnapshotRow]] = defaultdict(list)
    for s in state.snapshots:
        snapshots[s.operation_ref].append(s)
    stored: dict[str, list[EvaluationRow]] = defaultdict(list)
    for r in state.evaluations:
        stored[r.operation_ref].append(r)
    rows = {r.evaluation_id: r for r in state.evaluations}
    published: dict[str, list[PublicationRow]] = defaultdict(list)
    for p in state.publications:
        published[p.operation_ref].append(p)
    refused = {a.evaluation_id for a in state.attempts if a.outcome == "NOT_CURRENT"}

    members: list[IntegrityMember] = []
    filtered = 0
    for op in sorted(set(epochs) | set(snapshots)):
        scope = sorted(epochs.get(op, []), key=lambda e: e.epoch)
        own = sorted(
            stored.get(op, []), key=lambda r: (r.known_at, r.evaluation_clock, r.evaluation_id)
        )
        head = scope[-1] if scope else None
        if head is not None:
            profile: str | None = head.profile_ref
        else:
            latest = max(snapshots[op], key=lambda s: (s.known_at, s.evaluation_clock))
            profile = latest.profile_ref
        if wanted is not None and profile not in wanted:
            filtered += 1
            continue
        pubs = sorted(published.get(op, []), key=lambda p: p.epoch)
        detail: list[str] = []
        selected: SelectedEvaluation | None = None
        history: list[PublishedRef] = []
        status: MemberStatus
        if head is None:
            status = "unregistered"
            detail.append("with_evaluations" if own else "snapshots_only")
        elif not own:
            status = "registered_not_evaluated"
        elif not pubs:
            status = "stored_not_published"
            detail.append(
                "publication_refused"
                if any(r.evaluation_id in refused for r in own)
                else "no_publication_attempt_recorded"
            )
        else:
            last = pubs[-1]
            row = rows[last.evaluation_id]
            engine = engine_status(row.engine_ref)
            selected = SelectedEvaluation(
                evaluation_id=row.evaluation_id,
                evaluation_sha256=content_sha256(state.documents[row.evaluation_id]),
                snapshot_id=row.snapshot_id,
                published_epoch=last.epoch,
                valid_at=row.valid_at,
                known_at=row.known_at,
                evaluation_clock=row.evaluation_clock,
                result=row.result,  # type: ignore[arg-type]
                profile_ref=row.profile_ref,
                engine_ref=row.engine_ref,
                engine_status=engine,
            )
            history = [
                PublishedRef(
                    epoch=p.epoch,
                    evaluation_id=p.evaluation_id,
                    evaluation_sha256=content_sha256(state.documents[p.evaluation_id]),
                    known_at=rows[p.evaluation_id].known_at,
                )
                for p in pubs
            ]
            if last.epoch != head.epoch:
                status = "stale_publication"
                detail += sorted({e.cause for e in scope if e.epoch > last.epoch})
            elif engine not in ("current", "retired"):
                status = "engine_not_admissible"
                detail.append(engine)
            elif engine == "retired" and not rules.retired_admitted:
                status = "engine_retired_requires_reevaluation"
                detail.append(row.engine_ref)
            elif row.known_at > as_of:
                status = "published_after_cut"
            else:
                status = "admitted"
        done = {p.epoch for p in pubs}
        members.append(
            IntegrityMember(
                operation_ref=op,
                status=status,
                status_detail=detail,
                profile_ref=profile,
                scope_epoch=head.epoch if head else None,
                stored_evaluations=len(own),
                latest_stored_evaluation_id=own[-1].evaluation_id if own else None,
                selected=selected,
                published_history=history,
                epochs_without_publication=[e.epoch for e in scope if e.epoch not in done],
            )
        )
    exclusions = []
    if filtered:
        exclusions.append(
            ExclusionView(
                reason="profile_filter",
                operations=filtered,
                description="operations whose profile is not in the requested profile filter",
            )
        )
    return members, exclusions


# -------------------------------------------------------------- stage 2: the figures


def metric(
    metric_id: str,
    description: str,
    numerator: int,
    numerator_definition: str,
    denominator: int | None,
    denominator_definition: str | None,
    cases_key: str,
    *,
    incomplete: bool = False,
    unknown_excluded: int | None = None,
) -> MetricView:
    value: str | None = None
    undefined: str | None = None
    if denominator is not None:
        if incomplete:
            undefined = "incomplete"
        elif denominator == 0:
            undefined = "denominator_zero"
        else:
            percent = Decimal(numerator) * 100 / Decimal(denominator)
            value = str(percent.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))
    return MetricView(
        metric_id=metric_id,
        kind="count" if denominator is None else "ratio",
        description=description,
        numerator=numerator,
        numerator_definition=numerator_definition,
        denominator=denominator,
        denominator_definition=denominator_definition,
        value_percent=value,
        undefined_reason=undefined,  # type: ignore[arg-type]
        unknown_excluded=unknown_excluded,
        cases_key=cases_key,
    )


def _technical_error(document: EvaluationResult) -> bool:
    return any(c.reason_code == "EVALUATION_ERROR" for c in document.controls)


def _distribution(
    members: Sequence[IntegrityMember],
    documents: Mapping[str, EvaluationResult],
    rules: Rules,
) -> DistributionView:
    statuses_of = rules.statuses
    meaning = {**STATUS_MEANING, **dict(rules.meaning_overrides)}
    admitted = [m for m in members if m.status == "admitted"]
    results = Counter(m.selected.result for m in admitted if m.selected)
    match, brk, unknown = results["MATCH"], results["BREAK"], results["UNKNOWN"]
    technical_ops = [
        m.operation_ref
        for m in admitted
        if m.selected and _technical_error(documents[m.selected.evaluation_id])
    ]
    technical = len(technical_ops)
    statuses = Counter(m.status for m in members)
    included, n = len(members), len(admitted)
    stored = sum(m.stored_evaluations for m in members)
    selected = sum(1 for m in members if m.selected is not None)
    adm, inc = "admitted operations", "included operations"
    metrics = [
        metric(
            "A.included",
            "operations in the population after filters",
            included,
            inc,
            None,
            None,
            "included",
        ),
        metric(
            "A.admitted_share",
            "share of included operations with an admitted conclusion",
            n,
            adm,
            included,
            inc,
            "status:admitted",
        ),
        metric(
            "A.match_share_of_admitted",
            "MATCH among admitted operations",
            match,
            "admitted MATCH",
            n,
            adm,
            "result:MATCH",
        ),
        metric(
            "A.break_share_of_admitted",
            "BREAK among admitted operations",
            brk,
            "admitted BREAK",
            n,
            adm,
            "result:BREAK",
        ),
        metric(
            "A.unknown_share_of_admitted",
            "UNKNOWN among admitted operations",
            unknown,
            "admitted UNKNOWN",
            n,
            adm,
            "result:UNKNOWN",
        ),
        metric(
            "A.conclusive_share_of_admitted",
            "MATCH or BREAK among admitted operations",
            match + brk,
            "admitted MATCH + BREAK",
            n,
            adm,
            "conclusive",
        ),
        metric(
            "A.match_share_of_conclusive",
            "MATCH among conclusive operations; the UNKNOWN left out are shown beside it",
            match,
            "admitted MATCH",
            match + brk,
            "admitted MATCH + BREAK",
            "result:MATCH",
            unknown_excluded=unknown,
        ),
        metric(
            "A.match_share_of_included",
            "MATCH among all included operations",
            match,
            "admitted MATCH",
            included,
            inc,
            "result:MATCH",
        ),
        metric(
            "A.technical_error_share_of_admitted",
            "admitted conclusions with a control in EVALUATION_ERROR (technical state, "
            "reported apart from the financial result)",
            technical,
            "admitted with EVALUATION_ERROR",
            n,
            adm,
            "technical_error",
        ),
    ]
    metrics += [
        metric(
            f"A.status:{s}",
            meaning[s],
            statuses[s],
            f"operations {s}",
            included,
            inc,
            f"status:{s}",
        )
        for s in statuses_of
    ]
    return DistributionView(
        included=included,
        admitted=n,
        match_count=match,
        break_count=brk,
        unknown_count=unknown,
        technical_error=technical,
        technical_error_operations=technical_ops,
        statuses={s: statuses[s] for s in statuses_of},
        stored_evaluations=stored,
        evaluations_not_selected=stored - selected,
        metrics=metrics,
    )


def _segments(members: Sequence[IntegrityMember]) -> list[SegmentView]:
    # The engine status pinned at selection, never today's: a reproduction keeps its counts.
    groups: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    for m in members:
        if m.status == "admitted" and m.selected:
            s = m.selected
            groups[(s.profile_ref, s.engine_ref, s.engine_status)][s.result] += 1
    return [
        SegmentView(
            profile_ref=profile,
            engine_ref=engine,
            engine_status=status,  # type: ignore[arg-type]
            admitted=sum(c.values()),
            match_count=c["MATCH"],
            break_count=c["BREAK"],
            unknown_count=c["UNKNOWN"],
        )
        for (profile, engine, status), c in sorted(groups.items())
    ]


def _age(
    member: IntegrityMember,
    control_id: str,
    documents: Mapping[str, EvaluationResult],
    known_at: Mapping[str, datetime],
    horizon: datetime,
    rules: Rules,
) -> AgeView:
    """Walk the member's publications back from the selected one while the control is FAIL
    under the same profile. With 1.1.0 rules the run also stops at an unpublished epoch (its
    state is unknown: persistence is never claimed across it) and at a change of engine
    (another engine may give the control another meaning). Origin and reference are both in
    knowledge time: the origin's snapshot known_at and the evidence horizon."""
    assert member.selected is not None
    selected = member.selected
    chain = [p for p in member.published_history if p.epoch <= selected.published_epoch]
    unpublished = set(member.epochs_without_publication)
    run = [chain[-1]]
    stop: RunStop = "no_earlier_publication"
    gap_before: list[int] | None = None
    strict = rules.run_breaks_on_gap_and_engine
    for earlier in reversed(chain[:-1]):
        document = documents[earlier.evaluation_id]
        between = sorted(e for e in unpublished if earlier.epoch < e < run[-1].epoch)
        if strict and between:
            stop, gap_before = "unpublished_epoch_gap", between
            break
        if document.versions.profile_ref != selected.profile_ref:
            stop = "profile_changed"
            break
        if strict and document.versions.engine_ref != selected.engine_ref:
            stop = "engine_changed"
            break
        if known_at[earlier.evaluation_id] > horizon:
            stop = "earlier_publication_after_cut"
            break
        control = next((c for c in document.controls if c.control_id == control_id), None)
        if control is None or control.status != "FAIL":
            stop = "earlier_publication_not_fail"
            break
        run.append(earlier)
    origin = run[-1]
    origin_known = known_at[origin.evaluation_id]
    gaps = [e for e in unpublished if origin.epoch < e < selected.published_epoch]
    seconds: int | None = int((horizon - origin_known).total_seconds())
    reason = None
    if seconds is not None and seconds < 0:
        seconds, reason = None, "the run's origin known_at is after the evidence horizon"
    return AgeView(
        basis=rules.age_basis,
        origin_evaluation_id=origin.evaluation_id,
        origin_known_at=origin_known,
        age_seconds=seconds,
        publications_in_run=len(run),
        epochs_without_publication_in_run=sorted(gaps),
        run_stopped_by=stop,
        age_reference="evidence_horizon" if strict else None,
        unpublished_epochs_before_run=gap_before if strict else None,
        age_undetermined_reason=reason,
    )


def _discrepancies(
    members: Sequence[IntegrityMember],
    documents: Mapping[str, EvaluationResult],
    known_at: Mapping[str, datetime],
    as_of: datetime,
    admitted: int,
    rules: Rules,
) -> DiscrepanciesView:
    incidents: list[DiscrepancyIncident] = []
    for m in members:
        if m.status != "admitted" or m.selected is None:
            continue
        document = documents[m.selected.evaluation_id]
        for c in document.controls:
            if c.status == "FAIL":
                incidents.append(
                    DiscrepancyIncident(
                        operation_ref=m.operation_ref,
                        evaluation_id=m.selected.evaluation_id,
                        profile_ref=m.selected.profile_ref,
                        control_id=c.control_id,
                        mandatory=c.mandatory,
                        reason_code=c.reason_code,
                        age=_age(m, c.control_id, documents, known_at, as_of, rules),
                    )
                )
    incidents.sort(
        key=lambda i: (
            i.profile_ref,
            i.control_id,
            i.age.age_seconds is None,
            -(i.age.age_seconds or 0),
            i.operation_ref,
        )
    )
    by_group: dict[tuple[str, str], list[DiscrepancyIncident]] = defaultdict(list)
    for i in incidents:
        by_group[(i.profile_ref, i.control_id)].append(i)
    groups = [
        DiscrepancyGroup(
            profile_ref=profile,
            control_id=control,
            mandatory=any(i.mandatory for i in items),
            incidents=len(items),
            operations=len({i.operation_ref for i in items}),
            reason_codes=[
                ReasonCount(reason_code=code, incidents=count)
                for code, count in sorted(Counter(i.reason_code for i in items).items())
            ],
            oldest_age_seconds=max(
                (i.age.age_seconds for i in items if i.age.age_seconds is not None), default=None
            ),
            cases_key=f"discrepancy:{profile}/{control}",
        )
        for (profile, control), items in sorted(by_group.items())
    ]
    affected = len({i.operation_ref for i in incidents})
    return DiscrepanciesView(
        operations_with_discrepancy=affected,
        control_discrepancies=len(incidents),
        groups=groups,
        incidents=incidents,
        metrics=[
            metric(
                "B.operations_with_discrepancy_share_of_admitted",
                "admitted operations with at least one control FAIL (unique operations)",
                affected,
                "admitted operations with >= 1 FAIL",
                admitted,
                "admitted operations",
                "discrepancy:any",
            ),
            metric(
                "B.control_discrepancies",
                "controls in FAIL across admitted conclusions (incidents, not operations)",
                len(incidents),
                "FAIL controls",
                None,
                None,
                "discrepancy:any",
            ),
        ],
    )


def _blockers(
    members: Sequence[IntegrityMember],
    documents: Mapping[str, EvaluationResult],
    diagnoser: Diagnoser,
    max_replays: int,
    admitted: int,
) -> BlockersView:
    blocks: list[BlockRecord] = []
    not_evaluated: list[ControlRef] = []
    unavailable: list[ControlRef] = []
    mismatch: list[ControlRef] = []
    not_processed: list[str] = []
    requiring = replayed = 0
    for m in members:
        if m.status != "admitted" or m.selected is None:
            continue
        document = documents[m.selected.evaluation_id]
        unknown = [c for c in document.controls if c.status == "UNKNOWN"]
        if not unknown:
            continue
        requiring += 1
        ref = {"operation_ref": m.operation_ref, "evaluation_id": document.evaluation_id}
        if (
            document.versions.engine_ref not in DIAGNOSTIC_ENGINES
            or engine_status(document.versions.engine_ref) == "blocked"
        ):
            # No diagnosis exists for this engine, or it is blocked today: never replayed
            # for Analytics, not even to reproduce a pinned report.
            unavailable += [
                ControlRef(**ref, control_id=c.control_id, requirement_id=None) for c in unknown
            ]
            continue
        if replayed >= max_replays:
            not_processed.append(m.operation_ref)
            continue
        replayed += 1
        inputs, replay = diagnoser(document)
        for c in unknown:
            view = diagnose(document, inputs, replay, c.control_id)
            if view.diagnosis != "reconstructed":
                target = mismatch if view.diagnosis == "reconstruction_mismatch" else unavailable
                target.append(ControlRef(**ref, control_id=c.control_id, requirement_id=None))
                continue
            for r in view.requirements:
                if r.status == "not_evaluated":
                    not_evaluated.append(
                        ControlRef(**ref, control_id=c.control_id, requirement_id=r.requirement_id)
                    )
                elif r.determined_result:
                    step = r.next_step
                    blocks.append(
                        BlockRecord(
                            **ref,
                            control_id=c.control_id,
                            requirement_id=r.requirement_id,
                            category=r.category,
                            reason_code=r.reason_code,
                            fact_type=step.fact_type if step else None,
                            required_from_source=step.authoritative_source if step else None,
                            evidence_refs=sorted(
                                {
                                    *r.admitted_evidence,
                                    *r.questioned_evidence,
                                    *r.set_aside_evidence,
                                }
                            ),
                            coverage_ids=sorted(r.coverage_ids),
                        )
                    )
    by_category: dict[str, list[BlockRecord]] = defaultdict(list)
    for b in blocks:
        by_category[b.category or "uncategorised"].append(b)
    groups = [
        BlockGroup(
            category=category,
            operations=len({b.operation_ref for b in items}),
            requirements=len(items),
            cases_key=f"block:{category}",
        )
        for category, items in sorted(by_category.items())
    ]
    complete = not not_processed
    with_block = len({b.operation_ref for b in blocks})
    return BlockersView(
        complete=complete,
        max_replays=max_replays,
        evaluations_requiring_diagnosis=requiring,
        replayed=replayed,
        not_processed=not_processed,
        operations_with_block=with_block,
        blocked_requirements=len(blocks),
        groups=groups,
        blocks=blocks,
        not_evaluated=not_evaluated,
        diagnosis_unavailable=unavailable,
        reconstruction_mismatch=mismatch,
        metrics=[
            metric(
                "C.operations_with_block_share_of_admitted",
                "admitted operations with at least one block recorded by the engine "
                "(unique operations; an operation may be in several categories)",
                with_block,
                "admitted operations with >= 1 engine-recorded block",
                admitted,
                "admitted operations",
                "block:any",
                incomplete=not complete,
            ),
            metric(
                "C.blocked_requirements",
                "engine-recorded blocks (one per UNKNOWN control diagnosed)",
                len(blocks),
                "blocks",
                None,
                None,
                "block:any",
            ),
            metric(
                "C.not_evaluated_requirements",
                "requirements the engine did not reach; never counted as blocks",
                len(not_evaluated),
                "not evaluated requirements",
                None,
                None,
                "not_evaluated",
            ),
            metric(
                "C.diagnosis_unavailable_controls",
                "UNKNOWN controls of engines without diagnosis (not replayed)",
                len(unavailable),
                "controls",
                None,
                None,
                "diagnosis_unavailable",
            ),
            metric(
                "C.reconstruction_mismatch_controls",
                "UNKNOWN controls whose replay does not reproduce the stored evaluation",
                len(mismatch),
                "controls",
                None,
                None,
                "reconstruction_mismatch",
            ),
        ],
    )


@dataclass(frozen=True)
class Sections:
    distribution: DistributionView
    segments: list[SegmentView]
    discrepancies: DiscrepanciesView
    blockers: BlockersView


def compute_sections(
    members: Sequence[IntegrityMember],
    documents: Mapping[str, EvaluationResult],
    as_of: datetime,
    diagnoser: Diagnoser,
    max_replays: int,
    rules: Rules = CURRENT,
) -> Sections:
    """The figures, from the pinned members and the stored evaluations they name, under
    the rules the report was produced with."""
    known_at = {p.evaluation_id: p.known_at for m in members for p in m.published_history}
    distribution = _distribution(members, documents, rules)
    return Sections(
        distribution=distribution,
        segments=_segments(members),
        discrepancies=_discrepancies(
            members, documents, known_at, as_of, distribution.admitted, rules
        ),
        blockers=_blockers(members, documents, diagnoser, max_replays, distribution.admitted),
    )


def limitations_for(members: Sequence[IntegrityMember], sections: Sections) -> list[str]:
    notes = list(LIMITATIONS)
    segments = sections.segments
    if len({s.profile_ref for s in segments}) > 1 or len({s.engine_ref for s in segments}) > 1:
        notes.append(
            "The admitted conclusions mix profile or engine versions (see segments): their "
            "controls may have different semantics, so the totals are a composition, not a "
            "comparable series."
        )
    if any(s.engine_status == "retired" for s in segments):
        notes.append(
            "Some admitted conclusions were recorded with a retired engine: their stored "
            "results stand, but no diagnosis is given for them."
        )
    if not sections.blockers.complete:
        b = sections.blockers
        notes.append(
            f"Blocks are incomplete: {len(b.not_processed)} evaluations needing a diagnosis "
            f"were not processed (max_replays {b.max_replays}); shares that depend on the "
            "total are undefined."
        )
    retired = sum(1 for m in members if m.status == "engine_retired_requires_reevaluation")
    if retired:
        notes.append(
            f"{retired} operations have a current publication of a retired engine: their stored "
            "results are kept as history, out of the admitted MATCH/BREAK/UNKNOWN, until a "
            "re-evaluation; nothing was re-evaluated and the stored publications are unchanged."
        )
    if any(m.status == "registered_not_evaluated" for m in members):
        notes.append(
            "Operations without a stored evaluation are counted as such; no execution state "
            "of the worker is recorded, so none is inferred."
        )
    return notes


def build_report(
    state: IntegrityState,
    *,
    as_of: datetime,
    profile_refs: Iterable[str] | None,
    max_replays: int,
    diagnoser: Diagnoser,
    scope: ReportScopeView,
) -> IntegrityReport:
    profile_filter = None if profile_refs is None else sorted(set(profile_refs))
    members, exclusions = select_members(state, as_of, profile_filter, CURRENT)
    sections = compute_sections(members, state.documents, as_of, diagnoser, max_replays, CURRENT)
    closed = state.closed_cut
    body: dict[str, Any] = {
        "schema_version": "1.0",
        "report_format": CURRENT.report_format,
        "definition": CURRENT.definition,
        "selection_policy": CURRENT.selection_policy,
        "selection_description": CURRENT.selection_description,
        "scope": scope,
        "as_of": as_of,
        "store_read": StoreReadView(
            isolation="repeatable_read_read_only",
            read_at=state.read_at,
            closed_knowledge_cut=closed,
            as_of_within_closed_cut=closed is not None and as_of <= closed,
        ),
        "profile_filter": profile_filter,
        "max_replays": max_replays,
        "diagnostic_engines": sorted(DIAGNOSTIC_ENGINES),
        "members": members,
        "distribution": sections.distribution,
        "segments": sections.segments,
        "discrepancies": sections.discrepancies,
        "blockers": sections.blockers,
        "exclusions": exclusions,
        "limitations": limitations_for(members, sections),
        "report_sha256": "0" * 64,
        "temporal": TemporalView(
            evidence_horizon=as_of,
            evidence_horizon_applies_to="snapshot_known_at",
            publication_state="present_at_store_read",
            publication_state_read_at=state.read_at,
            publication_state_reconstructed_at_horizon=False,
            economic_time="not_filtered",
            economic_time_reason=ECONOMIC_TIME_REASON,
            age_reference="evidence_horizon",
            notes=TEMPORAL_NOTES,
        ),
    }
    draft = IntegrityReport(**body)
    return draft.model_copy(update={"report_sha256": report_sha256(draft)})


# ------------------------------------------------------------------ cases (detail)


def case_keys(report: IntegrityReport) -> list[str]:
    keys = ["included", "conclusive", "technical_error", "discrepancy:any", "block:any"]
    keys += [f"status:{s}" for s in rules_of(report).statuses]
    keys += [f"result:{r}" for r in ("MATCH", "BREAK", "UNKNOWN")]
    keys += [g.cases_key for g in report.discrepancies.groups]
    keys += [g.cases_key for g in report.blockers.groups]
    keys += ["not_evaluated", "diagnosis_unavailable", "reconstruction_mismatch", "not_processed"]
    return keys


def _admitted(m: IntegrityMember) -> bool:
    return m.status == "admitted" and m.selected is not None


def members_for(report: IntegrityReport, key: str) -> list[IntegrityMember]:
    """The operations a cases key names, in operation order."""
    if key not in case_keys(report):
        raise ValueError(f"unknown cases key {key!r}")
    by_op = {m.operation_ref: m for m in report.members}

    def ops(items: Iterable[Any]) -> list[IntegrityMember]:
        return [by_op[o] for o in sorted({i.operation_ref for i in items})]

    b = report.blockers
    if key == "included":
        return list(report.members)
    if key.startswith("status:"):
        return [m for m in report.members if m.status == key.split(":", 1)[1]]
    if key.startswith("result:"):
        wanted = key.split(":", 1)[1]
        return [m for m in report.members if _admitted(m) and m.selected.result == wanted]  # type: ignore[union-attr]
    if key == "conclusive":
        return [
            m
            for m in report.members
            if _admitted(m) and m.selected.result in ("MATCH", "BREAK")  # type: ignore[union-attr]
        ]
    if key == "technical_error":
        return [by_op[o] for o in report.distribution.technical_error_operations]
    if key == "discrepancy:any":
        return ops(report.discrepancies.incidents)
    if key.startswith("discrepancy:"):
        profile, control = key.split(":", 1)[1].split("/", 1)
        return ops(
            i
            for i in report.discrepancies.incidents
            if (i.profile_ref, i.control_id) == (profile, control)
        )
    if key == "block:any":
        return ops(b.blocks)
    if key.startswith("block:"):
        category = key.split(":", 1)[1]
        return ops(x for x in b.blocks if (x.category or "uncategorised") == category)
    if key == "not_processed":
        return [by_op[o] for o in sorted(b.not_processed)]
    return ops(getattr(b, key))


CRITERION: dict[str, str] = {
    "included": "operation in the population after filters",
    "conclusive": "admitted conclusion with result MATCH or BREAK",
    "technical_error": "admitted conclusion with at least one control in EVALUATION_ERROR",
    "discrepancy:any": "admitted conclusion with at least one control in FAIL",
    "block:any": "admitted conclusion with at least one engine-recorded block",
    "not_evaluated": "requirement the engine did not reach in an UNKNOWN control",
    "diagnosis_unavailable": "UNKNOWN control of an engine without diagnosis",
    "reconstruction_mismatch": "UNKNOWN control whose replay does not reproduce the stored one",
    "not_processed": "evaluation needing a diagnosis not processed within max_replays",
}


def criterion(key: str) -> str:
    if key in CRITERION:
        return CRITERION[key]
    kind, _, value = key.partition(":")
    if kind == "status":
        return f"selection status {value}: {STATUS_MEANING[value]}"
    if kind == "result":
        return f"admitted conclusion with stored result {value}"
    if kind == "discrepancy":
        return f"admitted conclusion with control {value} (profile/control) in FAIL"
    return f"admitted conclusion with an engine-recorded block of category {value}"


def page_members(
    report: IntegrityReport, key: str, offset: int, limit: int
) -> tuple[int, list[IntegrityMember]]:
    members = members_for(report, key)
    return len(members), members[offset : offset + limit]


def _reasons(
    report: IntegrityReport, key: str, m: IntegrityMember, document: EvaluationResult | None
) -> list[CaseReason]:
    empty: dict[str, Any] = {
        "control_id": None,
        "requirement_id": None,
        "status": None,
        "reason_code": None,
        "category": None,
        "evidence_refs": [],
        "coverage_ids": [],
        "delta": None,
        "age": None,
        "required_from_source": None,
    }

    def reason(kind: str, **fields: Any) -> CaseReason:
        return CaseReason(**{**empty, "kind": kind, **fields})

    controls = {c.control_id: c for c in document.controls} if document else {}
    b = report.blockers
    op = m.operation_ref
    if key == "included" or key.startswith("status:"):
        detail = f" ({', '.join(m.status_detail)})" if m.status_detail else ""
        return [reason("status", status=f"{m.status}{detail}")]
    if key.startswith("result:") or key == "conclusive":
        return [reason("result", status=m.selected.result if m.selected else None)]
    if key == "technical_error":
        return [
            reason(
                "technical_error",
                control_id=c.control_id,
                status=c.status,
                reason_code=c.reason_code,
                evidence_refs=list(c.evidence_refs),
            )
            for c in controls.values()
            if c.reason_code == "EVALUATION_ERROR"
        ]
    if key.startswith("discrepancy:"):
        wanted = None if key == "discrepancy:any" else key.split(":", 1)[1]
        return [
            reason(
                "discrepancy",
                control_id=i.control_id,
                status="FAIL",
                reason_code=i.reason_code,
                evidence_refs=list(controls[i.control_id].evidence_refs),
                delta=controls[i.control_id].delta,
                age=i.age,
            )
            for i in report.discrepancies.incidents
            if i.operation_ref == op and wanted in (None, f"{i.profile_ref}/{i.control_id}")
        ]
    if key.startswith("block:"):
        wanted = None if key == "block:any" else key.split(":", 1)[1]
        return [
            reason(
                "block",
                control_id=x.control_id,
                requirement_id=x.requirement_id,
                status="undetermined",
                reason_code=x.reason_code,
                category=x.category,
                evidence_refs=list(x.evidence_refs),
                coverage_ids=list(x.coverage_ids),
                required_from_source=x.required_from_source,
            )
            for x in b.blocks
            if x.operation_ref == op and wanted in (None, x.category or "uncategorised")
        ]
    if key == "not_processed":
        return [reason("not_processed")]
    refs: list[ControlRef] = getattr(b, key)
    return [
        reason(
            key,
            control_id=r.control_id,
            requirement_id=r.requirement_id,
            status="not_evaluated" if key == "not_evaluated" else None,
        )
        for r in refs
        if r.operation_ref == op
    ]


def _follow_up(m: IntegrityMember, reasons: Sequence[CaseReason]) -> list[FollowUpView]:
    calls = [FollowUpView(query="get_timeline", args={"operation_ref": m.operation_ref})]
    if m.selected is None:
        return calls
    evaluation_id = m.selected.evaluation_id
    calls.append(
        FollowUpView(
            query="get_operation_obligations",
            args={"operation_ref": m.operation_ref, "evaluation_id": evaluation_id},
        )
    )
    for control in sorted({r.control_id for r in reasons if r.control_id}):
        calls.append(
            FollowUpView(
                query="diagnose_control",
                args={"evaluation_id": evaluation_id, "control_id": control},
            )
        )
    for evidence in sorted({e for r in reasons for e in r.evidence_refs}):
        calls.append(FollowUpView(query="get_evidence", args={"evidence_id": evidence}))
    return calls


def cases(
    report: IntegrityReport,
    key: str,
    documents: Mapping[str, EvaluationResult],
    offset: int,
    limit: int,
) -> CasesView:
    """A deterministic page of the operations behind ``key``. Evidence references and deltas
    come from the stored evaluations in ``documents``, not from the report file."""
    total, page = page_members(report, key, offset, limit)
    items = []
    for m in page:
        document = documents[m.selected.evaluation_id] if m.selected else None
        reasons = _reasons(report, key, m, document)
        items.append(
            CaseView(
                operation_ref=m.operation_ref,
                status=m.status,
                result=m.selected.result if m.selected else None,
                selected=m.selected,
                criterion=criterion(key),
                reasons=reasons,
                follow_up=_follow_up(m, reasons),
            )
        )
    return CasesView(
        report_sha256=report.report_sha256,
        cases_key=key,
        total_operations=total,
        offset=offset,
        limit=limit,
        cases=items,
        limitations=[
            "Membership is the report's, pinned at its read; evidence references and deltas "
            "are read from the stored evaluations. A delta is the control's difference as "
            "stored: never summed, never a loss.",
            "Follow-up entries are query-service calls, not links.",
        ],
    )


def compare_sections(report: IntegrityReport, sections: Sections) -> list[str]:
    """Which recomputed sections differ from the report's."""
    differences = []
    for name in ("distribution", "segments", "discrepancies", "blockers"):
        stored = getattr(report, name)
        fresh = getattr(sections, name)
        dump = (
            (lambda v: [x.model_dump(mode="json") for x in v])
            if name == "segments"
            else (lambda v: v.model_dump(mode="json"))
        )
        if dump(stored) != dump(fresh):
            differences.append(f"section {name} differs when recomputed from the pinned references")
    return differences


def consistency_problems(report: IntegrityReport) -> list[str]:
    """Whether the report's figures agree with its pinned members under its own definition.
    Internal coherence only: it cannot tell a coherent edit from a legitimate report."""
    problems: list[str] = []
    members = report.members

    def cases_of(key: str) -> int | None:
        try:
            return len(members_for(report, key))
        except KeyError:
            problems.append(f"{key}: names operations that are not members")
            return None

    rules = rules_of(report)
    d = report.distribution
    admitted = [m for m in members if _admitted(m)]
    results = Counter(m.selected.result for m in admitted if m.selected)
    ops = [m.operation_ref for m in members]
    if len(set(ops)) != len(ops):
        problems.append("an operation appears more than once among the members")
    if set(d.statuses) != set(rules.statuses):
        problems.append("distribution statuses do not match the report's definition")
    counted = Counter(m.status for m in members)
    if any(d.statuses.get(s, 0) != counted[s] for s in rules.statuses):
        problems.append("distribution status counts differ from the members")
    expected = (len(members), len(admitted), results["MATCH"], results["BREAK"])
    if (d.included, d.admitted, d.match_count, d.break_count) != expected or (
        d.unknown_count != results["UNKNOWN"]
    ):
        problems.append("distribution totals differ from the members")
    admitted_ops = {m.operation_ref for m in admitted}
    if (
        d.technical_error != len(d.technical_error_operations)
        or not set(d.technical_error_operations) <= admitted_ops
    ):
        problems.append("technical errors do not match admitted members")
    denominators = {
        "included operations": len(members),
        "admitted operations": len(admitted),
        "admitted MATCH + BREAK": results["MATCH"] + results["BREAK"],
    }
    x, b = report.discrepancies, report.blockers
    keyed_counts = {
        "B.operations_with_discrepancy_share_of_admitted",
        "C.operations_with_block_share_of_admitted",
    }
    for section in (d, x, b):
        for mv in section.metrics:
            if mv.denominator_definition is not None:
                if denominators.get(mv.denominator_definition) != mv.denominator:
                    problems.append(f"{mv.metric_id}: denominator differs from the members")
                if (mv.metric_id.startswith("A.") or mv.metric_id in keyed_counts) and cases_of(
                    mv.cases_key
                ) not in (None, mv.numerator):
                    problems.append(f"{mv.metric_id}: numerator differs from its cases")
    if x.control_discrepancies != len(x.incidents) or x.operations_with_discrepancy != len(
        {i.operation_ref for i in x.incidents}
    ):
        problems.append("discrepancy totals differ from the incidents")
    if b.blocked_requirements != len(b.blocks) or b.operations_with_block != len(
        {k.operation_ref for k in b.blocks}
    ):
        problems.append("block totals differ from the block records")
    for g in x.groups:
        if cases_of(g.cases_key) not in (None, g.operations):
            problems.append(f"{g.cases_key}: operations differ from its cases")
    for k in b.groups:
        if cases_of(k.cases_key) not in (None, k.operations):
            problems.append(f"{k.cases_key}: operations differ from its cases")
    cited = {i.operation_ref for i in x.incidents} | {k.operation_ref for k in b.blocks}
    if not cited <= admitted_ops:
        problems.append("incidents or blocks name operations that are not admitted members")
    for m in members:
        if m.selected is not None and (
            not m.published_history
            or m.published_history[-1].evaluation_id != m.selected.evaluation_id
        ):
            problems.append(f"{m.operation_ref}: selected evaluation is not its last publication")
    return problems
