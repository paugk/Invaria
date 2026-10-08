"""Typed parties under the current engines (0.3.0 and 0.5.0).

The engines apply the rules they declare: a chain effect never satisfies
an obligation; one linked to the operation, or naming an address approved for its account
among its technical participants, makes only an equal or short comparison and an absence
UNKNOWN; an excess or a proven settlement stays FAIL; a clawback never bears on a
subscription delivery. Increment 3 adds participants (a non-account holder, a claimable
balance's creator and claimants). The one new rule, that an unidentified participant is
never ruled out, is subscription 0.4.0 and redemption 0.6.0.

The effects here are built on the synthetic corpora; their shapes are the adapter's.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from invaria.contracts.evaluation import EvaluationResult
from invaria.contracts.observation import (
    ChainEffectPayload,
    ChainParty,
    Observation,
    TokenMovementPayload,
    effect_parties,
)
from invaria.contracts.stellar import encode_strkey
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.common import chain_effects_bearing
from invaria.engine.evaluate import EvaluationInputs, evaluate, replay

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/corpus"
INVESTOR = "GA2JRO6WKTHMJTH7TDAXHNDHWT5SF72VKABPO2G6RL5BCGKTNIPIBEKW"
STRANGER = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
OTHER = "GCLCZEQZ2THTEDAOFI66LACNPLY4OBKN7VKLEZFMBIHYKYQOW2W7T3Z6"
BALANCE = encode_strkey("claimable_balance", b"\x00" + b"\x0b" * 32)
WALLET = encode_strkey("contract", b"\x0c" * 32)
POOL = encode_strkey("liquidity_pool", b"\x0d" * 32)


@pytest.fixture(scope="module")
def redemption() -> Corpus:
    return load_corpus(FIXTURES / "redemption-synthetic-1.4.0")


@pytest.fixture(scope="module")
def subscription() -> Corpus:
    return load_corpus(FIXTURES / "subscription-synthetic")


def _issuer(inputs: EvaluationInputs) -> tuple[str, str]:
    (rep,) = inputs.profile.representations
    return rep.asset_code, rep.issuer


def balance_context(
    creator: str | None, claimants: list[str] | None, operation: str | None = None
) -> dict[str, Any]:
    return {
        "balance_id": BALANCE,
        "operation_type": operation,
        "operation_source": None if operation is None else (creator or STRANGER),
        "creator": {"kind": "account", "id": creator}
        if creator
        else {"kind": "unresolved", "reason": "balance history not read"},
        "claimants": claimants,
    }


def typed(
    inputs: EvaluationInputs,
    template_from: EvaluationInputs,
    payload: dict[str, Any],
    *,
    operation_ref: str | None = None,
    instrument_id: str | None = None,
    label: str = "typed",
) -> EvaluationInputs:
    """``inputs`` with one chain effect built from ``payload`` (the adapter's shapes)."""
    template = next(
        o for o in template_from.observations.values() if o.fact_type == "token_movement"
    )
    assert isinstance(template.payload, TokenMovementPayload)
    code, issuer = _issuer(inputs)
    if payload.get("effect_kind") == "clawback":
        payload = {
            **payload,
            "counterparty": {"kind": "account", "id": issuer},
            "clawback": {
                "asset": f"{code}:{issuer}",
                "issuer": issuer,
                "operation_type": None,
                "operation_source": None,
            },
        }
    effect = Observation.model_validate_json(
        json.dumps(
            {
                **template.model_dump(mode="json"),
                "observation_id": f"obs-{label}",
                "fact_type": "chain_effect",
                "operation_ref": operation_ref,
                "instrument_id": instrument_id or template.instrument_id,
                "source": {**template.source.model_dump(), "record_key": f"typed:{label}"},
                "valid_time": inputs.snapshot.valid_at.isoformat(),
                "recorded_at": inputs.snapshot.known_at.isoformat(),
                "payload": {
                    "payload_type": "chain_effect",
                    "representation": "sac",
                    "direction": "debit",
                    "units": template.payload.units.model_dump(),
                    "chain": template.payload.chain.model_dump(mode="json"),
                    "path_payment": None,
                    "transaction": None,
                    **payload,
                },
            }
        )
    )
    snapshot = inputs.snapshot.model_copy(
        update={
            "observation_ids": sorted([*inputs.snapshot.observation_ids, effect.observation_id])
        }
    )
    return dataclasses.replace(
        inputs,
        snapshot=snapshot,
        observations={**inputs.observations, effect.observation_id: effect},
    )


def balance_clawback(creator: str | None, claimants: list[str] | None) -> dict[str, Any]:
    return {
        "effect_kind": "clawback",
        "account": None,
        "holder": {"kind": "claimable_balance", "id": BALANCE},
        "claimable_balance": balance_context(creator, claimants, "clawback_claimable_balance"),
    }


def balance_claim(claimant: str, creator: str | None) -> dict[str, Any]:
    return {
        "effect_kind": "claimable_balance_claimed",
        "account": claimant,
        "direction": "credit",
        "counterparty": {"kind": "claimable_balance", "id": BALANCE},
        "claimable_balance": balance_context(creator, [claimant], "claim_claimable_balance"),
    }


def movement(kind: str, account: str, party: dict[str, str], direction: str) -> dict[str, Any]:
    return {
        "effect_kind": kind,
        "account": account,
        "direction": direction,
        "counterparty": party,
    }


def status_of(result: EvaluationResult, control: str) -> tuple[str, str]:
    (c,) = [c for c in result.controls if c.control_id.endswith(control)]
    return c.status, c.reason_code


# ------------------------------------------------------------------- redemption


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # A clawback of a balance the investor created: relevant through its creator.
        (balance_clawback(INVESTOR, [OTHER]), "UNKNOWN"),
        # The investor is only a claimant: relevant (scope), never a recipient (attribution).
        (balance_clawback(STRANGER, [INVESTOR]), "UNKNOWN"),
        # Creator and claimants demonstrably foreign: nothing changes.
        (balance_clawback(STRANGER, [OTHER]), "MATCH"),
        (balance_claim(INVESTOR, STRANGER), "UNKNOWN"),
        (balance_claim(OTHER, STRANGER), "MATCH"),
        (
            movement("contract_transfer", INVESTOR, {"kind": "contract", "id": WALLET}, "debit"),
            "UNKNOWN",
        ),
        (
            movement("contract_transfer", STRANGER, {"kind": "contract", "id": WALLET}, "debit"),
            "MATCH",
        ),
        (
            movement("pool_transfer", INVESTOR, {"kind": "liquidity_pool", "id": POOL}, "debit"),
            "UNKNOWN",
        ),
        (
            movement("pool_transfer", OTHER, {"kind": "liquidity_pool", "id": POOL}, "credit"),
            "MATCH",
        ),
        # A contract-holder clawback names no account: foreign unless linked.
        (
            {
                "effect_kind": "clawback",
                "account": None,
                "holder": {"kind": "contract", "id": WALLET},
            },
            "MATCH",
        ),
    ],
    ids=[
        "balance-clawback-creator",
        "balance-clawback-claimant",
        "balance-clawback-foreign",
        "claim-by-investor",
        "claim-by-other",
        "contract-from-investor",
        "contract-foreign",
        "pool-investor",
        "pool-foreign",
        "contract-holder-clawback",
    ],
)
def test_typed_effects_bear_on_a_request_only_through_their_participants(
    redemption: Corpus, payload: dict[str, Any], expected: str
) -> None:
    base = redemption.inputs_for("RD-PAID")
    result = evaluate(typed(base, base, payload)).result
    assert result.result == expected
    burn = status_of(result, "burn_vs_request")
    assert burn == (
        ("UNKNOWN", "UNSUPPORTED_CAPABILITY") if expected == "UNKNOWN" else ("PASS", "EXACT_MATCH")
    )
    plain = evaluate(base).result
    for control in ("price_vs_approved", "units_within_position", "cash_vs_expected"):
        assert status_of(result, control) == status_of(plain, control), control


def test_an_execution_link_brings_a_foreign_typed_effect_into_scope(redemption: Corpus) -> None:
    base = redemption.inputs_for("RD-PAID")
    foreign = movement("contract_transfer", STRANGER, {"kind": "contract", "id": WALLET}, "debit")
    assert evaluate(typed(base, base, foreign)).result.result == "MATCH"
    linked = typed(base, base, foreign, operation_ref="RED-0001")
    assert evaluate(linked).result.result == "UNKNOWN"


def test_a_typed_effect_never_stands_in_for_the_burn(redemption: Corpus) -> None:
    """Without the linked burn, a clawback of a balance the investor created, a contract
    movement of the same amount or a claim never become the redemption burn."""
    base = redemption.inputs_for("RD-PAID")
    without_burn = dataclasses.replace(
        base,
        snapshot=base.snapshot.model_copy(
            update={"observation_ids": [i for i in base.snapshot.observation_ids if i != "obs-BN1"]}
        ),
    )
    for payload in (
        balance_clawback(INVESTOR, [OTHER]),
        movement("contract_transfer", INVESTOR, {"kind": "contract", "id": WALLET}, "debit"),
        balance_claim(INVESTOR, STRANGER),
    ):
        result = evaluate(typed(without_burn, base, payload)).result
        assert result.result == "UNKNOWN"
        assert status_of(result, "burn_vs_request")[0] == "UNKNOWN"


def test_a_credited_break_survives_a_relevant_typed_effect(redemption: Corpus) -> None:
    short = redemption.inputs_for("RD-SHORT-PAY")
    early_burn = redemption.inputs_for("RD-CANCELLED-EARLY-BURN")
    for payload in (
        balance_clawback(INVESTOR, [OTHER]),
        movement("pool_transfer", INVESTOR, {"kind": "liquidity_pool", "id": POOL}, "debit"),
    ):
        result = evaluate(typed(short, short, payload)).result
        assert result.result == "BREAK" and status_of(result, "cash_vs_expected")[0] == "FAIL"
        settled = evaluate(typed(early_burn, early_burn, payload)).result
        assert settled.result == "BREAK"
        assert status_of(settled, "no_settlement_after_cancellation") == (
            "FAIL",
            "SETTLED_DESPITE_CANCELLATION",
        )


def test_another_network_or_asset_is_never_this_requests_evidence(redemption: Corpus) -> None:
    base = redemption.inputs_for("RD-PAID")
    effect = balance_clawback(INVESTOR, [OTHER])
    assert evaluate(typed(base, base, effect, instrument_id="syn:fund:OTHER:z")).result.result == (
        "MATCH"
    )


def test_unresolved_participants_are_never_ruled_out(redemption: Corpus) -> None:
    """A balance whose history could not be read names an unresolved participant. Since
    redemption 0.6.0 such an effect cannot be shown foreign: it bears on the
    request (equal burn UNKNOWN). The retired 0.5.0 treated it as foreign (MATCH); its
    compatibility replay keeps that. The adapter also quarantines the record with unknown
    parties, which reaches every comparison that needs coverage."""
    base = redemption.inputs_for("RD-PAID")
    with_effect = typed(base, base, balance_clawback(None, None))
    payload = with_effect.observations["obs-typed"].payload
    assert isinstance(payload, ChainEffectPayload)
    assert ChainParty(kind="unresolved", reason="claimants of the claimable balance") in (
        effect_parties(payload)
    )
    result = evaluate(with_effect).result
    assert result.result == "UNKNOWN"
    assert status_of(result, "burn_vs_request") == ("UNKNOWN", "UNSUPPORTED_CAPABILITY")
    assert any("does not identify" in a for a in result.assumptions)
    old = replay(with_effect, "invaria-redemption-engine@0.5.0").result
    assert old.result == "MATCH"
    assert not any("does not identify" in a for a in old.assumptions)
    # A credited BREAK stays: the unresolved effect only stops what it could change.
    short = redemption.inputs_for("RD-SHORT-PAY")
    kept = evaluate(typed(short, short, balance_clawback(None, None))).result
    assert kept.result == "BREAK" and status_of(kept, "cash_vs_expected")[0] == "FAIL"
    # A resolved, demonstrably foreign balance changes nothing in either engine.
    foreign = typed(base, base, balance_clawback(STRANGER, [OTHER]))
    assert evaluate(foreign).result.result == "MATCH"


def test_subscription_never_rules_out_unresolved_participants(subscription: Corpus) -> None:
    """Subscription 0.4.0 applies the same rule: the equal delivery comparison is
    UNKNOWN, K3's BREAK stays. The retired 0.3.0 relied on the adapter's unknown-party
    quarantine, which a later Horizon-only certificate of the same source does not carry;
    alone, its compatibility replay treats the effect as foreign."""
    base = subscription.inputs_for("K3")
    claim = balance_claim(STRANGER, None)
    claim["claimable_balance"]["claimants"] = None
    with_effect = typed(base, base, claim)
    result = evaluate(with_effect).result
    assert status_of(result, "token_units_vs_order") == ("UNKNOWN", "UNSUPPORTED_CAPABILITY")
    assert result.result == "BREAK"
    old = replay(with_effect, "invaria-engine@0.3.0").result
    plain = replay(base, "invaria-engine@0.3.0").result
    assert [c.model_dump() for c in old.controls] == [c.model_dump() for c in plain.controls]


# ----------------------------------------------------------------- subscription

K3_INVESTOR = INVESTOR


def test_a_claim_by_the_investor_bears_on_the_delivery(subscription: Corpus) -> None:
    """A claim credits the investor from a claimable balance: it is not a delivery, and it
    could be the rest of one, so the equal comparison is UNKNOWN; K3's BREAK stays."""
    base = subscription.inputs_for("K3")
    result = evaluate(typed(base, base, balance_claim(K3_INVESTOR, STRANGER))).result
    plain = evaluate(base).result
    assert status_of(result, "token_units_vs_order") == ("UNKNOWN", "UNSUPPORTED_CAPABILITY")
    assert result.result == plain.result == "BREAK"


def test_a_balance_clawback_never_bears_on_the_delivery(subscription: Corpus) -> None:
    """The clawback roles hold for a typed holder: a clawback, whoever created the balance, is never
    relevant to a delivery under 0.3.0."""
    base = subscription.inputs_for("K3")
    result = evaluate(typed(base, base, balance_clawback(K3_INVESTOR, [K3_INVESTOR]))).result
    plain = evaluate(base).result
    assert [c.model_dump() for c in result.controls] == [c.model_dump() for c in plain.controls]


def test_foreign_typed_effects_change_no_delivery(subscription: Corpus) -> None:
    base = subscription.inputs_for("K3")
    plain = evaluate(base).result
    for payload in (
        movement("contract_transfer", STRANGER, {"kind": "contract", "id": WALLET}, "credit"),
        movement("pool_transfer", OTHER, {"kind": "liquidity_pool", "id": POOL}, "debit"),
        balance_claim(OTHER, STRANGER),
    ):
        result = evaluate(typed(base, base, payload)).result
        assert [c.model_dump() for c in result.controls] == [c.model_dump() for c in plain.controls]


def test_parties_of_earlier_effects_are_unchanged() -> None:
    """For the effects of increments 1 and 2 the participant set is exactly the one the
    engines used before increment 3 (account, counterparty, path ends, DEX submitter)."""
    payload = ChainEffectPayload.model_validate(
        {
            "payload_type": "chain_effect",
            "effect_kind": "path_payment_leg",
            "representation": "sac",
            "account": INVESTOR,
            "direction": "debit",
            "counterparty": {"kind": "liquidity_pool", "id": POOL},
            "units": {"atoms": "1", "scale": 7, "unit": "X"},
            "chain": {
                "network": "stellar:testnet",
                "ledger": 1,
                "tx_hash": "ab" * 32,
                "operation_index": 0,
                "tx_successful": True,
            },
            "path_payment": None,
            "transaction": None,
        }
    )
    assert {p.id for p in effect_parties(payload)} == {INVESTOR, POOL}
    assert chain_effects_bearing([], "s", "i", "op", None) == ()


# --------------------------------------------- corrected defect


def classic_path_debit(account: str) -> dict[str, Any]:
    """A Classic path payment debit, whose context carries a list (its path)."""
    return {
        "effect_kind": "path_payment_debit",
        "representation": "classic",
        "account": account,
        "counterparty": {"kind": "account", "id": OTHER},
        "path_payment": {
            "operation_type": "path_payment_strict_send",
            "source_account": account,
            "destination_account": OTHER,
            "source_asset": "native",
            "destination_asset": "native",
            "source_amount": "1",
            "destination_amount": "1",
            "path": [],
        },
        "transaction": {
            "fee_account": account,
            "fee_charged": {"atoms": "100", "scale": 7, "unit": "XLM"},
            "result_xdr": None,
        },
    }


@pytest.mark.parametrize(("account", "relevant"), [(STRANGER, False), (INVESTOR, True)])
def test_a_classic_path_effect_is_judged_not_crashed(
    redemption: Corpus, subscription: Corpus, account: str, relevant: bool
) -> None:
    """Until increment 3 a Classic path effect of the profile's instrument made the
    subscription engine raise TypeError (its path is a list, unhashable as a record key)
    and the redemption engine answer EVALUATION_ERROR even for a foreign effect. Now both
    apply their declared rule."""
    k3 = subscription.inputs_for("K3")
    sub = evaluate(typed(k3, k3, classic_path_debit(account))).result
    token = status_of(sub, "token_units_vs_order")
    plain_token = status_of(evaluate(k3).result, "token_units_vs_order")
    assert token == (("UNKNOWN", "UNSUPPORTED_CAPABILITY") if relevant else plain_token)
    paid = redemption.inputs_for("RD-PAID")
    red = evaluate(typed(paid, paid, classic_path_debit(account))).result
    assert red.result == ("UNKNOWN" if relevant else "MATCH")
    assert all(c.reason_code != "EVALUATION_ERROR" for c in (*sub.controls, *red.controls))


@pytest.mark.parametrize(
    "engine_ref",
    [
        "invaria-engine@0.1.0",
        "invaria-engine@0.2.0",
        "invaria-engine@0.3.0",
        "invaria-engine@0.4.0",
        "invaria-engine@0.5.0",
        "invaria-redemption-engine@0.4.0",
        "invaria-redemption-engine@0.5.0",
        "invaria-redemption-engine@0.6.0",
    ],
)
def test_the_corrected_defect_applies_to_the_compatibility_implementations(
    redemption: Corpus, subscription: Corpus, engine_ref: str
) -> None:
    """``record_content`` is shared code: the correction holds in the current engines and in
    every compatibility implementation that reads chain effects (subscription 0.2.0 to 0.5.0,
    redemption 0.5.0 and 0.6.0; 0.4.0, 0.5.0 and redemption 0.6.0 were written with the
    correction already in place, so for them it is no difference). Observable difference,
    declared when the defect was corrected: the historical 0.2.0
    and 0.3.0 code raised TypeError on such an input and recorded no conclusion, and the
    historical redemption 0.5.0 code answered EVALUATION_ERROR; their compatibility
    implementations now judge it. No recorded artifact holds such an input. Subscription
    0.1.0 and redemption 0.4.0 do not read chain effects, so the input never reached the
    defect there."""
    inputs = (
        typed(base := subscription.inputs_for("K3"), base, classic_path_debit(STRANGER))
        if engine_ref.startswith("invaria-engine")
        else typed(base := redemption.inputs_for("RD-PAID"), base, classic_path_debit(STRANGER))
    )
    result = replay(inputs, engine_ref).result
    assert all(c.reason_code != "EVALUATION_ERROR" for c in result.controls)
    assert result.result == evaluate(base).result.result


def test_a_path_payments_ends_are_participants_even_when_the_account_is_not_one(
    subscription: Corpus,
) -> None:
    """The adapter always writes a Classic path effect with one end as its account and the
    other as its counterparty, but the contract does not require it: a valid observation
    from another producer may name an account that is neither end. Its ends still make it
    bear on an operation of the destination. Removing the ends from ``effect_parties`` was
    reported as an equivalent mutation in increment 3; it is equivalent only for the
    adapter's output, and this test kills it."""
    payload = ChainEffectPayload.model_validate(
        {
            "payload_type": "chain_effect",
            "effect_kind": "path_payment_debit",
            "representation": "classic",
            "account": OTHER,
            "direction": "debit",
            "counterparty": {"kind": "account", "id": STRANGER},
            "units": {"atoms": "1", "scale": 7, "unit": "X"},
            "chain": {
                "network": "stellar:testnet",
                "ledger": 1,
                "tx_hash": "ab" * 32,
                "operation_index": 0,
                "tx_successful": True,
            },
            "path_payment": {
                "operation_type": "path_payment_strict_send",
                "source_account": STRANGER,
                "destination_account": INVESTOR,
                "source_asset": "native",
                "destination_asset": "native",
                "source_amount": "1",
                "destination_amount": "1",
                "path": [],
            },
            "transaction": {
                "fee_account": STRANGER,
                "fee_charged": {"atoms": "100", "scale": 7, "unit": "XLM"},
                "result_xdr": None,
            },
        }
    )
    assert {p.id for p in effect_parties(payload)} == {OTHER, STRANGER, INVESTOR}
    k3 = subscription.inputs_for("K3")
    inputs = typed(
        k3,
        k3,
        {
            k: v
            for k, v in payload.model_dump(mode="json").items()
            if k not in ("payload_type", "units", "chain")
        },
        label="ends",
    )
    effect = inputs.observations["obs-ends"]
    source, instrument = effect.source.source_id, effect.instrument_id
    assert chain_effects_bearing([effect], source, instrument, "op", {INVESTOR}) == ("obs-ends",)
    assert chain_effects_bearing([effect], source, instrument, "op", {"G" + "A" * 55}) == ()
