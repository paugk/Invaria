"""Two contradictory Classic copies of one execution (Stellar review H9).

Horizon lists the same payment (same transaction, operation and record key) twice with
different amounts. The SYNTHETIC network of ``build_muxed.py`` serves both copies in
memory, in either order: nothing here exists on any network and no recording is written.
The engine case derives a contradictory copy of the real T1 of the DEMOA demo.

What must hold, in both orders: neither copy is chosen (both stay, as a source conflict,
and the SAC event corroborates neither), the amount is never counted twice, and the
conclusion does not depend on the order of ingestion.
"""

from __future__ import annotations

import dataclasses
import json
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from invaria.contracts.base import parse_contract
from invaria.contracts.chain import ChainTarget
from invaria.contracts.observation import Observation, TokenMovementPayload
from invaria.engine.common import resolve_records
from invaria.engine.evaluate import EvaluationInputs, evaluate
from invaria.stellar.adapter import ingest_horizon_payments, ingest_sac_events
from invaria.stellar.sources import Horizon, Rpc, verify_network
from invaria.stellar.store import IngestStore
from invaria.vertical_testnet import OnchainRun, run_testnet_vertical

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
sys.path.insert(0, str(FIXTURES / "stellar/synthetic"))
import build_muxed as synthetic  # type: ignore[import-not-found]  # noqa: E402

CORPUS = FIXTURES / "corpus/subscription-testnet-1.4.0"
STELLAR = FIXTURES / "stellar"
MAPPINGS = FIXTURES / "corpus/subscription-synthetic/mappings"
RECORDED_AT = datetime(2026, 10, 8, tzinfo=UTC)
TOKEN = "subscription.token_units_vs_order"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_: Any, **__: Any) -> Any:
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


class TwoCopies(synthetic.SyntheticNetwork):  # type: ignore[misc]
    """One payment listed twice by Horizon (1 and 2 SYNUSD); its SAC event says 1."""

    def __init__(self, first: str) -> None:
        one = synthetic.Case("copies", 90000520, amount="1.0000000").event(None)
        two = synthetic.Case("copies", 90000520, amount="2.0000000")
        self.cases = [one, two] if first == "1" else [two, one]


def ingest(first: str, tmp_path: Path) -> tuple[list[Observation], int]:
    network = TwoCopies(first)
    target = parse_contract(ChainTarget, json.dumps(synthetic.target_document()))
    horizon = Horizon(synthetic.HORIZON_URL, network)
    rpc = Rpc(synthetic.RPC_URL, network)
    check = verify_network(horizon, rpc, "stellar:testnet")
    store = IngestStore(tmp_path / f"first-{first}")
    span = {"start_ledger": synthetic.START, "end_ledger": synthetic.END}
    ingest_horizon_payments(
        target,
        horizon,
        store,
        **span,
        history=(check.horizon_elder_ledger, check.horizon_latest_ledger),
        recorded_at=RECORDED_AT,
        links=(),
        page_limit=3,
    )
    sac = ingest_sac_events(target, rpc, horizon, store, **span, recorded_at=RECORDED_AT, links=())
    return store.observations(), sac.duplicates


def readings(observations: list[Observation]) -> list[tuple[str, str, str]]:
    """What each copy says, independent of the observation id (which names the page)."""
    return sorted(
        (o.source.record_key, o.provenance.mapping_ref, o.payload.units.atoms)
        for o in observations
        if isinstance(o.payload, TokenMovementPayload)
    )


def test_the_adapter_keeps_both_copies_and_chooses_neither(tmp_path: Path) -> None:
    results = {first: ingest(first, tmp_path) for first in ("1", "2")}
    for observations, duplicates in results.values():
        classic = [
            o for o in observations if o.provenance.mapping_ref.startswith("stellar-classic")
        ]
        assert sorted(o.payload.units.atoms for o in classic) == ["10000000", "20000000"]  # type: ignore[union-attr]
        # The event agrees with one copy, but a record key with two contents is never
        # corroborated (that would choose it): no SAC duplicate is counted, and the event is
        # read by itself under the same record key, joining the conflict.
        assert duplicates == 0
        (key,) = {o.source.record_key for o in observations}
        groups = resolve_records(observations)
        assert [(state, len(members)) for state, members in groups] == [("conflict", 3)]
        assert all(o.source.record_key == key for _, members in groups for o in members)
    # The same readings whatever Horizon listed first.
    assert readings(results["1"][0]) == readings(results["2"][0])


@pytest.fixture(scope="module")
def runs() -> dict[str, OnchainRun]:
    return {r.scenario.scenario_id: r for r in run_testnet_vertical(CORPUS, STELLAR, MAPPINGS)}


def with_copy(inputs: EvaluationInputs, atoms: str, *, first: bool) -> EvaluationInputs:
    """A second Classic copy of the linked delivery T1 (same record key, another amount),
    recorded before (``first``) or after the original: a DERIVED input."""
    original = next(
        o
        for o in inputs.observations.values()
        if o.operation_ref == inputs.snapshot.operation_ref
        and isinstance(o.payload, TokenMovementPayload)
    )
    assert isinstance(original.payload, TokenMovementPayload)
    units = original.payload.units.model_copy(update={"atoms": atoms})
    copy = original.model_copy(
        update={
            "observation_id": "obs-derived-classic-copy",
            "payload": original.payload.model_copy(update={"units": units}),
            "provenance": original.provenance.model_copy(
                update={"raw_locator": original.provenance.raw_locator + "#copy"}
            ),
        }
    )
    observations = dict(inputs.observations)
    if first:  # the copy recorded first, the original after it
        observations = {copy.observation_id: copy, **observations}
    else:
        observations[copy.observation_id] = copy
    ids = sorted([*inputs.snapshot.observation_ids, copy.observation_id])
    return dataclasses.replace(
        inputs,
        snapshot=inputs.snapshot.model_copy(update={"observation_ids": ids}),
        observations=observations,
    )


@pytest.mark.parametrize("scenario", ["TN-LINKED", "TN-OVER-LINKED"])
@pytest.mark.parametrize("atoms", ["5000000000", "20000000000"])  # 500 or 2,000 DEMOA
def test_the_engine_neither_chooses_a_copy_nor_counts_both(
    runs: dict[str, OnchainRun], scenario: str, atoms: str
) -> None:
    """With T1 copied at another amount the delivery is a source conflict: never a MATCH on
    the original, never a short or excess computed on either copy or on their sum, and the
    same conclusion in both orders."""
    base = runs[scenario].inputs
    outcomes = set()
    for first in (True, False):
        result = evaluate(with_copy(base, atoms, first=first)).result
        token = next(c for c in result.controls if c.control_id == TOKEN)
        assert (result.result, token.status, token.reason_code) == (
            "UNKNOWN",
            "UNKNOWN",
            "SOURCE_CONFLICT",
        )
        assert token.delta is None  # nothing was summed or compared
        assert "obs-derived-classic-copy" in token.evidence_refs
        outcomes.add(json.dumps(result.model_dump(mode="json"), sort_keys=True))
    assert len(outcomes) == 1
