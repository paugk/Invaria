"""Operational integrity analytics, pure: no store, no network.

Expectations E1-E20 were written before the code. Real
evaluations of the corpora are used wherever the case exists; copies marked SYNTHETIC are
built only for shapes the corpora lack (several FAIL controls, a blocked engine label, a
replay that does not reproduce).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from invaria.contracts.base import parse_contract
from invaria.contracts.evaluation import EvaluationResult
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.common import Evaluation, EvaluationInputs
from invaria.engine.evaluate import evaluate, replay
from invaria.engine.versions import engine_status
from invaria.query import analytics
from invaria.query.analytics import (
    AttemptRow,
    EpochRow,
    EvaluationRow,
    IntegrityState,
    PublicationRow,
    SnapshotRow,
)
from invaria.query.models import (
    IntegrityMember,
    IntegrityReport,
    ReportScopeView,
    SelectedEvaluation,
)

CORPUS = Path(__file__).resolve().parents[1] / "fixtures/corpus"
T0 = datetime(2026, 10, 1, tzinfo=UTC)
AS_OF = T0 + timedelta(days=10)


@pytest.fixture(scope="module")
def sub12() -> Corpus:
    return load_corpus(CORPUS / "subscription-synthetic-1.2.0")


@pytest.fixture(scope="module")
def sub10() -> Corpus:
    return load_corpus(CORPUS / "subscription-synthetic")


@pytest.fixture(scope="module")
def red17() -> Corpus:
    return load_corpus(CORPUS / "redemption-synthetic-1.7.0")


def run(
    corpus: Corpus, scenario: str, engine: str | None = None
) -> tuple[EvaluationInputs, EvaluationResult]:
    inputs = corpus.inputs_for(scenario)
    result = replay(inputs, engine) if engine else evaluate(inputs)
    return inputs, result.result


def synthetic(document: EvaluationResult, **changes: Any) -> EvaluationResult:
    """A SYNTHETIC copy (never presented as an engine output): new id, edited fields."""
    data = document.model_dump(mode="json")
    data.update(changes)
    return parse_contract(EvaluationResult, json.dumps(data))


def chosen(member: IntegrityMember) -> SelectedEvaluation:
    assert member.selected is not None
    return member.selected


@dataclass
class World:
    epochs: list[EpochRow] = field(default_factory=list)
    snapshots: list[SnapshotRow] = field(default_factory=list)
    evaluations: list[EvaluationRow] = field(default_factory=list)
    publications: list[PublicationRow] = field(default_factory=list)
    attempts: list[AttemptRow] = field(default_factory=list)
    documents: dict[str, EvaluationResult] = field(default_factory=dict)
    inputs: dict[str, EvaluationInputs] = field(default_factory=dict)
    replays: list[str] = field(default_factory=list)

    def scope(self, op: str, profile: str, causes: tuple[str, ...] = ("registered",)) -> None:
        for i, cause in enumerate(causes, start=1):
            self.epochs.append(EpochRow(op, i, profile, cause))

    def store(
        self,
        op: str,
        document: EvaluationResult,
        *,
        epoch: int = 1,
        known_at: datetime = T0,
        valid_at: datetime | None = None,
        publish: str = "yes",
        inputs: EvaluationInputs | None = None,
    ) -> None:
        snap = f"snap-{op}-{document.evaluation_id}"[:120]
        profile = document.versions.profile_ref
        self.snapshots.append(SnapshotRow(op, snap, profile, known_at, known_at))
        self.evaluations.append(
            EvaluationRow(
                document.evaluation_id,
                op,
                snap,
                profile,
                valid_at or known_at,
                known_at,
                known_at,
                document.result,
                document.versions.engine_ref,
            )
        )
        if publish == "yes":
            self.publications.append(PublicationRow(op, epoch, document.evaluation_id))
            self.attempts.append(AttemptRow(document.evaluation_id, epoch, "PUBLISHED"))
        elif publish == "refused":
            self.attempts.append(AttemptRow(document.evaluation_id, epoch, "NOT_CURRENT"))
        self.documents[document.evaluation_id] = document
        if inputs is not None:
            self.inputs[document.evaluation_id] = inputs

    def state(self, closed_cut: datetime | None = None) -> IntegrityState:
        published = {p.evaluation_id for p in self.publications}
        return IntegrityState(
            tenant_id="tenant-a",
            epochs=self.epochs,
            snapshots=self.snapshots,
            evaluations=self.evaluations,
            publications=self.publications,
            attempts=self.attempts,
            documents={k: v for k, v in self.documents.items() if k in published},
            read_at=T0,
            closed_cut=closed_cut,
        )

    def diagnoser(self) -> Callable[[EvaluationResult], tuple[EvaluationInputs, Evaluation]]:
        def diagnose(document: EvaluationResult) -> tuple[EvaluationInputs, Evaluation]:
            self.replays.append(document.evaluation_id)
            inputs = self.inputs[document.evaluation_id]
            return inputs, replay(inputs, document.versions.engine_ref)

        return diagnose

    def report(
        self,
        as_of: datetime = AS_OF,
        profiles: list[str] | None = None,
        max_replays: int = 100,
        closed_cut: datetime | None = None,
    ) -> IntegrityReport:
        return analytics.build_report(
            self.state(closed_cut),
            as_of=as_of,
            profile_refs=profiles,
            max_replays=max_replays,
            diagnoser=self.diagnoser(),
            scope=ReportScopeView(
                tenant_id="tenant-a",
                principal_id="analyst-1",
                access_scopes=["operations:read"],
                population="registered_accessible_operations",
                isolation=analytics.ISOLATION,
            ),
        )


def metric(report: IntegrityReport, metric_id: str) -> Any:
    for section in (report.distribution, report.discrepancies, report.blockers):
        for m in section.metrics:
            if m.metric_id == metric_id:
                return m
    raise KeyError(metric_id)


def terms(report: IntegrityReport, metric_id: str) -> tuple[int, int | None, str | None]:
    m = metric(report, metric_id)
    return m.numerator, m.denominator, m.value_percent


def copies(document: EvaluationResult, n: int, prefix: str) -> list[EvaluationResult]:
    return [synthetic(document, evaluation_id=f"{prefix}-{i:03d}") for i in range(n)]


def reference_world(sub10: Corpus, sub12: Corpus, red17: Corpus, unevaluated: int = 0) -> World:
    """E1: 70 MATCH, 10 BREAK, 20 UNKNOWN admitted (SYNTHETIC copies of real results)."""
    match_in, match = run(sub10, "K2")
    brk_in, brk = run(red17, "RD-SHORT-PAY")
    unknown_in, unknown = run(sub12, "K2")
    world = World()
    i = 0
    for inputs, document, n in (
        (match_in, match, 70),
        (brk_in, brk, 10),
        (unknown_in, unknown, 20),
    ):
        for copy in copies(document, n, f"e1-{document.result.lower()}"):
            op = f"OP-{i:03d}"
            world.scope(op, copy.versions.profile_ref)
            world.store(op, copy, inputs=inputs)
            i += 1
    for j in range(unevaluated):
        world.scope(f"OP-NEW-{j:03d}", match.versions.profile_ref)
    return world


# ---------------------------------------------------------------- distribution (A)


def test_e1_reference_ratios_keep_their_denominators(
    sub10: Corpus, sub12: Corpus, red17: Corpus
) -> None:
    report = reference_world(sub10, sub12, red17).report(max_replays=1)
    assert terms(report, "A.match_share_of_admitted") == (70, 100, "70.00")
    assert terms(report, "A.unknown_share_of_admitted") == (20, 100, "20.00")
    assert terms(report, "A.conclusive_share_of_admitted") == (80, 100, "80.00")
    share = metric(report, "A.match_share_of_conclusive")
    assert (share.numerator, share.denominator, share.value_percent) == (70, 80, "87.50")
    assert share.unknown_excluded == 20


def test_e2_unevaluated_operations_change_only_the_population_denominator(
    sub10: Corpus, sub12: Corpus, red17: Corpus
) -> None:
    report = reference_world(sub10, sub12, red17, unevaluated=25).report(max_replays=1)
    assert terms(report, "A.included") == (125, None, None)
    assert terms(report, "A.admitted_share") == (100, 125, "80.00")
    assert terms(report, "A.match_share_of_included") == (70, 125, "56.00")
    assert terms(report, "A.match_share_of_admitted") == (70, 100, "70.00")
    assert terms(report, "A.status:registered_not_evaluated") == (25, 125, "20.00")
    assert report.distribution.statuses["registered_not_evaluated"] == 25


def test_e3_reevaluations_count_one_operation(sub12: Corpus) -> None:
    world = World()
    world.scope(
        "OP-1",
        "fund-subscription-synthetic@1.2.0",
        ("registered", "evidence_appended", "evidence_appended"),
    )
    for epoch, scenario in enumerate(("K1", "K2", "K3"), start=1):
        inputs, document = run(sub12, scenario)
        world.store(
            "OP-1", document, epoch=epoch, known_at=T0 + timedelta(days=epoch), inputs=inputs
        )
    report = world.report()
    d = report.distribution
    assert (d.included, d.admitted, d.break_count) == (1, 1, 1)
    assert (d.stored_evaluations, d.evaluations_not_selected) == (3, 2)
    assert chosen(report.members[0]).result == "BREAK"  # K3, the current publication


def test_e4_the_published_evaluation_wins_over_a_later_unpublished_one(sub12: Corpus) -> None:
    world = World()
    world.scope("OP-1", "fund-subscription-synthetic@1.2.0")
    k2_inputs, k2 = run(sub12, "K2")
    _, k3 = run(sub12, "K3")
    world.store("OP-1", k2, known_at=T0, inputs=k2_inputs)
    world.store("OP-1", k3, known_at=T0 + timedelta(days=1), publish="refused")
    member = world.report().members[0]
    assert member.status == "admitted"
    assert chosen(member).evaluation_id == k2.evaluation_id
    assert member.latest_stored_evaluation_id == k3.evaluation_id


def test_e5_unpublished_operations_keep_what_is_recorded(sub12: Corpus) -> None:
    world = World()
    _, k2 = run(sub12, "K2")
    _, k3 = run(sub12, "K3")
    for op, document, how in (("OP-R", k2, "refused"), ("OP-N", k3, "no")):
        world.scope(op, document.versions.profile_ref)
        world.store(op, document, publish=how)
    report = world.report()
    found = {m.operation_ref: (m.status, m.status_detail) for m in report.members}
    assert found == {
        "OP-N": ("stored_not_published", ["no_publication_attempt_recorded"]),
        "OP-R": ("stored_not_published", ["publication_refused"]),
    }
    assert report.distribution.admitted == 0


def test_e6_a_stale_publication_is_history_not_a_current_conclusion(sub12: Corpus) -> None:
    world = World()
    world.scope("OP-1", "fund-subscription-synthetic@1.2.0", ("registered", "evidence_appended"))
    _, k3 = run(sub12, "K3")
    world.store("OP-1", k3, epoch=1)
    report = world.report()
    member = report.members[0]
    assert (member.status, member.status_detail) == ("stale_publication", ["evidence_appended"])
    assert chosen(member).result == "BREAK"  # kept as stored
    d = report.distribution
    assert (d.admitted, d.match_count, d.break_count, d.unknown_count) == (0, 0, 0, 0)
    assert report.discrepancies.control_discrepancies == 0


def test_e7_a_technical_error_is_reported_apart_and_as_its_own_block(sub12: Corpus) -> None:
    inputs = sub12.inputs_for("K2")
    refused = replace(
        inputs,
        snapshot=inputs.snapshot.model_copy(
            update={"rules_ref": "subscription-synthetic-rules@9.9.9"}
        ),
    )
    document = evaluate(refused).result
    world = World()
    world.scope("OP-1", document.versions.profile_ref)
    world.store("OP-1", document, inputs=refused)
    report = world.report()
    assert report.distribution.unknown_count == 1  # the stored financial result
    assert report.distribution.technical_error_operations == ["OP-1"]
    assert {g.category for g in report.blockers.groups} == {"technical_error"}
    assert all(b.fact_type is None for b in report.blockers.blocks)


def test_e8_a_retired_engine_requires_reevaluation_and_is_never_replayed(
    sub10: Corpus,
) -> None:
    """current-publication@2: kept as history, out of the admitted results, in the population."""
    _, document = run(sub10, "K1", engine="invaria-engine@0.9.0")
    world = World()
    world.scope("OP-1", document.versions.profile_ref)
    world.store("OP-1", document)
    report = world.report()
    member = report.members[0]
    assert (member.status, member.status_detail) == (
        "engine_retired_requires_reevaluation",
        ["invaria-engine@0.9.0"],
    )
    assert chosen(member).result == document.result  # stored result kept as history
    d = report.distribution
    assert (d.included, d.admitted, d.unknown_count, d.technical_error) == (1, 0, 0, 0)
    assert d.statuses["engine_not_admissible"] == 0  # apart from blocked or unknown
    assert report.segments == []
    assert report.blockers.diagnosis_unavailable == []
    assert world.replays == []
    assert report.selection_policy == "current-publication@2"
    assert any("retired engine" in n for n in report.limitations)


def test_a_publication_whose_engine_is_retired_later_needs_reevaluation(
    sub12: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal case: published while current, the engine is retired afterwards."""
    inputs, document = run(sub12, "K2")
    world = World()
    world.scope("OP-1", document.versions.profile_ref)
    world.store("OP-1", document, inputs=inputs)
    before = world.report()
    assert before.members[0].status == "admitted"
    engine = document.versions.engine_ref
    monkeypatch.setattr(
        "invaria.query.analytics.engine_status",
        lambda ref: "retired" if ref == engine else engine_status(ref),
    )
    after = world.report()
    assert after.members[0].status == "engine_retired_requires_reevaluation"
    assert (after.distribution.included, after.distribution.admitted) == (1, 0)
    assert world.documents[document.evaluation_id] == document  # nothing re-evaluated
    # The pinned report keeps the policy and engine status it was produced with.
    again = analytics.compute_sections(
        before.members,
        world.documents,
        before.as_of,
        world.diagnoser(),
        before.max_replays,
        analytics.rules_of(before),
    )
    assert analytics.compare_sections(before, again) == []


def test_a_blocked_engine_is_never_replayed_even_to_reproduce(
    sub12: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, document = run(sub12, "K2")
    world = World()
    world.scope("OP-1", document.versions.profile_ref)
    world.store("OP-1", document, inputs=inputs)
    report = world.report()
    replays = len(world.replays)
    engine = document.versions.engine_ref
    monkeypatch.setattr(
        "invaria.query.analytics.engine_status",
        lambda ref: "blocked" if ref == engine else engine_status(ref),
    )
    again = analytics.compute_sections(
        report.members,
        world.documents,
        report.as_of,
        world.diagnoser(),
        report.max_replays,
        analytics.rules_of(report),
    )
    assert len(world.replays) == replays  # no new replay
    assert again.blockers.blocks == [] and again.blockers.diagnosis_unavailable
    assert analytics.compare_sections(report, again) == [
        "section blockers differs when recomputed from the pinned references"
    ]


def test_e9_a_blocked_engine_is_not_admitted_nor_reproduced(red17: Corpus) -> None:
    _, document = run(red17, "RD-PENDING")
    blocked = synthetic(
        document,
        evaluation_id="blocked-copy",
        versions={
            **document.versions.model_dump(mode="json"),
            "engine_ref": "invaria-redemption-engine@0.1.0",
        },
    )
    world = World()
    world.scope("OP-1", blocked.versions.profile_ref)
    world.store("OP-1", blocked)
    report = world.report()
    assert (report.members[0].status, report.members[0].status_detail) == (
        "engine_not_admissible",
        ["blocked"],
    )
    assert report.distribution.admitted == 0
    assert world.replays == []


# ---------------------------------------------------------- discrepancies (B)


def two_fail_copy(red17: Corpus) -> EvaluationResult:
    """SYNTHETIC: RD-SHORT-PAY with the payment_deadline control of RD-LATE (FAIL)."""
    _, short = run(red17, "RD-SHORT-PAY")
    _, late = run(red17, "RD-LATE")
    deadline = next(c for c in late.controls if c.control_id == "redemption.payment_deadline")
    data = short.model_dump(mode="json")
    data["evaluation_id"] = "two-fail-copy"
    data["controls"] = [
        deadline.model_dump(mode="json") if c["control_id"] == deadline.control_id else c
        for c in data["controls"]
    ]
    return parse_contract(EvaluationResult, json.dumps(data))


def test_e10_an_operation_with_two_fail_counts_once_and_twice(red17: Corpus) -> None:
    copy = two_fail_copy(red17)
    world = World()
    world.scope("OP-1", copy.versions.profile_ref)
    world.store("OP-1", copy, inputs=red17.inputs_for("RD-SHORT-PAY"))
    report = world.report()
    x = report.discrepancies
    assert (x.operations_with_discrepancy, x.control_discrepancies) == (1, 2)
    assert sum(g.operations for g in x.groups) == 2  # groups overlap: never summed as ops
    assert terms(report, "B.operations_with_discrepancy_share_of_admitted") == (1, 1, "100.00")
    # E15: the copy does not reproduce: no explanation is attributed to its UNKNOWN control
    assert [r.control_id for r in report.blockers.reconstruction_mismatch] == [
        "redemption.burn_vs_request"
    ]
    assert report.blockers.blocks == []


def test_e10b_a_break_keeps_its_blocks(red17: Corpus) -> None:
    inputs, document = run(red17, "RD-MISSED")
    world = World()
    world.scope("OP-1", document.versions.profile_ref)
    world.store("OP-1", document, inputs=inputs)
    report = world.report()
    assert report.distribution.break_count == 1
    assert {g.category: (g.operations, g.requirements) for g in report.blockers.groups} == {
        "insufficient_coverage": (1, 1),
        "missing_evidence": (1, 1),
    }
    assert report.blockers.operations_with_block == 1


def test_e18_age_starts_at_the_published_run_and_counts_unpublished_epochs(
    red17: Corpus,
) -> None:
    world = World()
    world.scope(
        "OP-1",
        "fund-redemption-synthetic@1.7.0",
        ("registered", "evidence_appended", "evidence_appended", "evidence_appended"),
    )
    for epoch, scenario, day in (
        (1, "RD-PAID", 0),
        (2, "RD-SHORT-PAY", 1),
        (4, "RD-SHORT-PAY-BEFORE-CORRECTION", 3),
    ):
        inputs, document = run(red17, scenario)
        world.store("OP-1", document, epoch=epoch, known_at=T0 + timedelta(days=day), inputs=inputs)
    (incident,) = world.report().discrepancies.incidents
    age = incident.age
    # Epoch 3 has no publication: persistence across it is never claimed, so the contiguous
    # run is only epoch 4; the gap is reported.
    assert age.basis == "contiguous_fail_run_start"
    assert age.origin_known_at == T0 + timedelta(days=3)
    assert age.age_seconds == 7 * 86400  # to the evidence horizon, not to any wall clock
    assert age.age_reference == "evidence_horizon"
    assert (age.publications_in_run, age.epochs_without_publication_in_run) == (1, [])
    assert (age.run_stopped_by, age.unpublished_epochs_before_run) == ("unpublished_epoch_gap", [3])


def test_a_change_of_engine_stops_the_run(red17: Corpus) -> None:
    world = World()
    world.scope("OP-1", "fund-redemption-synthetic@1.7.0", ("registered", "evidence_appended"))
    inputs, short = run(red17, "RD-SHORT-PAY")
    older = synthetic(  # SYNTHETIC: the same FAIL recorded by another engine label
        short,
        evaluation_id="older-engine-copy",
        versions={
            **short.versions.model_dump(mode="json"),
            "engine_ref": "invaria-redemption-engine@0.10.0",
        },
    )
    world.store("OP-1", older, epoch=1, known_at=T0)
    _, later = run(red17, "RD-SHORT-PAY-BEFORE-CORRECTION")
    world.store("OP-1", later, epoch=2, known_at=T0 + timedelta(days=1), inputs=inputs)
    (incident,) = world.report().discrepancies.incidents
    assert (incident.age.run_stopped_by, incident.age.publications_in_run) == ("engine_changed", 1)


def test_an_age_is_never_negative(red17: Corpus) -> None:
    inputs, document = run(red17, "RD-SHORT-PAY")
    world = World()
    world.scope("OP-1", document.versions.profile_ref)
    world.store("OP-1", document, known_at=T0, inputs=inputs)
    (member,) = world.report().members
    age = analytics._age(
        member,
        "redemption.cash_vs_expected",
        world.documents,
        {document.evaluation_id: T0},
        T0 - timedelta(seconds=1),
        analytics.CURRENT,
    )
    assert age.age_seconds is None and age.age_undetermined_reason


# ---------------------------------------------------------------- blocks (C)


def test_e11_categories_overlap_and_not_evaluated_is_never_a_block(sub12: Corpus) -> None:
    inputs, document = run(sub12, "K1")
    world = World()
    world.scope("OP-1", document.versions.profile_ref)
    world.store("OP-1", document, inputs=inputs)
    b = world.report().blockers
    assert {g.category: (g.operations, g.requirements) for g in b.groups} == {
        "insufficient_coverage": (1, 1),
        "missing_evidence": (1, 2),
    }
    assert (b.operations_with_block, b.blocked_requirements) == (1, 3)
    assert b.not_evaluated  # reported apart
    assert b.blocked_requirements == len(b.blocks)
    assert world.replays == [document.evaluation_id]  # one replay for three controls


def test_e19_a_work_limit_never_presents_a_partial_total(red17: Corpus) -> None:
    world = World()
    for scenario in ("RD-PAID", "RD-PENDING", "RD-MISSED"):
        inputs, document = run(red17, scenario)
        world.scope(scenario, document.versions.profile_ref)
        world.store(scenario, document, inputs=inputs)
    b = world.report(max_replays=1).blockers
    assert (b.complete, b.replayed, b.evaluations_requiring_diagnosis) == (False, 1, 3)
    assert b.not_processed == ["RD-PAID", "RD-PENDING"]
    share = next(m for m in b.metrics if m.metric_id == "C.operations_with_block_share_of_admitted")
    assert (share.value_percent, share.undefined_reason) == (None, "incomplete")


# ----------------------------------------------------- cut, versions, filters, empty


def test_e14_the_cut_is_knowledge_time_never_valid_at(sub12: Corpus) -> None:
    _, document = run(sub12, "K2")
    world = World()
    world.scope("OP-1", document.versions.profile_ref)
    # economic time before as_of, knowledge after it: evidence the report must not show
    world.store(
        "OP-1", document, valid_at=AS_OF - timedelta(days=5), known_at=AS_OF + timedelta(hours=1)
    )
    report = world.report()
    assert report.members[0].status == "published_after_cut"
    assert report.distribution.admitted == 0


def test_e13_profiles_and_engines_are_segmented_with_a_comparability_note(
    sub10: Corpus, sub12: Corpus
) -> None:
    world = World()
    for op, corpus in (("OP-10", sub10), ("OP-12", sub12)):
        inputs, document = run(corpus, "K2")
        world.scope(op, document.versions.profile_ref)
        world.store(op, document, inputs=inputs)
    report = world.report()
    assert [(s.profile_ref, s.match_count, s.unknown_count) for s in report.segments] == [
        ("fund-subscription-synthetic@1.0.0", 1, 0),
        ("fund-subscription-synthetic@1.2.0", 0, 1),
    ]
    assert any("mix profile or engine versions" in n for n in report.limitations)


def test_e20_a_profile_filter_is_an_explicit_exclusion(sub10: Corpus, sub12: Corpus) -> None:
    world = World()
    for op, corpus in (("OP-10", sub10), ("OP-12", sub12)):
        inputs, document = run(corpus, "K2")
        world.scope(op, document.versions.profile_ref)
        world.store(op, document, inputs=inputs)
    report = world.report(profiles=["fund-subscription-synthetic@1.2.0"])
    assert [m.operation_ref for m in report.members] == ["OP-12"]
    assert [(e.reason, e.operations) for e in report.exclusions] == [("profile_filter", 1)]


def test_e12_an_empty_population_leaves_every_share_undefined() -> None:
    report = World().report()
    ratios = [
        m
        for s in (report.distribution, report.discrepancies, report.blockers)
        for m in s.metrics
        if m.kind == "ratio"
    ]
    assert ratios
    assert {(m.value_percent, m.undefined_reason) for m in ratios} == {(None, "denominator_zero")}


def test_unregistered_operations_are_visible_and_never_admitted(sub12: Corpus) -> None:
    _, document = run(sub12, "K2")
    world = World()
    world.store("OP-LOOSE", document, publish="no")
    member = world.report().members[0]
    assert (member.status, member.status_detail) == ("unregistered", ["with_evaluations"])


def test_closed_knowledge_cut_is_declared(sub12: Corpus) -> None:
    assert World().report(closed_cut=AS_OF).store_read.as_of_within_closed_cut
    assert (
        not World()
        .report(closed_cut=AS_OF - timedelta(seconds=1))
        .store_read.as_of_within_closed_cut
    )


# ------------------------------------------------------------ cases (D)


def mixed_world(sub12: Corpus, red17: Corpus) -> World:
    world = World()
    for scenario, corpus in (
        ("K1", sub12),
        ("K3", sub12),
        ("RD-MISSED", red17),
        ("RD-PAID", red17),
        ("RD-SHORT-PAY", red17),
    ):
        inputs, document = run(corpus, scenario)
        world.scope(f"OP-{scenario}", document.versions.profile_ref)
        world.store(f"OP-{scenario}", document, inputs=inputs)
    world.scope("OP-NEW-1", "fund-redemption-synthetic@1.7.0")
    world.scope("OP-NEW-2", "fund-redemption-synthetic@1.7.0")
    return world


OPERATION_METRICS = {
    "A.admitted_share",
    "A.match_share_of_admitted",
    "A.break_share_of_admitted",
    "A.unknown_share_of_admitted",
    "A.conclusive_share_of_admitted",
    "A.match_share_of_included",
    "A.technical_error_share_of_admitted",
    "A.included",
    "B.operations_with_discrepancy_share_of_admitted",
    "C.operations_with_block_share_of_admitted",
}


def test_e17_every_operation_figure_equals_its_cases(sub12: Corpus, red17: Corpus) -> None:
    report = mixed_world(sub12, red17).report()
    checked = 0
    for section in (report.distribution, report.discrepancies, report.blockers):
        for m in section.metrics:
            if m.metric_id in OPERATION_METRICS or m.metric_id.startswith("A.status:"):
                assert len(analytics.members_for(report, m.cases_key)) == m.numerator, m.metric_id
                checked += 1
    for g in report.discrepancies.groups:
        assert len(analytics.members_for(report, g.cases_key)) == g.operations
    for b in report.blockers.groups:
        assert len(analytics.members_for(report, b.cases_key)) == b.operations
    assert checked >= len(OPERATION_METRICS)


def test_e16_pages_are_deterministic_disjoint_and_complete(sub12: Corpus, red17: Corpus) -> None:
    report = mixed_world(sub12, red17).report()
    seen: list[str] = []
    for offset in (0, 3, 6):
        total, page = analytics.page_members(report, "included", offset, 3)
        assert total == 7
        seen += [m.operation_ref for m in page]
    assert seen == sorted(seen) == sorted(m.operation_ref for m in report.members)
    again = mixed_world(sub12, red17).report()
    assert again.report_sha256 == report.report_sha256  # same rows, same report


def test_cases_lead_to_the_control_requirement_and_evidence(sub12: Corpus, red17: Corpus) -> None:
    world = mixed_world(sub12, red17)
    report = world.report()
    view = analytics.cases(report, "block:missing_evidence", world.documents, 0, 10)
    assert [c.operation_ref for c in view.cases] == ["OP-K1", "OP-RD-MISSED"]
    k1 = view.cases[0]
    assert {r.control_id for r in k1.reasons} == {
        "subscription.cash_vs_order",
        "subscription.ta_units_vs_order",
    }
    assert all(r.kind == "block" and r.category == "missing_evidence" for r in k1.reasons)
    assert all(r.required_from_source for r in k1.reasons)
    queries = {f.query for f in k1.follow_up}
    assert {"get_timeline", "get_operation_obligations", "diagnose_control"} <= queries
    fails = analytics.cases(report, "discrepancy:any", world.documents, 0, 10)
    k3 = next(c for c in fails.cases if c.operation_ref == "OP-K3")
    (reason,) = k3.reasons
    assert reason.control_id == "subscription.cash_vs_order" and reason.evidence_refs
    assert reason.age is not None and reason.delta is not None
    with pytest.raises(ValueError):
        analytics.members_for(report, "status:invented")


def test_the_report_hash_covers_its_content(sub12: Corpus, red17: Corpus) -> None:
    report = mixed_world(sub12, red17).report()
    assert analytics.report_sha256(report) == report.report_sha256
    tampered = report.model_copy(update={"as_of": AS_OF + timedelta(days=1)})
    assert analytics.report_sha256(tampered) != report.report_sha256


def test_recomputing_from_pinned_members_reproduces_the_sections(
    sub12: Corpus, red17: Corpus
) -> None:
    world = mixed_world(sub12, red17)
    report = world.report()
    sections = analytics.compute_sections(
        report.members, world.documents, report.as_of, world.diagnoser(), report.max_replays
    )
    assert analytics.compare_sections(report, sections) == []


# ------------------------------------------------- compatibility of pinned reports

COMPAT_REPORT = CORPUS.parent / "analytics/integrity-report-1.0.json"


def compat_world(sub10: Corpus, sub12: Corpus, red17: Corpus) -> World:
    """The world behind the pinned 1.0 report fixture (generated before policy @2)."""
    world = World()
    inputs, retired = run(sub10, "K1", engine="invaria-engine@0.9.0")
    world.scope("OP-RETIRED", retired.versions.profile_ref)
    world.store("OP-RETIRED", retired, inputs=inputs)
    world.scope(
        "OP-RUN",
        "fund-redemption-synthetic@1.7.0",
        ("registered", "evidence_appended", "evidence_appended", "evidence_appended"),
    )
    for epoch, scenario, day in (
        (1, "RD-PAID", 0),
        (2, "RD-SHORT-PAY", 1),
        (4, "RD-SHORT-PAY-BEFORE-CORRECTION", 3),
    ):
        inputs, document = run(red17, scenario)
        world.store(
            "OP-RUN", document, epoch=epoch, known_at=T0 + timedelta(days=day), inputs=inputs
        )
    inputs, k1 = run(sub12, "K1")
    world.scope("OP-K1", k1.versions.profile_ref)
    world.store("OP-K1", k1, inputs=inputs)
    world.scope("OP-NEW", k1.versions.profile_ref)
    return world


def test_a_pinned_1_0_report_keeps_its_policy_and_reproduces(
    sub10: Corpus, sub12: Corpus, red17: Corpus
) -> None:
    """Fixture generated with current-publication@1 before @2 existed."""
    report = parse_contract(IntegrityReport, COMPAT_REPORT.read_text("utf-8"))
    assert analytics.report_sha256(report) == report.report_sha256  # unchanged by the 1.1 model
    assert analytics.rules_of(report) is analytics.RULES_V1
    retired = next(m for m in report.members if m.operation_ref == "OP-RETIRED")
    assert retired.status == "admitted"  # policy @1, kept
    world = compat_world(sub10, sub12, red17)
    sections = analytics.compute_sections(
        report.members,
        world.documents,
        report.as_of,
        world.diagnoser(),
        report.max_replays,
        analytics.rules_of(report),
    )
    assert analytics.compare_sections(report, sections) == []
    assert analytics.consistency_problems(report) == []
    (incident,) = report.discrepancies.incidents  # 1.0 age crossed the unpublished epoch
    assert (incident.age.basis, incident.age.epochs_without_publication_in_run) == (
        "published_run_start",
        [3],
    )


# ------------------------------------------------------ report integrity (hash limits)


def rehashed(report: IntegrityReport, edit: Callable[[dict[str, Any]], None]) -> IntegrityReport:
    data = report.model_dump(mode="json")
    edit(data)
    data["report_sha256"] = analytics.report_sha256(data)
    return parse_contract(IntegrityReport, json.dumps(data))


def test_an_edited_figure_with_a_recomputed_hash_is_incoherent(
    sub12: Corpus, red17: Corpus
) -> None:
    report = mixed_world(sub12, red17).report()
    assert analytics.consistency_problems(report) == []

    def numerator(data: dict[str, Any]) -> None:
        data["distribution"]["metrics"][1]["numerator"] += 1

    def drop_case(data: dict[str, Any]) -> None:
        data["members"] = [m for m in data["members"] if m["operation_ref"] != "OP-RD-MISSED"]

    for edit in (numerator, drop_case):
        forged = rehashed(report, edit)
        assert analytics.report_sha256(forged) == forged.report_sha256  # the hash passes
        assert analytics.consistency_problems(forged), edit.__name__


def test_a_coherent_subset_is_not_detectable_without_an_external_anchor(
    sub12: Corpus, red17: Corpus
) -> None:
    """Declared limit: a report rebuilt over fewer members is internally coherent."""
    world = mixed_world(sub12, red17)
    report = world.report()
    subset = [m for m in report.members if m.operation_ref != "OP-RD-MISSED"]
    sections = analytics.compute_sections(
        subset, world.documents, report.as_of, world.diagnoser(), report.max_replays
    )

    def coherent(data: dict[str, Any]) -> None:
        data["members"] = [m.model_dump(mode="json") for m in subset]
        for name in ("distribution", "discrepancies", "blockers"):
            data[name] = getattr(sections, name).model_dump(mode="json")
        data["segments"] = [s.model_dump(mode="json") for s in sections.segments]

    forged = rehashed(report, coherent)
    assert analytics.consistency_problems(forged) == []
    assert forged.report_sha256 != report.report_sha256  # only an external anchor tells
