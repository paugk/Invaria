"""Behaviour of the pure evaluator: corpus scenarios, absence, contradiction, UNKNOWN."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.evaluation import SnapshotRef
from invaria.contracts.observation import Observation
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.evaluate import ENGINE_REF, Evaluation, EvaluationInputs, evaluate
from invaria.engine.explain import explain, explain_change, export_evaluation
from invaria.vertical import run_vertical

CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


def statuses(evaluation: Evaluation) -> dict[str, tuple[str, str]]:
    return {
        c.control_id.split(".")[1]: (c.status, c.reason_code) for c in evaluation.result.controls
    }


# ------------------------------------------------------------ corpus scenarios


@pytest.mark.parametrize(
    "scenario_id",
    [
        "K1",
        "K2",
        "K3",
        "K2-historical",
        "RETRACTION",
        "DUPLICATE-DELIVERY",
        "SOURCE-CONFLICT",
        "AMBIGUOUS-IDENTITY",
        "AMBIGUOUS-SCALE",
    ],
)
def test_scenario_reproduces_expected_outcome(corpus: Corpus, scenario_id: str) -> None:
    expected = corpus.scenarios[scenario_id].expected
    evaluation = evaluate(corpus.inputs_for(scenario_id))
    assert evaluation.result.result == expected.result
    got = [
        (c.control_id, c.mandatory, c.status, c.reason_code, c.delta)
        for c in evaluation.result.controls
    ]
    want = [
        (c.control_id, c.mandatory, c.status, c.reason_code, c.delta) for c in expected.controls
    ]
    assert got == want
    assert list(evaluation.effective_observation_ids) == expected.effective_observation_ids


def test_k2_historical_ignores_later_correction_present_in_store(corpus: Corpus) -> None:
    inputs = corpus.inputs_for("K2-historical")
    assert "obs-B2" in inputs.observations  # the store knows the correction
    evaluation = evaluate(inputs)
    assert evaluation.result.result == "MATCH"
    refs = {r for c in evaluation.result.controls for r in c.evidence_refs}
    assert "obs-B2" not in refs


def test_evaluation_is_deterministic_and_uses_snapshot_clock(corpus: Corpus) -> None:
    inputs = corpus.inputs_for("K3")
    first, second = evaluate(inputs), evaluate(inputs)
    assert first == second
    snapshot = corpus.scenarios["K3"].snapshot
    assert export_evaluation(snapshot, first) == export_evaluation(snapshot, second)
    assert first.result.evaluation_clock == snapshot.evaluation_clock
    assert first.result.versions.engine_ref == ENGINE_REF


def test_evaluation_id_depends_on_snapshot_and_engine(corpus: Corpus) -> None:
    k2 = evaluate(corpus.inputs_for("K2")).result.evaluation_id
    hist = evaluate(corpus.inputs_for("K2-historical")).result.evaluation_id
    other_engine = evaluate(corpus.inputs_for("K2"), engine_ref="invaria-engine@9.9.9")
    assert len({k2, hist, other_engine.result.evaluation_id}) == 3


# ----------------------------------------------------------- variant builder


def variant(
    corpus: Corpus,
    scenario_id: str = "K2",
    *,
    replace: dict[str, Callable[[dict[str, Any]], None]] | None = None,
    drop: tuple[str, ...] = (),
    add: tuple[dict[str, Any], ...] = (),
    coverage_change: dict[str, Callable[[dict[str, Any]], None]] | None = None,
    snapshot_change: Callable[[dict[str, Any]], None] | None = None,
) -> EvaluationInputs:
    base = corpus.inputs_for(scenario_id)
    store: dict[str, Observation] = {}
    for oid in base.snapshot.observation_ids:
        if oid in drop:
            continue
        data = base.observations[oid].model_dump(mode="json")
        if replace and oid in replace:
            replace[oid](data)
        store[oid] = Observation.model_validate_json(json.dumps(data))
    for data in add:
        observation = Observation.model_validate_json(json.dumps(data))
        store[observation.observation_id] = observation
    coverage: dict[str, CoverageCertificate] = {}
    for cid in base.snapshot.coverage_ids:
        data = base.coverage[cid].model_dump(mode="json")
        if coverage_change and cid in coverage_change:
            coverage_change[cid](data)
        coverage[cid] = CoverageCertificate.model_validate_json(json.dumps(data))
    snap = base.snapshot.model_dump(mode="json")
    snap["observation_ids"] = sorted(store)
    if snapshot_change:
        snapshot_change(snap)
    return EvaluationInputs(
        snapshot=SnapshotRef.model_validate_json(json.dumps(snap)),
        profile=base.profile,
        observations=store,
        coverage=coverage,
        identity_links=base.identity_links,
    )


def obs_data(corpus: Corpus, oid: str, **changes: Any) -> dict[str, Any]:
    data = corpus.journals["main"][oid].model_dump(mode="json")
    data.update(changes)
    return data


# ----------------------------------------------------- absence and UNKNOWN


def test_missing_bank_is_unknown_not_zero(corpus: Corpus) -> None:
    evaluation = evaluate(variant(corpus, drop=("obs-B1",)))
    assert evaluation.result.result == "UNKNOWN"
    cash = next(c for c in evaluation.result.controls if c.control_id.endswith("cash_vs_order"))
    assert (cash.status, cash.reason_code, cash.delta) == ("UNKNOWN", "MISSING_EVIDENCE", None)


def test_missing_order_blocks_every_control(corpus: Corpus) -> None:
    evaluation = evaluate(variant(corpus, drop=("obs-O1",)))
    assert {s for s in statuses(evaluation).values()} == {("UNKNOWN", "MISSING_EVIDENCE")}


def test_evidence_without_coverage_is_insufficient(corpus: Corpus) -> None:
    def no_bank_cov(snap: dict[str, Any]) -> None:
        snap["coverage_ids"] = [c for c in snap["coverage_ids"] if c != "cov-bank-k2"]

    evaluation = evaluate(variant(corpus, snapshot_change=no_bank_cov))
    assert statuses(evaluation)["cash_vs_order"] == ("UNKNOWN", "INSUFFICIENT_COVERAGE")


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.update(gaps=[{"start": "2026-10-01T10:00:00Z", "end": "2026-10-01T11:00:00Z"}]),
        lambda c: c.update(level="provider_claimed"),
        lambda c: c["interval"].update(end="2026-10-01T16:00:00Z"),
        lambda c: c["interval"].update(start="2026-10-01T09:30:00Z"),
    ],
    ids=["gap", "level", "ends-before-valid_at", "starts-after-order"],
)
def test_insufficient_coverage_variants(corpus: Corpus, change: Any) -> None:
    evaluation = evaluate(variant(corpus, coverage_change={"cov-bank-k2": change}))
    assert statuses(evaluation)["cash_vs_order"] == ("UNKNOWN", "INSUFFICIENT_COVERAGE")
    assert evaluation.result.result == "UNKNOWN"


def test_failed_transaction_has_no_effect(corpus: Corpus) -> None:
    def failed(data: dict[str, Any]) -> None:
        data["payload"]["chain"]["tx_successful"] = False

    evaluation = evaluate(variant(corpus, replace={"obs-T1": failed}))
    assert statuses(evaluation)["token_units_vs_order"] == ("UNKNOWN", "MISSING_EVIDENCE")


def test_movement_from_non_issuer_is_unsupported(corpus: Corpus) -> None:
    investor = corpus.journals["main"]["obs-T1"].model_dump(mode="json")["payload"]["to_address"]

    def from_investor(data: dict[str, Any]) -> None:
        data["payload"]["from_address"] = investor

    evaluation = evaluate(variant(corpus, replace={"obs-T1": from_investor}))
    assert statuses(evaluation)["token_units_vs_order"] == ("UNKNOWN", "UNSUPPORTED_CAPABILITY")


def test_unlinked_destination_is_not_attributed(corpus: Corpus) -> None:
    issuer = corpus.profile.representations[0].issuer

    def to_issuer(data: dict[str, Any]) -> None:
        data["payload"]["to_address"] = issuer

    evaluation = evaluate(variant(corpus, replace={"obs-T1": to_issuer}))
    assert statuses(evaluation)["token_units_vs_order"] == ("UNKNOWN", "AMBIGUOUS_MATCH")


def test_second_cash_payment_is_unsupported(corpus: Corpus) -> None:
    extra = obs_data(corpus, "obs-B1", observation_id="obs-B9")
    extra["source"] = {**extra["source"], "record_key": "PAY-0009"}
    extra["payload"] = {**extra["payload"], "payment_ref": "PAY-0009"}
    evaluation = evaluate(variant(corpus, add=(extra,)))
    assert statuses(evaluation)["cash_vs_order"] == ("UNKNOWN", "UNSUPPORTED_CAPABILITY")


def test_cash_in_other_currency_is_unsupported(corpus: Corpus) -> None:
    def eur(data: dict[str, Any]) -> None:
        data["payload"]["amount"]["unit"] = "EUR"

    evaluation = evaluate(variant(corpus, replace={"obs-B1": eur}))
    assert statuses(evaluation)["cash_vs_order"] == ("UNKNOWN", "UNSUPPORTED_CAPABILITY")


def test_price_requiring_rounding_is_unsupported(corpus: Corpus) -> None:
    def odd_units(data: dict[str, Any]) -> None:
        data["payload"]["units"]["atoms"] = "10000000001"  # 1000.0000001 shares x USD 100

    evaluation = evaluate(variant(corpus, replace={"obs-O1": odd_units}))
    assert statuses(evaluation)["order_terms"] == ("UNKNOWN", "UNSUPPORTED_CAPABILITY")


def test_non_authoritative_source_is_ignored(corpus: Corpus) -> None:
    def from_ta(data: dict[str, Any]) -> None:
        data["source"]["source_id"] = "ta-synthetic"

    evaluation = evaluate(variant(corpus, replace={"obs-B1": from_ta}))
    assert statuses(evaluation)["cash_vs_order"] == ("UNKNOWN", "MISSING_EVIDENCE")


# ---------------------------------------------------------------- contradiction


def test_ta_units_contradiction_is_break_with_exact_delta(corpus: Corpus) -> None:
    def fewer(data: dict[str, Any]) -> None:
        data["payload"]["units"]["atoms"] = "9990000000"  # 999 shares

    evaluation = evaluate(variant(corpus, replace={"obs-R1": fewer}))
    ta = next(c for c in evaluation.result.controls if c.control_id.endswith("ta_units_vs_order"))
    assert (evaluation.result.result, ta.status, ta.reason_code) == (
        "BREAK",
        "FAIL",
        "UNITS_MISMATCH",
    )
    assert ta.delta is not None and (ta.delta.atoms, ta.delta.scale) == ("-10000000", 7)


def test_order_terms_contradiction(corpus: Corpus) -> None:
    def inconsistent(data: dict[str, Any]) -> None:
        data["payload"]["cash_amount"]["atoms"] = "10000100"  # USD 100,001.00

    evaluation = evaluate(variant(corpus, replace={"obs-O1": inconsistent}))
    assert statuses(evaluation)["order_terms"] == ("FAIL", "ORDER_TERMS_INCONSISTENT")
    assert evaluation.result.result == "BREAK"


def test_break_coexists_with_unknown(corpus: Corpus) -> None:
    def k3_without_ta(snap: dict[str, Any]) -> None:
        snap["coverage_ids"] = [c for c in snap["coverage_ids"] if c != "cov-ta-k2"]

    evaluation = evaluate(variant(corpus, "K3", drop=("obs-R1",), snapshot_change=k3_without_ta))
    assert evaluation.result.result == "BREAK"
    got = statuses(evaluation)
    assert got["cash_vs_order"] == ("FAIL", "CASH_AMOUNT_MISMATCH")
    assert got["ta_units_vs_order"] == ("UNKNOWN", "MISSING_EVIDENCE")


# ---------------------------------------------------------- technical errors


def test_member_missing_from_store_is_evaluation_error(corpus: Corpus) -> None:
    inputs = corpus.inputs_for("K2")
    store = {k: v for k, v in inputs.observations.items() if k != "obs-R1"}
    evaluation = evaluate(
        EvaluationInputs(
            inputs.snapshot, inputs.profile, store, inputs.coverage, inputs.identity_links
        )
    )
    assert evaluation.result.result == "UNKNOWN"
    assert {s for s in statuses(evaluation).values()} == {("UNKNOWN", "EVALUATION_ERROR")}


def test_member_recorded_after_known_at_is_evaluation_error(corpus: Corpus) -> None:
    def later(data: dict[str, Any]) -> None:
        data["recorded_at"] = "2026-10-01T19:00:00Z"

    evaluation = evaluate(variant(corpus, replace={"obs-B1": later}))
    assert {s for s in statuses(evaluation).values()} == {("UNKNOWN", "EVALUATION_ERROR")}


def test_profile_mismatch_is_evaluation_error(corpus: Corpus) -> None:
    def other_profile(snap: dict[str, Any]) -> None:
        snap["profile_ref"] = "fund-subscription-synthetic@2.0.0"

    evaluation = evaluate(variant(corpus, snapshot_change=other_profile))
    assert evaluation.result.result == "UNKNOWN"
    assert "profile" in evaluation.result.controls[0].reason


def test_unknown_control_is_not_skipped(corpus: Corpus) -> None:
    profile = corpus.profile.model_dump(mode="json")
    extra = {**profile["controls"][0], "control_id": "subscription.future_rule"}
    profile["controls"].append(extra)
    base = corpus.inputs_for("K2")
    inputs = EvaluationInputs(
        base.snapshot,
        type(corpus.profile).model_validate_json(json.dumps(profile)),
        base.observations,
        base.coverage,
        base.identity_links,
    )
    evaluation = evaluate(inputs)
    assert statuses(evaluation)["future_rule"] == ("UNKNOWN", "EVALUATION_ERROR")
    assert evaluation.result.result == "UNKNOWN"


def test_unexpected_exception_becomes_unknown(corpus: Corpus, monkeypatch: Any) -> None:
    import invaria.engine.evaluate as module

    def boom(*_: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "_price_times_units", boom)
    evaluation = evaluate(corpus.inputs_for("K2"))
    assert statuses(evaluation)["order_terms"] == ("UNKNOWN", "EVALUATION_ERROR")
    assert evaluation.result.result == "UNKNOWN"


# ------------------------------------------------------------ explanation


def test_explain_change_k2_to_k3(corpus: Corpus) -> None:
    change = explain_change(evaluate(corpus.inputs_for("K2")), evaluate(corpus.inputs_for("K3")))
    assert (change.from_result, change.to_result) == ("MATCH", "BREAK")
    assert [c.control_id for c in change.changed_controls] == ["subscription.cash_vs_order"]
    assert (change.added_evidence, change.removed_evidence) == (("obs-B2",), ("obs-B1",))


def test_explanation_lines_cite_operands_and_evidence(corpus: Corpus) -> None:
    text = "\n".join(explain(evaluate(corpus.inputs_for("K3"))))
    assert "99500.00 vs 100000.00 USD; delta -500.00 USD" in text
    assert "obs-B2" in text and "cov-bank-k3" in text


def test_export_is_canonical_and_unsigned(corpus: Corpus) -> None:
    snapshot = corpus.scenarios["K3"].snapshot
    text = export_evaluation(snapshot, evaluate(corpus.inputs_for("K3")))
    document = json.loads(text)
    assert document["signed"] is False
    assert document["snapshot"]["snapshot_id"] == "snap-K3"
    assert (
        text
        == json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    )
    with pytest.raises(ValueError):
        export_evaluation(corpus.scenarios["K2"].snapshot, evaluate(corpus.inputs_for("K3")))


# --------------------------------------------------------------- vertical


def test_vertical_from_raw_csv(corpus: Corpus) -> None:
    timeline, runs = run_vertical(corpus)
    assert [run.evaluation.result.result for run in runs] == ["UNKNOWN", "MATCH", "BREAK"]
    assert any("REVISION" in line for line in timeline.log)
    assert any("no Stellar ingestion" in line for line in timeline.log)
    k3 = runs[2].evaluation.result
    cash = next(c for c in k3.controls if c.control_id.endswith("cash_vs_order"))
    assert cash.delta is not None and cash.delta.atoms == "-50000"
    institutional = [o for o in timeline.observations if o.fact_type != "token_movement"]
    assert all(o.provenance.raw_locator.startswith("raw/") for o in institutional)
