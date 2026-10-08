"""The per-control quarantine is a declared profile rule, certificates stay active
until explicitly superseded within their chain scope, and a record's classification is
relied on only when the snapshot supports it.

Subscription engine 0.6.0 (now 0.8.0, which keeps these rules) on the real DEMOA
evidence of the testnet demo, under the current profile 1.4.0 (which keeps the
``quarantine_policy`` of 1.2.0) and 1.1.0 (does not). Today's adapter writes that evidence
with a mapping 1.1.0 does not admit, so a new evaluation under 1.1.0 is refused;
its promise is shown by replaying the retired 0.7.0, the last engine that evaluated it
(``PROMISE``), whose code path for a profile without a policy is the current one. The
quarantined records, the observations that support or contradict them, the short delivery
and the extra certificates are DERIVED inputs written by these tests (the shapes the
adapter writes), not recorded samples; the adapter's own output is exercised at the end of the file.

The expected outcome of every case is written in ``EXPECTED`` before the engines run; each
case also runs the retired 0.5.0, whose different answer shows what the declared policy changes.
"""

from __future__ import annotations

import dataclasses
import json
import random
import socket
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from invaria.bundle import build_bundle, verify_bundle
from invaria.contracts.coverage import CoverageCertificate, certificate_sha256
from invaria.contracts.evaluation import EvaluationResult
from invaria.contracts.observation import Observation, TokenMovementPayload
from invaria.contracts.profile import admitted_mapping_refs, parse_profile
from invaria.engine.common import active_coverage, chain_evidence, record_support
from invaria.engine.evaluate import ENGINE_REF, EvaluationInputs, evaluate, replay
from invaria.engine.snapshot import build_snapshot
from invaria.vertical_testnet import OnchainRun, run_testnet_vertical

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CORPUS = FIXTURES / "corpus/subscription-testnet-1.4.0"
PREVIOUS = FIXTURES / "corpus/subscription-testnet-1.1.0"
STELLAR = FIXTURES / "stellar"
MAPPINGS = FIXTURES / "corpus/subscription-synthetic/mappings"
PROFILE_1_1_0 = parse_profile((PREVIOUS / "profile.json").read_text("utf-8"))
INVESTOR = "GC6XNJNZOULFUZJBYORCTVNWI6UX5AVOEDC7EXDQM77BPFMQA3VWOYLK"
DEMOA_ISSUER = "GCGGXYAKBIOKOIMVSUTXPEHRLJJU7VB7ZIYYKUFXYIVZTTFIWKEKTBEP"
STRANGER = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
ELSEWHERE_TX = "ab" * 32
ELSEWHERE = f"{ELSEWHERE_TX}:0"  # derived: no such transaction is claimed
LOCATOR = "rpc:getEvents#derived-0"
TOKEN = "subscription.token_units_vs_order"
RETIRED = "invaria-engine@0.5.0"
PROMISE = "invaria-engine@0.7.0"  # replays 1.1.0, which no current evaluation admits

BREAK = ("BREAK", "FAIL", "UNITS_MISMATCH")
MATCH = ("MATCH", "PASS", "EXACT_MATCH")
QUARANTINED = ("UNKNOWN", "UNKNOWN", "QUARANTINED_INPUT")

# Written before running: case -> outcome on (short, exact, excess) deliveries, as
# (result, token control status, its reason). Profile 1.2.0, engine 0.6.0.
EXPECTED: dict[str, tuple[tuple[str, str, str], ...]] = {
    # The snapshot holds a successful clawback of the investor on that ledger operation
    # and nothing else there: a clawback changes no delivery.
    "supported clawback": (BREAK, MATCH, BREAK),
    # An unresolved movement relabelled "clawback_identified": the snapshot shows a movement
    # of unresolved origin on that operation, not a clawback. It could be a delivery.
    "unresolved relabelled as clawback": (QUARANTINED, QUARANTINED, BREAK),
    # Nothing of the snapshot is on the named ledger operation: the operation itself is not
    # supported, so the record could be on a counted delivery.
    "clawback without evidence": (QUARANTINED, QUARANTINED, QUARANTINED),
    # The supporting effect is of another asset, or of another network: foreign evidence.
    "evidence of another asset": (QUARANTINED, QUARANTINED, QUARANTINED),
    "evidence of another network": (QUARANTINED, QUARANTINED, QUARANTINED),
    # The record is read from the raw record of the counted delivery T1 but claims another
    # ledger operation, to look as if it did not touch a counted delivery.
    "operation moved off the delivery": (QUARANTINED, QUARANTINED, QUARANTINED),
    # The record omits the investor, whom the snapshot shows on that operation: it bears on
    # the operation, and its clawback nature is not supported.
    "parties omitted": (QUARANTINED, QUARANTINED, BREAK),
    # Its reasons say it is a movement Horizon did not list, not a clawback.
    "reasons contradict the nature": (QUARANTINED, QUARANTINED, BREAK),
    # Another kind of record (a contract transfer) on the same ledger operation.
    "another kind on the operation": (QUARANTINED, QUARANTINED, BREAK),
}
# What the retired 0.5.0 answered under 1.1.0 for the same records: it trusted the
# certificate, so every relabelled or unsupported "clawback_identified" was neutral.
RETIRED_ON_1_1_0: dict[str, tuple[str, ...]] = {
    "supported clawback": ("BREAK", "MATCH", "BREAK"),
    "unresolved relabelled as clawback": ("BREAK", "MATCH", "BREAK"),
    "clawback without evidence": ("BREAK", "MATCH", "BREAK"),
    "evidence of another asset": ("BREAK", "MATCH", "BREAK"),
    "evidence of another network": ("BREAK", "MATCH", "BREAK"),
    "operation moved off the delivery": ("BREAK", "MATCH", "BREAK"),
    "parties omitted": ("BREAK", "MATCH", "BREAK"),  # not relevant by its addresses
    "reasons contradict the nature": ("BREAK", "MATCH", "BREAK"),
    "another kind on the operation": ("BREAK", "MATCH", "BREAK"),
}


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


# ------------------------------------------------------------------- helpers


def delivery(inputs: EvaluationInputs) -> Observation:
    return next(
        o
        for o in inputs.observations.values()
        if o.operation_ref == "SUB-0001"
        and isinstance(o.payload, TokenMovementPayload)
        and o.observation_id in inputs.snapshot.observation_ids
    )


def shortened(inputs: EvaluationInputs) -> EvaluationInputs:
    """The linked delivery lowered to 400 of the 1,000 DEMOA ordered (derived)."""
    observations = dict(inputs.observations)
    for key, o in observations.items():
        if o.operation_ref == "SUB-0001" and isinstance(o.payload, TokenMovementPayload):
            units = o.payload.units.model_copy(update={"atoms": "4000000000"})
            observations[key] = o.model_copy(
                update={"payload": o.payload.model_copy(update={"units": units})}
            )
    return dataclasses.replace(inputs, observations=observations)


def bases(runs: dict[str, OnchainRun]) -> tuple[EvaluationInputs, ...]:
    """(short, exact, excess) deliveries of SUB-0001."""
    return (
        shortened(runs["TN-LINKED"].inputs),
        runs["TN-LINKED"].inputs,
        runs["TN-OVER-LINKED"].inputs,
    )


def with_profile(inputs: EvaluationInputs, profile: Any) -> EvaluationInputs:
    return dataclasses.replace(
        inputs,
        snapshot=inputs.snapshot.model_copy(
            update={
                "profile_ref": profile.profile_ref,
                "rules_ref": profile.rules_ref,
                "mapping_refs": admitted_mapping_refs(profile.sources),
            }
        ),
        profile=profile,
    )


def record(
    nature: str | None = "clawback_identified",
    chain_operation: str | None = ELSEWHERE,
    parties: list[str] | None = None,
    reasons: list[str] | None = None,
    links: list[str] | None = None,
    locator: str = LOCATOR,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "locator": locator,
        "reasons": reasons or ["CORRESPONDENCE_CONFLICT"],
        "addresses": sorted(parties or [INVESTOR, DEMOA_ISSUER]),
    }
    if nature is not None:
        entry["nature"] = nature
    if chain_operation is not None:
        entry["chain_operation"] = chain_operation
    if links is not None:
        entry["execution_links"] = links
    return entry


def chain_id(inputs: EvaluationInputs) -> str:
    """The SAC certificate (it supersedes the Horizon one of the same range)."""
    return max(
        (c.recorded_at, i) for i, c in inputs.coverage.items() if c.source_id == "stellar-testnet"
    )[1]


def with_records(inputs: EvaluationInputs, records: list[dict[str, Any]]) -> EvaluationInputs:
    cid = chain_id(inputs)
    chain = inputs.coverage[cid]
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
    return dataclasses.replace(inputs, coverage={**inputs.coverage, cid: altered})


def with_effect(
    inputs: EvaluationInputs,
    kind: str = "clawback",
    *,
    tx: str = ELSEWHERE_TX,
    account: str = INVESTOR,
    instrument_id: str | None = None,
    network: str | None = None,
    locator: str = LOCATOR,
    label: str = "0",
) -> EvaluationInputs:
    """One chain effect on ledger operation ``tx``:0 (derived, the adapter's shapes)."""
    template = delivery(inputs)
    assert isinstance(template.payload, TokenMovementPayload)
    chain = {
        **template.payload.chain.model_dump(mode="json"),
        "tx_hash": tx,
        "operation_index": 0,
    }
    if network is not None:
        chain["network"] = network
    payload: dict[str, Any] = {
        "payload_type": "chain_effect",
        "effect_kind": kind,
        "representation": "sac",
        "account": account,
        "direction": "debit",
        "counterparty": {"kind": "account", "id": DEMOA_ISSUER},
        "units": template.payload.units.model_dump(),
        "chain": chain,
        "path_payment": None,
        "transaction": None,
    }
    if kind == "clawback":
        payload["clawback"] = {
            "asset": f"DEMOA:{DEMOA_ISSUER}",
            "issuer": DEMOA_ISSUER,
            "operation_type": None,
            "operation_source": None,
        }
    elif kind == "unresolved_movement":
        payload["unresolved"] = {"reason": "operation_not_found", "operation_type": None}
    elif kind == "contract_transfer":
        payload["counterparty"] = {
            "kind": "contract",
            "id": "CDLZFC3SYJYDZT7K67VZ75HPJVIEUVNIXF47ZG2FB2RMQQVU2HHGCYSC",
        }
    effect = Observation.model_validate_json(
        json.dumps(
            {
                **template.model_dump(mode="json"),
                "observation_id": f"obs-derived-{kind}-{label}",
                "fact_type": "chain_effect",
                "operation_ref": None,
                "instrument_id": instrument_id or template.instrument_id,
                "source": {**template.source.model_dump(), "record_key": f"derived:{kind}:{label}"},
                "provenance": {
                    **template.provenance.model_dump(mode="json"),
                    "raw_locator": locator,
                },
                "payload": payload,
            }
        )
    )
    return dataclasses.replace(
        inputs,
        snapshot=inputs.snapshot.model_copy(
            update={
                "observation_ids": sorted([*inputs.snapshot.observation_ids, effect.observation_id])
            }
        ),
        observations={**inputs.observations, effect.observation_id: effect},
    )


def with_movement(inputs: EvaluationInputs, *, to: str) -> EvaluationInputs:
    """An unlinked payment of the instrument on ledger operation ``ELSEWHERE`` (derived)."""
    template = delivery(inputs)
    assert isinstance(template.payload, TokenMovementPayload)
    payload = template.payload.model_dump(mode="json")
    payload["to_address"] = to
    payload["chain"] = {**payload["chain"], "tx_hash": ELSEWHERE_TX, "operation_index": 0}
    movement = Observation.model_validate_json(
        json.dumps(
            {
                **template.model_dump(mode="json"),
                "observation_id": "obs-derived-movement",
                "operation_ref": None,
                "source": {**template.source.model_dump(), "record_key": "derived:movement"},
                "provenance": {
                    **template.provenance.model_dump(mode="json"),
                    "raw_locator": "horizon:derived-movement",
                },
                "payload": payload,
            }
        )
    )
    return dataclasses.replace(
        inputs,
        snapshot=inputs.snapshot.model_copy(
            update={
                "observation_ids": sorted(
                    [*inputs.snapshot.observation_ids, movement.observation_id]
                )
            }
        ),
        observations={**inputs.observations, movement.observation_id: movement},
    )


def case(name: str, inputs: EvaluationInputs) -> EvaluationInputs:
    """The case ``name`` built on ``inputs``."""
    if name == "supported clawback":
        return with_records(with_effect(inputs), [record()])
    if name == "unresolved relabelled as clawback":
        # A movement between the issuer and a stranger (as an effect it bears on nothing
        # here); its record is relabelled as an identified clawback of the investor.
        unresolved = with_effect(inputs, "unresolved_movement", account=STRANGER)
        return with_records(unresolved, [record(parties=[INVESTOR, STRANGER, DEMOA_ISSUER])])
    if name == "clawback without evidence":
        return with_records(inputs, [record()])
    if name == "evidence of another asset":
        return with_records(with_effect(inputs, instrument_id="testnet:asset:OTHER:X"), [record()])
    if name == "evidence of another network":
        return with_records(with_effect(inputs, network="stellar:pubnet"), [record()])
    if name == "operation moved off the delivery":
        moved = record(locator=delivery(inputs).provenance.raw_locator)
        return with_records(with_effect(inputs, locator="rpc:getEvents#other"), [moved])
    if name == "parties omitted":
        return with_records(with_effect(inputs), [record(parties=[STRANGER, DEMOA_ISSUER])])
    if name == "reasons contradict the nature":
        return with_records(with_effect(inputs), [record(reasons=["NOT_LISTED_BY_HORIZON"])])
    if name == "another kind on the operation":
        # Beside the clawback, a payment from the issuer to a stranger on the same operation.
        both = with_movement(with_effect(inputs), to=STRANGER)
        return with_records(both, [record(parties=[INVESTOR, STRANGER, DEMOA_ISSUER])])
    raise AssertionError(name)


def outcome(result: EvaluationResult) -> tuple[str, str, str]:
    control = next(c for c in result.controls if c.control_id == TOKEN)
    return result.result, control.status, control.reason_code


# ------------------------------------------------------- 1. profile and engine


def test_the_demo_runs_under_the_declared_policy(runs: dict[str, OnchainRun]) -> None:
    run = runs["TN-LINKED"]
    assert run.inputs.profile.profile_ref == "fund-subscription-testnet@1.4.0"
    # The policy of 1.2.0, kept by 1.4.0, evaluated by 0.8.0.
    assert run.evaluation.result.versions.engine_ref == ENGINE_REF
    assert any("declares its quarantine policy" in a for a in run.evaluation.result.assumptions)
    under_1_1_0 = replay(with_profile(run.inputs, PROFILE_1_1_0), PROMISE).result
    assert any("declares no quarantine policy" in a for a in under_1_1_0.assumptions)
    refused = evaluate(with_profile(run.inputs, PROFILE_1_1_0)).result
    assert {c.reason_code for c in refused.controls} == {"EVALUATION_ERROR"}
    assert "evidence provenance not admitted" in refused.controls[0].reason


def test_profile_1_1_0_keeps_its_promise_under_the_current_engine(
    runs: dict[str, OnchainRun],
) -> None:
    """1.1.0 declares no policy: a record bearing on the operation blocks every token
    comparison under 0.6.0 and 0.7.0, as under 0.4.0; the retired 0.5.0 relaxed it (BREAK).
    The current engine refuses today's evidence under 1.1.0 (M2)."""
    for base, retired in zip(bases(runs), ("BREAK", "MATCH", "BREAK"), strict=True):
        inputs = with_profile(case("supported clawback", base), PROFILE_1_1_0)
        assert outcome(replay(inputs, PROMISE).result) == QUARANTINED
        assert outcome(replay(inputs, "invaria-engine@0.6.0").result) == QUARANTINED
        assert outcome(replay(inputs, "invaria-engine@0.4.0").result) == QUARANTINED
        assert replay(inputs, RETIRED).result.result == retired


def test_a_retired_engine_rejects_a_profile_declaring_the_policy(
    runs: dict[str, OnchainRun],
) -> None:
    inputs = case("supported clawback", runs["TN-LINKED"].inputs)
    for engine in (RETIRED, "invaria-engine@0.4.0", "invaria-engine@0.1.0"):
        result = replay(inputs, engine).result
        assert {c.reason_code for c in result.controls} == {"EVALUATION_ERROR"}
        assert "declares a quarantine policy" in result.controls[0].reason
        assert result.result == "UNKNOWN"


# --------------------------------------------- 3. classification and snapshot


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_classification_is_relied_on_only_when_the_snapshot_supports_it(
    runs: dict[str, OnchainRun], name: str
) -> None:
    for base, expected, retired in zip(
        bases(runs), EXPECTED[name], RETIRED_ON_1_1_0[name], strict=True
    ):
        inputs = case(name, base)
        result = evaluate(inputs).result
        assert outcome(result) == expected, name
        old = replay(with_profile(inputs, PROFILE_1_1_0), RETIRED).result
        assert old.result == retired, name
        assert (certificate := inputs.coverage[chain_id(inputs)]).quarantined_records
        assert certificate.records_quarantined == 1  # the record stays quarantined


def test_each_unsupported_classification_says_why(runs: dict[str, OnchainRun]) -> None:
    short = bases(runs)[0]
    reasons = {
        name: next(
            c for c in evaluate(case(name, short)).result.controls if c.control_id == TOKEN
        ).reason
        for name in EXPECTED
    }
    assert "no clawback effect of the snapshot" in reasons["unresolved relabelled as clawback"]
    assert "no observation of the snapshot on" in reasons["clawback without evidence"]
    assert "(only of another one)" in reasons["evidence of another asset"]
    assert "(only of another one)" in reasons["evidence of another network"]
    assert "its locator is the raw record of" in reasons["operation moved off the delivery"]
    assert f"the snapshot shows parties it omits: {INVESTOR}" in reasons["parties omitted"]
    assert "NOT_LISTED_BY_HORIZON" in reasons["reasons contradict the nature"]
    assert "another kind of record" in reasons["another kind on the operation"]
    excess = bases(runs)[2]
    weighed = next(
        c
        for c in evaluate(case("supported clawback", excess)).result.controls
        if c.control_id == TOKEN
    )
    assert "1 identified clawback(s) change no delivery" in weighed.reason


def test_omitted_parties_bear_under_1_1_0_too(runs: dict[str, OnchainRun]) -> None:
    """The contrast of parties never relaxes a result: under 1.1.0 a record that omits the
    investor the snapshot shows on its operation blocks (0.4.0 and 0.5.0: MATCH)."""
    inputs = with_profile(case("parties omitted", runs["TN-LINKED"].inputs), PROFILE_1_1_0)
    assert outcome(replay(inputs, PROMISE).result) == QUARANTINED
    assert replay(inputs, "invaria-engine@0.4.0").result.result == "MATCH"
    assert replay(inputs, RETIRED).result.result == "MATCH"


def test_the_support_check_is_internal_not_a_ledger_verification(
    runs: dict[str, OnchainRun],
) -> None:
    """A fabricated but consistent pair (record and observation written alike) is accepted:
    the check compares artifacts of the snapshot, it does not consult the ledger."""
    inputs = case("supported clawback", runs["TN-LINKED"].inputs)
    members = [inputs.observations[i] for i in inputs.snapshot.observation_ids]
    evidence = chain_evidence(
        members, "stellar-testnet", inputs.profile.instrument.instrument_id, {"stellar:testnet"}
    )
    (rec,) = inputs.coverage[chain_id(inputs)].quarantined_records or []
    support = record_support(rec, evidence, {INVESTOR})
    assert (support.operation, support.clawback, support.notes) == (ELSEWHERE, True, ())


# ------------------------------------------------------- 4. unapproved parties


def test_a_contract_party_is_out_of_scope_unless_something_brings_it_in(
    runs: dict[str, OnchainRun],
) -> None:
    """A contract cannot be approved for the account; that does not make it foreign. Alone,
    a record naming only a contract (and the issuer) is outside the declared scope; an
    ExecutionLink to this order, or an unresolved participant, brings it in; a link to
    another order does not."""
    contract = "CDLZFC3SYJYDZT7K67VZ75HPJVIEUVNIXF47ZG2FB2RMQQVU2HHGCYSC"
    exact = runs["TN-LINKED"].inputs
    alone = record("movement_identified", parties=[contract, DEMOA_ISSUER])
    assert outcome(evaluate(with_records(exact, [alone])).result) == MATCH
    linked = record("movement_identified", parties=[contract, DEMOA_ISSUER], links=["SUB-0001"])
    assert outcome(evaluate(with_records(exact, [linked])).result) == QUARANTINED
    other = record("movement_identified", parties=[contract, DEMOA_ISSUER], links=["SUB-9999"])
    assert outcome(evaluate(with_records(exact, [other])).result) == MATCH
    unknown = {**alone, "addresses": None}
    assert outcome(evaluate(with_records(exact, [unknown])).result) == QUARANTINED


# ----------------------------------------------------------- 2. certificates


def certificate(
    base: CoverageCertificate, coverage_id: str, *, seconds: int = 1, **fields: Any
) -> CoverageCertificate:
    """A certificate derived from ``base``: recorded ``seconds`` later (still before the
    snapshot's known_at), without records unless given, with ``fields`` (``chain_scope``
    entries merged)."""
    document = base.model_dump(mode="json")
    scope = {**document["chain_scope"], **fields.pop("chain_scope", {})}
    document.update(
        {
            "coverage_id": coverage_id,
            "recorded_at": (base.recorded_at + timedelta(seconds=seconds)).isoformat(),
            "records_quarantined": 0,
            "quarantined_records": None,
            "supersedes": None,
            "chain_scope": scope,
            **{
                k: [e.model_dump(mode="json") if hasattr(e, "model_dump") else e for e in v]
                if k == "supersedes" and v
                else v
                for k, v in fields.items()
            },
        }
    )
    return CoverageCertificate.model_validate_json(json.dumps(document))


def ref(*certificates: CoverageCertificate) -> list[dict[str, str]]:
    """``supersedes`` entries naming ``certificates`` by id and content."""
    return [{"coverage_id": c.coverage_id, "sha256": certificate_sha256(c)} for c in certificates]


def with_certificates(
    inputs: EvaluationInputs, *certificates: CoverageCertificate, drop: tuple[str, ...] = ()
) -> EvaluationInputs:
    coverage = {k: v for k, v in inputs.coverage.items() if k not in drop}
    coverage.update({c.coverage_id: c for c in certificates})
    ids = [i for i in inputs.snapshot.coverage_ids if i not in drop]
    ids += [c.coverage_id for c in certificates if c.coverage_id not in ids]
    return dataclasses.replace(
        inputs,
        snapshot=inputs.snapshot.model_copy(update={"coverage_ids": sorted(ids)}),
        coverage=coverage,
    )


@pytest.fixture
def quarantined(runs: dict[str, OnchainRun]) -> EvaluationInputs:
    """TN-LINKED (exact) whose SAC certificate quarantines an unsupported record of the
    investor: UNKNOWN while that certificate is active, MATCH once it is resolved."""
    return with_records(runs["TN-LINKED"].inputs, [record("unresolved")])


def horizon_and_sac(inputs: EvaluationInputs) -> tuple[CoverageCertificate, CoverageCertificate]:
    certs = [c for c in inputs.coverage.values() if c.chain_scope is not None]
    (horizon,) = [c for c in certs if c.chain_scope and c.chain_scope.route == "horizon_payments"]
    (sac,) = [c for c in certs if c.chain_scope and c.chain_scope.route == "rpc_sac_events"]
    assert sac.supersedes is not None  # as the adapter writes it: id and content sha256
    assert [(e.coverage_id, e.sha256) for e in sac.supersedes] == [
        (horizon.coverage_id, certificate_sha256(horizon))
    ]
    return horizon, sac


def results(inputs: EvaluationInputs) -> tuple[str, str]:
    """(0.6.0 under 1.2.0, retired 0.5.0 under 1.1.0)."""
    return (
        evaluate(inputs).result.result,
        replay(with_profile(inputs, PROFILE_1_1_0), RETIRED).result.result,
    )


def test_baseline(quarantined: EvaluationInputs) -> None:
    assert results(quarantined) == ("UNKNOWN", "UNKNOWN")


def test_a_later_horizon_certificate_does_not_hide_the_sac_quarantine(
    quarantined: EvaluationInputs,
) -> None:
    """A Horizon re-run of the same range, recorded later, replaces the earlier Horizon run
    but not the SAC one (narrower route); its claim on the SAC one is refused. 0.5.0 took
    it as the latest certificate of the source and lost the record."""
    horizon, sac = horizon_and_sac(quarantined)
    rerun = certificate(horizon, "cov-derived-horizon-rerun", supersedes=ref(horizon))
    claims = certificate(horizon, "cov-derived-horizon-claim", supersedes=ref(horizon, sac))
    for later in (rerun, claims):
        inputs = with_certificates(quarantined, later)
        assert results(inputs) == ("UNKNOWN", "MATCH")
        coverage = active_coverage([inputs.coverage[i] for i in inputs.snapshot.coverage_ids])
        assert sac in coverage.active
    refused = active_coverage([horizon, sac, claims]).refused
    assert any("route) does not include it" in r for r in refused)


def test_the_ingestion_order_does_not_decide(quarantined: EvaluationInputs) -> None:
    """The SAC certificate recorded before the Horizon one: its supersession claim does not
    hold (recorded earlier), both stay active, the record still bears."""
    horizon, sac = horizon_and_sac(quarantined)
    early_sac = certificate(
        sac,
        sac.coverage_id,
        seconds=-1,
        records_quarantined=sac.records_quarantined,
        quarantined_records=[r.model_dump(mode="json") for r in sac.quarantined_records or []],
        supersedes=ref(horizon),
    )
    inputs = with_certificates(quarantined, early_sac)
    assert results(inputs) == ("UNKNOWN", "MATCH")
    assert any("recorded before it" in r for r in active_coverage([horizon, early_sac]).refused)


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"ledger_range": "shifted"}, "ledger range does not contain it"),
        ({"chain_scope": {"target_id": "another-target"}}, "does not include it"),
        ({"chain_scope": {"asset_code": "DEMOB"}}, "does not include it"),
        ({"chain_scope": {"network": "stellar:pubnet"}}, "does not include it"),
        ({"chain_scope": {"links_sha256": "1" * 64}}, "does not include it"),
        ({"tenant_id": "another-tenant"}, "another tenant"),
    ],
    ids=[
        "partial-overlap",
        "other-target",
        "other-asset",
        "other-network",
        "other-links",
        "other-tenant",
    ],
)
def test_a_certificate_of_another_scope_or_range_resolves_nothing(
    quarantined: EvaluationInputs, fields: dict[str, Any], why: str
) -> None:
    _horizon, sac = horizon_and_sac(quarantined)
    assert sac.ledger_range is not None
    if fields.get("ledger_range") == "shifted":
        fields = {
            "ledger_range": {
                "first": sac.ledger_range.first + 5,
                "last": sac.ledger_range.last + 5,
            }
        }
    claim = certificate(sac, "cov-derived-other", supersedes=ref(sac), **fields)
    inputs = with_certificates(quarantined, claim)
    assert evaluate(inputs).result.result == "UNKNOWN"
    assert any(why in r for r in active_coverage([sac, claim]).refused)


def test_an_explicit_resolution_ends_the_quarantine(quarantined: EvaluationInputs) -> None:
    """A later SAC certificate of the same scope, over a range containing the earlier one,
    declaring that it supersedes it and no longer quarantining the record: resolved."""
    horizon, sac = horizon_and_sac(quarantined)
    assert sac.ledger_range is not None
    wider = {"first": sac.ledger_range.first - 1, "last": sac.ledger_range.last + 1}
    resolved = certificate(
        sac,
        "cov-derived-sac-resolved",
        ledger_range=wider,
        supersedes=ref(horizon, sac),
    )
    inputs = with_certificates(quarantined, resolved)
    assert evaluate(inputs).result.result == "MATCH"
    # Still listing it: not resolved.
    still = certificate(
        sac,
        "cov-derived-sac-still",
        ledger_range=wider,
        supersedes=ref(sac),
        records_quarantined=1,
        quarantined_records=[record("unresolved")],
    )
    assert evaluate(with_certificates(quarantined, still)).result.result == "UNKNOWN"
    # Without the declaration, recency alone resolves nothing.
    silent = certificate(sac, "cov-derived-sac-silent", ledger_range=wider)
    assert evaluate(with_certificates(quarantined, silent)).result.result == "UNKNOWN"


def test_the_earlier_snapshot_keeps_its_historical_state(
    quarantined: EvaluationInputs, tmp_path: Path
) -> None:
    """The snapshot known before the resolution stays UNKNOWN when evaluated again and when
    its bundle is verified; the later snapshot, holding the resolution, is MATCH."""
    horizon, sac = horizon_and_sac(quarantined)
    resolved = certificate(sac, "cov-derived-sac-resolved", supersedes=ref(horizon, sac))
    later = with_certificates(quarantined, resolved)
    observations = [later.observations[i] for i in later.snapshot.observation_ids]
    certificates = [later.coverage[i] for i in later.snapshot.coverage_ids]

    def snapshot_at(moment: Any, snapshot_id: str) -> EvaluationInputs:
        snapshot = build_snapshot(
            snapshot_id=snapshot_id,
            tenant_id=later.snapshot.tenant_id,
            operation_ref=later.snapshot.operation_ref,
            profile=later.profile,
            valid_at=later.snapshot.valid_at,
            known_at=moment,
            evaluation_clock=moment,
            observations=observations,
            coverage=certificates,
            identity_links=later.identity_links.values(),
        )
        return dataclasses.replace(later, snapshot=snapshot)

    # Known just before the resolving certificate was recorded, and at the plan's known_at.
    before = snapshot_at(sac.recorded_at, "snap-before-resolution")
    after = snapshot_at(quarantined.snapshot.known_at, "snap-after-resolution")
    assert resolved.coverage_id not in before.snapshot.coverage_ids
    first = evaluate(before)
    assert outcome(first.result) == QUARANTINED
    assert outcome(evaluate(after).result) == MATCH
    assert before.snapshot.observation_ids == after.snapshot.observation_ids
    assert evaluate(before).result == first.result  # evaluated again: the same conclusion
    build_bundle(tmp_path / "before", before, first, mode="as_known")
    report = verify_bundle(tmp_path / "before")
    assert (report.status, report.financial_result) == ("REPRODUCED", "UNKNOWN")


def test_order_and_certificate_selection_do_not_change_the_conclusion(
    quarantined: EvaluationInputs,
) -> None:
    horizon, _sac = horizon_and_sac(quarantined)
    rerun = certificate(horizon, "cov-derived-horizon-rerun", supersedes=ref(horizon))
    base = with_certificates(with_effect(quarantined), rerun)
    expected = evaluate(base).result
    shuffle = random.Random(14)
    for _ in range(6):
        members = list(base.snapshot.observation_ids)
        certs = list(base.snapshot.coverage_ids)
        shuffle.shuffle(members)
        shuffle.shuffle(certs)
        items = list(base.coverage.items())
        shuffle.shuffle(items)
        inputs = dataclasses.replace(
            base,
            snapshot=base.snapshot.model_copy(
                update={"observation_ids": members, "coverage_ids": certs}
            ),
            coverage=dict(items),
        )
        result = evaluate(inputs).result
        assert (result.result, result.controls) == (expected.result, expected.controls)


# ------------------------------------------------------- 8. end to end


def test_end_to_end_a_tampered_classification_changes_the_reproduced_conclusion(
    runs: dict[str, OnchainRun], tmp_path: Path
) -> None:
    """Through the bundle: a supported clawback keeps the short delivery a BREAK and the
    bundle reproduces; moving the record onto the counted delivery's raw record (hashes
    recomputed, so only the content differs) makes the verifier's own evaluation UNKNOWN,
    a MISMATCH with the recorded BREAK."""
    inputs = case("supported clawback", bases(runs)[0])
    evaluation = evaluate(inputs)
    assert evaluation.result.result == "BREAK"
    build_bundle(tmp_path / "b", inputs, evaluation, mode="as_known")
    assert verify_bundle(tmp_path / "b").status == "REPRODUCED"
    moved = case("operation moved off the delivery", bases(runs)[0])
    assert evaluate(moved).result.result == "UNKNOWN"
    build_bundle(tmp_path / "m", moved, evaluation, mode="as_known")
    assert verify_bundle(tmp_path / "m").status == "MISMATCH"


def test_under_1_1_0_any_execution_link_still_blocks(runs: dict[str, OnchainRun]) -> None:
    """The promise of 1.1.0 (the rule of 0.4.0): a record an ExecutionLink names bears on
    every operation, even with foreign known addresses and a link to another order; 1.2.0
    weighs it only for the linked order."""
    exact = runs["TN-LINKED"].inputs
    foreign = record("movement_identified", parties=[STRANGER, DEMOA_ISSUER], links=["SUB-9999"])
    inputs = with_records(with_effect(exact, account=STRANGER), [foreign])
    assert outcome(replay(with_profile(inputs, PROFILE_1_1_0), PROMISE).result) == QUARANTINED
    assert outcome(replay(with_profile(inputs, PROFILE_1_1_0), "invaria-engine@0.4.0").result) == (
        QUARANTINED
    )
    assert outcome(evaluate(inputs).result) == MATCH


def test_the_coverage_is_met_by_any_active_certificate(quarantined: EvaluationInputs) -> None:
    """Under 1.2.0 a later Horizon re-run with a gap neither replaces the earlier
    certificates nor stops them meeting the coverage: once the record is resolved the
    delivery matches; with every active certificate gapped, the coverage is insufficient."""
    horizon, sac = horizon_and_sac(quarantined)
    start = sac.interval.start
    gap = [{"start": start.isoformat(), "end": (start + timedelta(seconds=1)).isoformat()}]
    gapped = certificate(horizon, "cov-derived-horizon-gapped", supersedes=ref(horizon), gaps=gap)
    clean = with_records(quarantined, [])
    assert outcome(evaluate(with_certificates(clean, gapped)).result) == MATCH
    # A gapped certificate supersedes nothing (review M3): the first Horizon run stays
    # active beside it.
    assert any("it declares gaps" in r for r in active_coverage([horizon, gapped]).refused)
    sac_gapped = certificate(sac, sac.coverage_id, seconds=0, gaps=gap, supersedes=sac.supersedes)
    horizon_gapped = certificate(horizon, horizon.coverage_id, seconds=0, gaps=gap)
    all_gapped = with_certificates(clean, gapped, sac_gapped, horizon_gapped)
    assert outcome(evaluate(all_gapped).result) == ("UNKNOWN", "UNKNOWN", "INSUFFICIENT_COVERAGE")


# ------------------------------------------- independent domain review


def test_under_1_1_0_the_coverage_is_the_most_recent_certificates(
    quarantined: EvaluationInputs,
) -> None:
    """Review M1: without a declared policy the coverage is judged on the most recent
    certificate, as 1.1.0 promised (0.4.0 too); only the quarantine is the union. A later
    gapped Horizon re-run leaves the delivery UNKNOWN there, while 1.2.0 uses an active
    certificate that meets it."""
    horizon, _sac = horizon_and_sac(quarantined)
    start = horizon.interval.start
    gap = [{"start": start.isoformat(), "end": (start + timedelta(seconds=1)).isoformat()}]
    gapped = certificate(horizon, "cov-derived-horizon-gapped", gaps=gap)
    inputs = with_certificates(with_records(quarantined, []), gapped)
    old = with_profile(inputs, PROFILE_1_1_0)
    expected = ("UNKNOWN", "UNKNOWN", "INSUFFICIENT_COVERAGE")
    assert outcome(replay(old, PROMISE).result) == expected
    assert outcome(replay(old, "invaria-engine@0.4.0").result) == expected
    assert outcome(evaluate(inputs).result) == MATCH


def test_a_gapped_or_weaker_certificate_supersedes_nothing(quarantined: EvaluationInputs) -> None:
    """Review M3: a record's absence inside a gap, or in a certificate of a lower level,
    does not resolve it."""
    _horizon, sac = horizon_and_sac(quarantined)
    assert sac.ledger_range is not None
    start = sac.interval.start
    gap = [{"start": start.isoformat(), "end": (start + timedelta(seconds=1)).isoformat()}]
    wider = {"first": sac.ledger_range.first - 1, "last": sac.ledger_range.last + 1}
    gapped = certificate(
        sac, "cov-derived-sac-gapped", ledger_range=wider, gaps=gap, supersedes=ref(sac)
    )
    assert evaluate(with_certificates(quarantined, gapped)).result.result == "UNKNOWN"
    stronger = certificate(sac, sac.coverage_id, seconds=0, level="internally_checked")
    weaker = certificate(
        stronger, "cov-derived-sac-weaker", level="provider_claimed", supersedes=ref(stronger)
    )
    assert active_coverage([stronger, weaker]).refused == (
        f"cov-derived-sac-weaker does not supersede {sac.coverage_id}: its level is lower",
    )
    assert any("it declares gaps" in r for r in active_coverage([sac, gapped]).refused)


def test_a_relevant_party_the_clawback_does_not_involve_is_not_backed(
    runs: dict[str, OnchainRun],
) -> None:
    """Review B2: the snapshot's clawback is of a stranger; a record naming the investor too
    is not a clawback the snapshot supports for this order: it could be a delivery."""
    short = bases(runs)[0]
    effect = with_effect(short, account=STRANGER)
    inputs = with_records(effect, [record(parties=[INVESTOR, STRANGER, DEMOA_ISSUER])])
    assert outcome(evaluate(inputs).result) == QUARANTINED
    assert replay(with_profile(inputs, PROFILE_1_1_0), RETIRED).result.result == "BREAK"


# ------------------------------------------- independent Stellar review


def test_a_supersession_names_the_content_it_replaces(quarantined: EvaluationInputs) -> None:
    """Review M3: a claim on an id whose content in the snapshot differs (another run of the
    same target, route and range) replaces nothing."""
    _horizon, sac = horizon_and_sac(quarantined)
    other_run = certificate(
        sac,
        sac.coverage_id,
        seconds=0,
        records_received=sac.records_received + 1,
        records_quarantined=sac.records_quarantined,
        quarantined_records=[r.model_dump(mode="json") for r in sac.quarantined_records or []],
        supersedes=sac.supersedes,
    )
    claim = certificate(sac, "cov-derived-sac-claim", supersedes=ref(sac))
    inputs = with_certificates(quarantined, other_run, claim)
    assert evaluate(inputs).result.result == "UNKNOWN"
    assert any(
        "it names another content under that id" in r
        for r in active_coverage([other_run, claim]).refused
    )


def test_at_the_same_instant_only_strict_containment_supersedes(
    quarantined: EvaluationInputs,
) -> None:
    """Review B1: with equal recorded_at, a certificate replaces another only if its route
    or range strictly contains it, never by the order of their ids."""
    horizon, sac = horizon_and_sac(quarantined)
    twin = certificate(sac, "cov-derived-sac-twin", seconds=0, supersedes=ref(sac))
    assert any(
        "same instant without strictly containing it" in r
        for r in active_coverage([sac, twin]).refused
    )
    assert evaluate(with_certificates(quarantined, twin)).result.result == "UNKNOWN"
    assert horizon.recorded_at == sac.recorded_at  # the adapter's SAC still replaces it
    assert not active_coverage([horizon, sac]).refused


def test_a_correspondence_locator_must_name_the_claimed_operation(
    runs: dict[str, OnchainRun],
) -> None:
    """Review B2: a correspondence check names its operation in its locator; claiming another
    one is not supported."""
    short = bases(runs)[0]
    locator = f"correspondence#testnet-demoa-own:{'ef' * 32}:0@{'0' * 16}"
    inputs = with_records(with_effect(short, locator=locator), [record(locator=locator)])
    assert outcome(evaluate(inputs).result) == QUARANTINED
    control = next(c for c in evaluate(inputs).result.controls if c.control_id == TOKEN)
    assert f"its locator names {'ef' * 32}:0, not {ELSEWHERE}" in control.reason
