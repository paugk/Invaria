"""Redemption semantics (profile 1.1.0) on the synthetic corpus: deadlines
with the snapshot clock, coverage-backed absence, position before acceptance, exact amounts,
explicit links, business cancellation and its retraction."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from invaria.bundle.build import build_bundle
from invaria.bundle.verify import verify_bundle
from invaria.cli import mismatches
from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.evaluation import ControlOutcome, EvaluationResult, aggregate
from invaria.contracts.observation import ChainEffectPayload, Observation, TokenMovementPayload
from invaria.contracts.profile import RedemptionProfile, parse_profile
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine import redemption
from invaria.engine.dependencies import dependencies
from invaria.engine.evaluate import ENGINE_REF, Evaluation, EvaluationInputs, evaluate, replay
from invaria.engine.redemption import REDEMPTION_ENGINE_REF

# Current corpus: profile 1.5.0, engine 0.7.0. The historical ones (profile 1.4.0,
# profile 1.3.0, retired engine 0.4.0) are kept to reproduce earlier evaluations.
CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/redemption-synthetic-1.5.0"
PREVIOUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/redemption-synthetic-1.4.0"
HISTORICAL_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/redemption-synthetic"
GENERATOR = HISTORICAL_DIR / "build_corpus.py"
SUBSCRIPTION_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"
DUE = datetime.fromisoformat("2026-10-07T10:00:00Z")
REQUIRED = {
    "RD-PENDING": "UNKNOWN",
    "RD-PAID": "MATCH",
    "RD-PAID-AT-DUE": "MATCH",
    "RD-MISSED": "BREAK",
    "RD-MISSED-REPLAYED": "BREAK",
    "RD-MISSED-NO-COVERAGE": "UNKNOWN",
    "RD-MISSED-COVERAGE-TO-DUE": "UNKNOWN",
    "RD-MISSED-BANK-GAP": "UNKNOWN",
    "RD-LATE": "BREAK",
    "RD-BAD-DUE": "UNKNOWN",
    "RD-BAD-DUE-MISSED": "BREAK",
    "RD-SHORT-PAY": "BREAK",
    "RD-PRICE-MISMATCH": "BREAK",
    "RD-INEXACT": "UNKNOWN",
    "RD-OVER-POSITION": "BREAK",
    "RD-POSITION-RECONSTRUCTED": "MATCH",
    "RD-POSITION-SHORT": "BREAK",
    "RD-POSITION-STALE": "UNKNOWN",
    "RD-POSITION-AMBIGUOUS": "UNKNOWN",
    "RD-BURN-BEFORE-ACCEPTANCE": "UNKNOWN",
    "RD-CANCELLED": "MATCH",
    "RD-CANCELLED-NO-COVERAGE": "UNKNOWN",
    "RD-CANCELLED-PAID": "BREAK",
    "RD-CANCELLED-BURNED": "BREAK",
    "RD-CANCELLED-AFTER-DUE": "BREAK",
    "RD-CANCEL-KNOWN-LATE-BEFORE": "BREAK",
    "RD-CANCEL-KNOWN-LATE-AFTER": "MATCH",
    "RD-CANCEL-UNAUTHORIZED": "UNKNOWN",
    "RD-CANCEL-RETRACTED-BEFORE": "MATCH",
    "RD-CANCEL-RETRACTED-AFTER": "MATCH",
    "RD-CANCEL-RETRACT-CONFLICT": "UNKNOWN",
    "RD-REACTIVATED": "UNKNOWN",
    "RD-RETRACTED": "UNKNOWN",
    "RD-DUPLICATE": "MATCH",
    "RD-RETRACTED-PAID": "UNKNOWN",
    "RD-WRONG-RECIPIENT": "BREAK",
    "RD-UNLINKED-CANDIDATE": "UNKNOWN",
    "RD-UNLINKED-NOT-CANDIDATE": "BREAK",
    "RD-PAID-AFTER-ECONOMIC-CUT": "UNKNOWN",
    "RD-MISSED-CUT-BEFORE-DUE": "UNKNOWN",
    "RD-MISSED-FILTER-COMPATIBLE": "BREAK",
    "RD-MISSED-FILTER-INCOMPATIBLE": "UNKNOWN",
    "RD-CANCELLED-EARLY-BURN": "BREAK",
    "RD-SHORT-PAY-BEFORE-CORRECTION": "BREAK",
    "RD-CORRECTION-KNOWN-LATE": "MATCH",
    "RD-SETTLEMENT-REF-COLLISION": "UNKNOWN",
}


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


def status(evaluation: Evaluation, control: str) -> tuple[str, str]:
    (c,) = [c for c in evaluation.result.controls if c.control_id == f"redemption.{control}"]
    return c.status, c.reason_code


def reason(evaluation: Evaluation, control: str) -> str:
    (c,) = [c for c in evaluation.result.controls if c.control_id == f"redemption.{control}"]
    return c.reason


def variant(
    corpus: Corpus,
    scenario: str,
    *,
    observations: dict[str, Observation] | None = None,
    coverage: dict[str, CoverageCertificate] | None = None,
    **snapshot: Any,
) -> EvaluationInputs:
    """The scenario's inputs with extra/replaced artifacts and snapshot fields."""
    base = corpus.inputs_for(scenario)
    observed = {**base.observations, **(observations or {})}
    certificates = {**base.coverage, **(coverage or {})}
    snap = base.snapshot.model_copy(update=snapshot)
    return dataclasses.replace(base, snapshot=snap, observations=observed, coverage=certificates)


def with_member(corpus: Corpus, scenario: str, extra: Observation, **kw: Any) -> EvaluationInputs:
    members = sorted([*corpus.scenarios[scenario].snapshot.observation_ids, extra.observation_id])
    return variant(
        corpus, scenario, observations={extra.observation_id: extra}, observation_ids=members, **kw
    )


def changed(o: Observation, **fields: Any) -> Observation:
    document = o.model_dump(mode="json")
    for key, value in fields.items():
        target = document
        *path, last = key.split("__")
        for part in path:
            target = target[part]
        target[last] = value
    return Observation.model_validate_json(json.dumps(document))


def cert(c: CoverageCertificate, **fields: Any) -> CoverageCertificate:
    document = c.model_dump(mode="json")
    document.update(fields)
    return CoverageCertificate.model_validate_json(json.dumps(document))


def swap_cert(
    corpus: Corpus, scenario: str, old: str, new: CoverageCertificate
) -> EvaluationInputs:
    ids = [i for i in corpus.scenarios[scenario].snapshot.coverage_ids if i != old]
    return variant(
        corpus,
        scenario,
        coverage={new.coverage_id: new},
        coverage_ids=sorted([*ids, new.coverage_id]),
    )


# ------------------------------------------------------------------- corpus


def test_required_scenarios_are_specified(corpus: Corpus) -> None:
    assert {s: corpus.scenarios[s].expected.result for s in corpus.scenarios} == REQUIRED
    assert isinstance(corpus.profile, RedemptionProfile)
    assert corpus.profile.profile_ref == "fund-redemption-synthetic@1.5.0"


@pytest.mark.parametrize("scenario_id", sorted(REQUIRED))
def test_every_scenario_reproduces_its_expected_result(corpus: Corpus, scenario_id: str) -> None:
    evaluation = evaluate(corpus.inputs_for(scenario_id))
    assert mismatches(corpus, scenario_id, evaluation) == []
    assert evaluation.result.versions.engine_ref == REDEMPTION_ENGINE_REF


@pytest.mark.parametrize(
    "args",
    [[], ["--profile=1.4.0"], ["--profile=1.5.0"], ["--profile=1.6.0"], ["--profile=1.7.0"]],
)
def test_committed_corpus_equals_the_generator_output(args: list[str]) -> None:
    run = subprocess.run(
        [sys.executable, str(GENERATOR), "--check", *args], capture_output=True, text=True
    )
    assert run.returncode == 0, run.stdout


@pytest.fixture(scope="module")
def historical() -> Corpus:
    return load_corpus(HISTORICAL_DIR)


def test_the_historical_corpus_reproduces_with_its_retired_engine(historical: Corpus) -> None:
    """Profile 1.3.0 evaluations, recorded with 0.4.0, are reproduced with 0.4.0 only."""
    assert historical.profile.profile_ref == "fund-redemption-synthetic@1.3.0"
    for scenario_id in sorted(REQUIRED):
        old = replay(historical.inputs_for(scenario_id), "invaria-redemption-engine@0.4.0")
        assert mismatches(historical, scenario_id, old) == [], scenario_id


@pytest.mark.parametrize("older_dir", [HISTORICAL_DIR, PREVIOUS_DIR], ids=["1.3.0", "1.4.0"])
def test_the_current_corpus_differs_only_in_its_profile_version(
    corpus: Corpus, older_dir: Path
) -> None:
    """Same raw bytes, observations, coverage and hand-written expectations; only the
    profile (1.4.0 declares quarantine_scope on the chain requirement, 1.5.0 its
    quarantine_policy) and its refs."""
    older = load_corpus(older_dir)
    assert corpus.profile.profile_ref == "fund-redemption-synthetic@1.5.0"
    assert sorted(p.name for p in (CORPUS_DIR / "raw").iterdir()) == sorted(
        p.name for p in (older_dir / "raw").iterdir()
    )
    for raw in sorted((older_dir / "raw").iterdir()):
        assert (CORPUS_DIR / "raw" / raw.name).read_bytes() == raw.read_bytes()
    assert corpus.coverage == older.coverage
    assert corpus.journals == older.journals
    for scenario_id in sorted(REQUIRED):
        assert corpus.scenarios[scenario_id].expected == older.scenarios[scenario_id].expected
    old = older.profile.model_dump(mode="json")
    new = corpus.profile.model_dump(mode="json")
    assert {k for k in old if old[k] != new[k]} == {
        "profile_ref",
        "rules_ref",
        "disclaimer",
        "coverage_requirements",
    }
    scoped = [r for r in new["coverage_requirements"] if r.get("quarantine_scope")]
    assert [r["source_id"] for r in scoped] == ["stellar-testnet-frozen"]
    assert [r["source_id"] for r in new["coverage_requirements"] if r.get("quarantine_policy")] == [
        "stellar-testnet-frozen"
    ]


def test_the_previous_corpus_keeps_its_results_under_the_current_engine() -> None:
    """Profile 1.4.0 declares no quarantine policy: 0.7.0 evaluates it in the mode that
    keeps its promise and every expectation still holds."""
    previous = load_corpus(PREVIOUS_DIR)
    for scenario_id in sorted(REQUIRED):
        evaluation = evaluate(previous.inputs_for(scenario_id))
        assert mismatches(previous, scenario_id, evaluation) == [], scenario_id


def test_corpus_is_synthetic_and_hashes_its_raw_bytes(corpus: Corpus) -> None:
    for journal in corpus.journals.values():
        for o in journal.values():
            assert o.synthetic
            data = (CORPUS_DIR / o.provenance.raw_locator).read_bytes()
            assert hashlib.sha256(data).hexdigest() == o.provenance.raw_sha256
            assert json.loads(data)["synthetic"] is True
    assert all(c.synthetic for c in corpus.coverage.values())


def test_snapshots_hold_exactly_what_was_recorded_by_known_at(corpus: Corpus) -> None:
    for scenario in corpus.scenarios.values():
        snap = scenario.snapshot
        journal = corpus.journals[scenario.timeline_id]
        recorded = sorted(i for i, o in journal.items() if o.recorded_at <= snap.known_at)
        assert snap.observation_ids == recorded, scenario.scenario_id
        assert all(corpus.coverage[c].recorded_at <= snap.known_at for c in snap.coverage_ids)


# ------------------------------------------------------------- cancellation (point 1)


def test_cancelled_state_is_apart_from_the_result(corpus: Corpus) -> None:
    cancelled = evaluate(corpus.inputs_for("RD-CANCELLED")).result
    assert (cancelled.result, cancelled.operation_state) == ("MATCH", "cancelled")
    extinguished = [
        c for c in cancelled.controls if c.reason_code == "EXTINGUISHED_BY_CANCELLATION"
    ]
    assert {c.control_id.split(".")[1] for c in extinguished} == {
        "cash_vs_expected",
        "payment_deadline",
        "ta_units_vs_request",
        "burn_vs_request",
    }
    assert all(c.reason for c in extinguished)
    assert '"operation_state":"cancelled"' in cancelled.model_dump_json()
    paid = evaluate(corpus.inputs_for("RD-PAID")).result
    assert paid.operation_state is None and "operation_state" not in paid.model_dump_json()


def test_a_cancelled_match_rests_on_explicit_passing_controls(corpus: Corpus) -> None:
    for scenario in corpus.scenarios.values():
        result = evaluate(corpus.inputs_for(scenario.scenario_id))
        if result.result.operation_state == "cancelled" and result.result.result == "MATCH":
            assert status(result, "cancellation_valid") == ("PASS", "EXACT_MATCH")
            assert status(result, "no_settlement_after_cancellation") == ("PASS", "EXACT_MATCH")
    only_not_applicable = [
        ControlOutcome(
            control_id="x",
            mandatory=True,
            status="NOT_APPLICABLE",
            reason_code="EXTINGUISHED_BY_CANCELLATION",
            delta=None,
        )
    ]
    assert aggregate(only_not_applicable) == "UNKNOWN"


def test_cancellation_uses_effective_time_not_recording_time(corpus: Corpus) -> None:
    """A cancellation recorded early but effective after the due time keeps the breach."""
    scheduled = changed(
        corpus.journals["cancelled-after-due"]["obs-CN-LATE"],
        recorded_at="2026-10-06T10:00:00Z",
    )
    after = evaluate(
        variant(corpus, "RD-CANCELLED-AFTER-DUE", observations={"obs-CN-LATE": scheduled})
    )
    assert status(after, "payment_deadline") == ("FAIL", "PAYMENT_MISSED")
    assert after.result.operation_state == "cancelled"
    # Before its effective time it is not in force: nothing is extinguished yet.
    s2 = datetime.fromisoformat("2026-10-06T16:00:00Z")
    before = evaluate(
        variant(
            corpus,
            "RD-CANCELLED",
            observations={"obs-CN-LATE": scheduled},
            observation_ids=["obs-CN-LATE", "obs-PR1", "obs-PS1", "obs-RQ1"],
            valid_at=s2,
            known_at=s2,
            evaluation_clock=s2,
        )
    )
    assert before.result.operation_state is None
    assert status(before, "payment_deadline") == ("UNKNOWN", "PAYMENT_NOT_DUE")


def test_known_late_cancellation_changes_the_present_not_the_past(corpus: Corpus) -> None:
    earlier = evaluate(corpus.inputs_for("RD-CANCEL-KNOWN-LATE-BEFORE"))
    later = evaluate(corpus.inputs_for("RD-CANCEL-KNOWN-LATE-AFTER"))
    assert (earlier.result.result, earlier.result.operation_state) == ("BREAK", None)
    assert (later.result.result, later.result.operation_state) == ("MATCH", "cancelled")
    assert "recorded 2026-10-07T15:00:00Z" in reason(later, "cancellation_valid")


def test_cancellation_before_acceptance_is_invalid(corpus: Corpus) -> None:
    cancel = corpus.journals["cancelled"]["obs-CN1"]
    early = changed(
        cancel, payload__cancelled_at="2026-10-05T09:00:00Z", valid_time="2026-10-05T09:00:00Z"
    )
    evaluation = evaluate(variant(corpus, "RD-CANCELLED", observations={"obs-CN1": early}))
    assert status(evaluation, "cancellation_valid") == ("UNKNOWN", "INVALID_CANCELLATION")
    assert evaluation.result.operation_state is None
    assert status(evaluation, "cash_vs_expected")[1] != "EXTINGUISHED_BY_CANCELLATION"


def test_cancellation_effective_time_must_match_its_record(corpus: Corpus) -> None:
    cancel = corpus.journals["cancelled"]["obs-CN1"]
    odd = changed(cancel, payload__cancelled_at="2026-10-05T11:30:00Z")
    evaluation = evaluate(variant(corpus, "RD-CANCELLED", observations={"obs-CN1": odd}))
    assert status(evaluation, "cancellation_valid") == ("UNKNOWN", "INVALID_CANCELLATION")
    assert "effective time differs" in reason(evaluation, "cancellation_valid")


def test_no_cancellation_needs_ta_coverage_to_valid_at(corpus: Corpus) -> None:
    """A TA certificate that covers everything except cancellations cannot rule one out."""
    blind = cert(
        corpus.coverage["cov-ta-s2"],
        coverage_id="cov-ta-blind",
        fact_types=["redemption_accepted", "position_held", "position_changed", "units_registered"],
    )
    evaluation = evaluate(swap_cert(corpus, "RD-PAID", "cov-ta-s2", blind))
    assert status(evaluation, "cancellation_valid") == ("UNKNOWN", "INSUFFICIENT_COVERAGE")
    assert status(evaluation, "ta_units_vs_request") == ("PASS", "EXACT_MATCH")
    assert evaluation.result.result == "UNKNOWN"


# ------------------------------------------------------- retraction (point 5)


def test_valid_retraction_keeps_the_original_due_time(corpus: Corpus) -> None:
    after = evaluate(corpus.inputs_for("RD-CANCEL-RETRACTED-AFTER"))
    assert status(after, "cancellation_valid") == ("NOT_APPLICABLE", "NO_CANCELLATION")
    assert "evidence correction" in reason(after, "cancellation_valid")
    assert "computed 2026-10-07T10:00:00Z" in reason(after, "payment_deadline")
    before = evaluate(corpus.inputs_for("RD-CANCEL-RETRACTED-BEFORE"))
    assert before.result.operation_state == "cancelled"


def test_retraction_with_insufficient_ta_coverage_is_undecided(corpus: Corpus) -> None:
    blind = cert(
        corpus.coverage["cov-ta-s2"],
        coverage_id="cov-ta-blind",
        fact_types=["redemption_accepted", "position_held", "position_changed", "units_registered"],
    )
    evaluation = evaluate(swap_cert(corpus, "RD-CANCEL-RETRACTED-AFTER", "cov-ta-s2", blind))
    assert status(evaluation, "cancellation_valid") == ("UNKNOWN", "INSUFFICIENT_COVERAGE")


def test_retraction_must_name_the_revision_it_withdraws(corpus: Corpus) -> None:
    retraction = corpus.journals["cancel-retracted"]["obs-CN1-R"]
    stray = changed(retraction, supersedes="obs-RQ1")
    evaluation = evaluate(
        variant(corpus, "RD-CANCEL-RETRACTED-AFTER", observations={"obs-CN1-R": stray})
    )
    assert status(evaluation, "cancellation_valid") == ("UNKNOWN", "INVALID_RETRACTION")
    assert evaluation.result.result == "UNKNOWN"


def test_invalid_retraction_never_silently_withdraws_a_cancellation(corpus: Corpus) -> None:
    """D-5: the retraction must resolve; otherwise both pieces of evidence stay visible."""
    retraction = corpus.journals["cancel-retracted"]["obs-CN1-R"]
    # (a) In another record, naming the cancellation: the cancellation record still asserts.
    elsewhere = changed(retraction, source__record_key="CAN-RED-OTHER")
    a = evaluate(
        variant(corpus, "RD-CANCEL-RETRACTED-AFTER", observations={"obs-CN1-R": elsewhere})
    )
    # (b) Naming a revision the snapshot does not keep.
    members = [
        i
        for i in corpus.scenarios["RD-CANCEL-RETRACTED-AFTER"].snapshot.observation_ids
        if i != "obs-CN1"
    ]
    b = evaluate(variant(corpus, "RD-CANCEL-RETRACTED-AFTER", observation_ids=members))
    for evaluation in (a, b):
        assert status(evaluation, "cancellation_valid") == ("UNKNOWN", "INVALID_RETRACTION")
        assert evaluation.result.operation_state is None
        assert evaluation.result.result == "UNKNOWN"
        (c,) = [
            c for c in evaluation.result.controls if c.control_id.endswith("cancellation_valid")
        ]
        assert {"obs-CN1-R", "obs-CN1"} <= set(c.evidence_refs)


# ------------------------------------------------------------ position (point 2)


def test_position_after_the_acceptance_is_not_evidence(corpus: Corpus) -> None:
    after = changed(corpus.journals["main"]["obs-PS1"], payload__sequence=1006)
    evaluation = evaluate(variant(corpus, "RD-PAID", observations={"obs-PS1": after}))
    assert status(evaluation, "units_within_position") == (
        "UNKNOWN",
        "POSITION_NOT_BEFORE_ACCEPTANCE",
    )


def test_reservations_are_subtracted(corpus: Corpus) -> None:
    reserved = changed(
        corpus.journals["main"]["obs-PS1"],
        payload__units={"atoms": "1000000000", "scale": 7, "unit": "FUND_SHARE"},
        payload__reserved={"atoms": "10000000", "scale": 7, "unit": "FUND_SHARE"},
    )
    evaluation = evaluate(variant(corpus, "RD-PAID", observations={"obs-PS1": reserved}))
    assert status(evaluation, "units_within_position") == ("FAIL", "UNITS_EXCEED_POSITION")
    ops = evaluation.operands["redemption.units_within_position"]
    assert ops.right.atoms == "990000000"


@pytest.mark.parametrize(
    ("fields", "detail"),
    [
        ({"payload__sequence": 900}, "shares a sequence"),
        (
            {"payload__effective_at": "2026-10-05T11:00:00Z", "valid_time": "2026-10-05T09:30:00Z"},
            "time and sequence disagree",
        ),
        (
            {"payload__request_ref": "RED-0001", "operation_ref": "RED-0001"},
            "before its acceptance",
        ),
    ],
    ids=["shared-sequence", "time-vs-sequence", "own-reservation-before-acceptance"],
)
def test_ambiguous_journal_order_is_unknown(
    corpus: Corpus, fields: dict[str, Any], detail: str
) -> None:
    entry = changed(corpus.journals["position-reconstructed"]["obs-CH-0960"], **fields)
    evaluation = evaluate(
        variant(corpus, "RD-POSITION-RECONSTRUCTED", observations={"obs-CH-0960": entry})
    )
    assert status(evaluation, "units_within_position") == ("UNKNOWN", "AMBIGUOUS_SEQUENCE")
    assert detail in reason(evaluation, "units_within_position")


def test_reconstruction_skips_the_requests_own_later_reservation(corpus: Corpus) -> None:
    evaluation = evaluate(corpus.inputs_for("RD-POSITION-RECONSTRUCTED"))
    ops = evaluation.operands["redemption.units_within_position"]
    assert ops.right.atoms == "10200000000"  # 1,000 + 50 - 30; J-1006 (own, after) excluded
    (c,) = [c for c in evaluation.result.controls if c.control_id.endswith("units_within_position")]
    assert "obs-CH-1006" not in c.evidence_refs


# ------------------------------------------------------------ burn (point 3)


def test_early_burn_is_kept_but_never_fulfils_retirement(corpus: Corpus) -> None:
    evaluation = evaluate(corpus.inputs_for("RD-BURN-BEFORE-ACCEPTANCE"))
    (c,) = [c for c in evaluation.result.controls if c.control_id.endswith("burn_vs_request")]
    assert (
        c.reason_code == "BURN_BEFORE_ACCEPTANCE_UNSUPPORTED" and "obs-BN-EARLY" in c.evidence_refs
    )
    assert "obs-BN-EARLY" not in evaluation.effective_observation_ids


def test_a_redelivered_burn_counts_once(corpus: Corpus) -> None:
    burn = corpus.journals["main"]["obs-BN1"]
    again = changed(burn, observation_id="obs-BN1-resent", recorded_at="2026-10-05T15:00:00Z")
    evaluation = evaluate(with_member(corpus, "RD-PAID", again))
    assert status(evaluation, "burn_vs_request") == ("PASS", "EXACT_MATCH")
    assert evaluation.operands["redemption.burn_vs_request"].left.atoms == "1000000000"


# ------------------------------------------------------ deadline (point 4)


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(seconds=-1), ("PASS", "EXACT_MATCH")),
        (timedelta(0), ("PASS", "EXACT_MATCH")),
        (timedelta(seconds=1), ("FAIL", "PAYMENT_LATE")),
    ],
)
def test_payment_on_the_due_second_is_on_time(
    corpus: Corpus, offset: timedelta, expected: tuple[str, str]
) -> None:
    paid = corpus.journals["at-due"]["obs-PY-DUE"]
    moved = changed(paid, valid_time=(DUE + offset).isoformat())
    evaluation = evaluate(
        variant(corpus, "RD-PAID-AT-DUE", observations={paid.observation_id: moved})
    )
    assert status(evaluation, "payment_deadline") == expected


def test_absence_turns_into_a_breach_only_after_the_due_time(corpus: Corpus) -> None:
    for clock, expected in (
        (DUE, ("UNKNOWN", "PAYMENT_NOT_DUE")),
        (DUE + timedelta(seconds=1), ("FAIL", "PAYMENT_MISSED")),
    ):
        edge = {
            f"cov-{name}-edge": cert(
                corpus.coverage[f"cov-{name}-s3"],
                coverage_id=f"cov-{name}-edge",
                recorded_at=clock.isoformat(),
                interval={"start": "2026-10-04T00:00:00Z", "end": clock.isoformat()},
            )
            for name in ("bank", "chain", "price", "ta")
        }
        inputs = variant(
            corpus,
            "RD-MISSED",
            coverage=edge,
            known_at=clock,
            valid_at=clock,
            evaluation_clock=clock,
            coverage_ids=sorted(edge),
        )
        assert status(evaluate(inputs), "payment_deadline") == expected, clock


@pytest.mark.parametrize(
    ("end", "expected"),
    [
        ("2026-10-07T09:59:59Z", ("UNKNOWN", "INSUFFICIENT_COVERAGE")),
        ("2026-10-07T10:00:00Z", ("UNKNOWN", "INSUFFICIENT_COVERAGE")),
        ("2026-10-07T10:00:01Z", ("FAIL", "PAYMENT_MISSED")),
    ],
    ids=["before-due", "half-open-end-at-due", "contains-due"],
)
def test_bank_coverage_must_contain_the_due_instant(
    corpus: Corpus, end: str, expected: tuple[str, str]
) -> None:
    bank = cert(
        corpus.coverage["cov-bank-s3"],
        coverage_id="cov-bank-edge",
        interval={"start": "2026-10-04T00:00:00Z", "end": end},
    )
    evaluation = evaluate(swap_cert(corpus, "RD-MISSED", "cov-bank-s3", bank))
    assert status(evaluation, "payment_deadline") == expected


@pytest.mark.parametrize(
    ("scope", "detail"),
    [
        (None, "declares no account scope"),
        ({"accounts": ["acct-other"], "currencies": ["USD"], "filters": []}, "account"),
        ({"accounts": ["acct-pseudo-0001"], "currencies": ["EUR"], "filters": []}, "currency USD"),
        (
            {
                "accounts": ["acct-pseudo-0001"],
                "currencies": ["USD"],
                "filters": [{"field": "status", "values": ["SETTLED"]}],
            },
            "may exclude records",
        ),
    ],
    ids=["no-scope", "other-account", "other-currency", "filtered"],
)
def test_bank_coverage_must_be_scoped_to_account_and_currency(
    corpus: Corpus, scope: dict[str, Any] | None, detail: str
) -> None:
    bank = cert(corpus.coverage["cov-bank-s3"], coverage_id="cov-bank-scoped", scope=scope)
    evaluation = evaluate(swap_cert(corpus, "RD-MISSED", "cov-bank-s3", bank))
    assert status(evaluation, "payment_deadline") == ("UNKNOWN", "INSUFFICIENT_COVERAGE")
    assert detail in reason(evaluation, "payment_deadline")


def test_uncertain_acceptance_leaves_the_deadline_unknown(corpus: Corpus) -> None:
    request = corpus.journals["main"]["obs-RQ1"]
    odd = changed(request, valid_time="2026-10-05T10:30:00Z")
    evaluation = evaluate(variant(corpus, "RD-MISSED", observations={"obs-RQ1": odd}))
    assert status(evaluation, "declared_due_consistency") == ("UNKNOWN", "DEADLINE_DATA_CONFLICT")
    assert status(evaluation, "payment_deadline") == ("UNKNOWN", "DEADLINE_DATA_CONFLICT")
    assert evaluation.result.result == "UNKNOWN"


def test_declared_and_computed_due_are_both_kept(corpus: Corpus) -> None:
    evaluation = evaluate(corpus.inputs_for("RD-BAD-DUE-MISSED"))
    text = reason(evaluation, "payment_deadline")
    assert "declared payment_due_at 2026-10-07T17:00:00Z" in text
    assert "computed 2026-10-07T10:00:00Z" in text and "redemption-synthetic-rules@1.5.0" in text


def test_valid_at_does_not_move_the_due_time(corpus: Corpus) -> None:
    """An earlier economic cut keeps the due time; it only stops absence being judged."""
    earlier = datetime.fromisoformat("2026-10-06T20:00:00Z")
    evaluation = evaluate(variant(corpus, "RD-MISSED", valid_at=earlier))
    assert status(evaluation, "payment_deadline") == ("UNKNOWN", "PAYMENT_NOT_DUE")
    assert "due 2026-10-07T10:00:00Z" in reason(evaluation, "payment_deadline")
    assert "computed 2026-10-07T10:00:00Z" in reason(evaluation, "declared_due_consistency")
    later = datetime.fromisoformat("2026-10-07T10:00:01Z")
    judged = evaluate(variant(corpus, "RD-MISSED", valid_at=later))
    assert status(judged, "payment_deadline") == ("FAIL", "PAYMENT_MISSED")


def test_the_clock_is_the_snapshot_clock_never_the_system_time(corpus: Corpus) -> None:
    source = inspect.getsource(redemption)
    for forbidden in ("now(", "utcnow", "today(", "time.time", "import time"):
        assert forbidden not in source
    later = datetime.fromisoformat("2026-10-06T23:00:00Z")  # still before the due time
    evaluation = evaluate(variant(corpus, "RD-PENDING", evaluation_clock=later))
    assert status(evaluation, "payment_deadline") == ("UNKNOWN", "PAYMENT_NOT_DUE")


def test_a_late_payment_completes_settlement_but_not_the_deadline(corpus: Corpus) -> None:
    late = evaluate(corpus.inputs_for("RD-LATE"))
    assert status(late, "cash_vs_expected") == ("PASS", "EXACT_MATCH")
    assert status(late, "payment_deadline") == ("FAIL", "PAYMENT_LATE")
    assert "does not erase the breach" in reason(late, "payment_deadline")
    historical = evaluate(corpus.inputs_for("RD-MISSED-REPLAYED"))
    assert historical.result.result == "BREAK"
    assert "obs-PY-LATE" not in historical.effective_observation_ids


# ------------------------------------------------------------ amounts, links


def test_price_is_chosen_by_price_ref_only(corpus: Corpus) -> None:
    other = changed(
        corpus.journals["main"]["obs-PR1"],
        observation_id="obs-PR-OTHER",
        source__record_key="price:DEMO-A:2026-10-05",
        payload__price_ref="price:DEMO-A:2026-10-05",
        payload__price_per_unit={"atoms": "9000", "scale": 2, "unit": "USD"},
        recorded_at="2026-10-05T22:05:00Z",
        valid_time="2026-10-05T22:00:00Z",
    )
    evaluation = evaluate(with_member(corpus, "RD-PAID", other))
    assert evaluation.result.result == "MATCH"
    assert "obs-PR-OTHER" not in evaluation.effective_observation_ids
    without = sorted(
        i for i in corpus.scenarios["RD-PAID"].snapshot.observation_ids if i != "obs-PR1"
    )
    missing = evaluate(variant(corpus, "RD-PAID", observation_ids=without))
    assert status(missing, "price_vs_approved") == ("UNKNOWN", "MISSING_EVIDENCE")


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {"payload__to_address": "GA2JRO6WKTHMJTH7TDAXHNDHWT5SF72VKABPO2G6RL5BCGKTNIPIBEKW"},
            ("UNKNOWN", "UNSUPPORTED_CAPABILITY"),
        ),
        ({"operation_ref": None}, ("UNKNOWN", "AMBIGUOUS_MATCH")),
        ({"payload__chain__tx_successful": False}, ("UNKNOWN", "MISSING_EVIDENCE")),
        (
            {"payload__from_address": "GDMYHWUG6BLHEGQSTMCAGUFUZFRZ6XGXJSHDKMDUZ6IRCKJ4FGZCCZVZ"},
            ("UNKNOWN", "AMBIGUOUS_MATCH"),
        ),
    ],
    ids=["transfer-not-burn", "no-execution-link", "failed-tx", "unlinked-source"],
)
def test_retirement_is_an_explicitly_linked_burn(
    corpus: Corpus, fields: dict[str, Any], expected: tuple[str, str]
) -> None:
    burn = corpus.journals["main"]["obs-BN1"]
    evaluation = evaluate(
        variant(corpus, "RD-PAID", observations={"obs-BN1": changed(burn, **fields)})
    )
    assert status(evaluation, "burn_vs_request") == expected
    assert evaluation.result.result != "MATCH"


def test_linked_payment_to_another_account_is_a_wrong_recipient(corpus: Corpus) -> None:
    evaluation = evaluate(corpus.inputs_for("RD-WRONG-RECIPIENT"))
    assert status(evaluation, "cash_vs_expected") == ("FAIL", "PAYMENT_TO_WRONG_ACCOUNT")
    assert status(evaluation, "payment_deadline") == ("UNKNOWN", "PAYMENT_NOT_DUE")
    wrong = corpus.journals["wrong-recipient"]["obs-PY-WRONG"]
    after_due = evaluate(with_member(corpus, "RD-MISSED", wrong))
    assert status(after_due, "payment_deadline") == ("FAIL", "PAYMENT_MISSED")
    assert "went to acct-pseudo-0002" in reason(after_due, "payment_deadline")


def test_wrong_recipient_needs_comparable_account_identities(corpus: Corpus) -> None:
    profile = corpus.profile
    assert isinstance(profile, RedemptionProfile)
    per_source = profile.model_copy(
        update={
            "correlation": profile.correlation.model_copy(
                update={"account_namespace": "per_source"}
            )
        }
    )
    inputs = dataclasses.replace(corpus.inputs_for("RD-WRONG-RECIPIENT"), profile=per_source)
    evaluation = evaluate(inputs)
    assert status(evaluation, "cash_vs_expected") == ("UNKNOWN", "AMBIGUOUS_MATCH")


def test_exact_operands_and_delta(corpus: Corpus) -> None:
    short = evaluate(corpus.inputs_for("RD-SHORT-PAY"))
    ops = short.operands["redemption.cash_vs_expected"]
    assert (ops.left.atoms, ops.right.atoms, ops.left.scale, ops.left.unit) == (
        "995000",
        "1000000",
        2,
        "USD",
    )
    over = evaluate(corpus.inputs_for("RD-OVER-POSITION"))
    (c,) = [c for c in over.result.controls if c.reason_code == "UNITS_EXCEED_POSITION"]
    assert c.delta is not None and (c.delta.atoms, c.delta.scale) == ("500000000", 7)


# ----------------------------------------------- regressions from the domain review


def test_proven_settlement_wins_over_another_undecided_view(corpus: Corpus) -> None:
    unlinked = changed(
        corpus.journals["main"]["obs-BN1"], observation_id="obs-BN-UNLINKED", operation_ref=None
    )
    evaluation = evaluate(with_member(corpus, "RD-CANCELLED-PAID", unlinked))
    assert status(evaluation, "no_settlement_after_cancellation") == (
        "FAIL",
        "SETTLED_DESPITE_CANCELLATION",
    )


def test_two_linked_payments_after_cancellation_are_settlement(corpus: Corpus) -> None:
    second = changed(
        corpus.journals["cancelled-paid"]["obs-PY1"],
        observation_id="obs-PY2",
        source__record_key="PAY-RED-0002",
        payload__payment_ref="PAY-RED-0002",
    )
    evaluation = evaluate(with_member(corpus, "RD-CANCELLED-PAID", second))
    assert status(evaluation, "no_settlement_after_cancellation")[1] == (
        "SETTLED_DESPITE_CANCELLATION"
    )


def test_facts_effective_after_the_clock_do_not_count_yet(corpus: Corpus) -> None:
    future = changed(corpus.journals["main"]["obs-PY1"], valid_time="2026-10-07T09:00:00Z")
    evaluation = evaluate(variant(corpus, "RD-PAID", observations={"obs-PY1": future}))
    assert status(evaluation, "payment_deadline") == ("UNKNOWN", "PAYMENT_NOT_DUE")
    assert evaluation.result.result == "UNKNOWN"


def test_unlinked_payment_is_ambiguous_only_as_a_relevant_candidate(corpus: Corpus) -> None:
    candidate = evaluate(corpus.inputs_for("RD-UNLINKED-CANDIDATE"))
    assert status(candidate, "payment_deadline") == ("UNKNOWN", "AMBIGUOUS_MATCH")
    assert "settlement_ref PAY-RED-0001" in reason(candidate, "payment_deadline")
    same_account = evaluate(corpus.inputs_for("RD-UNLINKED-NOT-CANDIDATE"))
    assert status(same_account, "payment_deadline") == ("FAIL", "PAYMENT_MISSED")
    assert "obs-PY-UNLINKED" not in same_account.effective_observation_ids
    unlinked = corpus.journals["unlinked-candidate"]["obs-PY-UNLINKED"]
    assert dependencies(corpus.profile, "RED-0001").matches_observation(unlinked, [None])


def test_unlinked_transfer_that_is_not_a_burn_is_ignored(corpus: Corpus) -> None:
    transfer = changed(
        corpus.journals["main"]["obs-BN1"],
        observation_id="obs-TR-OTHER",
        operation_ref=None,
        source__record_key="other-transfer:0",
        payload__to_address="GA2JRO6WKTHMJTH7TDAXHNDHWT5SF72VKABPO2G6RL5BCGKTNIPIBEKW",
    )
    assert evaluate(with_member(corpus, "RD-PAID", transfer)).result.result == "MATCH"


def test_dependency_predicates(corpus: Corpus) -> None:
    deps = dependencies(corpus.profile, "RED-0001")
    price = corpus.journals["main"]["obs-PR1"]
    assert deps.matches_observation(price, [None])
    assert deps.matches_observation(changed(price, operation_ref="RED-9999"), ["RED-9999"])
    journal = corpus.journals["position-reconstructed"]["obs-CH-0960"]
    assert journal.operation_ref == "RED-0000"
    assert deps.matches_observation(journal, ["RED-0000"])
    cancel = corpus.journals["cancelled"]["obs-CN1"]
    assert deps.matches_observation(cancel, ["RED-0001"])
    assert not deps.matches_observation(cancel, ["RED-9999"])
    subscription = load_corpus(SUBSCRIPTION_DIR)
    unlinked_cash = changed(subscription.journals["main"]["obs-B1"], operation_ref=None)
    assert not dependencies(subscription.profile, "SUB-0001").matches_observation(
        unlinked_cash, [None]
    )


# ------------------------------------------------------- engine and contracts


def test_engine_is_pinned_to_the_profile_type(corpus: Corpus) -> None:
    wrong = evaluate(corpus.inputs_for("RD-PAID"), engine_ref=ENGINE_REF)
    assert {c.reason_code for c in wrong.result.controls} == {"EVALUATION_ERROR"}
    for old in (
        "invaria-redemption-engine@0.1.0",
        "invaria-redemption-engine@0.2.0",
        "invaria-redemption-engine@0.3.0",
    ):
        retired = evaluate(corpus.inputs_for("RD-PAID"), engine_ref=old)
        assert {c.reason_code for c in retired.result.controls} == {"EVALUATION_ERROR"}
    subscription = load_corpus(SUBSCRIPTION_DIR)
    crossed = evaluate(subscription.inputs_for("K2"), engine_ref=REDEMPTION_ENGINE_REF)
    assert {c.reason_code for c in crossed.result.controls} == {"EVALUATION_ERROR"}
    assert evaluate(subscription.inputs_for("K2")).result.result == "MATCH"


def test_subscription_artifacts_carry_no_new_fields() -> None:
    subscription = load_corpus(SUBSCRIPTION_DIR)
    result = evaluate(subscription.inputs_for("K3")).result
    assert "operation_state" not in result.model_dump_json()
    golden = Path(__file__).resolve().parents[1] / "fixtures/bundles/K3"
    assert "operation_state" not in (golden / "evaluation.json").read_text()
    assert '"scope"' not in (golden / "evidence.json").read_text()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p["controls"].reverse(), "controls must be exactly"),
        (lambda p: p["controls"][0].update(mandatory=False), "mandatory"),
        (lambda p: p["sources"][1].update(kind="transfer_agent"), "cash_settled must come"),
        (lambda p: p["sources"][2].update(authoritative_for=["cash_settled"]), "two sources"),
        (lambda p: p["coverage_requirements"][1].update(scope="account"), "must be scoped"),
        (lambda p: p["controls"][3].update(coverage="absence_interval"), "coverage must be"),
    ],
    ids=[
        "control-order",
        "optional-control",
        "cash-from-ta",
        "double-authority",
        "bank-scope",
        "control-coverage",
    ],
)
def test_profile_contract_rejects_other_semantics(mutate: Any, message: str) -> None:
    document = json.loads((CORPUS_DIR / "profile.json").read_text())
    mutate(document)
    with pytest.raises(ValueError, match=message):
        parse_profile(json.dumps(document))


def test_extinguished_controls_require_the_cancelled_state(corpus: Corpus) -> None:
    document = evaluate(corpus.inputs_for("RD-CANCELLED")).result.model_dump(mode="json")
    del document["operation_state"]
    with pytest.raises(ValueError, match="require operation_state"):
        EvaluationResult.model_validate_json(json.dumps(document))


# ------------------------------------------------------------------ bundles


@pytest.mark.parametrize("scenario_id", sorted(REQUIRED))
def test_every_scenario_bundle_reproduces_offline_at_r1(
    corpus: Corpus, tmp_path: Path, scenario_id: str
) -> None:
    inputs = corpus.inputs_for(scenario_id)
    out = tmp_path / scenario_id
    build_bundle(out, inputs, evaluate(inputs), mode=corpus.scenarios[scenario_id].query_mode)
    report = verify_bundle(out)
    assert report.status == "REPRODUCED", report.reasons
    assert report.financial_result == REQUIRED[scenario_id]
    assert report.local_engine_ref == REDEMPTION_ENGINE_REF


def test_r2_is_not_available_for_redemptions_yet(corpus: Corpus, tmp_path: Path) -> None:
    inputs = corpus.inputs_for("RD-PAID")
    raw = {f"raw/{f.name}": f.read_bytes() for f in sorted((CORPUS_DIR / "raw").iterdir())}
    build_bundle(
        tmp_path / "b",
        inputs,
        evaluate(inputs),
        mode="as_known",
        raw_sources=raw,
        mappings=corpus.mappings,
    )
    report = verify_bundle(tmp_path / "b", level="R2")
    assert report.status == "INCOMPLETE"
    assert any("no local normalizer" in r for r in report.reasons)


def test_reconstruction_needs_coverage_of_the_journal_itself(corpus: Corpus) -> None:
    """Covering positions but not journal changes cannot rebuild an older position."""
    no_journal = cert(
        corpus.coverage["cov-ta-s2"],
        coverage_id="cov-ta-no-journal",
        fact_types=[
            "redemption_accepted",
            "redemption_cancelled",
            "redemption_reactivated",
            "position_held",
            "units_registered",
        ],
    )
    evaluation = evaluate(swap_cert(corpus, "RD-POSITION-RECONSTRUCTED", "cov-ta-s2", no_journal))
    assert status(evaluation, "units_within_position") == ("UNKNOWN", "INSUFFICIENT_COVERAGE")
    assert "position_changed" in reason(evaluation, "units_within_position")


def test_an_early_burn_contradicts_a_cancellation(corpus: Corpus) -> None:
    """D-7: it does not fulfil the retirement, yet it happened and contradicts a cancellation."""
    cancelled = evaluate(corpus.inputs_for("RD-CANCELLED-EARLY-BURN"))
    assert status(cancelled, "no_settlement_after_cancellation") == (
        "FAIL",
        "SETTLED_DESPITE_CANCELLATION",
    )
    assert status(cancelled, "burn_vs_request") == (
        "NOT_APPLICABLE",
        "EXTINGUISHED_BY_CANCELLATION",
    )
    assert (cancelled.result.result, cancelled.result.operation_state) == ("BREAK", "cancelled")
    not_cancelled = evaluate(corpus.inputs_for("RD-BURN-BEFORE-ACCEPTANCE"))
    assert status(not_cancelled, "burn_vs_request") == (
        "UNKNOWN",
        "BURN_BEFORE_ACCEPTANCE_UNSUPPORTED",
    )
    uncertain = changed(corpus.journals["cancelled-early-burn"]["obs-BN-EARLY"], operation_ref=None)
    unlinked = evaluate(
        variant(corpus, "RD-CANCELLED-EARLY-BURN", observations={"obs-BN-EARLY": uncertain})
    )
    assert status(unlinked, "no_settlement_after_cancellation") == ("UNKNOWN", "AMBIGUOUS_MATCH")


def test_historical_engines_available_absent_and_blocked(corpus: Corpus, tmp_path: Path) -> None:
    """D-8: the exact engine is resolved locally; never substituted by the
    current one. A retired engine reproduces its own conclusions and says so."""
    available = verify_bundle(Path(__file__).resolve().parents[1] / "fixtures/bundles/K3")
    assert available.status == "REPRODUCED"
    assert (available.local_engine_ref, available.engine_status) == (
        "invaria-engine@0.1.0",
        "retired",
    )
    # A retired engine does not evaluate a profile declaring a quarantine policy (1.5.0):
    # it rejects it instead of ignoring the declared rule.
    declared = replay(corpus.inputs_for("RD-PAID"), "invaria-redemption-engine@0.4.0")
    assert {c.reason_code for c in declared.result.controls} == {"EVALUATION_ERROR"}
    assert "declares a quarantine policy" in declared.result.controls[0].reason
    inputs = load_corpus(PREVIOUS_DIR).inputs_for("RD-PAID")  # profile 1.4.0
    historical = replay(inputs, "invaria-redemption-engine@0.4.0")
    assert historical.result.result == evaluate(inputs).result.result == "MATCH"
    build_bundle(tmp_path / "retired", inputs, historical, mode="as_known")
    retired = verify_bundle(tmp_path / "retired")
    assert (retired.status, retired.engine_status) == ("REPRODUCED", "retired")
    assert retired.local_engine_ref == "invaria-redemption-engine@0.4.0"
    # Not selectable for a new evaluation: every control is an evaluation error.
    refused = evaluate(inputs, engine_ref="invaria-redemption-engine@0.4.0")
    assert {c.reason_code for c in refused.result.controls} == {"EVALUATION_ERROR"}
    assert "retired for new evaluations" in refused.result.controls[0].reason
    cases = {
        "invaria-redemption-engine@0.1.0": "blocked by policy",
        "invaria-redemption-engine@0.2.0": "blocked by policy",
        "invaria-redemption-engine@0.3.0": "blocked by policy",
        "invaria-redemption-engine@9.9.9": "not available locally",
    }
    for n, (ref, reason_text) in enumerate(cases.items()):
        build_bundle(tmp_path / f"b{n}", inputs, evaluate(inputs, engine_ref=ref), mode="as_known")
        report = verify_bundle(tmp_path / f"b{n}")
        assert report.status == "INCOMPLETE", ref
        assert any(reason_text in r and "substituted" in r for r in report.reasons), report.reasons


# ------------------------------------------------- rev. 3 (decisions of 2026-10-06, part 2)


def test_positive_evidence_needs_no_continuous_coverage(corpus: Corpus) -> None:
    """I-B: a covered absence is needed only where absence is argued."""
    ids = [i for i in corpus.scenarios["RD-PAID"].snapshot.coverage_ids if i != "cov-bank-s2"]
    evaluation = evaluate(variant(corpus, "RD-PAID", coverage_ids=ids))
    assert status(evaluation, "cash_vs_expected") == ("PASS", "EXACT_MATCH")
    assert status(evaluation, "payment_deadline") == ("PASS", "EXACT_MATCH")
    assert evaluation.result.result == "MATCH"
    missed = evaluate(
        variant(
            corpus,
            "RD-MISSED",
            coverage_ids=[
                i for i in corpus.scenarios["RD-MISSED"].snapshot.coverage_ids if i != "cov-bank-s3"
            ],
        )
    )
    assert status(missed, "payment_deadline") == ("UNKNOWN", "INSUFFICIENT_COVERAGE")


def test_foreign_cancellation_is_out_of_scope(corpus: Corpus) -> None:
    cancel = corpus.journals["cancelled"]["obs-CN1"]
    foreign = changed(
        cancel,
        observation_id="obs-CN-FOREIGN",
        operation_ref="RED-0002",
        source__record_key="CAN-RED-0002",
    )
    evaluation = evaluate(with_member(corpus, "RD-PAID", foreign))
    assert evaluation.result.result == "MATCH" and evaluation.result.operation_state is None
    assert "obs-CN-FOREIGN" not in evaluation.effective_observation_ids


def test_contradictory_cancellation_association_keeps_the_conflict(corpus: Corpus) -> None:
    cancel = corpus.journals["cancelled"]["obs-CN1"]
    other = changed(cancel, payload__account_ref="acct-pseudo-0002")
    evaluation = evaluate(variant(corpus, "RD-CANCELLED", observations={"obs-CN1": other}))
    assert status(evaluation, "cancellation_valid") == ("UNKNOWN", "AMBIGUOUS_MATCH")
    assert "obs-CN1" in [
        r
        for c in evaluation.result.controls
        if c.control_id.endswith("cancellation_valid")
        for r in c.evidence_refs
    ]
    assert evaluation.result.operation_state is None


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {
                "observation_id": "obs-PR-TWIN",
                "recorded_at": "2026-10-04T23:00:00Z",
                "payload__price_per_unit": {"atoms": "10100", "scale": 2, "unit": "USD"},
            },
            ("UNKNOWN", "SOURCE_CONFLICT"),
        ),
        (
            {"observation_id": "obs-PR-SECOND", "source__record_key": "price:DEMO-A:second"},
            ("UNKNOWN", "UNSUPPORTED_CAPABILITY"),
        ),
    ],
    ids=["same-record-different-content", "two-records-same-price-ref"],
)
def test_conflicting_price_reference_is_unknown(
    corpus: Corpus, fields: dict[str, Any], expected: tuple[str, str]
) -> None:
    twin = changed(corpus.journals["main"]["obs-PR1"], **fields)
    evaluation = evaluate(with_member(corpus, "RD-PAID", twin))
    assert status(evaluation, "price_vs_approved") == expected


def test_request_retraction_keeps_conflicts_and_supported_evidence(corpus: Corpus) -> None:
    paid = evaluate(corpus.inputs_for("RD-RETRACTED-PAID"))
    assert "obs-PY1" in paid.effective_observation_ids
    assert "still supported: obs-PY1" in reason(paid, "cash_vs_expected")
    assert status(paid, "no_settlement_after_cancellation")[0] == "NOT_APPLICABLE"
    conflicting = changed(
        corpus.journals["main"]["obs-PY1"],
        observation_id="obs-PY1-OTHER",
        recorded_at="2026-10-06T15:40:00Z",
        payload__amount={"atoms": "999999", "scale": 2, "unit": "USD"},
    )
    evaluation = evaluate(with_member(corpus, "RD-RETRACTED-PAID", conflicting))
    assert status(evaluation, "cash_vs_expected") == ("UNKNOWN", "SOURCE_CONFLICT")
    assert status(evaluation, "ta_units_vs_request") == ("UNKNOWN", "LOSS_OF_SUPPORT")


def test_economic_cut_bounds_what_counts(corpus: Corpus) -> None:
    cut = evaluate(corpus.inputs_for("RD-PAID-AFTER-ECONOMIC-CUT"))
    assert "obs-PY1" not in cut.effective_observation_ids
    assert status(cut, "payment_deadline") == ("UNKNOWN", "PAYMENT_NOT_DUE")
    before_due = evaluate(corpus.inputs_for("RD-MISSED-CUT-BEFORE-DUE"))
    assert status(before_due, "payment_deadline") == ("UNKNOWN", "PAYMENT_NOT_DUE")
    assert "economic cut 2026-10-07T09:00:00Z" in reason(before_due, "payment_deadline")


def test_times_finer_than_the_declared_precision_are_not_judged(corpus: Corpus) -> None:
    paid = corpus.journals["at-due"]["obs-PY-DUE"]
    fine = changed(paid, valid_time="2026-10-07T09:59:59.500000Z")
    evaluation = evaluate(variant(corpus, "RD-PAID-AT-DUE", observations={"obs-PY-DUE": fine}))
    assert status(evaluation, "payment_deadline") == ("UNKNOWN", "UNSUPPORTED_CAPABILITY")


@pytest.mark.parametrize("target", ["position", "change"])
def test_sequences_of_another_journal_are_not_compared(corpus: Corpus, target: str) -> None:
    if target == "position":
        entry_id, scenario = "obs-PS-OLD", "RD-POSITION-RECONSTRUCTED"
    else:
        entry_id, scenario = "obs-CH-0960", "RD-POSITION-RECONSTRUCTED"
    entry = changed(
        corpus.journals["position-reconstructed"][entry_id], payload__journal_id="ta-journal-other"
    )
    evaluation = evaluate(variant(corpus, scenario, observations={entry_id: entry}))
    assert status(evaluation, "units_within_position") == ("UNKNOWN", "AMBIGUOUS_SEQUENCE")
    assert "journal" in reason(evaluation, "units_within_position")


# ------------------------------------------------- rev. 4 (decisions D-2 to D-11)


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ([{"field": "account_ref", "values": ["acct-pseudo-0001"]}], ("FAIL", "PAYMENT_MISSED")),
        ([{"field": "fact_type", "values": ["cash_settled"]}], ("FAIL", "PAYMENT_MISSED")),
        (
            [{"field": "account_ref", "values": ["acct-other"]}],
            ("UNKNOWN", "INSUFFICIENT_COVERAGE"),
        ),
        (
            [{"field": "fact_type", "values": ["units_registered"]}],
            ("UNKNOWN", "INSUFFICIENT_COVERAGE"),
        ),
        ([{"field": "revision", "values": ["latest"]}], ("UNKNOWN", "INSUFFICIENT_COVERAGE")),
        ([{"field": "other", "values": ["vendor-specific"]}], ("UNKNOWN", "INSUFFICIENT_COVERAGE")),
    ],
    ids=["own-account", "own-fact-type", "other-account", "other-fact-type", "revision", "other"],
)
def test_filters_count_only_when_they_keep_the_predicate(
    corpus: Corpus, filters: list[dict[str, Any]], expected: tuple[str, str]
) -> None:
    scope = {"accounts": ["acct-pseudo-0001"], "currencies": ["USD"], "filters": filters}
    bank = cert(corpus.coverage["cov-bank-s3"], coverage_id="cov-bank-f", scope=scope)
    evaluation = evaluate(swap_cert(corpus, "RD-MISSED", "cov-bank-s3", bank))
    assert status(evaluation, "payment_deadline") == expected


def test_settlement_ref_candidates_need_a_compatible_namespace(corpus: Corpus) -> None:
    profile = corpus.profile
    assert isinstance(profile, RedemptionProfile)
    incompatible = profile.model_copy(
        update={
            "correlation": profile.correlation.model_copy(
                update={"settlement_ref_namespace": "incompatible"}
            )
        }
    )
    inputs = dataclasses.replace(corpus.inputs_for("RD-UNLINKED-CANDIDATE"), profile=incompatible)
    assert status(evaluate(inputs), "payment_deadline") == ("FAIL", "PAYMENT_MISSED")


def test_settlement_ref_collisions_stay_visible(corpus: Corpus) -> None:
    evaluation = evaluate(corpus.inputs_for("RD-SETTLEMENT-REF-COLLISION"))
    assert "2 payment(s)" in reason(evaluation, "payment_deadline")
    (c,) = [c for c in evaluation.result.controls if c.control_id.endswith("payment_deadline")]
    assert {"obs-PY-UNLINKED", "obs-PY-OTHER-OP"} <= set(c.evidence_refs)


def test_a_late_known_correction_describes_an_earlier_economic_fact(corpus: Corpus) -> None:
    before = evaluate(corpus.inputs_for("RD-SHORT-PAY-BEFORE-CORRECTION"))
    after = evaluate(corpus.inputs_for("RD-CORRECTION-KNOWN-LATE"))
    snapshot = corpus.scenarios["RD-CORRECTION-KNOWN-LATE"].snapshot
    fix = corpus.journals["short-pay-corrected"]["obs-PY-FIX"]
    assert fix.recorded_at > snapshot.valid_at >= fix.valid_time
    assert before.result.result == "BREAK" and after.result.result == "MATCH"
    assert "obs-PY-FIX" in after.effective_observation_ids


def test_scoped_quarantine_is_not_read_by_the_redemption_engine(historical: Corpus) -> None:
    """The 1.3.0 profile declares no quarantine scope: under 0.5.0 every quarantined record
    still counts, whatever addresses it declares."""
    scenario = "RD-PAID"
    base = historical.inputs_for(scenario)
    assert evaluate(base).result.result == "MATCH"
    stranger = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
    scoped = {
        cid: CoverageCertificate.model_validate_json(
            json.dumps(
                {
                    **c.model_dump(mode="json"),
                    "records_received": c.records_received + 1,
                    "records_quarantined": 1,
                    "quarantined_records": [
                        {"locator": "foreign#1", "reasons": ["X"], "addresses": [stranger]}
                    ],
                }
            )
        )
        for cid, c in base.coverage.items()
        if cid in base.snapshot.coverage_ids
    }
    assert evaluate(variant(historical, scenario, coverage=scoped)).result.result == "UNKNOWN"


# ------------------------------------- engine 0.5.0: chain effects and scoped quarantine

INVESTOR = "GA2JRO6WKTHMJTH7TDAXHNDHWT5SF72VKABPO2G6RL5BCGKTNIPIBEKW"
STRANGER = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
OTHER = "GCGGXYAKBIOKOIMVSUTXPEHRLJJU7VB7ZIYYKUFXYIVZTTFIWKEKTBEP"


def chain_quarantine(
    inputs: EvaluationInputs,
    addresses: Sequence[list[str] | None],
    source: str = "stellar-testnet-frozen",
) -> EvaluationInputs:
    """The latest certificate of ``source`` carrying one quarantined record per entry."""
    cid = max(
        (c.recorded_at, i)
        for i, c in inputs.coverage.items()
        if c.source_id == source and i in inputs.snapshot.coverage_ids
    )[1]
    cert = inputs.coverage[cid]
    altered = CoverageCertificate.model_validate_json(
        json.dumps(
            {
                **cert.model_dump(mode="json"),
                "records_received": cert.records_received + len(addresses),
                "records_quarantined": len(addresses),
                "quarantined_records": [
                    {"locator": f"chain#{n}", "reasons": ["EFFECT_NOT_A_MOVEMENT"], "addresses": a}
                    for n, a in enumerate(addresses)
                ],
            }
        )
    )
    return dataclasses.replace(inputs, coverage={**inputs.coverage, cid: altered})


def chain_effect(
    inputs: EvaluationInputs,
    *,
    account: str,
    counterparty: str,
    operation_ref: str | None = None,
    instrument_id: str | None = None,
    template_from: EvaluationInputs | None = None,
) -> EvaluationInputs:
    """A DEX fill observed by the token source, added to the snapshot."""
    template = next(
        o
        for o in (template_from or inputs).observations.values()
        if o.fact_type == "token_movement"
    )
    assert isinstance(template.payload, TokenMovementPayload)
    effect = Observation.model_validate_json(
        json.dumps(
            {
                **template.model_dump(mode="json"),
                "observation_id": f"obs-fill-{account[:6]}-{operation_ref}-{instrument_id}",
                "fact_type": "chain_effect",
                "operation_ref": operation_ref,
                "instrument_id": instrument_id or template.instrument_id,
                "source": {**template.source.model_dump(), "record_key": f"fill:{account}"},
                "valid_time": inputs.snapshot.valid_at.isoformat(),
                "recorded_at": inputs.snapshot.known_at.isoformat(),
                "payload": {
                    "payload_type": "chain_effect",
                    "effect_kind": "dex_fill",
                    "representation": "sac",
                    "account": account,
                    "direction": "credit",
                    "counterparty": {"kind": "account", "id": counterparty},
                    "units": template.payload.units.model_dump(),
                    "chain": template.payload.chain.model_dump(mode="json"),
                    "path_payment": None,
                    "transaction": None,
                    "exchange": {
                        "operation_type": "manage_sell_offer",
                        "operation_source": counterparty,
                        "offer_id": None,
                        "sold_asset": None,
                        "sold_amount": None,
                        "bought_asset": None,
                        "bought_amount": None,
                    },
                },
            }
        )
    )
    snapshot = inputs.snapshot.model_copy(
        update={
            "observation_ids": sorted([*inputs.snapshot.observation_ids, effect.observation_id])
        }
    )
    return dataclasses.replace(
        inputs,
        snapshot=snapshot,
        observations={**inputs.observations, effect.observation_id: effect},
    )


def test_a_foreign_quarantined_record_does_not_degrade_an_absence(corpus: Corpus) -> None:
    """RD-CANCELLED argues that no burn followed the cancellation, under chain coverage."""
    base = corpus.inputs_for("RD-CANCELLED")
    assert evaluate(base).result.result == "MATCH"
    assert evaluate(chain_quarantine(base, [[STRANGER, OTHER]])).result.result == "MATCH"


@pytest.mark.parametrize(
    "addresses",
    [[[INVESTOR, OTHER]], [[STRANGER], None]],  # relevant; one with unknown parties
)
def test_a_relevant_or_unknown_quarantined_record_blocks_the_absence(
    corpus: Corpus, addresses: list[list[str] | None]
) -> None:
    result = evaluate(chain_quarantine((corpus.inputs_for("RD-CANCELLED")), addresses)).result
    assert result.result == "UNKNOWN"
    assert status_of(result, "no_settlement_after_cancellation") == (
        "UNKNOWN",
        "QUARANTINED_INPUT",
    )


def test_unscoped_profile_and_retired_engine_keep_every_quarantine_blocking(
    corpus: Corpus, historical: Corpus
) -> None:
    foreign = [[STRANGER, OTHER]]
    unscoped = chain_quarantine(historical.inputs_for("RD-CANCELLED"), foreign)  # profile 1.3.0
    assert evaluate(unscoped).result.result == "UNKNOWN"
    old = replay(
        chain_quarantine(load_corpus(PREVIOUS_DIR).inputs_for("RD-CANCELLED"), foreign),
        "invaria-redemption-engine@0.4.0",
    )
    assert old.result.result == "UNKNOWN"
    assert old.result.versions.engine_ref == "invaria-redemption-engine@0.4.0"


def test_a_quarantine_of_another_asset_does_not_reach_the_request(corpus: Corpus) -> None:
    base = corpus.inputs_for("RD-CANCELLED")
    cid = next(
        i
        for i in base.snapshot.coverage_ids
        if base.coverage[i].source_id == "stellar-testnet-frozen"
    )
    foreign_asset = CoverageCertificate.model_validate_json(
        json.dumps(
            {
                **base.coverage[cid].model_dump(mode="json"),
                "coverage_id": "cov-chain-other-asset",
                "instrument_id": "syn:fund:OTHER:class-z",
                "recorded_at": base.snapshot.known_at.isoformat(),  # later than the chain one
                "records_received": 1,
                "records_quarantined": 1,
                "quarantined_records": [{"locator": "x#1", "reasons": ["X"], "addresses": None}],
            }
        )
    )
    widened = dataclasses.replace(
        base,
        snapshot=base.snapshot.model_copy(
            update={"coverage_ids": sorted([*base.snapshot.coverage_ids, "cov-chain-other-asset"])}
        ),
        coverage={**base.coverage, "cov-chain-other-asset": foreign_asset},
    )
    assert evaluate(widened).result.result == "MATCH"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"account": STRANGER, "counterparty": OTHER}, "MATCH"),  # demonstrably foreign
        ({"account": INVESTOR, "counterparty": OTHER}, "UNKNOWN"),  # on the investor
        # An ExecutionLink outranks addresses: linked, it is considered though foreign.
        ({"account": STRANGER, "counterparty": OTHER, "operation_ref": "RED-0001"}, "UNKNOWN"),
        # Another asset (another target): never this request's evidence.
        (
            {"account": INVESTOR, "counterparty": OTHER, "instrument_id": "syn:fund:OTHER:z"},
            "MATCH",
        ),
    ],
)
def test_chain_effects_bearing_on_the_request(
    corpus: Corpus, kwargs: dict[str, Any], expected: str
) -> None:
    result = evaluate(chain_effect(corpus.inputs_for("RD-PAID"), **kwargs)).result
    assert result.result == expected
    if expected == "UNKNOWN":
        assert status_of(result, "burn_vs_request") == ("UNKNOWN", "UNSUPPORTED_CAPABILITY")
    # The retired engine ignored chain effects: its reproduction (profile 1.4.0) stays MATCH.
    old = replay(
        chain_effect(load_corpus(PREVIOUS_DIR).inputs_for("RD-PAID"), **kwargs),
        "invaria-redemption-engine@0.4.0",
    )
    assert old.result.result == "MATCH"


def test_a_chain_effect_never_stands_in_for_the_burn(corpus: Corpus) -> None:
    base = corpus.inputs_for("RD-PAID")
    without_burn = dataclasses.replace(
        base,
        snapshot=base.snapshot.model_copy(
            update={"observation_ids": [i for i in base.snapshot.observation_ids if i != "obs-BN1"]}
        ),
    )
    linked = chain_effect(
        without_burn, account=INVESTOR, counterparty=OTHER, operation_ref="RED-0001"
    )
    assert evaluate(linked).result.result == "UNKNOWN"
    assert status_of(evaluate(linked).result, "burn_vs_request")[0] == "UNKNOWN"


def test_a_demonstrated_break_survives_an_unresolved_chain_effect(corpus: Corpus) -> None:
    """RD-SHORT-PAY: the short payment stays a BREAK while the burn becomes UNKNOWN."""
    base = corpus.inputs_for("RD-SHORT-PAY")
    assert evaluate(base).result.result == "BREAK"
    result = evaluate(chain_effect(base, account=INVESTOR, counterparty=OTHER)).result
    assert result.result == "BREAK"
    assert status_of(result, "cash_vs_expected")[0] == "FAIL"
    assert status_of(result, "burn_vs_request") == ("UNKNOWN", "UNSUPPORTED_CAPABILITY")


def status_of(result: EvaluationResult, control: str) -> tuple[str, str]:
    (c,) = [c for c in result.controls if c.control_id == f"redemption.{control}"]
    return c.status, c.reason_code


def test_proven_settlement_after_cancellation_survives_a_chain_effect(corpus: Corpus) -> None:
    """A linked burn contradicting a cancellation stays a BREAK: no effect undoes a burn."""
    base = corpus.inputs_for("RD-CANCELLED-EARLY-BURN")
    assert evaluate(base).result.result == "BREAK"
    result = evaluate(chain_effect(base, account=INVESTOR, counterparty=OTHER)).result
    assert result.result == "BREAK"
    assert status_of(result, "no_settlement_after_cancellation") == (
        "FAIL",
        "SETTLED_DESPITE_CANCELLATION",
    )


def _burned(inputs: EvaluationInputs, atoms: str) -> EvaluationInputs:
    burn = inputs.observations["obs-BN1"]
    assert isinstance(burn.payload, TokenMovementPayload)
    payload = burn.payload.model_copy(
        update={"units": burn.payload.units.model_copy(update={"atoms": atoms})}
    )
    return dataclasses.replace(
        inputs,
        observations={
            **inputs.observations,
            "obs-BN1": burn.model_copy(update={"payload": payload}),
        },
    )


def test_an_excess_of_burns_stays_a_fail_but_a_shortfall_waits_for_the_effect(
    corpus: Corpus,
) -> None:
    base = corpus.inputs_for("RD-PAID")
    burn = base.observations["obs-BN1"]
    assert isinstance(burn.payload, TokenMovementPayload)
    requested = int(burn.payload.units.atoms)
    over = evaluate(
        chain_effect(_burned(base, str(requested + 1)), account=INVESTOR, counterparty=OTHER)
    ).result
    assert status_of(over, "burn_vs_request")[0] == "FAIL" and over.result == "BREAK"
    short = evaluate(
        chain_effect(_burned(base, str(requested - 1)), account=INVESTOR, counterparty=OTHER)
    ).result
    assert status_of(short, "burn_vs_request") == ("UNKNOWN", "UNSUPPORTED_CAPABILITY")
    assert (
        status_of(evaluate(_burned(base, str(requested - 1))).result, "burn_vs_request")[0]
        == "FAIL"
    )


def test_an_absence_of_settlement_waits_for_an_unresolved_effect(corpus: Corpus) -> None:
    base = corpus.inputs_for("RD-CANCELLED")
    template = corpus.inputs_for("RD-PAID")
    result = evaluate(
        chain_effect(base, account=INVESTOR, counterparty=OTHER, template_from=template)
    ).result
    assert result.result == "UNKNOWN"
    assert status_of(result, "no_settlement_after_cancellation") == (
        "UNKNOWN",
        "UNSUPPORTED_CAPABILITY",
    )


def test_an_effect_after_the_economic_cut_does_not_count(corpus: Corpus) -> None:
    base = corpus.inputs_for("RD-PAID")
    with_effect = chain_effect(base, account=INVESTOR, counterparty=OTHER)
    (effect_id,) = [
        i for i in with_effect.snapshot.observation_ids if i not in base.snapshot.observation_ids
    ]
    later = with_effect.observations[effect_id].model_copy(
        update={"valid_time": base.snapshot.valid_at + timedelta(seconds=1)}
    )
    moved = dataclasses.replace(
        with_effect, observations={**with_effect.observations, effect_id: later}
    )
    assert evaluate(moved).result.result == "MATCH"


def test_the_submitter_of_the_operation_is_a_party_of_the_effect(corpus: Corpus) -> None:
    """A fill on a stranger's offer, submitted by the investor, bears on the request."""
    inputs = chain_effect(corpus.inputs_for("RD-PAID"), account=STRANGER, counterparty=OTHER)
    (effect_id,) = [o for o in inputs.observations if o.startswith("obs-fill-")]
    effect = inputs.observations[effect_id]
    assert isinstance(effect.payload, ChainEffectPayload) and effect.payload.exchange is not None
    submitted = effect.model_copy(
        update={
            "payload": effect.payload.model_copy(
                update={
                    "exchange": effect.payload.exchange.model_copy(
                        update={"operation_source": INVESTOR}
                    )
                }
            )
        }
    )
    changed = dataclasses.replace(
        inputs, observations={**inputs.observations, effect_id: submitted}
    )
    assert evaluate(inputs).result.result == "MATCH"
    assert evaluate(changed).result.result == "UNKNOWN"


def test_why_the_migrated_expectations_do_not_change(corpus: Corpus) -> None:
    """The 0.5.0 rules read only chain effects and certificates listing quarantined records;
    this corpus has neither, so the hand-written expectations hold unchanged."""
    for scenario_id in sorted(corpus.scenarios):
        inputs = corpus.inputs_for(scenario_id)
        members = [inputs.observations[i] for i in inputs.snapshot.observation_ids]
        assert not any(o.fact_type == "chain_effect" for o in members), scenario_id
        certs = [inputs.coverage[i] for i in inputs.snapshot.coverage_ids]
        assert all(c.quarantined_records is None for c in certs), scenario_id


# ------------------------------------------------ clawback


def clawback(
    inputs: EvaluationInputs, holder: str, template_from: EvaluationInputs
) -> EvaluationInputs:
    """A SAC clawback of ``holder``'s units by the representation's issuer."""
    (rep,) = inputs.profile.representations
    template = next(
        o for o in template_from.observations.values() if o.fact_type == "token_movement"
    )
    assert isinstance(template.payload, TokenMovementPayload)
    effect = Observation.model_validate_json(
        json.dumps(
            {
                **template.model_dump(mode="json"),
                "observation_id": f"obs-clawback-{holder[:6]}",
                "fact_type": "chain_effect",
                "operation_ref": None,
                "source": {**template.source.model_dump(), "record_key": f"clawback:{holder}"},
                "valid_time": inputs.snapshot.valid_at.isoformat(),
                "recorded_at": inputs.snapshot.known_at.isoformat(),
                "payload": {
                    "payload_type": "chain_effect",
                    "effect_kind": "clawback",
                    "representation": "sac",
                    "account": holder,
                    "direction": "debit",
                    "counterparty": {"kind": "account", "id": rep.issuer},
                    "units": template.payload.units.model_dump(),
                    "chain": template.payload.chain.model_dump(mode="json"),
                    "path_payment": None,
                    "transaction": None,
                    "clawback": {
                        "asset": f"{rep.asset_code}:{rep.issuer}",
                        "issuer": rep.issuer,
                        "operation_type": None,
                        "operation_source": None,
                    },
                },
            }
        )
    )
    snapshot = inputs.snapshot.model_copy(
        update={
            "observation_ids": sorted([*inputs.snapshot.observation_ids, effect.observation_id])
        }
    )
    return dataclasses.replace(
        inputs,
        snapshot=snapshot,
        observations={**inputs.observations, effect.observation_id: effect},
    )


def test_a_clawback_is_never_the_redemption_burn(corpus: Corpus) -> None:
    """Same account and amount as the burn, issuer as counterparty: still not a retirement."""
    base = corpus.inputs_for("RD-PAID")
    without_burn = dataclasses.replace(
        base,
        snapshot=base.snapshot.model_copy(
            update={"observation_ids": [i for i in base.snapshot.observation_ids if i != "obs-BN1"]}
        ),
    )
    result = evaluate(clawback(without_burn, INVESTOR, base)).result
    assert result.result == "UNKNOWN"
    assert status_of(result, "burn_vs_request")[0] == "UNKNOWN"


@pytest.mark.parametrize(
    ("scenario", "holder", "expected", "burn"),
    [
        ("RD-PAID", STRANGER, "MATCH", ("PASS", "EXACT_MATCH")),  # someone else's: nothing
        ("RD-PAID", INVESTOR, "UNKNOWN", ("UNKNOWN", "UNSUPPORTED_CAPABILITY")),
        # A clawback by itself proves no breach, and a demonstrated BREAK stays.
        ("RD-SHORT-PAY", INVESTOR, "BREAK", ("UNKNOWN", "UNSUPPORTED_CAPABILITY")),
    ],
)
def test_clawbacks_bear_only_on_the_controls_they_could_change(
    corpus: Corpus, scenario: str, holder: str, expected: str, burn: tuple[str, str]
) -> None:
    base = corpus.inputs_for(scenario)
    result = evaluate(clawback(base, holder, base)).result
    assert result.result == expected
    assert status_of(result, "burn_vs_request") == burn
    plain = evaluate(base).result
    for control in (
        "price_vs_approved",
        "units_within_position",
        "cash_vs_expected",
        "payment_deadline",
    ):
        assert status_of(result, control) == status_of(plain, control), control


def test_a_clawback_after_cancellation_is_not_settlement(corpus: Corpus) -> None:
    """RD-CANCELLED: a clawback is not a payment or burn despite the cancellation; it only
    stops the absence from being shown (UNKNOWN), never a BREAK by itself."""
    base = corpus.inputs_for("RD-CANCELLED")
    result = evaluate(clawback(base, INVESTOR, corpus.inputs_for("RD-PAID"))).result
    assert status_of(result, "no_settlement_after_cancellation") == (
        "UNKNOWN",
        "UNSUPPORTED_CAPABILITY",
    )
    assert result.result == "UNKNOWN"
