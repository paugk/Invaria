"""Anchored inclusion of Classic operations from a real testnet history checkpoint.

The fixture ``ledger-archive/checkpoint-5027711`` holds the exact bytes of checkpoint
5027711 (DEMOA T1, T2 and T3) read from SDF's testnet archive, and the anchors that
``stellar-core verify-checkpoints`` wrote after observing the network's consensus. Every
check needs the official ``stellar`` CLI 28.1.0 for XDR; without it the tests are skipped,
never passed.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from invaria.contracts.base import parse_contract
from invaria.contracts.chain import ChainTarget, ExecutionLinkSet
from invaria.contracts.corpus import IdentityLinkSet
from invaria.contracts.identity import IdentityLink
from invaria.contracts.observation import Observation, TokenMovementPayload
from invaria.contracts.stellar import encode_muxed_account
from invaria.stellar.adapter import ingest_horizon_payments
from invaria.stellar.http import ReplayClient
from invaria.stellar.ledger_proof import (
    STELLAR_CLI_VERSION,
    Anchor,
    CheckpointEvidence,
    ReplayArtifacts,
    ReplayEvidence,
    StellarCli,
    anchor_from_run,
    archive_path,
    check_event,
    check_observation,
    check_replay,
    checkpoint_of,
    fetch_checkpoint,
    frames,
    kept_log,
    load_replay_artifacts,
    manual_anchor,
    observe_anchors,
    overall_result,
    replay_report,
    report,
    run_record,
    transaction_hash,
    verify_checkpoint,
    verify_replay,
)
from invaria.stellar.sources import Endpoints, Horizon, Rpc, verify_network
from invaria.stellar.store import IngestStore
from invaria.stellar.trustline_reconciliation import (
    approved_addresses,
    reconcile,
    select_ledgers,
)

STELLAR = Path(__file__).resolve().parents[1] / "fixtures/stellar"
FIXTURE = STELLAR / "ledger-archive/checkpoint-5027711"
CHECKPOINT = 5027711
ANCHOR_HASH = "f2bd048a37c6477794f79739f319a2a8328fb9bd136f2f2d4930288eb7e91379"
T1 = "87835238a663f716a6065a3f588000c4f2a92fa7d03a33e13a5b494d7fae99a2"
TESTNET = "Test SDF Network ; September 2015"


def _cli_available() -> bool:
    try:
        out = subprocess.run(["stellar", "--version"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.stdout.decode().startswith(STELLAR_CLI_VERSION + " ")


# A dedicated gate sets INVARIA_REQUIRE_STELLAR_CLI=1: there a missing CLI fails every test
# instead of skipping it, so that skipped tests can never pass for a verification.
if os.environ.get("INVARIA_REQUIRE_STELLAR_CLI") == "1":
    if not _cli_available():
        pytest.fail(f"INVARIA_REQUIRE_STELLAR_CLI=1 but {STELLAR_CLI_VERSION} is not available")
else:
    pytestmark = pytest.mark.skipif(
        not _cli_available(), reason=f"needs the official {STELLAR_CLI_VERSION} CLI for XDR"
    )


class CachedCli:
    """The real CLI, memoized: the same XDR call always gives the same bytes."""

    def __init__(self) -> None:
        self.cli = StellarCli()

        @cache
        def decode(xdr_type: str, record: bytes) -> str:
            return json.dumps(self.cli.decode(xdr_type, record))

        @cache
        def encode(xdr_type: str, value: str) -> bytes:
            return self.cli.encode(xdr_type, json.loads(value))

        self._decode, self._encode = decode, encode

    def decode(self, xdr_type: str, record: bytes) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self._decode(xdr_type, record))
        return value

    def encode(self, xdr_type: str, value: Any) -> bytes:
        return self._encode(xdr_type, json.dumps(value, sort_keys=True))


@pytest.fixture(scope="module")
def xdr() -> CachedCli:
    return CachedCli()


@pytest.fixture(scope="module")
def target() -> ChainTarget:
    return parse_contract(ChainTarget, (STELLAR / "targets/demoa-own.json").read_text("utf-8"))


@pytest.fixture(scope="module")
def observations(target: ChainTarget) -> list[Observation]:
    """The demo's Classic observations, as the adapter writes them from real recordings."""
    links = parse_contract(
        ExecutionLinkSet, (STELLAR / "links/demoa-own.json").read_text("utf-8")
    ).links
    client = ReplayClient(STELLAR / "recordings/demoa-own")
    endpoints = Endpoints.resolve(None, None)
    horizon, rpc = Horizon(endpoints.horizon_url, client), Rpc(endpoints.rpc_url, client)
    check = verify_network(horizon, rpc, target.network)
    with tempfile.TemporaryDirectory() as directory:
        store = IngestStore(Path(directory) / "store")
        ingest_horizon_payments(
            target,
            horizon,
            store,
            start_ledger=5027650,
            end_ledger=5027672,
            history=(check.horizon_elder_ledger, check.horizon_latest_ledger),
            recorded_at=datetime(2026, 10, 5, 0, 59, 44, tzinfo=UTC),
            links=links,
            page_limit=2,
        )
        return store.observations()


RUN = FIXTURE / "anchor-run"


def _anchor(hash_: str = ANCHOR_HASH, ledger: int = CHECKPOINT) -> Anchor:
    return Anchor("imported_with_provenance", ledger, hash_, {})


@pytest.fixture(scope="module")
def evidence(xdr: CachedCli) -> CheckpointEvidence:
    anchor, problems = anchor_from_run(RUN / "anchor-run.json", CHECKPOINT)
    assert problems == []
    return verify_checkpoint(FIXTURE / "archive", CHECKPOINT, "stellar:testnet", anchor, xdr)


def _copy_archive(tmp_path: Path) -> Path:
    return Path(shutil.copytree(FIXTURE / "archive", tmp_path / "archive"))


def _rewrite(path: Path, xdr: CachedCli, xdr_type: str, change: Any) -> None:
    """Decode a checkpoint file, change one record and write valid XDR back (gzip kept)."""
    records = [xdr.decode(xdr_type, r) for r in frames(gzip.decompress(path.read_bytes()))]
    change(records)
    out = b""
    for record in records:
        body = xdr.encode(xdr_type, record)
        out += (0x80000000 | len(body)).to_bytes(4, "big") + body
    path.write_bytes(gzip.compress(out))


def _t1(observations: list[Observation]) -> Observation:
    [found] = [
        o
        for o in observations
        if isinstance(o.payload, TokenMovementPayload) and o.payload.chain.tx_hash == T1
    ]
    return found


def _with_chain(observation: Observation, **changes: Any) -> Observation:
    assert isinstance(observation.payload, TokenMovementPayload)
    payload = observation.payload
    if "units" in changes:
        payload = payload.model_copy(update={"units": changes.pop("units")})
    chain = payload.chain.model_copy(update=changes)
    return observation.model_copy(update={"payload": payload.model_copy(update={"chain": chain})})


# ------------------------------------------------------------------ valid evidence


def test_the_checkpoint_layout_follows_the_archive() -> None:
    assert checkpoint_of(5027658) == CHECKPOINT
    assert checkpoint_of(CHECKPOINT) == CHECKPOINT and checkpoint_of(CHECKPOINT + 1) == 5027775
    assert archive_path("ledger", CHECKPOINT) == "ledger/00/4c/b7/ledger-004cb77f.xdr.gz"
    with pytest.raises(ValueError):
        archive_path("ledger", 5027658)


def test_valid_evidence_supports_the_demo_operations(
    evidence: CheckpointEvidence, observations: list[Observation], target: ChainTarget
) -> None:
    assert (evidence.status, evidence.problems) == ("VERIFIED", [])
    assert sorted(evidence.ledgers) == list(range(CHECKPOINT - 63, CHECKPOINT + 1))
    checks = {
        o.payload.chain.tx_hash[:8]: check_observation(evidence, o, target)
        for o in observations
        if isinstance(o.payload, TokenMovementPayload)
    }
    # T1 (linked delivery), T2 (failed: included with its failed result) and T3.
    assert {k: c.status for k, c in checks.items()} == {
        "87835238": "SUPPORTED",
        "29bca570": "SUPPORTED",
        "00fd0148": "SUPPORTED",
    }
    t1, t2 = checks["87835238"], checks["29bca570"]
    assert (t1.inclusion, t1.technical_result, t1.movement) == (
        "INCLUDED",
        "tx_success; operation 0: success",
        "EXECUTED_PER_RESULT",
    )
    # T2 is included, and its result is a failure: that shows the attempt, never a transfer.
    assert (t2.inclusion, t2.technical_result, t2.movement) == (
        "INCLUDED",
        "tx_failed; operation 0: underfunded",
        "NOT_EXECUTED",
    )
    assert "never a transfer" in t2.detail


def test_the_transaction_hash_matches_the_official_cli(evidence: CheckpointEvidence) -> None:
    ledger = evidence.ledgers[5027658]
    envelope = ledger.envelopes[ledger.tx_hashes.index(T1)]
    cli = StellarCli()
    raw = cli.encode("TransactionEnvelope", envelope)
    import base64

    official = subprocess.run(
        ["stellar", "tx", "hash", "--network-passphrase", TESTNET],
        input=base64.b64encode(raw),
        capture_output=True,
        check=True,
    ).stdout.decode()
    assert official.strip() == transaction_hash(cli, envelope, TESTNET) == T1


def test_the_report_declares_what_the_hashes_protect(evidence: CheckpointEvidence) -> None:
    scope = report(evidence, [], [])["evidence"]["integrity_scope"]
    assert any("gzip bytes" in item for item in scope["not_committed"])
    assert any("TransactionResultSet" in item for item in scope["committed"])


def test_the_report_keeps_inclusion_apart_from_completeness(
    evidence: CheckpointEvidence, observations: list[Observation], target: ChainTarget
) -> None:
    checks = [check_observation(evidence, o, target) for o in observations]
    out = report(evidence, checks, ["stellar 28.1.0"])
    assert out["coverage"]["kind"] == "inclusion_only"
    assert out["coverage"]["completeness"] == "not_established"
    anchor = out["anchor"]
    assert anchor["mode"] == "imported_with_provenance" and anchor["relative_to_anchor"]
    assert anchor["verifier_observed_consensus"] is False
    assert "unchanged" in out["financial_evaluation"]
    assert "absence" not in json.dumps(out).lower()


# ------------------------------------------------------------------ altered evidence


def test_an_altered_transaction_is_inconsistent(
    tmp_path: Path, xdr: CachedCli, observations: list[Observation], target: ChainTarget
) -> None:
    archive = _copy_archive(tmp_path)

    def more_units(records: list[dict[str, Any]]) -> None:
        [entry] = [r for r in records if r["ledger_seq"] == 5027658]
        phase = entry["ext"]["v1"]["v1"]["phases"][0]["v0"][0]
        payment = phase["txset_comp_txs_maybe_discounted_fee"]["txs"][0]["tx"]["tx"]
        payment["operations"][0]["body"]["payment"]["amount"] = "20000000000"

    _rewrite(archive / "transactions-004cb77f.xdr.gz", xdr, "TransactionHistoryEntry", more_units)
    altered = verify_checkpoint(archive, CHECKPOINT, "stellar:testnet", _anchor(), xdr)
    assert altered.status == "INCONSISTENT"
    assert altered.problems == ["ledger 5027658: transaction set does not match its header"]
    assert check_observation(altered, _t1(observations), target).status == "UNVERIFIED"


def test_an_altered_header_is_inconsistent(tmp_path: Path, xdr: CachedCli) -> None:
    archive = _copy_archive(tmp_path)

    def later(records: list[dict[str, Any]]) -> None:
        records[10]["header"]["scp_value"]["close_time"] = "1791161900"

    _rewrite(archive / "ledger-004cb77f.xdr.gz", xdr, "LedgerHeaderHistoryEntry", later)
    altered = verify_checkpoint(archive, CHECKPOINT, "stellar:testnet", _anchor(), xdr)
    assert (altered.status, altered.problems) == (
        "INCONSISTENT",
        ["header 5027658 does not hash to its recorded hash"],
    )


def test_corrupted_bytes_are_never_verified(tmp_path: Path, xdr: CachedCli) -> None:
    archive = _copy_archive(tmp_path)
    path = archive / "results-004cb77f.xdr.gz"
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    corrupted = verify_checkpoint(archive, CHECKPOINT, "stellar:testnet", _anchor(), xdr)
    assert corrupted.status in {"INCOMPLETE", "INCONSISTENT"} and corrupted.ledgers == {}


def test_a_rehashed_header_breaks_the_chain(tmp_path: Path, xdr: CachedCli) -> None:
    """An altered header whose recorded hash is recomputed passes its own hash check; only
    the next header's ``previous_ledger_hash`` exposes it."""
    archive = _copy_archive(tmp_path)

    def rehashed(records: list[dict[str, Any]]) -> None:
        records[10]["header"]["scp_value"]["close_time"] = "1791161900"
        body = xdr.encode("LedgerHeader", records[10]["header"])
        records[10]["hash"] = hashlib.sha256(body).hexdigest()

    _rewrite(archive / "ledger-004cb77f.xdr.gz", xdr, "LedgerHeaderHistoryEntry", rehashed)
    altered = verify_checkpoint(archive, CHECKPOINT, "stellar:testnet", _anchor(), xdr)
    assert (altered.status, altered.problems) == (
        "INCONSISTENT",
        ["header 5027659 does not chain to 5027658"],
    )


def test_an_altered_result_is_inconsistent(tmp_path: Path, xdr: CachedCli) -> None:
    archive = _copy_archive(tmp_path)

    def higher_fee(records: list[dict[str, Any]]) -> None:
        [entry] = [r for r in records if r["ledger_seq"] == 5027658]
        entry["tx_result_set"]["results"][0]["result"]["fee_charged"] = "200"

    _rewrite(archive / "results-004cb77f.xdr.gz", xdr, "TransactionHistoryResultEntry", higher_fee)
    altered = verify_checkpoint(archive, CHECKPOINT, "stellar:testnet", _anchor(), xdr)
    assert (altered.status, altered.problems) == (
        "INCONSISTENT",
        ["ledger 5027658: result set does not match its header"],
    )


# ------------------------------------------------------------------ anchors


def test_a_wrong_anchor_is_a_mismatch_not_a_proof_of_tampering(xdr: CachedCli) -> None:
    """The files are internally consistent; only their relation to the anchor fails, and
    the report does not say which side is wrong."""
    wrong = verify_checkpoint(
        FIXTURE / "archive", CHECKPOINT, "stellar:testnet", _anchor("00" * 32), xdr
    )
    assert wrong.status == "ANCHOR_MISMATCH" and wrong.ledgers == {}
    assert wrong.problems == [
        "the checkpoint header chain is internally consistent but its hash is not the "
        "anchor's: files and anchor come from different histories, or one of them is wrong"
    ]


def test_a_missing_anchor_is_incomplete(xdr: CachedCli) -> None:
    assert anchor_from_run(RUN / "anchor-run.json", 5027775)[0] is not None
    assert anchor_from_run(RUN / "anchor-run.json", 5027647) == (
        None,
        ["the run verified from ledger 5027711, not 5027647"],
    )
    for anchor in (None, _anchor(ledger=5027775)):
        missing = verify_checkpoint(FIXTURE / "archive", CHECKPOINT, "stellar:testnet", anchor, xdr)
        assert missing.status == "INCOMPLETE"
        assert missing.problems == [f"no anchor for checkpoint {CHECKPOINT}"]


def test_a_manual_anchor_is_declared_relative(xdr: CachedCli) -> None:
    manual = manual_anchor(CHECKPOINT, ANCHOR_HASH, "copied from a colleague")
    checked = verify_checkpoint(FIXTURE / "archive", CHECKPOINT, "stellar:testnet", manual, xdr)
    anchor = report(checked, [], [])["anchor"]
    assert checked.status == "VERIFIED"
    assert anchor["mode"] == "manual" and anchor["relative_to_anchor"]
    assert anchor["provenance"] == {"supplied": "manually"}
    assert anchor["note"] == "copied from a colleague"
    assert not anchor["verifier_observed_consensus"]
    with pytest.raises(ValueError):
        manual_anchor(CHECKPOINT, "F2BD", None)


def test_an_imported_anchor_carries_its_run_not_a_free_label() -> None:
    anchor, problems = anchor_from_run(RUN / "anchor-run.json", CHECKPOINT)
    assert problems == [] and anchor is not None
    assert (anchor.mode, anchor.hash) == ("imported_with_provenance", ANCHOR_HASH)
    provenance = anchor.provenance
    assert provenance["stellar_core"] == {
        "image": "stellar/stellar-core@sha256:"
        "4fae6c47fea671e4883b0ed3c58fa1cdbd97f51c9196a7256b8fba80fd55f0e2",
        "version": "stellar-core 29.0.0 (4eb83337380a29ad0907f4e32196ce97b8dc7649)",
    }
    assert [v["name"] for v in provenance["validators"]] == ["sdftest1", "sdftest2", "sdftest3"]
    assert provenance["top_checkpoint"]["ledger"] == 5095423
    assert "not other operators" in provenance["depends_on"]
    observed, _ = anchor_from_run(RUN / "anchor-run.json", CHECKPOINT, observed_here=True)
    assert observed is not None and observed.mode == "observed_run"


@pytest.mark.parametrize("name", ["anchors.json", "testnet.cfg", "verify-checkpoints.log"])
def test_an_anchor_record_whose_artifacts_changed_gives_no_anchor(
    tmp_path: Path, name: str
) -> None:
    run = Path(shutil.copytree(RUN, tmp_path / "run"))
    with (run / name).open("ab") as handle:
        handle.write(b" ")
    assert anchor_from_run(run / "anchor-run.json", CHECKPOINT) == (
        None,
        [f"{name} does not match the anchor record"],
    )


@pytest.mark.parametrize(("key", "value"), [("exit_code", 1), ("kind", "something_else")])
def test_a_record_of_a_failed_or_other_run_gives_no_anchor(
    tmp_path: Path, key: str, value: object
) -> None:
    run = Path(shutil.copytree(RUN, tmp_path / "run"))
    record = json.loads((run / "anchor-run.json").read_text("utf-8"))
    record[key] = value
    (run / "anchor-run.json").write_text(json.dumps(record), "utf-8")
    assert anchor_from_run(run / "anchor-run.json", CHECKPOINT) == (
        None,
        ["the anchor record is not a successful verify-checkpoints run"],
    )


def test_the_run_record_is_derived_from_its_artifacts(tmp_path: Path) -> None:
    record = json.loads((RUN / "anchor-run.json").read_text("utf-8"))
    rebuilt = run_record(
        RUN,
        from_ledger=record["from_ledger"],
        image=record["stellar_core"]["image"],
        core_version=record["stellar_core"]["version"],
        started_at=record["started_at"],
        finished_at=record["finished_at"],
        exit_code=0,
    )
    assert rebuilt == record
    assert all(e["agree"] == 3 and e["disagree"] == 0 for e in record["externalized_seen"])


def test_observing_keeps_only_the_run_artifacts(tmp_path: Path) -> None:
    """``observe_anchors`` with a stand-in for docker: the record, the anchors, the config
    and a log without peer addresses stay; scratch files go."""
    calls: list[list[str]] = []

    def fake(command: list[str], timeout: int) -> tuple[int, bytes]:
        calls.append(command)
        work = Path(command[command.index("-v") + 1].split(":")[0])
        if "version" in command:
            return 0, b"stellar-core 29.0.0 (4eb83337380a29ad0907f4e32196ce97b8dc7649)\n"
        if "verify-checkpoints" in command:
            shutil.copyfile(RUN / "anchors.json", work / "anchors.json")
            (work / "stellar.db").write_bytes(b"scratch")
            (work / "buckets").mkdir()
            line = b"2026 X [Overlay INFO] connected to 203.0.113.7:11625\n"
            return 0, (RUN / "verify-checkpoints.log").read_bytes() + line
        return 0, b""

    record_path = observe_anchors(
        tmp_path / "run", RUN / "testnet.cfg", CHECKPOINT, run=fake, timeout_seconds=5
    )
    kept = sorted(p.name for p in (tmp_path / "run").iterdir())
    assert kept == ["anchor-run.json", "anchors.json", "testnet.cfg", "verify-checkpoints.log"]
    assert b"203.0.113.7" not in (tmp_path / "run/verify-checkpoints.log").read_bytes()
    assert any("--user" in c for c in calls)
    assert anchor_from_run(record_path, CHECKPOINT)[0] is not None
    assert kept_log(b"a [Overlay INFO] x\nb 10.0.0.1:1 y\n") == b"b <address> y\n"


# ------------------------------------------------------------------ incomplete interval


def test_a_missing_file_is_incomplete(tmp_path: Path, xdr: CachedCli) -> None:
    archive = _copy_archive(tmp_path)
    (archive / "results-004cb77f.xdr.gz").unlink()
    missing = verify_checkpoint(archive, CHECKPOINT, "stellar:testnet", _anchor(), xdr)
    assert (missing.status, missing.problems) == ("INCOMPLETE", ["missing results file"])


def test_a_truncated_ledger_file_is_incomplete(tmp_path: Path, xdr: CachedCli) -> None:
    archive = _copy_archive(tmp_path)
    _rewrite(archive / "ledger-004cb77f.xdr.gz", xdr, "LedgerHeaderHistoryEntry", lambda r: r.pop())
    short = verify_checkpoint(archive, CHECKPOINT, "stellar:testnet", _anchor(), xdr)
    assert short.status == "INCOMPLETE" and short.ledgers == {}
    assert short.problems == ["the ledger file does not hold exactly the checkpoint's ledgers"]


def test_a_ledger_without_its_results_is_not_verified(
    tmp_path: Path, xdr: CachedCli, observations: list[Observation], target: ChainTarget
) -> None:
    archive = _copy_archive(tmp_path)

    def drop(records: list[dict[str, Any]]) -> None:
        records[:] = [r for r in records if r["ledger_seq"] != 5027658]

    _rewrite(archive / "results-004cb77f.xdr.gz", xdr, "TransactionHistoryResultEntry", drop)
    partial = verify_checkpoint(archive, CHECKPOINT, "stellar:testnet", _anchor(), xdr)
    assert partial.status == "INCOMPLETE"
    assert partial.problems == ["ledger 5027658 has no transaction or result entry"]
    assert check_observation(partial, _t1(observations), target).status == "UNVERIFIED"


def test_incomplete_evidence_supports_nothing_even_in_a_checked_ledger(
    tmp_path: Path, xdr: CachedCli, observations: list[Observation], target: ChainTarget
) -> None:
    """Another ledger of the checkpoint lacks its results: T1's own ledger checks out, but
    the checkpoint is INCOMPLETE and nothing is SUPPORTED."""
    archive = _copy_archive(tmp_path)

    def drop(records: list[dict[str, Any]]) -> None:
        records[:] = [r for r in records if r["ledger_seq"] != 5027660]

    _rewrite(archive / "results-004cb77f.xdr.gz", xdr, "TransactionHistoryResultEntry", drop)
    partial = verify_checkpoint(archive, CHECKPOINT, "stellar:testnet", _anchor(), xdr)
    assert partial.status == "INCOMPLETE" and 5027658 in partial.ledgers
    check = check_observation(partial, _t1(observations), target)
    assert (check.status, check.detail) == ("UNVERIFIED", "evidence is INCOMPLETE")


def test_a_ledger_outside_the_checkpoint_is_not_verified(
    evidence: CheckpointEvidence, observations: list[Observation], target: ChainTarget
) -> None:
    elsewhere = _with_chain(_t1(observations), ledger=5027712)
    assert check_observation(evidence, elsewhere, target).status == "UNVERIFIED"


# ------------------------------------------------------------------ network and history


def test_another_network_is_rejected(xdr: CachedCli) -> None:
    other = verify_checkpoint(FIXTURE / "archive", CHECKPOINT, "stellar:pubnet", _anchor(), xdr)
    assert (other.status, other.problems) == (
        "REJECTED",
        ["the archive belongs to another network"],
    )


def test_a_history_state_of_another_checkpoint_is_rejected(tmp_path: Path, xdr: CachedCli) -> None:
    archive = _copy_archive(tmp_path)
    state_path = archive / "history-004cb77f.json"
    state = json.loads(state_path.read_text("utf-8"))
    state["currentLedger"] = CHECKPOINT + 64
    state_path.write_text(json.dumps(state), "utf-8")
    mixed = verify_checkpoint(archive, CHECKPOINT, "stellar:testnet", _anchor(), xdr)
    assert mixed.status == "REJECTED"


def test_a_history_state_claiming_another_network_does_not_correspond(
    tmp_path: Path, xdr: CachedCli
) -> None:
    """Testnet files under a state that claims the public network: every header and set
    still hashes, but transaction hashes depend on the network, so they no longer match
    the verified results."""
    archive = _copy_archive(tmp_path)
    state_path = archive / "history-004cb77f.json"
    state = json.loads(state_path.read_text("utf-8"))
    state["networkPassphrase"] = "Public Global Stellar Network ; September 2015"
    state_path.write_text(json.dumps(state), "utf-8")
    mixed = verify_checkpoint(archive, CHECKPOINT, "stellar:pubnet", _anchor(), xdr)
    assert mixed.status == "INCONSISTENT"
    assert mixed.problems == ["ledger 5027648: transactions and results do not correspond"]


def test_an_observation_of_another_network_is_contradicted(
    evidence: CheckpointEvidence, observations: list[Observation], target: ChainTarget
) -> None:
    pubnet = _with_chain(_t1(observations), network="stellar:pubnet")
    assert check_observation(evidence, pubnet, target).status == "CONTRADICTED"


# ------------------------------------------------------------------ unsupported observations


def test_an_observation_that_differs_is_contradicted(
    evidence: CheckpointEvidence, observations: list[Observation], target: ChainTarget
) -> None:
    t1 = _t1(observations)
    assert isinstance(t1.payload, TokenMovementPayload)
    more = _with_chain(t1, units=t1.payload.units.model_copy(update={"atoms": 10_010_000_000}))
    failed = _with_chain(t1, tx_successful=False)
    assert check_observation(evidence, more, target).detail == "differs in amount"
    assert check_observation(evidence, failed, target).detail == "differs in success"
    other_asset = target.model_copy(update={"asset_code": "DEMOB"})
    assert check_observation(evidence, t1, other_asset).status == "CONTRADICTED"


def test_a_transaction_not_in_the_ledger_is_not_found(
    evidence: CheckpointEvidence, observations: list[Observation], target: ChainTarget
) -> None:
    absent = _with_chain(_t1(observations), tx_hash="ab" * 32)
    assert check_observation(evidence, absent, target).status == "NOT_FOUND"


def test_an_event_that_needs_meta_cannot_be_reconstructed(
    evidence: CheckpointEvidence, observations: list[Observation], target: ChainTarget
) -> None:
    """A contract call (as a SAC transfer is) holds no amount or parties in the archive: they
    are events in the transaction meta, which history archives do not keep."""
    invocations = [
        (seq, ledger.tx_hashes[i])
        for seq, ledger in evidence.ledgers.items()
        for i, envelope in enumerate(ledger.envelopes)
        if "tx" in envelope
        and "invoke_host_function" in envelope["tx"]["tx"]["operations"][0]["body"]
    ]
    assert invocations
    seq, tx_hash = invocations[0]
    sac_like = _with_chain(_t1(observations), ledger=seq, tx_hash=tx_hash)
    check = check_observation(evidence, sac_like, target)
    assert check.status == "CANNOT_RECONSTRUCT"
    assert "meta" in check.detail


# ------------------------------------------------------------------ acquisition


def test_acquisition_keeps_bytes_and_provenance(tmp_path: Path) -> None:
    served = {
        f"https://archive.example/{archive_path(k, CHECKPOINT)}": (FIXTURE / "archive")
        .joinpath(Path(archive_path(k, CHECKPOINT)).name)
        .read_bytes()
        for k in ("history", "ledger", "transactions", "results")
    }
    fetched = fetch_checkpoint(
        "https://archive.example",
        CHECKPOINT,
        tmp_path / "out",
        get=served.__getitem__,
        now=lambda: datetime(2026, 10, 8, tzinfo=UTC),
    )
    provenance = json.loads((tmp_path / "out/provenance.json").read_text("utf-8"))
    assert [f.kind for f in fetched] == ["history", "ledger", "transactions", "results"]
    assert (
        provenance["files"][2]["sha256"]
        == json.loads((FIXTURE / "archive/provenance.json").read_text("utf-8"))["files"][2][
            "sha256"
        ]
    )
    with pytest.raises(ValueError, match="HTTPS"):
        fetch_checkpoint(
            "http://archive.example", CHECKPOINT, tmp_path / "x", get=served.__getitem__
        )
    with pytest.raises(FileExistsError):
        fetch_checkpoint(
            "https://archive.example", CHECKPOINT, tmp_path / "out", get=served.__getitem__
        )


# ------------------------------------------------------------------ replay (catchup 5027711/64)

REPLAY = FIXTURE / "replay"


@pytest.fixture(scope="module")
def meta_records(xdr: CachedCli) -> list[dict[str, Any]]:
    return [
        xdr.decode("LedgerCloseMeta", r)
        for r in frames(gzip.decompress((REPLAY / "meta.xdr.gz").read_bytes()))
    ]


def _stream(xdr: CachedCli, records: list[dict[str, Any]]) -> bytes:
    out = b""
    for record in records:
        body = xdr.encode("LedgerCloseMeta", record)
        out += (0x80000000 | len(body)).to_bytes(4, "big") + body
    return out


def _processing(records: list[dict[str, Any]], tx_hash: str) -> dict[str, Any]:
    for record in records:
        for entry in record["v2"]["tx_processing"]:
            if entry["result"]["transaction_hash"] == tx_hash:
                found: dict[str, Any] = entry
                return found
    raise AssertionError(tx_hash)


def test_the_replay_record_keeps_references_not_state() -> None:
    record = json.loads((REPLAY / "replay-run.json").read_text("utf-8"))
    assert (
        record["meta"]["kept_sha256"]
        == hashlib.sha256((REPLAY / "meta.xdr.gz").read_bytes()).hexdigest()
    )
    assert (
        hashlib.sha256(gzip.decompress((REPLAY / "meta.xdr.gz").read_bytes())).hexdigest()
        == (record["meta"]["raw_sha256"])
    )
    assert len(record["initial_state"]["bucket_hashes"]) == 36
    assert record["initial_state"]["buckets_retained"] is False
    assert "cannot be repeated offline" in record["not_retained"]
    assert (
        record["consumption"]["exit_code"] == 0 and record["consumption"]["refused_downloads"] == 0
    )
    assert record["consumption"]["downloaded_bytes"] <= record["limits"]["download_total_bytes"]
    assert record["consumption"]["peak_disk_bytes"] <= record["limits"]["disk_bytes"]
    assert b"<address>" in (REPLAY / "catchup.log").read_bytes()


def test_the_replay_reproduces_the_anchored_checkpoint_and_its_events(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    observations: list[Observation],
    target: ChainTarget,
) -> None:
    replay = check_replay(evidence, _stream(xdr, meta_records), xdr)
    assert (replay.status, replay.problems) == ("REPLAY_CONSISTENT", [])
    assert replay.ledgers == list(range(CHECKPOINT - 63, CHECKPOINT + 1))
    checks = {
        o.payload.chain.tx_hash[:8]: check_event(replay, o, target)
        for o in observations
        if isinstance(o.payload, TokenMovementPayload)
    }
    investor = target.account
    for key in ("87835238", "00fd0148"):
        check = checks[key]
        assert check.status == "EVENT_MATCH"
        assert [(e["name"], e["amount"]) for e in check.asset_events] == [("mint", "10000000000")]
        assert check.balance_deltas == {investor: 10_000_000_000}
        # The fee is a separate XLM event, never part of the DEMOA movement.
        assert check.fee["charged"] == "100" and check.fee["events"][0]["name"] == "fee"
        assert check.other_operations == 0
        # CAP-67 carries the transaction memo in the event's to_muxed_id field: it is the
        # memo text, never read as a sub-account.
        assert check.memo_in_event == "SUB-0001"
        assert "not committed by consensus" in check.detail
    t2 = checks["29bca570"]
    assert (t2.status, t2.asset_events, t2.balance_deltas) == ("NO_MOVEMENT_IN_TX", (), {})
    assert t2.diagnostic_events == 0 and "says nothing about the account" in t2.detail


def test_altered_events_are_caught_by_the_contrast_not_by_the_anchor(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    observations: list[Observation],
    target: ChainTarget,
) -> None:
    """Events are not committed by consensus: an altered amount still replays consistently,
    and only the contrast with the observation and the trustline change exposes it."""
    records = json.loads(json.dumps(meta_records))
    entry = _processing(records, T1)
    event = entry["tx_apply_processing"]["v4"]["operations"][0]["events"][0]
    event["body"]["v0"]["data"]["map"][0]["val"]["i128"] = "20000000000"
    replay = check_replay(evidence, _stream(xdr, records), xdr)
    assert replay.status == "REPLAY_CONSISTENT"
    check = check_event(replay, _t1(observations), target)
    assert check.status == "EVENT_MISMATCH" and "event amount differs" in check.detail


def test_a_replayed_result_that_is_not_the_anchored_one_is_inconsistent(
    evidence: CheckpointEvidence, xdr: CachedCli, meta_records: list[dict[str, Any]]
) -> None:
    records = json.loads(json.dumps(meta_records))
    _processing(records, T1)["result"]["result"]["fee_charged"] = "200"
    replay = check_replay(evidence, _stream(xdr, records), xdr)
    assert (replay.status, replay.problems) == (
        "INCONSISTENT",
        ["ledger 5027658: a replayed result is not the anchored one"],
    )


def test_a_replayed_header_that_is_not_the_anchored_one_is_inconsistent(
    evidence: CheckpointEvidence, xdr: CachedCli, meta_records: list[dict[str, Any]]
) -> None:
    records = json.loads(json.dumps(meta_records))
    records[10]["v2"]["ledger_header"]["hash"] = "00" * 32
    replay = check_replay(evidence, _stream(xdr, records), xdr)
    assert replay.status == "INCONSISTENT"


def test_an_incomplete_meta_stream_is_incomplete(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    observations: list[Observation],
    target: ChainTarget,
) -> None:
    replay = check_replay(evidence, _stream(xdr, meta_records[:-1]), xdr)
    assert replay.status == "INCOMPLETE"
    assert check_event(replay, _t1(observations), target).status == "UNVERIFIED"


def test_a_replay_of_unverified_evidence_is_unverified(
    xdr: CachedCli, meta_records: list[dict[str, Any]]
) -> None:
    wrong = verify_checkpoint(
        FIXTURE / "archive", CHECKPOINT, "stellar:testnet", _anchor("00" * 32), xdr
    )
    replay = check_replay(wrong, _stream(xdr, meta_records), xdr)
    assert (replay.status, replay.problems) == (
        "UNVERIFIED",
        ["checkpoint evidence is ANCHOR_MISMATCH"],
    )


T2 = "29bca5708470cb6e912b1e97f3c55f64c038b74a9d3c266639ddd6bce6fb6126"
XLM_SAC = "CDLZFC3SYJYDZT7K67VZ75HPJVIEUVNIXF47ZG2FB2RMQQVU2HHGCYSC"


def test_a_replay_missing_a_transaction_is_inconsistent(
    evidence: CheckpointEvidence, xdr: CachedCli, meta_records: list[dict[str, Any]]
) -> None:
    records = json.loads(json.dumps(meta_records))
    processing = records[10]["v2"]["tx_processing"]
    processing[:] = [e for e in processing if e["result"]["transaction_hash"] != T1]
    replay = check_replay(evidence, _stream(xdr, records), xdr)
    assert (replay.status, replay.problems) == (
        "INCONSISTENT",
        ["ledger 5027658: replayed transactions are not the anchored ones"],
    )


def _t1_operation(records: list[dict[str, Any]]) -> dict[str, Any]:
    op: dict[str, Any] = _processing(records, T1)["tx_apply_processing"]["v4"]["operations"][0]
    return op


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("receiver", "event mint"),
        ("balance", "trustline changes"),
        ("duplicate", "2 asset events in the operation"),
    ],
)
def test_an_event_or_balance_that_differs_is_a_mismatch(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    observations: list[Observation],
    target: ChainTarget,
    change: str,
    expected: str,
) -> None:
    records = json.loads(json.dumps(meta_records))
    op = _t1_operation(records)
    if change == "receiver":
        op["events"][0]["body"]["v0"]["topics"][1]["address"] = target.asset_issuer
    elif change == "balance":
        op["changes"][1]["updated"]["data"]["trustline"]["balance"] = "10000000001"
    else:
        op["events"].append(json.loads(json.dumps(op["events"][0])))
    replay = check_replay(evidence, _stream(xdr, records), xdr)
    check = check_event(replay, _t1(observations), target)
    assert check.status == "EVENT_MISMATCH" and expected in check.detail


def test_an_event_of_another_contract_is_not_the_movement(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    observations: list[Observation],
    target: ChainTarget,
) -> None:
    records = json.loads(json.dumps(meta_records))
    op = _t1_operation(records)
    foreign = json.loads(json.dumps(op["events"][0]))
    foreign["contract_id"] = XLM_SAC
    op["events"].append(foreign)
    replay = check_replay(evidence, _stream(xdr, records), xdr)
    check = check_event(replay, _t1(observations), target)
    assert check.status == "EVENT_MATCH" and len(check.asset_events) == 1


def test_a_failed_transaction_showing_a_movement_is_a_mismatch(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    observations: list[Observation],
    target: ChainTarget,
) -> None:
    records = json.loads(json.dumps(meta_records))
    t2_meta = _processing(records, T2)["tx_apply_processing"]["v4"]
    t2_meta["operations"] = [json.loads(json.dumps(_t1_operation(records)))]
    replay = check_replay(evidence, _stream(xdr, records), xdr)
    [t2] = [
        o
        for o in observations
        if isinstance(o.payload, TokenMovementPayload) and o.payload.chain.tx_hash == T2
    ]
    check = check_event(replay, t2, target)
    assert (check.status, check.detail) == (
        "EVENT_MISMATCH",
        "a failed transaction shows an asset movement",
    )


# ------------------------------------------------------------------ replay artifacts

# The sha256 of the run's replay-run.json (original bytes) and of its later revision
# (2026-10-09, files added after the run), as recorded with the fixtures. These pins live
# in the same working tree as the fixtures: they stand for hashes obtained through a
# trusted channel, they are not independent ones.
REPLAY_RECORD_SHA256 = "b7459be8c89d05d360e990cdf81bec4c7a5c7930deca6aa8f8d55c48deb9cdc2"
REPLAY_REVISION_SHA256 = "89b1b95f79f78710cd4ed89228e2947e6b4dea766b6bb0b427348110cab66ddd"
TRUSTED = {
    "expected_record_sha256": REPLAY_RECORD_SHA256,
    "expected_revision_sha256": REPLAY_REVISION_SHA256,
}


class NoDecode:
    """An XDR tool that must not be reached: artifacts are checked before decoding."""

    def decode(self, xdr_type: str, record: bytes) -> dict[str, Any]:
        raise AssertionError("decoded before the artifacts were checked")

    def encode(self, xdr_type: str, value: Any) -> bytes:
        raise AssertionError("encoded before the artifacts were checked")


def _copy_replay(tmp_path: Path) -> Path:
    return Path(shutil.copytree(REPLAY, tmp_path / "replay"))


def _record(directory: Path) -> dict[str, Any]:
    record: dict[str, Any] = json.loads((directory / "replay-run.json").read_text("utf-8"))
    return record


def _write_record(directory: Path, record: dict[str, Any]) -> None:
    """Rewrite the record, and the revision so that it still names it (a supplier who
    rewrites everything consistently)."""
    path = directory / "replay-run.json"
    path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", "utf-8")
    revision_path = directory / "replay-run.revision-1.json"
    if revision_path.exists():
        revision = json.loads(revision_path.read_text("utf-8"))
        revision["revises"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        revision_path.write_text(json.dumps(revision, indent=1, sort_keys=True) + "\n", "utf-8")


def _write_meta(directory: Path, stream: bytes, rehash: str) -> None:
    """Write an altered meta; ``rehash`` says which of the record's hashes follow it."""
    kept = gzip.compress(stream, mtime=0)
    (directory / "meta.xdr.gz").write_bytes(kept)
    record = _record(directory)
    if rehash in ("kept", "both"):
        record["meta"]["kept_sha256"] = hashlib.sha256(kept).hexdigest()
    if rehash == "both":
        record["meta"]["raw_sha256"] = hashlib.sha256(stream).hexdigest()
        record["meta"]["raw_bytes"] = len(stream)
    _write_record(directory, record)


def _t1_trustline_balances(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines = []
    for change in _t1_operation(records)["changes"]:
        entry = next(iter(change.values()))
        if isinstance(entry, dict) and "trustline" in entry.get("data", {}):
            lines.append(entry["data"]["trustline"])
    return lines


def test_the_normal_flow_checks_the_artifacts_before_decoding(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    observations: list[Observation],
    target: ChainTarget,
) -> None:
    artifacts, replay = verify_replay(evidence, REPLAY, xdr, **TRUSTED)
    assert (artifacts.status, artifacts.problems) == ("ARTIFACTS_MATCH", [])
    assert artifacts.provenance == "imported_with_provenance"
    assert sorted(artifacts.files) == sorted(
        p.name for p in REPLAY.iterdir() if not p.name.startswith("replay-run")
    )
    # The logs the run did not hash are kept apart, as added after the run.
    assert sorted(artifacts.added_after_run) == ["download.log", "resources.log"]
    assert replay.status == "REPLAY_CONSISTENT"
    events = [
        check_event(replay, o, target)
        for o in observations
        if isinstance(o.payload, TokenMovementPayload)
    ]
    section = replay_report(evidence, artifacts, replay, events)
    assert list(section) == [
        "history_against_anchor",
        "core_execution",
        "meta_integrity",
        "correspondence",
    ]
    assert section["history_against_anchor"] == {
        "status": "VERIFIED",
        "anchor_mode": "imported_with_provenance",
        "verifier_observed_consensus": False,
    }
    execution = section["core_execution"]
    # An offline check of imported files never claims to have observed the original run.
    assert execution["observed_by_this_process"] is False
    assert execution["declared_by_record"]["invariant_checks"] == [
        "EventsAreConsistentWithEntryDiffs"
    ]
    assert "do not protect a copy modified afterwards" in execution["invariants"]
    integrity = section["meta_integrity"]
    assert integrity["record_sha256"] == REPLAY_RECORD_SHA256
    assert "channel it trusts" in integrity["trusted_record_source"]
    assert integrity["revision"] == {
        "sha256": REPLAY_REVISION_SHA256,
        "expected_sha256": REPLAY_REVISION_SHA256,
        "revises_record_sha256": REPLAY_RECORD_SHA256,
        "added_after_run": artifacts.added_after_run,
        "hashes_contemporaneous": False,
    }
    # A trusted hash shows correspondence with the record, not that the run happened.
    assert "not that the run happened" in integrity["what_a_trusted_hash_shows"]
    assert "not trust in whoever documented the run" in integrity["what_a_trusted_hash_shows"]
    correspondence = section["correspondence"]
    assert any("tx_apply_processing" in n for n in correspondence["replay_checks"]["not_checked"])
    assert "not authenticated" in correspondence["replay_checks"]["meaning"]
    assert sorted(e["status"] for e in correspondence["events"]) == [
        "EVENT_MATCH",
        "EVENT_MATCH",
        "NO_MOVEMENT_IN_TX",
    ]
    # Without a trusted hash of the record, the same files are only supplier_declared.
    untrusted, _ = verify_replay(evidence, REPLAY, xdr)
    assert (untrusted.status, untrusted.provenance) == ("ARTIFACTS_MATCH", "supplier_declared")
    section = replay_report(evidence, untrusted, replay, events)
    assert "not authenticity" in section["meta_integrity"]["trusted_record_source"]


@pytest.mark.parametrize(
    ("rehash", "problem"),
    [
        ("none", "meta.xdr.gz does not match the record"),
        ("kept", "the decompressed meta does not match the record"),
    ],
)
def test_an_altered_event_is_rejected_by_the_normal_flow(
    tmp_path: Path,
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    rehash: str,
    problem: str,
) -> None:
    records = json.loads(json.dumps(meta_records))
    event = _t1_operation(records)["events"][0]
    event["body"]["v0"]["data"]["map"][0]["val"]["i128"] = "20000000000"
    stream = _stream(xdr, records)
    # The header, transaction and result checks alone still pass on the altered stream.
    assert check_replay(evidence, stream, xdr).status == "REPLAY_CONSISTENT"
    directory = _copy_replay(tmp_path)
    _write_meta(directory, stream, rehash)
    artifacts, replay = verify_replay(evidence, directory, NoDecode())
    assert (artifacts.status, artifacts.problems, artifacts.meta_stream) == (
        "REJECTED",
        [problem],
        None,
    )
    assert replay.status == "UNVERIFIED"


def _drop(directory: Path, name: str) -> None:
    (directory / name).unlink()


def _symlink_fetch_script(directory: Path) -> None:
    _drop(directory, "fetch.sh")
    (directory / "fetch.sh").symlink_to(REPLAY / "fetch.sh")


@pytest.mark.parametrize(
    ("change", "status", "problem"),
    [
        (lambda d: _drop(d, "catchup.log"), "INCOMPLETE", "catchup.log is missing"),
        (lambda d: _drop(d, "replay-run.json"), "INCOMPLETE", "replay-run.json is missing"),
        (
            lambda d: (d / "notes.txt").write_text("x"),
            "REJECTED",
            "notes.txt is not declared by the record",
        ),
        (
            lambda d: (d / "replay.cfg").write_text("INVARIANT_CHECKS=[]\n"),
            "REJECTED",
            "replay.cfg does not match the record",
        ),
        (
            lambda d: (d / "resources.log").write_text("peak 1\n"),
            "REJECTED",
            "resources.log does not match the record",
        ),
        (
            _symlink_fetch_script,
            "REJECTED",
            "fetch.sh is not a regular file",
        ),
    ],
)
def test_missing_altered_or_undeclared_files_fail_closed(
    tmp_path: Path, evidence: CheckpointEvidence, change: Any, status: str, problem: str
) -> None:
    directory = _copy_replay(tmp_path)
    change(directory)
    artifacts, replay = verify_replay(evidence, directory, NoDecode())
    assert (artifacts.status, artifacts.problems, artifacts.meta_stream) == (
        status,
        [problem],
        None,
    )
    assert replay.status == "UNVERIFIED"


def test_a_record_that_is_not_the_trusted_one_is_rejected(
    tmp_path: Path, evidence: CheckpointEvidence
) -> None:
    directory = _copy_replay(tmp_path)
    record = _record(directory)
    record["consumption"]["peak_disk_bytes"] = 1
    _write_record(directory, record)
    artifacts, _ = verify_replay(
        evidence, directory, NoDecode(), expected_record_sha256=REPLAY_RECORD_SHA256
    )
    assert (artifacts.status, artifacts.problems) == (
        "REJECTED",
        ["the replay record is not the trusted one (sha256 differs)"],
    )


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (
            lambda r: r["anchor"].update(trusted_checkpoint=[CHECKPOINT, "00" * 32]),
            "the replay ran under another trusted checkpoint hash than the anchor",
        ),
        (
            lambda r: r["anchor"].update(record_sha256="00" * 32),
            "the replay ran with another anchor record than the one verified",
        ),
        (
            lambda r: r.update(replayed_ledgers=[CHECKPOINT - 127, CHECKPOINT]),
            "the record replayed other ledgers than this checkpoint's",
        ),
        (
            lambda r: r["initial_state"]["bucket_hashes"].pop(),
            "the bucket references differ from the initial history state",
        ),
        (
            lambda r: r.update(kind="something_else"),
            "the record is not a successful stellar-core catchup run",
        ),
        (
            lambda r: r["meta"].update(kept="../meta.xdr.gz"),
            "the record declares a file outside its directory",
        ),
    ],
)
def test_the_record_must_be_this_checkpoints_run_under_this_anchor(
    tmp_path: Path, evidence: CheckpointEvidence, change: Any, problem: str
) -> None:
    directory = _copy_replay(tmp_path)
    record = _record(directory)
    change(record)
    _write_record(directory, record)
    artifacts, _ = verify_replay(evidence, directory, NoDecode())
    assert (artifacts.status, artifacts.problems, artifacts.meta_stream) == (
        "REJECTED",
        [problem],
        None,
    )


def test_an_initial_state_of_another_checkpoint_is_rejected(
    tmp_path: Path, evidence: CheckpointEvidence
) -> None:
    directory = _copy_replay(tmp_path)
    record = _record(directory)
    name = record["initial_state"]["history_state"]
    state = json.loads((directory / name).read_text("utf-8"))
    state["currentLedger"] = CHECKPOINT - 128
    (directory / name).write_text(json.dumps(state), "utf-8")
    record["initial_state"]["history_state_sha256"] = hashlib.sha256(
        (directory / name).read_bytes()
    ).hexdigest()
    _write_record(directory, record)
    artifacts, _ = verify_replay(evidence, directory, NoDecode())
    assert (artifacts.status, artifacts.problems) == (
        "REJECTED",
        ["the initial history state is not the previous checkpoint's"],
    )


def test_a_manual_anchor_binds_the_replay_by_hash_only(
    evidence: CheckpointEvidence, xdr: CachedCli
) -> None:
    manual = replace_anchor(evidence, manual_anchor(CHECKPOINT, ANCHOR_HASH, "test"))
    artifacts, replay = verify_replay(manual, REPLAY, xdr)
    assert (artifacts.status, replay.status) == ("ARTIFACTS_MATCH", "REPLAY_CONSISTENT")
    other = replace_anchor(evidence, manual_anchor(CHECKPOINT, "00" * 32, "test"))
    artifacts, _ = verify_replay(other, REPLAY, NoDecode())
    assert artifacts.status == "REJECTED"


def replace_anchor(evidence: CheckpointEvidence, anchor: Anchor) -> CheckpointEvidence:
    copy = CheckpointEvidence(**{**evidence.__dict__})
    copy.anchor = anchor
    return copy


@pytest.fixture(scope="module")
def store_dir(tmp_path_factory: pytest.TempPathFactory, target: ChainTarget) -> Path:
    """The demo's ingest store on disk, for the command."""
    root = tmp_path_factory.mktemp("ledger-cli") / "store"
    client = ReplayClient(STELLAR / "recordings/demoa-own")
    endpoints = Endpoints.resolve(None, None)
    horizon = Horizon(endpoints.horizon_url, client)
    check = verify_network(horizon, Rpc(endpoints.rpc_url, client), target.network)
    ingest_horizon_payments(
        target,
        horizon,
        IngestStore(root),
        start_ledger=5027650,
        end_ledger=5027672,
        history=(check.horizon_elder_ledger, check.horizon_latest_ledger),
        recorded_at=datetime(2026, 10, 5, 0, 59, 44, tzinfo=UTC),
        links=parse_contract(
            ExecutionLinkSet, (STELLAR / "links/demoa-own.json").read_text("utf-8")
        ).links,
        page_limit=2,
    )
    return root


@pytest.fixture
def ledger_verify(xdr: CachedCli, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Run ``invaria stellar ledger-verify`` on the fixture checkpoint; returns the exit
    code and the written report."""
    from invaria import cli as invaria_cli
    from invaria.stellar import cli as stellar_cli

    tool = xdr
    tool.version = xdr.cli.version  # type: ignore[attr-defined]
    monkeypatch.setattr(stellar_cli, "StellarCli", lambda: tool)

    def run(store: Path, replay_dir: Path | None, *extra: str) -> tuple[int, dict[str, Any]]:
        out = tmp_path / "report.json"
        replay = [] if replay_dir is None else ["--replay-dir", str(replay_dir)]
        code = invaria_cli.main(
            [
                "stellar",
                "ledger-verify",
                "--archive-dir",
                str(FIXTURE / "archive"),
                "--ledger",
                str(CHECKPOINT),
                "--anchor-run",
                str(RUN / "anchor-run.json"),
                "--target",
                str(STELLAR / "targets/demoa-own.json"),
                "--store",
                str(store),
                *replay,
                "--out",
                str(out),
                *extra,
            ]
        )
        result: dict[str, Any] = json.loads(out.read_text("utf-8"))
        return code, result

    return run


def _trusted_flags() -> list[str]:
    return [
        "--expect-replay-record-sha256",
        REPLAY_RECORD_SHA256,
        "--expect-replay-revision-sha256",
        REPLAY_REVISION_SHA256,
    ]


def _forge_store(store: Path, tmp_path: Path, atoms: str) -> Path:
    """A copy of the store whose T1 observation claims ``atoms``."""
    forged = Path(shutil.copytree(store, tmp_path / "forged-store"))
    path = forged / "observations.jsonl"
    lines = []
    for line in path.read_text("utf-8").splitlines():
        observation = json.loads(line)
        if observation["payload"].get("chain", {}).get("tx_hash") == T1:
            observation["payload"]["units"]["atoms"] = atoms
        lines.append(json.dumps(observation, sort_keys=True, separators=(",", ":")))
    path.write_text("\n".join(lines) + "\n", "utf-8")
    return forged


def test_a_coordinated_amount_alteration_is_contradicted_never_a_success(
    tmp_path: Path,
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    observations: list[Observation],
    target: ChainTarget,
    store_dir: Path,
    ledger_verify: Any,
) -> None:
    """The T1 amount altered in the meta (event and trustline) and in the observation, with
    every local hash updated. Which checks pass, which one catches it, and the global
    result and exit code."""
    records = json.loads(json.dumps(meta_records))
    event = _t1_operation(records)["events"][0]
    event["body"]["v0"]["data"]["map"][0]["val"]["i128"] = "20000000000"
    _, after = _t1_trustline_balances(records)
    after["balance"] = str(int(after["balance"]) + 10_000_000_000)
    directory = _copy_replay(tmp_path)
    _write_meta(directory, _stream(xdr, records), "both")
    t1 = _t1(observations)
    assert isinstance(t1.payload, TokenMovementPayload)
    forged = _with_chain(t1, units=t1.payload.units.model_copy(update={"atoms": "20000000000"}))

    # Passing: artifacts against their own record, replay against the anchored checkpoint,
    # and the event and trustline against the forged observation.
    artifacts, replay = verify_replay(evidence, directory, xdr)
    assert (artifacts.status, artifacts.provenance) == ("ARTIFACTS_MATCH", "supplier_declared")
    assert replay.status == "REPLAY_CONSISTENT"
    event_check = check_event(replay, forged, target)
    assert event_check.status == "EVENT_MATCH"
    # Catching it: the forged amount contradicts the anchored transaction envelope.
    inclusion = check_observation(evidence, forged, target)
    assert inclusion.status == "CONTRADICTED"
    overall = overall_result(evidence, [inclusion], artifacts, replay, [event_check])
    assert (overall["status"], overall["exit_code"]) == ("CONTRADICTED", 1)
    assert len(overall["contradictions"]) == 1

    # The command, on the forged store and the coherent forged artifacts.
    store = _forge_store(store_dir, tmp_path, "20000000000")
    code, result = ledger_verify(store, directory)
    assert (code, result["overall"]["status"]) == (1, "CONTRADICTED")
    assert result["replay"]["meta_integrity"]["status"] == "ARTIFACTS_MATCH"
    assert result["replay"]["correspondence"]["status"] == "REPLAY_CONSISTENT"
    t1_event = [
        e
        for e in result["replay"]["correspondence"]["events"]
        if e["observation_id"] == forged.observation_id
    ]
    assert [e["status"] for e in t1_event] == ["EVENT_MATCH"]
    # With the trusted hashes, the artifacts are rejected unread.
    code, result = ledger_verify(store, directory, *_trusted_flags())
    assert (code, result["overall"]["status"]) == (1, "CONTRADICTED")
    assert result["replay"]["meta_integrity"]["status"] == "REJECTED"


def test_a_coordinated_balance_alteration_shows_the_limit_of_coherence(
    tmp_path: Path,
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    store_dir: Path,
    ledger_verify: Any,
) -> None:
    """State nobody observes: T1's absolute trustline balance shifted on both sides, local
    hashes updated. Nothing in the flow contradicts it; only a trusted record hash rejects
    it, and without one the global result is never CONSISTENT."""
    records = json.loads(json.dumps(meta_records))
    before, after = _t1_trustline_balances(records)
    before["balance"] = str(int(before["balance"]) + 50_000_000_000)
    after["balance"] = str(int(after["balance"]) + 50_000_000_000)
    directory = _copy_replay(tmp_path)
    _write_meta(directory, _stream(xdr, records), "both")
    artifacts, replay = verify_replay(evidence, directory, xdr)
    assert (artifacts.status, replay.status) == ("ARTIFACTS_MATCH", "REPLAY_CONSISTENT")
    _, entry = replay.processing[T1]
    balances = _t1_trustline_balances([{"v2": {"tx_processing": [entry]}}])
    assert [b["balance"] for b in balances] == ["50000000000", "60000000000"]

    code, result = ledger_verify(store_dir, directory)
    overall = result["overall"]
    assert (code, overall["status"], overall["contradictions"]) == (
        1,
        "COHERENT_UNAUTHENTICATED",
        [],
    )
    assert sorted(e["status"] for e in result["replay"]["correspondence"]["events"]) == [
        "EVENT_MATCH",
        "EVENT_MATCH",
        "NO_MOVEMENT_IN_TX",
    ]
    code, result = ledger_verify(store_dir, directory, *_trusted_flags())
    assert (code, result["overall"]["status"]) == (1, "NOT_ESTABLISHED")
    assert result["replay"]["meta_integrity"]["problems"] == [
        "the replay record is not the trusted one (sha256 differs)"
    ]


def test_a_replayed_header_body_that_is_not_the_verified_one_is_inconsistent(
    evidence: CheckpointEvidence, xdr: CachedCli, meta_records: list[dict[str, Any]]
) -> None:
    records = json.loads(json.dumps(meta_records))
    header = records[10]["v2"]["ledger_header"]["header"]
    header["scp_value"]["close_time"] = str(int(header["scp_value"]["close_time"]) + 1)
    replay = check_replay(evidence, _stream(xdr, records), xdr)
    assert (replay.status, replay.problems) == (
        "INCONSISTENT",
        ["ledger 5027658: replayed header body is not the verified one"],
    )


def test_a_ledger_replayed_twice_is_incomplete(
    evidence: CheckpointEvidence, xdr: CachedCli, meta_records: list[dict[str, Any]]
) -> None:
    replay = check_replay(evidence, _stream(xdr, [*meta_records, meta_records[0]]), xdr)
    assert replay.status == "INCOMPLETE"


def test_the_cli_reports_the_replay_in_four_parts(
    store_dir: Path, ledger_verify: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    code, result = ledger_verify(store_dir, REPLAY, *_trusted_flags())
    assert (code, result["overall"]["status"]) == (0, "CONSISTENT")
    section = result["replay"]
    assert section["meta_integrity"]["status"] == "ARTIFACTS_MATCH"
    assert section["core_execution"]["provenance"] == "imported_with_provenance"
    assert section["correspondence"]["status"] == "REPLAY_CONSISTENT"
    printed = capsys.readouterr().out
    assert "execution not observed by this process" in printed
    assert "not committed by any ledger header" in printed
    # Without the revision's trusted hash the files it adds are not authenticated.
    code, result = ledger_verify(
        store_dir, REPLAY, "--expect-replay-record-sha256", REPLAY_RECORD_SHA256
    )
    assert (code, result["overall"]["status"]) == (1, "COHERENT_UNAUTHENTICATED")
    assert result["overall"]["unauthenticated"] == ["no trusted sha256 of the record revision"]
    code, result = ledger_verify(store_dir, REPLAY, "--expect-replay-record-sha256", "00" * 32)
    assert (code, result["overall"]["status"]) == (1, "NOT_ESTABLISHED")
    assert "REJECTED" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("change", "status", "problem", "trusted"),
    [
        (
            lambda d: (d / "replay-run.revision-1.json").write_text(
                (d / "replay-run.revision-1.json")
                .read_text("utf-8")
                .replace(REPLAY_RECORD_SHA256, "00" * 32)
            ),
            "REJECTED",
            "the record revision does not revise this record",
            {"expected_record_sha256": REPLAY_RECORD_SHA256},
        ),
        (
            lambda d: (d / "download.log").write_text("forged\n"),
            "REJECTED",
            "download.log does not match the record",
            TRUSTED,
        ),
        (
            lambda d: (d / "replay-run.revision-1.json").unlink(),
            "INCOMPLETE",
            "replay-run.revision-1.json is missing",
            TRUSTED,
        ),
    ],
)
def test_the_revision_is_checked_apart_from_the_record(
    tmp_path: Path,
    evidence: CheckpointEvidence,
    change: Any,
    status: str,
    problem: str,
    trusted: dict[str, str],
) -> None:
    directory = _copy_replay(tmp_path)
    change(directory)
    artifacts, _ = verify_replay(evidence, directory, NoDecode(), **trusted)
    assert (artifacts.status, artifacts.problems) == (status, [problem])


def test_a_revision_that_is_not_the_trusted_one_is_rejected(
    tmp_path: Path, evidence: CheckpointEvidence
) -> None:
    directory = _copy_replay(tmp_path)
    path = directory / "replay-run.revision-1.json"
    revision = json.loads(path.read_text("utf-8"))
    revision["hashes_contemporaneous"] = True
    path.write_text(json.dumps(revision, indent=1, sort_keys=True) + "\n", "utf-8")
    artifacts, _ = verify_replay(evidence, directory, NoDecode(), **TRUSTED)
    assert artifacts.problems == ["the record revision is not the trusted one (sha256 differs)"]
    # Without a trusted hash, the revision is only checked against the record it names.
    untrusted = load_replay_artifacts(
        directory, CHECKPOINT, evidence.anchor, expected_record_sha256=REPLAY_RECORD_SHA256
    )
    assert (untrusted.status, untrusted.provenance) == (
        "ARTIFACTS_MATCH",
        "imported_with_provenance",
    )
    assert untrusted.expected_revision_sha256 is None


def test_the_overall_result_never_hides_a_contradiction_or_a_gap(
    evidence: CheckpointEvidence,
) -> None:
    from dataclasses import replace as copy

    from invaria.stellar.ledger_proof import EventCheck, ReplayArtifacts, ReplayEvidence

    artifacts = ReplayArtifacts("ARTIFACTS_MATCH", "imported_with_provenance", "a", "a")
    replay = ReplayEvidence("REPLAY_CONSISTENT", CHECKPOINT)
    match = EventCheck("t1", "EVENT_MATCH", "")
    assert overall_result(evidence, [], artifacts, replay, [match])["status"] == "CONSISTENT"
    mismatch = EventCheck("t1", "EVENT_MISMATCH", "event amount differs")
    result = overall_result(evidence, [], artifacts, replay, [mismatch])
    assert (result["status"], result["exit_code"]) == ("CONTRADICTED", 1)
    rejected = copy(artifacts, status="REJECTED")
    assert overall_result(evidence, [], rejected, replay, [match])["not_established"] == [
        "replay artifacts are REJECTED"
    ]
    untrusted = copy(artifacts, provenance="supplier_declared", expected_record_sha256=None)
    result = overall_result(evidence, [], untrusted, replay, [match])
    assert (result["status"], result["unauthenticated"]) == (
        "COHERENT_UNAUTHENTICATED",
        ["no trusted sha256 of the replay record"],
    )
    # Classic payment completeness: gaps are never a success, an incomplete cut is not
    # established, and a complete one adds nothing beyond its scope.
    gaps = overall_result(evidence, [], completeness="GAPS_FOUND")
    assert (gaps["status"], gaps["exit_code"]) == ("CONTRADICTED", 1)
    assert overall_result(evidence, [], completeness="INCOMPLETE")["status"] == "NOT_ESTABLISHED"
    assert overall_result(evidence, [], completeness="COMPLETE_IN_SCOPE")["status"] == "CONSISTENT"


# ------------------------------------------------------------------ Classic payment completeness

RECORDINGS = STELLAR / "recordings/demoa-own"
T2_HASH = "29bca5708470cb6e912b1e97f3c55f64c038b74a9d3c266639ddd6bce6fb6126"
T3 = "00fd0148b4dc7d334169406bdc0af3829ffe53c72396c563b0077da4300d34bf"
FIRST, LAST = 5027650, 5027672
PAGE_1, PAGE_2 = "21593592325734400", "21593652455284737"


def _completeness(
    evidence: CheckpointEvidence,
    target: ChainTarget,
    recordings: Path,
    store: Path,
    start: int = FIRST,
    end: int = LAST,
) -> Any:
    from invaria.stellar.ledger_completeness import compare, enumerate_payments, read_capture

    return compare(
        enumerate_payments(evidence, target, start, end),
        read_capture(recordings, target.account, start, end),
        IngestStore(store).observations(),
        target,
        start,
        end,
    )


def _page(directory: Path, cursor: str) -> Path:
    [found] = [
        p
        for p in directory.glob("*.json")
        if "/payments?" in json.loads(p.read_text("utf-8"))["url"]
        and f"cursor={cursor}&" in json.loads(p.read_text("utf-8"))["url"]
    ]
    return found


def _edit_page(directory: Path, cursor: str, change: Any, url: Any = None) -> None:
    """Rewrite a recorded page (and its sha256): a simulated provider response."""
    meta_path = _page(directory, cursor)
    meta = json.loads(meta_path.read_text("utf-8"))
    body_path = meta_path.with_suffix(".body")
    document = json.loads(body_path.read_bytes())
    change(document["_embedded"]["records"])
    body = json.dumps(document).encode()
    body_path.write_bytes(body)
    meta["response_sha256"] = hashlib.sha256(body).hexdigest()
    if url is not None:
        meta["url"] = url(meta["url"])
    meta_path.write_text(json.dumps(meta), "utf-8")


def _copy_recordings(tmp_path: Path) -> Path:
    return Path(shutil.copytree(RECORDINGS, tmp_path / "recordings"))


def _drop_record(tx_hash: str) -> Any:
    return lambda records: records.__setitem__(
        slice(None), [r for r in records if r["transaction_hash"] != tx_hash]
    )


def _edit_store(store: Path, tmp_path: Path, change: Any) -> Path:
    copy = Path(shutil.copytree(store, tmp_path / "store-copy"))
    path = copy / "observations.jsonl"
    lines = []
    for line in path.read_text("utf-8").splitlines():
        observation = change(json.loads(line))
        if observation is not None:
            lines.append(json.dumps(observation, sort_keys=True, separators=(",", ":")))
    path.write_text("".join(f"{line}\n" for line in lines), "utf-8")
    return copy


def _keys(report: Any, kind: str) -> list[tuple[str, int]]:
    return sorted(d.key for d in report.differences if d.kind == kind)


def test_completeness_rests_only_on_verified_evidence(
    xdr: CachedCli, evidence: CheckpointEvidence, target: ChainTarget, store_dir: Path
) -> None:
    wrong = verify_checkpoint(
        FIXTURE / "archive", CHECKPOINT, "stellar:testnet", _anchor("00" * 32), xdr
    )
    report = _completeness(wrong, target, RECORDINGS, store_dir)
    assert (report.status, report.enumeration.status) == ("INCOMPLETE", "UNVERIFIED")
    outside = _completeness(evidence, target, RECORDINGS, store_dir, FIRST, CHECKPOINT + 1)
    assert outside.status == "INCOMPLETE"
    assert "not inside the verified checkpoint" in outside.enumeration.problems[0]


def test_the_real_interval_is_complete_in_scope(
    evidence: CheckpointEvidence, target: ChainTarget, store_dir: Path
) -> None:
    from invaria.stellar.ledger_completeness import completeness_section

    report = _completeness(evidence, target, RECORDINGS, store_dir)
    assert [(p.ledger, p.tx_hash, p.tx_successful) for p in report.enumeration.payments] == [
        (5027658, T1, True),
        (5027664, T2_HASH, False),
        (5027669, T3, True),
    ]
    assert report.enumeration.ledgers_checked == list(range(FIRST, LAST + 1))
    assert report.capture.status == "COMPLETE"
    assert [p["cursor"] for p in report.capture.pages] == [PAGE_1, PAGE_2]
    assert report.capture.end_evidence is not None
    assert "ingested up to ledger" in report.capture.end_evidence
    # The failed attempt is kept by the adapter as a failed transaction, not as a movement.
    assert [e["key"] for e in report.adapter["failed_attempts"]] == [[T2_HASH, 0]]
    assert (report.status, report.differences) == ("COMPLETE_IN_SCOPE", [])
    section = completeness_section(report, evidence, target, FIRST, LAST)
    assert section["scope"]["bounds"] == "both included"
    assert section["scope"]["operation_types"] == ["payment"]
    assert "payment operations only, not all the account's movements" in section["proven_absence"]
    assert any("balance did not change" in n for n in section["not_proven"])
    assert "not another operator" in section["trust"]["operator"]
    assert section["coverage_effect"].startswith("none")


def test_a_simulated_omission_is_a_provider_omission_only_with_a_complete_capture(
    tmp_path: Path, evidence: CheckpointEvidence, target: ChainTarget, store_dir: Path
) -> None:
    """T3 removed from a copy of the recorded page: a simulated omission, not a failure
    observed in Horizon."""
    recordings = _copy_recordings(tmp_path)
    _edit_page(recordings, PAGE_2, _drop_record(T3))
    report = _completeness(evidence, target, recordings, store_dir)
    assert report.capture.status == "COMPLETE"
    assert (report.status, _keys(report, "PROVIDER_OMISSION")) == ("GAPS_FOUND", [(T3, 0)])
    [omission] = report.differences
    assert omission.ledger == 5027669 and "shown complete" in omission.detail


def test_an_omission_in_the_store_alone_is_an_extraction_gap(
    tmp_path: Path, evidence: CheckpointEvidence, target: ChainTarget, store_dir: Path
) -> None:
    store = _edit_store(
        store_dir,
        tmp_path,
        lambda o: None if o["payload"]["chain"]["tx_hash"] == T3 else o,
    )
    report = _completeness(evidence, target, RECORDINGS, store)
    assert (report.status, _keys(report, "EXTRACTION_GAP")) == ("GAPS_FOUND", [(T3, 0)])
    assert _keys(report, "PROVIDER_OMISSION") == []


def test_a_simulated_omission_of_the_failed_transaction_is_an_omission(
    tmp_path: Path, evidence: CheckpointEvidence, target: ChainTarget, store_dir: Path
) -> None:
    """T2 removed from the first page: that page then ends the stream (fewer records than
    its limit, Horizon already past the interval), so T3 is not listed either."""
    recordings = _copy_recordings(tmp_path)
    _edit_page(recordings, PAGE_1, _drop_record(T2_HASH))
    report = _completeness(evidence, target, recordings, store_dir)
    assert report.status == "GAPS_FOUND"
    assert _keys(report, "PROVIDER_OMISSION") == sorted([(T2_HASH, 0), (T3, 0)])


def test_without_failed_transactions_the_filter_is_incompatible_not_an_omission(
    tmp_path: Path, evidence: CheckpointEvidence, target: ChainTarget, store_dir: Path
) -> None:
    """A simulated capture without failed transactions: page 1 lists T1 and T3, and the
    next page, asked from T3, is empty. T2 is outside the capture's filters."""
    recordings = _copy_recordings(tmp_path)
    t3_record = json.loads(_page(recordings, PAGE_2).with_suffix(".body").read_bytes())[
        "_embedded"
    ]["records"][0]
    t3_token = t3_record["paging_token"]

    def no_failed(url: str) -> str:
        return url.replace("include_failed=true", "include_failed=false")

    _edit_page(
        recordings,
        PAGE_1,
        lambda records: records.__setitem__(1, t3_record),
        no_failed,
    )
    _edit_page(
        recordings,
        PAGE_2,
        lambda records: records.clear(),
        lambda url: no_failed(url).replace(f"cursor={PAGE_2}", f"cursor={t3_token}"),
    )
    report = _completeness(evidence, target, recordings, store_dir)
    assert (report.capture.status, report.capture.filters["include_failed"]) == (
        "COMPLETE",
        False,
    )
    assert [d.kind for d in report.differences] == ["SCOPE_INCOMPATIBLE"]
    assert _keys(report, "SCOPE_INCOMPATIBLE") == [(T2_HASH, 0)]
    assert report.status == "INCOMPLETE"


@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        ("page", "the page after cursor"),
        ("root", "no root recorded before it"),
    ],
)
def test_an_unproven_capture_is_incomplete_never_an_omission(
    tmp_path: Path,
    evidence: CheckpointEvidence,
    target: ChainTarget,
    store_dir: Path,
    damage: str,
    problem: str,
) -> None:
    recordings = _copy_recordings(tmp_path)
    if damage == "page":
        meta = _page(recordings, PAGE_2)
    else:
        [meta] = [
            p
            for p in recordings.glob("*.json")
            if json.loads(p.read_text("utf-8"))["url"] == "https://horizon-testnet.stellar.org/"
        ]
    meta.with_suffix(".body").unlink()
    meta.unlink()
    report = _completeness(evidence, target, recordings, store_dir)
    assert report.capture.status == "INCOMPLETE"
    assert problem in report.capture.problems[0]
    assert report.status == "INCOMPLETE"
    assert _keys(report, "PROVIDER_OMISSION") == []
    if damage == "page":
        assert _keys(report, "CAPTURE_INCOMPLETE") == [(T3, 0)]


def test_a_provider_record_that_differs_is_a_contradiction(
    tmp_path: Path, evidence: CheckpointEvidence, target: ChainTarget, store_dir: Path
) -> None:
    recordings = _copy_recordings(tmp_path)
    _edit_page(recordings, PAGE_1, lambda records: records[0].update(amount="2000.0000000"))
    report = _completeness(evidence, target, recordings, store_dir)
    assert (report.status, _keys(report, "PROVIDER_CONTRADICTION")) == ("CONTRADICTED", [(T1, 0)])
    recordings = _copy_recordings(tmp_path / "b")
    _edit_page(recordings, PAGE_1, lambda records: records[1].update(transaction_successful=True))
    assert _keys(
        _completeness(evidence, target, recordings, store_dir), "PROVIDER_CONTRADICTION"
    ) == [(T2_HASH, 0)]


def test_a_stored_record_that_differs_is_a_contradiction(
    tmp_path: Path, evidence: CheckpointEvidence, target: ChainTarget, store_dir: Path
) -> None:
    def forge(observation: dict[str, Any]) -> dict[str, Any]:
        if observation["payload"]["chain"]["tx_hash"] == T1:
            observation["payload"]["units"]["atoms"] = "20000000000"
        return observation

    report = _completeness(evidence, target, RECORDINGS, _edit_store(store_dir, tmp_path, forge))
    assert (report.status, _keys(report, "ADAPTER_CONTRADICTION")) == ("CONTRADICTED", [(T1, 0)])


def _extra_record(records: list[dict[str, Any]], **fields: Any) -> None:
    extra = json.loads(json.dumps(records[-1]))
    extra.update(fields)
    records.append(extra)


def test_a_record_out_of_scope_is_reported_without_widening_the_guarantee(
    tmp_path: Path, evidence: CheckpointEvidence, target: ChainTarget, store_dir: Path
) -> None:
    from invaria.contracts.profile import parse_profile
    from invaria.engine.common import strict_absence_scope
    from invaria.engine.evaluate import SUBSCRIPTION_UNSEEN

    recordings = _copy_recordings(tmp_path)
    token = str((5027670 << 32) | (1 << 12) | 1)
    _edit_page(
        recordings,
        PAGE_2,
        lambda records: _extra_record(
            records,
            type="path_payment_strict_send",
            transaction_hash="ab" * 32,
            paging_token=token,
            id=token,
        ),
        lambda url: url.replace("limit=2", "limit=3"),
    )
    report = _completeness(evidence, target, recordings, store_dir)
    assert _keys(report, "OUT_OF_SCOPE") == [("ab" * 32, 0)]
    assert report.status == "COMPLETE_IN_SCOPE"
    # The report adds no certificate: the profile's strict completeness rule stays unmet.
    profile = parse_profile(
        (STELLAR.parent / "corpus/subscription-testnet-1.6.0/profile.json").read_text("utf-8")
    )
    certificates = IngestStore(store_dir).coverage()
    shortfall, _ = strict_absence_scope(
        certificates,
        list(profile.representations),
        {"stellar:testnet": {target.account}},
        SUBSCRIPTION_UNSEEN,
    )
    assert shortfall is not None
    assert {c.level for c in certificates} == {"provider_claimed"}


def test_a_duplicated_capture_record_is_counted_once(
    tmp_path: Path, evidence: CheckpointEvidence, target: ChainTarget, store_dir: Path
) -> None:
    recordings = _copy_recordings(tmp_path)
    _edit_page(
        recordings,
        PAGE_2,
        lambda records: records.append(json.loads(json.dumps(records[0]))),
        lambda url: url.replace("limit=2", "limit=3"),
    )
    report = _completeness(evidence, target, recordings, store_dir)
    assert (report.status, len(report.capture.records)) == ("COMPLETE_IN_SCOPE", 3)
    recordings = _copy_recordings(tmp_path / "b")
    _edit_page(
        recordings,
        PAGE_2,
        lambda records: _extra_record(records, amount="1.0000000"),
        lambda url: url.replace("limit=2", "limit=3"),
    )
    report = _completeness(evidence, target, recordings, store_dir)
    assert report.capture.status == "INCOMPLETE"
    assert "listed twice with different content" in report.capture.problems[0]


def _payment_tx(
    evidence: CheckpointEvidence, **changes: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """T1's envelope and result, changed: a synthetic transaction for the enumerator."""
    content = evidence.ledgers[5027658]
    envelope = json.loads(json.dumps(content.envelopes[content.tx_hashes.index(T1)]))
    result = json.loads(json.dumps(content.results[T1]))
    tx = envelope["tx"]["tx"]
    op = tx["operations"][0]
    payment = op["body"]["payment"]
    for key, value in changes.items():
        if key == "tx_source":
            tx["source_account"] = value
        elif key == "op_source":
            op["source_account"] = value
        elif key in ("destination", "amount"):
            payment[key] = value
        elif key == "issuer":
            asset = payment["asset"]
            next(iter(asset.values()))["issuer"] = value
        elif key == "path":
            op["body"] = {"path_payment_strict_send": {"destination": payment["destination"]}}
        elif key == "failed":
            result["result"] = {"tx_failed": [{"op_inner": {"payment": "underfunded"}}]}
    return envelope, result


def _fee_bump(
    envelope: dict[str, Any], result: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    bumped = {
        "tx_fee_bump": {
            "tx": {
                "fee_source": ISSUER,
                "fee": 400,
                "inner_tx": {"tx": envelope["tx"]},
                "ext": "v0",
            },
            "signatures": [],
        }
    }
    inner = {"transaction_hash": "cd" * 32, "result": {**result, "ext": "v0"}}
    outer = {"fee_charged": "200", "result": {"tx_fee_bump_inner_success": inner}, "ext": "v0"}
    return bumped, outer


ISSUER = "GCGGXYAKBIOKOIMVSUTXPEHRLJJU7VB7ZIYYKUFXYIVZTTFIWKEKTBEP"
OTHER = "GDKXE2OZMJIPOSLNA6N6F2BVCI3O777I2OOC4BV7VOYUEHYX7RTRYA7Y"


def test_the_enumerator_keeps_every_in_scope_case_and_nothing_else(
    evidence: CheckpointEvidence, target: ChainTarget
) -> None:
    from invaria.contracts.stellar import encode_muxed_account
    from invaria.stellar.ledger_completeness import scoped_payments

    account = target.account
    muxed = encode_muxed_account(account, 7)

    def scoped(envelope: dict[str, Any], result: dict[str, Any]) -> list[Any]:
        return scoped_payments(1, "ef" * 32, envelope, result, target)

    # Implicit source: the transaction's source is the account, the operation names none.
    [implicit] = scoped(*_payment_tx(evidence, tx_source=account, destination=OTHER))
    assert (implicit.source, implicit.account_roles) == (account, ("source",))
    # Explicit operation source, over another transaction source.
    [explicit] = scoped(
        *_payment_tx(evidence, tx_source=OTHER, op_source=account, destination=OTHER)
    )
    assert explicit.account_roles == ("source",)
    # Muxed source and destination, decoded to the base only to enumerate.
    [m_source] = scoped(*_payment_tx(evidence, tx_source=OTHER, op_source=muxed, destination=OTHER))
    assert (m_source.source, m_source.source_muxed_id) == (account, "7")
    [m_dest] = scoped(*_payment_tx(evidence, destination=muxed))
    assert (m_dest.destination, m_dest.destination_muxed_id) == (account, "7")
    # Source and destination at once: one operation, counted once.
    [both] = scoped(*_payment_tx(evidence, tx_source=account, destination=muxed))
    assert both.account_roles == ("source", "destination")
    # The inner transaction of a fee bump, keyed by the hash given (the outer one).
    [bumped] = scoped(*_fee_bump(*_payment_tx(evidence)))
    assert (bumped.fee_bump, bumped.tx_hash, bumped.tx_successful) == (True, "ef" * 32, True)
    # A failed transaction is enumerated as failed.
    [failed] = scoped(*_payment_tx(evidence, failed=True))
    assert failed.tx_successful is False and "tx_failed" in failed.technical_result
    # Out of scope: another issuer with the same code, a path payment, another account.
    assert scoped(*_payment_tx(evidence, issuer=OTHER)) == []
    assert scoped(*_payment_tx(evidence, path=True)) == []
    assert scoped(*_payment_tx(evidence, destination=OTHER)) == []


def test_both_bounds_are_included_and_holes_or_unreadable_entries_block_completeness(
    evidence: CheckpointEvidence, target: ChainTarget
) -> None:
    import copy

    from invaria.stellar.ledger_completeness import enumerate_payments

    changed = copy.deepcopy(evidence)
    envelope, result = _payment_tx(evidence)
    for seq in (FIRST - 1, FIRST, LAST, LAST + 1):
        content = changed.ledgers[seq]
        fake = f"{seq:064x}"
        content.envelopes += [envelope, envelope]  # the same transaction twice: counted once
        content.tx_hashes += [fake, fake]
        content.results[fake] = result
    found = enumerate_payments(changed, target, FIRST, LAST)
    assert found.status == "ENUMERATED"
    assert [p.ledger for p in found.payments] == [FIRST, 5027658, 5027664, 5027669, LAST]
    hole = copy.deepcopy(evidence)
    del hole.ledgers[5027660]
    found = enumerate_payments(hole, target, FIRST, LAST)
    assert (found.status, found.problems) == (
        "INCOMPLETE",
        ["ledger 5027660 has no verified transaction set"],
    )
    unreadable = copy.deepcopy(evidence)
    content = unreadable.ledgers[5027655]
    content.envelopes.append({"tx_v0": {}})
    content.tx_hashes.append("ff" * 32)
    content.results["ff" * 32] = result
    found = enumerate_payments(unreadable, target, FIRST, LAST)
    assert found.status == "INCOMPLETE" and "unreadable transaction" in found.problems[0]


def test_the_command_reports_completeness_and_never_a_success_with_gaps(
    tmp_path: Path, store_dir: Path, ledger_verify: Any
) -> None:
    flags = [
        "--completeness-ledgers",
        str(FIRST),
        str(LAST),
        "--horizon-recording",
    ]
    coverage = (store_dir / "coverage.jsonl").read_bytes()
    code, result = ledger_verify(store_dir, None, *flags, str(RECORDINGS))
    section = result["classic_payment_completeness"]
    assert (code, result["overall"]["status"], section["status"]) == (
        0,
        "CONSISTENT",
        "COMPLETE_IN_SCOPE",
    )
    assert [list(k["key"]) for k in section["adapter_records"]["failed_attempts"]] == [[T2_HASH, 0]]
    recordings = _copy_recordings(tmp_path)
    _edit_page(recordings, PAGE_2, _drop_record(T3))
    code, result = ledger_verify(store_dir, None, *flags, str(recordings))
    assert (code, result["overall"]["status"]) == (1, "CONTRADICTED")
    assert result["classic_payment_completeness"]["proven_absence"] is None
    # Nothing in the store changes: no certificate or coverage level is written.
    assert (store_dir / "coverage.jsonl").read_bytes() == coverage


def test_records_beyond_the_interval_end_the_capture_and_are_not_counted(
    tmp_path: Path, evidence: CheckpointEvidence, target: ChainTarget, store_dir: Path
) -> None:
    recordings = _copy_recordings(tmp_path)
    token = str((LAST + 1 << 32) | (1 << 12) | 1)
    _edit_page(
        recordings,
        PAGE_2,
        lambda records: _extra_record(records, transaction_hash="ab" * 32, paging_token=token),
        lambda url: url.replace("limit=2", "limit=3"),
    )
    report = _completeness(evidence, target, recordings, store_dir)
    assert report.capture.end_evidence == f"a record of ledger {LAST + 1}, after {LAST}"
    assert (report.status, len(report.capture.records)) == ("COMPLETE_IN_SCOPE", 3)


def test_an_in_scope_payment_the_history_lacks_is_a_contradiction(
    tmp_path: Path, evidence: CheckpointEvidence, target: ChainTarget, store_dir: Path
) -> None:
    recordings = _copy_recordings(tmp_path)
    token = str((5027670 << 32) | (1 << 12) | 1)
    _edit_page(
        recordings,
        PAGE_2,
        lambda records: _extra_record(records, transaction_hash="ab" * 32, paging_token=token),
        lambda url: url.replace("limit=2", "limit=3"),
    )
    report = _completeness(evidence, target, recordings, store_dir)
    assert (report.status, _keys(report, "PROVIDER_CONTRADICTION")) == (
        "CONTRADICTED",
        [("ab" * 32, 0)],
    )


def test_stored_records_are_compared_only_from_the_classic_route(
    tmp_path: Path, evidence: CheckpointEvidence, target: ChainTarget, store_dir: Path
) -> None:
    from invaria.stellar.adapter import RPC_MAPPING

    lines = (store_dir / "observations.jsonl").read_text("utf-8").splitlines()
    t1 = next(json.loads(line) for line in lines if T1 in line)

    def with_copy(route: str, tx_hash: str) -> Path:
        copy = json.loads(json.dumps(t1))
        copy["observation_id"] = "obs-" + "0" * 32
        copy["provenance"]["mapping_ref"] = route
        copy["payload"]["chain"]["tx_hash"] = tx_hash
        copy["payload"]["units"]["atoms"] = "1"
        directory = tmp_path / f"{route}-{tx_hash[:2]}"
        store = Path(shutil.copytree(store_dir, directory))
        with (store / "observations.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(copy, sort_keys=True, separators=(",", ":")) + "\n")
        return store

    # A Classic payment record with no anchored operation: the adapter contradicts history.
    classic = _completeness(
        evidence, target, RECORDINGS, with_copy(t1["provenance"]["mapping_ref"], "ab" * 32)
    )
    assert (classic.status, _keys(classic, "ADAPTER_CONTRADICTION")) == (
        "CONTRADICTED",
        [("ab" * 32, 0)],
    )
    # A copy read from another route (SAC events) is not the extraction compared here.
    other = _completeness(evidence, target, RECORDINGS, with_copy(RPC_MAPPING, T1))
    assert (other.status, other.differences) == ("COMPLETE_IN_SCOPE", [])


def test_a_stored_failed_attempt_recorded_as_successful_is_a_contradiction(
    tmp_path: Path, evidence: CheckpointEvidence, target: ChainTarget, store_dir: Path
) -> None:
    def forge(observation: dict[str, Any]) -> dict[str, Any]:
        if observation["payload"]["chain"]["tx_hash"] == T2_HASH:
            observation["payload"]["chain"]["tx_successful"] = True
        return observation

    report = _completeness(evidence, target, RECORDINGS, _edit_store(store_dir, tmp_path, forge))
    assert _keys(report, "ADAPTER_CONTRADICTION") == [(T2_HASH, 0)]
    assert "success" in report.differences[0].detail


@pytest.mark.parametrize("damage", ["body", "root_after"])
def test_a_capture_without_integrity_or_timely_ingestion_proof_is_incomplete(
    tmp_path: Path,
    evidence: CheckpointEvidence,
    target: ChainTarget,
    store_dir: Path,
    damage: str,
) -> None:
    recordings = _copy_recordings(tmp_path)
    if damage == "body":
        body = _page(recordings, PAGE_2).with_suffix(".body")
        body.write_bytes(body.read_bytes().replace(b"1000.0000000", b"2000.0000000"))
        problem = "does not match its sha256"
    else:
        # Horizon's ingestion recorded only after the last page: the end of the stream
        # no longer shows the interval was already ingested.
        [root] = [
            p
            for p in recordings.glob("*.json")
            if json.loads(p.read_text("utf-8"))["url"] == "https://horizon-testnet.stellar.org/"
        ]
        meta = json.loads(root.read_text("utf-8"))
        meta["captured_at"] = "2026-10-05T01:30:00+00:00"
        root.write_text(json.dumps(meta), "utf-8")
        problem = "no root recorded before it"
    report = _completeness(evidence, target, recordings, store_dir)
    assert report.capture.status == "INCOMPLETE" and problem in report.capture.problems[0]
    assert report.status == "INCOMPLETE" and _keys(report, "PROVIDER_OMISSION") == []


# ------------------------------------------------------------------ trustline reconciliation
# The trustline of DEMOA of the approved addresses, in the order's interval, from
# the kept replay meta and the anchored history. A report only: nothing here changes a
# profile, an engine, a certificate or a coverage level.

ORDER_START = datetime(2026, 10, 5, 0, 57, 20, tzinfo=UTC)  # accepted_at of SUB-0001
ORDER_END = datetime(2026, 10, 5, 0, 59, 0, tzinfo=UTC)  # valid_at, excluded by the control
CHANGE_TRUST_TX = "335a5271d030a32053778e5178d106f0af2b88a136acec03256ea55032b2a713"
LINKS = STELLAR.parent / "corpus/subscription-testnet-1.6.0/identity_links.json"
DEMOA_SAC = "CC5E43MS34OKBBNBK7X56CZDTDODKJ3NO3O2UDFCNQ2FFQLK74EGFDRS"


@pytest.fixture(scope="module")
def trusted_replay(
    evidence: CheckpointEvidence, xdr: CachedCli
) -> tuple[ReplayArtifacts, ReplayEvidence]:
    return verify_replay(evidence, REPLAY, xdr, **TRUSTED)


def _reconcile(
    evidence: CheckpointEvidence,
    replayed: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
    observations: list[Observation] | None = None,
    start: datetime = ORDER_START,
    end: datetime = ORDER_END,
    bounds: Any = "half_open",
    links: list[IdentityLink] | None = None,
) -> dict[str, Any]:
    artifacts, replay = replayed
    approvals = approved_addresses(
        _demo_links() if links is None else links,
        "stellar:testnet",
        "acct-pseudo-0001",
        start,
        end,
        bounds,
    )
    return reconcile(
        evidence,
        artifacts,
        replay,
        target,
        approvals,
        select_ledgers(evidence, start, end, bounds),
        observations or [],
    )


def _demo_links() -> list[IdentityLink]:
    return parse_contract(IdentityLinkSet, LINKS.read_text("utf-8")).links


def _link(link_id: str, address: str, **fields: Any) -> IdentityLink:
    """DERIVED, SYNTHETIC: the demo's approved link with another id, address or window."""
    return _demo_links()[0].model_copy(update={"link_id": link_id, "address": address, **fields})


def _derived(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    records: list[dict[str, Any]],
    trusted: tuple[ReplayArtifacts, ReplayEvidence],
) -> tuple[ReplayArtifacts, ReplayEvidence]:
    """DERIVED, SYNTHETIC: the real meta altered in memory (the fixtures never change). The
    artifacts stand in as if a trusted record named this altered stream; the check that the
    decoded meta is the stream the artifacts hold is kept."""
    stream = _stream(xdr, records)
    return replace(trusted[0], meta_stream=stream), check_replay(evidence, stream, xdr)


def _ops(records: list[dict[str, Any]], tx_hash: str) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = _processing(records, tx_hash)["tx_apply_processing"]["v4"][
        "operations"
    ]
    return ops


def _host_op(records: list[dict[str, Any]], ledger: int) -> dict[str, Any]:
    """The operation meta of an unrelated successful transaction of ``ledger``, to carry a
    synthetic change or event (its anchored operation is not a DEMOA payment)."""
    for record in records:
        body = record["v2"]
        if body["ledger_header"]["header"]["ledger_seq"] != ledger:
            continue
        for entry in body["tx_processing"]:
            outcome = entry["result"]["result"]["result"]
            ops = entry["tx_apply_processing"]["v4"]["operations"]
            if isinstance(outcome, dict) and "tx_success" in outcome and ops:
                if entry["result"]["transaction_hash"] not in (T1, T3, CHANGE_TRUST_TX):
                    op: dict[str, Any] = ops[0]
                    return op
    raise AssertionError(ledger)


def _line_change(kind: str, account: str, balance: int, issuer: str = ISSUER) -> dict[str, Any]:
    line = {
        "account_id": account,
        "asset": {"credit_alphanum12": {"asset_code": "DEMOA", "issuer": issuer}},
    }
    if kind == "removed":
        return {"removed": {"trustline": line}}
    entry = {
        "last_modified_ledger_seq": 5027660,
        "data": {
            "trustline": {
                **line,
                "balance": str(balance),
                "limit": "9223372036854775807",
                "flags": 1,
                "ext": "v0",
            }
        },
        "ext": "v0",
    }
    return {kind: entry}


def _asset_event(
    name: str,
    *addresses: str,
    amount: int,
    contract: str = DEMOA_SAC,
    asset: str = f"DEMOA:{ISSUER}",
) -> dict[str, Any]:
    return {
        "ext": "v0",
        "contract_id": contract,
        "type": "contract",
        "body": {
            "v0": {
                "topics": [
                    {"symbol": name},
                    *({"address": a} for a in addresses),
                    {"string": asset},
                ],
                "data": {"i128": str(amount)},
            }
        },
    }


def _changes(section: dict[str, Any]) -> list[tuple[int, str, int | None, str]]:
    return [
        (c["ledger"], c["kind"], c["delta"], c["status"]) for c in section["lines"][0]["changes"]
    ]


def test_the_real_trustline_is_reconstructed_and_explained(
    evidence: CheckpointEvidence,
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
    observations: list[Observation],
) -> None:
    section = _reconcile(evidence, trusted_replay, target, observations)
    assert (section["status"], section["evidence_basis"]) == (
        "RECONCILED_IN_SCOPE",
        "authenticated_replay",
    )
    temporal = section["temporal"]
    assert (temporal["status"], temporal["examined_ledgers"]) == ("COVERED", [5027651, 5027670])
    assert temporal["border_before_start"] == {
        "ledger": 5027650,
        "close_time": "2026-10-05T00:57:17Z",
    }
    assert temporal["border_after_end"] == {"ledger": 5027671, "close_time": "2026-10-05T00:59:02Z"}
    [line] = section["lines"]
    assert line["line"] == {
        "network": "stellar:testnet",
        "account": target.account,
        "asset": {"type": "credit_alphanum12", "code": "DEMOA", "issuer": ISSUER},
    }
    assert _changes(section) == [
        (5027652, "created", 0, "EXPLAINED"),
        (5027658, "updated", 10000000000, "EXPLAINED"),
        (5027669, "updated", 10000000000, "EXPLAINED"),
    ]
    creation, t1, t3 = line["changes"]
    assert (creation["tx_hash"], creation["operation_type"]) == (CHANGE_TRUST_TX, "change_trust")
    assert creation["before"] == {"state": "ABSENT", "balance": None}
    assert (t1["tx_hash"], t1["operation_index"], t3["tx_hash"]) == (T1, 0, T3)
    # The mint carries the transaction memo, a text: not a muxed sub-account.
    assert [(e["name"], e["amount"], e["memo"], e["muxed_id"]) for e in t1["events"]] == [
        ("mint", "10000000000", "SUB-0001", None)
    ]
    # Provenance of each correspondence: the event's contract and index in its operation.
    assert [(e["contract_id"], e["event_index"]) for e in t1["events"]] == [(DEMOA_SAC, 0)]
    assert t1["gross_basis"] == "asset_events"
    assert len(t1["observation_refs"]) == 1 and len(t3["observation_refs"]) == 1
    assert line["opening"]["state"] == "ABSENT"
    assert line["closing"] == {"state": "PRESENT", "balance": 20000000000}
    assert (line["net_variation"], line["sum_of_changes"]) == (20000000000, 20000000000)
    assert (line["gross_in"], line["gross_out"]) == (20000000000, 0)
    assert line["outside_interval"] == [] and line["contradictions"] == []
    # T2 failed: its fee is charged in XLM and reported apart; no DEMOA movement.
    assert [(t["tx_hash"], t["status"], t["fee_xlm_stroops"]) for t in section["transactions"]] == [
        (CHANGE_TRUST_TX, "LINE_CHANGED", "100"),
        (T1, "LINE_CHANGED", "100"),
        (T2, "NO_EXECUTED_MOVEMENT", "100"),
        (T3, "LINE_CHANGED", "100"),
    ]
    assert section["unlocated_references"] == 0
    assert section["coverage_effect"].startswith("none")
    assert any("does not rule out" in item for item in section["not_shown"])


def test_the_command_adds_the_reconciliation_only_when_asked(
    ledger_verify: Any, store_dir: Path
) -> None:
    flags = [
        "--trustline-reconciliation",
        "2026-10-05T00:57:20Z",
        "2026-10-05T00:59:00Z",
        "--approved-links",
        str(LINKS),
        "--account-ref",
        "acct-pseudo-0001",
    ]
    code, out = ledger_verify(store_dir, REPLAY, *_trusted_flags(), *flags)
    assert (code, out["overall"]["status"]) == (0, "CONSISTENT")
    assert out["trustline_reconciliation"]["status"] == "RECONCILED_IN_SCOPE"
    code, out = ledger_verify(store_dir, REPLAY, *_trusted_flags())
    assert code == 0 and "trustline_reconciliation" not in out
    # Without a trusted hash the same reading is a diagnostic, never a success.
    code, out = ledger_verify(store_dir, REPLAY, *flags)
    section = out["trustline_reconciliation"]
    assert (section["status"], section["evidence_basis"]) == (
        "RECONCILED_UNAUTHENTICATED",
        "unauthenticated_diagnostic",
    )
    assert (code, out["overall"]["status"]) == (1, "COHERENT_UNAUTHENTICATED")
    from invaria import cli as invaria_cli

    assert (
        invaria_cli.main(
            [
                "stellar",
                "ledger-verify",
                "--archive-dir",
                str(FIXTURE / "archive"),
                "--ledger",
                str(CHECKPOINT),
                "--anchor-run",
                str(RUN / "anchor-run.json"),
                "--target",
                str(STELLAR / "targets/demoa-own.json"),
                "--store",
                str(store_dir),
                *flags,
            ]
        )
        == 2
    )  # needs --replay-dir: no reconciliation without the checked replay


def test_compensated_movements_are_never_no_movement(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    records = json.loads(json.dumps(meta_records))
    host = _host_op(records, 5027660)
    host["events"] += [
        _asset_event("transfer", OTHER, target.account, amount=5),
        _asset_event("transfer", target.account, OTHER, amount=5),
    ]
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    [line] = section["lines"]
    [item] = line["events_without_change"]
    assert (item["ledger"], item["status"], item["events_net"]) == (
        5027660,
        "COMPENSATED_IN_OPERATION",
        0,
    )
    assert "not «no movement»" in item["detail"]
    assert (line["net_variation"], line["gross_in"], line["gross_out"]) == (
        20000000000,
        20000000005,
        5,
    )
    assert section["status"] == "RECONCILED_IN_SCOPE"


def test_an_entry_and_an_exit_in_two_operations_keep_their_gross(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    records = json.loads(json.dumps(meta_records))
    entry, exit_ = _host_op(records, 5027660), _host_op(records, 5027662)
    entry["changes"] += [
        _line_change("state", target.account, 10000000000),
        _line_change("updated", target.account, 10000000005),
    ]
    entry["events"].append(_asset_event("mint", target.account, amount=5))
    exit_["changes"] += [
        _line_change("state", target.account, 10000000005),
        _line_change("updated", target.account, 10000000000),
    ]
    exit_["events"].append(_asset_event("burn", target.account, amount=5))
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    assert [c[:3] for c in _changes(section) if c[0] in (5027660, 5027662)] == [
        (5027660, "updated", 5),
        (5027662, "updated", -5),
    ]
    [line] = section["lines"]
    # Net and gross apart: the net variation hides the entry and the exit.
    assert (line["net_variation"], line["gross_in"], line["gross_out"]) == (
        20000000000,
        20000000005,
        5,
    )
    assert section["status"] == "RECONCILED_IN_SCOPE"


def test_an_unknown_opening_balance_is_never_zero(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    records = json.loads(json.dumps(meta_records))
    _ops(records, CHANGE_TRUST_TX)[0]["changes"] = [
        c for c in _ops(records, CHANGE_TRUST_TX)[0]["changes"] if "created" not in c
    ]
    _t1_operation(records)["changes"] = _t1_operation(records)["changes"][1:]  # no pre-image
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    [line] = section["lines"]
    assert line["opening"]["state"] == "NOT_ESTABLISHED"
    assert (line["net_variation"], line["sum_of_changes"]) == (None, None)
    assert _changes(section)[0] == (5027658, "updated", None, "NOT_RECONSTRUCTED")
    assert (line["status"], section["status"]) == ("INCOMPLETE", "INCOMPLETE")
    # A line the replay never touches: its balance is not established, never zero.
    section = _reconcile(evidence, trusted_replay, target, links=[_link("other", OTHER)])
    [line] = section["lines"]
    assert line["opening"]["state"] == line["closing"]["state"] == "NOT_ESTABLISHED"
    assert line["opening"]["balance"] is None and section["status"] == "INCOMPLETE"


def test_a_balance_change_without_an_event_stays_unexplained(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    records = json.loads(json.dumps(meta_records))
    _t1_operation(records)["events"] = []
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    assert _changes(section)[1] == (5027658, "updated", 10000000000, "UNEXPLAINED")
    # Only the aggregate change is visible: no decomposition is presented, not even 0/0.
    [line] = section["lines"]
    t1 = line["changes"][1]
    assert (t1["gross_in"], t1["gross_out"]) == (None, None)
    assert t1["gross_basis"].startswith("net only")
    assert (line["gross_in"], line["gross_out"]) == (None, None)
    assert line["gross_shown_by_events"] == {"in": 10000000000, "out": 0}
    assert section["status"] == "UNRESOLVED_CHANGES"
    overall = overall_result(evidence, [], reconciliation=section["status"])
    assert (overall["status"], overall["exit_code"]) == ("NOT_ESTABLISHED", 1)


def test_an_event_without_a_compatible_change_is_a_contradiction(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    records = json.loads(json.dumps(meta_records))
    _host_op(records, 5027662)["events"].append(_asset_event("mint", target.account, amount=5))
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    [item] = section["lines"][0]["events_without_change"]
    assert (item["ledger"], item["status"]) == (5027662, "EVENT_WITHOUT_CHANGE")
    assert section["status"] == "CONTRADICTED"
    overall = overall_result(evidence, [], reconciliation=section["status"])
    assert (overall["status"], overall["exit_code"]) == ("CONTRADICTED", 1)


@pytest.mark.parametrize(
    ("change", "expected", "reason"),
    [
        ("amount", "CONTRADICTED", "events net 10000000001"),
        ("account", "UNEXPLAINED", "no asset event of this operation names the account"),
    ],
)
def test_correspondence_is_never_by_amount_alone(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
    change: str,
    expected: str,
    reason: str,
) -> None:
    records = json.loads(json.dumps(meta_records))
    event = _t1_operation(records)["events"][0]["body"]["v0"]
    if change == "amount":
        event["data"]["map"][0]["val"]["i128"] = "10000000001"
    else:
        # The same amount, minted to another account: it explains nothing for this line.
        event["topics"][1]["address"] = OTHER
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    t1 = section["lines"][0]["changes"][1]
    assert t1["status"] == expected and reason in t1["reasons"][0]


def test_creation_and_removal_of_the_line_are_explicit(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    records = json.loads(json.dumps(meta_records))
    # DERIVED: the change_trust of 5027652 removes a line of 0 instead of creating it, and
    # T1 creates it; only ledger 5027652 is examined.
    creation = _ops(records, CHANGE_TRUST_TX)[0]
    creation["changes"] = [
        _line_change("state", target.account, 0),
        _line_change("removed", target.account, 0),
        *[c for c in creation["changes"] if "created" not in c],
    ]
    t1 = _t1_operation(records)
    t1["changes"] = [_line_change("created", target.account, 10000000000)]
    section = _reconcile(
        evidence,
        _derived(evidence, xdr, records, trusted_replay),
        target,
        start=datetime(2026, 10, 5, 0, 57, 25, tzinfo=UTC),
        end=datetime(2026, 10, 5, 0, 57, 30, tzinfo=UTC),
    )
    [line] = section["lines"]
    assert _changes(section) == [(5027652, "removed", 0, "EXPLAINED")]
    assert "change_trust" in line["changes"][0]["reasons"][0]
    assert line["opening"]["state"] == "PRESENT" and line["closing"]["state"] == "ABSENT"
    assert [c["ledger"] for c in line["outside_interval"]] == [5027658, 5027669]
    # T1 creates the line without a change_trust: unexplained (outside this interval).
    assert line["outside_interval"][0]["status"] == "UNEXPLAINED"
    assert section["status"] == "RECONCILED_IN_SCOPE"


def test_a_failed_transaction_never_moves_the_asset(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    records = json.loads(json.dumps(meta_records))
    t2_meta = _processing(records, T2)["tx_apply_processing"]["v4"]
    t2_meta["operations"] = [
        {
            "ext": "v0",
            "changes": [
                _line_change("state", target.account, 10000000000),
                _line_change("updated", target.account, 10000000000),
            ],
            "events": [],
        }
    ]
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    [failed] = [c for c in section["lines"][0]["changes"] if c["tx_hash"] == T2]
    assert failed["status"] == "CONTRADICTED" and "failed" in failed["reasons"][0]
    [tx] = [t for t in section["transactions"] if t["tx_hash"] == T2]
    assert tx["status"] == "CONTRADICTED" and section["status"] == "CONTRADICTED"


@pytest.mark.parametrize("same", [True, False])
def test_repeated_representations_count_once_or_contradict(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
    same: bool,
) -> None:
    records = json.loads(json.dumps(meta_records))
    op = _t1_operation(records)
    repeated = json.loads(json.dumps(op["changes"]))
    if not same:
        repeated[1]["updated"]["data"]["trustline"]["balance"] = "10000000001"
    op["changes"] += repeated
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    [line] = section["lines"]
    t1 = line["changes"][1]
    if same:
        assert t1["duplicate_representations"] == 1 and t1["status"] == "EXPLAINED"
        assert (line["net_variation"], section["status"]) == (20000000000, "RECONCILED_IN_SCOPE")
    else:
        assert t1["status"] == "CONTRADICTED" and section["status"] == "CONTRADICTED"


def test_a_broken_balance_chain_is_a_contradiction(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    records = json.loads(json.dumps(meta_records))
    t3 = _ops(records, T3)[0]["changes"]
    t3[0]["state"]["data"]["trustline"]["balance"] = "10000000003"
    t3[1]["updated"]["data"]["trustline"]["balance"] = "20000000003"
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    [line] = section["lines"]
    # Each change matches its own event; only the chain shows a change the meta lacks.
    assert line["changes"][2]["status"] == "EXPLAINED"
    assert "balance chain broken" in line["contradictions"][0]
    assert section["status"] == "CONTRADICTED"


def test_the_same_code_of_another_issuer_is_never_mixed(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    records = json.loads(json.dumps(meta_records))
    host = _host_op(records, 5027660)
    host["changes"] += [
        _line_change("state", target.account, 0, issuer=OTHER),
        _line_change("updated", target.account, 5, issuer=OTHER),
    ]
    host["events"].append(
        _asset_event("mint", target.account, amount=5, contract=XLM_SAC, asset=f"DEMOA:{OTHER}")
    )
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    out_of_scope = section["out_of_scope"]
    assert out_of_scope["same_code_other_issuer_changes"] == 1
    assert out_of_scope["same_code_other_issuer_events"] == 1
    [line] = section["lines"]
    assert len(line["changes"]) == 3 and line["net_variation"] == 20000000000
    assert section["status"] == "RECONCILED_IN_SCOPE"


@pytest.mark.parametrize(
    ("bounds", "last", "after"),
    [("half_open", 5027668, 5027669), ("closed", 5027669, 5027670)],
)
def test_interval_bounds_follow_the_verified_close_time(
    evidence: CheckpointEvidence, bounds: Any, last: int, after: int
) -> None:
    # Bounds exactly at the close_time of 5027652 (00:57:27) and 5027669 (00:58:52).
    temporal = select_ledgers(
        evidence,
        datetime(2026, 10, 5, 0, 57, 27, tzinfo=UTC),
        datetime(2026, 10, 5, 0, 58, 52, tzinfo=UTC),
        bounds,
    )
    assert (temporal.status, temporal.ledgers[0], temporal.ledgers[-1]) == (
        "COVERED",
        5027652,
        last,
    )
    assert temporal.before_start is not None and temporal.before_start["ledger"] == 5027651
    assert temporal.after_end is not None and temporal.after_end["ledger"] == after


@pytest.mark.parametrize(
    ("start", "end", "problem"),
    [
        ((0, 57, 0), (0, 59, 0), "no verified ledger closes before the start"),
        ((0, 57, 20), (1, 3, 0), "no verified ledger closes at or after the end"),
    ],
)
def test_an_interval_beyond_the_verified_history_is_incomplete(
    evidence: CheckpointEvidence,
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    problem: str,
) -> None:
    section = _reconcile(
        evidence,
        trusted_replay,
        target,
        start=datetime(2026, 10, 5, *start, tzinfo=UTC),
        end=datetime(2026, 10, 5, *end, tzinfo=UTC),
    )
    temporal = section["temporal"]
    assert temporal["status"] == "INCOMPLETE" and problem in temporal["problems"][0]
    # Never widened or narrowed to fit: the examined ledgers are the verified ones inside.
    assert temporal["examined_ledgers"][0] == (5027648 if start == (0, 57, 0) else 5027651)
    assert section["status"] == "INCOMPLETE"
    with pytest.raises(ValueError, match="must follow"):
        select_ledgers(evidence, ORDER_END, ORDER_START)
    with pytest.raises(ValueError, match="whole seconds"):
        select_ledgers(evidence, ORDER_START.replace(microsecond=1), ORDER_END)


def test_unverified_or_unbound_meta_is_never_reconciled(
    tmp_path: Path,
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    altered = _copy_replay(tmp_path)
    (altered / "meta.xdr.gz").write_bytes(gzip.compress(b"\x80\x00\x00\x00", mtime=0))
    section = _reconcile(evidence, verify_replay(evidence, altered, NoDecode(), **TRUSTED), target)
    assert (section["status"], section["lines"]) == ("NOT_ESTABLISHED", [])
    assert "meta is not read" in section["problems"][0]
    # Coherent artifacts without a trusted hash: an identified diagnostic only.
    section = _reconcile(evidence, verify_replay(evidence, REPLAY, xdr), target)
    assert (section["status"], section["evidence_basis"]) == (
        "RECONCILED_UNAUTHENTICATED",
        "unauthenticated_diagnostic",
    )
    # A replay decoded from another stream than the checked artifacts is not read.
    artifacts, replay = trusted_replay
    other = replace(replay, stream_sha256="00" * 32)
    section = _reconcile(evidence, (artifacts, other), target)
    assert section["status"] == "NOT_ESTABLISHED"
    assert "not the stream" in section["problems"][0]
    # Artifacts that did not match are never read, whatever else they carry.
    section = _reconcile(evidence, (replace(artifacts, status="REJECTED"), replay), target)
    assert (section["status"], section["lines"]) == ("NOT_ESTABLISHED", [])


def test_a_muxed_participant_gets_no_identity(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    records = json.loads(json.dumps(meta_records))
    data = _t1_operation(records)["events"][0]["body"]["v0"]["data"]
    data["map"][1]["val"] = {"u64": "42"}  # a sub-account id, not a memo
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    t1 = section["lines"][0]["changes"][1]
    assert t1["status"] == "AMBIGUOUS" and "MUXED_UNATTRIBUTED" in t1["reasons"][-1]
    assert section["status"] == "UNRESOLVED_CHANGES"


def test_a_change_outside_the_interval_is_listed_apart(
    evidence: CheckpointEvidence,
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    section = _reconcile(
        evidence, trusted_replay, target, start=datetime(2026, 10, 5, 0, 58, 0, tzinfo=UTC)
    )
    [line] = section["lines"]
    assert _changes(section) == [(5027669, "updated", 10000000000, "EXPLAINED")]
    assert [c["ledger"] for c in line["outside_interval"]] == [5027652, 5027658]
    opening = line["opening"]
    assert (opening["state"], opening["balance"]) == ("PRESENT", 10000000000)
    assert opening["evidence"]["kind"] == "reconstructed_from_changes"
    assert "ledgers 5027659-5027669" in opening["evidence"]["detail"]
    assert (line["net_variation"], section["status"]) == (10000000000, "RECONCILED_IN_SCOPE")


@pytest.mark.parametrize(
    ("reconciliation", "expected"),
    [
        (None, "CONSISTENT"),
        ("RECONCILED_IN_SCOPE", "CONSISTENT"),
        ("RECONCILED_UNAUTHENTICATED", "COHERENT_UNAUTHENTICATED"),
        ("UNRESOLVED_CHANGES", "NOT_ESTABLISHED"),
        ("INCOMPLETE", "NOT_ESTABLISHED"),
        ("NOT_ESTABLISHED", "NOT_ESTABLISHED"),
        ("CONTRADICTED", "CONTRADICTED"),
    ],
)
def test_the_overall_result_maps_the_reconciliation(
    evidence: CheckpointEvidence, reconciliation: str | None, expected: str
) -> None:
    overall = overall_result(evidence, [], reconciliation=reconciliation)
    assert overall["status"] == expected
    assert overall["exit_code"] == (0 if expected == "CONSISTENT" else 1)


def test_each_link_applies_only_in_its_window(target: ChainTarget) -> None:
    links = [
        *_demo_links(),
        _link("m", encode_muxed_account(OTHER, 7), schema_version="1.1"),
        _link("late", OTHER, valid_from=datetime(2026, 10, 5, 0, 58, tzinfo=UTC)),
        _link("expired", OTHER, valid_to=datetime(2026, 10, 5, 0, 57, tzinfo=UTC)),
        _link("pubnet", OTHER, network="stellar:pubnet"),
        _link("other-ref", OTHER, account_ref="acct-pseudo-0002"),
    ]
    approvals = approved_addresses(
        links, "stellar:testnet", "acct-pseudo-0001", ORDER_START, ORDER_END
    )
    found = {
        (a.address[:6], tuple(a.link_ids), a.status): [
            (w["start"][11:], w["end"][11:]) for w in a.section()["applicable_subintervals"]
        ]
        for a in approvals
    }
    assert found == {
        ("GC6XNJ", ("link-testnet-0001",), "EXAMINED"): [("00:57:20Z", "00:59:00Z")],
        (encode_muxed_account(OTHER, 7)[:6], ("m",), "NOT_EXAMINABLE"): [
            ("00:57:20Z", "00:59:00Z")
        ],
        (OTHER[:6], ("late",), "EXAMINED"): [("00:58:00Z", "00:59:00Z")],
        (OTHER[:6], ("expired", "pubnet"), "NOT_APPLICABLE"): [],
    }
    # A fraction of a second is never rounded into the approval.
    [approval] = approved_addresses(
        [_link("frac", OTHER, valid_from=datetime(2026, 10, 5, 0, 58, 2, 1, tzinfo=UTC))],
        "stellar:testnet",
        "acct-pseudo-0001",
        ORDER_START,
        ORDER_END,
    )
    assert not approval.applies(1791161882) and approval.applies(1791161883)  # 00:58:02/03


def test_an_anchored_payment_missing_from_the_meta_is_a_contradiction(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    # A coordinated removal of T1's change and event: the anchored payment still shows it.
    records = json.loads(json.dumps(meta_records))
    op = _t1_operation(records)
    op["changes"], op["events"] = [], []
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    [line] = section["lines"]
    assert any("anchored payment moves 10000000000" in c for c in line["contradictions"])
    assert section["status"] == "CONTRADICTED"


def test_gross_movements_inside_a_changing_operation_are_kept(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    # DERIVED: T1's operation also moves 7 in and 7 out; the meta still shows one net change.
    records = json.loads(json.dumps(meta_records))
    _t1_operation(records)["events"] += [
        _asset_event("transfer", OTHER, target.account, amount=7),
        _asset_event("transfer", target.account, OTHER, amount=7),
    ]
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    [line] = section["lines"]
    t1 = line["changes"][1]
    assert (t1["delta"], t1["gross_in"], t1["gross_out"], t1["status"]) == (
        10000000000,
        10000000007,
        7,
        "EXPLAINED",
    )
    assert (line["net_variation"], line["gross_in"], line["gross_out"]) == (
        20000000000,
        20000000007,
        7,
    )


def test_a_coordinated_meta_alteration_is_caught_by_the_anchored_payment(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    # DERIVED: T1's change and event say 10000000001, and T3 follows on: the meta agrees
    # with itself, only the anchored payment (1000 DEMOA) contradicts it.
    records = json.loads(json.dumps(meta_records))
    op = _t1_operation(records)
    op["changes"][1]["updated"]["data"]["trustline"]["balance"] = "10000000001"
    op["events"][0]["body"]["v0"]["data"]["map"][0]["val"]["i128"] = "10000000001"
    t3 = _ops(records, T3)[0]["changes"]
    t3[0]["state"]["data"]["trustline"]["balance"] = "10000000001"
    t3[1]["updated"]["data"]["trustline"]["balance"] = "20000000001"
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    [line] = section["lines"]
    assert line["contradictions"] == []
    t1 = line["changes"][1]
    assert (
        t1["status"] == "CONTRADICTED" and "anchored payment moves 10000000000" in t1["reasons"][0]
    )
    assert section["status"] == "CONTRADICTED"


def test_the_opening_absence_is_reconstructed_never_inferred_from_a_creation(
    evidence: CheckpointEvidence,
    xdr: CachedCli,
    meta_records: list[dict[str, Any]],
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    opening = _reconcile(evidence, trusted_replay, target)["lines"][0]["opening"]
    evidence_ = opening["evidence"]
    assert (opening["state"], evidence_["kind"]) == ("ABSENT", "reconstructed_from_changes")
    assert "ledgers 5027651-5027652" in evidence_["detail"]
    assert evidence_["observed_initial_state"].startswith("not available")
    assert "not authenticated" in evidence_["depends_on"]
    # DERIVED: a reference to the line outside the locations read (an evicted key in
    # 5027651). The sequence of changes is no longer shown complete, so the later creation
    # alone does not establish the absence.
    records = json.loads(json.dumps(meta_records))
    for record in records:
        if record["v2"]["ledger_header"]["header"]["ledger_seq"] == 5027651:
            record["v2"]["evicted_keys"].append(
                _line_change("removed", target.account, 0)["removed"]
            )
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    opening = section["lines"][0]["opening"]
    assert (opening["state"], opening["evidence"]["kind"]) == (
        "NOT_ESTABLISHED",
        "inferred_from_a_change_only",
    )
    assert opening["evidence"]["rejected_state"] == {"state": "ABSENT", "balance": None}
    assert section["status"] == "UNRESOLVED_CHANGES"
    # DERIVED: a creation over an entry the meta shows as existing is a contradiction.
    records = json.loads(json.dumps(meta_records))
    creation = _ops(records, CHANGE_TRUST_TX)[0]
    creation["changes"].insert(0, _line_change("state", target.account, 0))
    section = _reconcile(evidence, _derived(evidence, xdr, records, trusted_replay), target)
    first = section["lines"][0]["changes"][0]
    assert (first["kind"], first["status"]) == ("created", "CONTRADICTED")
    assert section["status"] == "CONTRADICTED"


def test_an_approval_never_applies_outside_its_window(
    evidence: CheckpointEvidence,
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    # DERIVED: GC6X approved only from 00:58:00 (its line is examined from ledger 5027659).
    late = [_link("late", target.account, valid_from=datetime(2026, 10, 5, 0, 58, tzinfo=UTC))]
    section = _reconcile(evidence, trusted_replay, target, links=late)
    [line] = section["lines"]
    assert line["examined_ledgers"] == [5027659, 5027670]
    assert _changes(section) == [(5027669, "updated", 10000000000, "EXPLAINED")]
    assert [c["ledger"] for c in line["outside_interval"]] == [5027652, 5027658]
    assert line["opening"]["state"] == "PRESENT" and line["opening"]["balance"] == 10000000000
    assert [t["tx_hash"] for t in section["transactions"]] == [T2, T3]
    assert section["status"] == "RECONCILED_IN_SCOPE"
    # Attributed to the account only until 00:58:00: T3 is outside the approval.
    early = [_link("early", target.account, valid_to=datetime(2026, 10, 5, 0, 58, tzinfo=UTC))]
    section = _reconcile(evidence, trusted_replay, target, links=early)
    assert [c[0] for c in _changes(section)] == [5027652, 5027658]
    assert section["lines"][0]["examined_ledgers"] == [5027651, 5027658]


def test_one_reconciled_address_is_not_every_approved_address(
    evidence: CheckpointEvidence,
    trusted_replay: tuple[ReplayArtifacts, ReplayEvidence],
    target: ChainTarget,
) -> None:
    links = [*_demo_links(), _link("other", OTHER)]
    section = _reconcile(evidence, trusted_replay, target, links=links)
    assert [line["status"] for line in section["lines"]] == ["RECONCILED", "INCOMPLETE"]
    assert section["status"] == "INCOMPLETE"
    muxed = [*_demo_links(), _link("m", encode_muxed_account(target.account, 7))]
    section = _reconcile(
        evidence,
        trusted_replay,
        target,
        links=[*muxed[:1], muxed[1].model_copy(update={"schema_version": "1.1"})],
    )
    assert [line["status"] for line in section["lines"]] == ["RECONCILED"]
    assert section["status"] == "INCOMPLETE"
    assert any("not reconciled" in p for p in section["problems"])
