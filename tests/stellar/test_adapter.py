"""Offline tests of the Stellar adapter. Real testnet responses are replayed from
recordings; nothing here touches the network (sockets are blocked for the replays)."""

from __future__ import annotations

import ast
import base64
import copy
import hashlib
import json
import shutil
import socket
import struct
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from invaria.cli import main
from invaria.contracts.base import parse_contract
from invaria.contracts.chain import ChainTarget, ExecutionLink
from invaria.contracts.observation import (
    ChainEffectPayload,
    ChainParty,
    Observation,
    TokenMovementPayload,
    TransactionMemo,
    party_identity,
)
from invaria.contracts.stellar import claimable_balance_hex
from invaria.stellar.adapter import (
    Exclusion,
    PendingBalanceEffect,
    classic_clawback,
    ingest_horizon_payments,
    ingest_sac_events,
    integrate_chain,
    least_resolved,
    normalize_horizon_record,
    normalize_sac_event,
    resolve_sac_contract,
    toid_parts,
)
from invaria.stellar.executions import executions
from invaria.stellar.http import (
    ChainUnavailable,
    ReplayClient,
    Response,
    UrllibClient,
    request_key,
)
from invaria.stellar.sources import (
    DataUnavailable,
    Endpoints,
    Horizon,
    NetworkMismatch,
    Page,
    Rpc,
    verify_network,
)
from invaria.stellar.store import IngestStore
from invaria.stellar.xdr import XdrError, decode_scval, sac_contract_id

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/stellar"
RECORDINGS = FIXTURES / "recordings"
TARGETS = FIXTURES / "targets"
ISSUER = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
USDC_SAC = "CBIELTK6YBZJU5UP2WWQEUCYKLPU6AUNZ2BQ4WWFEIE3USCIHMXQDAMA"
TESTNET = "Test SDF Network ; September 2015"
# The testnet USDC/XLM liquidity pool seen in the path payment samples (Horizon id
# 4cd1f6defba237ee...); never attributed to its liquidity providers.
POOL_ID = "LBGND5W67ORDP3WLYX7P4JM7RHV4JNPN2SIRNPVVKNWEANH4JDLD7Q7I"
GDQLIJ = "GDQLIJV7OYIFRNZ7TZ7TCKB5YGHWECCUX2Q3PCKS7RBIWC7PDN6LAQUE"
RECORDED_AT = datetime(2026, 10, 5, 0, 41, tzinfo=UTC)
SAMPLES = {
    "usdc-gclcz": (5024520, 5024600),
    "usdc-issuer": (5015930, 5015945),
    "usdc-gb4mm": (5000475, 5000490),
    "usdc-gcdkpp": (5061229, 5061233),
    "usdc-gazy2f": (5061281, 5061285),
    "usdc-gbain6": (5069339, 5069339),
    "usdc-issuer-fill": (5069194, 5069194),
    "testusb-holder-clawback": (5070992, 5070992),
    "testusb-issuer-clawback": (5070992, 5070992),
    "gdice-issuer-cb-clawback": (5061607, 5061607),
    "p0b41b8-holder-admin-clawback": (5070289, 5070289),
    # Typed parties (captured 2026-10-07)
    "usdc-gdes54wt-cb-create": (5024596, 5024599),
    "usdc-gapnrjhi-cb-claim": (5024599, 5024599),
    "usdc-gdbxa45u-contract": (5024520, 5024524),
    "usdc-feebump-payment": (5052280, 5052280),
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_: Any, **__: Any) -> Any:
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def target(name: str) -> ChainTarget:
    return parse_contract(ChainTarget, (TARGETS / f"{name}.json").read_text("utf-8"))


def clients(sample: str, directory: Path | None = None) -> tuple[Horizon, Rpc]:
    client = ReplayClient(directory or RECORDINGS / sample)
    endpoints = Endpoints.resolve(None, None)
    return Horizon(endpoints.horizon_url, client), Rpc(endpoints.rpc_url, client)


def ingest(
    sample: str,
    store: IngestStore,
    *,
    max_pages: int = 50,
    directory: Path | None = None,
    links: tuple[ExecutionLink, ...] = (),
) -> Any:
    horizon, rpc = clients(sample, directory)
    check = verify_network(horizon, rpc, "stellar:testnet")
    start, end = SAMPLES[sample]
    classic = ingest_horizon_payments(
        target(sample),
        horizon,
        store,
        start_ledger=start,
        end_ledger=end,
        history=(check.horizon_elder_ledger, check.horizon_latest_ledger),
        recorded_at=RECORDED_AT,
        links=links,
        page_limit=2,
        max_pages=max_pages,
    )
    sac = ingest_sac_events(
        target(sample),
        rpc,
        horizon,
        store,
        start_ledger=start,
        end_ledger=end,
        recorded_at=RECORDED_AT,
        links=links,
        page_limit=200,
        max_pages=max_pages,
    )
    return classic, sac


def movement(observation: Observation) -> TokenMovementPayload:
    assert isinstance(observation.payload, TokenMovementPayload)
    return observation.payload


def effect(observation: Observation) -> ChainEffectPayload:
    assert isinstance(observation.payload, ChainEffectPayload)
    return observation.payload


# ------------------------------------------------------------- network identity


def test_recorded_endpoints_report_testnet() -> None:
    for sample in SAMPLES:
        horizon, rpc = clients(sample)
        check = verify_network(horizon, rpc, "stellar:testnet")
        assert check.passphrase == TESTNET and check.protocol_version >= 23


def test_wrong_network_is_rejected_before_reading_data() -> None:
    horizon, rpc = clients("usdc-issuer")
    with pytest.raises(NetworkMismatch):
        verify_network(horizon, rpc, "stellar:pubnet")


def test_tampered_recording_fails_loudly(tmp_path: Path) -> None:
    copy_dir = tmp_path / "rec"
    shutil.copytree(RECORDINGS / "usdc-issuer", copy_dir)
    body = next(copy_dir.glob("*.body"))
    body.write_bytes(body.read_bytes() + b" ")
    with pytest.raises(ChainUnavailable):
        ingest("usdc-issuer", IngestStore(tmp_path / "store"), directory=copy_dir)


# --------------------------------------------------------------- real samples


def test_classic_sample_gclcz(tmp_path: Path) -> None:
    store = IngestStore(tmp_path)
    classic, sac = ingest("usdc-gclcz", store)
    assert classic.complete and classic.pages == 4  # page size 2: real pagination
    amounts = [movement(o).units.to_decimal_text() for o in classic.appended]
    assert amounts == [
        "51.6300748",
        "51.6300748",
        "2.0247088",
        "51.6300748",
        "1.0000000",
        "51.6325535",
    ]
    assert all(o.representation_id and o.instrument_id.endswith(ISSUER) for o in classic.appended)
    assert all(movement(o).chain.tx_successful for o in classic.appended)
    assert all(o.operation_ref is None for o in classic.appended)  # nothing links to SUB-0001
    # unified SAC events: the same 6 economic effects. One carries the u64 to_muxed_id
    # 732207663626: the Classic payment names no muxed receiver and its memo is the id
    # 732207663626, so under CAP-67 the u64 is that memo, never a sub-account.
    # Until increment 4 it was quarantined as MUXED_ACCOUNT.
    assert sac.complete and sac.appended == () and sac.duplicates == 6
    assert sac.exclusions == ()
    assert len(store.observations()) == 6
    memos = [movement(o).memo for o in classic.appended]
    assert TransactionMemo(memo_type="id", value="732207663626") in memos
    assert all(movement(o).to_muxed_id is None for o in classic.appended)
    coverage = {c.coverage_id.rsplit("-", 2)[0]: c for c in store.coverage()}
    horizon_cov = coverage["cov-testnet-usdc-gclcz-horizon_payments"]
    assert (horizon_cov.records_received, horizon_cov.records_quarantined) == (6, 0)
    assert horizon_cov.level == "provider_claimed" and horizon_cov.is_gap_free
    assert horizon_cov.ledger_range is not None and horizon_cov.ledger_range.first == 5024520
    sac_cov = coverage["cov-testnet-usdc-gclcz-rpc_sac_events"]
    assert sac_cov.records_quarantined == 0


def test_issuer_burn_is_one_effect_across_paths(tmp_path: Path) -> None:
    store = IngestStore(tmp_path)
    classic, sac = ingest("usdc-issuer", store)
    (burn,) = classic.appended
    payload = movement(burn)
    assert payload.to_address == ISSUER and payload.units.atoms == "200000000"
    assert payload.chain.ledger == 5015939 and payload.chain.operation_index == 0
    assert burn.source.record_key == f"{payload.chain.tx_hash}:0:0"
    assert sac.duplicates == 1 and sac.appended == ()


def test_real_path_payment_is_kept_as_chain_effects_not_movements(tmp_path: Path) -> None:
    """Real testnet strict_receive (usdc-gb4mm): USDCAllow -> USDC, GB4MM -> GAYF33."""
    store = IngestStore(tmp_path)
    classic, sac = ingest("usdc-gb4mm", store)
    tx = "fbab5a26d8bcab43bf9178101e61c878148c67a2a56bb1f23a1997c5dbdc24d0"
    gayf, gb4mm = (
        "GAYF33NNNMI2Z6VNRFXQ64D4E4SF77PM46NW3ZUZEEU5X7FCHAZCMHKU",
        target("usdc-gb4mm").account,
    )
    assert all(o.fact_type == "chain_effect" for o in store.observations())
    (credit,) = classic.appended  # the source asset is not USDC: no debit effect
    payload = effect(credit)
    assert credit.source.record_key == f"{tx}:0:classic:credit" and credit.operation_ref is None
    assert (payload.effect_kind, payload.account, payload.direction) == (
        "path_payment_credit",
        gayf,
        "credit",
    )
    assert payload.units is not None and payload.units.atoms == "345670000000"
    context = payload.path_payment
    assert context is not None and context.operation_type == "path_payment_strict_receive"
    assert context.source_asset.startswith("USDCAllow:") and context.source_amount == "345670000000"
    assert context.destination_amount == "345670000000" and context.path == []
    assert payload.transaction is not None
    assert (payload.transaction.fee_account, payload.transaction.fee_charged.atoms) == (
        gb4mm,
        "200",
    )
    assert [e.reason for e in classic.exclusions] == ["EFFECT_NOT_A_MOVEMENT"]
    assert classic.coverage is not None and not classic.coverage.is_gap_free
    # SAC: the conversion (mint from the issuer) and the transfer are legs, not movements.
    mint, transfer = sac.appended
    assert [o.source.record_key for o in sac.appended] == [f"{tx}:0:sac:0", f"{tx}:0:sac:1"]
    # Legs are seen from their sender, whoever the watched account is.
    assert (effect(mint).account, effect(mint).direction) == (ISSUER, "debit")
    assert effect(mint).counterparty == ChainParty(kind="account", id=gb4mm)
    assert (effect(transfer).account, effect(transfer).direction) == (gb4mm, "debit")
    assert effect(transfer).counterparty == ChainParty(kind="account", id=gayf)
    (check,) = store.correspondence()
    assert check["status"] == "corroborated"
    assert check["classic_net"] == check["sac_net"] == {gayf: "345670000000", gb4mm: "0"}
    assert sac.coverage is not None and not sac.coverage.is_gap_free


GAYF33 = "GAYF33NNNMI2Z6VNRFXQ64D4E4SF77PM46NW3ZUZEEU5X7FCHAZCMHKU"


@pytest.mark.parametrize(
    ("sample", "tx", "submitter", "offer", "direction", "atoms", "sold", "bought"),
    [
        # GBAIN6's offer 894515 (LUSD for USDC) crossed by GALBIMJM's strict_receive.
        (
            "usdc-gbain6",
            "836d21330e88f9b268132dc7499d05ada4e27780b210ebc36f7274ebd664ff95",
            "GALBIMJMMFXWP2FCDPV34OJXZTZDGPXYXOYNH4XKVR2I6Q2H3XUERYNM",
            "894515",
            "credit",
            "2479821",
            "LUSD",
            "USDC",
        ),
        # The issuer's offer 32 (USDC for USDCAllow) crossed by GB4MM's strict_receive: the
        # SAC side is a mint, which the first adapter release would have read as a movement.
        (
            "usdc-issuer-fill",
            "ba0ab984ac337907883f1b3d1046ab6df94a1925436b38642b64e1817dec9362",
            "GB4MMSZ5FY3KOCMMN77DNJBSKXFZVRXMLM5SKKDIVGTWGR55DKJM7GSD",
            "32",
            "debit",
            "345670000000",
            "USDC",
            "USDCAllow",
        ),
    ],
)
def test_real_dex_fill_is_an_exchange_effect_not_a_movement(
    tmp_path: Path,
    sample: str,
    tx: str,
    submitter: str,
    offer: str,
    direction: str,
    atoms: str,
    sold: str,
    bought: str,
) -> None:
    """A fill of the watched account's offer: Horizon does not list it among the account's
    payments, which is the endpoint's scope, not evidence that nothing moved."""
    store = IngestStore(tmp_path)
    classic, sac = ingest(sample, store)
    watched = target(sample).account
    assert classic.appended == () and classic.exclusions == ()
    assert classic.coverage is not None and classic.coverage.is_gap_free
    assert not any(o.fact_type == "token_movement" for o in store.observations())
    fill_classic, fill_sac = sac.appended
    assert fill_classic.source.record_key.startswith(f"{tx}:0:classic:fill:")
    assert fill_sac.source.record_key.startswith(f"{tx}:0:sac:0:dex_fill:")
    for observation in (fill_classic, fill_sac):
        payload = effect(observation)
        assert (payload.effect_kind, payload.account, payload.direction) == (
            "dex_fill",
            watched,
            direction,
        )
        assert payload.units is not None and payload.units.atoms == atoms
        assert observation.operation_ref is None
        # The submitter is the technical counterparty, never the owner of the effect.
        assert payload.counterparty == ChainParty(kind="account", id=submitter)
        assert payload.exchange is not None
        assert payload.exchange.operation_type == "path_payment_strict_receive"
        assert payload.exchange.operation_source == submitter
    trade = effect(fill_classic).exchange
    assert trade is not None and trade.offer_id == offer
    assert trade.sold_asset is not None and trade.sold_asset.split(":")[0] == sold
    assert trade.bought_asset is not None and trade.bought_asset.split(":")[0] == bought
    sac_exchange = effect(fill_sac).exchange
    assert sac_exchange is not None and sac_exchange.offer_id is None
    (check,) = store.correspondence()
    assert (check["kind"], check["status"], check["claim"]) == (
        "dex_fill",
        "corroborated",
        "aggregate_reconciliation",
    )
    assert [e.reason for e in sac.exclusions] == ["EFFECT_NOT_A_MOVEMENT"]
    assert sac.coverage is not None and sac.coverage.quarantined_records is not None
    (record,) = sac.coverage.quarantined_records
    # In scope: the account, the submitter and the operation's destination (scope only).
    destination = {"usdc-gbain6": submitter, "usdc-issuer-fill": GAYF33}[sample]
    assert record.addresses == sorted({watched, submitter, destination})


def test_real_failed_path_payment_keeps_no_amount(tmp_path: Path) -> None:
    """Real FAILED strict_receive (usdc-gcdkpp): Horizon shows source_amount 0.0000000 and
    the requested 40613661.1519794 XLM; neither was executed."""
    store = IngestStore(tmp_path)
    classic, sac = ingest("usdc-gcdkpp", store)
    gcdkpp = target("usdc-gcdkpp").account
    failed_tx = "c1959d21a148894d7f0e58d4a3d2873c4811a101606a9c0f2adbc9ebb0108e25"
    (failed,) = [o for o in classic.appended if o.source.record_key.startswith(failed_tx)]
    payload = effect(failed)
    assert failed.source.record_key == f"{failed_tx}:0:classic:failed"
    assert (payload.effect_kind, payload.account, payload.units) == (
        "path_payment_failed",
        gcdkpp,
        None,
    )
    assert not payload.chain.tx_successful and payload.path_payment is not None
    assert payload.path_payment.source_asset == f"USDC:{ISSUER}"
    assert payload.path_payment.destination_asset == "native"
    assert (payload.path_payment.source_amount, payload.path_payment.destination_amount) == (
        None,
        None,
    )
    assert payload.transaction is not None
    assert payload.transaction.fee_charged.atoms == "100"
    assert payload.transaction.result_xdr == "AAAAAAAAAGT/////AAAAAQAAAAAAAAAC////9AAAAAA="
    checks = {e["tx_hash"]: e["status"] for e in store.correspondence()}
    assert checks[failed_tx] == "failed_no_effect"
    # The successful XLM -> USDC operation of the same window: credit of 341.3500694 USDC.
    # Its SAC side has a liquidity pool leg (ScAddress type 4). Since typed parties
    # the pool is a typed party, so the correspondence resolves (it was sac_incomplete).
    credit_tx = "994d6b2f63c99aaba4f17094d1839ab76bdc4fb41039b50f2a722e64ee3a4c3c"
    (credit,) = [o for o in classic.appended if o.source.record_key.startswith(credit_tx)]
    assert credit.source.record_key == f"{credit_tx}:2:classic:credit"
    credited = effect(credit).units
    assert credited is not None and credited.atoms == "3413500694"
    assert checks[credit_tx] == "corroborated"
    (pool_leg,) = [o for o in sac.appended if effect(o).counterparty.kind == "liquidity_pool"]  # type: ignore[union-attr]
    # The pool converts for the path payment's source; it is a party, never an account.
    assert effect(pool_leg).counterparty == ChainParty(kind="liquidity_pool", id=POOL_ID)
    assert (effect(pool_leg).account, effect(pool_leg).direction) == (GDQLIJ, "credit")
    assert [e.reason for e in sac.exclusions] == ["EFFECT_NOT_A_MOVEMENT"]
    assert all(o.fact_type == "chain_effect" for o in store.observations())


def test_real_strict_send_to_itself_in_the_target_asset(tmp_path: Path) -> None:
    """Real strict_send XLM -> USDC from GAZY2F to itself (usdc-gazy2f): 0.0000009 USDC."""
    store = IngestStore(tmp_path)
    classic, sac = ingest("usdc-gazy2f", store)
    account = target("usdc-gazy2f").account
    (credit,) = classic.appended
    payload = effect(credit)
    assert (
        payload.effect_kind,
        payload.account,
        payload.units.atoms if payload.units else None,
    ) == (
        "path_payment_credit",
        account,
        "9",
    )
    assert payload.counterparty == ChainParty(kind="account", id=account)
    assert payload.path_payment is not None and payload.path_payment.source_asset == "native"
    assert payload.path_payment.source_amount == "10"  # 0.0000010 XLM, executed, unconverted
    # The pool -> GAZY2F leg is a typed leg since increment 3 (it was undecodable), and
    # the self transfer of GAZY2F is the other leg.
    pool_leg, own = sorted(sac.appended, key=lambda o: o.source.record_key)
    assert effect(pool_leg).counterparty == ChainParty(kind="liquidity_pool", id=POOL_ID)
    assert (effect(pool_leg).account, effect(pool_leg).direction) == (account, "credit")
    assert (effect(own).account, effect(own).direction) == (account, "debit")
    assert effect(own).counterparty == ChainParty(kind="account", id=account)
    (check,) = store.correspondence()
    assert check["status"] == "corroborated"  # net and gross of GAZY2F: +0.0000009
    # The path legs (one SAC record) plus the Horizon record of the same operation; no
    # correspondence conflict any more.
    assert sac.coverage is not None and sac.coverage.records_quarantined == 1 + 1


def test_ingestion_is_deterministic(tmp_path: Path) -> None:
    first, second = IngestStore(tmp_path / "a"), IngestStore(tmp_path / "b")
    ingest("usdc-gclcz", first)
    ingest("usdc-gclcz", second)
    for name in ("observations.jsonl", "coverage.jsonl", "exclusions.jsonl"):
        a, b = tmp_path / "a" / name, tmp_path / "b" / name
        assert a.is_file() == b.is_file()  # gclcz has no exclusion since increment 4
        assert not a.is_file() or a.read_bytes() == b.read_bytes()


def test_provenance_points_to_stored_raw_pages(tmp_path: Path) -> None:
    store = IngestStore(tmp_path)
    ingest("usdc-gclcz", store)
    for observation in store.observations():
        page = tmp_path / "pages" / f"{observation.provenance.raw_sha256}.json"
        assert hashlib.sha256(page.read_bytes()).hexdigest() == observation.provenance.raw_sha256
        assert observation.provenance.parser_ref.startswith("stellar-horizon-payments-parser@")


def test_toid_decoding_matches_real_operation() -> None:
    assert toid_parts(21543293963735041) == (5015939, 1, 0)
    with pytest.raises(ValueError):
        toid_parts(21543293963735040)  # transaction TOID, not an operation


# ------------------------------------------------- pagination, budget, checkpoints


def test_budget_exhaustion_gives_no_coverage_then_resume_completes(tmp_path: Path) -> None:
    store = IngestStore(tmp_path)
    horizon, rpc = clients("usdc-gclcz")
    check = verify_network(horizon, rpc, "stellar:testnet")
    history = (check.horizon_elder_ledger, check.horizon_latest_ledger)
    partial = ingest_horizon_payments(
        target("usdc-gclcz"),
        horizon,
        store,
        start_ledger=5024520,
        end_ledger=5024600,
        history=history,
        recorded_at=RECORDED_AT,
        page_limit=2,
        max_pages=2,
    )
    assert not partial.complete and partial.coverage is None and "budget" in partial.stop_reason
    checkpoint = store.get_checkpoint("testnet-usdc-gclcz", "horizon_payments")
    assert checkpoint is not None and checkpoint.pages == 2 and not checkpoint.complete
    resumed = ingest_horizon_payments(
        target("usdc-gclcz"),
        horizon,
        store,
        start_ledger=5024520,
        end_ledger=5024600,
        history=history,
        recorded_at=RECORDED_AT,
        page_limit=2,
        max_pages=50,
    )
    assert resumed.complete and len(store.observations()) == 6
    assert resumed.coverage is not None and resumed.coverage.records_received == 6


def test_checkpoint_does_not_advance_when_persistence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = IngestStore(tmp_path)
    original = IngestStore.append_observations
    calls = {"n": 0}

    def failing(self: IngestStore, observations: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        original(self, observations)

    monkeypatch.setattr(IngestStore, "append_observations", failing)
    with pytest.raises(OSError):
        ingest("usdc-gclcz", store)
    checkpoint = store.get_checkpoint("testnet-usdc-gclcz", "horizon_payments")
    assert checkpoint is not None and checkpoint.pages == 1  # page 2 not acknowledged
    monkeypatch.setattr(IngestStore, "append_observations", original)
    ingest("usdc-gclcz", store)
    keys = [o.source.record_key for o in store.observations()]
    assert len(keys) == 6 == len(set(keys))


def test_crash_after_persist_before_checkpoint_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = IngestStore(tmp_path)
    original = IngestStore.put_checkpoint
    calls = {"n": 0}

    def crash(self: IngestStore, checkpoint: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("power loss")
        original(self, checkpoint)

    monkeypatch.setattr(IngestStore, "put_checkpoint", crash)
    with pytest.raises(OSError):
        ingest("usdc-gclcz", store)
    monkeypatch.setattr(IngestStore, "put_checkpoint", original)
    classic, _ = ingest("usdc-gclcz", store)
    assert classic.duplicates == 2  # page 2 replayed, recognised, not appended again
    assert len(store.observations()) == 6


def test_redelivered_pages_do_not_duplicate_effects(tmp_path: Path) -> None:
    store = IngestStore(tmp_path)
    ingest("usdc-gclcz", store)
    for checkpoint in (tmp_path / "checkpoints").iterdir():
        checkpoint.unlink()
    classic, sac = ingest("usdc-gclcz", store)
    assert classic.appended == () and classic.duplicates == 6
    assert sac.appended == () and sac.duplicates == 6  # the memo-id event corroborates too
    assert len(store.observations()) == 6


def test_conflicting_content_for_same_effect_is_kept_not_chosen(tmp_path: Path) -> None:
    store = IngestStore(tmp_path)
    ingest("usdc-issuer", store)
    (existing,) = store.observations()
    data = existing.model_dump(mode="json")
    data["observation_id"] = "obs-conflicting-delivery"
    data["payload"]["units"]["atoms"] = "199999999"
    other = Observation.model_validate_json(json.dumps(data))
    appended, duplicates, conflicts = integrate_chain(store.observations(), [other])
    assert appended == [other] and duplicates == 0 and conflicts == [existing.source.record_key]


# --------------------------------------------------------- record-level rules


def _page_and_record(sample: str) -> tuple[Page, dict[str, Any]]:
    horizon, _ = clients(sample)
    start, _ = SAMPLES[sample]
    page = horizon.account_payments(target(sample).account, str(start << 32), 2)
    return page, copy.deepcopy(page.document["_embedded"]["records"][0])


def _normalize(sample: str, record: dict[str, Any], page: Page) -> Any:
    """The single result, None when out of scope (the record-level rules give at most one)."""
    results = normalize_horizon_record(target(sample), record, page, 0, RECORDED_AT, {})
    assert len(results) <= 1
    return results[0] if results else None


def test_failed_transaction_is_observed_without_effect() -> None:
    page, record = _page_and_record("usdc-issuer")
    record["transaction_successful"] = False
    record["transaction"]["successful"] = False
    observation = _normalize("usdc-issuer", record, page)
    assert isinstance(observation, Observation) and not movement(observation).chain.tx_successful


def test_a_quarantined_record_names_the_ledger_operation_as_the_engine_does() -> None:
    """The bounded quarantine joins the adapter and the engine: a quarantined record on the
    ledger operation of a counted delivery must carry exactly the ``<tx_hash>:<op_index>``
    the engine builds from that delivery's chain. A real payment, and a DERIVED copy of it as a
    contract call (which the Horizon route excludes; a muxed payment no longer is since
    increment 4), name the same operation in the same form."""
    page, record = _page_and_record("usdc-issuer")
    delivery = _normalize("usdc-issuer", record, page)
    assert isinstance(delivery, Observation)
    chain = movement(delivery).chain
    call = dict(
        record,
        type="invoke_host_function",
        asset_balance_changes=[
            {
                "asset_code": record["asset_code"],
                "asset_issuer": record["asset_issuer"],
                "type": "transfer",
                "from": record["from"],
                "to": record["to"],
            }
        ],
    )
    (excluded,) = normalize_horizon_record(target("usdc-issuer"), call, page, 0, RECORDED_AT, {})
    assert isinstance(excluded, Exclusion) and excluded.reason == "UNSUPPORTED_OPERATION"
    assert excluded.chain_op == f"{chain.tx_hash}:{chain.operation_index}"


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"asset_code": "EURC"}, None),
        ({"asset_issuer": "GAYF33NNNMI2Z6VNRFXQ64D4E4SF77PM46NW3ZUZEEU5X7FCHAZCMHKU"}, None),
        ({"asset_type": "native"}, None),
        ({"type": "create_account"}, None),
        ({"amount": "20.00000001"}, "MALFORMED"),
        ({"amount": "-20.0000000"}, "MALFORMED"),
        ({"paging_token": "21543293963735040"}, "MALFORMED"),
        # an M address of another base account than ``to`` (increment 4: a consistent one
        # is kept, test_muxed_memo.py)
        (
            {"to_muxed": "MA7QYNF7SOWQ3GLR2BGMZEHXAVIRZA4KVWLTJJFC7MGXUA74P7UJUAAAAAAAAAAAACJUQ"},
            "MALFORMED",
        ),
        # a path payment record without its source asset fields cannot be read
        ({"type": "path_payment_strict_send"}, "MALFORMED"),
    ],
)
def test_horizon_record_rules(change: dict[str, str], expected: str | None) -> None:
    page, record = _page_and_record("usdc-issuer")
    record.update(change)
    result = _normalize("usdc-issuer", record, page)
    if expected is None:
        assert result is None
    else:
        assert isinstance(result, Exclusion) and result.reason == expected


def test_joined_transaction_must_match_operation() -> None:
    page, record = _page_and_record("usdc-issuer")
    record["transaction"]["ledger"] = 5015940
    result = _normalize("usdc-issuer", record, page)
    assert isinstance(result, Exclusion) and result.reason == "MALFORMED"


def _sac_event(sample: str) -> tuple[Page, dict[str, Any]]:
    _, rpc = clients(sample)
    start, end = SAMPLES[sample]
    page = rpc.get_events(USDC_SAC, start_ledger=start, end_ledger=end, cursor=None, limit=200)
    burn = next(
        e for e in page.document["result"]["events"] if decode_scval(e["topic"][0]) == "burn"
    )
    return page, copy.deepcopy(burn)


def _scval_symbol(text: str) -> str:
    raw = text.encode()
    return base64.b64encode(
        struct.pack(">iI", 15, len(raw)) + raw.ljust(-(-len(raw) // 4) * 4, b"\0")
    ).decode()


def _scval_amount_map(atoms: int) -> str:
    key = base64.b64decode(_scval_symbol("amount"))
    value = struct.pack(">iqQ", 10, atoms >> 64, atoms & (2**64 - 1))
    return base64.b64encode(struct.pack(">iiI", 17, 1, 1) + key + value).decode()


def _scval_contract(raw32: bytes) -> str:
    return base64.b64encode(struct.pack(">ii", 18, 1) + raw32).decode()


def test_real_sac_burn_maps_to_issuer_movement() -> None:
    page, event = _sac_event("usdc-issuer")
    result = normalize_sac_event(target("usdc-issuer"), USDC_SAC, event, 0, page, RECORDED_AT, {})
    assert isinstance(result, Observation)
    assert movement(result).to_address == ISSUER and movement(result).units.atoms == "200000000"


def _scval_address(kind: int, raw: bytes) -> str:
    return base64.b64encode(struct.pack(">ii", 18, kind) + raw).decode()


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            # A muxed account party (ScAddress type 2), which CAP-67 never puts in the topics:
            # decoded since increment 4, kept unresolved.
            lambda e: e["topic"].__setitem__(
                1, _scval_address(2, struct.pack(">Q", 7) + b"\x01" * 32)
            ),
            "MUXED_ACCOUNT",
        ),
        (lambda e: e["topic"].__setitem__(1, _scval_address(9, b"\x01" * 32)), "UNSUPPORTED_PARTY"),
        (lambda e: e["topic"].__setitem__(2, _scval_symbol("EURC:" + ISSUER)), "MALFORMED"),
        (lambda e: e.__setitem__("value", _scval_symbol("x")), "UNSUPPORTED_VALUE"),
        (
            # A pool is no clawback holder; it is never attributed to its providers.
            lambda e: (
                e["topic"].__setitem__(0, _scval_symbol("clawback"))
                or e["topic"].__setitem__(1, _scval_address(4, b"\x01" * 32))
            ),
            "UNSUPPORTED_PARTY",
        ),
        (
            # An unsuccessful call moved nothing to or from a typed party either.
            lambda e: (
                e["topic"].__setitem__(1, _scval_contract(b"\x07" * 32))
                or e.__setitem__("inSuccessfulContractCall", False)
            ),
            "UNSUCCESSFUL_EVENT",
        ),
        (
            # CAP-67 gives a clawback a plain i128 amount, never a map.
            lambda e: (
                e["topic"].__setitem__(0, _scval_symbol("clawback"))
                or e.__setitem__("value", _scval_amount_map(5))
            ),
            "UNSUPPORTED_VALUE",
        ),
    ],
    ids=[
        "muxed-party",
        "unknown-address-type",
        "other-asset-string",
        "non-i128-value",
        "clawback-pool-party",
        "unsuccessful-contract-movement",
        "clawback-map-value",
    ],
)
def test_sac_event_rules(mutate: Any, expected: str) -> None:
    page, event = _sac_event("usdc-issuer")
    mutate(event)
    result = normalize_sac_event(target("usdc-issuer"), USDC_SAC, event, 0, page, RECORDED_AT, {})
    # the issuer target is involved in every supply change, so each problem is in scope
    assert isinstance(result, Exclusion) and result.reason == expected


@pytest.mark.parametrize(
    ("kind", "raw", "effect_kind", "party"),
    [
        (1, b"\x07" * 32, "contract_transfer", "contract"),
        (4, b"\x01" * 32, "pool_transfer", "liquidity_pool"),
    ],
)
def test_a_burn_from_a_typed_party_is_a_typed_effect(
    kind: int, raw: bytes, effect_kind: str, party: str
) -> None:
    """Typed parties (derived from the real burn of usdc-issuer): a contract or pool
    party is a typed counterparty, never an account, and the effect is canonical (the
    issuer's side of a burn)."""
    page, event = _sac_event("usdc-issuer")
    event["topic"][1] = _scval_address(kind, raw)
    result = normalize_sac_event(target("usdc-issuer"), USDC_SAC, event, 0, page, RECORDED_AT, {})
    assert isinstance(result, Observation) and result.fact_type == "chain_effect"
    payload = effect(result)
    assert (payload.effect_kind, payload.account, payload.direction) == (
        effect_kind,
        ISSUER,
        "credit",
    )
    assert payload.counterparty is not None and payload.counterparty.kind == party
    assert result.source.record_key.endswith(f":sac:0:{effect_kind}")
    assert result.operation_ref is None


def test_a_claimable_balance_party_waits_for_the_balances_history() -> None:
    """A clawback of a claimable balance (derived from the real burn of usdc-issuer) is a
    clawback with a typed holder and no account: it needs the balance's history first."""
    page, event = _sac_event("usdc-issuer")
    event["topic"][0] = _scval_symbol("clawback")
    event["topic"][1] = base64.b64encode(struct.pack(">iii", 18, 3, 0) + b"\x07" * 32).decode()
    result = normalize_sac_event(target("usdc-issuer"), USDC_SAC, event, 0, page, RECORDED_AT, {})
    assert isinstance(result, PendingBalanceEffect)
    assert result.expected_operation == "clawback_claimable_balance"
    assert result.payload["account"] is None
    assert result.payload["holder"] == {"kind": "claimable_balance", "id": result.balance_id}
    assert result.balance_id.startswith("B") and len(result.balance_id) == 58


def test_events_of_other_contracts_and_non_movements_are_out_of_scope() -> None:
    page, event = _sac_event("usdc-issuer")
    other = dict(event, contractId="CAYPAQDKNWMHRATKU5DQ327VDHVRSIVK7UGVWT2A5SUZCUFTLUHXH2JA")
    assert (
        normalize_sac_event(target("usdc-issuer"), USDC_SAC, other, 0, page, RECORDED_AT, {})
        is None
    )
    approve = dict(event, topic=[_scval_symbol("approve"), *event["topic"][1:]])
    assert (
        normalize_sac_event(target("usdc-issuer"), USDC_SAC, approve, 0, page, RECORDED_AT, {})
        is None
    )


# ------------------------------------------------------------------- links


def test_only_explicit_execution_link_sets_operation_ref(tmp_path: Path) -> None:
    link = ExecutionLink(
        schema_version="1.0",
        network="stellar:testnet",
        tx_hash="776ea1954166a01f82042f55948a8ea53cae01bfea9a3d0f75a9617f5d8f8b81",
        operation_index=0,
        ordinal=0,
        operation_ref="OP-LINK-TEST",
        approval_ref="approval-test-only",
        recorded_at=RECORDED_AT,
    )
    store = IngestStore(tmp_path)
    classic, _ = ingest("usdc-gclcz", store, links=(link,))
    linked = [o for o in classic.appended if o.operation_ref is not None]
    assert [o.source.record_key for o in linked] == [f"{link.tx_hash}:0:0"]
    memo_tx = next(o for o in classic.appended if o.source.record_key.startswith("b0725d99"))
    assert memo_tx.operation_ref is None  # its memo id is data, never a link


# ------------------------------------------------------------------ SAC identity


def test_sac_contract_id_derivation_matches_horizon() -> None:
    assert sac_contract_id("USDC", ISSUER, TESTNET) == USDC_SAC
    horizon, _ = clients("usdc-issuer")
    assert resolve_sac_contract(target("usdc-issuer"), horizon) == USDC_SAC
    assert sac_contract_id("USDC", ISSUER, "Public Global Stellar Network ; September 2015") != (
        USDC_SAC
    )


def test_expected_sac_mismatch_is_rejected() -> None:
    data = target("usdc-issuer").model_dump(mode="json")
    data["expected_sac_contract_id"] = "CAYPAQDKNWMHRATKU5DQ327VDHVRSIVK7UGVWT2A5SUZCUFTLUHXH2JA"
    with pytest.raises(ValueError):
        resolve_sac_contract(ChainTarget.model_validate_json(json.dumps(data)))


@pytest.mark.parametrize(
    "b64",
    ["AAAAEQ==", "AAAADwAAAARidXJuAAAAAA==", "AAAAEgAAAAkAAAAA", "not base64!"],
    ids=["empty-map", "trailing-bytes", "unknown-address", "bad-base64"],
)
def test_xdr_rejects_unsupported_or_malformed(b64: str) -> None:
    with pytest.raises(XdrError):
        decode_scval(b64)


# ------------------------------------------------------------- availability


class _ScriptedClient:
    def __init__(self, responses: dict[str, Response]) -> None:
        self.responses = responses

    def request(self, method: str, url: str, body: bytes | None) -> Response:
        return self.responses[url]


def test_rpc_retention_error_is_data_unavailable() -> None:
    error = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": -32600,
            "message": "startLedger must be within the ledger range: 4906374 - 5027333",
        },
    }
    rpc = Rpc(
        "https://rpc.example",
        _ScriptedClient({"https://rpc.example": Response(200, json.dumps(error).encode())}),
    )
    with pytest.raises(DataUnavailable):
        rpc.get_events(USDC_SAC, start_ledger=4000000, end_ledger=4000010, cursor=None, limit=10)


def test_horizon_range_outside_history_is_data_unavailable(tmp_path: Path) -> None:
    horizon, _ = clients("usdc-issuer")
    with pytest.raises(DataUnavailable):
        ingest_horizon_payments(
            target("usdc-issuer"),
            horizon,
            IngestStore(tmp_path),
            start_ledger=100,
            end_ledger=200,
            history=(128, 5027000),
            recorded_at=RECORDED_AT,
        )


def test_http_retries_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []
    sleeps: list[int] = []

    def refuse(request: Any, timeout: int) -> Any:
        attempts.append(request.full_url)
        raise urllib.error.URLError("refused")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    client = UrllibClient(retries=2, backoff_ms=100, sleep_ms=sleeps.append)
    with pytest.raises(ChainUnavailable, match="3 attempts"):
        client.request("GET", "https://horizon.example/", None)
    assert len(attempts) == 3 and sleeps == [100, 200]
    with pytest.raises(ChainUnavailable, match="non-HTTPS"):
        client.request("GET", "http://horizon.example/", None)


def test_replay_refuses_unrecorded_requests(tmp_path: Path) -> None:
    with pytest.raises(ChainUnavailable, match="no recorded exchange"):
        ReplayClient(tmp_path).request("GET", "https://x.example/", None)
    assert len(request_key("GET", "https://x.example/", None)) == 64


# -------------------------------------------------------------- CLI and replay


def test_cli_ingest_from_recordings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "stellar",
            "ingest",
            "--target",
            str(TARGETS / "usdc-issuer.json"),
            "--start-ledger",
            "5015930",
            "--end-ledger",
            "5015945",
            "--store",
            str(tmp_path),
            "--sac",
            "--page-limit",
            "2",
            "--recorded-at",
            "2026-10-05T00:41:00+00:00",
            "--replay",
            str(RECORDINGS / "usdc-issuer"),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0 and "verified" in out and "duplicates=1" in out


def test_cli_reports_unavailable_data(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "stellar",
            "ingest",
            "--target",
            str(TARGETS / "usdc-issuer.json"),
            "--start-ledger",
            "1",
            "--end-ledger",
            "5",
            "--store",
            str(tmp_path),
            "--replay",
            str(RECORDINGS / "usdc-issuer"),
        ]
    )
    assert code == 3 and "DataUnavailable" in capsys.readouterr().err


def test_bundle_verifier_never_imports_network_code() -> None:
    package = Path(__file__).resolve().parents[2] / "src/invaria/bundle"
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not name.startswith(("invaria.stellar", "urllib", "socket", "http")), (
                    path,
                    name,
                )


class _CursorPagingRpc(Rpc):
    """RPC double built from real recorded events: first page bounded by start/end ledger,
    later pages by cursor only (like the real RPC), which can run past the end ledger."""

    def __init__(self, events: list[dict[str, Any]], latest: int) -> None:
        super().__init__("https://rpc.example", _ScriptedClient({}))
        self.events, self.latest, self.calls = events, latest, 0

    def get_events(
        self,
        contract_id: str,
        *,
        start_ledger: int | None,
        end_ledger: int,
        cursor: str | None,
        limit: int,
    ) -> Page:
        self.calls += 1
        if cursor is None:
            assert start_ledger is not None
            pool = [e for e in self.events if start_ledger <= e["ledger"] <= end_ledger]
        else:
            pool = [e for e in self.events if e["id"] > cursor]
        chunk = pool[:limit]
        document = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "events": chunk,
                "latestLedger": self.latest,
                # Like the real RPC: a short page reports how far it scanned.
                "cursor": chunk[-1]["id"]
                if len(chunk) == limit
                else f"{((end_ledger if cursor is None else self.latest) + 1 << 32) - 1:019d}"
                "-4294967295",
            },
        }
        raw = json.dumps(document, sort_keys=True).encode()
        return Page(
            f"rpc:getEvents#call={self.calls}", raw, hashlib.sha256(raw).hexdigest(), document
        )


def test_sac_cursor_pagination_stops_at_end_ledger(tmp_path: Path) -> None:
    horizon, rpc = clients("usdc-gclcz")
    start, end = SAMPLES["usdc-gclcz"]
    first = rpc.get_events(USDC_SAC, start_ledger=start, end_ledger=end, cursor=None, limit=200)
    events = copy.deepcopy(first.document["result"]["events"])
    involving = next(e for e in events if e["txHash"].startswith("776ea195"))
    later = dict(
        copy.deepcopy(involving), ledger=end + 1, id="9" * 19 + "-0000000000", txHash="ab" * 32
    )
    fake = _CursorPagingRpc([*events, later], latest=end + 100)
    store = IngestStore(tmp_path)
    check = verify_network(horizon, rpc, "stellar:testnet")
    ingest_horizon_payments(
        target("usdc-gclcz"),
        horizon,
        store,
        start_ledger=start,
        end_ledger=end,
        history=(check.horizon_elder_ledger, check.horizon_latest_ledger),
        recorded_at=RECORDED_AT,
        page_limit=2,
    )
    result = ingest_sac_events(
        target("usdc-gclcz"),
        fake,
        horizon,
        store,
        start_ledger=start,
        end_ledger=end,
        recorded_at=RECORDED_AT,
        page_limit=10,
    )
    assert result.complete and fake.calls > 2  # really paginated by cursor
    ledgers = [movement(o).chain.ledger for o in store.observations()]
    assert ledgers and max(ledgers) <= end
    assert all(not o.source.record_key.startswith("ab" * 32) for o in store.observations())
    checkpoint = store.get_checkpoint("testnet-usdc-gclcz", "rpc_sac_events")
    assert checkpoint is not None and checkpoint.records_received == len(events)


# ------------------------------------------------- own testnet issuance (DEMOA)

DEMOA_ISSUER = "GCGGXYAKBIOKOIMVSUTXPEHRLJJU7VB7ZIYYKUFXYIVZTTFIWKEKTBEP"
T1 = "87835238a663f716a6065a3f588000c4f2a92fa7d03a33e13a5b494d7fae99a2"
T2_FAILED = "29bca5708470cb6e912b1e97f3c55f64c038b74a9d3c266639ddd6bce6fb6126"
T3_DUPLICATE = "00fd0148b4dc7d334169406bdc0af3829ffe53c72396c563b0077da4300d34bf"


def _ingest_demoa(store: IngestStore) -> Any:
    from invaria.contracts.chain import ExecutionLinkSet

    links = parse_contract(
        ExecutionLinkSet, (FIXTURES / "links/demoa-own.json").read_text("utf-8")
    ).links
    horizon, rpc = clients("demoa-own")
    check = verify_network(horizon, rpc, "stellar:testnet")
    own = target("demoa-own")
    classic = ingest_horizon_payments(
        own,
        horizon,
        store,
        start_ledger=5027650,
        end_ledger=5027672,
        history=(check.horizon_elder_ledger, check.horizon_latest_ledger),
        recorded_at=RECORDED_AT,
        links=links,
        page_limit=2,
    )
    sac = ingest_sac_events(
        own,
        rpc,
        horizon,
        store,
        start_ledger=5027650,
        end_ledger=5027672,
        recorded_at=RECORDED_AT,
        links=links,
        page_limit=200,
    )
    return classic, sac


def test_own_issuance_links_only_the_approved_delivery(tmp_path: Path) -> None:
    store = IngestStore(tmp_path)
    classic, sac = _ingest_demoa(store)
    by_tx = {movement(o).chain.tx_hash: o for o in classic.appended}
    assert set(by_tx) == {T1, T2_FAILED, T3_DUPLICATE}
    delivery = by_tx[T1]
    assert delivery.operation_ref == "SUB-0001"  # explicit ExecutionLink
    assert movement(delivery).units.to_decimal_text() == "1000.0000000"
    assert movement(delivery).from_address == DEMOA_ISSUER
    assert by_tx[T3_DUPLICATE].operation_ref is None  # same memo, no link: not attributed
    failed = movement(by_tx[T2_FAILED])
    assert not failed.chain.tx_successful and by_tx[T2_FAILED].operation_ref is None
    # unified mint events (memo text as to_muxed_id string) are the same effects
    assert sac.complete and sac.appended == () and sac.duplicates == 2
    assert classic.coverage is not None and classic.coverage.is_gap_free


def test_undeployed_sac_is_accepted_but_a_different_published_id_is_not() -> None:
    horizon, _ = clients("demoa-own")
    own = target("demoa-own")
    page = horizon.asset(own.asset_code, own.asset_issuer)
    assert "contract_id" not in page.document["_embedded"]["records"][0]  # absent until deployed
    assert resolve_sac_contract(own, horizon) == own.expected_sac_contract_id

    class _Published(Horizon):
        def asset(self, code: str, issuer: str) -> Page:
            document = copy.deepcopy(page.document)
            document["_embedded"]["records"][0]["contract_id"] = USDC_SAC
            raw = json.dumps(document).encode()
            return Page(page.locator, raw, hashlib.sha256(raw).hexdigest(), document)

    with pytest.raises(ValueError, match="Horizon publishes SAC"):
        resolve_sac_contract(own, _Published(horizon.base_url, horizon.client))


# ------------------------------------------------- clawback

CLAWBACK_TX = "5202896d04a399164cea697423f2e02bea2f030bb2c5fa5200ef4e83c9e8b77f"
TESTUSB_HOLDER = "GBV6VEMHOOLOAHERL4JO35X3IDLR4HQIZ3BTUYXFCAO4Z526COZMUJMN"
TESTUSB_ISSUER = "GCWSNDOOTAGUCTR4X62RCH5UOAUVSWV5VL2AZSCWBK27DHL2IFQDPHVX"


def test_real_account_clawback_from_holder_and_issuer_is_one_withdrawal(tmp_path: Path) -> None:
    """Real testnet clawback of 10.123 TESTUSB from GBV6VEMH: Horizon does not list it among
    the holder's payments; the SAC event and the Classic operation agree, and the holder's
    and the issuer's targets record the same two effects, never four."""
    store = IngestStore(tmp_path)
    holder_classic, holder_sac = ingest("testusb-holder-clawback", store)
    _, issuer_sac = ingest("testusb-issuer-clawback", store)
    assert holder_classic.appended == () and holder_classic.coverage is not None
    assert holder_classic.coverage.is_gap_free  # absence from payments is no contradiction
    assert issuer_sac.conflicts == () and len(issuer_sac.appended) == 0
    effects = store.observations()
    assert sorted(o.source.record_key for o in effects) == [
        f"{CLAWBACK_TX}:0:classic:clawback",
        f"{CLAWBACK_TX}:0:sac:0:clawback",
    ]
    for observation in effects:
        payload = effect(observation)
        assert (payload.effect_kind, payload.account, payload.direction) == (
            "clawback",
            TESTUSB_HOLDER,
            "debit",
        )
        assert payload.units is not None and payload.units.atoms == "101230000"
        assert payload.clawback is not None and payload.clawback.issuer == TESTUSB_ISSUER
        assert payload.clawback.asset == f"TESTUSB:{TESTUSB_ISSUER}"
        assert observation.operation_ref is None
    for target_id in ("testnet-testusb-holder-clawback", "testnet-testusb-issuer-clawback"):
        check = store.current_correspondence()[(target_id, CLAWBACK_TX, 0)]
        assert check["status"] == "corroborated"
    assert [e.reason for e in holder_sac.exclusions] == ["EFFECT_NOT_A_MOVEMENT"]


def test_real_clawback_is_one_execution_per_target_and_jointly(tmp_path: Path) -> None:
    """The "two effects" of the real TESTUSB clawback are two representations (the Classic
    operation and the SAC event) of one execution. Ingested from the holder's target alone,
    from the issuer's alone, and both: one execution each time, never two withdrawals."""
    holder_store = IngestStore(tmp_path / "holder")
    issuer_store = IngestStore(tmp_path / "issuer")
    joint_store = IngestStore(tmp_path / "joint")
    ingest("testusb-holder-clawback", holder_store)
    ingest("testusb-issuer-clawback", issuer_store)
    ingest("testusb-holder-clawback", joint_store)
    ingest("testusb-issuer-clawback", joint_store)
    views = {
        "holder": holder_store.observations(),
        "issuer": issuer_store.observations(),
        "joint": joint_store.observations(),
        "union of the separate stores": [
            *holder_store.observations(),
            *issuer_store.observations(),
        ],
    }
    for name, observations in views.items():
        (execution,) = executions(observations)
        assert execution.status == "corroborated", name
        assert execution.key[:2] == ("stellar:testnet", CLAWBACK_TX), name
        assert execution.key[5] == f"stellar:testnet/account/{TESTUSB_HOLDER}", name
        assert [o.source.record_key for o in execution.representations] == [
            f"{CLAWBACK_TX}:0:classic:clawback",
            f"{CLAWBACK_TX}:0:sac:0:clawback",
        ], name
        units = {effect(o).units.atoms for o in execution.representations}  # type: ignore[union-attr]
        assert units == {"101230000"}, name  # 10.123 TESTUSB once, not 20.246
    # Each target keeps its own correspondence record of the same evidence.
    for store, target_id in (
        (holder_store, "testnet-testusb-holder-clawback"),
        (issuer_store, "testnet-testusb-issuer-clawback"),
    ):
        check = store.current_correspondence()[(target_id, CLAWBACK_TX, 0)]
        assert check["classic_effects"] == [f"{CLAWBACK_TX}:0:classic:clawback"]
        assert check["sac_legs"] == [f"{CLAWBACK_TX}:0:sac:0:clawback"]


def test_real_claimable_balance_clawback_and_creation_are_typed(tmp_path: Path) -> None:
    """Real gdICE ledger 5061607 (issuer target): op 0 claws back balance b978... (ScAddress
    type 3), op 1 mints a new balance a289... Increment 2 kept the clawback unsupported;
    increment 3 records both with the balance as a typed party, never as an account, and
    checks them against the balance's history (Horizon keeps it after the clawback)."""
    store = IngestStore(tmp_path)
    _, sac = ingest("gdice-issuer-cb-clawback", store)
    issuer = target("gdice-issuer-cb-clawback").asset_issuer
    horizon, _ = clients("gdice-issuer-cb-clawback")
    clawback, created = sorted(store.observations(), key=lambda o: o.source.record_key)
    taken, made = effect(clawback), effect(created)
    assert (taken.effect_kind, taken.account, taken.direction) == ("clawback", None, "debit")
    assert taken.holder is not None and taken.holder.kind == "claimable_balance"
    assert taken.counterparty == ChainParty(kind="account", id=issuer)  # the supply side
    assert claimable_balance_hex(str(taken.holder.id)).startswith("00000000b978c53b7fb364ed")
    assert (made.effect_kind, made.account, made.direction) == (
        "claimable_balance_created",
        issuer,  # minted into the balance: the issuer's side
        "debit",
    )
    assert made.counterparty is not None and made.counterparty.kind == "claimable_balance"
    for payload in (taken, made):
        balance = payload.claimable_balance
        assert balance is not None
        history = horizon.claimable_balance_operations(claimable_balance_hex(balance.balance_id))
        (creation,) = [
            r
            for r in history.document["_embedded"]["records"]
            if r["type"] == "create_claimable_balance"
        ]
        assert balance.creator == ChainParty(kind="account", id=creation["source_account"])
        assert balance.claimants == [c["destination"] for c in creation["claimants"]]
        assert issuer not in balance.claimants  # claimants are possible recipients only
    assert taken.claimable_balance is not None
    assert taken.claimable_balance.operation_type == "clawback_claimable_balance"
    assert made.claimable_balance is not None
    assert made.claimable_balance.operation_type == "create_claimable_balance"
    checks = sorted(store.correspondence(), key=lambda c: c["op_index"])
    assert [(c["kind"], c["status"], c["participants"]) for c in checks] == [
        ("claimable_balance", "corroborated", "resolved")
    ] * 2
    assert sac.coverage is not None and sac.coverage.records_quarantined == 0
    assert all(o.operation_ref is None for o in store.observations())


def test_real_claimable_balance_seen_by_creator_and_claimant(tmp_path: Path) -> None:
    """Real USDC claimable balance 53cd2191...: GDES54WT creates it (0.5 USDC, claimants
    GAPNRJHI and GDES54WT) and GAPNRJHI claims it. Each target records its own canonical
    effect; the balance is the same typed party in both, and nobody is presumed to have
    received the other balances GDES54WT created."""
    store = IngestStore(tmp_path)
    ingest("usdc-gdes54wt-cb-create", store)
    ingest("usdc-gapnrjhi-cb-claim", store)
    creator = target("usdc-gdes54wt-cb-create").account
    claimant = target("usdc-gapnrjhi-cb-claim").account
    effects = {o.source.record_key: effect(o) for o in store.observations()}
    created = [p for p in effects.values() if p.effect_kind == "claimable_balance_created"]
    (claimed,) = [p for p in effects.values() if p.effect_kind == "claimable_balance_claimed"]
    assert len(created) == 4 and all(
        (p.account, p.direction, p.units.atoms if p.units else None)
        == (creator, "debit", "5000000")
        for p in created
    )
    assert (claimed.account, claimed.direction) == (claimant, "credit")
    assert claimed.counterparty is not None
    # The claimed balance is one of the four GDES54WT created (Horizon id 53cd2191...).
    (same,) = [p for p in created if p.counterparty == claimed.counterparty]
    assert claimable_balance_hex(str(claimed.counterparty.id)).startswith("0000000053cd2191")
    assert claimed.claimable_balance is not None and same.claimable_balance is not None
    assert claimed.claimable_balance.creator == ChainParty(kind="account", id=creator)
    assert claimed.claimable_balance.claimants == same.claimable_balance.claimants
    assert claimant in (claimed.claimable_balance.claimants or [])
    assert claimed.claimable_balance.operation_source == claimant
    # Identity: kind and id on the network, never the account of either side.
    assert party_identity("stellar:testnet", claimed.counterparty).startswith(
        "stellar:testnet/claimable_balance/B"
    )
    statuses = {c["status"] for c in store.correspondence()}
    assert statuses == {"corroborated"}


def test_real_contract_movements_keep_the_contract_as_a_party(tmp_path: Path) -> None:
    """Real USDC transfers between GDBXA45U and contract CAYPAQDK (invoke_host_function,
    listed by Horizon): typed contract effects, the contract never an account and its
    owner never inferred. Horizon's record of each call stays quarantined as the Horizon
    run left it, so the coverage still does not show these operations resolved."""
    store = IngestStore(tmp_path)
    classic, sac = ingest("usdc-gdbxa45u-contract", store)
    account = target("usdc-gdbxa45u-contract").account
    contract = "CAYPAQDKNWMHRATKU5DQ327VDHVRSIVK7UGVWT2A5SUZCUFTLUHXH2JA"
    effects = [effect(o) for o in store.observations()]
    assert len(effects) == 6 and {p.effect_kind for p in effects} == {"contract_transfer"}
    assert all(p.account == account for p in effects)
    assert all(p.counterparty == ChainParty(kind="contract", id=contract) for p in effects)
    assert {p.direction for p in effects} == {"debit", "credit"}
    assert {e.reason for e in sac.exclusions} == {"EFFECT_NOT_A_MOVEMENT"}
    assert not any(e.quarantine for e in sac.exclusions)
    assert {e.reason for e in classic.exclusions} == {"UNSUPPORTED_OPERATION"}
    assert sac.coverage is not None and sac.coverage.quarantined_records is not None
    assert sac.coverage.records_quarantined == len(classic.exclusions) == 3
    assert all(
        r.addresses is not None and {account, contract} <= set(r.addresses)
        for r in sac.coverage.quarantined_records
    )


def test_real_failed_clawback_operation_withdraws_nothing() -> None:
    """Real FAILED Classic clawback (Horizon shows 50 TESTUSB requested): no effect."""
    client = ReplayClient(RECORDINGS / "testusb-failed-clawback")
    horizon = Horizon(Endpoints.resolve(None, None).horizon_url, client)
    page = horizon.transaction_operations(
        "4d4db33e234ee809816c992c23f18bd46bd57b962dc3c25721791fc96466de88"
    )
    (record,) = page.document["_embedded"]["records"]
    assert (record["type"], record["transaction_successful"], record["amount"]) == (
        "clawback",
        False,
        "50.0000000",
    )
    testusb = target("testusb-holder-clawback")
    assert classic_clawback(testusb, record, page, "x", RECORDED_AT) is None
    executed = dict(record, transaction_successful=True)
    done = classic_clawback(testusb, executed, page, "x", RECORDED_AT)
    assert done is not None and effect(done).units.atoms == "500000000"  # type: ignore[union-attr]


def test_real_sac_admin_clawback_is_native_and_scoped_on_both_paths(tmp_path: Path) -> None:
    """Real clawback by contract call (SAC admin): Horizon lists the invoke_host_function
    among the holder's payments with a ``clawback`` balance change and no ``to``. Both runs
    keep the holder and the issuer as its parties; there is no Classic clawback to compare."""
    store = IngestStore(tmp_path)
    classic, sac = ingest("p0b41b8-holder-admin-clawback", store)
    holder = "GA7UOU2LWCN2THOL3GDY3HGUBJUAAEA2MCODHFOUCUOSYGQSLVXVAID4"
    issuer = "GDTWQZV6BDG5QT6XAJ5PFUNPPJD7FJTML4YXYFKW7G3CC5BIN3CVJSRS"
    (listed,) = classic.exclusions
    assert listed.reason == "UNSUPPORTED_OPERATION"
    assert listed.addresses is not None and set(listed.addresses) == {holder, issuer}
    (withdrawal,) = store.observations()
    payload = effect(withdrawal)
    assert (payload.effect_kind, payload.representation, payload.account) == (
        "clawback",
        "sac",
        holder,
    )
    (check,) = store.correspondence()
    assert check["status"] == "sac_native"
    assert sac.coverage is not None and sac.coverage.quarantined_records is not None
    assert all(
        r.addresses is not None and {holder, issuer} <= set(r.addresses)
        for r in sac.coverage.quarantined_records
    )
    # Both records identify the clawback (the SAC event; Horizon's balance change
    # of type clawback, the only change of the asset), on the same ledger operation; only
    # the Classic correspondence is open.
    chain_op = f"{payload.chain.tx_hash}:{payload.chain.operation_index}"
    assert {(r.nature, r.chain_operation) for r in sac.coverage.quarantined_records} == {
        ("clawback_identified", chain_op)
    }
    assert len(sac.coverage.quarantined_records) == 2


def test_horizons_reading_of_a_call_identifies_a_clawback_only_when_that_is_all_it_did() -> None:
    """The real admin clawback call moves two assets: USDC (``transfer``, another asset) and
    P0B41B8 (``clawback``). Its only change of the target asset is a clawback with both
    sides known: ``clawback_identified``. A DERIVED copy that adds a transfer of the target
    asset in the same call stays unresolved."""
    sample = "p0b41b8-holder-admin-clawback"
    horizon, _ = clients(sample)
    start, _ = SAMPLES[sample]
    page = horizon.account_payments(target(sample).account, str(start << 32), 2)
    record = copy.deepcopy(page.document["_embedded"]["records"][0])
    assert record["type"] == "invoke_host_function"
    (excluded,) = normalize_horizon_record(target(sample), record, page, 0, RECORDED_AT, {})
    assert isinstance(excluded, Exclusion) and excluded.nature == "clawback_identified"
    clawback = next(c for c in record["asset_balance_changes"] if c["type"] == "clawback")
    record["asset_balance_changes"].append(
        {**clawback, "type": "transfer", "to": target(sample).asset_issuer}
    )
    (mixed,) = normalize_horizon_record(target(sample), record, page, 0, RECORDED_AT, {})
    assert isinstance(mixed, Exclusion) and mixed.nature is None
    assert mixed.reason == excluded.reason == "UNSUPPORTED_OPERATION"


def test_copies_of_one_record_take_the_least_resolved_nature() -> None:
    assert least_resolved({"clawback_identified", "movement_identified"}) == "movement_identified"
    assert least_resolved({"clawback_identified", "contradictory"}) == "contradictory"
    assert least_resolved({"contradictory", "unresolved"}) == "unresolved"
    assert least_resolved({"clawback_identified"}) == "clawback_identified"


FEE_BUMP_OUTER = "415723e786ff24d318b7fa2942caa74344658323a57e17d62398fe4258b2dd07"
FEE_BUMP_INNER = "5775359727749907801b0ae760a15b1b2937cfccc0cdb0d1a1819b1454dac4d0"


def test_real_fee_bump_is_named_by_its_outer_hash_everywhere(tmp_path: Path) -> None:
    """Real fee bump payment of 2.2367085 USDC to GB3NNNLA (usdc-feebump-payment): RPC's
    event, Horizon's payment and Horizon's operations all name the outer hash, even when
    the operation is looked up by the inner one, so both paths share one effect key. Only
    the transaction resource looked up by the inner hash reports it as its ``hash``."""
    store = IngestStore(tmp_path)
    classic, sac = ingest("usdc-feebump-payment", store)
    (payment,) = classic.appended
    assert payment.source.record_key == f"{FEE_BUMP_OUTER}:0:0"
    assert sac.appended == () and sac.duplicates == 1 and sac.conflicts == ()
    horizon, _ = clients("usdc-feebump-payment")
    for looked_up in (FEE_BUMP_OUTER, FEE_BUMP_INNER):
        operations = horizon.transaction_operations(looked_up).document["_embedded"]["records"]
        assert [o["transaction_hash"] for o in operations] == [FEE_BUMP_OUTER]
        transaction = horizon.get(f"/transactions/{looked_up}").document
        assert transaction["hash"] == looked_up
        assert transaction["fee_bump_transaction"]["hash"] == FEE_BUMP_OUTER
        assert transaction["inner_transaction"]["hash"] == FEE_BUMP_INNER


def test_a_classic_operation_naming_another_hash_is_never_paired() -> None:
    """DERIVED from the real failed clawback response (no real fee bump clawback exists in
    the samples): a Classic record naming another transaction hash than the one looked up
    is never paired with the event, so no correspondence is fabricated."""
    client = ReplayClient(RECORDINGS / "testusb-failed-clawback")
    horizon = Horizon(Endpoints.resolve(None, None).horizon_url, client)
    looked_up = "4d4db33e234ee809816c992c23f18bd46bd57b962dc3c25721791fc96466de88"
    page = horizon.transaction_operations(looked_up)
    (record,) = page.document["_embedded"]["records"]
    executed = dict(record, transaction_successful=True)
    testusb = target("testusb-holder-clawback")
    done = classic_clawback(testusb, executed, page, "x", RECORDED_AT, tx_hash=looked_up)
    assert done is not None and effect(done).chain.tx_hash == looked_up
    with pytest.raises(ValueError, match="another transaction hash"):
        classic_clawback(testusb, executed, page, "x", RECORDED_AT, tx_hash="ab" * 32)
