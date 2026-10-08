"""Structural coherence of the synthetic corpus. These tests do not evaluate scenarios:
expected results are specifications for the future evaluator."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from invaria.contracts import (
    CoverageSet,
    EvidenceBundleManifest,
    IdentityLinkSet,
    ObservationJournal,
    OperationProfile,
    ScenarioCatalog,
    parse_contract,
)
from invaria.contracts.observation import Observation, TokenMovementPayload
from invaria.contracts.stellar import encode_account_id

CORPUS = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"
REQUIRED_SCENARIOS = {
    "K1": "UNKNOWN",
    "K2": "MATCH",
    "K3": "BREAK",
    "K2-historical": "MATCH",
    "RETRACTION": "UNKNOWN",
    "DUPLICATE-DELIVERY": "MATCH",
    "AMBIGUOUS-IDENTITY": "UNKNOWN",
    "AMBIGUOUS-SCALE": "UNKNOWN",
}


def _sha256(relative: str) -> str:
    return hashlib.sha256((CORPUS / relative).read_bytes()).hexdigest()


def _all_observations(journals: dict[str, ObservationJournal]) -> list[Observation]:
    return [o for journal in journals.values() for o in journal.observations]


def test_required_scenarios_and_expected_results(catalog: ScenarioCatalog) -> None:
    found = {s.scenario_id: s.expected.result for s in catalog.scenarios}
    for scenario_id, result in REQUIRED_SCENARIOS.items():
        assert found.get(scenario_id) == result, scenario_id


def test_everything_is_marked_synthetic(
    profile: OperationProfile,
    journals: dict[str, ObservationJournal],
    coverage: CoverageSet,
) -> None:
    assert profile.synthetic and profile.instrument.synthetic
    assert all(o.synthetic for o in _all_observations(journals))
    assert all(c.synthetic for c in coverage.certificates)
    for raw in (CORPUS / "raw").glob("*.json"):
        assert json.loads(raw.read_text("utf-8"))["synthetic"] is True


def test_stellar_identifiers_are_derived_synthetic_labels(
    profile: OperationProfile, journals: dict[str, ObservationJournal]
) -> None:
    def account(label: str) -> str:
        return encode_account_id(hashlib.sha256(label.encode()).digest())

    issuer = account("invaria:synthetic:account:issuer:DEMOA")
    investor = account("invaria:synthetic:account:investor:INV-0001")
    assert profile.representations[0].issuer == issuer
    assert profile.representations[0].network == "stellar:testnet"
    for observation in _all_observations(journals):
        payload = observation.payload
        if isinstance(payload, TokenMovementPayload):
            assert payload.from_address == issuer and payload.to_address == investor
            assert payload.chain.network == profile.representations[0].network
            locator = observation.provenance.raw_locator
            raw = json.loads((CORPUS / locator.split("#")[0]).read_text("utf-8"))
            record = raw["records"][int(locator.rsplit("/", 1)[1])]
            assert record["tx_hash"] == payload.chain.tx_hash
            assert record["asset_issuer"] == issuer
            assert raw["network"] == payload.chain.network
            assert observation.representation_id == profile.representations[0].representation_id


def test_raw_provenance_hashes_match_bytes(journals: dict[str, ObservationJournal]) -> None:
    for observation in _all_observations(journals):
        path = observation.provenance.raw_locator.split("#", 1)[0]
        assert observation.provenance.raw_sha256 == _sha256(path), observation.observation_id


def test_observations_use_profile_sources_and_versions(
    profile: OperationProfile, journals: dict[str, ObservationJournal]
) -> None:
    sources = {s.source_id: s for s in profile.sources}
    for observation in _all_observations(journals):
        source = sources[observation.source.source_id]
        assert observation.fact_type in source.authoritative_for
        assert observation.provenance.mapping_ref == source.mapping_ref
        assert observation.provenance.parser_ref == source.parser_ref
        assert observation.instrument_id == profile.instrument.instrument_id
        if observation.payload is not None and hasattr(observation.payload, "units"):
            units = observation.payload.units
            assert units is not None
            assert (units.unit, units.scale) == ("FUND_SHARE", profile.instrument.scale)


def test_shared_observations_identical_across_timelines(
    journals: dict[str, ObservationJournal],
) -> None:
    seen: dict[str, Observation] = {}
    for observation in _all_observations(journals):
        previous = seen.setdefault(observation.observation_id, observation)
        assert previous == observation, observation.observation_id


def test_order_terms_are_exact(journals: dict[str, ObservationJournal]) -> None:
    order = next(o for o in journals["main"].observations if o.fact_type == "order_accepted")
    payload = order.payload
    assert payload is not None and payload.payload_type == "order_accepted"
    units = int(payload.units.atoms)
    price = int(payload.price_per_unit.atoms)
    cash = int(payload.cash_amount.atoms)
    # units at scale 7 x price at scale 2 -> cash at scale 2, without rounding
    assert (units * price) % 10**7 == 0
    assert units * price // 10**7 == cash == 10_000_000


def test_correction_preserves_history(journals: dict[str, ObservationJournal]) -> None:
    main = {o.observation_id: o for o in journals["main"].observations}
    b1, b2 = main["obs-B1"], main["obs-B2"]
    assert b2.supersedes == "obs-B1"
    assert (b1.source.record_key, b1.source.revision) == ("PAY-0001", 1)
    assert (b2.source.record_key, b2.source.revision) == ("PAY-0001", 2)
    assert b1.payload is not None and b1.payload.payload_type == "cash_settled"
    assert b1.payload.amount.atoms == "10000000"  # the original is kept, not edited
    assert b2.payload is not None and b2.payload.payload_type == "cash_settled"
    assert b2.payload.amount.atoms == "9950000"
    assert b2.valid_time == b1.valid_time and b2.recorded_at > b1.recorded_at


def test_duplicate_delivery_is_same_record_and_content(
    journals: dict[str, ObservationJournal],
) -> None:
    obs = {o.observation_id: o for o in journals["duplicate-delivery"].observations}
    first, again = obs["obs-T1"], obs["obs-T1-redelivery"]
    assert first.source == again.source
    assert first.provenance == again.provenance and first.payload == again.payload
    assert first.recorded_at != again.recorded_at


def test_source_conflict_is_same_key_different_content(
    journals: dict[str, ObservationJournal],
) -> None:
    obs = {o.observation_id: o for o in journals["source-conflict"].observations}
    original, resent = obs["obs-B1"], obs["obs-B1-conflicting-resend"]
    assert original.source == resent.source and resent.supersedes is None
    assert original.payload != resent.payload


def test_ambiguous_identity_has_no_execution_link(
    journals: dict[str, ObservationJournal],
) -> None:
    movements = [
        o for o in journals["ambiguous-identity"].observations if o.fact_type == "token_movement"
    ]
    assert len(movements) == 2 and all(m.operation_ref is None for m in movements)
    assert len({m.source.record_key for m in movements}) == 2


def test_ambiguous_scale_row_is_quarantined_not_observed(
    journals: dict[str, ObservationJournal], coverage: CoverageSet
) -> None:
    timeline = journals["ambiguous-scale"].observations
    assert all(o.fact_type != "units_registered" for o in timeline)
    cert = next(c for c in coverage.certificates if c.coverage_id == "cov-ta-scale-ambiguous-k2")
    assert cert.records_quarantined == 1 and not cert.is_gap_free
    row = (CORPUS / "raw/ta_register_2026-10-01_scale_ambiguous.csv").read_text("utf-8")
    assert ",1000," in row.splitlines()[1]
    assert cert.raw_sha256 == [_sha256("raw/ta_register_2026-10-01_scale_ambiguous.csv")]


def test_coverage_hashes_and_sources(coverage: CoverageSet, profile: OperationProfile) -> None:
    sources = {s.source_id for s in profile.sources}
    raw_hashes = {_sha256(f"raw/{p.name}") for p in (CORPUS / "raw").iterdir()}
    for cert in coverage.certificates:
        assert cert.source_id in sources
        assert set(cert.raw_sha256) <= raw_hashes, cert.coverage_id


def test_snapshots_are_closed_and_consistent(
    catalog: ScenarioCatalog,
    journals: dict[str, ObservationJournal],
    coverage: CoverageSet,
    links: IdentityLinkSet,
    profile: OperationProfile,
) -> None:
    certs = {c.coverage_id: c for c in coverage.certificates}
    link_ids = {link.link_id: link for link in links.links}
    for scenario in catalog.scenarios:
        snap = scenario.snapshot
        journal = {o.observation_id: o for o in journals[scenario.timeline_id].observations}
        known = {oid for oid, o in journal.items() if o.recorded_at <= snap.known_at}
        # closed snapshot = everything the journal had recorded at known_at, nothing later
        assert set(snap.observation_ids) == known, scenario.scenario_id
        for cid in snap.coverage_ids:
            assert certs[cid].recorded_at <= snap.known_at, (scenario.scenario_id, cid)
        for lid in snap.identity_link_ids:
            assert link_ids[lid].recorded_at <= snap.known_at
        assert snap.profile_ref == profile.profile_ref == catalog.profile_ref
        assert snap.rules_ref == profile.rules_ref
        assert set(snap.mapping_refs) == {s.mapping_ref for s in profile.sources}
        assert set(scenario.expected.effective_observation_ids) <= set(snap.observation_ids)
        expected_controls = [c.control_id for c in scenario.expected.controls]
        assert expected_controls == [c.control_id for c in profile.controls]


def test_k2_historical_excludes_later_correction(
    catalog: ScenarioCatalog, journals: dict[str, ObservationJournal]
) -> None:
    by_id = {s.scenario_id: s for s in catalog.scenarios}
    k2, hist, k3 = by_id["K2"], by_id["K2-historical"], by_id["K3"]
    b2 = next(o for o in journals["main"].observations if o.observation_id == "obs-B2")
    assert hist.timeline_id == k3.timeline_id == "main"
    assert hist.snapshot.observation_ids == k2.snapshot.observation_ids
    assert hist.snapshot.known_at == k2.snapshot.known_at < b2.recorded_at
    assert hist.snapshot.evaluation_clock >= k3.snapshot.known_at
    assert "obs-B2" in k3.snapshot.observation_ids
    assert hist.expected.result == "MATCH" and k3.expected.result == "BREAK"


def test_k3_delta_is_exact(catalog: ScenarioCatalog) -> None:
    k3 = next(s for s in catalog.scenarios if s.scenario_id == "K3")
    cash = next(c for c in k3.expected.controls if c.control_id == "subscription.cash_vs_order")
    assert cash.status == "FAIL" and cash.delta is not None
    assert (cash.delta.atoms, cash.delta.scale, cash.delta.unit) == ("-50000", 2, "USD")
    assert cash.delta.to_decimal_text() == "-500.00"


def test_retraction_is_loss_of_support_not_break(catalog: ScenarioCatalog) -> None:
    retraction = next(s for s in catalog.scenarios if s.scenario_id == "RETRACTION")
    cash = next(
        c for c in retraction.expected.controls if c.control_id == "subscription.cash_vs_order"
    )
    assert (cash.status, cash.reason_code) == ("UNKNOWN", "LOSS_OF_SUPPORT")
    assert retraction.expected.result == "UNKNOWN"


def test_bundle_manifest_hashes_match_corpus() -> None:
    text = (CORPUS / "bundle_manifest_K2.draft.json").read_text("utf-8")
    manifest = parse_contract(EvidenceBundleManifest, text)
    for artifact in manifest.artifacts:
        assert artifact.sha256 == _sha256(artifact.path), artifact.path
    assert manifest.snapshot_id == "snap-K2" and manifest.expected_result == "MATCH"


def test_kit_acceptance_refs_exist(catalog: ScenarioCatalog) -> None:
    declared = json.loads((CORPUS.parents[1] / "acceptance-cases.json").read_text("utf-8"))["cases"]
    ids = {case["id"] for case in declared}
    for scenario in catalog.scenarios:
        assert set(scenario.acceptance_refs) <= ids


@pytest.mark.parametrize("name", sorted(p.name for p in (CORPUS / "raw").iterdir()))
def test_raw_files_have_no_float_ambiguity_in_json(name: str) -> None:
    if name.endswith(".json"):
        from invaria.contracts import check_strict_json

        check_strict_json((CORPUS / "raw" / name).read_text("utf-8"))
