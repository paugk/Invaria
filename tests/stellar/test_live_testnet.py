"""Opt-in integration test against the public Stellar testnet (network required).

Run with:  INVARIA_LIVE_TESTNET=1 uv run --locked pytest -m live_testnet tests/stellar
Testnet is reset periodically; after a reset the historical samples no longer exist and
this test skips instead of failing.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from invaria.contracts.base import parse_contract
from invaria.contracts.chain import ChainTarget
from invaria.contracts.observation import TokenMovementPayload
from invaria.stellar.adapter import ingest_horizon_payments, ingest_sac_events
from invaria.stellar.http import UrllibClient
from invaria.stellar.sources import DataUnavailable, Endpoints, Horizon, Rpc, verify_network
from invaria.stellar.store import IngestStore

pytestmark = [
    pytest.mark.live_testnet,
    pytest.mark.skipif(
        os.environ.get("INVARIA_LIVE_TESTNET") != "1",
        reason="set INVARIA_LIVE_TESTNET=1 to query Stellar testnet",
    ),
]
TARGET = Path(__file__).resolve().parents[1] / "fixtures/stellar/targets/usdc-issuer.json"


def test_live_issuer_burn_matches_recording(tmp_path: Path) -> None:
    endpoints = Endpoints.resolve(None, None)
    client = UrllibClient()
    horizon, rpc = Horizon(endpoints.horizon_url, client), Rpc(endpoints.rpc_url, client)
    check = verify_network(horizon, rpc, "stellar:testnet")
    if check.horizon_elder_ledger > 5015930:
        pytest.skip("testnet history no longer contains the recorded sample (reset?)")
    target = parse_contract(ChainTarget, TARGET.read_text("utf-8"))
    store = IngestStore(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    classic = ingest_horizon_payments(
        target,
        horizon,
        store,
        start_ledger=5015930,
        end_ledger=5015945,
        history=(check.horizon_elder_ledger, check.horizon_latest_ledger),
        recorded_at=now,
    )
    (burn,) = classic.appended
    assert isinstance(burn.payload, TokenMovementPayload)
    assert burn.payload.chain.tx_hash.startswith("b1c46a5e")
    assert burn.payload.units.atoms == "200000000"
    try:
        sac = ingest_sac_events(
            target, rpc, horizon, store, start_ledger=5015930, end_ledger=5015945, recorded_at=now
        )
    except DataUnavailable:
        pytest.skip("ledger range is now outside RPC event retention")
    assert sac.duplicates == 1 and sac.appended == ()
