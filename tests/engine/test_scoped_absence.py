"""An on-chain certificate without a declared, sufficient chain scope never shows an
absence by itself.

Absent, insufficient and sufficient scopes in both engines, the conclusions the rule must not
erase (an excess, proven settlement, a BREAK of another control), the real testnet
certificates, and the profile gating that keeps historical profiles' promise. Certificates
with a chain scope are DERIVED from the corpus ones here (re-validated by the contract);
nothing on disk changes.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from invaria.contracts.base import parse_contract
from invaria.contracts.coverage import (
    ROUTE_TABLE_STORE_FORMATS,
    CoverageCertificate,
    route_unseen,
    scope_coherence_problem,
)
from invaria.contracts.evaluation import ControlResult, EvaluationResult
from invaria.contracts.observation import TokenMovementPayload
from invaria.contracts.profile import OperationProfile, parse_profile
from invaria.contracts.stellar import encode_muxed_account
from invaria.corpus_loader import load_corpus
from invaria.engine.common import EvaluationInputs, scope_shortfall, strict_absence_scope
from invaria.engine.evaluate import evaluate, replay
from invaria.stellar.store import STORE_FORMAT
from invaria.vertical_testnet import OnchainRun, run_testnet_vertical

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CORPORA = FIXTURES / "corpus"
ISSUER = "GDMYHWUG6BLHEGQSTMCAGUFUZFRZ6XGXJSHDKMDUZ6IRCKJ4FGZCCZVZ"  # synthetic DEMOA issuer
INVESTOR = "GA2JRO6WKTHMJTH7TDAXHNDHWT5SF72VKABPO2G6RL5BCGKTNIPIBEKW"  # approved, link-0001
OTHER = "GC6XNJNZOULFUZJBYORCTVNWI6UX5AVOEDC7EXDQM77BPFMQA3VWOYLK"  # a valid, unapproved G
TOKEN = "subscription.token_units_vs_order"
CASH = "subscription.cash_vs_order"
SETTLE = "redemption.no_settlement_after_cancellation"
DEADLINE = "redemption.payment_deadline"
HOLDER_UNSEEN = ["claimable_balance_clawback", "claimable_balance_claim"]  # adapter, holder


def scope(**change: Any) -> dict[str, Any]:
    """A chain scope like the adapter's SAC route for the investor, unless changed."""
    return {
        "network": "stellar:testnet",
        "target_id": "derived-target",
        "account": INVESTOR,
        "asset_code": "DEMOA",
        "asset_issuer": ISSUER,
        "route": "rpc_sac_events",
        "links_sha256": "0" * 64,
        "not_covered": HOLDER_UNSEEN,
    } | change


def with_scope(
    inputs: EvaluationInputs,
    prefix: str,
    chain_scope: dict[str, Any] | None,
    *,
    end: str | None = None,
) -> EvaluationInputs:
    """Every chain certificate (``prefix``) of the snapshot with ``chain_scope`` (None:
    without one) and, if given, another interval end, re-validated by the contract."""
    coverage = dict(inputs.coverage)
    for cid, certificate in inputs.coverage.items():
        if not cid.startswith(prefix):
            continue
        document = certificate.model_dump(mode="json")
        if end is not None:
            document["interval"]["end"] = end
        document.pop("chain_scope", None)
        if chain_scope is None:
            document.pop("supersedes", None)  # only a certificate with a scope supersedes
        if chain_scope is not None:
            document["chain_scope"] = {k: v for k, v in chain_scope.items() if v is not None}
        coverage[cid] = parse_contract(CoverageCertificate, json.dumps(document))
    return dataclasses.replace(inputs, coverage=coverage)


def control(
    inputs: EvaluationInputs, control_id: str, engine_ref: str | None = None
) -> tuple[EvaluationResult, ControlResult]:
    result = (replay(inputs, engine_ref) if engine_ref else evaluate(inputs)).result
    (found,) = [c for c in result.controls if c.control_id == control_id]
    return result, found


# ------------------------------------------------------------------ subscription


@pytest.fixture(scope="module")
def sub11() -> Any:
    return load_corpus(CORPORA / "subscription-synthetic-1.1.0")


def test_subscription_absent_scope_leaves_an_equal_comparison_unknown(sub11: Any) -> None:
    result, token = control(sub11.inputs_for("K2"), TOKEN)
    assert (result.result, token.status, token.reason_code) == (
        "UNKNOWN",
        "UNKNOWN",
        "INSUFFICIENT_COVERAGE",
    )
    assert "cov-chain-k2 declares no chain scope" in token.reason
    assert "cannot show that no further delivery reached the account" in token.reason
    assert token.delta is None


@pytest.mark.parametrize("not_covered", [[], HOLDER_UNSEEN], ids=["nothing-unseen", "holder-route"])
def test_subscription_sufficient_scope_concludes(sub11: Any, not_covered: list[str]) -> None:
    """Neither a claimable balance clawback nor a third party's claim of the account's balance
    can add a delivery, so the holder route's declared exclusions do not stop it."""
    inputs = with_scope(sub11.inputs_for("K2"), "cov-chain", scope(not_covered=not_covered))
    result, token = control(inputs, TOKEN)
    assert (result.result, token.status, token.reason_code) == ("MATCH", "PASS", "EXACT_MATCH")


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"not_covered": None}, "does not declare what it leaves out of sight"),
        ({"route": "horizon_payments"}, "reads only horizon_payments"),
        ({"asset_issuer": OTHER}, "not a representation of the instrument"),
        ({"network": "stellar:pubnet"}, "not a representation of the instrument"),
        ({"asset_code": "DEMOB"}, "not a representation of the instrument"),
        ({"account": OTHER}, f"no usable certificate reads {INVESTOR}"),
        ({"account": ISSUER}, f"no usable certificate reads {INVESTOR}"),  # mints only
    ],
    ids=[
        "exclusions-undeclared",
        "horizon-only",
        "issuer",
        "network",
        "asset",
        "other-account",
        "issuer-acct",
    ],
)
def test_subscription_insufficient_scope_is_unknown_with_its_reason(
    sub11: Any, change: dict[str, Any], why: str
) -> None:
    inputs = with_scope(sub11.inputs_for("K2"), "cov-chain", scope(**change))
    result, token = control(inputs, TOKEN)
    assert (result.result, token.status, token.reason_code) == (
        "UNKNOWN",
        "UNKNOWN",
        "INSUFFICIENT_COVERAGE",
    )
    assert why in token.reason


def test_subscription_keeps_the_break_of_another_control(sub11: Any) -> None:
    """K3: the cash shortfall does not depend on the chain; only the token PASS goes."""
    result, cash = control(sub11.inputs_for("K3"), CASH)
    then, cash_then = control(
        load_corpus(CORPORA / "subscription-synthetic").inputs_for("K3"), CASH
    )
    assert result.result == then.result == "BREAK"
    assert cash.model_dump() == cash_then.model_dump()
    assert cash.status == "FAIL" and cash.delta is not None


@pytest.fixture(scope="module")
def testnet() -> dict[str, OnchainRun]:
    runs = run_testnet_vertical(
        CORPORA / "subscription-testnet-1.5.0",
        FIXTURES / "stellar",
        CORPORA / "subscription-synthetic/mappings",
    )
    return {r.scenario.scenario_id: r for r in runs}


def test_real_adapter_certificates_have_a_sufficient_scope(
    testnet: dict[str, OnchainRun],
) -> None:
    """The real TN-LINKED delivery under 1.5.0: the adapter's certificates declare their
    scope, so the MATCH stands; the same evidence with the scope stripped does not."""
    run = testnet["TN-LINKED"]
    result, token = control(run.inputs, TOKEN)
    assert (result.result, token.status) == ("MATCH", "PASS")
    chain = [c for c in run.inputs.coverage.values() if c.source_id == "stellar-testnet"]
    assert chain and all(c.chain_scope is not None for c in chain)
    stripped = with_scope(run.inputs, "cov-testnet", None)
    result, token = control(stripped, TOKEN)
    assert (result.result, token.status, token.reason_code) == (
        "UNKNOWN",
        "UNKNOWN",
        "INSUFFICIENT_COVERAGE",
    )


def test_an_excess_stays_a_fail_whatever_the_scope(testnet: dict[str, OnchainRun]) -> None:
    """TN-OVER-LINKED: further deliveries could only add to the excess."""
    run = testnet["TN-OVER-LINKED"]
    _, scoped = control(run.inputs, TOKEN)
    _, stripped = control(with_scope(run.inputs, "cov-testnet", None), TOKEN)
    for token in (scoped, stripped):
        assert (token.status, token.reason_code) == ("FAIL", "UNITS_MISMATCH")
        assert token.delta is not None and token.delta.atoms == "10000000000"
    assert "cannot show the absence of further deliveries" in stripped.reason
    assert "could only add to this excess" in stripped.reason
    assert "could only add to this excess" not in scoped.reason


def test_historical_subscription_profiles_keep_their_promise() -> None:
    """Under 1.0.0 (no declaration) the current engine still lets the scopeless certificate
    show the absence: the declared limit of the earlier profiles."""
    result, token = control(load_corpus(CORPORA / "subscription-synthetic").inputs_for("K2"), TOKEN)
    assert (result.result, token.status) == ("MATCH", "PASS")
    assert result.versions.engine_ref == "invaria-engine@0.10.0"


def test_a_retired_subscription_engine_refuses_the_declaring_profile(sub11: Any) -> None:
    _, token = control(sub11.inputs_for("K2"), TOKEN, "invaria-engine@0.8.0")
    assert (token.status, token.reason_code) == ("UNKNOWN", "EVALUATION_ERROR")
    assert "sufficient chain scope" in token.reason


# ------------------------------------------------------------------ redemption


@pytest.fixture(scope="module")
def red16() -> Any:
    return load_corpus(CORPORA / "redemption-synthetic-1.6.0")


def test_redemption_absent_scope_leaves_the_absence_of_settlement_unknown(red16: Any) -> None:
    result, settle = control(red16.inputs_for("RD-CANCELLED"), SETTLE)
    assert (result.result, settle.status, settle.reason_code) == (
        "UNKNOWN",
        "UNKNOWN",
        "INSUFFICIENT_COVERAGE",
    )
    assert "cov-chain-s2 declares no chain scope" in settle.reason
    assert result.operation_state == "cancelled"


@pytest.mark.parametrize(
    "chain_scope",
    [scope(not_covered=[])],
    ids=["investor-sees-all"],
)
def test_redemption_sufficient_scope_shows_the_absence(
    red16: Any, chain_scope: dict[str, Any]
) -> None:
    inputs = with_scope(red16.inputs_for("RD-CANCELLED"), "cov-chain", chain_scope)
    result, settle = control(inputs, SETTLE)
    assert (result.result, settle.status, settle.reason_code) == ("MATCH", "PASS", "EXACT_MATCH")


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"not_covered": None}, "does not declare what it leaves out of sight"),
        ({}, "declares out of sight: claimable_balance_claim, claimable_balance_clawback"),
        ({"not_covered": ["claimable_balance_claim"]}, "declares out of sight"),
        ({"not_covered": [], "account": OTHER}, f"no usable certificate reads {INVESTOR}"),
        ({"not_covered": [], "asset_issuer": OTHER}, "not a representation of the instrument"),
        ({"not_covered": [], "route": "horizon_payments"}, "reads only horizon_payments"),
        # The issuer receives every burn, but does not see what only the holder's routes see.
        ({"not_covered": [], "account": ISSUER}, f"no usable certificate reads {INVESTOR}"),
    ],
    ids=[
        "exclusions-undeclared",
        "holder-route",
        "claim-unseen",
        "other-account",
        "issuer",
        "horizon-only",
        "issuer-account",
    ],
)
def test_redemption_insufficient_scope_is_unknown_with_its_reason(
    red16: Any, change: dict[str, Any], why: str
) -> None:
    inputs = with_scope(red16.inputs_for("RD-CANCELLED"), "cov-chain", scope(**change))
    result, settle = control(inputs, SETTLE)
    assert (result.result, settle.status, settle.reason_code) == (
        "UNKNOWN",
        "UNKNOWN",
        "INSUFFICIENT_COVERAGE",
    )
    assert why in settle.reason


@pytest.mark.parametrize("scenario_id", ["RD-CANCELLED-BURNED", "RD-CANCELLED-PAID"])
def test_proven_settlement_stays_a_break(red16: Any, scenario_id: str) -> None:
    result, settle = control(red16.inputs_for(scenario_id), SETTLE)
    assert (result.result, settle.status, settle.reason_code) == (
        "BREAK",
        "FAIL",
        "SETTLED_DESPITE_CANCELLATION",
    )


def test_a_missed_payment_keeps_its_break(red16: Any) -> None:
    result, deadline = control(red16.inputs_for("RD-CANCELLED-AFTER-DUE"), DEADLINE)
    _, settle = control(red16.inputs_for("RD-CANCELLED-AFTER-DUE"), SETTLE)
    assert (result.result, deadline.status, deadline.reason_code) == (
        "BREAK",
        "FAIL",
        "PAYMENT_MISSED",
    )
    assert (settle.status, settle.reason_code) == ("UNKNOWN", "INSUFFICIENT_COVERAGE")


def test_historical_redemption_profiles_keep_their_promise() -> None:
    corpus = load_corpus(CORPORA / "redemption-synthetic-1.5.0")
    result, settle = control(corpus.inputs_for("RD-CANCELLED"), SETTLE)
    assert (result.result, settle.status) == ("MATCH", "PASS")
    assert result.versions.engine_ref == "invaria-redemption-engine@0.11.0"


def test_a_retired_redemption_engine_refuses_the_declaring_profile(red16: Any) -> None:
    _, settle = control(red16.inputs_for("RD-CANCELLED"), SETTLE, "invaria-redemption-engine@0.9.0")
    assert (settle.status, settle.reason_code) == ("UNKNOWN", "EVALUATION_ERROR")
    assert "sufficient chain scope" in settle.reason


def test_the_rule_is_stated_only_under_a_declaring_profile(red16: Any, sub11: Any) -> None:
    declaring = [
        evaluate(red16.inputs_for("RD-PAID")).result.assumptions,
        evaluate(sub11.inputs_for("K2")).result.assumptions,
    ]
    assert all(
        any("needs a sufficient chain scope" in a for a in assumptions) for assumptions in declaring
    )
    historical = evaluate(load_corpus(CORPORA / "subscription-synthetic").inputs_for("K2"))
    assert not any("needs a sufficient chain scope" in a for a in historical.result.assumptions)


# ------------------------------------------------------------------ the new corpora


def _same_bytes(new: Path, old: Path, sub: str) -> None:
    assert sorted(p.name for p in (new / sub).iterdir()) == sorted(
        p.name for p in (old / sub).iterdir()
    )
    for f in (old / sub).iterdir():
        assert (new / sub / f.name).read_bytes() == f.read_bytes()


def _changed_expectations(new: Any, old: Any) -> dict[str, tuple[str, list[str]]]:
    """Per scenario whose expectation changed: the new result and the controls changed."""
    changed = {}
    for sid, scenario in new.scenarios.items():
        before, after = old.scenarios[sid].expected, scenario.expected
        if before == after:
            continue
        controls = [
            a.control_id for a, b in zip(after.controls, before.controls, strict=True) if a != b
        ]
        changed[sid] = (after.result, controls)
    return changed


def _reproduces(corpus: Any) -> None:
    for sid, scenario in corpus.scenarios.items():
        result = evaluate(corpus.inputs_for(sid)).result
        got = [(c.control_id, c.status, c.reason_code) for c in result.controls]
        want = [(c.control_id, c.status, c.reason_code) for c in scenario.expected.controls]
        assert (result.result, got) == (scenario.expected.result, want), sid


def test_subscription_1_1_0_is_1_0_0_with_the_rule_and_the_table(sub11: Any) -> None:
    old_dir, new_dir = CORPORA / "subscription-synthetic", CORPORA / "subscription-synthetic-1.1.0"
    old = load_corpus(old_dir)
    for sub in ("raw", "mappings"):
        _same_bytes(new_dir, old_dir, sub)
    assert sub11.coverage == old.coverage and sub11.journals == old.journals
    assert sub11.identity_links == old.identity_links
    before, after = old.profile.model_dump(mode="json"), sub11.profile.model_dump(mode="json")
    assert {k for k in before if before[k] != after[k]} == {
        "profile_ref",
        "rules_ref",
        "disclaimer",
        "coverage_requirements",
    }
    assert [
        r["source_id"] for r in after["coverage_requirements"] if r.get("absence_needs_chain_scope")
    ] == ["stellar-testnet-frozen"]
    # Expectations of the synthetic subscription, profile 1.1.0, written before running
    token = [TOKEN]
    assert _changed_expectations(sub11, old) == {
        "K1": ("UNKNOWN", token),
        "K2": ("UNKNOWN", token),
        "K3": ("BREAK", token),
        "K2-historical": ("UNKNOWN", token),
        "RETRACTION": ("UNKNOWN", token),
        "DUPLICATE-DELIVERY": ("UNKNOWN", token),
        "SOURCE-CONFLICT": ("UNKNOWN", token),
        "AMBIGUOUS-SCALE": ("UNKNOWN", token),
    }
    _reproduces(sub11)


def test_redemption_1_6_0_is_1_5_0_with_the_rule_and_the_table(red16: Any) -> None:
    old_dir = CORPORA / "redemption-synthetic-1.5.0"
    old = load_corpus(old_dir)
    _same_bytes(CORPORA / "redemption-synthetic-1.6.0", old_dir, "raw")
    assert red16.coverage == old.coverage and red16.journals == old.journals
    before, after = old.profile.model_dump(mode="json"), red16.profile.model_dump(mode="json")
    assert {k for k in before if before[k] != after[k]} == {
        "profile_ref",
        "rules_ref",
        "disclaimer",
        "coverage_requirements",
    }
    # Expectations of the synthetic redemption, profile 1.6.0, written before running
    assert _changed_expectations(red16, old) == {
        "RD-CANCELLED": ("UNKNOWN", [SETTLE]),
        "RD-CANCELLED-AFTER-DUE": ("BREAK", [SETTLE]),
        "RD-CANCEL-KNOWN-LATE-AFTER": ("UNKNOWN", [SETTLE]),
        "RD-CANCEL-RETRACTED-BEFORE": ("UNKNOWN", [SETTLE]),
    }
    _reproduces(red16)


def test_testnet_1_5_0_is_1_4_0_with_the_rule_and_the_same_expectations(
    testnet: dict[str, OnchainRun],
) -> None:
    old_dir, new_dir = (
        CORPORA / "subscription-testnet-1.4.0",
        CORPORA / "subscription-testnet-1.5.0",
    )
    _same_bytes(new_dir, old_dir, "raw")
    assert (new_dir / "identity_links.json").read_bytes() == (
        old_dir / "identity_links.json"
    ).read_bytes()
    old = json.loads((old_dir / "scenarios.json").read_text("utf-8"))
    new = json.loads((new_dir / "scenarios.json").read_text("utf-8"))
    assert {k for k in old if old[k] != new[k]} == {"corpus_id", "profile_ref", "notice"}
    for run in testnet.values():
        result = run.evaluation.result
        expected = run.scenario.expected
        assert result.result == expected.result, run.scenario.scenario_id
        assert result.versions.profile_ref == "fund-subscription-testnet@1.5.0"


@pytest.mark.parametrize(
    ("addresses", "why"),
    [
        (None, "the operation's account is not known"),
        (set(), "no address is approved for the operation's account"),
        ({OTHER}, f"no usable certificate reads {OTHER}"),
        ({INVESTOR, OTHER}, f"no usable certificate reads {OTHER}"),
    ],
    ids=["account-unknown", "no-approved-address", "other", "one-of-two"],
)
def test_without_every_approved_address_no_scope_suffices(
    red16: Any, addresses: set[str] | None, why: str
) -> None:
    """Even a scope that sees everything for the investor cannot show an absence for an
    account whose addresses are not known, or that has another approved address it does
    not read."""
    certificate = with_scope(
        red16.inputs_for("RD-CANCELLED"), "cov-chain", scope(not_covered=[])
    ).coverage["cov-chain-s2"]
    shortfall = scope_shortfall([certificate], red16.profile.representations, addresses, ())
    assert shortfall is not None and why in shortfall


def test_a_muxed_approved_address_is_read_through_its_base_account(red16: Any) -> None:
    certificate = with_scope(
        red16.inputs_for("RD-CANCELLED"), "cov-chain", scope(not_covered=[])
    ).coverage["cov-chain-s2"]
    muxed = encode_muxed_account(INVESTOR, 0)
    assert scope_shortfall([certificate], red16.profile.representations, {muxed}, ()) is None


def _unscoped_copy(certificate: CoverageCertificate, cid: str) -> CoverageCertificate:
    """A copy of ``certificate`` under another id, at the same instant, without a scope."""
    document = certificate.model_dump(mode="json")
    document.pop("chain_scope", None)
    document.pop("supersedes", None)
    document["coverage_id"] = cid
    return parse_contract(CoverageCertificate, json.dumps(document))


def test_redemption_cites_a_certificate_that_can_show_the_absence(red16: Any) -> None:
    """Two active certificates meet the coverage: a later one without a scope and a
    sufficient one. The absence is shown, and only by the sufficient one (review M1)."""
    inputs = with_scope(red16.inputs_for("RD-CANCELLED"), "cov-chain", scope(not_covered=[]))
    newer = _unscoped_copy(inputs.coverage["cov-chain-s2"], "cov-chain-s2-unscoped")
    # The most recent by (recorded_at, coverage_id), the order the engines use.
    assert (newer.recorded_at, newer.coverage_id) > (
        inputs.coverage["cov-chain-s2"].recorded_at,
        "cov-chain-s2",
    )
    snapshot = inputs.snapshot.model_copy(
        update={"coverage_ids": sorted([*inputs.snapshot.coverage_ids, newer.coverage_id])}
    )
    both = dataclasses.replace(
        inputs, snapshot=snapshot, coverage={**inputs.coverage, newer.coverage_id: newer}
    )
    result, settle = control(both, SETTLE)
    assert (result.result, settle.status) == ("MATCH", "PASS")
    assert "cov-chain-s2" in settle.evidence_refs
    assert "cov-chain-s2-unscoped" not in settle.evidence_refs


def _chain(run: OnchainRun) -> dict[str, CoverageCertificate]:
    return {k: c for k, c in run.inputs.coverage.items() if c.source_id == "stellar-testnet"}


def test_a_horizon_only_certificate_cannot_show_the_absence(
    testnet: dict[str, OnchainRun],
) -> None:
    """Review A1: Horizon's payments do not list what only SAC events show (the account's own
    claim of a claimable balance, the fill of its own offer). Without the SAC certificate the
    real TN-LINKED delivery is not concluded."""
    run = testnet["TN-LINKED"]
    chain = _chain(run)
    sac = [k for k, c in chain.items() if c.chain_scope and c.chain_scope.route == "rpc_sac_events"]
    assert sac and len(chain) > len(sac)
    snapshot = run.inputs.snapshot.model_copy(
        update={"coverage_ids": [c for c in run.inputs.snapshot.coverage_ids if c not in sac]}
    )
    coverage = {k: c for k, c in run.inputs.coverage.items() if k not in sac}
    horizon_only = dataclasses.replace(run.inputs, snapshot=snapshot, coverage=coverage)
    result, token = control(horizon_only, TOKEN)
    assert (result.result, token.status, token.reason_code) == (
        "UNKNOWN",
        "UNKNOWN",
        "INSUFFICIENT_COVERAGE",
    )
    assert "reads only horizon_payments" in token.reason


def test_the_sac_certificate_counts_whatever_the_order(testnet: dict[str, OnchainRun]) -> None:
    """Review B4: under the declared policy every active certificate meeting the coverage
    is judged, so a later Horizon-only certificate does not hide the SAC one, which is the one
    cited."""
    run = testnet["TN-LINKED"]
    chain = _chain(run)
    (horizon,) = [
        k for k, c in chain.items() if c.chain_scope and c.chain_scope.route == "horizon_payments"
    ]
    (sac,) = [
        k for k, c in chain.items() if c.chain_scope and c.chain_scope.route == "rpc_sac_events"
    ]
    document = chain[horizon].model_dump(mode="json")
    later = max(c.recorded_at for c in chain.values()) + timedelta(seconds=1)
    document["recorded_at"] = later.isoformat().replace("+00:00", "Z")
    document.pop("supersedes", None)
    moved = parse_contract(CoverageCertificate, json.dumps(document))
    inputs = dataclasses.replace(run.inputs, coverage={**run.inputs.coverage, horizon: moved})
    result, token = control(inputs, TOKEN)
    assert (result.result, token.status) == ("MATCH", "PASS")
    assert sac in token.evidence_refs and horizon not in token.evidence_refs


def _short_delivery(inputs: EvaluationInputs) -> EvaluationInputs:
    """K2 with its single token delivery halved: a DERIVED short comparison."""
    observations = dict(inputs.observations)
    (oid,) = [
        k
        for k in inputs.snapshot.observation_ids
        if isinstance(observations[k].payload, TokenMovementPayload)
    ]
    payload = observations[oid].payload
    assert isinstance(payload, TokenMovementPayload)
    half = payload.units.model_copy(update={"atoms": str(int(payload.units.atoms) // 2)})
    observations[oid] = observations[oid].model_copy(
        update={"payload": payload.model_copy(update={"units": half})}
    )
    return dataclasses.replace(inputs, observations=observations)


def test_a_short_delivery_needs_the_scope_too(sub11: Any) -> None:
    """Short says that nothing else was delivered: UNKNOWN without a sufficient scope, a
    FAIL with one."""
    short = _short_delivery(sub11.inputs_for("K2"))
    _, unscoped = control(short, TOKEN)
    assert (unscoped.status, unscoped.reason_code) == ("UNKNOWN", "INSUFFICIENT_COVERAGE")
    _, scoped = control(with_scope(short, "cov-chain", scope()), TOKEN)
    assert (scoped.status, scoped.reason_code) == ("FAIL", "UNITS_MISMATCH")
    assert scoped.delta is not None and int(scoped.delta.atoms) < 0


def test_the_rule_binds_only_the_chain_authority_requirement() -> None:
    """Review B2: declared on a requirement the engines never read for an absence, it would
    gate them without being applied, so the profile refuses it."""
    document = json.loads((CORPORA / "subscription-synthetic-1.1.0/profile.json").read_text())
    for source in document["sources"]:
        if source["source_id"] == "stellar-testnet-frozen":
            source["kind"] = "transfer_agent"
    with pytest.raises(ValidationError, match="absence_needs_chain_scope belongs"):
        OperationProfile.model_validate_json(json.dumps(document))


# ------------------------------------------------- second part: M3, B1 and B3

BURN = "redemption.burn_vs_request"
AFTER_CUT = "2026-10-06T16:00:01Z"  # RD-PAID's valid_at is 16:00:00, an included instant
DEMOB = "rep-derived-demob"


@pytest.fixture(scope="module")
def red17() -> Any:
    return load_corpus(CORPORA / "redemption-synthetic-1.7.0")


@pytest.fixture(scope="module")
def sub12() -> Any:
    return load_corpus(CORPORA / "subscription-synthetic-1.2.0")


def _burn(inputs: EvaluationInputs, factor: tuple[int, int]) -> EvaluationInputs:
    """The single linked burn scaled by ``factor`` (numerator, denominator): DERIVED."""
    observations = dict(inputs.observations)
    ids = [
        k
        for k in inputs.snapshot.observation_ids
        if isinstance(observations[k].payload, TokenMovementPayload)
        and observations[k].operation_ref == inputs.snapshot.operation_ref
    ]
    (oid,) = ids
    payload = observations[oid].payload
    assert isinstance(payload, TokenMovementPayload)
    atoms = str(int(payload.units.atoms) * factor[0] // factor[1])
    observations[oid] = observations[oid].model_copy(
        update={
            "payload": payload.model_copy(
                update={"units": payload.units.model_copy(update={"atoms": atoms})}
            )
        }
    )
    return dataclasses.replace(inputs, observations=observations)


def test_m3_an_equal_burn_without_coverage_is_unknown(red17: Any) -> None:
    result, burn = control(red17.inputs_for("RD-PAID"), BURN)
    assert (result.result, burn.status, burn.reason_code) == (
        "UNKNOWN",
        "UNKNOWN",
        "INSUFFICIENT_COVERAGE",
    )
    assert "cannot be shown to be all the burns" in burn.reason
    assert burn.delta is None


def test_m3_an_equal_burn_with_coherent_coverage_passes_and_cites_it(red17: Any) -> None:
    """The adapter's holder SAC scope (both effects declared) covering acceptance to the
    included cut: the burns observed are complete, and the certificate is cited."""
    inputs = with_scope(red17.inputs_for("RD-PAID"), "cov-chain", scope(), end=AFTER_CUT)
    result, burn = control(inputs, BURN)
    assert (result.result, burn.status, burn.reason_code) == ("MATCH", "PASS", "EXACT_MATCH")
    assert "cov-chain-s2" in burn.evidence_refs


def test_m3_the_cut_instant_must_be_covered(red17: Any) -> None:
    """A fact counts while effective at the cut, so a certificate ending at valid_at
    (half-open) leaves that instant unseen."""
    inputs = with_scope(red17.inputs_for("RD-PAID"), "cov-chain", scope())
    _, burn = control(inputs, BURN)
    assert (burn.status, burn.reason_code) == ("UNKNOWN", "INSUFFICIENT_COVERAGE")
    assert "interval does not cover" in burn.reason


@pytest.mark.parametrize("scoped", [False, True], ids=["no-coverage", "coverage"])
def test_m3_a_short_burn_is_a_deficit_only_with_coverage(red17: Any, scoped: bool) -> None:
    inputs = _burn(red17.inputs_for("RD-PAID"), (1, 2))
    if scoped:
        inputs = with_scope(inputs, "cov-chain", scope(), end=AFTER_CUT)
    _, burn = control(inputs, BURN)
    if scoped:
        assert (burn.status, burn.reason_code) == ("FAIL", "UNITS_MISMATCH")
        assert burn.delta is not None and int(burn.delta.atoms) < 0
    else:
        assert (burn.status, burn.reason_code) == ("UNKNOWN", "INSUFFICIENT_COVERAGE")


@pytest.mark.parametrize("scoped", [False, True], ids=["no-coverage", "coverage"])
def test_m3_an_excess_burn_stays_a_fail(red17: Any, scoped: bool) -> None:
    inputs = _burn(red17.inputs_for("RD-PAID"), (2, 1))
    if scoped:
        inputs = with_scope(inputs, "cov-chain", scope(), end=AFTER_CUT)
    result, burn = control(inputs, BURN)
    assert (result.result, burn.status, burn.reason_code) == ("BREAK", "FAIL", "UNITS_MISMATCH")
    assert burn.delta is not None and int(burn.delta.atoms) > 0


def test_m3_proven_settlement_despite_cancellation_needs_no_completeness(red17: Any) -> None:
    result, settle = control(red17.inputs_for("RD-CANCELLED-BURNED"), SETTLE)
    assert (result.result, settle.status, settle.reason_code) == (
        "BREAK",
        "FAIL",
        "SETTLED_DESPITE_CANCELLATION",
    )


def test_m3_binds_only_the_declaring_profile(red16: Any, red17: Any) -> None:
    _, then = control(red16.inputs_for("RD-PAID"), BURN)
    assert (then.status, then.reason_code) == ("PASS", "EXACT_MATCH")  # 1.6.0's promise
    _, refused = control(red17.inputs_for("RD-PAID"), BURN, "invaria-redemption-engine@0.10.0")
    assert (refused.status, refused.reason_code) == ("UNKNOWN", "EVALUATION_ERROR")
    assert "complete needs coverage" in refused.reason


def _two_representations(inputs: EvaluationInputs) -> EvaluationInputs:
    """The profile with a second representation of the instrument (DEMOB, another
    issuer): DERIVED, re-validated by the contract."""
    document = inputs.profile.model_dump(mode="json")
    first = document["representations"][0]
    document["representations"].append(
        {**first, "representation_id": DEMOB, "asset_code": "DEMOB", "issuer": OTHER}
    )
    profile = type(inputs.profile).model_validate_json(json.dumps(document))
    return dataclasses.replace(inputs, profile=profile)


def _two_certificates(
    inputs: EvaluationInputs, prefix: str, scopes: list[dict[str, Any]]
) -> EvaluationInputs:
    """The chain certificate replaced by one certificate per scope (ids suffixed)."""
    (cid,) = [k for k in inputs.snapshot.coverage_ids if k.startswith(prefix)]
    coverage = {k: c for k, c in inputs.coverage.items() if k != cid}
    ids = []
    for n, chain_scope in enumerate(scopes):
        document = inputs.coverage[cid].model_dump(mode="json")
        document.update(coverage_id=f"{cid}-{n}", chain_scope=chain_scope)
        coverage[f"{cid}-{n}"] = parse_contract(CoverageCertificate, json.dumps(document))
        ids.append(f"{cid}-{n}")
    others = [k for k in inputs.snapshot.coverage_ids if k != cid]
    snapshot = inputs.snapshot.model_copy(update={"coverage_ids": sorted([*others, *ids])})
    return dataclasses.replace(inputs, snapshot=snapshot, coverage=coverage)


@pytest.mark.parametrize(
    ("covered", "verdict", "why"),
    [
        (
            ["DEMOB"],
            "UNKNOWN",
            "no usable certificate reads representation rep-stellar-testnet-demoa",
        ),
        (["DEMOA"], "UNKNOWN", f"no usable certificate reads representation {DEMOB}"),
        (["DEMOA", "DEMOB"], "MATCH", None),
    ],
    ids=["only-the-other", "only-the-evaluated", "both"],
)
def test_b1_every_representation_needs_its_own_coverage(
    sub12: Any, covered: list[str], verdict: str, why: str | None
) -> None:
    """K2 counts a DEMOA delivery. A certificate of DEMOB never stands in for DEMOA, and
    affirming that no other delivery happened needs DEMOB's own coverage too."""
    issuers = {"DEMOA": ISSUER, "DEMOB": OTHER}
    inputs = _two_representations(sub12.inputs_for("K2"))
    scopes = [scope(asset_code=a, asset_issuer=issuers[a]) for a in covered]
    result, token = control(_two_certificates(inputs, "cov-chain", scopes), TOKEN)
    assert result.result == verdict
    if why is not None:
        assert (token.status, token.reason_code) == ("UNKNOWN", "INSUFFICIENT_COVERAGE")
        assert why in token.reason


@pytest.mark.parametrize(
    ("chain_scope", "verdict"),
    [
        (scope(), "MATCH"),  # holder, both effects declared: what the adapter writes
        (scope(not_covered=[]), "UNKNOWN"),  # holder claiming to see everything
        (scope(not_covered=["claimable_balance_claim"]), "UNKNOWN"),  # misses the clawback
    ],
    ids=["coherent", "empty", "partial"],
)
def test_b3_a_declaration_the_route_cannot_back_counts_for_nothing(
    sub12: Any, chain_scope: dict[str, Any], verdict: str
) -> None:
    result, token = control(with_scope(sub12.inputs_for("K2"), "cov-chain", chain_scope), TOKEN)
    assert result.result == verdict
    if verdict == "UNKNOWN":
        assert "never observe" in token.reason


def test_b3_under_1_6_0_an_empty_declaration_still_counts(red16: Any) -> None:
    """The earlier profile keeps its promise: there, a holder's [] showed the absence."""
    inputs = with_scope(red16.inputs_for("RD-CANCELLED"), "cov-chain", scope(not_covered=[]))
    assert control(inputs, SETTLE)[1].status == "PASS"


@pytest.mark.parametrize("account", [INVESTOR, ISSUER], ids=["holder", "issuer"])
@pytest.mark.parametrize("route", ["horizon_payments", "rpc_sac_events"])
def test_b3_with_coherent_certificates_the_absence_of_settlement_is_never_shown(
    red17: Any, account: str, route: str
) -> None:
    """Every role and route leaves at least a third party's claim of a claimable balance
    out of sight, so a coherent certificate never shows the absence of settlement: the
    limitation is not reachable, and nothing more is implemented for it."""
    probe = scope(account=account, route=route, not_covered=[])
    certificate = parse_contract(
        CoverageCertificate,
        json.dumps(
            {**red17.coverage["cov-chain-s2"].model_dump(mode="json"), "chain_scope": probe}
        ),
    )
    assert certificate.chain_scope is not None
    unseen = route_unseen(certificate.chain_scope)
    assert "claimable_balance_claim" in unseen
    coherent = scope(account=account, route=route, not_covered=sorted(unseen))
    inputs = with_scope(red17.inputs_for("RD-CANCELLED"), "cov-chain", coherent, end=AFTER_CUT)
    _, settle = control(inputs, SETTLE)
    assert (settle.status, settle.reason_code) == ("UNKNOWN", "INSUFFICIENT_COVERAGE")


def test_b3_the_adapter_certificates_are_coherent_and_the_table_is_current() -> None:
    """The real certificates the adapter writes match the table, and the table names the
    store format the adapter writes (a new format must review it)."""
    runs = run_testnet_vertical(
        CORPORA / "subscription-testnet-1.6.0",
        FIXTURES / "stellar",
        CORPORA / "subscription-synthetic/mappings",
    )
    chain = {
        c.coverage_id: c
        for r in runs
        for c in r.inputs.coverage.values()
        if c.chain_scope is not None
    }
    assert chain
    assert all(
        scope_coherence_problem(c.chain_scope) is None for c in chain.values() if c.chain_scope
    )
    assert STORE_FORMAT in ROUTE_TABLE_STORE_FORMATS
    assert {r.evaluation.result.result for r in runs} == {"MATCH", "UNKNOWN", "BREAK"}


def _same_profile_but(new: Any, old: Any, field: str) -> None:
    before, after = old.profile.model_dump(mode="json"), new.profile.model_dump(mode="json")
    assert {k for k in before if before[k] != after[k]} == {
        "profile_ref",
        "rules_ref",
        "disclaimer",
        "coverage_requirements",
    }
    assert [r["source_id"] for r in after["coverage_requirements"] if r.get(field)] == [
        r["source_id"] for r in after["coverage_requirements"] if r.get("absence_needs_chain_scope")
    ]


def test_subscription_1_2_0_is_1_1_0_with_completeness_and_its_expectations(sub12: Any) -> None:
    old_dir, new_dir = (
        CORPORA / "subscription-synthetic-1.1.0",
        CORPORA / "subscription-synthetic-1.2.0",
    )
    old = load_corpus(old_dir)
    for sub in ("raw", "mappings"):
        _same_bytes(new_dir, old_dir, sub)
    assert sub12.coverage == old.coverage and sub12.journals == old.journals
    _same_profile_but(sub12, old, "completeness_needs_coverage")
    assert _changed_expectations(sub12, old) == {}  # as registered before running
    _reproduces(sub12)


def test_redemption_1_7_0_is_1_6_0_with_m3_and_the_table(red17: Any, red16: Any) -> None:
    _same_bytes(
        CORPORA / "redemption-synthetic-1.7.0", CORPORA / "redemption-synthetic-1.6.0", "raw"
    )
    assert red17.coverage == red16.coverage and red17.journals == red16.journals
    _same_profile_but(red17, red16, "completeness_needs_coverage")
    changed = _changed_expectations(red17, red16)
    # Second part: every changed scenario changes only the burn
    assert len(changed) == 29 and {tuple(c) for _, c in changed.values()} == {(BURN,)}
    assert sorted(
        s for s, (result, _) in changed.items() if red16.scenarios[s].expected.result == "MATCH"
    ) == sorted(
        [
            "RD-PAID",
            "RD-PAID-AT-DUE",
            "RD-POSITION-RECONSTRUCTED",
            "RD-CANCEL-RETRACTED-AFTER",
            "RD-CORRECTION-KNOWN-LATE",
            "RD-DUPLICATE",
        ]
    )
    for sid, (result, _) in changed.items():
        before = red16.scenarios[sid].expected.result
        assert result == ("UNKNOWN" if before == "MATCH" else before), sid  # BREAKs kept
    _reproduces(red17)


def test_testnet_1_6_0_is_1_5_0_with_completeness_and_the_same_expectations() -> None:
    old_dir, new_dir = (
        CORPORA / "subscription-testnet-1.5.0",
        CORPORA / "subscription-testnet-1.6.0",
    )
    _same_bytes(new_dir, old_dir, "raw")
    old = json.loads((old_dir / "scenarios.json").read_text("utf-8"))
    new = json.loads((new_dir / "scenarios.json").read_text("utf-8"))
    assert {k for k in old if old[k] != new[k]} == {"corpus_id", "profile_ref", "notice"}
    runs = run_testnet_vertical(
        new_dir, FIXTURES / "stellar", CORPORA / "subscription-synthetic/mappings"
    )
    for run in runs:
        assert run.evaluation.result.result == run.scenario.expected.result
        assert run.evaluation.result.versions.profile_ref == "fund-subscription-testnet@1.6.0"


def test_b1_addresses_are_those_approved_on_the_representation_s_network(red17: Any) -> None:
    certificate = with_scope(red17.inputs_for("RD-PAID"), "cov-chain", scope()).coverage[
        "cov-chain-s2"
    ]
    reps = red17.profile.representations
    assert strict_absence_scope([certificate], reps, {"stellar:testnet": {INVESTOR}}, ())[0] is None
    shortfall, cited = strict_absence_scope([certificate], reps, {"stellar:pubnet": {INVESTOR}}, ())
    assert shortfall is not None and "no address on stellar:testnet is approved" in shortfall
    assert cited == ()


@pytest.mark.parametrize(
    ("covered", "status"),
    [(["DEMOA"], "UNKNOWN"), (["DEMOA", "DEMOB"], "PASS")],
    ids=["one-of-two", "both"],
)
def test_b1_burns_need_every_representation_covered(
    red17: Any, covered: list[str], status: str
) -> None:
    issuers = {"DEMOA": ISSUER, "DEMOB": OTHER}
    inputs = _two_representations(red17.inputs_for("RD-PAID"))
    scopes = [scope(asset_code=a, asset_issuer=issuers[a]) for a in covered]
    inputs = _two_certificates(inputs, "cov-chain", scopes)
    coverage = {
        k: parse_contract(
            CoverageCertificate,
            json.dumps(
                {
                    **c.model_dump(mode="json"),
                    "interval": {**c.model_dump(mode="json")["interval"], "end": AFTER_CUT},
                }
            ),
        )
        if k.startswith("cov-chain")
        else c
        for k, c in inputs.coverage.items()
    }
    _, burn = control(dataclasses.replace(inputs, coverage=coverage), BURN)
    assert burn.status == status


def test_completeness_needs_the_scope_declaration() -> None:
    document = json.loads((CORPORA / "redemption-synthetic-1.7.0/profile.json").read_text())
    for requirement in document["coverage_requirements"]:
        requirement.pop("absence_needs_chain_scope", None)
    with pytest.raises(ValidationError, match="completeness_needs_coverage needs"):
        parse_profile(json.dumps(document))


def test_the_completeness_rule_is_stated_only_under_its_profiles(
    red16: Any, red17: Any, sub11: Any, sub12: Any
) -> None:
    def states(corpus: Any, sid: str) -> bool:
        return any(
            "affirming the observed chain records complete" in a
            for a in evaluate(corpus.inputs_for(sid)).result.assumptions
        )

    assert states(red17, "RD-PAID") and states(sub12, "K2")
    assert not states(red16, "RD-PAID") and not states(sub11, "K2")


def test_b3_under_1_7_0_an_empty_declaration_no_longer_shows_the_absence(red17: Any) -> None:
    """The same holder [] that 1.6.0 accepted (above) is incoherent under 1.7.0."""
    inputs = with_scope(
        red17.inputs_for("RD-CANCELLED"), "cov-chain", scope(not_covered=[]), end=AFTER_CUT
    )
    _, settle = control(inputs, SETTLE)
    assert (settle.status, settle.reason_code) == ("UNKNOWN", "INSUFFICIENT_COVERAGE")
    assert "never observe" in settle.reason
