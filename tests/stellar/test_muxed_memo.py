"""Muxed accounts and memos in the Stellar adapter.

The derived cases come from the SYNTHETIC recordings in ``tests/fixtures/stellar/synthetic/
muxed`` (``build_muxed.py``; nothing there exists on any network). The real samples are
``usdc-gbiujq-muxed`` (a payment to the muxed sub-account 310350723), ``usdc-gclcz`` (a memo
id that the SAC event carries as the same u64) and ``demoa-own`` (a text memo, SUB-0001).
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from invaria.contracts.base import parse_contract
from invaria.contracts.chain import ChainTarget, ExecutionLink
from invaria.contracts.observation import (
    Observation,
    TokenMovementPayload,
    TransactionMemo,
    movement_receiver,
    movement_sender,
)
from invaria.contracts.stellar import U64_MAX, encode_muxed_account
from invaria.stellar.adapter import (
    RunResult,
    expected_datum,
    ingest_horizon_payments,
    ingest_sac_events,
    normalize_sac_leg,
    path_operation_entry,
)
from invaria.stellar.http import ReplayClient
from invaria.stellar.sources import Horizon, Page, Rpc, verify_network
from invaria.stellar.store import IngestStore

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
SYNTHETIC = ROOT / "stellar/synthetic/muxed"
STELLAR = ROOT / "stellar"
START, END = 90000500, 90000530
RECORDED_AT = datetime(2026, 10, 7, 16, 0, tzinfo=UTC)
HORIZON_URL, RPC_URL = "https://horizon.synthetic.invalid", "https://rpc.synthetic.invalid"
TARGET = parse_contract(ChainTarget, (SYNTHETIC / "target.json").read_text("utf-8"))
W, ISSUER = TARGET.account, TARGET.asset_issuer


def _tx(label: str) -> str:
    return hashlib.sha256(f"invaria:synthetic:stellar:tx:muxed:{label}".encode()).hexdigest()


def _ingest(
    store: IngestStore,
    target: ChainTarget = TARGET,
    recordings: Path = SYNTHETIC / "recordings",
    span: tuple[int, int] = (START, END),
    links: tuple[ExecutionLink, ...] = (),
    urls: tuple[str, str] = (HORIZON_URL, RPC_URL),
) -> tuple[RunResult, RunResult]:
    client = ReplayClient(recordings)
    horizon, rpc = Horizon(urls[0], client), Rpc(urls[1], client)
    check = verify_network(horizon, rpc, "stellar:testnet")
    classic = ingest_horizon_payments(
        target,
        horizon,
        store,
        start_ledger=span[0],
        end_ledger=span[1],
        history=(check.horizon_elder_ledger, check.horizon_latest_ledger),
        recorded_at=RECORDED_AT,
        links=links,
        page_limit=3 if target.synthetic else 2,
    )
    sac = ingest_sac_events(
        target,
        rpc,
        horizon,
        store,
        start_ledger=span[0],
        end_ledger=span[1],
        recorded_at=RECORDED_AT,
        links=links,
    )
    return classic, sac


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> tuple[IngestStore, RunResult, RunResult]:
    store = IngestStore(tmp_path_factory.mktemp("muxed"))
    classic, sac = _ingest(store)
    return store, classic, sac


def _movement(store: IngestStore, label: str) -> tuple[Observation, TokenMovementPayload]:
    (o,) = [o for o in store.observations() if o.source.record_key == f"{_tx(label)}:0:0"]
    assert isinstance(o.payload, TokenMovementPayload)
    return o, o.payload


def _movement_copies(store: IngestStore, key: str) -> tuple[Observation, TokenMovementPayload]:
    """The Classic copy of a record key that also has a conflicting SAC copy."""
    (o,) = [
        o
        for o in store.observations()
        if o.source.record_key == key and o.provenance.mapping_ref.startswith("stellar-classic")
    ]
    assert isinstance(o.payload, TokenMovementPayload)
    return o, o.payload


def _text(raw: bytes) -> TransactionMemo:
    return TransactionMemo(memo_type="text", value=base64.b64encode(raw).decode())


# ------------------------------------------------------------------ the fixture


def test_the_fixture_is_marked_synthetic() -> None:
    assert TARGET.synthetic and START > 50_000_000
    for meta in (SYNTHETIC / "recordings").glob("*.json"):
        assert "SYNTHETIC" in json.loads(meta.read_text("utf-8"))["synthetic"]
    for body in (SYNTHETIC / "recordings").glob("*.body"):
        assert "_synthetic" in json.loads(body.read_bytes())


# ------------------------------------------------- Classic: exact ids and memos


@pytest.mark.parametrize(
    ("label", "from_id", "to_id", "memo"),
    [
        ("to-zero", None, "0", TransactionMemo(memo_type="none", value=None)),
        ("to-max", None, str(U64_MAX), TransactionMemo(memo_type="none", value=None)),
        ("from-muxed", "77", None, _text(b"order 77")),
        ("memo-id", None, None, TransactionMemo(memo_type="id", value="42")),
        ("to-42-with-memo", None, "42", _text(b"ignored")),
        ("memo-hash", None, None, TransactionMemo(memo_type="hash", value=bytes(range(32)).hex())),
        (
            "memo-return",
            None,
            None,
            TransactionMemo(memo_type="return", value=bytes(range(32, 64)).hex()),
        ),
        ("memo-empty", None, None, TransactionMemo(memo_type="text", value="")),
        ("burn-memo", "9", None, _text(b"redeem")),
        ("memo-not-utf8", None, None, _text(b"\xffok")),  # its exact bytes, not UTF-8
    ],
)
def test_a_classic_payment_keeps_its_muxed_ids_and_memo_exactly(
    run: tuple[IngestStore, RunResult, RunResult],
    label: str,
    from_id: str | None,
    to_id: str | None,
    memo: TransactionMemo,
) -> None:
    store, _, _ = run
    o, payload = _movement(store, label)
    assert (payload.from_muxed_id, payload.to_muxed_id, payload.memo) == (from_id, to_id, memo)
    assert o.provenance.mapping_ref == "stellar-classic-payment@1.1.0"
    assert o.operation_ref is None  # a memo never links anything
    # The balance that moves is the base account's; the sub-account is named apart.
    assert payload.to_address == (ISSUER if label == "burn-memo" else W)


def test_zero_and_the_largest_u64_survive_storage_as_text(
    run: tuple[IngestStore, RunResult, RunResult],
) -> None:
    store, _, _ = run
    lines = (store.root / "observations.jsonl").read_text("utf-8")
    assert f'"to_muxed_id":"{U64_MAX}"' in lines.replace(" ", "")
    assert '"to_muxed_id":"0"' in lines.replace(" ", "")
    _, zero = _movement(store, "to-zero")
    _, none = _movement(store, "memo-id")
    assert zero.to_muxed_id == "0" and none.to_muxed_id is None  # zero is not absent
    assert movement_receiver(zero) == encode_muxed_account(W, 0) != movement_receiver(none) == W
    _, top = _movement(store, "to-max")
    assert movement_receiver(top) == encode_muxed_account(W, U64_MAX)


def test_a_memo_id_is_never_the_sub_account_of_equal_number(
    run: tuple[IngestStore, RunResult, RunResult],
) -> None:
    """The memo id 42 and the sub-account 42 are different things."""
    store, _, _ = run
    _, memo = _movement(store, "memo-id")
    _, muxed = _movement(store, "to-42-with-memo")
    assert memo.memo == TransactionMemo(memo_type="id", value="42") and memo.to_muxed_id is None
    assert muxed.to_muxed_id == "42" and movement_receiver(memo) != movement_receiver(muxed)
    _, sender = _movement(store, "from-muxed")
    assert movement_sender(sender) == encode_muxed_account(sender.from_address, 77)


# ------------------------------------------------ SAC: corroboration, conflicts


def test_sac_events_corroborate_without_building_records(
    run: tuple[IngestStore, RunResult, RunResult],
) -> None:
    """Ten events agree with their Classic payment under CAP-67 (the non-UTF-8 text memo
    included, compared byte for byte): each corroborates it and adds nothing (no SAC copy is
    built, so nothing the event does not state is invented)."""
    store, classic, sac = run
    assert sac.duplicates == 10
    assert len(classic.appended) == 15
    assert len(store.observations()) == 16  # and the amount conflict's SAC copy


def test_an_agreeing_datum_never_hides_another_amount(
    run: tuple[IngestStore, RunResult, RunResult],
) -> None:
    """amount-mismatch: the memo agrees but the event moves 2 where Classic says 1. The
    event's own reading (without memo, which it does not state) is kept beside the Classic
    record: a visible source conflict, as for any payment; nothing is corroborated."""
    store, _, sac = run
    key = f"{_tx('amount-mismatch')}:0:0"
    assert sac.conflicts == (key,)
    (sac_copy,) = sac.appended
    assert sac_copy.source.record_key == key and isinstance(sac_copy.payload, TokenMovementPayload)
    assert sac_copy.payload.units.atoms == "20000000" and sac_copy.payload.memo is None
    _, classic = _movement_copies(store, key)
    assert classic.units.atoms == "10000000" and classic.memo == _text(b"X")


def test_contradicting_events_are_quarantined_and_nothing_is_chosen(
    run: tuple[IngestStore, RunResult, RunResult],
) -> None:
    store, _, sac = run
    by_tx = {e.chain_op: e for e in sac.exclusions}
    for label, said in (("bad-muxed-id", "6"), ("bad-memo", "Qg=="), ("missing-datum", "None")):
        e = by_tx[f"{_tx(label)}:0"]
        assert (e.reason, e.nature, e.quarantine) == (
            "CORRESPONDENCE_CONFLICT",
            "contradictory",
            True,
        )
        assert said in e.detail
    _, five = _movement(store, "bad-muxed-id")  # the Classic record stays as it was
    assert five.to_muxed_id == "5"
    named = by_tx[f"{_tx('bad-muxed-id')}:0"].addresses or ()
    # Both readings: the Classic sub-account 5 and the event's 6 (Stellar review H2).
    assert encode_muxed_account(W, 5) in named and encode_muxed_account(W, 6) in named


def test_another_amount_with_a_u64_is_quarantined_not_copied(
    run: tuple[IngestStore, RunResult, RunResult],
) -> None:
    """A copy built from the event would say "receiver not muxed", which the event does not
    state (Stellar review H4): the record is quarantined, naming both readings."""
    store, _, sac = run
    key = f"{_tx('amount-mismatch-muxed')}:0:0"
    assert [
        o.provenance.mapping_ref for o in store.observations() if o.source.record_key == key
    ] == ["stellar-classic-payment@1.1.0"]
    (e,) = [e for e in sac.exclusions if e.chain_op == f"{_tx('amount-mismatch-muxed')}:0"]
    assert (e.reason, e.nature) == ("CORRESPONDENCE_CONFLICT", "contradictory")
    assert encode_muxed_account(W, 8) in (e.addresses or ())


def test_a_u64_datum_without_a_classic_record_stays_unresolved(
    run: tuple[IngestStore, RunResult, RunResult],
) -> None:
    store, _, sac = run
    (e,) = [e for e in sac.exclusions if e.chain_op == f"{_tx('call-u64')}:0"]
    assert e.reason == "MUXED_ACCOUNT" and e.quarantine and e.nature is None
    assert "u64 123" in e.detail and W in (e.addresses or ())
    # The sub-account the u64 would name is a candidate party, never an attribution: an
    # account approved only by that M address is not ruled out (Stellar review H1).
    assert encode_muxed_account(W, 123) in (e.addresses or ())
    assert not [o for o in store.observations() if o.source.record_key.startswith(_tx("call-u64"))]


def test_the_certificate_lists_each_quarantined_record_with_its_nature(
    run: tuple[IngestStore, RunResult, RunResult],
) -> None:
    _, _, sac = run
    assert sac.coverage is not None and sac.coverage.quarantined_records is not None
    natures = {r.chain_operation: r.nature for r in sac.coverage.quarantined_records}
    assert natures == {
        f"{_tx('bad-muxed-id')}:0": "contradictory",
        f"{_tx('bad-memo')}:0": "contradictory",
        f"{_tx('missing-datum')}:0": "contradictory",
        f"{_tx('amount-mismatch-muxed')}:0": "contradictory",
        f"{_tx('call-u64')}:0": "unresolved",
    }
    assert sac.coverage.records_quarantined == 5


@pytest.mark.parametrize(
    ("transaction", "expected"),
    [
        ({"memo_type": "text"}, TransactionMemo(memo_type="text", value="")),  # omitempty
        ({"memo_type": "text", "memo": "abc"}, None),  # bytes unknown: not read
        ({"memo_type": "text", "memo_bytes": "!!"}, None),
        ({"memo_type": "id", "memo": "-1"}, None),
        ({"memo_type": "hash", "memo": "AAAA"}, None),
        ({"memo_type": "future"}, None),
        ({}, None),
    ],
)
def test_an_unreadable_memo_is_not_read_and_never_drops_the_payment(
    transaction: dict[str, str], expected: TransactionMemo | None
) -> None:
    from invaria.stellar.adapter import transaction_memo

    record = {"transaction": {"hash": "ab" * 32, **transaction}} if transaction else {}
    memo = transaction_memo(record)
    assert (None if memo is None else TransactionMemo.model_validate(memo)) == expected


@pytest.mark.parametrize(
    ("head", "fields", "expected"),
    [
        ("transfer", {"to_muxed_id": "5", "memo": {"memo_type": "id", "value": "9"}}, ("u64", "5")),
        ("transfer", {"memo": {"memo_type": "id", "value": "9"}}, ("u64", "9")),
        ("mint", {"memo": {"memo_type": "text", "value": "QQ=="}}, ("string", "QQ==")),
        ("transfer", {"memo": {"memo_type": "hash", "value": "ab" * 32}}, ("bytes", "ab" * 32)),
        ("transfer", {"memo": {"memo_type": "return", "value": "cd" * 32}}, ("bytes", "cd" * 32)),
        ("transfer", {"memo": {"memo_type": "none", "value": None}}, None),
        ("burn", {"to_muxed_id": "5", "memo": {"memo_type": "id", "value": "9"}}, None),
        ("transfer", {}, "unknown"),
    ],
)
def test_cap67_datum_of_a_classic_payment(
    head: str, fields: dict[str, object], expected: object
) -> None:
    payload = TokenMovementPayload.model_validate(
        {
            "payload_type": "token_movement",
            "from_address": W,
            "to_address": W,
            "units": {"atoms": "1", "scale": 7, "unit": "SYNUSD"},
            "chain": {
                "network": "stellar:testnet",
                "ledger": 1,
                "tx_hash": "ab" * 32,
                "operation_index": 0,
                "tx_successful": True,
            },
            **fields,
        }
    )
    assert expected_datum(head, payload) == expected


# --------------------------------------------------------- links and memos


def test_only_an_execution_link_links_a_muxed_payment(tmp_path: Path) -> None:
    """The memo text \"order 77\" names nothing; the explicit link does."""
    link = ExecutionLink.model_validate_json(
        json.dumps(
            {
                "schema_version": "1.0",
                "network": "stellar:testnet",
                "tx_hash": _tx("to-zero"),
                "operation_index": 0,
                "ordinal": 0,
                "operation_ref": "SUB-0077",
                "approval_ref": "synthetic-approval",
                "recorded_at": "2026-10-07T15:59:00Z",
            }
        )
    )
    store = IngestStore(tmp_path)
    _ingest(store, links=(link,))
    linked = {o.source.record_key: o.operation_ref for o in store.observations()}
    assert linked.pop(f"{_tx('to-zero')}:0:0") == "SUB-0077"
    assert set(linked.values()) == {None}


# ------------------------------------------------------------- real samples


def test_real_payment_to_a_muxed_sub_account(tmp_path: Path) -> None:
    """usdc-gbiujq-muxed: a real testnet USDC payment to MBIUJQ…, sub-account 310350723.
    Classic keeps the id; the SAC event's u64 to_muxed_id is that id, so it corroborates the
    record (until increment 4 the payment was quarantined as MUXED_ACCOUNT)."""
    target = parse_contract(
        ChainTarget, (STELLAR / "targets/usdc-gbiujq-muxed.json").read_text("utf-8")
    )
    store = IngestStore(tmp_path)
    classic, sac = _ingest(
        store,
        target,
        STELLAR / "recordings/usdc-gbiujq-muxed",
        (5072600, 5072610),
        urls=("https://horizon-testnet.stellar.org", "https://soroban-testnet.stellar.org"),
    )
    (o,) = classic.appended
    payload = o.payload
    assert isinstance(payload, TokenMovementPayload)
    assert payload.to_address == target.account and payload.to_muxed_id == "310350723"
    assert payload.memo == TransactionMemo(memo_type="none", value=None)
    # Horizon's own M address equals the encoding of the base account and the id.
    raw = json.loads((store.root / "pages" / f"{o.provenance.raw_sha256}.json").read_bytes())
    (record,) = [
        r for r in raw["_embedded"]["records"] if r["transaction_hash"] == payload.chain.tx_hash
    ]
    assert movement_receiver(payload) == record["to_muxed"]
    assert (sac.appended, sac.duplicates, sac.exclusions) == ((), 1, ())
    assert classic.coverage is not None and sac.coverage is not None
    assert (classic.coverage.records_quarantined, sac.coverage.records_quarantined) == (0, 0)


def test_path_payment_effects_keep_the_transaction_memo(tmp_path: Path) -> None:
    """usdc-gb4mm: a real strict_receive whose transaction carries the text memo "mint";
    the Classic path effects keep it in their transaction outcome (mapping 1.1.0)."""
    from invaria.contracts.observation import ChainEffectPayload

    target = parse_contract(ChainTarget, (STELLAR / "targets/usdc-gb4mm.json").read_text("utf-8"))
    store = IngestStore(tmp_path)
    _ingest(
        store,
        target,
        STELLAR / "recordings/usdc-gb4mm",
        (5000475, 5000490),  # as recorded (test_adapter.SAMPLES)
        urls=("https://horizon-testnet.stellar.org", "https://soroban-testnet.stellar.org"),
    )
    classic = [
        o.payload
        for o in store.observations()
        if isinstance(o.payload, ChainEffectPayload) and o.payload.representation == "classic"
    ]
    assert classic and all(
        p.transaction is not None and p.transaction.memo == _text(b"mint") for p in classic
    )


def test_the_demo_memo_names_the_order_but_links_nothing() -> None:
    """DEMOA's T1 and T3 carry the text memo SUB-0001, the order they were sent for: kept
    exactly, and never an ExecutionLink (T3 stays unlinked in the demo)."""
    from invaria.vertical_testnet import run_testnet_vertical

    runs = {
        r.scenario.scenario_id: r
        for r in run_testnet_vertical(
            ROOT / "corpus/subscription-testnet-1.3.0",
            STELLAR,
            ROOT / "corpus/subscription-synthetic/mappings",
        )
    }
    run = runs["TN-NO-LINK"]
    movements = [
        o
        for i in run.snapshot.observation_ids
        if isinstance((o := run.inputs.observations[i]).payload, TokenMovementPayload)
    ]
    memos = {o.payload.memo for o in movements if isinstance(o.payload, TokenMovementPayload)}
    assert _text(b"SUB-0001") in memos
    assert all(o.operation_ref is None for o in movements)
    assert run.evaluation.result.result == "UNKNOWN"


# ------------------------------------------------------------ path payment legs


DESTINATION = "GA7QYNF7SOWQ3GLR2BGMZEHXAVIRZA4KVWLTJJFC7MGXUA74P7UJVSGZ"


def _leg(datum_u64: int, receiver: str, tx: str) -> tuple[Page, dict]:  # type: ignore[type-arg]
    from build_muxed import movement_map, sc_u64  # type: ignore[import-not-found]
    from build_path_payments import (  # type: ignore[import-not-found]
        sc_account,
        sc_string,
        sc_symbol,
    )

    event = {
        "type": "contract",
        "ledger": 90000520,
        "ledgerClosedAt": "2026-10-07T15:01:40Z",
        "contractId": TARGET.expected_sac_contract_id,
        "id": "0386547645050519553-0000000001",
        "operationIndex": 0,
        "txHash": tx,
        "inSuccessfulContractCall": True,
        "topic": [
            sc_symbol("transfer"),
            sc_account(W),
            sc_account(receiver),
            sc_string(f"SYNUSD:{ISSUER}"),
        ],
        "value": movement_map(5, sc_u64(datum_u64)),
    }
    raw = json.dumps({"_synthetic": "test", "result": {"events": [event]}}).encode()
    return Page("synthetic#leg", raw, hashlib.sha256(raw).hexdigest(), json.loads(raw)), event


def test_a_leg_u64_is_accepted_only_as_the_operations_memo_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path payment leg (a chain effect, never a movement) whose u64 to_muxed_id the
    Classic operation shows to be its memo id: kept; any other u64 stays unsupported."""
    monkeypatch.syspath_prepend(str(ROOT / "stellar/synthetic"))
    tx = "ef" * 32
    record = {
        "paging_token": str((90000520 << 32) | (1 << 12) | 1),
        "transaction_hash": tx,
        "transaction_successful": True,
        "type": "path_payment_strict_send",
        "from": W,
        "to": DESTINATION,
        "amount": "0.0000005",
        "source_amount": "0.0000005",
        "path": [],
        "asset_type": "credit_alphanum12",
        "asset_code": "SYNUSD",
        "asset_issuer": ISSUER,
        "source_asset_type": "credit_alphanum12",
        "source_asset_code": "SYNUSD",
        "source_asset_issuer": ISSUER,
        "transaction": {
            "hash": tx,
            "ledger": 90000520,
            "successful": True,
            "memo_type": "id",
            "memo": "88",
        },
    }
    operation = path_operation_entry(TARGET, record)
    assert operation["memo"] == {"memo_type": "id", "value": "88"}
    page, event = _leg(88, DESTINATION, tx)
    leg, _note = normalize_sac_leg(TARGET, event, 0, operation, page, RECORDED_AT)
    assert isinstance(leg, Observation)
    assert leg.provenance.mapping_ref == "stellar-sac-path-leg@1.2.0"
    for datum, receiver in ((89, DESTINATION), (88, ISSUER)):  # not the memo id / leg
        page, event = _leg(datum, receiver, tx)
        (other,) = normalize_sac_leg(TARGET, event, 0, operation, page, RECORDED_AT)
        assert not isinstance(other, Observation) and other.reason == "UNSUPPORTED_VALUE"
    muxed = dict(record, to_muxed=encode_muxed_account(DESTINATION, 88), to_muxed_id="88")
    muxed_operation = path_operation_entry(TARGET, muxed)
    page, event = _leg(88, DESTINATION, tx)  # the destination's sub-account, not a memo
    (excluded,) = normalize_sac_leg(TARGET, event, 0, muxed_operation, page, RECORDED_AT)
    assert not isinstance(excluded, Observation) and excluded.reason == "UNSUPPORTED_VALUE"
