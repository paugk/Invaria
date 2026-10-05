"""SUB-0001 evaluated with real (recorded) Stellar testnet evidence, fully offline."""

from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest

from invaria.bundle import build_bundle, verify_bundle
from invaria.cli import main
from invaria.contracts.observation import TokenMovementPayload
from invaria.vertical_testnet import OnchainRun, run_testnet_vertical

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CORPUS = FIXTURES / "corpus/subscription-testnet"
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
