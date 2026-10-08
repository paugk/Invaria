"""Behaviour of the pure evaluator: corpus scenarios, absence, contradiction, UNKNOWN."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.evaluation import SnapshotRef
from invaria.contracts.observation import Observation, TokenMovementPayload
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.common import chain_effects_bearing, quarantine_affecting
from invaria.engine.evaluate import ENGINE_REF, Evaluation, EvaluationInputs, evaluate, replay
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
        data["provenance"]["mapping_ref"] = "ta-csv-synthetic@1.0.0"

    evaluation = evaluate(variant(corpus, replace={"obs-B1": from_ta}))
    assert statuses(evaluation)["cash_vs_order"] == ("UNKNOWN", "MISSING_EVIDENCE")


def test_a_mapping_the_source_does_not_admit_is_refused_not_relabelled(corpus: Corpus) -> None:
    """The bank record claimed by the TA source keeps the bank mapping that
    produced it, which the profile does not admit for the TA source: the new evaluation is
    refused with the source, the mapping and what the profile admits."""

    def from_ta(data: dict[str, Any]) -> None:
        data["source"]["source_id"] = "ta-synthetic"

    inputs = variant(corpus, replace={"obs-B1": from_ta})
    evaluation = evaluate(inputs)
    assert {c.reason_code for c in evaluation.result.controls} == {"EVALUATION_ERROR"}
    reason = evaluation.result.controls[0].reason
    assert "obs-B1" in reason and "bank-csv-synthetic@1.0.0" in reason
    assert "admits: ta-csv-synthetic@1.0.0" in reason and "nothing is relabelled" in reason
    assert evaluation.result.versions.evidence_mapping_refs is not None
    assert "bank-csv-synthetic@1.0.0" in evaluation.result.versions.evidence_mapping_refs
    # The current engine refuses on replay too (its recorded refusal reproduces); a retired
    # one replays as it did, without the admission.
    assert replay(inputs, ENGINE_REF).result == evaluation.result
    retired = replay(inputs, "invaria-engine@0.7.0").result
    assert retired.controls[0].reason_code != "EVALUATION_ERROR"


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


# ------------------------------------- engine 0.2.0: chain effects

K3_INVESTOR = "GA2JRO6WKTHMJTH7TDAXHNDHWT5SF72VKABPO2G6RL5BCGKTNIPIBEKW"
FOREIGN = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"


def _with_chain_effect(
    inputs: EvaluationInputs, account: str, instrument_id: str | None = None
) -> EvaluationInputs:
    template = next(
        inputs.observations[i]
        for i in inputs.snapshot.observation_ids
        if inputs.observations[i].fact_type == "token_movement"
    )
    document = template.model_dump(mode="json")
    chain = document["payload"]["chain"]
    effect = Observation.model_validate_json(
        json.dumps(
            {
                **document,
                "observation_id": f"obs-chain-effect-{account[:6]}-{instrument_id}",
                "fact_type": "chain_effect",
                "operation_ref": None,
                "instrument_id": instrument_id or template.instrument_id,
                "source": {**document["source"], "record_key": f"effect:{account}"},
                "payload": {
                    "payload_type": "chain_effect",
                    "effect_kind": "dex_fill",
                    "representation": "sac",
                    "account": account,
                    "direction": "debit",
                    "counterparty": {"kind": "account", "id": FOREIGN},
                    "units": document["payload"]["units"],
                    "chain": chain,
                    "path_payment": None,
                    "transaction": None,
                    "exchange": {
                        "operation_type": "manage_sell_offer",
                        "operation_source": FOREIGN,
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
    return EvaluationInputs(
        snapshot=snapshot,
        profile=inputs.profile,
        observations={**inputs.observations, effect.observation_id: effect},
        coverage=inputs.coverage,
        identity_links=inputs.identity_links,
    )


def test_a_demonstrated_break_survives_an_unresolved_chain_effect(corpus: Corpus) -> None:
    """K3: the cash shortfall stays a BREAK; the token control becomes UNKNOWN."""
    result = evaluate(_with_chain_effect(corpus.inputs_for("K3"), K3_INVESTOR)).result
    by_id = {c.control_id: (c.status, c.reason_code) for c in result.controls}
    assert result.result == "BREAK"
    assert by_id["subscription.cash_vs_order"] == ("FAIL", "CASH_AMOUNT_MISMATCH")
    assert by_id["subscription.token_units_vs_order"] == ("UNKNOWN", "UNSUPPORTED_CAPABILITY")


def test_chain_effects_of_others_or_of_another_asset_change_nothing(corpus: Corpus) -> None:
    base = evaluate(corpus.inputs_for("K3")).result
    for account, instrument in ((FOREIGN, None), (K3_INVESTOR, "syn:fund:OTHER:class-z")):
        other = evaluate(_with_chain_effect(corpus.inputs_for("K3"), account, instrument)).result
        assert [c.model_dump() for c in other.controls] == [c.model_dump() for c in base.controls]


def test_retired_engine_is_only_for_replay(corpus: Corpus) -> None:
    inputs = corpus.inputs_for("K3")
    refused = evaluate(inputs, engine_ref="invaria-engine@0.1.0").result
    assert {c.reason_code for c in refused.controls} == {"EVALUATION_ERROR"}
    assert "retired for new evaluations" in refused.controls[0].reason
    historical = replay(inputs, "invaria-engine@0.1.0").result
    assert historical.versions.engine_ref == "invaria-engine@0.1.0"
    assert historical.evaluation_id != evaluate(inputs).result.evaluation_id
    # The retired engine ignored chain effects: reproducing it keeps its old reading.
    effect = _with_chain_effect(inputs, K3_INVESTOR)
    assert replay(effect, "invaria-engine@0.1.0").result.controls == historical.controls
    unknown = replay(inputs, "invaria-engine@9.9.9").result
    assert {c.reason_code for c in unknown.controls} == {"EVALUATION_ERROR"}


def test_without_a_known_account_nothing_is_shown_foreign(corpus: Corpus) -> None:
    """Both engines fail closed: with no account to scope by, every record counts and
    every chain effect bears on the operation (unreachable through the current profiles,
    which need the order or request first)."""
    cert = next(iter(corpus.coverage.values()))
    scoped = CoverageCertificate.model_validate_json(
        json.dumps(
            {
                **cert.model_dump(mode="json"),
                "records_received": cert.records_received + 2,
                "records_quarantined": 2,
                "quarantined_records": [
                    {"locator": "a#1", "reasons": ["X"], "addresses": [FOREIGN]},
                    {"locator": "a#2", "reasons": ["X"], "addresses": None},
                ],
            }
        )
    )
    assert quarantine_affecting(scoped, None) == 2
    assert quarantine_affecting(scoped, {K3_INVESTOR}) == 1
    inputs = _with_chain_effect(corpus.inputs_for("K3"), FOREIGN)
    members = [inputs.observations[i] for i in inputs.snapshot.observation_ids]
    effect = next(o for o in members if o.fact_type == "chain_effect")
    args = (members, effect.source.source_id, effect.instrument_id, "SUB-0001")
    assert chain_effects_bearing(*args, None) == (effect.observation_id,)
    assert chain_effects_bearing(*args, {K3_INVESTOR}) == ()


def test_the_synthetic_corpus_is_compatible_with_engine_0_2_0(corpus: Corpus) -> None:
    """Not migrated: profile 1.0.0 declares no quarantine scope and the corpus has
    no chain effect nor certificate listing quarantined records, so 0.2.0 and the retired
    0.1.0 give the same controls on every scenario."""
    for scenario_id in sorted(corpus.scenarios):
        inputs = corpus.inputs_for(scenario_id)
        members = [inputs.observations[i] for i in inputs.snapshot.observation_ids]
        assert not any(o.fact_type == "chain_effect" for o in members)
        assert all(
            inputs.coverage[i].quarantined_records is None for i in inputs.snapshot.coverage_ids
        )
        assert all(r.quarantine_scope is None for r in inputs.profile.coverage_requirements)
        new, old = evaluate(inputs).result, replay(inputs, "invaria-engine@0.1.0").result
        assert (new.result, new.controls) == (old.result, old.controls), scenario_id


# ------------------------------------------------ clawback


def _with_clawback(inputs: EvaluationInputs, holder: str) -> EvaluationInputs:
    """A SAC clawback of ``holder``'s units by the representation's issuer."""
    (rep,) = inputs.profile.representations
    template = next(
        inputs.observations[i]
        for i in inputs.snapshot.observation_ids
        if inputs.observations[i].fact_type == "token_movement"
    )
    document = template.model_dump(mode="json")
    effect = Observation.model_validate_json(
        json.dumps(
            {
                **document,
                "observation_id": f"obs-clawback-{holder[:6]}",
                "fact_type": "chain_effect",
                "operation_ref": None,
                "source": {**document["source"], "record_key": f"clawback:{holder}"},
                "payload": {
                    "payload_type": "chain_effect",
                    "effect_kind": "clawback",
                    "representation": "sac",
                    "account": holder,
                    "direction": "debit",
                    "counterparty": {"kind": "account", "id": rep.issuer},
                    "units": document["payload"]["units"],
                    "chain": document["payload"]["chain"],
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
    return EvaluationInputs(
        snapshot=snapshot,
        profile=inputs.profile,
        observations={**inputs.observations, effect.observation_id: effect},
        coverage=inputs.coverage,
        identity_links=inputs.identity_links,
    )


def test_a_clawback_of_the_investor_does_not_bear_on_the_delivery(corpus: Corpus) -> None:
    """A clawback can neither complete nor undo a linked delivery. K3 with a
    clawback of the investor keeps every control; the retired 0.2.0 made the token
    comparison UNKNOWN."""
    inputs = _with_clawback(corpus.inputs_for("K3"), K3_INVESTOR)
    base = evaluate(corpus.inputs_for("K3")).result
    result = evaluate(inputs).result
    assert [c.model_dump() for c in result.controls] == [c.model_dump() for c in base.controls]
    assert result.result == "BREAK"
    old = replay(inputs, "invaria-engine@0.2.0").result
    token = next(c for c in old.controls if c.control_id == "subscription.token_units_vs_order")
    assert (token.status, token.reason_code) == ("UNKNOWN", "UNSUPPORTED_CAPABILITY")


def _delivered(inputs: EvaluationInputs, atoms: str) -> EvaluationInputs:
    """K3 with its linked delivery set to ``atoms``."""
    observations = dict(inputs.observations)
    for i in inputs.snapshot.observation_ids:
        o = observations[i]
        if o.fact_type == "token_movement" and isinstance(o.payload, TokenMovementPayload):
            units = o.payload.units.model_copy(update={"atoms": atoms})
            observations[i] = o.model_copy(
                update={"payload": o.payload.model_copy(update={"units": units})}
            )
    return EvaluationInputs(
        snapshot=inputs.snapshot,
        profile=inputs.profile,
        observations=observations,
        coverage=inputs.coverage,
        identity_links=inputs.identity_links,
    )


@pytest.mark.parametrize("delta", [-1, 1], ids=["short", "over"])
def test_a_demonstrated_delivery_breach_survives_a_clawback(corpus: Corpus, delta: int) -> None:
    """A short or excess linked delivery is a credited FAIL; a clawback of the investor does
    not make it UNKNOWN (it could do neither in 0.2.0 for the short case)."""
    inputs = corpus.inputs_for("K3")
    movement = next(
        inputs.observations[i]
        for i in inputs.snapshot.observation_ids
        if inputs.observations[i].fact_type == "token_movement"
    )
    assert isinstance(movement.payload, TokenMovementPayload)
    changed = _delivered(inputs, str(int(movement.payload.units.atoms) + delta))
    with_clawback = _with_clawback(changed, K3_INVESTOR)
    token = next(
        c
        for c in evaluate(with_clawback).result.controls
        if c.control_id == "subscription.token_units_vs_order"
    )
    assert token.status == "FAIL"
    assert evaluate(with_clawback).result.result == "BREAK"


def test_a_clawback_of_someone_else_changes_nothing(corpus: Corpus) -> None:
    base = evaluate(corpus.inputs_for("K3")).result
    other = evaluate(_with_clawback(corpus.inputs_for("K3"), FOREIGN)).result
    assert [c.model_dump() for c in other.controls] == [c.model_dump() for c in base.controls]
    # The retired engine never read clawbacks: its reproduction is unchanged too.
    effect = _with_clawback(corpus.inputs_for("K3"), K3_INVESTOR)
    historical = replay(corpus.inputs_for("K3"), "invaria-engine@0.1.0").result
    assert replay(effect, "invaria-engine@0.1.0").result.controls == historical.controls


def test_an_undeclared_source_is_neither_refused_nor_stated_as_evidence(corpus: Corpus) -> None:
    """An observation of a source the profile does not declare is never read,
    so its mapping is not the evaluation's evidence and does not refuse it."""
    base = corpus.inputs_for("K3")
    template = base.observations["obs-T1"].model_dump(mode="json")
    stray = Observation.model_validate_json(
        json.dumps(
            {
                **template,
                "observation_id": "obs-undeclared-source",
                "operation_ref": None,
                "source": {**template["source"], "source_id": "elsewhere"},
                "provenance": {**template["provenance"], "mapping_ref": "other-mapping@9.0.0"},
            }
        )
    )
    inputs = dataclasses.replace(
        base,
        snapshot=base.snapshot.model_copy(
            update={
                "observation_ids": sorted([*base.snapshot.observation_ids, "obs-undeclared-source"])
            }
        ),
        observations={**base.observations, stray.observation_id: stray},
    )
    result, before = evaluate(inputs).result, evaluate(base).result
    assert (result.result, result.controls) == (before.result, before.controls)
    assert result.versions == before.versions
    assert "other-mapping@9.0.0" not in (result.versions.evidence_mapping_refs or [])


def test_a_linked_delivery_to_an_unapproved_address_keeps_the_profile_promise(
    corpus: Corpus,
) -> None:
    """B2 weighs a linked delivery to an address without an approved link only under a
    profile that declares its quarantine policy. The synthetic profile declares none: the
    delivery leaves the token comparison UNKNOWN as before, for the same stated reason."""

    def unapproved(data: dict[str, Any]) -> None:
        data["payload"]["to_address"] = FOREIGN

    result = evaluate(variant(corpus, "K3", replace={"obs-T1": unapproved})).result
    token = next(c for c in result.controls if c.control_id.endswith("token_units_vs_order"))
    assert (token.status, token.reason_code) == ("UNKNOWN", "AMBIGUOUS_MATCH")
    assert "destination address has no approved identity link" in token.reason
