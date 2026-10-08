"""SUB-0001 evaluated with real (recorded) Stellar testnet evidence, fully offline."""

from __future__ import annotations

import json
import shutil
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from invaria.bundle import build_bundle, verify_bundle
from invaria.cli import main
from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.observation import (
    ChainEffectPayload,
    ChainParty,
    Observation,
    TokenMovementPayload,
)
from invaria.contracts.profile import admitted_mapping_refs, parse_profile
from invaria.engine.evaluate import ENGINE_REF, EvaluationInputs, evaluate, replay
from invaria.stellar.adapter import HORIZON_MAPPING, MAPPING_REFS, RPC_FILL_MAPPING
from invaria.vertical_testnet import OnchainRun, run_testnet_vertical

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
# Current demo: profile 1.4.0, engine 0.8.0 (the mappings admitted for the
# on-chain source are explicit). The historical corpora (profiles 1.3.0, 1.2.0,
# 1.1.0 and 1.0.0, evaluated with the retired 0.1.0) keep their profiles and
# expectations; today's adapter produces evidence that 1.0.0 to 1.2.0 do not admit, so a new
# evaluation of them is refused.
CORPUS = FIXTURES / "corpus/subscription-testnet-1.4.0"
MUXED_CORPUS = FIXTURES / "corpus/subscription-testnet-1.3.0"
POLICY_CORPUS = FIXTURES / "corpus/subscription-testnet-1.2.0"
PREVIOUS_CORPUS = FIXTURES / "corpus/subscription-testnet-1.1.0"
HISTORICAL_CORPUS = FIXTURES / "corpus/subscription-testnet"
STELLAR = FIXTURES / "stellar"
MAPPINGS = FIXTURES / "corpus/subscription-synthetic/mappings"
T1 = "87835238a663f716a6065a3f588000c4f2a92fa7d03a33e13a5b494d7fae99a2"
T2 = "29bca5708470cb6e912b1e97f3c55f64c038b74a9d3c266639ddd6bce6fb6126"
T3 = "00fd0148b4dc7d334169406bdc0af3829ffe53c72396c563b0077da4300d34bf"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_: Any, **__: Any) -> Any:
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture(scope="module")
def runs() -> dict[str, OnchainRun]:
    return {r.scenario.scenario_id: r for r in run_testnet_vertical(CORPUS, STELLAR, MAPPINGS)}


def token_txs(run: OnchainRun) -> set[str]:
    control = next(
        c
        for c in run.evaluation.result.controls
        if c.control_id == "subscription.token_units_vs_order"
    )
    txs = set()
    for ref in control.evidence_refs:
        observation = run.inputs.observations.get(ref)
        if observation is not None and isinstance(observation.payload, TokenMovementPayload):
            txs.add(observation.payload.chain.tx_hash)
    return txs


@pytest.mark.parametrize(
    "scenario_id", ["TN-LINKED", "TN-NO-LINK", "TN-OVER-LINKED", "TN-LINKED-FAILED"]
)
def test_scenario_matches_expectation(runs: dict[str, OnchainRun], scenario_id: str) -> None:
    assert runs[scenario_id].mismatches() == []


def test_match_rests_on_the_linked_real_delivery_only(runs: dict[str, OnchainRun]) -> None:
    linked = runs["TN-LINKED"]
    assert linked.evaluation.result.result == "MATCH"
    assert token_txs(linked) == {T1}
    effective = {
        o.payload.chain.tx_hash
        for o in linked.inputs.observations.values()
        if isinstance(o.payload, TokenMovementPayload)
        and o.observation_id in linked.evaluation.effective_observation_ids
    }
    assert effective == {T1}  # T3 unlinked and T2 failed are observed but not effective
    chain = [o for o in linked.chain_observations]
    assert {
        o.payload.chain.tx_hash for o in chain if isinstance(o.payload, TokenMovementPayload)
    } == {T1, T2, T3}
    assert all(not o.synthetic for o in chain)


def test_bad_link_is_a_visible_break(runs: dict[str, OnchainRun]) -> None:
    over = runs["TN-OVER-LINKED"]
    assert token_txs(over) == {T1, T3}
    control = next(c for c in over.evaluation.result.controls if c.reason_code == "UNITS_MISMATCH")
    assert control.delta is not None and control.delta.to_decimal_text() == "1000.0000000"


def test_vertical_is_deterministic() -> None:
    first = run_testnet_vertical(CORPUS, STELLAR, MAPPINGS)
    second = run_testnet_vertical(CORPUS, STELLAR, MAPPINGS)
    assert [r.evaluation.result for r in first] == [r.evaluation.result for r in second]


def test_stricter_chain_coverage_would_not_match(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    shutil.copytree(CORPUS, corpus)
    profile = json.loads((corpus / "profile.json").read_text("utf-8"))
    for requirement in profile["coverage_requirements"]:
        if requirement["source_id"] == "stellar-testnet":
            requirement["min_level"] = "internally_checked"
    (corpus / "profile.json").write_text(json.dumps(profile), "utf-8")
    linked = next(
        r
        for r in run_testnet_vertical(corpus, STELLAR, MAPPINGS)
        if r.scenario.scenario_id == "TN-LINKED"
    )
    control = next(
        c
        for c in linked.evaluation.result.controls
        if c.control_id == "subscription.token_units_vs_order"
    )
    assert (control.status, control.reason_code) == ("UNKNOWN", "INSUFFICIENT_COVERAGE")


def test_real_evidence_bundle_replays_offline(runs: dict[str, OnchainRun], tmp_path: Path) -> None:
    linked = runs["TN-LINKED"]
    out = tmp_path / "bundle"
    build_bundle(out, linked.inputs, linked.evaluation, mode="as_known")
    report = verify_bundle(out)
    assert (report.status, report.financial_result) == ("REPRODUCED", "MATCH")
    evidence = json.loads((out / "evidence.json").read_text("utf-8"))
    assert any(
        o["provenance"]["raw_locator"].startswith("horizon:/accounts/")
        for o in evidence["observations"]
    )


def test_cli_demo_testnet(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["demo-testnet", str(CORPUS), "--stellar", str(STELLAR), "--mappings", str(MAPPINGS)]
    )
    assert code == 0
    assert "4/4 testnet scenarios reproduce their expected result" in capsys.readouterr().out


# ------------------------------------------------------------------- scoped quarantine

INVESTOR = "GC6XNJNZOULFUZJBYORCTVNWI6UX5AVOEDC7EXDQM77BPFMQA3VWOYLK"
DEMOA_ISSUER = "GCGGXYAKBIOKOIMVSUTXPEHRLJJU7VB7ZIYYKUFXYIVZTTFIWKEKTBEP"
STRANGER = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"


SCOPED_PROFILE = parse_profile((CORPUS / "profile.json").read_text("utf-8"))
HISTORICAL_PROFILE = parse_profile((HISTORICAL_CORPUS / "profile.json").read_text("utf-8"))


def _with_profile(inputs: EvaluationInputs, profile: Any) -> EvaluationInputs:
    """The same evidence under another version of the profile."""
    return EvaluationInputs(
        snapshot=inputs.snapshot.model_copy(
            update={
                "profile_ref": profile.profile_ref,
                "rules_ref": profile.rules_ref,
                "mapping_refs": admitted_mapping_refs(profile.sources),
            }
        ),
        profile=profile,
        observations=inputs.observations,
        coverage=inputs.coverage,
        identity_links=inputs.identity_links,
    )


def _evaluate_with(
    run: OnchainRun,
    records: list[dict[str, Any]] | None,
    count: int,
    *,
    profile: Any = SCOPED_PROFILE,
    engine_ref: str | None = None,
) -> Any:
    """TN-LINKED re-evaluated with its chain coverage carrying quarantined records, under
    the profile that scopes quarantine (1.1.0) unless told otherwise."""
    inputs = _with_profile(run.inputs, profile)
    chain_id = max(
        (c.recorded_at, i) for i, c in inputs.coverage.items() if c.source_id == "stellar-testnet"
    )[1]  # the latest certificate of the source is the one the engine reads
    chain = inputs.coverage[chain_id]
    altered = CoverageCertificate.model_validate_json(
        json.dumps(
            {
                **chain.model_dump(mode="json"),
                "records_received": chain.records_received + count,
                "records_quarantined": count,
                "quarantined_records": records,
            }
        )
    )
    altered_inputs = EvaluationInputs(
        snapshot=inputs.snapshot,
        profile=inputs.profile,
        observations=inputs.observations,
        coverage={**inputs.coverage, chain_id: altered},
        identity_links=inputs.identity_links,
    )
    if engine_ref is not None:
        return replay(altered_inputs, engine_ref).result
    return evaluate(altered_inputs).result


def _quarantined(*addresses: str | None) -> list[dict[str, Any]]:
    return [
        {
            "locator": f"rpc:getEvents#synthetic-{i}",
            "reasons": ["EFFECT_NOT_A_MOVEMENT"],
            "addresses": None if address is None else [address, DEMOA_ISSUER],
        }
        for i, address in enumerate(addresses)
    ]


def test_a_foreign_quarantined_effect_does_not_make_the_operation_unknown(
    runs: dict[str, OnchainRun],
) -> None:
    run = runs["TN-LINKED"]
    assert run.evaluation.result.result == "MATCH"
    assert evaluate(_with_profile(run.inputs, SCOPED_PROFILE)).result.result == "MATCH"
    assert _evaluate_with(run, _quarantined(STRANGER), 1).result == "MATCH"


def test_the_existing_profile_version_keeps_every_quarantine_blocking(
    runs: dict[str, OnchainRun],
) -> None:
    """1.0.0 declares no scope: under 0.1.0 a foreign record blocks. The current engine no
    longer evaluates 1.0.0 on today's evidence: it refuses it for its provenance (M2), before
    and whatever the quarantine says."""
    run = runs["TN-LINKED"]
    profile = HISTORICAL_PROFILE
    assert run.evaluation.result.versions.engine_ref == ENGINE_REF
    result = _evaluate_with(run, _quarantined(STRANGER), 1, profile=profile, engine_ref=None)
    assert {c.reason_code for c in result.controls} == {"EVALUATION_ERROR"}
    assert "evidence provenance not admitted" in result.controls[0].reason
    replayed = _evaluate_with(
        run, _quarantined(STRANGER), 1, profile=profile, engine_ref="invaria-engine@0.1.0"
    )
    assert replayed.result == "UNKNOWN"
    assert "QUARANTINED_INPUT" in {c.reason_code for c in replayed.controls}


def test_the_retired_engine_reproduces_its_old_semantics(runs: dict[str, OnchainRun]) -> None:
    """0.1.0 ignores the profile's scope and counts every quarantined record."""
    result = _evaluate_with(
        runs["TN-LINKED"], _quarantined(STRANGER), 1, engine_ref="invaria-engine@0.1.0"
    )
    assert result.result == "UNKNOWN"
    assert result.versions.engine_ref == "invaria-engine@0.1.0"


@pytest.mark.parametrize(
    "records",
    [
        _quarantined(INVESTOR),  # an effect on the investor's address: relevant
        _quarantined(STRANGER, None),  # parties unknown: affects every operation
        None,  # a certificate that cannot scope its quarantine
    ],
)
def test_a_relevant_or_unscoped_quarantine_never_yields_match(
    runs: dict[str, OnchainRun], records: list[dict[str, Any]] | None
) -> None:
    result = _evaluate_with(runs["TN-LINKED"], records, 2 if records is None else len(records))
    assert result.result == "UNKNOWN"
    assert "QUARANTINED_INPUT" in {c.reason_code for c in result.controls}


def test_a_chain_effect_bearing_on_the_operation_never_yields_match(
    runs: dict[str, OnchainRun],
) -> None:
    """A DEX fill linked to the operation, or on the investor's address, satisfies nothing
    and is not ignored into a MATCH; a foreign one changes nothing."""
    run = runs["TN-LINKED"]
    inputs = run.inputs
    movement = next(
        o
        for o in inputs.observations.values()
        if o.operation_ref == run.snapshot.operation_ref and o.fact_type == "token_movement"
    )
    assert isinstance(movement.payload, TokenMovementPayload)
    fill = Observation.model_validate_json(
        json.dumps(
            {
                **movement.model_dump(mode="json"),
                "observation_id": "obs-linked-dex-fill",
                "fact_type": "chain_effect",
                "source": {**movement.source.model_dump(), "record_key": "fill:0"},
                # The mapping a SAC fill is written with (derived input, not a sample).
                "provenance": {
                    **movement.provenance.model_dump(),
                    "mapping_ref": RPC_FILL_MAPPING,
                },
                "payload": {
                    "payload_type": "chain_effect",
                    "effect_kind": "dex_fill",
                    "representation": "sac",
                    "account": INVESTOR,
                    "direction": "credit",
                    "counterparty": {"kind": "account", "id": STRANGER},
                    "units": movement.payload.units.model_dump(),
                    "chain": movement.payload.chain.model_dump(mode="json"),
                    "path_payment": None,
                    "transaction": None,
                    "exchange": {
                        "operation_type": "manage_sell_offer",
                        "operation_source": STRANGER,
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
    assert isinstance(fill.payload, ChainEffectPayload)
    foreign_payload = fill.payload.model_copy(
        update={"account": STRANGER, "counterparty": ChainParty(kind="account", id=DEMOA_ISSUER)}
    )
    variants = [
        (fill, True, "UNKNOWN"),  # linked to the operation, with the real delivery present
        (fill, False, "UNKNOWN"),  # cannot stand in for the missing delivery
        (fill.model_copy(update={"operation_ref": None}), True, "UNKNOWN"),  # investor address
        (fill.model_copy(update={"payload": foreign_payload}), True, "UNKNOWN"),  # linked only
        (
            fill.model_copy(update={"operation_ref": None, "payload": foreign_payload}),
            True,
            run.evaluation.result.result,
        ),
    ]
    for effect, with_movement, expected in variants:
        ids = [*run.snapshot.observation_ids, effect.observation_id]
        if not with_movement:
            ids.remove(movement.observation_id)
        result = evaluate(
            EvaluationInputs(
                snapshot=run.snapshot.model_copy(update={"observation_ids": sorted(ids)}),
                profile=inputs.profile,
                observations={**inputs.observations, effect.observation_id: effect},
                coverage=inputs.coverage,
                identity_links=inputs.identity_links,
            )
        ).result
        assert result.result == expected
        if with_movement and expected == "UNKNOWN":
            token = next(
                c for c in result.controls if c.control_id == "subscription.token_units_vs_order"
            )
            assert token.reason_code == "UNSUPPORTED_CAPABILITY"


def test_any_approved_address_of_the_account_makes_a_quarantine_relevant(
    runs: dict[str, OnchainRun],
) -> None:
    """A second address approved for the investor is in scope, whatever its window."""
    run = runs["TN-LINKED"]
    inputs = run.inputs
    (link,) = inputs.identity_links.values()
    other = link.model_copy(
        update={
            "link_id": "link-testnet-second-address",
            "address": STRANGER,
            "valid_from": datetime(2020, 1, 1, tzinfo=UTC),
            "valid_to": datetime(2020, 2, 1, tzinfo=UTC),
        }
    )
    widened = EvaluationInputs(
        snapshot=run.snapshot.model_copy(
            update={"identity_link_ids": sorted([*run.snapshot.identity_link_ids, other.link_id])}
        ),
        profile=inputs.profile,
        observations=inputs.observations,
        coverage=inputs.coverage,
        identity_links={**inputs.identity_links, other.link_id: other},
    )
    assert evaluate(widened).result.result == "MATCH"
    scoped = OnchainRun(
        run.scenario, widened.snapshot, widened, run.evaluation, run.chain_observations, run.log
    )
    assert _evaluate_with(scoped, _quarantined(STRANGER), 1).result == "UNKNOWN"


def test_quarantined_records_must_match_the_count(runs: dict[str, OnchainRun]) -> None:
    chain = next(
        c for c in runs["TN-LINKED"].inputs.coverage.values() if c.source_id == "stellar-testnet"
    )
    document = {
        **chain.model_dump(mode="json"),
        "records_received": chain.records_received + 2,
        "records_quarantined": 2,
    }
    empty = [{"locator": "a", "reasons": ["X"], "addresses": []}, *_quarantined(STRANGER)]
    lowercase = [{"locator": "a", "reasons": ["X"], "addresses": [STRANGER.lower()]}]
    for records in (
        _quarantined(STRANGER),
        _quarantined(STRANGER, STRANGER)[:1] * 2,
        empty,
        lowercase + _quarantined(STRANGER),
    ):
        with pytest.raises(ValueError):
            CoverageCertificate.model_validate_json(
                json.dumps({**document, "quarantined_records": records})
            )


def test_the_latest_chain_certificate_carries_both_paths(runs: dict[str, OnchainRun]) -> None:
    """The engine reads the latest certificate of the source: the SAC one, which also
    carries the Horizon run of the range, so no quarantine of either path is hidden."""
    certificates = [
        c for c in runs["TN-LINKED"].inputs.coverage.values() if c.source_id == "stellar-testnet"
    ]
    latest = max(certificates, key=lambda c: (c.recorded_at, c.coverage_id))
    classic = next(c for c in certificates if "horizon_payments" in c.coverage_id)
    assert "rpc_sac_events" in latest.coverage_id
    assert latest.records_received > classic.records_received
    assert set(classic.raw_sha256) <= set(latest.raw_sha256)


def test_an_over_delivery_stays_a_break_with_an_unresolved_effect(
    runs: dict[str, OnchainRun],
) -> None:
    """TN-OVER-LINKED: the excess of linked deliveries is proven whatever the effect is."""
    run = runs["TN-OVER-LINKED"]
    assert run.evaluation.result.result == "BREAK"
    template = next(o for o in run.inputs.observations.values() if o.fact_type == "token_movement")
    assert isinstance(template.payload, TokenMovementPayload)
    effect = Observation.model_validate_json(
        json.dumps(
            {
                **template.model_dump(mode="json"),
                "observation_id": "obs-over-effect",
                "fact_type": "chain_effect",
                "operation_ref": None,
                "source": {**template.source.model_dump(), "record_key": "over:effect"},
                "payload": {
                    "payload_type": "chain_effect",
                    "effect_kind": "unresolved_movement",
                    "representation": "sac",
                    "account": INVESTOR,
                    "direction": "debit",
                    "counterparty": {"kind": "account", "id": STRANGER},
                    "units": template.payload.units.model_dump(),
                    "chain": template.payload.chain.model_dump(mode="json"),
                    "path_payment": None,
                    "transaction": None,
                    "unresolved": {"reason": "operation_not_found", "operation_type": None},
                },
            }
        )
    )
    inputs = run.inputs
    result = evaluate(
        EvaluationInputs(
            snapshot=run.snapshot.model_copy(
                update={
                    "observation_ids": sorted(
                        [*run.snapshot.observation_ids, effect.observation_id]
                    )
                }
            ),
            profile=inputs.profile,
            observations={**inputs.observations, effect.observation_id: effect},
            coverage=inputs.coverage,
            identity_links=inputs.identity_links,
        )
    ).result
    token = next(c for c in result.controls if c.control_id == "subscription.token_units_vs_order")
    assert (result.result, token.status) == ("BREAK", "FAIL")


def test_the_historical_corpus_reproduces_with_its_retired_engine() -> None:
    """The compatibility implementation of 0.1.0 (replay only) gives the 1.0.0 expectations on
    today's evidence. No evaluation of this corpus was kept, so this tests the compatibility
    implementation, not a recorded conclusion; and the evidence comes from the Classic
    payment mapping 1.1.0, which 1.0.0 does not admit: a new evaluation is refused."""
    for run in run_testnet_vertical(HISTORICAL_CORPUS, STELLAR, MAPPINGS):
        assert run.inputs.profile.profile_ref == "fund-subscription-testnet@1.0.0"
        assert {c.reason_code for c in run.evaluation.result.controls} == {"EVALUATION_ERROR"}
        historical = replay(run.inputs, "invaria-engine@0.1.0")
        old = OnchainRun(
            run.scenario, run.snapshot, run.inputs, historical, run.chain_observations, run.log
        )
        assert old.mismatches() == [], run.scenario.scenario_id
        assert historical.result.versions.engine_ref == "invaria-engine@0.1.0"


@pytest.mark.parametrize(
    "older",
    [HISTORICAL_CORPUS, PREVIOUS_CORPUS, POLICY_CORPUS, MUXED_CORPUS],
    ids=["1.0.0", "1.1.0", "1.2.0", "1.3.0"],
)
def test_the_current_demo_differs_only_in_its_profile_version(
    runs: dict[str, OnchainRun], older: Path
) -> None:
    for name in sorted((older / "raw").iterdir()):
        assert (CORPUS / "raw" / name.name).read_bytes() == name.read_bytes()
    assert sorted(p.name for p in (CORPUS / "raw").iterdir()) == sorted(
        p.name for p in (older / "raw").iterdir()
    )
    assert (CORPUS / "identity_links.json").read_bytes() == (
        older / "identity_links.json"
    ).read_bytes()
    old = json.loads((older / "scenarios.json").read_text("utf-8"))
    new = json.loads((CORPUS / "scenarios.json").read_text("utf-8"))
    assert {k for k in old if old[k] != new[k]} == {"corpus_id", "profile_ref", "notice"}
    assert old["scenarios"] == new["scenarios"]  # same plan, links and expectations
    assert {r.evaluation.result.versions.engine_ref for r in runs.values()} == {ENGINE_REF}
    assert {r.inputs.profile.profile_ref for r in runs.values()} == {
        "fund-subscription-testnet@1.4.0"
    }


def test_profile_1_3_0_only_declares_the_muxed_attribution() -> None:
    """1.3.0 = 1.2.0 plus how a muxed sub-account is attributed and the Classic
    payment mapping 1.1.0 that keeps muxed ids and memos: same controls, coverage and
    quarantine policy; its own references and one disclaimer sentence."""
    old = json.loads((POLICY_CORPUS / "profile.json").read_text("utf-8"))
    new = json.loads((MUXED_CORPUS / "profile.json").read_text("utf-8"))
    assert {k for k in old if old[k] != new[k]} == {
        "profile_ref",
        "rules_ref",
        "disclaimer",
        "correlation",
        "sources",
    }
    assert new["correlation"] == {
        **old["correlation"],
        "muxed_account_link": "identity_link_to_the_muxed_address_only",
    }
    changed = [(a, b) for a, b in zip(old["sources"], new["sources"], strict=True) if a != b]
    assert [(a["source_id"], b["mapping_ref"]) for a, b in changed] == [
        ("stellar-testnet", "stellar-classic-payment@1.1.0")
    ]
    assert new["disclaimer"].startswith(old["disclaimer"].split(" 1.1.0 is unchanged")[0])


def test_profile_1_2_0_only_declares_the_quarantine_policy() -> None:
    """1.2.0 = 1.1.0 plus the declared policy: same sources, controls, coverage
    levels and scope; its own references and one disclaimer sentence."""
    old = json.loads((PREVIOUS_CORPUS / "profile.json").read_text("utf-8"))
    new = json.loads((POLICY_CORPUS / "profile.json").read_text("utf-8"))
    assert {k for k in old if old[k] != new[k]} == {
        "profile_ref",
        "rules_ref",
        "disclaimer",
        "coverage_requirements",
    }
    chain = [r for r in new["coverage_requirements"] if r.get("quarantine_scope")]
    assert len(chain) == 1 and chain[0]["quarantined_records_allowed"] is False
    stripped = [
        {k: v for k, v in r.items() if k != "quarantine_policy"}
        for r in new["coverage_requirements"]
    ]
    assert stripped == old["coverage_requirements"]


def test_why_the_migrated_expectations_do_not_change(runs: dict[str, OnchainRun]) -> None:
    """The rules of 0.2.0 and 0.3.0 read only chain effects and certificates listing
    quarantined records; the demo has neither (plain payments and SAC duplicates)."""
    for run in runs.values():
        members = [run.inputs.observations[i] for i in run.snapshot.observation_ids]
        assert not any(o.fact_type == "chain_effect" for o in members)
        certs = [run.inputs.coverage[i] for i in run.snapshot.coverage_ids]
        assert all(c.quarantined_records is None for c in certs)


def test_profile_1_4_0_only_admits_the_adapter_mappings() -> None:
    """1.4.0 = 1.3.0 plus the mappings admitted for the on-chain source: every
    mapping the adapter writes observations with, besides the Classic payment mapping of its
    authoritative movements; same rules (rules_ref), controls and coverage."""
    old = json.loads((MUXED_CORPUS / "profile.json").read_text("utf-8"))
    new = json.loads((CORPUS / "profile.json").read_text("utf-8"))
    assert {k for k in old if old[k] != new[k]} == {"profile_ref", "disclaimer", "sources"}
    assert new["rules_ref"] == old["rules_ref"]
    changed = [(a, b) for a, b in zip(old["sources"], new["sources"], strict=True) if a != b]
    assert len(changed) == 1
    before, after = changed[0]
    assert {k: v for k, v in after.items() if k != "additional_mapping_refs"} == before
    assert after["mapping_ref"] == HORIZON_MAPPING
    assert after["additional_mapping_refs"] == sorted(MAPPING_REFS - {HORIZON_MAPPING})


@pytest.mark.parametrize(
    "older", [HISTORICAL_CORPUS, PREVIOUS_CORPUS, POLICY_CORPUS], ids=["1.0.0", "1.1.0", "1.2.0"]
)
def test_a_historical_profile_refuses_evidence_of_a_mapping_it_does_not_admit(
    older: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """1.0.0 to 1.2.0 admit stellar-classic-payment@1.0.0; today's adapter
    produces their evidence with 1.1.0. A new evaluation is refused with a diagnostic naming
    the source, the mapping and what the profile admits; nothing is relabelled, and no
    1.0.0 ingestion mode exists. The demo says so instead of reporting mismatches."""
    for run in run_testnet_vertical(older, STELLAR, MAPPINGS):
        assert {c.reason_code for c in run.evaluation.result.controls} == {"EVALUATION_ERROR"}
        reason = run.evaluation.result.controls[0].reason
        assert "source stellar-testnet" in reason
        assert f"mapping {HORIZON_MAPPING}" in reason
        assert "admits: stellar-classic-payment@1.0.0" in reason
        versions = run.evaluation.result.versions
        assert versions.mapping_refs == run.snapshot.mapping_refs  # what the profile admits
        assert versions.evidence_mapping_refs is not None
        assert HORIZON_MAPPING in versions.evidence_mapping_refs  # what produced the evidence
        assert "stellar-classic-payment@1.0.0" not in versions.evidence_mapping_refs
    code = main(
        ["demo-testnet", str(older), "--stellar", str(STELLAR), "--mappings", str(MAPPINGS)]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "REFUSED: evidence provenance not admitted by the profile" in out
    assert "testnet scenarios reproduce" not in out


def test_the_evaluation_states_the_true_provenance_of_its_evidence(
    runs: dict[str, OnchainRun], tmp_path: Path
) -> None:
    """The evaluation and its bundle name the mapping that produced each observation; the
    evaluation's evidence mappings are exactly those of the profile's sources in the
    snapshot, and a bundle whose observation claims another mapping is not reproduced."""
    run = runs["TN-LINKED"]
    members = [run.inputs.observations[i] for i in run.snapshot.observation_ids]
    versions = run.evaluation.result.versions
    assert versions.evidence_mapping_refs == sorted({o.provenance.mapping_ref for o in members})
    assert HORIZON_MAPPING in versions.evidence_mapping_refs
    out = tmp_path / "bundle"
    build_bundle(out, run.inputs, run.evaluation, mode="as_known")
    evidence = json.loads((out / "evidence.json").read_text("utf-8"))
    assert {
        o["observation_id"]: o["provenance"]["mapping_ref"] for o in evidence["observations"]
    } == {o.observation_id: o.provenance.mapping_ref for o in members}
    assert verify_bundle(out).status == "REPRODUCED"
    # Relabel one chain observation as if the profile's mapping 1.0.0 had produced it.
    for o in evidence["observations"]:
        if o["provenance"]["mapping_ref"] == HORIZON_MAPPING:
            o["provenance"]["mapping_ref"] = "stellar-classic-payment@1.0.0"
            break
    (out / "evidence.json").write_text(json.dumps(evidence), "utf-8")
    assert verify_bundle(out).status != "REPRODUCED"


def test_a_chain_effect_mapping_is_admitted_by_1_4_0_and_refused_by_1_3_0(
    runs: dict[str, OnchainRun],
) -> None:
    """Acceptance and refusal by mapping: the same derived SAC fill, written
    with the adapter's fill mapping, is evaluated under 1.4.0, which admits it (UNKNOWN by
    the chain-effect rule, never MATCH), and refused under 1.3.0, which admits only the
    Classic payment mapping for that source."""
    run = runs["TN-LINKED"]
    movement = next(
        o
        for o in run.inputs.observations.values()
        if o.operation_ref == run.snapshot.operation_ref and o.fact_type == "token_movement"
    )
    assert isinstance(movement.payload, TokenMovementPayload)
    document = movement.model_dump(mode="json")
    fill = Observation.model_validate_json(
        json.dumps(
            {
                **document,
                "observation_id": "obs-derived-sac-fill",
                "fact_type": "chain_effect",
                "operation_ref": None,
                "source": {**document["source"], "record_key": "fill:derived"},
                "provenance": {**document["provenance"], "mapping_ref": RPC_FILL_MAPPING},
                "payload": {
                    "payload_type": "chain_effect",
                    "effect_kind": "dex_fill",
                    "representation": "sac",
                    "account": INVESTOR,
                    "direction": "debit",
                    "counterparty": {"kind": "account", "id": STRANGER},
                    "units": document["payload"]["units"],
                    "chain": document["payload"]["chain"],
                    "path_payment": None,
                    "transaction": None,
                    "exchange": {
                        "operation_type": "manage_sell_offer",
                        "operation_source": STRANGER,
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
    results = {}
    for corpus in (CORPUS, MUXED_CORPUS):
        profile = parse_profile((corpus / "profile.json").read_text("utf-8"))
        inputs = _with_profile(run.inputs, profile)
        inputs = EvaluationInputs(
            snapshot=inputs.snapshot.model_copy(
                update={
                    "observation_ids": sorted(
                        [*inputs.snapshot.observation_ids, fill.observation_id]
                    ),
                }
            ),
            profile=profile,
            observations={**inputs.observations, fill.observation_id: fill},
            coverage=inputs.coverage,
            identity_links=inputs.identity_links,
        )
        results[profile.profile_ref] = evaluate(inputs).result
    admitted = results["fund-subscription-testnet@1.4.0"]
    token = next(
        c for c in admitted.controls if c.control_id == "subscription.token_units_vs_order"
    )
    assert (admitted.result, token.reason_code) == ("UNKNOWN", "UNSUPPORTED_CAPABILITY")
    assert admitted.versions.evidence_mapping_refs == sorted(
        {HORIZON_MAPPING, RPC_FILL_MAPPING}
        | {
            o.provenance.mapping_ref
            for o in run.inputs.observations.values()
            if o.source.source_id != "stellar-testnet"
        }
    )
    refused = results["fund-subscription-testnet@1.3.0"]
    assert {c.reason_code for c in refused.controls} == {"EVALUATION_ERROR"}
    assert f"mapping {RPC_FILL_MAPPING}" in refused.controls[0].reason


def test_a_retired_engine_rejects_a_profile_admitting_further_mappings(
    runs: dict[str, OnchainRun],
) -> None:
    """1.4.0 admits further mappings for its chain source: only the engines that state the
    provenance of their evidence evaluate it; 0.7.0, which implements the
    muxed attribution of 1.3.0, still rejects it, naming why."""
    inputs = runs["TN-LINKED"].inputs
    result = replay(inputs, "invaria-engine@0.7.0").result
    assert {c.reason_code for c in result.controls} == {"EVALUATION_ERROR"}
    assert "admits further mappings for a source" in result.controls[0].reason
    assert replay(inputs, ENGINE_REF).result == runs["TN-LINKED"].evaluation.result


def test_a_recorded_refusal_reproduces_in_its_bundle(tmp_path: Path) -> None:
    """Review H1: the refusal of 1.2.0 under 0.8.0 is a conclusion; its bundle replays with
    0.8.0, which refuses again (the admission is part of its semantics): REPRODUCED."""
    (run, *_) = run_testnet_vertical(POLICY_CORPUS, STELLAR, MAPPINGS)
    assert {c.reason_code for c in run.evaluation.result.controls} == {"EVALUATION_ERROR"}
    build_bundle(tmp_path / "refused", run.inputs, run.evaluation, mode="as_known")
    report = verify_bundle(tmp_path / "refused")
    assert report.status == "REPRODUCED", report.reasons
