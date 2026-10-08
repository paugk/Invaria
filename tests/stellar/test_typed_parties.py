"""Typed parties (contract, claimable balance, liquidity pool).

The cases come from the SYNTHETIC recordings in ``tests/fixtures/stellar/synthetic/
typed-parties`` (``build_typed_parties.py``; nothing there exists on any network). The real
samples (``gdice-issuer-cb-clawback``, ``usdc-gdes54wt-cb-create``,
``usdc-gapnrjhi-cb-claim``, ``usdc-gdbxa45u-contract`` and the pool legs) are covered in
``test_adapter.py``.
"""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from invaria.contracts.base import parse_contract
from invaria.contracts.chain import ChainTarget, ExecutionLink
from invaria.contracts.observation import (
    ChainEffectPayload,
    ChainParty,
    Observation,
    effect_parties,
    party_identity,
)
from invaria.contracts.quantity import Quantity
from invaria.contracts.stellar import (
    claimable_balance_hex,
    claimable_balance_strkey,
    encode_strkey,
    liquidity_pool_hex,
    liquidity_pool_strkey,
)
from invaria.engine.common import resolve_records
from invaria.stellar.adapter import (
    BalanceHistory,
    PendingBalanceEffect,
    RunResult,
    _balance_check,
    ingest_horizon_payments,
    ingest_sac_events,
)
from invaria.stellar.executions import executions
from invaria.stellar.http import ReplayClient
from invaria.stellar.sources import Horizon, Rpc, verify_network
from invaria.stellar.store import IngestStore

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
SYNTHETIC = ROOT / "stellar/synthetic/typed-parties"
START, END = 90000500, 90000520
RECORDED_AT = datetime(2026, 10, 7, 16, 0, tzinfo=UTC)
HORIZON_URL, RPC_URL = "https://horizon.synthetic.invalid", "https://rpc.synthetic.invalid"


def _target(name: str) -> ChainTarget:
    return parse_contract(ChainTarget, (SYNTHETIC / name).read_text("utf-8"))


WATCHED, ISSUER_T = _target("target-watched.json"), _target("target-issuer.json")
A, ISSUER = WATCHED.account, WATCHED.asset_issuer


def _raw(label: str) -> bytes:
    return hashlib.sha256(f"invaria:synthetic:stellar:{label}".encode()).digest()


def _account(label: str) -> str:
    from invaria.contracts.stellar import encode_account_id

    return encode_account_id(_raw(f"account:{label}"))


C, D = _account("typed-claimant"), _account("typed-creator")
WALLET = encode_strkey("contract", _raw("contract:typed-wallet"))
POOL = encode_strkey("liquidity_pool", _raw("liquidity-pool:synusd-xlm"))


def _balance(label: str) -> str:
    return encode_strkey("claimable_balance", b"\x00" + _raw(f"claimable-balance:{label}"))


def _tx(label: str) -> str:
    return _raw(f"tx:typed:{label}").hex()


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_: Any, **__: Any) -> Any:
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def _ingest(
    store: IngestStore, target: ChainTarget, links: tuple[ExecutionLink, ...] = ()
) -> tuple[RunResult, RunResult]:
    client = ReplayClient(SYNTHETIC / "recordings")
    horizon, rpc = Horizon(HORIZON_URL, client), Rpc(RPC_URL, client)
    check = verify_network(horizon, rpc, "stellar:testnet")
    classic = ingest_horizon_payments(
        target,
        horizon,
        store,
        start_ledger=START,
        end_ledger=END,
        history=(check.horizon_elder_ledger, check.horizon_latest_ledger),
        recorded_at=RECORDED_AT,
        links=links,
    )
    sac = ingest_sac_events(
        target,
        rpc,
        horizon,
        store,
        start_ledger=START,
        end_ledger=END,
        recorded_at=RECORDED_AT,
        links=links,
    )
    return classic, sac


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    store = IngestStore(tmp_path_factory.mktemp("typed"))
    watched = _ingest(store, WATCHED)
    issuer = _ingest(store, ISSUER_T)
    return {"store": store, "watched": watched, "issuer": issuer}


def _effect(store: IngestStore, label: str) -> ChainEffectPayload:
    (o,) = [o for o in store.observations() if o.source.record_key.startswith(_tx(label))]
    assert isinstance(o.payload, ChainEffectPayload)
    return o.payload


def _check(store: IngestStore, target: ChainTarget, label: str) -> dict[str, Any]:
    return dict(store.current_correspondence()[(target.target_id, _tx(label), 0)])


def test_the_fixture_is_marked_synthetic() -> None:
    assert WATCHED.synthetic and ISSUER_T.synthetic and START > 50_000_000
    for meta in (SYNTHETIC / "recordings").glob("*.json"):
        assert "SYNTHETIC" in json.loads(meta.read_text("utf-8"))["synthetic"]


def test_native_ids_and_strkeys_are_one_identifier() -> None:
    balance = _balance("ok")
    assert claimable_balance_strkey(claimable_balance_hex(balance)) == balance
    assert claimable_balance_hex(balance).startswith("00000000")
    assert liquidity_pool_strkey(liquidity_pool_hex(POOL)) == POOL
    for bad in [
        {"kind": "account", "id": WALLET},  # a contract never reads as an account
        {"kind": "contract", "id": A},
        {"kind": "claimable_balance", "id": POOL},
        {"kind": "liquidity_pool", "id": balance},
        {"kind": "unresolved", "id": A, "reason": "x"},
        {"kind": "unresolved"},
        {"kind": "account", "id": A, "reason": "x"},
    ]:
        with pytest.raises(ValueError):
            ChainParty.model_validate(bad)


def test_identity_carries_network_and_kind() -> None:
    wallet = ChainParty(kind="contract", id=WALLET)
    assert party_identity("stellar:testnet", wallet) != party_identity("stellar:pubnet", wallet)
    assert party_identity("stellar:testnet", wallet) == f"stellar:testnet/contract/{WALLET}"
    unresolved = ChainParty(kind="unresolved", reason="history not read")
    assert party_identity("stellar:testnet", unresolved) == "stellar:testnet/unresolved/?"


def test_a_creation_is_a_typed_debit_with_its_participants(run: dict[str, Any]) -> None:
    store = run["store"]
    created = _effect(store, "cb-create")
    assert (created.effect_kind, created.account, created.direction) == (
        "claimable_balance_created",
        A,
        "debit",
    )
    assert created.counterparty == ChainParty(kind="claimable_balance", id=_balance("ok"))
    balance = created.claimable_balance
    assert balance is not None
    assert balance.creator == ChainParty(kind="account", id=A)
    assert balance.claimants == [C, A]  # possible recipients, none of them paid yet
    assert balance.operation_type == "create_claimable_balance"
    check = _check(store, WATCHED, "cb-create")
    assert (check["status"], check["participants"]) == ("corroborated", "resolved")


def test_a_claim_names_the_creator_without_making_the_claimant_its_owner(
    run: dict[str, Any],
) -> None:
    store = run["store"]
    claimed = _effect(store, "cb-claim")
    assert (claimed.effect_kind, claimed.account, claimed.direction) == (
        "claimable_balance_claimed",
        A,
        "credit",
    )
    assert claimed.claimable_balance is not None
    # Created by D before the range: the history names D, read from Horizon.
    assert claimed.claimable_balance.creator == ChainParty(kind="account", id=D)
    assert claimed.claimable_balance.operation_source == A
    assert _check(store, WATCHED, "cb-claim")["status"] == "corroborated"


def test_a_conflict_keeps_both_sides_and_is_quarantined(run: dict[str, Any]) -> None:
    store, (_, sac) = run["store"], run["watched"]
    created = _effect(store, "cb-conflict")
    assert created.units is not None and created.units.atoms == "50000000"
    check = _check(store, WATCHED, "cb-conflict")
    assert check["status"] == "conflict" and check["participants"] == "resolved"
    assert check["classic_net"] == {A: "-40000000"} and check["sac_net"] == {A: "-50000000"}
    assert sac.coverage is not None and sac.coverage.quarantined_records is not None
    (record,) = [
        r for r in sac.coverage.quarantined_records if _tx("cb-conflict")[:16] in r.locator
    ]
    assert record.addresses is not None and A in record.addresses


def test_a_contradicting_or_missing_history_leaves_participants_unknown(
    run: dict[str, Any],
) -> None:
    store, (_, sac) = run["store"], run["watched"]
    assert _check(store, WATCHED, "cb-failed-op")["status"] == "classic_contradicts"
    wrong = _check(store, WATCHED, "cb-wrong-type")
    assert (wrong["status"], wrong["classic_operation"]) == (
        "classic_contradicts",
        "claim_claimable_balance",
    )
    missing = _effect(store, "cb-missing")
    assert missing.claimable_balance is not None
    assert missing.claimable_balance.creator.kind == "unresolved"
    assert missing.claimable_balance.claimants is None
    assert ChainParty(kind="unresolved", reason="claimants of the claimable balance") in (
        effect_parties(missing)
    )
    check = _check(store, WATCHED, "cb-missing")
    assert (check["status"], check["participants"]) == ("classic_unavailable", "unresolved")
    assert sac.coverage is not None and sac.coverage.quarantined_records is not None
    for label in ("cb-failed-op", "cb-missing"):
        (record,) = [r for r in sac.coverage.quarantined_records if _tx(label)[:16] in r.locator]
        assert record.addresses is None, label  # reaches every operation


def test_an_unsuccessful_claim_moved_nothing(run: dict[str, Any]) -> None:
    store, (_, sac) = run["store"], run["watched"]
    assert not [
        o for o in store.observations() if o.source.record_key.startswith(_tx("cb-unsuccessful"))
    ]
    (excluded,) = [e for e in sac.exclusions if e.reason == "UNSUCCESSFUL_EVENT"]
    assert excluded.quarantine and excluded.addresses is not None and A in excluded.addresses


def test_contract_movements_keep_the_contract_and_listing_decides_quarantine(
    run: dict[str, Any],
) -> None:
    store, (classic, sac) = run["store"], run["watched"]
    listed, unlisted = _effect(store, "contract-listed"), _effect(store, "contract-unlisted")
    for payload, direction in ((listed, "debit"), (unlisted, "credit")):
        assert (payload.effect_kind, payload.account, payload.direction) == (
            "contract_transfer",
            A,
            direction,
        )
        assert payload.counterparty == ChainParty(kind="contract", id=WALLET)
    notes = {e.reason: e for e in sac.exclusions if e.addresses and WALLET in e.addresses}
    assert not notes["EFFECT_NOT_A_MOVEMENT"].quarantine  # listed by Horizon too
    assert notes["NOT_LISTED_BY_HORIZON"].quarantine  # the sources disagree
    # Horizon's record of the listed call stays as the Horizon run left it.
    (horizon,) = classic.exclusions
    assert horizon.reason == "UNSUPPORTED_OPERATION" and horizon.quarantine


def test_a_pool_deposit_is_kept_without_attributing_the_pool(run: dict[str, Any]) -> None:
    store, (_, sac) = run["store"], run["watched"]
    deposit = _effect(store, "pool-deposit")
    assert (deposit.effect_kind, deposit.account, deposit.direction) == (
        "pool_transfer",
        A,
        "debit",
    )
    assert deposit.counterparty == ChainParty(kind="liquidity_pool", id=POOL)
    (note,) = [e for e in sac.exclusions if e.addresses and POOL in e.addresses]
    assert note.quarantine and set(note.addresses or ()) == {A, POOL}
    assert "liquidity providers" in note.detail


def test_the_issuer_sees_the_balance_clawback_and_the_contract_holder_one(
    run: dict[str, Any],
) -> None:
    """A holder's target never sees a clawback whose party is a balance it created (the
    event names the balance): only the issuer's target records it. Its participants link
    it back to A for the profiles."""
    store, (_, sac) = run["store"], run["issuer"]
    taken = _effect(store, "cb-clawback")
    assert (taken.effect_kind, taken.account) == ("clawback", None)
    assert taken.holder == ChainParty(kind="claimable_balance", id=_balance("ok"))
    assert taken.claimable_balance is not None
    assert taken.claimable_balance.creator == ChainParty(kind="account", id=A)
    assert A in {p.id for p in effect_parties(taken)}
    assert _check(store, ISSUER_T, "cb-clawback")["status"] == "corroborated"
    wallet = _effect(store, "contract-clawback")
    assert wallet.holder == ChainParty(kind="contract", id=WALLET) and wallet.account is None
    assert _check(store, ISSUER_T, "contract-clawback")["status"] == "sac_native"
    assert sac.coverage is not None and sac.coverage.records_quarantined == 1


def test_targets_are_isolated_and_another_asset_is_ignored(run: dict[str, Any]) -> None:
    store = run["store"]
    watched_checks = {k[1] for k in store.current_correspondence() if k[0] == WATCHED.target_id}
    issuer_checks = {k[1] for k in store.current_correspondence() if k[0] == ISSUER_T.target_id}
    assert _tx("cb-clawback") in issuer_checks and _tx("cb-clawback") not in watched_checks
    assert _tx("cb-create") in watched_checks and _tx("cb-create") not in issuer_checks
    assert not [o for o in store.observations() if o.source.record_key.startswith(_tx("cb-eurx"))]


def test_one_execution_per_effect_and_networks_never_merge(run: dict[str, Any]) -> None:
    store = run["store"]
    found = executions(store.observations())
    assert len(found) == len({e.key for e in found}) == 11
    assert all(e.status == "single" for e in found)  # SAC only; Classic is in the checks
    created = next(
        o for o in store.observations() if o.source.record_key.startswith(_tx("cb-create"))
    )
    assert isinstance(created.payload, ChainEffectPayload)
    elsewhere = created.model_copy(
        update={
            "observation_id": "obs-pubnet-copy",
            "source": created.source.model_copy(update={"source_id": "stellar-pubnet"}),
            "payload": created.payload.model_copy(
                update={
                    "chain": created.payload.chain.model_copy(update={"network": "stellar:pubnet"})
                }
            ),
        }
    )
    joint = executions([*store.observations(), elsewhere])
    assert len(joint) == 12
    networks = {e.key[0] for e in joint if e.key[1] == _tx("cb-create")}
    assert networks == {"stellar:testnet", "stellar:pubnet"}  # never one execution


def test_rerun_is_idempotent_and_two_contents_are_never_chosen(tmp_path: Path) -> None:
    store = IngestStore(tmp_path)
    _ingest(store, WATCHED)
    before, checks = store.observations(), store.correspondence()
    _, again = _ingest(store, WATCHED)
    assert again.appended == () and store.observations() == before
    assert store.correspondence() == checks
    created = next(o for o in before if o.source.record_key.startswith(_tx("cb-create")))
    assert isinstance(created.payload, ChainEffectPayload) and created.payload.units is not None
    altered: Observation = created.model_copy(
        update={
            "observation_id": "obs-altered-balance",
            "payload": created.payload.model_copy(
                update={"units": created.payload.units.model_copy(update={"atoms": "1"})}
            ),
        }
    )
    resolved = resolve_records([created, altered])
    assert [state for state, _ in resolved] == ["conflict"]
    (execution,) = [e for e in executions([*before, altered]) if e.key[1] == _tx("cb-create")]
    # Two contents of one record: both kept, the execution is a conflict, none is chosen.
    assert execution.status == "conflict" and len(execution.representations) == 2
    # Same amount, other ledger: still two contents of one record, never "single".
    moved = created.model_copy(
        update={
            "observation_id": "obs-moved-balance",
            "payload": created.payload.model_copy(
                update={"chain": created.payload.chain.model_copy(update={"ledger": 1})}
            ),
        }
    )
    (same_amount,) = [e for e in executions([*before, moved]) if e.key[1] == _tx("cb-create")]
    assert same_amount.status == "conflict" and len(same_amount.representations) == 2


def test_occurrences_pair_classic_with_the_matching_sac_event(run: dict[str, Any]) -> None:
    """A contract call can claw back one holder twice in one operation; a Classic operation
    claws back once. The n-th effect of a party on one side pairs with the n-th on the
    other, so a second SAC event is a second execution, never merged into the first."""
    store = run["store"]
    wallet = next(
        o for o in store.observations() if o.source.record_key.startswith(_tx("contract-clawback"))
    )
    second = wallet.model_copy(
        update={
            "observation_id": "obs-second-clawback",
            "source": wallet.source.model_copy(
                update={"record_key": wallet.source.record_key.replace(":sac:0:", ":sac:1:")}
            ),
        }
    )
    found = [e for e in executions([wallet, second]) if e.key[1] == _tx("contract-clawback")]
    assert [e.key[6] for e in found] == [0, 1]
    assert all(e.status == "single" for e in found)
    payload = wallet.payload
    assert isinstance(payload, ChainEffectPayload)
    # The non-account holder is a participant of the effect, for relevance and identity.
    assert ChainParty(kind="contract", id=WALLET) in effect_parties(payload)


def test_an_execution_link_brings_a_corroborated_note_into_the_linked_evaluation(
    tmp_path: Path,
) -> None:
    """A corroborated balance effect is a note (not quarantined), and the adapter never sets
    ``operation_ref`` on a chain effect. If an explicit ExecutionLink names its operation,
    the note is quarantined and its record lists the linked operation, so its evaluation
    cannot ignore it whatever its addresses; the known addresses are kept."""
    link = ExecutionLink(
        schema_version="1.0",
        network="stellar:testnet",
        tx_hash=_tx("cb-create"),
        operation_index=0,
        ordinal=0,
        operation_ref="SUB-LINKED",
        approval_ref="approval-test-only",
        recorded_at=RECORDED_AT,
    )
    plain_store, linked_store = IngestStore(tmp_path / "plain"), IngestStore(tmp_path / "linked")
    _, plain = _ingest(plain_store, WATCHED)
    _, linked = _ingest(linked_store, WATCHED, (link,))
    assert plain.coverage is not None and linked.coverage is not None
    assert linked.coverage.records_quarantined == plain.coverage.records_quarantined + 1
    (note,) = [e for e in linked.exclusions if e.chain_op == f"{_tx('cb-create')}:0"]
    assert note.quarantine and "ExecutionLink" in note.detail
    assert linked.coverage.quarantined_records is not None
    (record,) = [r for r in linked.coverage.quarantined_records if r.locator == note.locator]
    assert record.execution_links == ["SUB-LINKED"]
    assert record.chain_operation == f"{_tx('cb-create')}:0"
    assert record.nature == "movement_identified"
    assert record.addresses is not None and set(note.addresses or ()) <= set(record.addresses)
    # The effect itself stays unlinked: only an explicit association could set it.
    assert all(o.operation_ref is None for o in linked_store.observations())


def test_unresolved_participants_are_never_corroborated() -> None:
    """A creation record without claimants (anomalous: the protocol requires one) agrees on
    party and amount but leaves the participants unresolved: never corroborated, so the
    check is quarantined with unknown parties."""
    balance = _balance("ok")
    ledger = 90000501
    tx = _tx("cb-create")
    token = str((ledger << 32) | (1 << 12) | 1)
    creation = {
        "type": "create_claimable_balance",
        "transaction_successful": True,
        "paging_token": token,
        "transaction_hash": tx,
        "source_account": A,
        "asset": f"{WATCHED.asset_code}:{WATCHED.asset_issuer}",
        "amount": "2.0000000",
        "claimants": [],
    }
    pending = PendingBalanceEffect(
        payload={
            "account": A,
            "direction": "debit",
            "units": Quantity(atoms="20000000", scale=7, unit=WATCHED.unit),
            "chain": {"tx_hash": tx, "operation_index": 0, "ledger": ledger},
        },
        record_key=f"{tx}:0:sac:0:claimable_balance_created",
        balance_id=balance,
        expected_operation="create_claimable_balance",
        valid_time="2026-10-07T15:00:05Z",
        locator="x",
        mapping="m",
    )
    context, record, _ = _balance_check(WATCHED, pending, BalanceHistory((), (creation,)))
    assert context["claimants"] is None
    assert (record["status"], record["participants"]) == ("classic_unavailable", "unresolved")
