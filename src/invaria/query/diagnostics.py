"""Structured diagnosis of a control's evidence requirements. Pure: no I/O.

The view is built from the checks the engine recorded while it replayed the stored
evaluation (``Evaluation.checks``), never from the aggregate status or the free-text
reason. Shared by the query service and the offline CLI.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast, get_args

from invaria.contracts.evaluation import ControlResult, EvaluationResult
from invaria.contracts.observation import FactType
from invaria.contracts.profile import OperationProfile, RedemptionProfile
from invaria.engine.common import Evaluation, EvaluationInputs
from invaria.engine.trace import CheckRecord, control_plan, describe
from invaria.engine.versions import DIAGNOSTIC_ENGINES, engine_status
from invaria.query.models import (
    LIMITATION_STORED_ONLY,
    BlockCategory,
    ControlDiagnosisView,
    NextStepKind,
    NextStepView,
    Requirement,
    RequirementCheckView,
    SnapshotView,
)

CATEGORY_BY_REASON: dict[str, BlockCategory] = {
    "MISSING_EVIDENCE": "missing_evidence",
    "LOSS_OF_SUPPORT": "withdrawn_evidence",
    "SOURCE_CONFLICT": "source_conflict",
    "DEADLINE_DATA_CONFLICT": "inconsistent_evidence",
    "AMBIGUOUS_SEQUENCE": "inconsistent_evidence",
    "POSITION_NOT_BEFORE_ACCEPTANCE": "inconsistent_evidence",
    "INVALID_CANCELLATION": "invalid_evidence",
    "INVALID_RETRACTION": "invalid_evidence",
    "INSUFFICIENT_COVERAGE": "insufficient_coverage",
    "QUARANTINED_INPUT": "quarantined_input",
    "AMBIGUOUS_MATCH": "unattributed",
    "UNSUPPORTED_CAPABILITY": "unsupported_capability",
    "INEXACT_AMOUNT": "unsupported_capability",
    "REACTIVATION_UNSUPPORTED": "unsupported_capability",
    "BURN_BEFORE_ACCEPTANCE_UNSUPPORTED": "unsupported_capability",
    "PAYMENT_NOT_DUE": "not_yet_due",
    "EVALUATION_ERROR": "technical_error",
}
# Blocks that put the cited observations themselves in question. For the others (coverage,
# time) the observations cited were admitted; what is missing is elsewhere.
_QUESTIONING = frozenset(CATEGORY_BY_REASON.values()) - {"insufficient_coverage", "not_yet_due"}

LIMITATION_OFFLINE = (
    "Evaluated offline from a corpus snapshot, not read from a store; no source was queried."
)
CAVEAT = (
    "New evidence may confirm, contradict or change the result; this is never a promise of a "
    "MATCH, and nothing is linked, approved or corrected automatically."
)

_NEXT: dict[BlockCategory, tuple[NextStepKind, str]] = {
    "missing_evidence": (
        "provide_observation",
        "Provide an observation of {fact} from its authoritative source {source}, linked to "
        "the operation by the source itself.",
    ),
    "withdrawn_evidence": (
        "provide_observation",
        "The source withdrew the supporting record; only a new revision from {source} can "
        "restore support.",
    ),
    "source_conflict": (
        "resolve_source_conflict",
        "Review the conflicting deliveries of the same record; the source must resolve them "
        "with a new revision. The engine never chooses between them.",
    ),
    "inconsistent_evidence": (
        "review_inconsistent_evidence",
        "Review the admitted records whose data disagree; a corrected record from the source "
        "is needed. Evidence is never edited to fit.",
    ),
    "invalid_evidence": (
        "review_inconsistent_evidence",
        "The record does not meet the profile's validity rules and is not applied; review it "
        "with its source.",
    ),
    "insufficient_coverage": (
        "obtain_coverage",
        "Obtain a coverage certificate produced by reading {source} for {fact}, at the "
        "required level, over the required interval and approved addresses. A declaration "
        "without that reading does not extend coverage.",
    ),
    "quarantined_input": (
        "resolve_quarantine",
        "Resolve the quarantined records with a corrected delivery or a review; quarantined "
        "records are never counted.",
    ),
    "unattributed": (
        "provide_approved_link",
        "An approved ExecutionLink or IdentityLink is required to attribute the evidence. "
        "Amount, time or memo never link, and identities are never approved automatically.",
    ),
    "unsupported_capability": (
        "outside_declared_capabilities",
        "Outside this profile's declared capabilities: more evidence of the same kind cannot "
        "decide it. Changing that is a domain decision (a new profile or engine version).",
    ),
    "not_yet_due": (
        "await_due_time",
        "Not due at the evaluation clock and economic cut; re-evaluate after the due time. No "
        "document is missing yet.",
    ),
    "technical_error": (
        "correct_technical_error",
        "Correct the technical error identified in the explanation; no document is missing.",
    ),
}

_OUTCOME_OF_STATUS = {
    "PASS": "satisfied",
    "FAIL": "contradicted",
    "UNKNOWN": "undetermined",
    "NOT_APPLICABLE": "not_applicable",
}


def requirements_for(
    profile: OperationProfile | RedemptionProfile, fact_types: Sequence[FactType]
) -> list[Requirement]:
    """What the profile requires of each fact: authoritative source, mapping and coverage.
    The static catalogue ``get_missing_evidence`` reports; the diagnosis uses it to orient
    the evidence that could help."""
    found = []
    for fact_type in fact_types:
        requirement = _requirement(profile, fact_type)
        if requirement is not None:
            found.append(requirement)
    return found


def _requirement(
    profile: OperationProfile | RedemptionProfile, fact_type: FactType
) -> Requirement | None:
    source_id = profile.authority_for(fact_type)
    source = next(s for s in profile.sources if s.source_id == source_id)
    coverage = next(
        (
            r
            for r in profile.coverage_requirements
            if r.source_id == source_id and fact_type in r.fact_types
        ),
        None,
    )
    if coverage is None:
        return None
    return Requirement(
        fact_type=fact_type,
        authoritative_source=source_id,
        mapping_ref=source.mapping_ref,
        min_coverage_level=coverage.min_level,
        must_cover=coverage.must_cover,
        gaps_allowed=coverage.gaps_allowed,
    )


def determining_index(records: Sequence[CheckRecord], control: ControlResult) -> int | None:
    """The check whose outcome fixed the control's result: the last one with the control's
    outcome and reason code. None when the trace does not account for the result."""
    outcome = _OUTCOME_OF_STATUS[control.status]
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        if record.outcome == outcome and record.reason_code == control.reason_code:
            return index
    return None


def _snapshot(inputs: EvaluationInputs) -> SnapshotView:
    s = inputs.snapshot
    return SnapshotView(
        snapshot_id=s.snapshot_id,
        valid_at=s.valid_at,
        known_at=s.known_at,
        evaluation_clock=s.evaluation_clock,
    )


def _next_step(
    category: BlockCategory, record: CheckRecord, profile: OperationProfile | RedemptionProfile
) -> NextStepView:
    """The fact is the one the engine states it was reading (never inferred from text)."""
    kind, text = _NEXT[category]
    named = (
        record.requirement_id.removeprefix("fact:")
        if record.requirement_id.startswith("fact:")
        else record.fact
    )
    fact = cast("FactType", named) if named in get_args(FactType) else None
    oriented = category in ("missing_evidence", "withdrawn_evidence", "insufficient_coverage")
    requirement = _requirement(profile, fact) if fact is not None and oriented else None
    source = requirement.authoritative_source if requirement else "the authoritative source"
    return NextStepView(
        kind=kind,
        description=text.format(fact=fact or "the required facts", source=source),
        fact_type=fact if requirement else None,
        authoritative_source=requirement.authoritative_source if requirement else None,
        mapping_ref=requirement.mapping_ref if requirement else None,
        min_coverage_level=requirement.min_coverage_level if requirement else None,
        must_cover=requirement.must_cover if requirement else None,
        caveat=CAVEAT,
    )


def _check_view(
    record: CheckRecord,
    operation_type: str,
    control: ControlResult,
    determined: bool,
    coverage_ids: frozenset[str],
    profile: OperationProfile | RedemptionProfile,
) -> RequirementCheckView:
    description, applies_when = describe(operation_type, record.requirement_id)
    category = (
        CATEGORY_BY_REASON.get(record.reason_code or "")
        if record.outcome == "undetermined"
        else None
    )
    coverage = [r for r in record.refs if r in coverage_ids]
    aside = [r for r in record.set_aside if r not in coverage_ids]
    others = [r for r in record.refs if r not in coverage_ids and r not in aside]
    questioning = category in _QUESTIONING
    explanation = record.detail or f"checked by the engine: {record.outcome.replace('_', ' ')}"
    if record.requirement_id == "comparison" and not determined and control.status == "UNKNOWN":
        explanation += "; observed only: the control is not concluded"
    if record.outcome == "undetermined" and not determined:
        explanation += "; it does not change this result"
    return RequirementCheckView(
        requirement_id=record.requirement_id,
        description=description,
        applies_when=applies_when,
        status=record.outcome,
        determined_result=determined,
        category=category,
        reason_code=record.reason_code,
        explanation=explanation,
        admitted_evidence=[] if questioning else others,
        questioned_evidence=others if questioning else [],
        set_aside_evidence=aside,
        coverage_ids=coverage,
        left=record.left,
        right=record.right,
        delta=record.delta,
        next_step=(
            _next_step(category, record, profile) if determined and category is not None else None
        ),
    )


def diagnose(
    evaluation: EvaluationResult,
    inputs: EvaluationInputs,
    replayed: Evaluation,
    control_id: str,
    *,
    stored: bool = True,
) -> ControlDiagnosisView:
    """The diagnosis of one control of a stored evaluation. ``replayed`` is the replay of
    its recorded engine on its own snapshot; ``control_id`` must be one of its controls."""
    control = next(c for c in evaluation.controls if c.control_id == control_id)
    profile = inputs.profile
    engine_ref = evaluation.versions.engine_ref
    status = engine_status(engine_ref)
    consistent = replayed.result == evaluation
    limitations = [LIMITATION_STORED_ONLY if stored else LIMITATION_OFFLINE]
    requirements: list[RequirementCheckView] = []
    records = replayed.checks.get(control_id, ())
    determined = determining_index(records, control) if records else None
    if not consistent:
        basis = "reconstruction_mismatch"
        limitations.append(
            "Replaying the recorded engine on the stored snapshot does not reproduce this "
            "evaluation: no explanation is attributed to the recorded result."
        )
    elif engine_ref not in DIAGNOSTIC_ENGINES:
        basis = "unavailable_for_engine"
        limitations.append(
            f"{engine_ref} is {status}: it is reproduced by a compatibility implementation "
            "whose checks are not the historical engine's reasoning, so no requirement detail "
            "is given. The recorded control result stands as stored."
        )
    elif determined is None:
        basis = "reconstruction_mismatch"
        limitations.append(
            "The engine's record of its checks does not account for the recorded result: no "
            "explanation is attributed to it."
        )
    else:
        basis = "reconstructed"
        spec = next(c for c in profile.controls if c.control_id == control_id)
        plan = control_plan(
            profile.operation_type,
            control_id,
            list(spec.requires),
            getattr(spec, "applies", None),
        )
        coverage_ids = frozenset(inputs.snapshot.coverage_ids)
        by_id = {r.requirement_id: (i, r) for i, r in enumerate(records)}
        decisive = records[determined].requirement_id
        for requirement_id in plan:
            if requirement_id in by_id:
                index, record = by_id[requirement_id]
                requirements.append(
                    _check_view(
                        record,
                        profile.operation_type,
                        control,
                        index == determined,
                        coverage_ids,
                        profile,
                    )
                )
                continue
            description, applies_when = describe(profile.operation_type, requirement_id)
            requirements.append(
                RequirementCheckView(
                    requirement_id=requirement_id,
                    description=description,
                    applies_when=applies_when,
                    status="not_evaluated",
                    determined_result=False,
                    category=None,
                    reason_code=None,
                    explanation=(
                        f"not run: the engine fixed the result at {decisive} before reaching "
                        "this check; not evaluated is never failed"
                    ),
                    admitted_evidence=[],
                    questioned_evidence=[],
                    set_aside_evidence=[],
                    coverage_ids=[],
                    left=None,
                    right=None,
                    delta=None,
                    next_step=None,
                )
            )
        evaluated = "stored evaluation" if stored else "evaluation computed from the corpus"
        limitations += [
            f"Reconstructed by replaying {engine_ref} on the evaluation's closed snapshot and "
            f"contrasted with the {evaluated}; the checks are the engine's own record of that "
            "run, not stored with the evaluation.",
            "Checks after the one that fixed the result were not run and are reported as not "
            "evaluated.",
            "A diagnosis never raises coverage_level; an auxiliary report (ledger verification, "
            "trust line reconciliation) never makes an insufficient certificate sufficient.",
        ]
    return ControlDiagnosisView(
        schema_version="1.0",
        evaluation_id=evaluation.evaluation_id,
        operation_ref=evaluation.operation_ref,
        snapshot=_snapshot(inputs),
        profile_ref=evaluation.versions.profile_ref,
        rules_ref=evaluation.versions.rules_ref,
        engine_ref=engine_ref,
        engine_status=status,
        control_id=control.control_id,
        mandatory=control.mandatory,
        status=control.status,
        reason_code=control.reason_code,
        concluded=control.status in ("PASS", "FAIL"),
        diagnosis=basis,  # type: ignore[arg-type]
        replay_consistent=consistent,
        requirements=requirements,
        limitations=limitations,
    )
