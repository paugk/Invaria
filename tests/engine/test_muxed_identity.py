"""Muxed sub-accounts and memos in
the engines.

Subscription 0.8.0 on the real DEMOA evidence of the testnet demo, with DERIVED muxed
movements (the demo has none): under fund-subscription-testnet@1.3.0 and @1.4.0 a movement
to a muxed sub-account is attributed only by an IdentityLink naming that M address. Profiles
1.0.0 to 1.2.0 do not admit the mapping that produces today's evidence: a new evaluation
under them is refused. Redemption 0.9.0 on the synthetic DEMO-A corpus: a muxed
movement is never a burn. The retired engines, which never received one, answer
UNSUPPORTED instead of guessing.

B2 (decision of 2026-10-08): a movement never counted because its destination is not
attributed leaves UNKNOWN only what it could change. ``GVSM`` sets the same evidence with a
G and with an M destination side by side; where they differ, the difference is one of
identity (an IdentityLink names one and not the other).

``EXPECTED``, ``GVSM`` and ``CHANGED_FROM_0_7_0`` were written before the engines ran.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from invaria.contracts.base import parse_contract
from invaria.contracts.coverage import QuarantinedRecord
from invaria.contracts.evaluation import EvaluationResult
from invaria.contracts.identity import IdentityLink
from invaria.contracts.observation import Observation, TokenMovementPayload
from invaria.contracts.profile import OperationProfile, admitted_mapping_refs, parse_profile
from invaria.contracts.stellar import encode_muxed_account
from invaria.corpus_loader import load_corpus
from invaria.engine.common import bears_by_address, by_address, by_address_muxed
from invaria.engine.evaluate import ENGINE_REF, EvaluationInputs, evaluate, replay
from invaria.engine.versions import REDEMPTION_ENGINE_REF
from invaria.vertical_testnet import OnchainRun, run_testnet_vertical

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CORPORA = FIXTURES / "corpus"
STELLAR = FIXTURES / "stellar"
MAPPINGS = CORPORA / "subscription-synthetic/mappings"
INVESTOR = "GC6XNJNZOULFUZJBYORCTVNWI6UX5AVOEDC7EXDQM77BPFMQA3VWOYLK"
ISSUER = "GCGGXYAKBIOKOIMVSUTXPEHRLJJU7VB7ZIYYKUFXYIVZTTFIWKEKTBEP"
STRANGER = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
ACCOUNT = "acct-pseudo-0001"
TOKEN = "subscription.token_units_vs_order"
PREVIOUS = "invaria-engine@0.7.0"


def _profile(suffix: str) -> OperationProfile:
    profile = parse_profile(
        (CORPORA / f"subscription-testnet{suffix}/profile.json").read_text("utf-8")
    )
    assert isinstance(profile, OperationProfile)
    return profile


PROFILES = {
    v: _profile(s)
    for v, s in (
        ("1.0.0", ""),
        ("1.1.0", "-1.1.0"),
        ("1.2.0", "-1.2.0"),
        ("1.3.0", "-1.3.0"),
        ("1.4.0", "-1.4.0"),
    )
}

MATCH = ("MATCH", "PASS", "EXACT_MATCH")
BREAK = ("BREAK", "FAIL", "UNITS_MISMATCH")
AMBIGUOUS = ("UNKNOWN", "UNKNOWN", "AMBIGUOUS_MATCH")
UNSUPPORTED = ("UNKNOWN", "UNKNOWN", "UNSUPPORTED_CAPABILITY")
MISSING = ("UNKNOWN", "UNKNOWN", "MISSING_EVIDENCE")
CONFLICT = ("UNKNOWN", "UNKNOWN", "SOURCE_CONFLICT")
# Profiles 1.0.0 to 1.2.0 admit stellar-classic-payment@1.0.0; today's evidence is 1.1.0.
REFUSED = ("UNKNOWN", "UNKNOWN", "EVALUATION_ERROR")
OLD = {"1.2.0": REFUSED, "1.1.0": REFUSED, "1.0.0": REFUSED}

# case -> {profile: (result, token control status, reason)}, engine 0.8.0.
EXPECTED: dict[str, dict[str, tuple[str, str, str]]] = {
    # The linked delivery T1 goes to the investor's base account: as in the demo.
    "plain delivery": {"1.4.0": MATCH, "1.3.0": MATCH, **OLD},
    # T1 to the sub-account 5 of the investor's base account, no link to it: the base
    # account's link does not attribute it; never counted, the delivery is unresolved.
    "to an unlinked sub-account": {"1.4.0": AMBIGUOUS, "1.3.0": AMBIGUOUS, **OLD},
    # ... with an IdentityLink (schema 1.1) naming that M address: attributed.
    "to a linked sub-account": {"1.4.0": MATCH, "1.3.0": MATCH, **OLD},
    # ... with a link to the sibling sub-account 6 only: still not attributed.
    "to a sibling of a linked sub-account": {"1.4.0": AMBIGUOUS},
    # ... with a link to that M address that expired before the delivery: not attributed.
    "to a sub-account linked outside its window": {"1.4.0": AMBIGUOUS},
    # ... with a link to that M address for another account: the execution linked to this
    # order went to someone else's sub-account: never counted, never ignored.
    "to a sub-account linked to another account": {"1.4.0": AMBIGUOUS},
    # T1 sent from the issuer's sub-account 3 to the investor's base account: the receiver
    # is attributed; a muxed sender is the issuer's own sub-account.
    "from an issuer sub-account": {"1.4.0": MATCH, "1.3.0": MATCH},
    # B2: an extra, unlinked movement to the investor's sub-account 9 beside the linked T1.
    # Under explicit execution links it can never count, resolved or not, as an unlinked
    # movement to the investor's G address beside a linked delivery (the demo's T3).
    "extra to an investor sub-account": {"1.4.0": MATCH, "1.3.0": MATCH},
    # The same beside an excess of linked deliveries (T1 and T3, TN-OVER-LINKED).
    "extra to an investor sub-account, excess": {"1.4.0": BREAK, "1.3.0": BREAK},
    # An extra muxed movement between strangers: outside the declared scope.
    "extra between strangers": {"1.4.0": MATCH, "1.3.0": MATCH},
    # Nothing linked to the order and an unlinked movement to the investor's sub-account 9
    # (no unlinked G delivery): it could be the delivery whose link is missing.
    "extra to an investor sub-account, nothing linked": {"1.4.0": AMBIGUOUS},
    # ... the same sub-account attributed by an IdentityLink to another account: foreign;
    # nothing bears and nothing is delivered (absence is not shown as a delivery).
    "extra to a sub-account linked to another account, nothing linked": {"1.4.0": MISSING},
    # A further execution linked to the order, to the unattributed sub-account 9, beside
    # the linked T1: it could add to the delivery (equal is UNKNOWN) ...
    "linked extra to an investor sub-account": {"1.4.0": AMBIGUOUS, "1.3.0": AMBIGUOUS},
    # ... but never undo a demonstrated excess (T1 and T3 linked).
    "linked extra to an investor sub-account, excess": {"1.4.0": BREAK, "1.3.0": BREAK},
    # A second reading of the attributed T1 to sub-account 5, under the same record key and
    # identical: one execution, counted once.
    "identical second reading of a linked sub-account delivery": {"1.4.0": MATCH},
    # A second reading of T1 under the same record key naming a sub-account: the readings
    # contradict each other; neither is chosen.
    "second reading naming a sub-account": {"1.4.0": CONFLICT},
}
SCENARIO = {
    "extra to an investor sub-account, excess": "TN-OVER-LINKED",
    "linked extra to an investor sub-account, excess": "TN-OVER-LINKED",
    "extra to an investor sub-account, nothing linked": "TN-NO-LINK",
    "extra to a sub-account linked to another account, nothing linked": "TN-NO-LINK",
}

# The same evidence with a G destination (left) and with an M destination (right), profile
# 1.4.0, engine 0.8.0: (G case, M case, G outcome, M outcome).
GVSM: list[tuple[str, str, tuple[str, str, str], tuple[str, str, str]]] = [
    ("extra to the investor", "extra to an investor sub-account", MATCH, MATCH),
    (
        "extra to the investor, nothing linked",
        "extra to an investor sub-account, nothing linked",
        AMBIGUOUS,
        AMBIGUOUS,
    ),
    (
        "extra to an address linked to another account, nothing linked",
        "extra to a sub-account linked to another account, nothing linked",
        MISSING,
        MISSING,
    ),
    (
        "linked extra to an unapproved address",
        "linked extra to an investor sub-account",
        AMBIGUOUS,
        AMBIGUOUS,
    ),
    (
        "linked extra to an unapproved address, excess",
        "linked extra to an investor sub-account, excess",
        BREAK,
        BREAK,
    ),
    ("delivery to an unapproved address", "to an unlinked sub-account", AMBIGUOUS, AMBIGUOUS),
    (
        "identical second reading of the delivery",
        "identical second reading of a linked sub-account delivery",
        MATCH,
        MATCH,
    ),
    (
        "second reading naming another address",
        "second reading naming a sub-account",
        CONFLICT,
        CONFLICT,
    ),
]
GVSM_SCENARIO = {
    "extra to the investor, nothing linked": "TN-NO-LINK",
    "extra to an address linked to another account, nothing linked": "TN-NO-LINK",
    "linked extra to an unapproved address, excess": "TN-OVER-LINKED",
}

# What the retired 0.7.0 gave where 0.8.0 differs, under 1.3.0 (replay): the B2 rule it
# applied (an unlinked muxed movement beside a linked delivery bears) and the linked
# delivery to an address without an approved link, which blocked every comparison.
CHANGED_FROM_0_7_0: dict[str, tuple[str, str, str]] = {
    "extra to an investor sub-account": AMBIGUOUS,
    "linked extra to an unapproved address, excess": AMBIGUOUS,
}


@pytest.fixture(scope="module")
def runs() -> dict[str, OnchainRun]:
    return {
        r.scenario.scenario_id: r
        for r in run_testnet_vertical(CORPORA / "subscription-testnet-1.4.0", STELLAR, MAPPINGS)
    }


# ------------------------------------------------------------------- helpers


def with_profile(inputs: EvaluationInputs, profile: OperationProfile) -> EvaluationInputs:
    snapshot = inputs.snapshot.model_copy(
        update={
            "profile_ref": profile.profile_ref,
            "rules_ref": profile.rules_ref,
            "mapping_refs": admitted_mapping_refs(profile.sources),
        }
    )
    return dataclasses.replace(inputs, snapshot=snapshot, profile=profile)


def delivery(inputs: EvaluationInputs) -> Observation:
    return next(
        o
        for i in sorted(inputs.snapshot.observation_ids)
        if (o := inputs.observations[i]).operation_ref == "SUB-0001"
        and isinstance(o.payload, TokenMovementPayload)
    )


def with_payload(inputs: EvaluationInputs, o: Observation, **fields: object) -> EvaluationInputs:
    assert isinstance(o.payload, TokenMovementPayload)
    changed = o.model_copy(update={"payload": o.payload.model_copy(update=fields)})
    return dataclasses.replace(
        inputs, observations={**inputs.observations, o.observation_id: changed}
    )


def with_extra(
    inputs: EvaluationInputs,
    sender: str,
    receiver: str,
    to_muxed_id: str | None,
    *,
    linked: bool = False,
) -> EvaluationInputs:
    """A derived movement of 1 DEMOA on another ledger operation, unlinked unless
    ``linked`` (an ExecutionLink to the order); a real DEMOA payment is its template."""
    o = next(
        o
        for i in sorted(inputs.observations)
        if isinstance((o := inputs.observations[i]).payload, TokenMovementPayload)
        and o.payload.chain.tx_successful
    )
    assert isinstance(o.payload, TokenMovementPayload)
    chain = o.payload.chain.model_copy(update={"tx_hash": "ab" * 32})
    payload = o.payload.model_copy(
        update={
            "from_address": sender,
            "to_address": receiver,
            "to_muxed_id": to_muxed_id,
            "units": o.payload.units.model_copy(update={"atoms": "10000000"}),
            "chain": chain,
        }
    )
    extra = o.model_copy(
        update={
            "observation_id": "obs-derived-muxed",
            "operation_ref": "SUB-0001" if linked else None,
            "source": o.source.model_copy(update={"record_key": f"{'ab' * 32}:0:0"}),
            "payload": payload,
        }
    )
    snapshot = inputs.snapshot.model_copy(
        update={"observation_ids": sorted([*inputs.snapshot.observation_ids, extra.observation_id])}
    )
    return dataclasses.replace(
        inputs,
        snapshot=snapshot,
        observations={**inputs.observations, extra.observation_id: extra},
    )


def with_link(
    inputs: EvaluationInputs, address: str, account: str = ACCOUNT, valid_to: str | None = None
) -> EvaluationInputs:
    link = parse_contract(
        IdentityLink,
        json.dumps(
            {
                "schema_version": "1.1",
                "link_id": f"link-derived-{address[-8:]}-{account}",
                "account_ref": account,
                "network": "stellar:testnet",
                "address": address,
                "valid_from": "2026-10-05T00:00:00Z",
                "valid_to": valid_to,
                "approval_ref": "approval-derived",
                "recorded_at": "2026-10-05T00:00:00Z",
            }
        ),
    )
    snapshot = inputs.snapshot.model_copy(
        update={"identity_link_ids": sorted([*inputs.snapshot.identity_link_ids, link.link_id])}
    )
    return dataclasses.replace(
        inputs,
        snapshot=snapshot,
        identity_links={**inputs.identity_links, link.link_id: link},
    )


def outcome(result: EvaluationResult) -> tuple[str, str, str]:
    control = next(c for c in result.controls if c.control_id == TOKEN)
    return result.result, control.status, control.reason_code


def m(sub_account: int) -> str:
    return encode_muxed_account(INVESTOR, sub_account)


def without_unlinked_deliveries(inputs: EvaluationInputs) -> EvaluationInputs:
    """Drop T3 (an unlinked payment to the investor's base account) so that only the muxed
    rule can make the delivery undecided (mutation testing found that T3 alone did)."""
    kept = [
        i
        for i in inputs.snapshot.observation_ids
        if not (
            isinstance((o := inputs.observations[i]).payload, TokenMovementPayload)
            and o.operation_ref is None
            and o.payload.chain.tx_successful
        )
    ]
    return dataclasses.replace(
        inputs, snapshot=inputs.snapshot.model_copy(update={"observation_ids": kept})
    )


def with_second_reading(inputs: EvaluationInputs, **fields: object) -> EvaluationInputs:
    """A second reading of the delivery T1 under its record key (another observation id,
    the payload changed by ``fields``): DERIVED."""
    o = delivery(inputs)
    assert isinstance(o.payload, TokenMovementPayload)
    second = o.model_copy(
        update={
            "observation_id": "obs-derived-second-reading",
            "payload": o.payload.model_copy(update=fields),
        }
    )
    snapshot = inputs.snapshot.model_copy(
        update={
            "observation_ids": sorted([*inputs.snapshot.observation_ids, second.observation_id])
        }
    )
    return dataclasses.replace(
        inputs,
        snapshot=snapshot,
        observations={**inputs.observations, second.observation_id: second},
    )


def derive(case: str, inputs: EvaluationInputs) -> EvaluationInputs:
    if case.startswith(("to ", "from ", "delivery ", "identical ", "second ")) or case.endswith(
        "nothing linked"
    ):
        inputs = without_unlinked_deliveries(inputs)
    if case == "plain delivery":
        return inputs
    if case == "to an unlinked sub-account":
        return with_payload(inputs, delivery(inputs), to_muxed_id="5")
    if case == "to a linked sub-account":
        return with_link(with_payload(inputs, delivery(inputs), to_muxed_id="5"), m(5))
    if case == "to a sibling of a linked sub-account":
        return with_link(with_payload(inputs, delivery(inputs), to_muxed_id="5"), m(6))
    if case == "to a sub-account linked outside its window":
        return with_link(
            with_payload(inputs, delivery(inputs), to_muxed_id="5"),
            m(5),
            valid_to="2026-10-05T00:00:01Z",
        )
    if case == "to a sub-account linked to another account":
        return with_link(
            with_payload(inputs, delivery(inputs), to_muxed_id="5"), m(5), "acct-other"
        )
    if case == "from an issuer sub-account":
        return with_payload(inputs, delivery(inputs), from_muxed_id="3")
    if case == "extra to a sub-account linked to another account, nothing linked":
        return with_link(with_extra(inputs, ISSUER, INVESTOR, "9"), m(9), "acct-other")
    if case.startswith("extra to an investor sub-account"):
        return with_extra(inputs, ISSUER, INVESTOR, "9")
    if case == "extra between strangers":
        return with_extra(inputs, ISSUER, STRANGER, "1")
    if case.startswith("linked extra to an investor sub-account"):
        return with_extra(inputs, ISSUER, INVESTOR, "9", linked=True)
    if case == "identical second reading of a linked sub-account delivery":
        attributed = with_link(with_payload(inputs, delivery(inputs), to_muxed_id="5"), m(5))
        return with_second_reading(attributed)
    if case == "second reading naming a sub-account":
        return with_second_reading(inputs, to_muxed_id="5")
    # The G side of GVSM.
    if case.startswith("extra to the investor"):
        return with_extra(inputs, ISSUER, INVESTOR, None)
    if case == "extra to an address linked to another account, nothing linked":
        return with_link(with_extra(inputs, ISSUER, STRANGER, None), STRANGER, "acct-other")
    if case.startswith("linked extra to an unapproved address"):
        return with_extra(inputs, ISSUER, STRANGER, None, linked=True)
    if case == "delivery to an unapproved address":
        return with_payload(inputs, delivery(inputs), to_address=STRANGER)
    if case == "identical second reading of the delivery":
        return with_second_reading(inputs)
    if case == "second reading naming another address":
        return with_second_reading(inputs, to_address=STRANGER)
    raise AssertionError(case)


CASES = [(case, version) for case, by in EXPECTED.items() for version in by]


@pytest.mark.parametrize(("case", "version"), CASES, ids=[f"{c}@{v}" for c, v in CASES])
def test_muxed_attribution_by_profile(runs: dict[str, OnchainRun], case: str, version: str) -> None:
    run = runs[SCENARIO.get(case, "TN-LINKED")]
    inputs = with_profile(derive(case, run.inputs), PROFILES[version])
    result = evaluate(inputs).result
    assert result.versions.engine_ref == ENGINE_REF
    assert outcome(result) == EXPECTED[case][version], case
    control = next(c for c in result.controls if c.control_id == TOKEN)
    if EXPECTED[case][version] == AMBIGUOUS:
        assert "attributes" in control.reason, control.reason
    if EXPECTED[case][version] == REFUSED:
        assert "stellar-classic-payment@1.1.0" in control.reason, control.reason
        assert "admits: stellar-classic-payment@1.0.0" in control.reason, control.reason


@pytest.mark.parametrize(
    ("g_case", "m_case", "g_expected", "m_expected"), GVSM, ids=[m for _, m, _, _ in GVSM]
)
def test_g_and_m_destinations_with_equivalent_evidence(
    runs: dict[str, OnchainRun],
    g_case: str,
    m_case: str,
    g_expected: tuple[str, str, str],
    m_expected: tuple[str, str, str],
) -> None:
    """B2: the same evidence with a G and with an M destination. Every pair agrees: the only
    difference between them is the destination's identity, and in each pair an IdentityLink
    resolves it, or fails to, the same way for both."""
    for case, expected, scenario in (
        (g_case, g_expected, GVSM_SCENARIO.get(g_case)),
        (m_case, m_expected, SCENARIO.get(m_case)),
    ):
        run = runs[scenario or "TN-LINKED"]
        result = evaluate(with_profile(derive(case, run.inputs), PROFILES["1.4.0"])).result
        assert outcome(result) == expected, case
    assert g_expected == m_expected


@pytest.mark.parametrize("case", sorted(CHANGED_FROM_0_7_0))
def test_what_0_8_0_changes_from_0_7_0(runs: dict[str, OnchainRun], case: str) -> None:
    """The retired 0.7.0 (replay) under 1.3.0 gives the earlier answer; 0.8.0 the new one.
    Every other case of ``EXPECTED`` under 1.3.0 is the same under both."""
    scenario = SCENARIO.get(case) or GVSM_SCENARIO.get(case) or "TN-LINKED"
    inputs = with_profile(derive(case, runs[scenario].inputs), PROFILES["1.3.0"])
    assert outcome(replay(inputs, PREVIOUS).result) == CHANGED_FROM_0_7_0[case]
    assert outcome(evaluate(inputs).result) != CHANGED_FROM_0_7_0[case]


def test_0_7_0_and_0_8_0_agree_elsewhere(runs: dict[str, OnchainRun]) -> None:
    for case, by in EXPECTED.items():
        if "1.3.0" not in by or case in CHANGED_FROM_0_7_0:
            continue
        inputs = with_profile(
            derive(case, runs[SCENARIO.get(case, "TN-LINKED")].inputs), PROFILES["1.3.0"]
        )
        assert outcome(replay(inputs, PREVIOUS).result) == by["1.3.0"], case


def test_a_muxed_excess_is_reported_as_weighed(runs: dict[str, OnchainRun]) -> None:
    case = "linked extra to an investor sub-account, excess"
    inputs = derive(case, runs["TN-OVER-LINKED"].inputs)
    control = next(c for c in evaluate(inputs).result.controls if c.control_id == TOKEN)
    assert "could only add to an excess" in control.reason
    assert "obs-derived-muxed" in control.evidence_refs


def test_the_assumptions_state_the_declared_rule(runs: dict[str, OnchainRun]) -> None:
    run = runs["TN-LINKED"]
    declared = run.evaluation.result.assumptions
    assert any("only by an IdentityLink naming that M address" in a for a in declared)
    assert any("never links an observation to an operation" in a for a in declared)
    assert any("evidence_mapping_refs" in a for a in declared)
    earlier = replay(with_profile(run.inputs, PROFILES["1.2.0"]), PREVIOUS).result.assumptions
    assert any("does not declare how a muxed sub-account" in a for a in earlier)


def test_the_retired_engine_never_guesses_a_muxed_movement(runs: dict[str, OnchainRun]) -> None:
    """0.6.0 never received a muxed movement (its adapter quarantined them): UNSUPPORTED,
    never attributed through the base account's link. It cannot evaluate 1.3.0 at all."""
    inputs = with_profile(
        derive("to an unlinked sub-account", runs["TN-LINKED"].inputs), PROFILES["1.2.0"]
    )
    assert outcome(replay(inputs, "invaria-engine@0.6.0").result) == UNSUPPORTED
    plain = with_profile(runs["TN-LINKED"].inputs, PROFILES["1.2.0"])
    assert outcome(replay(plain, "invaria-engine@0.6.0").result) == MATCH
    declared = replay(
        with_profile(
            derive("to a linked sub-account", runs["TN-LINKED"].inputs), PROFILES["1.3.0"]
        ),
        "invaria-engine@0.6.0",
    )
    assert {c.reason_code for c in declared.result.controls} == {"EVALUATION_ERROR"}
    assert "muxed sub-account is attributed" in declared.result.controls[0].reason


def test_a_memo_naming_the_order_never_links_the_delivery(runs: dict[str, OnchainRun]) -> None:
    """T1 and T3 carry the memo SUB-0001; without an ExecutionLink nothing is counted."""
    run = runs["TN-NO-LINK"]
    movements = [
        o
        for i in run.snapshot.observation_ids
        if isinstance((o := run.inputs.observations[i]).payload, TokenMovementPayload)
    ]
    assert any(
        isinstance(o.payload, TokenMovementPayload)
        and o.payload.memo is not None
        and o.payload.memo.value == "U1VCLTAwMDE="
        for o in movements
    )
    assert outcome(run.evaluation.result) == AMBIGUOUS


# --------------------------------------------------------- relevance helpers


def test_a_sub_account_of_an_approved_base_bears_and_the_reverse_does_not() -> None:
    m5 = encode_muxed_account(INVESTOR, 5)
    stranger_m = encode_muxed_account(STRANGER, 5)
    assert bears_by_address([m5], {INVESTOR})  # unresolved holder: never foreign
    assert bears_by_address([m5], {m5})
    assert not bears_by_address([INVESTOR], {m5})  # an approved sub-account's base is not
    assert not bears_by_address([encode_muxed_account(INVESTOR, 6)], {m5})  # nor a sibling
    assert not bears_by_address([stranger_m], {INVESTOR})
    assert bears_by_address([stranger_m], None)
    record = QuarantinedRecord.model_validate(
        {"locator": "x", "reasons": ["MUXED_ACCOUNT"], "addresses": [STRANGER, stranger_m]}
    )
    assert not by_address_muxed(record, {INVESTOR})
    assert by_address(record, {INVESTOR})  # the earlier rule: any M counts everywhere


# ------------------------------------------------------------------ redemption


REDEMPTION_INVESTOR = "GA2JRO6WKTHMJTH7TDAXHNDHWT5SF72VKABPO2G6RL5BCGKTNIPIBEKW"
BURN = "redemption.burn_vs_request"
NO_SETTLEMENT = "redemption.no_settlement_after_cancellation"


def _burn(inputs: EvaluationInputs) -> Observation:
    return next(
        o
        for i in inputs.snapshot.observation_ids
        if isinstance((o := inputs.observations[i]).payload, TokenMovementPayload)
    )


def _status(result: EvaluationResult, control_id: str) -> tuple[str, str]:
    control = next(c for c in result.controls if c.control_id == control_id)
    return control.status, control.reason_code


@pytest.mark.parametrize("version", ["1.5.0", "1.4.0"])
def test_a_linked_muxed_burn_is_never_a_burn(version: str) -> None:
    corpus = load_corpus(CORPORA / f"redemption-synthetic-{version}")
    inputs = corpus.inputs_for("RD-PAID")
    burn = _burn(inputs)
    muxed = with_payload(inputs, burn, from_muxed_id="0")
    result = evaluate(muxed).result
    assert result.versions.engine_ref == REDEMPTION_ENGINE_REF
    assert _status(result, BURN) == ("UNKNOWN", "AMBIGUOUS_MATCH")
    assert _status(evaluate(inputs).result, BURN) == ("PASS", "EXACT_MATCH")
    retired = replay(muxed, "invaria-redemption-engine@0.7.0").result
    assert _status(retired, BURN) == ("UNKNOWN", "UNSUPPORTED_CAPABILITY")


# (equal, excess) burn plus an unlinked muxed movement from the investor's base account.
# Domain review M1: the demonstrated excess stays a FAIL under 1.4.0 too (its promise never
# blocked a burn comparison with the quarantine); B5: a muxed movement has its own reason.
# B2 (0.9.0): beside the linked burn an unlinked movement can never be a burn of the request,
# so the equal burn passes where the profile scopes its quarantine (1.4.0, 1.5.0); under
# 1.3.0 every muxed movement of the source still bears. This table was 0.8.0's (UNKNOWN for
# the equal burn under every profile) and was revised for 0.9.0 after its first run: the
# change is the B2 rule itself, which 0.8.0 still gives (``REDEMPTION_0_8_0``).
REDEMPTION_EXPECTED = {
    "1.5.0": (("PASS", "EXACT_MATCH"), ("FAIL", "UNITS_MISMATCH")),
    "1.4.0": (("PASS", "EXACT_MATCH"), ("FAIL", "UNITS_MISMATCH")),
    "1.3.0": (("UNKNOWN", "AMBIGUOUS_MATCH"), ("FAIL", "UNITS_MISMATCH")),
}
REDEMPTION_0_8_0 = (("UNKNOWN", "AMBIGUOUS_MATCH"), ("FAIL", "UNITS_MISMATCH"))


def _redemption_extra(
    inputs: EvaluationInputs, sender: str, *, linked: bool = False, muxed: bool = True
) -> EvaluationInputs:
    o = _burn(inputs)
    assert isinstance(o.payload, TokenMovementPayload)
    payload = o.payload.model_copy(
        update={
            "from_address": sender,
            "from_muxed_id": "4" if muxed else None,
            "chain": o.payload.chain.model_copy(update={"tx_hash": "cd" * 32}),
        }
    )
    extra = o.model_copy(
        update={
            "observation_id": "obs-derived-muxed-burn",
            "operation_ref": inputs.snapshot.operation_ref if linked else None,
            "source": o.source.model_copy(update={"record_key": f"{'cd' * 32}:0:0"}),
            "payload": payload,
        }
    )
    snapshot = inputs.snapshot.model_copy(
        update={"observation_ids": sorted([*inputs.snapshot.observation_ids, extra.observation_id])}
    )
    return dataclasses.replace(
        inputs, snapshot=snapshot, observations={**inputs.observations, extra.observation_id: extra}
    )


@pytest.mark.parametrize("version", sorted(REDEMPTION_EXPECTED))
def test_an_unattributed_muxed_movement_weighs_on_the_burn(version: str) -> None:
    corpus = load_corpus(
        CORPORA
        / ("redemption-synthetic" if version == "1.3.0" else f"redemption-synthetic-{version}")
    )
    equal = corpus.inputs_for("RD-PAID")
    o = _burn(equal)
    assert isinstance(o.payload, TokenMovementPayload)
    excess = with_payload(
        equal, o, units=o.payload.units.model_copy(update={"atoms": "1500000000"})
    )
    got = tuple(
        _status(evaluate(_redemption_extra(i, REDEMPTION_INVESTOR)).result, BURN)
        for i in (equal, excess)
    )
    assert got == REDEMPTION_EXPECTED[version]
    before = tuple(
        _status(
            replay(
                _redemption_extra(i, REDEMPTION_INVESTOR), "invaria-redemption-engine@0.8.0"
            ).result,
            BURN,
        )
        for i in (equal, excess)
    )
    assert before == REDEMPTION_0_8_0
    # Linked to the request, the same movement is a further execution: an equal burn is
    # UNKNOWN, an excess stays a FAIL.
    linked = tuple(
        _status(evaluate(_redemption_extra(i, REDEMPTION_INVESTOR, linked=True)).result, BURN)
        for i in (equal, excess)
    )
    assert linked == (("UNKNOWN", "AMBIGUOUS_MATCH"), ("FAIL", "UNITS_MISMATCH"))
    # From a stranger's base account: outside the declared scope where the profile scopes its
    # on-chain quarantine (1.4.0, 1.5.0); under 1.3.0 every muxed movement bears.
    stranger = _status(evaluate(_redemption_extra(equal, STRANGER)).result, BURN)
    assert stranger == (
        ("UNKNOWN", "AMBIGUOUS_MATCH") if version == "1.3.0" else ("PASS", "EXACT_MATCH")
    )


def test_absence_of_settlement_needs_the_muxed_movement_resolved_but_a_proven_one_stays() -> None:
    corpus = load_corpus(CORPORA / "redemption-synthetic-1.5.0")
    cancelled = _redemption_extra_cancelled(corpus.inputs_for("RD-CANCELLED"))
    assert _status(evaluate(cancelled).result, NO_SETTLEMENT)[0] == "UNKNOWN"
    burned = corpus.inputs_for("RD-CANCELLED-BURNED")
    assert _status(
        evaluate(_redemption_extra(burned, REDEMPTION_INVESTOR)).result, NO_SETTLEMENT
    ) == (
        "FAIL",
        "SETTLED_DESPITE_CANCELLATION",
    )


def _redemption_extra_cancelled(inputs: EvaluationInputs) -> EvaluationInputs:
    """RD-CANCELLED has no burn: a derived muxed movement from the investor's base."""
    corpus = load_corpus(CORPORA / "redemption-synthetic-1.5.0")
    template = _burn(corpus.inputs_for("RD-PAID"))
    assert isinstance(template.payload, TokenMovementPayload)
    extra = template.model_copy(
        update={
            "observation_id": "obs-derived-muxed-cancelled",
            "operation_ref": None,
            "source": template.source.model_copy(update={"record_key": f"{'ee' * 32}:0:0"}),
            "valid_time": datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
            "recorded_at": min(template.recorded_at, inputs.snapshot.known_at),
            "payload": template.payload.model_copy(
                update={
                    "from_muxed_id": "4",
                    "chain": template.payload.chain.model_copy(update={"tx_hash": "ee" * 32}),
                }
            ),
        }
    )
    snapshot = inputs.snapshot.model_copy(
        update={"observation_ids": sorted([*inputs.snapshot.observation_ids, extra.observation_id])}
    )
    return dataclasses.replace(
        inputs, snapshot=snapshot, observations={**inputs.observations, extra.observation_id: extra}
    )


def test_absence_of_settlement_under_a_profile_without_policy() -> None:
    """Profile 1.4.0 (scoped, no policy): a muxed movement from the investor's base account
    leaves the absence of settlement UNKNOWN, as the quarantined record did."""
    corpus = load_corpus(CORPORA / "redemption-synthetic-1.4.0")
    cancelled = _redemption_extra_cancelled_from(corpus, corpus.inputs_for("RD-CANCELLED"))
    assert _status(evaluate(cancelled).result, NO_SETTLEMENT) == ("UNKNOWN", "AMBIGUOUS_MATCH")


def _redemption_extra_cancelled_from(corpus: object, inputs: EvaluationInputs) -> EvaluationInputs:
    template = _burn(corpus.inputs_for("RD-PAID"))  # type: ignore[attr-defined]
    assert isinstance(template.payload, TokenMovementPayload)
    extra = template.model_copy(
        update={
            "observation_id": "obs-derived-muxed-cancelled",
            "operation_ref": None,
            "source": template.source.model_copy(update={"record_key": f"{'ee' * 32}:0:0"}),
            "valid_time": datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
            "recorded_at": min(template.recorded_at, inputs.snapshot.known_at),
            "payload": template.payload.model_copy(
                update={
                    "from_muxed_id": "4",
                    "chain": template.payload.chain.model_copy(update={"tx_hash": "ee" * 32}),
                }
            ),
        }
    )
    snapshot = inputs.snapshot.model_copy(
        update={"observation_ids": sorted([*inputs.snapshot.observation_ids, extra.observation_id])}
    )
    return dataclasses.replace(
        inputs, snapshot=snapshot, observations={**inputs.observations, extra.observation_id: extra}
    )


def test_a_linked_burn_to_an_issuer_sub_account_is_not_a_proven_settlement() -> None:
    """Declared limit (domain review): no redemption profile attributes a muxed movement, so a
    linked burn from the approved account to a sub-account of the issuer, after a
    cancellation, is UNKNOWN rather than SETTLED_DESPITE_CANCELLATION (before increment 4 it
    was quarantined: no regression)."""
    corpus = load_corpus(CORPORA / "redemption-synthetic-1.5.0")
    burned = corpus.inputs_for("RD-CANCELLED-BURNED")
    muxed = with_payload(burned, _burn(burned), to_muxed_id="2")
    status, reason = _status(evaluate(muxed).result, NO_SETTLEMENT)
    assert status == "UNKNOWN" and reason == "AMBIGUOUS_MATCH"
    assert _status(evaluate(burned).result, NO_SETTLEMENT)[0] == "FAIL"


STRANGER_BURN = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"


@pytest.mark.parametrize("version", ["1.5.0", "1.4.0"])
def test_a_linked_burn_from_an_unapproved_address_is_weighed_not_blocking(version: str) -> None:
    """The G side of B2 in redemption: a further burn linked to the request but sent from an
    address without an approved link is never counted. Under 1.5.0 (declared policy) 0.9.0
    weighs it like an unattributed muxed movement: an equal burn is UNKNOWN, an excess of
    counted burns stays a FAIL (the retired 0.8.0 made every burn comparison UNKNOWN). Under
    1.4.0 (no policy) the earlier promise holds: the view is ambiguous."""
    corpus = load_corpus(CORPORA / f"redemption-synthetic-{version}")
    equal = corpus.inputs_for("RD-PAID")
    o = _burn(equal)
    assert isinstance(o.payload, TokenMovementPayload)
    excess = with_payload(
        equal, o, units=o.payload.units.model_copy(update={"atoms": "1500000000"})
    )
    for inputs, weighed in (
        (equal, ("UNKNOWN", "AMBIGUOUS_MATCH")),
        (excess, ("FAIL", "UNITS_MISMATCH")),
    ):
        derived = _redemption_extra(inputs, STRANGER_BURN, linked=True, muxed=False)
        got = _status(evaluate(derived).result, BURN)
        retired = _status(replay(derived, "invaria-redemption-engine@0.8.0").result, BURN)
        assert retired == ("UNKNOWN", "AMBIGUOUS_MATCH")
        if version == "1.5.0":
            assert got == weighed
        else:
            assert got == ("UNKNOWN", "AMBIGUOUS_MATCH")


@pytest.mark.parametrize(
    "case", ["delivery to an unapproved address", "to an unlinked sub-account"]
)
def test_an_execution_linked_to_an_unattributed_destination_says_so(
    runs: dict[str, OnchainRun], case: str
) -> None:
    """With nothing counted, the token view names the linked execution whose destination no
    IdentityLink attributes (G without an approved link, or unattributed sub-account)."""
    inputs = with_profile(derive(case, runs["TN-LINKED"].inputs), PROFILES["1.4.0"])
    control = next(c for c in evaluate(inputs).result.controls if c.control_id == TOKEN)
    assert "linked to the operation name a destination" in control.reason, control.reason


def test_foreign_by_link_needs_a_profile_declaring_muxed_attribution(
    runs: dict[str, OnchainRun],
) -> None:
    """An IdentityLink attributes a sub-account to another account only where the profile
    declares how a muxed sub-account is attributed. 1.4.0 with that declaration removed (a
    DERIVED profile, no real version) does not show the same movement foreign: it bears."""
    case = "extra to a sub-account linked to another account, nothing linked"
    derived = derive(case, runs["TN-NO-LINK"].inputs)
    declared = evaluate(with_profile(derived, PROFILES["1.4.0"])).result
    profile = PROFILES["1.4.0"]
    stripped = profile.model_copy(
        update={"correlation": profile.correlation.model_copy(update={"muxed_account_link": None})}
    )
    undeclared = evaluate(with_profile(derived, stripped)).result
    assert outcome(declared) == MISSING
    assert outcome(undeclared) == AMBIGUOUS


def test_every_valid_sub_account_id_is_truthy() -> None:
    """Why ``not to_muxed_id`` and ``to_muxed_id is None`` cannot differ on a valid payload
    (the two equivalent mutations of increment 4): a sub-account id is canonical u64 text,
    never empty, so the only falsy value it can take is None. The payload is strict and
    every product path validates it (the adapter builds it with ``model_validate``, the
    stores parse it back); "" and "00" are refused."""
    for value in ("0", "1", str(2**53), "18446744073709551615"):
        payload = _payload(value)
        assert payload.to_muxed_id == value and bool(payload.to_muxed_id)
    for bad in ("", "00", "-0"):
        with pytest.raises(ValueError):
            _payload(bad)


def _payload(to_muxed_id: str) -> TokenMovementPayload:
    return TokenMovementPayload.model_validate(
        {
            "payload_type": "token_movement",
            "from_address": ISSUER,
            "to_address": INVESTOR,
            "to_muxed_id": to_muxed_id,
            "units": {"atoms": "1", "scale": 7, "unit": "FUND_SHARE"},
            "chain": {
                "network": "stellar:testnet",
                "ledger": 1,
                "tx_hash": "ab" * 32,
                "operation_index": 0,
                "tx_successful": True,
            },
        }
    )


def test_a_new_redemption_refuses_a_mapping_its_profile_does_not_admit() -> None:
    """M2 in redemption: an observation of a declared source produced by a mapping the
    profile does not admit refuses the new evaluation; replay reproduces without it."""
    corpus = load_corpus(CORPORA / "redemption-synthetic-1.5.0")
    inputs = corpus.inputs_for("RD-PAID")
    o = _burn(inputs)
    relabelled = o.model_copy(
        update={"provenance": o.provenance.model_copy(update={"mapping_ref": "elsewhere@2.0.0"})}
    )
    changed = dataclasses.replace(
        inputs, observations={**inputs.observations, o.observation_id: relabelled}
    )
    refused = evaluate(changed).result
    assert {c.reason_code for c in refused.controls} == {"EVALUATION_ERROR"}
    assert "elsewhere@2.0.0" in refused.controls[0].reason
    assert refused.versions.evidence_mapping_refs is not None
    assert "elsewhere@2.0.0" in refused.versions.evidence_mapping_refs
    assert replay(changed, REDEMPTION_ENGINE_REF).result == refused
    retired = replay(changed, "invaria-redemption-engine@0.8.0").result
    assert retired.result == evaluate(inputs).result.result


def test_an_unlinked_movement_that_cannot_be_a_burn_does_not_bear() -> None:
    """Review H2: the absence of settlement with an unlinked movement from the issuer to a
    sub-account of the investor. It cannot be a retirement (it does not go to the issuer),
    as the same movement to the investor's G address: the absence is shown for both. From
    the investor's sub-account to the issuer it could be the burn whose link is missing."""
    corpus = load_corpus(CORPORA / "redemption-synthetic-1.5.0")
    cancelled = corpus.inputs_for("RD-CANCELLED")
    template = _burn(corpus.inputs_for("RD-PAID"))
    assert isinstance(template.payload, TokenMovementPayload)
    issuer = template.payload.to_address

    def extra(
        sender: str, receiver: str, from_id: str | None, to_id: str | None
    ) -> EvaluationInputs:
        assert isinstance(template.payload, TokenMovementPayload)
        o = template.model_copy(
            update={
                "observation_id": "obs-derived-unlinked",
                "operation_ref": None,
                "source": template.source.model_copy(update={"record_key": f"{'ee' * 32}:0:0"}),
                "valid_time": datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
                "recorded_at": min(template.recorded_at, cancelled.snapshot.known_at),
                "payload": template.payload.model_copy(
                    update={
                        "from_address": sender,
                        "to_address": receiver,
                        "from_muxed_id": from_id,
                        "to_muxed_id": to_id,
                        "chain": template.payload.chain.model_copy(update={"tx_hash": "ee" * 32}),
                    }
                ),
            }
        )
        snapshot = cancelled.snapshot.model_copy(
            update={
                "observation_ids": sorted([*cancelled.snapshot.observation_ids, o.observation_id])
            }
        )
        return dataclasses.replace(
            cancelled,
            snapshot=snapshot,
            observations={**cancelled.observations, o.observation_id: o},
        )

    to_m = _status(evaluate(extra(issuer, REDEMPTION_INVESTOR, None, "7")).result, NO_SETTLEMENT)
    to_g = _status(evaluate(extra(issuer, REDEMPTION_INVESTOR, None, None)).result, NO_SETTLEMENT)
    assert to_m == to_g == ("PASS", "EXACT_MATCH")
    from_m = extra(REDEMPTION_INVESTOR, issuer, "7", None)
    assert _status(evaluate(from_m).result, NO_SETTLEMENT) == ("UNKNOWN", "AMBIGUOUS_MATCH")
    # From the investor's sub-account to a stranger: not a retirement either.
    away = extra(REDEMPTION_INVESTOR, STRANGER, "7", None)
    assert _status(evaluate(away).result, NO_SETTLEMENT) == ("PASS", "EXACT_MATCH")
    # From a stranger's sub-account to the issuer, even with the issuer's address approved
    # for the account (a DERIVED, pathological link): the burn would be the investor's only
    # by its sender, which is not approved.
    stranger_burn = with_link(extra(STRANGER, issuer, "7", None), issuer, "acct-pseudo-0001")
    assert _status(evaluate(stranger_burn).result, NO_SETTLEMENT) == ("PASS", "EXACT_MATCH")


def test_an_unlinked_movement_from_an_investor_sub_account_is_no_delivery(
    runs: dict[str, OnchainRun],
) -> None:
    """Review H2 (subscription): nothing linked, an unlinked movement from the investor's
    sub-account 9 to a stranger could not be the delivery (its receiver is not the
    investor's), as the same movement from the investor's G address: nothing bears. Under
    1.4.0 a muxed sender is attributable and never set aside; the movement is set aside
    only where the profile does not declare muxed attribution (a DERIVED 1.4.0 without it)."""
    profile = PROFILES["1.4.0"]
    stripped = profile.model_copy(
        update={"correlation": profile.correlation.model_copy(update={"muxed_account_link": None})}
    )
    base = without_unlinked_deliveries(runs["TN-NO-LINK"].inputs)
    for p in (profile, stripped):
        m_side = with_profile(with_extra(base, INVESTOR, STRANGER, None), p)
        o = m_side.observations["obs-derived-muxed"]
        assert isinstance(o.payload, TokenMovementPayload)
        m_side = with_payload(m_side, o, from_muxed_id="9")
        g_side = with_profile(with_extra(base, INVESTOR, STRANGER, None), p)
        assert outcome(evaluate(m_side).result) == outcome(evaluate(g_side).result) == MISSING


def test_the_retired_0_7_0_keeps_its_own_reason(runs: dict[str, OnchainRun]) -> None:
    """Review H4: the token view of 0.8.0 for an execution linked to an unattributed
    destination is not taken by the replay of 0.7.0, which states its own reason."""
    inputs = with_profile(
        derive("to an unlinked sub-account", runs["TN-LINKED"].inputs), PROFILES["1.3.0"]
    )
    old = next(c for c in replay(inputs, PREVIOUS).result.controls if c.control_id == TOKEN)
    new = next(c for c in evaluate(inputs).result.controls if c.control_id == TOKEN)
    assert old.reason_code == new.reason_code == "AMBIGUOUS_MATCH"
    assert "linked to the operation name a destination" not in old.reason
    assert "linked to the operation name a destination" in new.reason
