"""Per-control quarantine in redemption (engine 0.7.0, profile 1.5.0).

An equal burn is positive evidence, but under a declared quarantine policy a quarantined
record bearing on the request leaves UNKNOWN what resolving it could change: on the
counted burn's ledger operation (or one the snapshot does not support), any burn result
and a settlement shown only by that burn; on another operation, an equal or short burn (it
could be another retirement; an identified clawback included, and it is never
counted as a burn); any bearing record, the absence of settlement. An excess of burns, a
settlement proven by cash and the BREAKs of other controls stay.

Everything here is synthetic (the DEMO-A redemption corpus); the quarantined records and
the observations supporting them are derived by the tests. ``EXPECTED`` is written before
the engines run.
"""

from __future__ import annotations

import dataclasses
import json
import random
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from invaria.contracts.coverage import CoverageCertificate, certificate_sha256
from invaria.contracts.evaluation import EvaluationResult
from invaria.contracts.observation import Observation, TokenMovementPayload
from invaria.contracts.profile import admitted_mapping_refs
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.common import covers_absence
from invaria.engine.evaluate import EvaluationInputs, evaluate, replay

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/corpus"
INVESTOR = "GA2JRO6WKTHMJTH7TDAXHNDHWT5SF72VKABPO2G6RL5BCGKTNIPIBEKW"
ISSUER = "GDMYHWUG6BLHEGQSTMCAGUFUZFRZ6XGXJSHDKMDUZ6IRCKJ4FGZCCZVZ"
STRANGER = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
ELSEWHERE_TX = "cd" * 32
ELSEWHERE = f"{ELSEWHERE_TX}:0"
CHAIN = "stellar-testnet-frozen"
RETIRED = "invaria-redemption-engine@0.6.0"

PASS = ("PASS", "EXACT_MATCH")
FAIL = ("FAIL", "UNITS_MISMATCH")
Q = ("UNKNOWN", "QUARANTINED_INPUT")
# Written before running: burn_vs_request on (equal, short, excess) burns, profile 1.5.0.
EXPECTED: dict[str, tuple[tuple[str, str], ...]] = {
    "no quarantine": (PASS, FAIL, FAIL),
    # On the counted burn's own ledger operation: its attribution, execution or nature is
    # in doubt, in either direction.
    "on the counted burn": (Q, Q, Q),
    # A ledger operation the snapshot does not support: it could be the counted burn's.
    "unsupported operation": (Q, Q, Q),
    # Another operation (supported): it could be another retirement; an excess stays.
    "unresolved elsewhere": (Q, Q, FAIL),
    # An identified clawback elsewhere: it could be the real retirement, never an
    # accepted burn; the excess of burns stays a FAIL.
    "identified clawback elsewhere": (Q, Q, FAIL),
    # Foreign by its known parties, on a supported operation: nothing changes.
    "foreign": (PASS, FAIL, FAIL),
    # An ExecutionLink names its operation as this request's: weighed, addresses kept.
    "linked to this request": (Q, Q, FAIL),
    # A link to another request does not bring a foreign record in.
    "linked to another request": (PASS, FAIL, FAIL),
}
# The retired 0.6.0 under 1.4.0 (no policy): an equal burn never consulted the quarantine.
RETIRED_ON_1_4_0: dict[str, tuple[str, ...]] = {
    **{name: ("PASS", "FAIL", "FAIL") for name in EXPECTED},
    # The identified clawback is also a chain effect of the investor: since 0.5.0 it
    # leaves an equal or short burn UNKNOWN by itself, with or without quarantine.
    "identified clawback elsewhere": ("UNKNOWN", "UNKNOWN", "FAIL"),
}


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(FIXTURES / "redemption-synthetic-1.5.0")


@pytest.fixture(scope="module")
def previous() -> Corpus:
    return load_corpus(FIXTURES / "redemption-synthetic-1.4.0")


def burn(inputs: EvaluationInputs) -> Observation:
    return next(
        o
        for i in inputs.snapshot.observation_ids
        if isinstance((o := inputs.observations[i]).payload, TokenMovementPayload)
    )


def burn_op(inputs: EvaluationInputs) -> str:
    payload = burn(inputs).payload
    assert isinstance(payload, TokenMovementPayload)
    return f"{payload.chain.tx_hash}:{payload.chain.operation_index}"


def with_burn_units(inputs: EvaluationInputs, atoms: str) -> EvaluationInputs:
    o = burn(inputs)
    assert isinstance(o.payload, TokenMovementPayload)
    units = o.payload.units.model_copy(update={"atoms": atoms})
    changed = o.model_copy(update={"payload": o.payload.model_copy(update={"units": units})})
    return dataclasses.replace(
        inputs, observations={**inputs.observations, o.observation_id: changed}
    )


def burns(corpus: Corpus, scenario: str = "RD-PAID") -> tuple[EvaluationInputs, ...]:
    """(equal, short, excess) burns of the 100 units requested (derived amounts)."""
    base = corpus.inputs_for(scenario)
    return base, with_burn_units(base, "400000000"), with_burn_units(base, "1500000000")


def record(
    nature: str | None = "unresolved",
    chain_operation: str | None = ELSEWHERE,
    parties: list[str] | None = None,
    links: list[str] | None = None,
    locator: str = "rpc:getEvents#derived-0",
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "locator": locator,
        "reasons": ["CORRESPONDENCE_CONFLICT"],
        "addresses": sorted(parties or [INVESTOR, STRANGER, ISSUER]),
    }
    if nature is not None:
        entry["nature"] = nature
    if chain_operation is not None:
        entry["chain_operation"] = chain_operation
    if links is not None:
        entry["execution_links"] = links
    return entry


def chain_id(inputs: EvaluationInputs) -> str:
    return max(
        (c.recorded_at, i)
        for i, c in inputs.coverage.items()
        if c.source_id == CHAIN and i in inputs.snapshot.coverage_ids
    )[1]


def with_records(inputs: EvaluationInputs, records: list[dict[str, Any]]) -> EvaluationInputs:
    cid = chain_id(inputs)
    cert = inputs.coverage[cid]
    altered = CoverageCertificate.model_validate_json(
        json.dumps(
            {
                **cert.model_dump(mode="json"),
                "records_received": cert.records_received + len(records),
                "records_quarantined": len(records),
                "quarantined_records": records,
            }
        )
    )
    return dataclasses.replace(inputs, coverage={**inputs.coverage, cid: altered})


def with_effect(
    inputs: EvaluationInputs,
    kind: str,
    *,
    account: str,
    template: Observation,
    locator: str = "rpc:getEvents#derived-0",
) -> EvaluationInputs:
    """One chain effect of the instrument on ``ELSEWHERE`` (derived, adapter shapes)."""
    assert isinstance(template.payload, TokenMovementPayload)
    payload: dict[str, Any] = {
        "payload_type": "chain_effect",
        "effect_kind": kind,
        "representation": "sac",
        "account": account,
        "direction": "debit",
        "counterparty": {"kind": "account", "id": ISSUER},
        "units": template.payload.units.model_dump(),
        "chain": {
            **template.payload.chain.model_dump(mode="json"),
            "tx_hash": ELSEWHERE_TX,
            "operation_index": 0,
        },
        "path_payment": None,
        "transaction": None,
    }
    if kind == "clawback":
        code = next(r.asset_code for r in inputs.profile.representations)
        payload["clawback"] = {
            "asset": f"{code}:{ISSUER}",
            "issuer": ISSUER,
            "operation_type": None,
            "operation_source": None,
        }
    else:
        payload["unresolved"] = {"reason": "operation_not_found", "operation_type": None}
    effect = Observation.model_validate_json(
        json.dumps(
            {
                **template.model_dump(mode="json"),
                "observation_id": f"obs-derived-{kind}",
                "fact_type": "chain_effect",
                "operation_ref": None,
                "source": {**template.source.model_dump(), "record_key": f"derived:{kind}"},
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


def case(name: str, inputs: EvaluationInputs, template: Observation) -> EvaluationInputs:
    """The case ``name`` on ``inputs``; ``template`` is a burn to derive effects from."""
    unresolved = with_effect(inputs, "unresolved_movement", account=STRANGER, template=template)
    if name == "no quarantine":
        return inputs
    if name == "on the counted burn":
        return with_records(
            inputs,
            [record(chain_operation=burn_op(inputs), locator=template.provenance.raw_locator)],
        )
    if name == "unsupported operation":
        return with_records(inputs, [record()])
    if name == "unresolved elsewhere":
        return with_records(unresolved, [record()])
    if name == "identified clawback elsewhere":
        clawback = with_effect(inputs, "clawback", account=INVESTOR, template=template)
        return with_records(clawback, [record("clawback_identified", parties=[INVESTOR, ISSUER])])
    if name == "foreign":
        return with_records(unresolved, [record(parties=[STRANGER, ISSUER])])
    if name == "linked to this request":
        return with_records(unresolved, [record(parties=[STRANGER, ISSUER], links=["RED-0001"])])
    if name == "linked to another request":
        return with_records(unresolved, [record(parties=[STRANGER, ISSUER], links=["RED-9999"])])
    raise AssertionError(name)


def under(inputs: EvaluationInputs, other: Corpus) -> EvaluationInputs:
    """The same evidence under another version of the profile."""
    return dataclasses.replace(
        inputs,
        snapshot=inputs.snapshot.model_copy(
            update={
                "profile_ref": other.profile.profile_ref,
                "rules_ref": other.profile.rules_ref,
                "mapping_refs": admitted_mapping_refs(other.profile.sources),
            }
        ),
        profile=other.profile,
    )


def status(result: EvaluationResult, control: str) -> tuple[str, str]:
    (c,) = [c for c in result.controls if c.control_id == f"redemption.{control}"]
    return c.status, c.reason_code


# ------------------------------------------------------------------ the burn


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_the_burn_is_unknown_only_where_the_quarantine_could_change_it(
    corpus: Corpus, previous: Corpus, name: str
) -> None:
    template = burn(corpus.inputs_for("RD-PAID"))
    for base, expected, retired in zip(
        burns(corpus), EXPECTED[name], RETIRED_ON_1_4_0[name], strict=True
    ):
        inputs = case(name, base, template)
        assert status(evaluate(inputs).result, "burn_vs_request") == expected, name
        old = replay(under(inputs, previous), RETIRED).result
        assert status(old, "burn_vs_request")[0] == retired, name


def test_the_previous_profile_keeps_its_promise_under_the_current_engine(
    corpus: Corpus, previous: Corpus
) -> None:
    """1.4.0 declares no policy: under 0.7.0 an equal burn stays positive evidence (PASS),
    as under 0.6.0; the declared policy of 1.5.0 is what makes it UNKNOWN."""
    template = burn(corpus.inputs_for("RD-PAID"))
    inputs = case("unresolved elsewhere", corpus.inputs_for("RD-PAID"), template)
    assert status(evaluate(under(inputs, previous)).result, "burn_vs_request") == PASS
    assert status(evaluate(inputs).result, "burn_vs_request") == Q


def test_a_retired_engine_rejects_the_declared_policy(corpus: Corpus) -> None:
    for engine in (RETIRED, "invaria-redemption-engine@0.5.0", "invaria-redemption-engine@0.4.0"):
        result = replay(corpus.inputs_for("RD-PAID"), engine).result
        assert {c.reason_code for c in result.controls} == {"EVALUATION_ERROR"}
        assert "declares a quarantine policy" in result.controls[0].reason


def test_an_excess_says_what_it_weighed_and_counts_no_clawback(corpus: Corpus) -> None:
    template = burn(corpus.inputs_for("RD-PAID"))
    excess = case("identified clawback elsewhere", burns(corpus)[2], template)
    (control,) = [
        c for c in evaluate(excess).result.controls if c.control_id == "redemption.burn_vs_request"
    ]
    assert control.status == "FAIL" and control.delta is not None
    assert control.delta.atoms == "500000000"  # 150 burned - 100 requested; no clawback added
    assert "1 could only add to an excess" in control.reason


def test_a_break_of_another_control_survives(corpus: Corpus) -> None:
    template = burn(corpus.inputs_for("RD-SHORT-PAY"))
    inputs = case("unresolved elsewhere", corpus.inputs_for("RD-SHORT-PAY"), template)
    result = evaluate(inputs).result
    assert status(result, "cash_vs_expected")[0] == "FAIL"
    assert status(result, "burn_vs_request") == Q
    assert result.result == "BREAK"


# ---------------------------------------------------------- the cancellation


def test_a_settlement_shown_only_by_a_doubted_burn_is_unknown(corpus: Corpus) -> None:
    base = corpus.inputs_for("RD-CANCELLED-BURNED")
    template = burn(base)
    assert status(evaluate(base).result, "no_settlement_after_cancellation") == (
        "FAIL",
        "SETTLED_DESPITE_CANCELLATION",
    )
    doubted = evaluate(case("on the counted burn", base, template)).result
    assert status(doubted, "no_settlement_after_cancellation") == Q
    assert doubted.result == "UNKNOWN"
    elsewhere = evaluate(case("unresolved elsewhere", base, template)).result
    assert status(elsewhere, "no_settlement_after_cancellation") == (
        "FAIL",
        "SETTLED_DESPITE_CANCELLATION",
    )
    assert elsewhere.result == "BREAK"


def test_a_settlement_proven_by_cash_stays_a_break(corpus: Corpus) -> None:
    base = corpus.inputs_for("RD-CANCELLED-PAID")
    template = burn(corpus.inputs_for("RD-PAID"))
    result = evaluate(case("unsupported operation", base, template)).result
    assert status(result, "no_settlement_after_cancellation") == (
        "FAIL",
        "SETTLED_DESPITE_CANCELLATION",
    )
    assert result.result == "BREAK"


def test_an_absence_of_settlement_needs_every_bearing_record_resolved(corpus: Corpus) -> None:
    base = corpus.inputs_for("RD-CANCELLED")
    template = burn(corpus.inputs_for("RD-PAID"))
    assert evaluate(base).result.result == "MATCH"
    for name in ("unresolved elsewhere", "linked to this request"):
        result = evaluate(case(name, base, template)).result
        assert status(result, "no_settlement_after_cancellation") == Q, name
    # The identified clawback is also a chain effect of the investor; that reason was
    # already reported first by 0.6.0 and still is.
    result = evaluate(case("identified clawback elsewhere", base, template)).result
    assert status(result, "no_settlement_after_cancellation") == (
        "UNKNOWN",
        "UNSUPPORTED_CAPABILITY",
    )
    for name in ("foreign", "linked to another request"):
        assert evaluate(case(name, base, template)).result.result == "MATCH", name


# ---------------------------------------------------------- certificates


def scoped(
    cert: CoverageCertificate,
    route: str,
    coverage_id: str,
    chain_scope_extra: dict[str, Any] | None = None,
    **fields: Any,
) -> CoverageCertificate:
    document = {
        **cert.model_dump(mode="json"),
        "coverage_id": coverage_id,
        "ledger_range": {"first": 100, "last": 200},
        "chain_scope": {
            "network": "stellar:testnet",
            "target_id": "synthetic-demoa-investor",
            "account": INVESTOR,
            "asset_code": "DEMOA",
            "asset_issuer": ISSUER,
            "route": route,
            "links_sha256": "0" * 64,
            **(chain_scope_extra or {}),
        },
        **fields,
    }
    return CoverageCertificate.model_validate_json(json.dumps(document))


def test_a_later_horizon_certificate_does_not_hide_the_sac_quarantine(
    corpus: Corpus, previous: Corpus
) -> None:
    """RD-CANCELLED: the SAC certificate quarantines a record of the investor; a Horizon
    certificate of the same scope, recorded later, does not supersede it. 0.7.0 keeps the
    absence UNKNOWN; the retired 0.6.0 read only the latest certificate and passed it."""
    template = burn(corpus.inputs_for("RD-PAID"))
    base = case("unresolved elsewhere", corpus.inputs_for("RD-CANCELLED"), template)
    cid = chain_id(base)
    known = base.coverage[cid].recorded_at  # equal to the snapshot's known_at
    sac = scoped(
        base.coverage[cid],
        "rpc_sac_events",
        cid,
        recorded_at=(known - timedelta(seconds=2)).isoformat(),
    )
    horizon = scoped(
        base.coverage[cid],
        "horizon_payments",
        "cov-derived-horizon-later",
        records_quarantined=0,
        quarantined_records=None,
        recorded_at=(sac.recorded_at + timedelta(seconds=1)).isoformat(),
        supersedes=[{"coverage_id": cid, "sha256": certificate_sha256(sac)}],
    )
    inputs = dataclasses.replace(
        base,
        snapshot=base.snapshot.model_copy(
            update={"coverage_ids": sorted([*base.snapshot.coverage_ids, horizon.coverage_id])}
        ),
        coverage={**base.coverage, cid: sac, horizon.coverage_id: horizon},
    )
    assert status(evaluate(inputs).result, "no_settlement_after_cancellation") == Q
    old = replay(under(inputs, previous), RETIRED).result
    assert status(old, "no_settlement_after_cancellation") == ("PASS", "EXACT_MATCH")
    resolved = scoped(
        base.coverage[cid],
        "rpc_sac_events",
        "cov-derived-sac-resolved",
        records_quarantined=0,
        quarantined_records=None,
        recorded_at=(sac.recorded_at + timedelta(seconds=2)).isoformat(),
        supersedes=[
            {"coverage_id": c.coverage_id, "sha256": certificate_sha256(c)} for c in (sac, horizon)
        ],
    )
    done = dataclasses.replace(
        inputs,
        snapshot=inputs.snapshot.model_copy(
            update={"coverage_ids": sorted([*inputs.snapshot.coverage_ids, resolved.coverage_id])}
        ),
        coverage={**inputs.coverage, resolved.coverage_id: resolved},
    )
    assert evaluate(done).result.result == "MATCH"


def test_order_does_not_change_the_conclusion(corpus: Corpus) -> None:
    template = burn(corpus.inputs_for("RD-PAID"))
    base = case("linked to this request", burns(corpus)[2], template)
    expected = evaluate(base).result
    shuffle = random.Random(15)
    for _ in range(5):
        members = list(base.snapshot.observation_ids)
        shuffle.shuffle(members)
        items = list(base.observations.items())
        shuffle.shuffle(items)
        inputs = dataclasses.replace(
            base,
            snapshot=base.snapshot.model_copy(update={"observation_ids": members}),
            observations=dict(items),
        )
        result = evaluate(inputs).result
        assert (result.result, result.controls) == (expected.result, expected.controls)


def test_a_cash_settlement_keeps_the_break_whatever_the_burn(corpus: Corpus) -> None:
    """Cancelled, then both paid in cash and burned; a record puts the burn's own operation
    in doubt. The cash payment proves the settlement by itself: the BREAK stays."""
    burned = corpus.inputs_for("RD-CANCELLED-BURNED")
    paid = corpus.inputs_for("RD-CANCELLED-PAID")
    extra = [i for i in paid.snapshot.observation_ids if i not in burned.snapshot.observation_ids]
    both = dataclasses.replace(
        burned,
        snapshot=burned.snapshot.model_copy(
            update={"observation_ids": sorted([*burned.snapshot.observation_ids, *extra])}
        ),
        observations={**burned.observations, **{i: paid.observations[i] for i in extra}},
    )
    doubted = case("on the counted burn", both, burn(both))
    result = evaluate(doubted).result
    assert status(result, "no_settlement_after_cancellation") == (
        "FAIL",
        "SETTLED_DESPITE_CANCELLATION",
    )
    assert result.result == "BREAK"


# ------------------------------------------- independent domain review


def holder_and_issuer(base: EvaluationInputs) -> EvaluationInputs:
    """``base`` whose chain certificate is a holder target's (declaring what it does not see)."""
    cid = chain_id(base)
    holder = scoped(
        base.coverage[cid],
        "rpc_sac_events",
        cid,
        chain_scope_extra={
            "not_covered": ["claimable_balance_clawback", "claimable_balance_claim"]
        },
    )
    return dataclasses.replace(base, coverage={**base.coverage, cid: holder})


def test_an_observation_after_the_cut_still_backs_a_records_operation(corpus: Corpus) -> None:
    """Review M4: the effect on the record's operation is effective after valid_at; it does
    not count as economic evidence, but it does show the operation is not the burn's. The
    excess of burns stays a FAIL."""
    template = burn(corpus.inputs_for("RD-PAID"))
    excess = case("unresolved elsewhere", burns(corpus)[2], template)
    (effect_id,) = [i for i in excess.observations if i.startswith("obs-derived-")]
    late = excess.observations[effect_id].model_copy(
        update={"valid_time": excess.snapshot.valid_at + timedelta(hours=1)}
    )
    inputs = dataclasses.replace(excess, observations={**excess.observations, effect_id: late})
    assert status(evaluate(inputs).result, "burn_vs_request") == FAIL


NO_SETTLEMENT = "no_settlement_after_cancellation"


@pytest.mark.parametrize("version", ["1.5.0", "1.4.0", "1.3.0"])
@pytest.mark.parametrize(
    ("not_covered", "expected"),
    [
        (["claimable_balance_clawback", "claimable_balance_claim"], "UNKNOWN"),  # holder
        (["claimable_balance_claim"], "UNKNOWN"),  # issuer target, SAC route
        (["claimable_balance_clawback"], "UNKNOWN"),
        (None, "MATCH"),  # a scope that declares nothing out of sight
    ],
)
def test_the_absence_of_settlement_needs_the_effects_that_could_change_it_in_sight(
    version: str, not_covered: list[str] | None, expected: str
) -> None:
    """Decided on 2026-10-08: "no burn the profile admits" is not "no
    retirement or settlement occurred". A chain certificate that declares a claimable balance
    clawback or claim out of its sight (by role and route) cannot show the absence of
    settlement, since such an effect could be it: 0.9.0 leaves it UNKNOWN
    (INSUFFICIENT_COVERAGE, naming what is not seen) and the effect is never evidence of
    compliance. The retired 0.8.0 showed it, as recorded. A certificate without a chain
    scope (the synthetic corpus) declares nothing out of sight. The rule holds under every
    profile, whichever certificate it reads (1.5.0: an active certificate; 1.4.0: the most
    recent of the scoped ones; 1.3.0: the most recent); it can only stop a PASS."""
    corpus = load_corpus(
        FIXTURES
        / ("redemption-synthetic" if version == "1.3.0" else f"redemption-synthetic-{version}")
    )
    base = corpus.inputs_for("RD-CANCELLED")
    cid = chain_id(base)
    extra = {} if not_covered is None else {"not_covered": not_covered}
    cert = scoped(base.coverage[cid], "rpc_sac_events", cid, chain_scope_extra=extra)
    inputs = dataclasses.replace(base, coverage={**base.coverage, cid: cert})
    assert covers_absence(cert, ["claimable_balance_clawback", "claimable_balance_claim"]) == (
        not_covered is None
    )
    result = evaluate(inputs).result
    assert result.result == expected
    control = next(c for c in result.controls if c.control_id.endswith(NO_SETTLEMENT))
    if expected == "UNKNOWN":
        assert (control.status, control.reason_code) == ("UNKNOWN", "INSUFFICIENT_COVERAGE")
        assert all(effect in control.reason for effect in not_covered or ())
        assert "not evidence of compliance" in control.reason
        assert cid in control.evidence_refs
        assert all(
            c.status in ("PASS", "NOT_APPLICABLE")
            for c in result.controls
            if not c.control_id.endswith(NO_SETTLEMENT)
        )
    else:
        assert control.status == "PASS"
    assert replay(inputs, "invaria-redemption-engine@0.8.0").result.result == "MATCH"
    assert evaluate(base).result.result == "MATCH"  # no chain scope: nothing declared unseen


def test_proven_settlement_stays_a_break_whatever_the_chain_scope(corpus: Corpus) -> None:
    """The coverage rule only stops an absence: a linked burn after a cancellation is a
    settlement shown, a BREAK, with or without the effects out of sight."""
    holder = holder_and_issuer(corpus.inputs_for("RD-CANCELLED-BURNED"))
    control = next(
        c for c in evaluate(holder).result.controls if c.control_id.endswith(NO_SETTLEMENT)
    )
    assert (control.status, control.reason_code) == ("FAIL", "SETTLED_DESPITE_CANCELLATION")


def test_under_1_4_0_an_absence_is_judged_on_the_most_recent_certificate(
    corpus: Corpus, previous: Corpus
) -> None:
    """Review M1: without a declared policy the absence of settlement is judged on the most
    recent certificate, as 1.4.0 promised; a later gapped Horizon certificate leaves it
    UNKNOWN there, while 1.5.0 uses the earlier active one that meets the coverage."""
    base = corpus.inputs_for("RD-CANCELLED")
    cid = chain_id(base)
    known = base.coverage[cid].recorded_at
    sac = scoped(
        base.coverage[cid],
        "rpc_sac_events",
        cid,
        recorded_at=(known - timedelta(seconds=2)).isoformat(),
    )
    start = sac.interval.start
    gapped = scoped(
        base.coverage[cid],
        "horizon_payments",
        "cov-derived-horizon-gapped",
        recorded_at=(known - timedelta(seconds=1)).isoformat(),
        gaps=[{"start": start.isoformat(), "end": (start + timedelta(seconds=1)).isoformat()}],
    )
    inputs = dataclasses.replace(
        base,
        snapshot=base.snapshot.model_copy(
            update={"coverage_ids": sorted([*base.snapshot.coverage_ids, gapped.coverage_id])}
        ),
        coverage={**base.coverage, cid: sac, gapped.coverage_id: gapped},
    )
    assert evaluate(inputs).result.result == "MATCH"
    assert status(evaluate(under(inputs, previous)).result, "no_settlement_after_cancellation") == (
        "UNKNOWN",
        "INSUFFICIENT_COVERAGE",
    )
