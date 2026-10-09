"""Structured diagnosis of each control's evidence requirements, without a database.

The expectations (rows S* and R*) were written as a table from the engine rules and
the corpus descriptions before the implementation, never copied from its output.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from invaria.cli import main
from invaria.contracts.evaluation import EvaluationResult
from invaria.contracts.observation import OrderPayload, TokenMovementPayload
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine import evaluate as subscription_engine
from invaria.engine.common import EvaluationInputs
from invaria.engine.evaluate import evaluate, replay
from invaria.engine.trace import control_plan
from invaria.engine.versions import (
    DIAGNOSTIC_ENGINES,
    REDEMPTION_ENGINE_REF,
    SUBSCRIPTION_ENGINE_0_9_0,
    SUBSCRIPTION_ENGINE_REF,
)
from invaria.query.diagnostics import determining_index, diagnose
from invaria.query.models import AccessProfile, ControlDiagnosisView
from invaria.query.service import QueryError, QueryService
from invaria.vertical_testnet import run_testnet_vertical

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SUBSCRIPTION = FIXTURES / "corpus/subscription-synthetic-1.2.0"
REDEMPTION = FIXTURES / "corpus/redemption-synthetic-1.7.0"
TESTNET = FIXTURES / "corpus/subscription-testnet-1.6.0"
TENANT = "tenant-synthetic-demo"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_: Any, **__: Any) -> Any:
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture(scope="module")
def subscription() -> Corpus:
    return load_corpus(SUBSCRIPTION)


@pytest.fixture(scope="module")
def redemption() -> Corpus:
    return load_corpus(REDEMPTION)


def diagnosis_of(inputs: EvaluationInputs, control: str) -> ControlDiagnosisView:
    stored = evaluate(inputs).result
    return diagnose(stored, inputs, replay(inputs, stored.versions.engine_ref), control)


def by_id(view: ControlDiagnosisView) -> dict[str, Any]:
    return {r.requirement_id: r for r in view.requirements}


def statuses(view: ControlDiagnosisView) -> dict[str, str]:
    return {r.requirement_id: r.status for r in view.requirements}


def decisive(view: ControlDiagnosisView) -> Any:
    (only,) = [r for r in view.requirements if r.determined_result]
    return only


# ------------------------------------------------------------------ the expectations table

# (corpus, scenario, control, decisive requirement, its status, category, next step kind,
#  other requirement statuses that must hold)
TABLE = [
    (
        "S1",
        "sub",
        "K2",
        "subscription.token_units_vs_order",
        "completeness",
        "undetermined",
        "insufficient_coverage",
        "obtain_coverage",
        {
            "admission": "satisfied",
            "fact:order_accepted": "satisfied",
            "fact:token_movement": "satisfied",
            "operands": "satisfied",
            "comparison": "satisfied",
            "quarantine": "not_applicable",
            "attribution": "not_applicable",
            "chain_effects": "not_evaluated",
        },
    ),
    (
        "S2",
        "sub",
        "K1",
        "subscription.cash_vs_order",
        "fact:cash_settled",
        "undetermined",
        "missing_evidence",
        "provide_observation",
        {
            "fact:order_accepted": "satisfied",
            "operands": "not_evaluated",
            "comparison": "not_evaluated",
        },
    ),
    (
        "S3",
        "sub",
        "K3",
        "subscription.cash_vs_order",
        "comparison",
        "contradicted",
        None,
        None,
        {"fact:cash_settled": "satisfied", "operands": "satisfied"},
    ),
    (
        "S4",
        "sub",
        "SOURCE-CONFLICT",
        "subscription.cash_vs_order",
        "fact:cash_settled",
        "undetermined",
        "source_conflict",
        "resolve_source_conflict",
        {"comparison": "not_evaluated"},
    ),
    (
        "S5",
        "sub",
        "RETRACTION",
        "subscription.cash_vs_order",
        "fact:cash_settled",
        "undetermined",
        "withdrawn_evidence",
        "provide_observation",
        {},
    ),
    (
        "S6",
        "sub",
        "AMBIGUOUS-IDENTITY",
        "subscription.token_units_vs_order",
        "fact:token_movement",
        "undetermined",
        "unattributed",
        "provide_approved_link",
        {"completeness": "not_evaluated"},
    ),
    (
        "S7",
        "sub",
        "AMBIGUOUS-SCALE",
        "subscription.ta_units_vs_order",
        "fact:units_registered",
        "undetermined",
        "quarantined_input",
        "resolve_quarantine",
        {},
    ),
    (
        "S8",
        "sub",
        "K1",
        "subscription.order_terms",
        "comparison",
        "satisfied",
        None,
        None,
        {"fact:order_accepted": "satisfied", "operands": "satisfied"},
    ),
    (
        "R1",
        "red",
        "RD-PAID",
        "redemption.burn_vs_request",
        "completeness",
        "undetermined",
        "insufficient_coverage",
        "obtain_coverage",
        {
            "fact:token_movement": "satisfied",
            "comparison": "satisfied",
            "quarantine": "satisfied",
            "chain_effects": "not_applicable",
        },
    ),
    (
        "R2",
        "red",
        "RD-CANCELLED",
        "redemption.cash_vs_expected",
        "applicability",
        "not_applicable",
        None,
        None,
        {"request_support": "not_evaluated", "comparison": "not_evaluated"},
    ),
    (
        "R3a",
        "red",
        "RD-CANCELLED",
        "redemption.cancellation_valid",
        "cancellation",
        "satisfied",
        None,
        None,
        {"fact:redemption_accepted": "satisfied"},
    ),
    (
        "R3b",
        "red",
        "RD-CANCELLED",
        "redemption.no_settlement_after_cancellation",
        "absence_coverage",
        "undetermined",
        "insufficient_coverage",
        "obtain_coverage",
        {
            "cancellation_in_force": "satisfied",
            "settlement": "satisfied",
            "chain_effects": "not_applicable",
        },
    ),
    (
        "R4",
        "red",
        "RD-PENDING",
        "redemption.cash_vs_expected",
        "fact:cash_settled",
        "undetermined",
        "not_yet_due",
        "await_due_time",
        {"comparison": "not_evaluated"},
    ),
    (
        "R5a",
        "red",
        "RD-MISSED",
        "redemption.payment_deadline",
        "deadline",
        "contradicted",
        None,
        None,
        {"acceptance_time": "satisfied"},
    ),
    (
        "R5b",
        "red",
        "RD-MISSED",
        "redemption.cash_vs_expected",
        "fact:cash_settled",
        "undetermined",
        "missing_evidence",
        "provide_observation",
        {},
    ),
    (
        "R6",
        "red",
        "RD-BAD-DUE",
        "redemption.declared_due_consistency",
        "declared_due",
        "undetermined",
        "inconsistent_evidence",
        "review_inconsistent_evidence",
        {"acceptance_time": "satisfied"},
    ),
    (
        "R7",
        "red",
        "RD-POSITION-AMBIGUOUS",
        "redemption.units_within_position",
        "position",
        "undetermined",
        "inconsistent_evidence",
        "review_inconsistent_evidence",
        {"comparison": "not_evaluated"},
    ),
    (
        "R8",
        "red",
        "RD-INEXACT",
        "redemption.cash_vs_expected",
        "expected_amount",
        "undetermined",
        "unsupported_capability",
        "outside_declared_capabilities",
        {"fact:cash_settled": "not_evaluated"},
    ),
    (
        "R9",
        "red",
        "RD-WRONG-RECIPIENT",
        "redemption.cash_vs_expected",
        "recipient",
        "contradicted",
        None,
        None,
        {"fact:cash_settled": "satisfied", "comparison": "not_evaluated"},
    ),
    (
        "R10a",
        "red",
        "RD-RETRACTED",
        "redemption.declared_due_consistency",
        "request_support",
        "undetermined",
        "withdrawn_evidence",
        "provide_observation",
        {"acceptance_time": "not_evaluated"},
    ),
    (
        "R10b",
        "red",
        "RD-RETRACTED",
        "redemption.cancellation_valid",
        "fact:redemption_accepted",
        "undetermined",
        "withdrawn_evidence",
        "provide_observation",
        {"cancellation": "not_evaluated"},
    ),
    (
        "R10c",
        "red",
        "RD-RETRACTED",
        "redemption.no_settlement_after_cancellation",
        "cancellation_in_force",
        "not_applicable",
        None,
        None,
        {"settlement": "not_evaluated"},
    ),
    (
        "R11",
        "red",
        "RD-UNLINKED-CANDIDATE",
        "redemption.cash_vs_expected",
        "fact:cash_settled",
        "undetermined",
        "unattributed",
        "provide_approved_link",
        {},
    ),
    (
        "R12",
        "red",
        "RD-CANCEL-UNAUTHORIZED",
        "redemption.cancellation_valid",
        "cancellation",
        "undetermined",
        "invalid_evidence",
        "review_inconsistent_evidence",
        {"retractions": "not_applicable"},
    ),
    (
        "R13",
        "red",
        "RD-CANCELLED-PAID",
        "redemption.no_settlement_after_cancellation",
        "settlement",
        "contradicted",
        None,
        None,
        {"absence_coverage": "not_evaluated"},
    ),
    (
        "R14",
        "red",
        "RD-BURN-BEFORE-ACCEPTANCE",
        "redemption.burn_vs_request",
        "fact:token_movement",
        "undetermined",
        "unsupported_capability",
        "outside_declared_capabilities",
        {"comparison": "not_evaluated"},
    ),
]


@pytest.mark.parametrize(
    ("case", "which", "scenario", "control", "check", "status", "category", "kind", "others"),
    TABLE,
    ids=[row[0] for row in TABLE],
)
def test_the_table_of_expected_diagnoses(
    subscription: Corpus,
    redemption: Corpus,
    case: str,
    which: str,
    scenario: str,
    control: str,
    check: str,
    status: str,
    category: str | None,
    kind: str | None,
    others: dict[str, str],
) -> None:
    corpus = subscription if which == "sub" else redemption
    view = diagnosis_of(corpus.inputs_for(scenario), control)
    assert view.diagnosis == "reconstructed"
    assert view.replay_consistent
    found = decisive(view)
    assert (found.requirement_id, found.status, found.category) == (check, status, category)
    assert found.reason_code == view.reason_code
    assert (found.next_step.kind if found.next_step else None) == kind
    got = statuses(view)
    for requirement, expected in others.items():
        assert got[requirement] == expected, (case, requirement)
    assert view.concluded == (view.status in ("PASS", "FAIL"))


def test_an_observed_match_without_completeness_is_not_shown_as_compliance(
    subscription: Corpus,
) -> None:
    """The example of the request: the amounts match, completeness is not shown (S1)."""
    view = diagnosis_of(subscription.inputs_for("K2"), "subscription.token_units_vs_order")
    checks = by_id(view)
    comparison = checks["comparison"]
    assert comparison.status == "satisfied"
    assert not comparison.determined_result
    assert comparison.left == comparison.right
    assert (comparison.left.atoms, comparison.left.scale, comparison.left.unit) == (
        "10000000000",
        7,
        "FUND_SHARE",
    )
    assert "observed only: the control is not concluded" in comparison.explanation
    assert checks["fact:token_movement"].admitted_evidence == ["obs-T1"]
    assert checks["fact:token_movement"].coverage_ids == ["cov-chain-k2"]
    assert checks["completeness"].coverage_ids == ["cov-chain-k2"]
    assert view.status == "UNKNOWN" and not view.concluded
    next_step = checks["completeness"].next_step
    assert next_step.fact_type == "token_movement"
    assert next_step.authoritative_source == "stellar-testnet-frozen"
    assert "never a promise" in next_step.caveat
    assert "declaration" in next_step.description  # a declaration never extends coverage


def test_a_proven_fail_stays_a_break_beside_blocked_requirements(subscription: Corpus) -> None:
    inputs = subscription.inputs_for("K3")
    stored = evaluate(inputs).result
    assert stored.result == "BREAK"
    cash = diagnosis_of(inputs, "subscription.cash_vs_order")
    found = decisive(cash)
    assert found.status == "contradicted" and found.next_step is None
    assert (found.delta.atoms, found.delta.scale, found.delta.unit) == ("-50000", 2, "USD")
    token = diagnosis_of(inputs, "subscription.token_units_vs_order")
    assert decisive(token).status == "undetermined"


def test_an_excess_keeps_its_fail_and_reports_what_stays_unresolved(subscription: Corpus) -> None:
    """A FAIL coexists with an undetermined requirement that cannot change it."""
    inputs = subscription.inputs_for("K2")
    observations = dict(inputs.observations)
    t1 = observations["obs-T1"]
    assert isinstance(t1.payload, TokenMovementPayload)
    more = t1.payload.units.model_copy(update={"atoms": str(int(t1.payload.units.atoms) + 1)})
    payload = t1.payload.model_copy(update={"units": more})
    observations["obs-T1"] = t1.model_copy(update={"payload": payload})
    view = diagnosis_of(replace(inputs, observations=observations), SUB_TOKEN)
    assert (view.status, view.reason_code) == ("FAIL", "UNITS_MISMATCH")
    checks = by_id(view)
    assert checks["comparison"].determined_result
    assert checks["comparison"].status == "contradicted"
    completeness = checks["completeness"]
    assert completeness.status == "undetermined" and not completeness.determined_result
    assert completeness.next_step is None
    assert "does not change this result" in completeness.explanation


SUB_TOKEN = "subscription.token_units_vs_order"


def test_an_unsupported_capability_is_not_a_request_for_documents(subscription: Corpus) -> None:
    """S11: units x price needing rounding stays outside the profile's capabilities."""
    inputs = subscription.inputs_for("K1")
    observations = dict(inputs.observations)
    order = observations["obs-O1"]
    assert isinstance(order.payload, OrderPayload)
    price = order.payload.price_per_unit
    odd = price.model_copy(update={"atoms": str(int(price.atoms) + 1)})
    units = order.payload.units.model_copy(update={"atoms": "1"})
    payload = order.payload.model_copy(update={"price_per_unit": odd, "units": units})
    observations["obs-O1"] = order.model_copy(update={"payload": payload})
    view = diagnosis_of(replace(inputs, observations=observations), "subscription.order_terms")
    found = decisive(view)
    assert (found.requirement_id, found.category) == ("operands", "unsupported_capability")
    assert found.next_step.kind == "outside_declared_capabilities"


def test_a_refused_admission_is_a_technical_error_not_a_missing_document(
    subscription: Corpus,
) -> None:
    """S9: an evaluation the engine refuses has no requirement checked but its admission."""
    inputs = subscription.inputs_for("K2")
    refused = inputs.snapshot.model_copy(update={"rules_ref": "subscription-synthetic-rules@9.9.9"})
    view = diagnosis_of(replace(inputs, snapshot=refused), "subscription.cash_vs_order")
    assert view.reason_code == "EVALUATION_ERROR"
    found = decisive(view)
    assert (found.requirement_id, found.category) == ("admission", "technical_error")
    assert found.next_step.kind == "correct_technical_error"
    assert found.next_step.fact_type is None
    assert {r.status for r in view.requirements[1:]} == {"not_evaluated"}


def test_a_technical_failure_inside_a_check_is_attributed_to_it(
    subscription: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S10: a crash in a check is a technical error there; nothing after it is evaluated."""

    def broken(self: Any, control_id: str) -> Any:
        raise RuntimeError("operand computation failed")

    monkeypatch.setattr(subscription_engine._Evaluator, "operands", broken)
    view = diagnosis_of(subscription.inputs_for("K2"), "subscription.cash_vs_order")
    found = decisive(view)
    assert (found.requirement_id, found.category) == ("operands", "technical_error")
    assert found.next_step.kind == "correct_technical_error"
    assert statuses(view)["comparison"] == "not_evaluated"
    assert by_id(view)["fact:cash_settled"].status == "satisfied"


def test_the_diagnosis_follows_the_engine_not_a_second_interpretation(
    subscription: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Change one engine check: the stored result and the diagnosis move together, because
    the diagnosis is the engine's own record of that run."""
    original = subscription_engine._Evaluator._coverage_problems

    def stricter(self: Any, certificate: Any, *args: Any) -> list[str]:
        problems = original(self, certificate, *args)
        return (
            [*problems, "rejected by the test"]
            if certificate.source_id == "bank-synthetic"
            else problems
        )

    monkeypatch.setattr(subscription_engine._Evaluator, "_coverage_problems", stricter)
    inputs = subscription.inputs_for("K2")
    stored = evaluate(inputs).result
    control = next(c for c in stored.controls if c.control_id == "subscription.cash_vs_order")
    assert control.reason_code == "INSUFFICIENT_COVERAGE"
    view = diagnose(stored, inputs, replay(inputs, stored.versions.engine_ref), control.control_id)
    found = decisive(view)
    assert found.requirement_id == "fact:cash_settled"
    assert "rejected by the test" in found.explanation
    assert found.admitted_evidence == ["obs-B1"]  # the payment was admitted; coverage was not


# ------------------------------------------------------------------ invariants


def _all_inputs() -> list[tuple[str, EvaluationInputs]]:
    found = []
    for root in (SUBSCRIPTION, REDEMPTION):
        corpus = load_corpus(root)
        found += [(f"{root.name}/{s}", corpus.inputs_for(s)) for s in corpus.scenarios]
    mappings = FIXTURES / "corpus/subscription-synthetic/mappings"
    for run in run_testnet_vertical(TESTNET, FIXTURES / "stellar", mappings):
        found.append((f"{TESTNET.name}/{run.scenario.scenario_id}", run.inputs))
    return found


@pytest.fixture(scope="module")
def every_input() -> list[tuple[str, EvaluationInputs]]:
    return _all_inputs()


def test_every_current_control_has_one_decisive_check_matching_its_result(
    every_input: list[tuple[str, EvaluationInputs]],
) -> None:
    status_of = {
        "PASS": "satisfied",
        "FAIL": "contradicted",
        "UNKNOWN": "undetermined",
        "NOT_APPLICABLE": "not_applicable",
    }
    controls = 0
    for label, inputs in every_input:
        stored = evaluate(inputs).result
        assert stored.versions.engine_ref in DIAGNOSTIC_ENGINES
        replayed = replay(inputs, stored.versions.engine_ref)
        members = {*inputs.snapshot.observation_ids, *inputs.snapshot.coverage_ids}
        for control in stored.controls:
            controls += 1
            view = diagnose(stored, inputs, replayed, control.control_id)
            assert view.diagnosis == "reconstructed", (label, control.control_id)
            found = decisive(view)
            assert found.reason_code == control.reason_code, (label, control.control_id)
            assert found.status == status_of[control.status], (label, control.control_id)
            spec = next(c for c in inputs.profile.controls if c.control_id == control.control_id)
            plan = control_plan(
                inputs.profile.operation_type,
                control.control_id,
                list(spec.requires),
                getattr(spec, "applies", None),
            )
            assert [r.requirement_id for r in view.requirements] == list(plan)
            position = [r.requirement_id for r in view.requirements].index(found.requirement_id)
            for index, requirement in enumerate(view.requirements):
                if requirement.status == "not_evaluated":
                    assert index > position, (label, control.control_id, requirement)
                if requirement.next_step is not None:
                    assert requirement.determined_result
                if requirement.category == "technical_error":
                    assert requirement.next_step is None or (
                        requirement.next_step.kind == "correct_technical_error"
                    )
                cited = {
                    *requirement.admitted_evidence,
                    *requirement.questioned_evidence,
                    *requirement.set_aside_evidence,
                    *requirement.coverage_ids,
                }
                assert cited <= members, (label, control.control_id, cited - members)
                assert cited <= set(control.evidence_refs), (
                    label,
                    control.control_id,
                    requirement.requirement_id,
                    cited - set(control.evidence_refs),
                )
    assert controls > 400


def test_the_real_testnet_delivery_is_fully_checked(
    every_input: list[tuple[str, EvaluationInputs]],
) -> None:
    """Real testnet chain data (profile 1.6.0): the linked delivery is complete per a
    coherent chain scope, so the token control concludes."""
    inputs = dict(every_input)[f"{TESTNET.name}/TN-LINKED"]
    view = diagnosis_of(inputs, SUB_TOKEN)
    assert (view.status, view.concluded) == ("PASS", True)
    checks = by_id(view)
    assert checks["completeness"].status == "satisfied"
    assert checks["comparison"].determined_result
    assert {r.status for r in view.requirements} <= {"satisfied", "not_applicable"}


# ------------------------------------------------------------------ history and integrity


def test_a_retired_engine_gets_no_reconstructed_reasoning(subscription: Corpus) -> None:
    """H1: a retired label runs as a compatibility implementation; its checks are not the
    historical engine's, so none are attributed to it."""
    inputs = subscription.inputs_for("K2")
    stored = replay(inputs, SUBSCRIPTION_ENGINE_0_9_0).result
    view = diagnose(stored, inputs, replay(inputs, SUBSCRIPTION_ENGINE_0_9_0), SUB_TOKEN)
    assert view.diagnosis == "unavailable_for_engine"
    assert view.engine_status == "retired"
    assert view.requirements == []
    assert any("compatibility implementation" in text for text in view.limitations)
    assert SUBSCRIPTION_ENGINE_0_9_0 not in DIAGNOSTIC_ENGINES
    assert DIAGNOSTIC_ENGINES == {SUBSCRIPTION_ENGINE_REF, REDEMPTION_ENGINE_REF}


def test_a_stored_evaluation_the_replay_does_not_reproduce_gets_no_explanation(
    subscription: Corpus,
) -> None:
    """H2: never a plausible explanation for a result the engine does not reproduce."""
    inputs = subscription.inputs_for("K2")
    stored = evaluate(inputs).result
    controls = [
        c.model_copy(update={"reason": "altered after the fact"})
        if c.control_id == SUB_TOKEN
        else c
        for c in stored.controls
    ]
    altered = stored.model_copy(update={"controls": controls})
    view = diagnose(altered, inputs, replay(inputs, stored.versions.engine_ref), SUB_TOKEN)
    assert view.diagnosis == "reconstruction_mismatch"
    assert not view.replay_consistent
    assert view.requirements == []


def test_evidence_known_later_never_enters_a_historical_diagnosis(subscription: Corpus) -> None:
    """H3: the corpus journal holds K2 and K3 evidence; K1's diagnosis cites only K1's."""
    inputs = subscription.inputs_for("K1")
    later = set(inputs.observations) - set(inputs.snapshot.observation_ids)
    assert {"obs-B1", "obs-B2"} <= later
    for control in ("subscription.cash_vs_order", "subscription.ta_units_vs_order", SUB_TOKEN):
        view = diagnosis_of(inputs, control)
        for requirement in view.requirements:
            cited = {*requirement.admitted_evidence, *requirement.questioned_evidence}
            assert not cited & later
    cash = decisive(diagnosis_of(inputs, "subscription.cash_vs_order"))
    assert cash.category == "missing_evidence"


def test_the_canonical_result_does_not_carry_the_trace(subscription: Corpus) -> None:
    evaluation = evaluate(subscription.inputs_for("K2"))
    assert evaluation.checks  # the side channel exists
    assert "checks" not in EvaluationResult.model_fields
    assert "checks" not in evaluation.result.model_dump(mode="json")
    retired = replay(subscription.inputs_for("K2"), SUBSCRIPTION_ENGINE_0_9_0)
    assert retired.checks == {}


# ------------------------------------------------------------------ query service and CLI


@dataclass
class Store:
    """The reads ``diagnose_control`` uses, over stored evaluations of one corpus."""

    corpus: Corpus
    evaluations: dict[str, tuple[EvaluationResult, EvaluationInputs]] = field(default_factory=dict)

    def add(self, scenario: str, engine: str | None = None) -> EvaluationResult:
        inputs = self.corpus.inputs_for(scenario)
        result = (replay(inputs, engine) if engine else evaluate(inputs)).result
        self.evaluations[result.evaluation_id] = (result, inputs)
        return result

    def _tenant(self, tenant_id: str) -> None:
        if tenant_id != TENANT:
            raise KeyError(tenant_id)

    def load_evaluation(self, tenant_id: str, evaluation_id: str) -> EvaluationResult:
        self._tenant(tenant_id)
        return self.evaluations[evaluation_id][0]

    def load_inputs(self, tenant_id: str, snapshot_id: str) -> EvaluationInputs:
        self._tenant(tenant_id)
        for result, inputs in self.evaluations.values():
            if result.snapshot_id == snapshot_id:
                return inputs
        raise KeyError(snapshot_id)


def access(**overrides: Any) -> AccessProfile:
    fields: dict[str, Any] = {
        "schema_version": "1.0",
        "principal_id": "analyst-1",
        "tenant_id": TENANT,
        "scopes": ["operations:read"],
        "max_items": 50,
        **overrides,
    }
    return AccessProfile.model_validate(fields)


def test_the_service_diagnoses_a_stored_evaluation_within_the_access_profile(
    subscription: Corpus,
) -> None:
    store = Store(subscription)
    stored = store.add("K2")
    service = QueryService(store, access())  # type: ignore[arg-type]
    view = service.diagnose_control(stored.evaluation_id, SUB_TOKEN)
    assert decisive(view).requirement_id == "completeness"
    assert view == diagnosis_of(subscription.inputs_for("K2"), SUB_TOKEN)

    other = QueryService(store, access(tenant_id="tenant-other"))  # type: ignore[arg-type]
    with pytest.raises(QueryError) as not_found:
        other.diagnose_control(stored.evaluation_id, SUB_TOKEN)
    assert not_found.value.code == "NOT_FOUND"
    assert "obs-" not in not_found.value.message

    no_scope = QueryService(store, access(scopes=["evidence:read"]))  # type: ignore[arg-type]
    with pytest.raises(QueryError) as forbidden:
        no_scope.diagnose_control(stored.evaluation_id, SUB_TOKEN)
    assert forbidden.value.code == "FORBIDDEN"

    with pytest.raises(QueryError) as unknown:
        service.diagnose_control(stored.evaluation_id, "subscription.nope")
    assert unknown.value.code == "NOT_FOUND"
    with pytest.raises(QueryError) as invalid:
        service.diagnose_control("../etc/passwd", SUB_TOKEN)
    assert invalid.value.code == "VALIDATION_ERROR"


def test_the_service_reports_the_historical_limitation(subscription: Corpus) -> None:
    store = Store(subscription)
    stored = store.add("K2", SUBSCRIPTION_ENGINE_0_9_0)
    service = QueryService(store, access())  # type: ignore[arg-type]
    view = service.diagnose_control(stored.evaluation_id, SUB_TOKEN)
    assert view.diagnosis == "unavailable_for_engine" and view.requirements == []


def test_get_missing_evidence_keeps_its_static_catalogue(subscription: Corpus) -> None:
    store = Store(subscription)
    stored = store.add("K1")
    service = QueryService(store, access())  # type: ignore[arg-type]
    missing = service.get_missing_evidence(stored.evaluation_id, "subscription.cash_vs_order")
    assert [(r.fact_type, r.authoritative_source) for r in missing.requirements] == [
        ("order_accepted", "oms-synthetic"),
        ("cash_settled", "bank-synthetic"),
    ]
    assert missing.missing is True
    assert missing.model_dump().keys() == {
        "evaluation_id",
        "control_id",
        "status",
        "reason_code",
        "missing",
        "requirements",
        "note",
    }


def test_the_cli_prints_the_diagnosis_offline(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["diagnose", str(SUBSCRIPTION), "K2", "--control", SUB_TOKEN, "--json"]) == 0
    (view,) = json.loads(capsys.readouterr().out)
    parsed = ControlDiagnosisView.model_validate_json(json.dumps(view))
    assert decisive(parsed).requirement_id == "completeness"
    assert any("offline" in text for text in parsed.limitations)
    assert main(["diagnose", str(REDEMPTION), "RD-PAID", "--control", "x"]) == 2
    retired = ["diagnose", str(SUBSCRIPTION), "K2", "--engine", SUBSCRIPTION_ENGINE_0_9_0]
    assert main(retired) == 0
    assert "unavailable_for_engine" in capsys.readouterr().out


def test_decisive_index_is_none_when_the_trace_does_not_explain_the_result(
    subscription: Corpus,
) -> None:
    evaluation = evaluate(subscription.inputs_for("K2"))
    control = next(c for c in evaluation.result.controls if c.control_id == SUB_TOKEN)
    records = evaluation.checks[SUB_TOKEN]
    assert determining_index(records, control) == len(records) - 1
    other = control.model_copy(update={"reason_code": "MISSING_EVIDENCE"})
    assert determining_index(records, other) is None


def test_the_next_step_names_the_fact_the_engine_was_reading(redemption: Corpus) -> None:
    """R3b: the absence check reads bank then chain coverage; the engine states which one
    blocked, so the suggestion names that source, never one inferred from the text."""
    view = diagnosis_of(
        redemption.inputs_for("RD-CANCELLED"), "redemption.no_settlement_after_cancellation"
    )
    next_step = decisive(view).next_step
    assert (next_step.fact_type, next_step.authoritative_source) == (
        "token_movement",
        "stellar-testnet-frozen",
    )
    paid = diagnosis_of(redemption.inputs_for("RD-PAID"), "redemption.burn_vs_request")
    assert decisive(paid).next_step.fact_type == "token_movement"


def test_weighed_evidence_is_set_aside_not_questioned(subscription: Corpus) -> None:
    """What a weighing check set aside (never counted) is reported apart from what a block
    questions and from what was admitted."""
    from invaria.engine.trace import CheckRecord
    from invaria.query.diagnostics import _check_view

    inputs = subscription.inputs_for("K2")
    control = next(c for c in evaluate(inputs).result.controls if c.control_id == SUB_TOKEN)
    record = CheckRecord(
        "attribution",
        "undetermined",
        "AMBIGUOUS_MATCH",
        "movement set aside",
        ("obs-T1", "cov-chain-k2"),
        ("obs-T1",),
    )
    view = _check_view(
        record,
        "subscription",
        control,
        True,
        frozenset(inputs.snapshot.coverage_ids),
        inputs.profile,
    )
    assert view.set_aside_evidence == ["obs-T1"]
    assert view.questioned_evidence == []
    assert view.admitted_evidence == []
    assert view.coverage_ids == ["cov-chain-k2"]
    assert view.next_step is not None
    assert view.next_step.kind == "provide_approved_link"
    assert "never link" in view.next_step.description
