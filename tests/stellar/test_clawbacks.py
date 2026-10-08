"""Clawbacks as forced withdrawals, apart from admitted movements.

The cases come from the SYNTHETIC recordings in ``tests/fixtures/stellar/synthetic/
clawbacks`` (``build_clawbacks.py``; nothing there exists on any network). The real samples
(``testusb-*-clawback``, ``gdice-issuer-cb-clawback``, ``testusb-failed-clawback``) are
covered in ``test_adapter.py``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from invaria.contracts.base import parse_contract
from invaria.contracts.chain import ChainTarget, ExecutionLink
from invaria.contracts.coverage import certificate_sha256
from invaria.contracts.observation import ChainEffectPayload, ChainParty, Observation
from invaria.contracts.stellar import encode_account_id
from invaria.engine.common import active_coverage, chain_evidence, covers_absence, record_support
from invaria.stellar.adapter import (
    RunResult,
    _supersedes,
    clawback_correspondence,
    ingest_horizon_payments,
    ingest_sac_events,
    links_sha256,
)
from invaria.stellar.executions import executions
from invaria.stellar.http import ReplayClient
from invaria.stellar.sources import Horizon, Rpc, verify_network
from invaria.stellar.store import IngestStore

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
SYNTHETIC = ROOT / "stellar/synthetic/clawbacks"
START, END = 90000300, 90000315
RECORDED_AT = datetime(2026, 10, 7, 13, 0, tzinfo=UTC)
HORIZON_URL, RPC_URL = "https://horizon.synthetic.invalid", "https://rpc.synthetic.invalid"


def _target(name: str) -> ChainTarget:
    return parse_contract(ChainTarget, (SYNTHETIC / name).read_text("utf-8"))


HOLDER_A, HOLDER_B, ISSUER_T = (
    _target(n) for n in ("target-holder-a.json", "target-holder-b.json", "target-issuer.json")
)
A, B, ISSUER = HOLDER_A.account, HOLDER_B.account, HOLDER_A.asset_issuer


def _tx(label: str) -> str:
    return hashlib.sha256(f"invaria:synthetic:stellar:tx:clawback:{label}".encode()).hexdigest()


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
def run(tmp_path_factory: pytest.TempPathFactory) -> tuple[IngestStore, RunResult]:
    store = IngestStore(tmp_path_factory.mktemp("clawback"))
    _, sac = _ingest(store, HOLDER_A)
    return store, sac


def _of(store: IngestStore, label: str) -> list[Observation]:
    return [o for o in store.observations() if o.source.record_key.startswith(_tx(label))]


def _payload(o: Observation) -> ChainEffectPayload:
    assert isinstance(o.payload, ChainEffectPayload)
    return o.payload


def _status(store: IngestStore, target: ChainTarget, label: str) -> str:
    return str(store.current_correspondence()[(target.target_id, _tx(label), 0)]["status"])


def test_the_fixture_is_marked_synthetic() -> None:
    assert all(t.synthetic for t in (HOLDER_A, HOLDER_B, ISSUER_T)) and START > 50_000_000
    for meta in (SYNTHETIC / "recordings").glob("*.json"):
        assert "SYNTHETIC" in json.loads(meta.read_text("utf-8"))["synthetic"]


def test_an_executed_clawback_is_a_forced_withdrawal_not_a_movement(run: Any) -> None:
    store, sac = run
    effects = _of(store, "ok")
    assert [o.fact_type for o in effects] == ["chain_effect", "chain_effect"]
    classic, event = sorted(effects, key=lambda o: _payload(o).representation)
    for observation in (classic, event):
        payload = _payload(observation)
        assert (payload.effect_kind, payload.account, payload.direction) == ("clawback", A, "debit")
        assert payload.counterparty == ChainParty(kind="account", id=ISSUER)
        assert payload.units is not None and payload.units.atoms == "30000000"
        assert (payload.chain.network, payload.chain.tx_hash, payload.chain.operation_index) == (
            "stellar:testnet",
            _tx("ok"),
            0,
        )
        assert payload.clawback is not None and payload.clawback.asset == f"SYNUSD:{ISSUER}"
        assert observation.operation_ref is None
    assert _payload(classic).clawback.operation_type == "clawback"  # type: ignore[union-attr]
    assert _payload(classic).clawback.operation_source == ISSUER  # type: ignore[union-attr]
    assert _payload(event).clawback.operation_type is None  # type: ignore[union-attr]
    check = store.current_correspondence()[(HOLDER_A.target_id, _tx("ok"), 0)]
    assert (check["kind"], check["status"], check["claim"]) == (
        "clawback",
        "corroborated",
        "aggregate_reconciliation",
    )
    assert not any(o.fact_type == "token_movement" for o in store.observations())
    # Fully represented and corroborated: a note, not a quarantine. The profiles judge its
    # relevance; only an unresolved correspondence is quarantined.
    (record,) = [e for e in sac.exclusions if e.locator == event.provenance.raw_locator]
    assert record.reason == "EFFECT_NOT_A_MOVEMENT" and not record.quarantine
    assert sac.coverage is not None and sac.coverage.quarantined_records is not None
    assert not any(_tx("ok") in r.locator for r in sac.coverage.quarantined_records)


def test_a_contradiction_keeps_both_sides_and_quarantines(run: Any) -> None:
    store, _ = run
    assert {_payload(o).units.atoms for o in _of(store, "conflict")} == {  # type: ignore[union-attr]
        "30000000",
        "40000000",
    }
    assert _status(store, HOLDER_A, "conflict") == "conflict"


@pytest.mark.parametrize(
    ("label", "reason", "status", "why"),
    [
        ("not-found", "CLAWBACK_CLASSIC_UNAVAILABLE", "classic_unavailable", "does not find"),
        ("invoke", "CLAWBACK_SAC_NATIVE", "sac_native", "a clawback by contract call"),
        ("failed-op", "CLAWBACK_CLASSIC_CONTRADICTS", "classic_contradicts", "as failed"),
    ],
)
def test_without_a_classic_clawback_the_correspondence_stays_explicit(
    run: Any, label: str, reason: str, status: str, why: str
) -> None:
    store, sac = run
    effects = _of(store, label)
    assert [_payload(o).representation for o in effects] == ["sac"]  # no Classic withdrawal
    assert _status(store, HOLDER_A, label) == status
    (note,) = [e for e in sac.exclusions if e.reason == reason and why in e.detail]
    assert not note.quarantine  # the quarantined record is the correspondence check
    conflicts = [
        e
        for e in sac.exclusions
        if e.reason == "CORRESPONDENCE_CONFLICT" and _tx(label) in e.locator
    ]
    assert len(conflicts) == 1 and conflicts[0].quarantine


def test_an_unsuccessful_event_is_never_an_executed_withdrawal(run: Any) -> None:
    store, sac = run
    assert _of(store, "failed-event") == []
    (excluded,) = [e for e in sac.exclusions if e.reason == "UNSUCCESSFUL_EVENT"]
    assert "no withdrawal was executed" in excluded.detail and excluded.quarantine
    assert excluded.addresses is not None and set(excluded.addresses) == {A, ISSUER}


def test_one_clawback_is_one_effect_for_every_target_of_the_asset(tmp_path: Path) -> None:
    """Holder A, holder B and the issuer in one store: canonical effects, no double count,
    and each holder sees only its own clawbacks."""
    store = IngestStore(tmp_path)
    runs = {t.target_id: _ingest(store, t)[1] for t in (HOLDER_A, HOLDER_B, ISSUER_T)}
    assert all(r.conflicts == () for r in runs.values())
    effects = [o for o in store.observations() if _payload(o).effect_kind == "clawback"]
    assert len({o.source.record_key for o in effects}) == len(effects) == 9
    current = store.current_correspondence()
    assert {k[1] for k in current if k[0] == HOLDER_B.target_id} == {_tx("other-holder")}
    assert _tx("other-holder") not in {k[1] for k in current if k[0] == HOLDER_A.target_id}
    assert current[(ISSUER_T.target_id, _tx("other-holder"), 0)]["status"] == "corroborated"
    # Another asset's clawback of A (another contract) is nobody's evidence here.
    assert _of(store, "other-asset") == []


def test_every_quarantined_clawback_names_holder_and_issuer(run: Any) -> None:
    _, sac = run
    assert sac.coverage is not None and sac.coverage.quarantined_records is not None
    records = [
        r for r in sac.coverage.quarantined_records if "CORRESPONDENCE_CONFLICT" in r.reasons
    ]
    assert len(records) == 4  # conflict, not found, contract call, failed operation
    assert all(r.addresses is not None and {A, ISSUER} <= set(r.addresses) for r in records)
    unsuccessful = [
        r for r in sac.coverage.quarantined_records if "UNSUCCESSFUL_EVENT" in r.reasons
    ]
    assert len(unsuccessful) == 1 and unsuccessful[0].addresses is not None


def test_each_quarantined_clawback_says_what_is_established(run: Any) -> None:
    """The SAC event identifies the clawback whenever its Classic side is missing
    or disagrees on amounts (``clawback_identified``); a Classic operation reported as
    failed contradicts it (``contradictory``); an event of a failed invocation executed
    nothing and stays ``unresolved``. Every record names its ledger operation."""
    _, sac = run
    assert sac.coverage is not None and sac.coverage.quarantined_records is not None
    by_tx: dict[str, set[str | None]] = {}
    for record in sac.coverage.quarantined_records:
        assert record.chain_operation is not None
        by_tx.setdefault(record.chain_operation.split(":")[0], set()).add(record.nature)
    assert by_tx[_tx("conflict")] == {"clawback_identified"}
    assert by_tx[_tx("not-found")] == {"clawback_identified"}
    assert by_tx[_tx("invoke")] == {"clawback_identified"}
    assert by_tx[_tx("failed-op")] == {"contradictory"}
    assert by_tx[_tx("failed-event")] == {"unresolved"}
    assert all(r.execution_links is None for r in sac.coverage.quarantined_records)


def test_a_rerun_is_idempotent_and_two_contents_leave_it_incomplete(tmp_path: Path) -> None:
    store = IngestStore(tmp_path)
    _, first = _ingest(store, HOLDER_A)
    before = store.observations()
    _, again = _ingest(store, HOLDER_A)
    assert again.appended == () and store.observations() == before
    event = next(o for o in _of(store, "ok") if _payload(o).representation == "sac")
    units = _payload(event).units
    assert units is not None
    altered = event.model_copy(
        update={
            "observation_id": "obs-altered-clawback",
            "payload": _payload(event).model_copy(
                update={"units": units.model_copy(update={"atoms": "1"})}
            ),
        }
    )
    store.append_observations([altered])
    _ingest(store, HOLDER_A)
    assert _status(store, HOLDER_A, "ok") == "sac_incomplete"
    assert first.coverage is not None


def test_clawback_correspondence_never_merges_by_sums(run: Any) -> None:
    store, _ = run
    classic, event = sorted(_of(store, "ok"), key=lambda o: _payload(o).representation)
    operation = (_tx("ok"), 0, START + 1)
    assert clawback_correspondence(HOLDER_A, operation, [classic], [event], False)["status"] == (
        "corroborated"
    )
    assert clawback_correspondence(HOLDER_A, operation, [], [event], False)["status"] == (
        "classic_unavailable"
    )
    without = clawback_correspondence(HOLDER_A, operation, [], [event], False, "sac_native")
    assert without["status"] == "sac_native"
    other = encode_account_id(b"\x01" * 32)
    moved = event.model_copy(
        update={
            "observation_id": "obs-moved",
            "payload": _payload(event).model_copy(update={"account": other}),
        }
    )
    # Same total, another account: a conflict, never the same withdrawal.
    assert clawback_correspondence(HOLDER_A, operation, [classic], [moved], False)["status"] == (
        "conflict"
    )


def test_clawback_contract_rules(run: Any) -> None:
    store, _ = run
    classic, event = sorted(_of(store, "ok"), key=lambda o: _payload(o).representation)
    document = _payload(classic).model_dump(mode="json")
    sac_document = _payload(event).model_dump(mode="json")
    stranger = encode_account_id(b"\x02" * 32)
    for bad in [
        {**document, "direction": "credit"},  # a clawback debits the affected account
        {**document, "counterparty": {"kind": "account", "id": stranger}},  # the issuer acts
        {**document, "clawback": {**document["clawback"], "issuer": stranger}},
        {**document, "clawback": {**document["clawback"], "operation_type": None}},
        {**sac_document, "clawback": document["clawback"]},  # SAC does not name the operation
        {**document, "clawback": None},
        {**document, "effect_kind": "dex_fill"},  # only a clawback carries this context
    ]:
        with pytest.raises(ValueError):
            ChainEffectPayload.model_validate(bad)
    ChainEffectPayload.model_validate(document)
    ChainEffectPayload.model_validate(sac_document)


def test_a_classic_clawback_of_another_asset_is_not_this_targets(run: Any) -> None:
    from invaria.stellar.adapter import classic_clawback

    store, _ = run
    classic = next(o for o in _of(store, "ok") if _payload(o).representation == "classic")
    record = {
        "type": "clawback",
        "transaction_successful": True,
        "paging_token": str((START + 1) << 32 | 1 << 12 | 1),
        "transaction_hash": _tx("ok"),
        "from": A,
        "amount": "3.0000000",
        "asset_type": "credit_alphanum4",
        "asset_code": "EURX",
        "asset_issuer": ISSUER,
        "source_account": ISSUER,
        "created_at": "2026-10-07T12:00:05Z",
    }
    from invaria.stellar.sources import Page

    page = Page("synthetic#test", b"{}", classic.provenance.raw_sha256, {})
    assert classic_clawback(HOLDER_A, record, page, "x", RECORDED_AT) is None
    same = dict(record, asset_code="SYNUSD", asset_type="credit_alphanum12")
    assert classic_clawback(HOLDER_A, same, page, "x", RECORDED_AT) is not None


def test_executions_group_representations_and_keep_conflicts(tmp_path: Path) -> None:
    """Holder A, holder B and the issuer in one store: each clawback is one execution; the
    disagreeing amounts of "conflict" stay one execution in conflict (never 3 + 4), and
    a SAC-only clawback is a single representation."""
    store = IngestStore(tmp_path)
    for target in (HOLDER_A, HOLDER_B, ISSUER_T):
        _ingest(store, target)
    found = {e.key[1]: e for e in executions(store.observations())}
    assert found[_tx("ok")].status == "corroborated"
    assert found[_tx("other-holder")].key[5] == f"stellar:testnet/account/{B}"
    conflict = found[_tx("conflict")]
    assert conflict.status == "conflict" and len(conflict.representations) == 2
    assert found[_tx("invoke")].status == "single"  # SAC native: no Classic side
    assert found[_tx("not-found")].status == "single"
    assert _tx("failed-event") not in found  # never an executed withdrawal
    assert len(found) == len(executions(store.observations()))  # one per operation here


def test_a_linked_contradicted_clawback_keeps_its_nature_and_addresses(tmp_path: Path) -> None:
    """An ExecutionLink on the failed-op clawback quarantines its notes too: the event note
    says the clawback is identified, the note on its Classic side says it is contradicted,
    and every record keeps holder and issuer and lists the linked operation."""
    link = ExecutionLink(
        schema_version="1.0",
        network="stellar:testnet",
        tx_hash=_tx("failed-op"),
        operation_index=0,
        ordinal=0,
        operation_ref="SYN-OP-CLAWBACK",
        approval_ref="approval-synthetic-test",
        recorded_at=RECORDED_AT,
    )
    store = IngestStore(tmp_path)
    _, sac = _ingest(store, HOLDER_A, (link,))
    # The link does not change what the evidence says: still contradicted, not incomplete.
    assert _status(store, HOLDER_A, "failed-op") == "classic_contradicts"
    assert sac.coverage is not None and sac.coverage.quarantined_records is not None
    records = [
        r for r in sac.coverage.quarantined_records if r.chain_operation == f"{_tx('failed-op')}:0"
    ]
    natures = {
        ("classic" if r.locator.endswith("#classic") else r.reasons[0]): r.nature for r in records
    }
    assert natures == {
        "EFFECT_NOT_A_MOVEMENT": "clawback_identified",
        "classic": "contradictory",
        "CORRESPONDENCE_CONFLICT": "contradictory",
    }
    for record in records:
        assert record.execution_links == ["SYN-OP-CLAWBACK"]
        assert record.addresses is not None and {A, ISSUER} <= set(record.addresses)


# --------------------------------------- active certificates, end to end from the adapter


def test_end_to_end_a_later_horizon_run_keeps_the_sac_quarantine_active(tmp_path: Path) -> None:
    """Adapter output only. The SAC certificate declares that it supersedes the Horizon run
    of the same range; a Horizon re-run recorded later (here in a fresh store, so its id is
    the same as the first run's) supersedes nothing of the SAC route. The SAC records stay
    active, while "the most recent certificate" would be the re-run, without them."""
    store = IngestStore(tmp_path / "first")
    classic, sac = _ingest(store, HOLDER_A)
    assert classic.coverage is not None and sac.coverage is not None
    assert sac.coverage.supersedes is not None
    assert [e.coverage_id for e in sac.coverage.supersedes] == [classic.coverage.coverage_id]
    assert sac.coverage.chain_scope is not None
    assert sac.coverage.chain_scope.route == "rpc_sac_events"
    client = ReplayClient(SYNTHETIC / "recordings")
    horizon = Horizon(HORIZON_URL, client)
    check = verify_network(horizon, Rpc(RPC_URL, client), "stellar:testnet")
    rerun = ingest_horizon_payments(
        HOLDER_A,
        horizon,
        IngestStore(tmp_path / "rerun"),
        start_ledger=START,
        end_ledger=END,
        history=(check.horizon_elder_ledger, check.horizon_latest_ledger),
        recorded_at=RECORDED_AT.replace(hour=14),
    ).coverage
    assert rerun is not None and rerun.coverage_id == classic.coverage.coverage_id
    delivered = [sac.coverage, rerun]  # the re-run replaces the first run's id in a snapshot
    latest = max(delivered, key=lambda c: (c.recorded_at, c.coverage_id))
    assert latest is rerun and latest.records_quarantined == 0
    coverage = active_coverage(delivered)
    assert {c.coverage_id for c in coverage.active} == {
        sac.coverage.coverage_id,
        rerun.coverage_id,
    }
    # The SAC certificate names the first run's content: the re-run, under the same id with
    # other content, is not what it replaced (Stellar review M3).
    assert any("it names another content under that id" in r for r in coverage.refused)
    assert len(coverage.records()) == sac.coverage.records_quarantined == 5


def test_end_to_end_the_snapshot_supports_the_adapters_classifications(run: Any) -> None:
    """Every identified clawback the adapter writes is backed by its own observations (a
    successful clawback effect on the named operation, nothing else there, parties and
    reasons consistent); the contradicted one is not relied on as a clawback; the event of
    a failed invocation, which the adapter keeps as no observation, has no supported
    operation and is weighed as on an unknown one."""
    store, sac = run
    evidence = chain_evidence(
        store.observations(), HOLDER_A.source_id, HOLDER_A.instrument_id, {"stellar:testnet"}
    )
    supported = {}
    for record in sac.coverage.quarantined_records:
        support = record_support(record, evidence, {A})
        supported[record.chain_operation.split(":")[0]] = (
            support.operation is not None,
            support.clawback,
            support.bears,
        )
    assert supported[_tx("conflict")] == (True, True, False)
    assert supported[_tx("not-found")] == (True, True, False)
    assert supported[_tx("invoke")] == (True, True, False)
    assert supported[_tx("failed-op")] == (True, False, False)
    assert supported[_tx("failed-event")] == (False, False, False)


def test_a_holder_certificate_declares_what_it_cannot_see(tmp_path: Path) -> None:
    """Corrected after the Stellar review: what each route cannot see is
    declared by role and route. A holder's routes see neither the clawback nor a third
    party's claim of a balance it created; the issuer's Horizon route sees neither; the
    issuer's SAC route sees the clawback but not a third party's claim. Absence of an
    effect a certificate declares not covered can never be argued from it, although no
    current profile tries."""
    holder_store, issuer_store = IngestStore(tmp_path / "h"), IngestStore(tmp_path / "i")
    h_classic, h_sac = _ingest(holder_store, HOLDER_A)
    i_classic, i_sac = _ingest(issuer_store, ISSUER_T)
    both = ["claimable_balance_clawback", "claimable_balance_claim"]
    for cert in (h_classic.coverage, h_sac.coverage, i_classic.coverage):
        assert cert is not None and cert.chain_scope is not None
        assert cert.chain_scope.not_covered == both
        assert not covers_absence(cert, ["claimable_balance_clawback"])
        assert not covers_absence(cert, ["claimable_balance_claim"])
        assert covers_absence(cert, [])
    # The issuer's SAC route reads a claimable balance clawback (it debits the balance under
    # the issuer's authority); no route of the issuer sees a third party's claim.
    assert i_sac.coverage is not None and i_sac.coverage.chain_scope is not None
    assert i_sac.coverage.chain_scope.not_covered == ["claimable_balance_claim"]
    assert covers_absence(i_sac.coverage, ["claimable_balance_clawback"])
    assert not covers_absence(i_sac.coverage, ["claimable_balance_claim"])


def test_the_adapter_declares_only_supersessions_that_hold(tmp_path: Path) -> None:
    """A new certificate names the earlier ones of its store it replaces: same scope, a
    route it includes, a range it contains, recorded before it. A Horizon certificate never
    names the SAC one (narrower route), nor one of a wider range or another target."""
    store = IngestStore(tmp_path / "s")
    classic, sac = _ingest(store, HOLDER_A)
    assert classic.coverage is not None and sac.coverage is not None
    horizon_scope = classic.coverage.chain_scope
    assert horizon_scope is not None
    checkpoint = store.get_checkpoint(HOLDER_A.target_id, "horizon_payments")
    assert checkpoint is not None
    later = RECORDED_AT.replace(hour=15)
    assert _supersedes(store, "cov-new", horizon_scope, checkpoint, later) == [
        {
            "coverage_id": classic.coverage.coverage_id,
            "sha256": certificate_sha256(classic.coverage),
        }
    ]
    narrow = checkpoint.model_copy(update={"start_ledger": START + 1})
    assert _supersedes(store, "cov-new", horizon_scope, narrow, later) is None
    assert (
        _supersedes(store, "cov-new", horizon_scope, checkpoint, RECORDED_AT.replace(hour=1))
        is None
    )
    other = horizon_scope.model_copy(update={"target_id": "another-target"})
    assert _supersedes(store, "cov-new", other, checkpoint, later) is None
    sac_scope = horizon_scope.model_copy(update={"route": "rpc_sac_events"})
    found = _supersedes(store, "cov-new", sac_scope, checkpoint, later)
    assert [e["coverage_id"] for e in found or []] == sorted(
        [classic.coverage.coverage_id, sac.coverage.coverage_id]
    )
    other_links = horizon_scope.model_copy(update={"links_sha256": "0" * 64})
    assert _supersedes(store, "cov-new", other_links, checkpoint, later) is None
    same_instant = _supersedes(store, "cov-new", sac_scope, checkpoint, RECORDED_AT)
    assert [e["coverage_id"] for e in same_instant or []] == [classic.coverage.coverage_id]


def test_a_certificate_carries_the_links_its_run_applied(tmp_path: Path) -> None:
    """Stellar review M2: the ExecutionLinks a run applied decide which notes are
    quarantined; the certificate's scope carries their digest, so a run with other links
    never replaces it."""
    link = ExecutionLink(
        schema_version="1.0",
        network="stellar:testnet",
        tx_hash=_tx("failed-op"),
        operation_index=0,
        ordinal=0,
        operation_ref="SYN-OP-CLAWBACK",
        approval_ref="approval-synthetic-test",
        recorded_at=RECORDED_AT,
    )
    _, linked = _ingest(IngestStore(tmp_path / "linked"), HOLDER_A, (link,))
    _, plain = _ingest(IngestStore(tmp_path / "plain"), HOLDER_A)
    assert linked.coverage is not None and plain.coverage is not None
    assert linked.coverage.chain_scope is not None and plain.coverage.chain_scope is not None
    assert linked.coverage.chain_scope.links_sha256 == links_sha256(HOLDER_A, (link,))
    assert plain.coverage.chain_scope.links_sha256 == links_sha256(HOLDER_A, ())
    assert not linked.coverage.chain_scope.includes(plain.coverage.chain_scope)
