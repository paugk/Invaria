"""A quarantine leaves UNKNOWN only the controls its records could change.

Subscription engine 0.5.0 against 0.4.0, both retired and run here as
compatibility implementations through ``replay``, on the real DEMOA evidence of the
testnet demo (profile 1.1.0, which scopes the on-chain quarantine). These tests pin what
0.5.0 did, including what the declared policy corrects: it applied the per-control split
under 1.1.0, whose promise was that any record bearing on the operation blocks, and it trusted each
record's nature and ledger operation as written. The current engine is tested in
``test_declared_quarantine.py``. The quarantined records
are written into the chain certificate by the test: they are DERIVED inputs (the shapes the
adapter writes, ``QuarantinedRecord``), not recorded samples; the short delivery is derived
too, by lowering the units of the real linked delivery T1. Each case states what the record
establishes (``nature``), on which ledger operation, and why that can or cannot change each
conclusion.
"""

from __future__ import annotations

import json
import random
import socket
from pathlib import Path
from typing import Any

import pytest

from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.evaluation import EvaluationResult
from invaria.contracts.observation import CashPayload, TokenMovementPayload
from invaria.contracts.profile import parse_profile
from invaria.engine.evaluate import EvaluationInputs, replay
from invaria.vertical_testnet import OnchainRun, run_testnet_vertical

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CORPUS = FIXTURES / "corpus/subscription-testnet-1.1.0"
STELLAR = FIXTURES / "stellar"
MAPPINGS = FIXTURES / "corpus/subscription-synthetic/mappings"
PROFILE = parse_profile((CORPUS / "profile.json").read_text("utf-8"))
INVESTOR = "GC6XNJNZOULFUZJBYORCTVNWI6UX5AVOEDC7EXDQM77BPFMQA3VWOYLK"
DEMOA_ISSUER = "GCGGXYAKBIOKOIMVSUTXPEHRLJJU7VB7ZIYYKUFXYIVZTTFIWKEKTBEP"
STRANGER = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
# A ledger operation other than any delivery (derived: no such transaction is claimed).
ELSEWHERE = "ab" * 32 + ":0"
TOKEN = "subscription.token_units_vs_order"
OLD = "invaria-engine@0.4.0"
NEW = "invaria-engine@0.5.0"


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


def record(
    nature: str | None,
    chain_operation: str | None = ELSEWHERE,
    party: str | None = INVESTOR,
    links: list[str] | None = None,
    label: str = "0",
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "locator": f"rpc:getEvents#derived-{label}",
        "reasons": ["CORRESPONDENCE_CONFLICT"],
        "addresses": None if party is None else sorted({party, DEMOA_ISSUER}),
    }
    if nature is not None:
        entry["nature"] = nature
    if chain_operation is not None:
        entry["chain_operation"] = chain_operation
    if links is not None:
        entry["execution_links"] = links
    return entry


def delivered_op(run: OnchainRun) -> str:
    """The ledger operation of a counted delivery (T1, linked to SUB-0001)."""
    observation = next(
        o
        for o in run.inputs.observations.values()
        if o.operation_ref == "SUB-0001" and isinstance(o.payload, TokenMovementPayload)
    )
    assert isinstance(observation.payload, TokenMovementPayload)
    chain = observation.payload.chain
    return f"{chain.tx_hash}:{chain.operation_index}"


def shortened(run: OnchainRun) -> EvaluationInputs:
    """TN-LINKED with the linked delivery lowered to 400 of the 1,000 DEMOA ordered."""
    observations = dict(run.inputs.observations)
    for key, o in observations.items():
        if o.operation_ref == "SUB-0001" and isinstance(o.payload, TokenMovementPayload):
            units = o.payload.units.model_copy(update={"atoms": "4000000000"})
            observations[key] = o.model_copy(
                update={"payload": o.payload.model_copy(update={"units": units})}
            )
    return EvaluationInputs(
        snapshot=run.inputs.snapshot,
        profile=run.inputs.profile,
        observations=observations,
        coverage=run.inputs.coverage,
        identity_links=run.inputs.identity_links,
    )


def with_records(inputs: EvaluationInputs, records: list[dict[str, Any]]) -> EvaluationInputs:
    """``inputs`` with the latest on-chain certificate carrying ``records``."""
    chain_id = max(
        (c.recorded_at, i) for i, c in inputs.coverage.items() if c.source_id == "stellar-testnet"
    )[1]
    chain = inputs.coverage[chain_id]
    altered = CoverageCertificate.model_validate_json(
        json.dumps(
            {
                **chain.model_dump(mode="json"),
                "records_received": chain.records_received + len(records),
                "records_quarantined": len(records),
                "quarantined_records": records,
            }
        )
    )
    return EvaluationInputs(
        snapshot=inputs.snapshot,
        profile=inputs.profile,
        observations=inputs.observations,
        coverage={**inputs.coverage, chain_id: altered},
        identity_links=inputs.identity_links,
    )


def both(inputs: EvaluationInputs) -> tuple[EvaluationResult, EvaluationResult]:
    """(retired 0.5.0, retired 0.4.0) on the same inputs."""
    return replay(inputs, NEW).result, replay(inputs, OLD).result


def token(result: EvaluationResult) -> tuple[str, str]:
    control = next(c for c in result.controls if c.control_id == TOKEN)
    return control.status, control.reason_code


def test_the_base_scenarios(runs: dict[str, OnchainRun]) -> None:
    assert runs["TN-LINKED"].inputs.profile.profile_ref == PROFILE.profile_ref
    assert replay(runs["TN-LINKED"].inputs, NEW).result.result == "MATCH"
    assert replay(runs["TN-OVER-LINKED"].inputs, NEW).result.result == "BREAK"
    short = replay(shortened(runs["TN-LINKED"]), NEW).result
    assert (short.result, token(short)) == ("BREAK", ("FAIL", "UNITS_MISMATCH"))


@pytest.mark.parametrize("scenario", ["short", "excess"])
def test_an_identified_clawback_with_incomplete_correspondence_keeps_the_break(
    runs: dict[str, OnchainRun], scenario: str
) -> None:
    """Established: the event identifies a clawback, of this asset, from the investor, on a
    ledger operation that is not a delivery. Open: its Classic/SAC correspondence. A
    clawback neither completes nor undoes a delivery, so the demonstrated short or excess
    stays a BREAK; the record stays quarantined. 0.4.0 lost the BREAK."""
    base = shortened(runs["TN-LINKED"]) if scenario == "short" else runs["TN-OVER-LINKED"].inputs
    inputs = with_records(base, [record("clawback_identified")])
    new, old = both(inputs)
    assert (new.result, token(new)) == ("BREAK", ("FAIL", "UNITS_MISMATCH"))
    assert (old.result, token(old)) == ("UNKNOWN", ("UNKNOWN", "QUARANTINED_INPUT"))
    control = next(c for c in new.controls if c.control_id == TOKEN)
    assert "1 identified clawback(s) change no delivery" in control.reason
    (certificate,) = [
        c for c in inputs.coverage.values() if c.quarantined_records and c.records_quarantined
    ]
    assert certificate.coverage_id in control.evidence_refs  # weighed, and cited
    assert certificate.quarantined_records is not None
    assert certificate.quarantined_records[0].reasons == ["CORRESPONDENCE_CONFLICT"]


def test_an_identified_clawback_does_not_block_an_exact_delivery(
    runs: dict[str, OnchainRun],
) -> None:
    """The same reasoning on an exact delivery: nothing the clawback could be changes it, so
    the comparison passes (0.4.0: UNKNOWN). Quality of evidence stays visible: the record is
    in the certificate and named in the control's reason."""
    new, old = both(with_records(runs["TN-LINKED"].inputs, [record("clawback_identified")]))
    assert (new.result, token(new)) == ("MATCH", ("PASS", "EXACT_MATCH"))
    assert old.result == "UNKNOWN"


@pytest.mark.parametrize("nature", ["unresolved", "movement_identified", None])
def test_a_movement_that_could_complete_the_delivery_leaves_it_unknown(
    runs: dict[str, OnchainRun], nature: str | None
) -> None:
    """An unresolved movement involving the investor (or an identified movement no profile
    admits) could be the rest of a short delivery, or turn an exact one into an excess:
    UNKNOWN in both engines."""
    for base in (shortened(runs["TN-LINKED"]), runs["TN-LINKED"].inputs):
        new, old = both(with_records(base, [record(nature)]))
        assert (new.result, token(new)) == ("UNKNOWN", ("UNKNOWN", "QUARANTINED_INPUT"))
        assert old.result == "UNKNOWN"


def test_an_unresolved_movement_cannot_undo_a_proven_excess(runs: dict[str, OnchainRun]) -> None:
    """It could only add deliveries: the excess stays a BREAK (0.4.0: UNKNOWN)."""
    new, old = both(with_records(runs["TN-OVER-LINKED"].inputs, [record("unresolved")]))
    assert (new.result, token(new)) == ("BREAK", ("FAIL", "UNITS_MISMATCH"))
    assert old.result == "UNKNOWN"
    control = next(c for c in new.controls if c.control_id == TOKEN)
    assert "1 could only add to an excess" in control.reason


def test_a_contradiction_on_the_clawback_leaves_only_the_delivery_unknown(
    runs: dict[str, OnchainRun],
) -> None:
    """The sources disagree on whether it was a clawback: it could be a delivery. The token
    control is UNKNOWN; the cash and transfer-agent controls are not affected."""
    new, _ = both(with_records(shortened(runs["TN-LINKED"]), [record("contradictory")]))
    assert (new.result, token(new)) == ("UNKNOWN", ("UNKNOWN", "QUARANTINED_INPUT"))
    others = {c.control_id: c.status for c in new.controls if c.control_id != TOKEN}
    assert set(others.values()) == {"PASS"}
    over, _ = both(with_records(runs["TN-OVER-LINKED"].inputs, [record("contradictory")]))
    assert (over.result, token(over)) == ("BREAK", ("FAIL", "UNITS_MISMATCH"))


def test_a_record_on_a_counted_delivery_puts_the_delivery_itself_in_doubt(
    runs: dict[str, OnchainRun],
) -> None:
    """On the ledger operation of a counted delivery, or on an unknown one (an earlier
    certificate), even an identified clawback leaves every comparison UNKNOWN, an excess
    included: the delivered amount itself could be wrong."""
    run = runs["TN-OVER-LINKED"]
    for entry in (
        record("clawback_identified", chain_operation=delivered_op(run)),
        record("clawback_identified", chain_operation=None),
        record(None, chain_operation=None),
    ):
        new, _ = both(with_records(run.inputs, [entry]))
        assert (new.result, token(new)) == ("UNKNOWN", ("UNKNOWN", "QUARANTINED_INPUT"))


@pytest.mark.parametrize("nature", ["clawback_identified", "unresolved", "contradictory"])
def test_a_foreign_record_changes_nothing(runs: dict[str, OnchainRun], nature: str) -> None:
    for run, expected in ((runs["TN-LINKED"], "MATCH"), (runs["TN-OVER-LINKED"], "BREAK")):
        new, old = both(with_records(run.inputs, [record(nature, party=STRANGER)]))
        assert new.result == old.result == expected


def test_an_execution_link_is_weighed_without_erasing_known_addresses(
    runs: dict[str, OnchainRun],
) -> None:
    """A record with known addresses that are not the investor's, on a ledger operation an
    ExecutionLink names as SUB-0001's execution: relevant to SUB-0001 because of the link,
    not to another operation; the addresses stay as written. 0.4.0 reads the link as
    unknown parties (what its adapter wrote), so it affects every operation."""
    linked = record("unresolved", party=STRANGER, links=["SUB-0001"])
    elsewhere = record("unresolved", party=STRANGER, links=["SUB-9999"])
    inputs = with_records(runs["TN-LINKED"].inputs, [linked])
    new, old = both(inputs)
    assert (new.result, token(new)) == ("UNKNOWN", ("UNKNOWN", "QUARANTINED_INPUT"))
    assert old.result == "UNKNOWN"
    certificate = next(c for c in inputs.coverage.values() if c.quarantined_records)
    assert certificate.quarantined_records is not None
    kept = certificate.quarantined_records[0]
    assert (kept.addresses, kept.execution_links) == (
        sorted({STRANGER, DEMOA_ISSUER}),
        ["SUB-0001"],
    )
    new, old = both(with_records(runs["TN-LINKED"].inputs, [elsewhere]))
    assert new.result == "MATCH"  # linked to another operation, foreign addresses
    assert old.result == "UNKNOWN"
    # An identified clawback that an ExecutionLink names as SUB-0001's execution
    # contradicts the chain: the linked execution may be another delivery. Short ⇒
    # UNKNOWN; an excess stays a BREAK; linked to another operation it is neutral.
    clawback = record("clawback_identified", party=STRANGER, links=["SUB-0001"])
    new, _ = both(with_records(shortened(runs["TN-LINKED"]), [clawback]))
    assert (new.result, token(new)) == ("UNKNOWN", ("UNKNOWN", "QUARANTINED_INPUT"))
    new, _ = both(with_records(runs["TN-OVER-LINKED"].inputs, [clawback]))
    assert (new.result, token(new)) == ("BREAK", ("FAIL", "UNITS_MISMATCH"))
    clawback_elsewhere = record("clawback_identified", party=INVESTOR, links=["SUB-9999"])
    new, _ = both(with_records(shortened(runs["TN-LINKED"]), [clawback_elsewhere]))
    assert (new.result, token(new)) == ("BREAK", ("FAIL", "UNITS_MISMATCH"))


def test_a_break_of_another_control_survives_an_undecided_delivery(
    runs: dict[str, OnchainRun],
) -> None:
    """Cash short of the order is a BREAK, whatever the delivery comparison becomes."""
    run = runs["TN-LINKED"]
    observations = dict(run.inputs.observations)
    for key, o in observations.items():
        if isinstance(o.payload, CashPayload):
            amount = o.payload.amount.model_copy(update={"atoms": "1"})
            observations[key] = o.model_copy(
                update={"payload": o.payload.model_copy(update={"amount": amount})}
            )
    base = EvaluationInputs(
        snapshot=run.inputs.snapshot,
        profile=run.inputs.profile,
        observations=observations,
        coverage=run.inputs.coverage,
        identity_links=run.inputs.identity_links,
    )
    new, old = both(with_records(base, [record("unresolved")]))
    assert token(new) == ("UNKNOWN", "QUARANTINED_INPUT")
    cash = next(c for c in new.controls if c.control_id == "subscription.cash_vs_order")
    assert cash.status == "FAIL"
    assert new.result == old.result == "BREAK"


def test_the_conclusion_does_not_depend_on_order_or_certificate_selection(
    runs: dict[str, OnchainRun],
) -> None:
    """The same evidence in another order (snapshot members, stores, quarantined records),
    and with an earlier certificate of the source added, gives the same conclusion."""
    base = with_records(
        shortened(runs["TN-LINKED"]),
        [
            record("clawback_identified", label="a"),
            record("unresolved", party=STRANGER, label="b"),
            record("contradictory", party=STRANGER, links=["SUB-9999"], label="c"),
        ],
    )
    expected = replay(base, NEW).result
    assert (expected.result, token(expected)) == ("BREAK", ("FAIL", "UNITS_MISMATCH"))
    shuffle = random.Random(13)
    chain_id = max(
        (c.recorded_at, i) for i, c in base.coverage.items() if c.source_id == "stellar-testnet"
    )[1]
    latest = base.coverage[chain_id]
    assert latest.quarantined_records is not None
    earlier = latest.model_copy(
        update={
            "coverage_id": "cov-earlier-derived",
            "recorded_at": latest.recorded_at.replace(year=latest.recorded_at.year - 1),
            "records_quarantined": 0,
            "quarantined_records": [],
        }
    )
    for _ in range(5):
        records = list(latest.quarantined_records)
        shuffle.shuffle(records)
        coverage = dict(base.coverage)
        coverage[chain_id] = latest.model_copy(update={"quarantined_records": records})
        coverage[earlier.coverage_id] = earlier
        members = list(base.snapshot.observation_ids)
        shuffle.shuffle(members)
        certificates = [*base.snapshot.coverage_ids, earlier.coverage_id]
        shuffle.shuffle(certificates)
        observations = list(base.observations.items())
        shuffle.shuffle(observations)
        coverage_items = list(coverage.items())
        shuffle.shuffle(coverage_items)
        inputs = EvaluationInputs(
            snapshot=base.snapshot.model_copy(
                update={"observation_ids": members, "coverage_ids": certificates}
            ),
            profile=base.profile,
            observations=dict(observations),
            coverage=dict(coverage_items),
            identity_links=base.identity_links,
        )
        result = replay(inputs, NEW).result
        assert (result.result, result.controls) == (expected.result, expected.controls)


def test_an_earlier_certificate_is_read_as_conservatively_as_before(
    runs: dict[str, OnchainRun],
) -> None:
    """Records without ``nature`` or ``chain_operation`` (written before subscription 0.5.0)
    keep the earlier outcome under 0.5.0: UNKNOWN whenever they bear on the operation."""
    legacy = {
        "locator": "rpc:getEvents#legacy",
        "reasons": ["CORRESPONDENCE_CONFLICT"],
        "addresses": [DEMOA_ISSUER, INVESTOR],
    }
    for base in (runs["TN-LINKED"].inputs, runs["TN-OVER-LINKED"].inputs):
        new, old = both(with_records(base, [legacy]))
        assert new.result == old.result == "UNKNOWN"


def without_deliveries(run: OnchainRun) -> EvaluationInputs:
    """TN-LINKED without any movement to the investor in the snapshot (derived)."""
    members = [
        i
        for i in run.inputs.snapshot.observation_ids
        if not isinstance(run.inputs.observations[i].payload, TokenMovementPayload)
    ]
    return EvaluationInputs(
        snapshot=run.inputs.snapshot.model_copy(update={"observation_ids": members}),
        profile=run.inputs.profile,
        observations=run.inputs.observations,
        coverage=run.inputs.coverage,
        identity_links=run.inputs.identity_links,
    )


def with_gap(inputs: EvaluationInputs) -> EvaluationInputs:
    """The latest on-chain certificate declaring a gap inside its interval (derived)."""
    chain_id = max(
        (c.recorded_at, i) for i, c in inputs.coverage.items() if c.source_id == "stellar-testnet"
    )[1]
    chain = inputs.coverage[chain_id]
    start = chain.interval.start
    gap = {"start": start.isoformat(), "end": start.replace(second=start.second + 1).isoformat()}
    altered = CoverageCertificate.model_validate_json(
        json.dumps({**chain.model_dump(mode="json"), "gaps": [gap]})
    )
    return EvaluationInputs(
        snapshot=inputs.snapshot,
        profile=inputs.profile,
        observations=inputs.observations,
        coverage={**inputs.coverage, chain_id: altered},
        identity_links=inputs.identity_links,
    )


def test_the_reason_order_is_kept_when_the_comparison_cannot_be_made(
    runs: dict[str, OnchainRun],
) -> None:
    """Without a delivery, or with a coverage gap, the control is UNKNOWN either way. A
    record that could be a delivery is reported first (QUARANTINED_INPUT), as before 0.5.0;
    an identified clawback is not a reason, so the evidence or coverage reason shows."""
    run = runs["TN-LINKED"]
    for base, own_reason in (
        (without_deliveries(run), "MISSING_EVIDENCE"),
        (with_gap(run.inputs), "INSUFFICIENT_COVERAGE"),
    ):
        new, old = both(with_records(base, [record("unresolved")]))
        assert token(new) == token(old) == ("UNKNOWN", "QUARANTINED_INPUT")
        new, old = both(with_records(base, [record("clawback_identified")]))
        assert token(new) == ("UNKNOWN", own_reason)
        assert token(old) == ("UNKNOWN", "QUARANTINED_INPUT")
