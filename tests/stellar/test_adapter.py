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
from invaria.contracts.observation import Observation, TokenMovementPayload
from invaria.stellar.adapter import (
    Exclusion,
    ingest_horizon_payments,
    ingest_sac_events,
    integrate_chain,
    normalize_horizon_record,
    normalize_sac_event,
    resolve_sac_contract,
    toid_parts,
)
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
RECORDED_AT = datetime(2026, 10, 5, 0, 41, tzinfo=UTC)
SAMPLES = {
    "usdc-gclcz": (5024520, 5024600),
    "usdc-issuer": (5015930, 5015945),
    "usdc-gb4mm": (5000475, 5000490),
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
    # unified SAC events: 5 are the same economic effects; 1 carries a u64 to_muxed_id (memo id)
    assert sac.complete and sac.appended == () and sac.duplicates == 5
    assert [e.reason for e in sac.exclusions] == ["MUXED_ACCOUNT"]
    assert len(store.observations()) == 6
    coverage = {c.coverage_id.rsplit("-", 2)[0]: c for c in store.coverage()}
    horizon_cov = coverage["cov-testnet-usdc-gclcz-horizon_payments"]
    assert (horizon_cov.records_received, horizon_cov.records_quarantined) == (6, 0)
    assert horizon_cov.level == "provider_claimed" and horizon_cov.is_gap_free
    assert horizon_cov.ledger_range is not None and horizon_cov.ledger_range.first == 5024520
    sac_cov = coverage["cov-testnet-usdc-gclcz-rpc_sac_events"]
    assert sac_cov.records_quarantined == 1 and not sac_cov.is_gap_free


def test_issuer_burn_is_one_effect_across_paths(tmp_path: Path) -> None:
    store = IngestStore(tmp_path)
    classic, sac = ingest("usdc-issuer", store)
    (burn,) = classic.appended
    payload = movement(burn)
    assert payload.to_address == ISSUER and payload.units.atoms == "200000000"
    assert payload.chain.ledger == 5015939 and payload.chain.operation_index == 0
    assert burn.source.record_key == f"{payload.chain.tx_hash}:0:0"
    assert sac.duplicates == 1 and sac.appended == ()


def test_path_payment_excluded_in_classic_but_sac_movements_kept(tmp_path: Path) -> None:
    store = IngestStore(tmp_path)
    classic, sac = ingest("usdc-gb4mm", store)
    assert classic.appended == () and [e.reason for e in classic.exclusions] == [
        "UNSUPPORTED_OPERATION"
    ]
    assert classic.coverage is not None and not classic.coverage.is_gap_free
    mint, transfer = sac.appended
    assert (movement(mint).from_address, mint.source.record_key[-4:]) == (ISSUER, ":0:0")
    assert movement(transfer).from_address == movement(mint).to_address
    assert transfer.source.record_key[-4:] == ":0:1"  # second effect of the same operation


def test_ingestion_is_deterministic(tmp_path: Path) -> None:
    first, second = IngestStore(tmp_path / "a"), IngestStore(tmp_path / "b")
    ingest("usdc-gclcz", first)
    ingest("usdc-gclcz", second)
    for name in ("observations.jsonl", "coverage.jsonl", "exclusions.jsonl"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


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
    assert sac.appended == () and sac.duplicates == 5
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
    return normalize_horizon_record(target(sample), record, page, 0, RECORDED_AT, {})


def test_failed_transaction_is_observed_without_effect() -> None:
    page, record = _page_and_record("usdc-issuer")
    record["transaction_successful"] = False
    record["transaction"]["successful"] = False
    observation = _normalize("usdc-issuer", record, page)
    assert isinstance(observation, Observation) and not movement(observation).chain.tx_successful


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
        (
            {"to_muxed": "MA7QYNF7SOWQ3GLR2BGMZEHXAVIRZA4KVWLTJJFC7MGXUA74P7UJUAAAAAAAAAAAACJUQ"},
            "MUXED_ACCOUNT",
        ),
        ({"type": "path_payment_strict_send"}, "UNSUPPORTED_OPERATION"),
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


def _scval_contract(raw32: bytes) -> str:
    return base64.b64encode(struct.pack(">ii", 18, 1) + raw32).decode()


def test_real_sac_burn_maps_to_issuer_movement() -> None:
    page, event = _sac_event("usdc-issuer")
    result = normalize_sac_event(target("usdc-issuer"), USDC_SAC, event, 0, page, RECORDED_AT, {})
    assert isinstance(result, Observation)
    assert movement(result).to_address == ISSUER and movement(result).units.atoms == "200000000"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda e: e["topic"].__setitem__(0, _scval_symbol("clawback")), "UNSUPPORTED_EVENT"),
        (lambda e: e["topic"].__setitem__(1, _scval_contract(b"\x07" * 32)), "CONTRACT_PARTY"),
        (
            lambda e: e["topic"].__setitem__(
                1, base64.b64encode(struct.pack(">ii", 18, 4) + b"\x01" * 32).decode()
            ),
            "UNSUPPORTED_PARTY",
        ),
        (lambda e: e["topic"].__setitem__(2, _scval_symbol("EURC:" + ISSUER)), "MALFORMED"),
        (lambda e: e.__setitem__("value", _scval_symbol("x")), "UNSUPPORTED_VALUE"),
    ],
    ids=["clawback", "contract-party", "pool-party", "other-asset-string", "non-i128-value"],
)
def test_sac_event_rules(mutate: Any, expected: str) -> None:
    page, event = _sac_event("usdc-issuer")
    mutate(event)
    result = normalize_sac_event(target("usdc-issuer"), USDC_SAC, event, 0, page, RECORDED_AT, {})
    # the issuer target is involved in every supply change, so each problem is in scope
    assert isinstance(result, Exclusion) and result.reason == expected


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
                "cursor": chunk[-1]["id"] if chunk else (cursor or "0"),
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
