"""Obligations view of the current profiles, without a database.

The expectations (cases O1-O19) were written as a table from the profiles, the engine
rules and the corpus descriptions before the implementation.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from invaria.cli import main
from invaria.contracts.evaluation import EvaluationResult
from invaria.contracts.observation import TokenMovementPayload
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.common import EvaluationInputs
from invaria.engine.evaluate import evaluate, replay
from invaria.engine.versions import (
    DIAGNOSTIC_ENGINES,
    REDEMPTION_ENGINE_0_10_0,
    SUBSCRIPTION_ENGINE_0_9_0,
)
from invaria.persistence.store import CurrentView
from invaria.query.models import AccessProfile, ObligationView, OperationObligationsView
from invaria.query.obligations import (
    CATALOGUE,
    CATALOGUE_REF,
    applicability_state,
    catalogue_entry,
    catalogue_problems,
    project_obligations,
    resolve,
)
from invaria.query.service import QueryError, QueryService
from invaria.vertical_testnet import run_testnet_vertical

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SUBSCRIPTION = FIXTURES / "corpus/subscription-synthetic-1.2.0"
SUBSCRIPTION_1_0_0 = FIXTURES / "corpus/subscription-synthetic"
REDEMPTION = FIXTURES / "corpus/redemption-synthetic-1.7.0"
TESTNET = FIXTURES / "corpus/subscription-testnet-1.6.0"
TENANT = "tenant-synthetic-demo"
SUB_TOKEN = "subscription.token_units_vs_order"
# Words a generated field must never use for an undetermined or measured result.
VERDICT_WORDS = ("unpaid", "undelivered", "breach", "default", "pending execution")


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


@pytest.fixture(scope="module")
def testnet() -> dict[str, EvaluationInputs]:
    mappings = FIXTURES / "corpus/subscription-synthetic/mappings"
    return {
        run.scenario.scenario_id: run.inputs
        for run in run_testnet_vertical(TESTNET, FIXTURES / "stellar", mappings)
    }


def view_of(inputs: EvaluationInputs, engine: str | None = None) -> OperationObligationsView:
    stored = (replay(inputs, engine) if engine else evaluate(inputs)).result
    return project_obligations(
        stored, inputs, replay(inputs, stored.versions.engine_ref), stored=False
    )


def obligation(view: OperationObligationsView, obligation_id: str) -> ObligationView:
    (found,) = [o for o in view.obligations if o.obligation_id == obligation_id]
    return found


def links(o: ObligationView) -> dict[str, tuple[str, str, str]]:
    return {c.control_id: (c.role, c.status, c.reason_code) for c in o.controls}


def generated_texts(o: ObligationView) -> list[str]:
    """Every text this view writes about the obligation; profile values are excluded (they
    are the profile's own words, cited)."""
    texts = [o.description, *o.not_derivable, *o.limitations]
    texts += [c.relation for c in o.controls]
    texts += [b.explanation for b in o.blocks]
    texts += [b.next_step.description for b in o.blocks if b.next_step is not None]
    texts += [c.explanation for c in o.applicability.checks]
    return texts


# ------------------------------------------------------------------ the table (O1-O19)


def test_o1_a_fully_checked_testnet_subscription(testnet: dict[str, EvaluationInputs]) -> None:
    view = view_of(testnet["TN-LINKED"])
    assert (view.catalogue, view.catalogue_ref, view.diagnosis) == (
        "available",
        CATALOGUE_REF,
        "reconstructed",
    )
    assert [o.obligation_id for o in view.obligations] == [
        "subscription.cash_settlement",
        "subscription.unit_registration",
        "subscription.token_delivery",
    ]
    for o in view.obligations:
        assert o.applicability.state == "applies"
        assert all(c.status == "PASS" and c.concluded for c in o.controls)
        assert o.blocks == []
    (delivered,) = obligation(view, "subscription.token_delivery").observed
    assert (delivered.status, delivered.scope) == ("satisfied", "decided")


def test_o2_an_observed_match_without_coverage_is_not_a_conclusion(subscription: Corpus) -> None:
    view = view_of(subscription.inputs_for("K2"))
    token = obligation(view, "subscription.token_delivery")
    assert links(token)[SUB_TOKEN] == ("performance", "UNKNOWN", "INSUFFICIENT_COVERAGE")
    (seen,) = token.observed
    assert (seen.status, seen.scope) == ("satisfied", "observed_only")
    assert seen.left is not None and seen.right is not None
    assert seen.left.to_decimal_text() == seen.right.to_decimal_text() == "1000.0000000"
    (block,) = token.blocks
    assert (block.requirement_id, block.category) == ("completeness", "insufficient_coverage")
    assert block.next_step is not None and block.next_step.kind == "obtain_coverage"
    assert any("observed quantities only" in text for text in token.limitations)
    assert (token.evidence_refs, token.coverage_ids) == (
        ["obs-O1", "obs-T1"],
        ["cov-oms-k2", "cov-chain-k2"],
    )
    for other in ("subscription.cash_settlement", "subscription.unit_registration"):
        assert all(c.status == "PASS" for c in obligation(view, other).controls)


def test_o3_a_delivery_without_execution_link_asks_for_an_approved_link(
    testnet: dict[str, EvaluationInputs],
) -> None:
    token = obligation(view_of(testnet["TN-NO-LINK"]), "subscription.token_delivery")
    (block,) = token.blocks
    assert block.category == "unattributed"
    assert block.next_step is not None and block.next_step.kind == "provide_approved_link"
    assert "never link" in block.next_step.description
    assert token.observed == []


def test_o4_a_proven_fail_stays_beside_an_unknown_obligation(subscription: Corpus) -> None:
    view = view_of(subscription.inputs_for("K3"))
    assert view.result == "BREAK"
    cash = obligation(view, "subscription.cash_settlement")
    assert links(cash)["subscription.cash_vs_order"][1] == "FAIL"
    (seen,) = cash.observed
    assert (seen.status, seen.scope) == ("contradicted", "decided")
    assert seen.delta is not None
    assert (seen.delta.atoms, seen.delta.scale, seen.delta.unit) == ("-50000", 2, "USD")
    assert cash.blocks == []
    token = obligation(view, "subscription.token_delivery")
    assert links(token)[SUB_TOKEN][1] == "UNKNOWN" and len(token.blocks) == 1


def _excess(inputs: EvaluationInputs) -> EvaluationInputs:
    observations = dict(inputs.observations)
    t1 = observations["obs-T1"]
    assert isinstance(t1.payload, TokenMovementPayload)
    more = t1.payload.units.model_copy(update={"atoms": str(int(t1.payload.units.atoms) + 1)})
    payload = t1.payload.model_copy(update={"units": more})
    observations["obs-T1"] = t1.model_copy(update={"payload": payload})
    return replace(inputs, observations=observations)


def test_o5_an_excess_keeps_its_fail_beside_another_unknown_control(subscription: Corpus) -> None:
    view = view_of(_excess(subscription.inputs_for("RETRACTION")))
    token = obligation(view, "subscription.token_delivery")
    assert links(token)[SUB_TOKEN][1:] == ("FAIL", "UNITS_MISMATCH")
    (seen,) = token.observed
    assert (seen.status, seen.scope) == ("contradicted", "decided")
    # The undetermined completeness check did not fix the FAIL: not a block.
    assert token.blocks == []
    cash = obligation(view, "subscription.cash_settlement")
    assert links(cash)["subscription.cash_vs_order"][1:] == ("UNKNOWN", "LOSS_OF_SUPPORT")
    assert view.result == "BREAK"


def test_o6_before_the_due_time_nothing_is_missing(redemption: Corpus) -> None:
    cash = obligation(view_of(redemption.inputs_for("RD-PENDING")), "redemption.cash_payment")
    assert cash.applicability.state == "applies"
    assert {(b.control_id, b.category) for b in cash.blocks} == {
        ("redemption.cash_vs_expected", "not_yet_due"),
        ("redemption.payment_deadline", "not_yet_due"),
    }
    assert {b.next_step.kind for b in cash.blocks if b.next_step} == {"await_due_time"}
    assert not any(w in t.lower() for t in generated_texts(cash) for w in VERDICT_WORDS)


def test_o7_after_the_due_time_a_missed_deadline_is_a_fail(redemption: Corpus) -> None:
    cash = obligation(view_of(redemption.inputs_for("RD-MISSED")), "redemption.cash_payment")
    assert links(cash)["redemption.payment_deadline"] == (
        "timeliness",
        "FAIL",
        "PAYMENT_MISSED",
    )
    (block,) = cash.blocks
    assert (block.control_id, block.category) == (
        "redemption.cash_vs_expected",
        "missing_evidence",
    )


def test_o8_a_clean_cancellation_extinguishes_without_fulfilling(redemption: Corpus) -> None:
    view = view_of(redemption.inputs_for("RD-CANCELLED"))
    for extinguished in (
        "redemption.cash_payment",
        "redemption.register_debit",
        "redemption.token_retirement",
    ):
        o = obligation(view, extinguished)
        assert o.applicability.state == "not_applicable"
        assert {c.status for c in o.applicability.checks} == {"not_applicable"}
        measured = [c for c in o.controls if c.role in ("performance", "timeliness")]
        assert {c.status for c in measured} == {"NOT_APPLICABLE"}
        assert not any(c.concluded for c in measured)
        assert o.observed == [] and o.blocks == []
        assert any("not fulfilled" in text for text in o.limitations)
    after = obligation(view, "redemption.no_settlement_after_cancellation")
    assert after.applicability.state == "applies"
    (block,) = after.blocks
    assert (block.requirement_id, block.category) == ("absence_coverage", "insufficient_coverage")


def test_o9_a_settlement_despite_cancellation_stays_visible(redemption: Corpus) -> None:
    view = view_of(redemption.inputs_for("RD-CANCELLED-PAID"))
    after = obligation(view, "redemption.no_settlement_after_cancellation")
    assert links(after)["redemption.no_settlement_after_cancellation"] == (
        "performance",
        "FAIL",
        "SETTLED_DESPITE_CANCELLATION",
    )
    assert view.result == "BREAK"


def test_o10_a_late_cancellation_keeps_the_missed_deadline(redemption: Corpus) -> None:
    cash = obligation(
        view_of(redemption.inputs_for("RD-CANCELLED-AFTER-DUE")), "redemption.cash_payment"
    )
    assert cash.applicability.state == "mixed"
    checks = {c.control_id: c.status for c in cash.applicability.checks}
    assert checks == {
        "redemption.cash_vs_expected": "not_applicable",
        "redemption.payment_deadline": "satisfied",
    }
    assert links(cash)["redemption.payment_deadline"][1] == "FAIL"


def test_o11_an_inapplicable_obligation_is_never_shown_as_fulfilled(redemption: Corpus) -> None:
    after = obligation(
        view_of(redemption.inputs_for("RD-PAID")), "redemption.no_settlement_after_cancellation"
    )
    assert after.applicability.state == "not_applicable"
    assert links(after)["redemption.no_settlement_after_cancellation"][1] == "NOT_APPLICABLE"
    assert after.observed == [] and after.blocks == []


@pytest.mark.parametrize(
    ("root", "scenario"), [(SUBSCRIPTION, "K2"), (REDEMPTION, "RD-PAID")], ids=["sub", "red"]
)
def test_o12_a_technical_error_never_asks_for_documents(root: Path, scenario: str) -> None:
    inputs = load_corpus(root).inputs_for(scenario)
    refused = inputs.snapshot.model_copy(update={"rules_ref": "unknown-rules@9.9.9"})
    view = view_of(replace(inputs, snapshot=refused))
    for o in view.obligations:
        assert o.blocks, o.obligation_id
        assert {b.category for b in o.blocks} == {"technical_error"}
        assert {b.next_step.kind for b in o.blocks if b.next_step} == {"correct_technical_error"}
        if o.applicability.condition != "always":
            assert o.applicability.state == "not_determined"


def test_o13_withdrawn_evidence(subscription: Corpus, redemption: Corpus) -> None:
    cash = obligation(
        view_of(subscription.inputs_for("RETRACTION")), "subscription.cash_settlement"
    )
    assert [b.category for b in cash.blocks] == ["withdrawn_evidence"]
    view = view_of(redemption.inputs_for("RD-RETRACTED"))
    debit = obligation(view, "redemption.register_debit")
    assert {b.category for b in debit.blocks} == {"withdrawn_evidence"}


def test_o14_a_retired_engine_keeps_its_stored_results_without_detail(
    subscription: Corpus, redemption: Corpus
) -> None:
    view = view_of(subscription.inputs_for("K2"), SUBSCRIPTION_ENGINE_0_9_0)
    assert (view.catalogue, view.diagnosis) == ("available", "unavailable_for_engine")
    assert view.versions.engine_status == "retired"
    stored = replay(subscription.inputs_for("K2"), SUBSCRIPTION_ENGINE_0_9_0).result
    status = {c.control_id: (c.status, c.reason_code) for c in stored.controls}
    for o in view.obligations:
        assert o.observed == [] and o.blocks == []
        assert o.applicability.state == "applies"  # declared "always" by the profile
        for c in o.controls:
            assert (c.status, c.reason_code) == status[c.control_id]
        assert any("No check detail" in text for text in o.limitations)
    assert all(c.diagnosis.requirements == [] for c in view.controls)

    retired = view_of(redemption.inputs_for("RD-CANCELLED"), REDEMPTION_ENGINE_0_10_0)
    assert retired.diagnosis == "unavailable_for_engine"
    cash = obligation(retired, "redemption.cash_payment")
    assert cash.applicability.state == "not_determined"
    assert {c.status for c in cash.applicability.checks} == {None}
    recorded = replay(redemption.inputs_for("RD-CANCELLED"), REDEMPTION_ENGINE_0_10_0).result
    status = {c.control_id: (c.status, c.reason_code) for c in recorded.controls}
    assert all((c.status, c.reason_code) == status[c.control_id] for c in cash.controls)


def test_o15_a_stored_evaluation_the_replay_does_not_reproduce(subscription: Corpus) -> None:
    inputs = subscription.inputs_for("K2")
    stored = evaluate(inputs).result
    controls = [
        c.model_copy(update={"reason": "altered after the fact"})
        if c.control_id == SUB_TOKEN
        else c
        for c in stored.controls
    ]
    altered = stored.model_copy(update={"controls": controls})
    view = project_obligations(altered, inputs, replay(inputs, stored.versions.engine_ref))
    assert (view.diagnosis, view.replay_consistent) == ("reconstruction_mismatch", False)
    assert view.evaluation_id == stored.evaluation_id and view.result == stored.result
    for o in view.obligations:
        assert o.observed == [] and o.blocks == []
    assert all(c.diagnosis.requirements == [] for c in view.controls)


def test_o16_a_profile_outside_the_catalogue_is_not_inferred() -> None:
    view = view_of(load_corpus(SUBSCRIPTION_1_0_0).inputs_for("K2"))
    assert view.versions.profile_ref == "fund-subscription-synthetic@1.0.0"
    assert (view.catalogue, view.catalogue_ref) == ("unavailable_for_profile", None)
    assert view.obligations == [] and view.not_projected == []
    assert [c.control_id for c in view.controls] == [
        "subscription.order_terms",
        "subscription.cash_vs_order",
        "subscription.ta_units_vs_order",
        SUB_TOKEN,
    ]
    assert all(c.obligations == [] for c in view.controls)
    assert any("none is inferred" in text for text in view.limitations)


def test_o17_a_profile_that_does_not_match_its_entry_projects_nothing(
    subscription: Corpus,
) -> None:
    inputs = subscription.inputs_for("K2")
    renamed = [
        c.model_copy(update={"left": ""}) if c.control_id == SUB_TOKEN else c
        for c in inputs.profile.controls
    ]
    altered = replace(inputs, profile=inputs.profile.model_copy(update={"controls": renamed}))
    stored = evaluate(inputs).result
    view = project_obligations(stored, altered, replay(inputs, stored.versions.engine_ref))
    assert view.catalogue == "mismatch_with_profile"
    assert view.obligations == []
    assert any("does not resolve" in text for text in view.limitations)


def test_a_catalogue_entry_must_cover_exactly_the_profile_and_its_conditions(
    subscription: Corpus, redemption: Corpus
) -> None:
    """The catalogue never projects onto a profile whose controls or declared conditions
    differ from the entry's, even when every cited path still resolves."""
    inputs = subscription.inputs_for("K2")
    entry = catalogue_entry(inputs.profile.profile_ref)
    assert entry is not None and catalogue_problems(entry, inputs) == []
    fewer = [c for c in inputs.profile.controls if c.control_id != "subscription.order_terms"]
    missing = replace(inputs, profile=inputs.profile.model_copy(update={"controls": fewer}))
    assert catalogue_problems(entry, missing) == [
        "control subscription.order_terms is not in both the profile and the catalogue"
    ]
    rd = redemption.inputs_for("RD-PAID")
    entry = catalogue_entry(rd.profile.profile_ref)
    assert entry is not None and catalogue_problems(entry, rd) == []
    always = [
        c.model_copy(update={"applies": "always"})
        if c.control_id == "redemption.cash_vs_expected"
        else c
        for c in rd.profile.controls
    ]
    changed = replace(rd, profile=rd.profile.model_copy(update={"controls": always}))
    assert catalogue_problems(entry, changed) == [
        "redemption.cash_vs_expected does not declare the condition unless_cancelled"
    ]


def test_o18_a_control_shared_by_several_obligations_appears_once(
    subscription: Corpus, redemption: Corpus
) -> None:
    sub = view_of(subscription.inputs_for("K2"))
    shared = {c.control_id: c.obligations for c in sub.controls}
    assert shared["subscription.order_terms"] == [
        "subscription.cash_settlement",
        "subscription.unit_registration",
        "subscription.token_delivery",
    ]
    red = view_of(redemption.inputs_for("RD-PAID"))
    shared = {c.control_id: c.obligations for c in red.controls}
    assert len(shared["redemption.units_within_position"]) == 3
    assert len(shared["redemption.cancellation_valid"]) == 4
    for view in (sub, red):
        ids = [o.obligation_id for o in view.obligations]
        assert len(ids) == len(set(ids))
        linked = {link.control_id for o in view.obligations for link in o.controls}
        assert linked == {c.control_id for c in view.controls}


def test_o19_evidence_known_later_never_enters(subscription: Corpus) -> None:
    inputs = subscription.inputs_for("K1")
    later = set(inputs.observations) - set(inputs.snapshot.observation_ids)
    assert {"obs-B1", "obs-B2"} <= later
    view = view_of(inputs)
    for o in view.obligations:
        assert not set(o.evidence_refs) & later
    cash = obligation(view, "subscription.cash_settlement")
    assert [b.category for b in cash.blocks] == ["missing_evidence"]


# ------------------------------------------------------------------ invariants


@pytest.fixture(scope="module")
def every_view(
    subscription: Corpus, redemption: Corpus, testnet: dict[str, EvaluationInputs]
) -> list[tuple[str, EvaluationInputs, EvaluationResult, OperationObligationsView]]:
    found = []
    pairs = [(f"sub/{s}", subscription.inputs_for(s)) for s in subscription.scenarios]
    pairs += [(f"red/{s}", redemption.inputs_for(s)) for s in redemption.scenarios]
    pairs += [(f"tn/{s}", inputs) for s, inputs in testnet.items()]
    for label, inputs in pairs:
        stored = evaluate(inputs).result
        view = project_obligations(stored, inputs, replay(inputs, stored.versions.engine_ref))
        found.append((label, inputs, stored, view))
    return found


def test_every_current_scenario_projects_grounded_obligations(
    every_view: list[tuple[str, EvaluationInputs, EvaluationResult, OperationObligationsView]],
) -> None:
    assert len(every_view) == 59
    for label, inputs, stored, view in every_view:
        assert (view.catalogue, view.diagnosis) == ("available", "reconstructed"), label
        assert view.result == stored.result and view.evaluation_id == stored.evaluation_id
        data = inputs.profile.model_dump(mode="json")
        ids = [o.obligation_id for o in view.obligations]
        assert len(ids) == len(set(ids)), label
        recorded = {c.control_id: c for c in stored.controls}
        assert [c.control_id for c in view.controls] == list(recorded), label
        members = {*inputs.snapshot.observation_ids, *inputs.snapshot.coverage_ids}
        for c in view.controls:
            assert c.obligations, (label, c.control_id)
            assert (c.status, c.reason_code) == (
                recorded[c.control_id].status,
                recorded[c.control_id].reason_code,
            )
        for o in view.obligations:
            assert o.basis, (label, o.obligation_id)
            for b in (*o.basis, *o.applicability.declared_by):
                assert resolve(data, b.path) == b.value != "", (label, b.path)
            cited: set[str] = set()
            for link in o.controls:
                control = recorded[link.control_id]
                assert (link.status, link.reason_code) == (control.status, control.reason_code)
                cited |= set(control.evidence_refs)
            refs = {*o.evidence_refs, *o.coverage_ids}
            assert refs <= members and refs <= cited, (label, o.obligation_id)
            for block in o.blocks:
                assert recorded[block.control_id].status == "UNKNOWN", (label, block)
            for item in o.observed:
                if item.scope == "decided":
                    assert recorded[item.control_id].status in ("PASS", "FAIL")
            for text in generated_texts(o):
                assert not any(w in text.lower() for w in VERDICT_WORDS), (label, text)


def test_the_view_is_deterministic(
    every_view: list[tuple[str, EvaluationInputs, EvaluationResult, OperationObligationsView]],
) -> None:
    for _, inputs, stored, view in every_view[::7]:
        again = project_obligations(stored, inputs, replay(inputs, stored.versions.engine_ref))
        assert again.model_dump_json() == view.model_dump_json()


def test_the_catalogue_is_bound_to_exact_profile_versions() -> None:
    bound = {ref for entry in CATALOGUE for ref in entry.profile_refs}
    assert bound == {
        "fund-subscription-synthetic@1.2.0",
        "fund-subscription-testnet@1.6.0",
        "fund-redemption-synthetic@1.7.0",
    }
    assert catalogue_entry("fund-subscription-synthetic@1.1.0") is None
    assert catalogue_entry("fund-subscription-synthetic@1.2") is None


def test_the_presentation_rule_of_applicability() -> None:
    assert applicability_state(["satisfied", "satisfied"]) == "applies"
    assert applicability_state(["not_applicable"]) == "not_applicable"
    assert applicability_state(["satisfied", "not_applicable"]) == "mixed"
    assert applicability_state(["satisfied", "undetermined"]) == "not_determined"
    assert applicability_state(["satisfied", None]) == "not_determined"
    assert applicability_state([]) == "not_determined"


def test_the_projection_does_not_change_the_engine_or_its_labels(subscription: Corpus) -> None:
    inputs = subscription.inputs_for("K2")
    before = evaluate(inputs).result.model_dump_json()
    view_of(inputs)
    assert evaluate(inputs).result.model_dump_json() == before
    assert DIAGNOSTIC_ENGINES == {"invaria-engine@0.10.0", "invaria-redemption-engine@0.11.0"}


# ------------------------------------------------------------------ query service and CLI


@dataclass
class Store:
    """The reads ``get_operation_obligations`` uses, over stored evaluations of one corpus."""

    corpus: Corpus
    evaluations: dict[str, tuple[EvaluationResult, EvaluationInputs]] = field(default_factory=dict)
    published: dict[str, CurrentView] = field(default_factory=dict)

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

    def current_evaluation(self, tenant_id: str, operation_ref: str) -> CurrentView | None:
        self._tenant(tenant_id)
        if operation_ref not in self.published:
            raise KeyError(operation_ref)
        return self.published[operation_ref]

    def evaluations_for(self, tenant_id: str, operation_ref: str) -> list[EvaluationResult]:
        self._tenant(tenant_id)
        return [r for r, _ in self.evaluations.values() if r.operation_ref == operation_ref]


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


def test_the_service_projects_within_the_access_profile(subscription: Corpus) -> None:
    store = Store(subscription)
    k1 = store.add("K1")
    k2 = store.add("K2")
    service = QueryService(store, access())  # type: ignore[arg-type]
    latest = service.get_operation_obligations(k2.operation_ref)
    assert latest.evaluation_id == k2.evaluation_id
    assert latest.currency is not None and latest.currency.currency == "not_published"
    offline = view_of(subscription.inputs_for("K2"))
    assert latest.obligations == offline.obligations

    store.published[k1.operation_ref] = CurrentView(k1.evaluation_id, 1, 2, "stale")
    published = service.get_operation_obligations(k1.operation_ref)
    assert published.evaluation_id == k1.evaluation_id
    assert published.currency is not None and published.currency.currency == "stale"
    historical = service.get_operation_obligations(k2.operation_ref, k2.evaluation_id)
    assert historical.evaluation_id == k2.evaluation_id
    assert historical.currency is not None and historical.currency.currency == "superseded"

    other = QueryService(store, access(tenant_id="tenant-other"))  # type: ignore[arg-type]
    with pytest.raises(QueryError) as not_found:
        other.get_operation_obligations(k2.operation_ref, k2.evaluation_id)
    assert not_found.value.code == "NOT_FOUND"
    assert "obs-" not in not_found.value.message

    no_scope = QueryService(store, access(scopes=["evidence:read"]))  # type: ignore[arg-type]
    with pytest.raises(QueryError) as forbidden:
        no_scope.get_operation_obligations(k2.operation_ref)
    assert forbidden.value.code == "FORBIDDEN"

    with pytest.raises(QueryError) as foreign:
        service.get_operation_obligations("SUB-9999", k2.evaluation_id)
    assert foreign.value.code == "NOT_FOUND"
    with pytest.raises(QueryError) as unknown:
        service.get_operation_obligations("SUB-9999")
    assert unknown.value.code == "NOT_FOUND"
    with pytest.raises(QueryError) as invalid:
        service.get_operation_obligations("../etc/passwd")
    assert invalid.value.code == "VALIDATION_ERROR"


def test_the_service_projects_a_retired_engine_without_detail(subscription: Corpus) -> None:
    store = Store(subscription)
    stored = store.add("K2", SUBSCRIPTION_ENGINE_0_9_0)
    service = QueryService(store, access())  # type: ignore[arg-type]
    view = service.get_operation_obligations(stored.operation_ref, stored.evaluation_id)
    assert view.diagnosis == "unavailable_for_engine"
    assert all(o.blocks == [] and o.observed == [] for o in view.obligations)


def test_the_service_replays_once_per_query(
    subscription: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invaria.query import service as service_module

    calls: list[str] = []
    real = replay

    def counted(inputs: EvaluationInputs, engine_ref: str) -> Any:
        calls.append(engine_ref)
        return real(inputs, engine_ref)

    monkeypatch.setattr(service_module, "replay", counted)
    store = Store(subscription)
    stored = store.add("K2")
    QueryService(store, access()).get_operation_obligations(stored.operation_ref)  # type: ignore[arg-type]
    assert len(calls) == 1


def test_the_cli_prints_the_obligations_offline(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["obligations", str(SUBSCRIPTION), "K2", "--json"]) == 0
    parsed = OperationObligationsView.model_validate_json(capsys.readouterr().out)
    assert parsed.currency is None
    assert any("offline" in text for text in parsed.limitations)
    assert obligation(parsed, "subscription.token_delivery").observed[0].scope == "observed_only"
    assert main(["obligations", str(REDEMPTION), "RD-CANCELLED-AFTER-DUE"]) == 0
    text = capsys.readouterr().out
    assert "applicability unless_cancelled: mixed" in text
    # The engine's check is shown as a check of the condition, never as the applicability.
    assert (
        "condition check applicability in redemption.payment_deadline: satisfied "
        "(condition holds: this control applies)" in text
    )
    assert (
        "condition check applicability in redemption.cash_vs_expected: not_applicable "
        "(condition does not hold: this control does not apply)" in text
    )
    assert "payment_deadline applicability: satisfied" not in text
    assert "[timeliness] redemption.payment_deadline: FAIL PAYMENT_MISSED" in text
    assert main(["obligations", str(REDEMPTION), "RD-NOPE"]) == 2
    retired = ["obligations", str(SUBSCRIPTION), "K2", "--engine", SUBSCRIPTION_ENGINE_0_9_0]
    assert main(retired) == 0
    assert "diagnosis unavailable_for_engine" in capsys.readouterr().out
    assert main(["obligations", str(SUBSCRIPTION_1_0_0), "K2"]) == 0
    assert "catalogue unavailable_for_profile" in capsys.readouterr().out


def test_only_a_reconstructed_diagnosis_feeds_observed_and_blocks(
    subscription: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: should a diagnosis that is not reconstructed ever carry checks,
    the view still attributes none of them to the stored result."""
    from invaria.query import obligations as module
    from invaria.query.diagnostics import diagnose as real

    def labelled(*args: Any, **kwargs: Any) -> Any:
        view = real(*args, **kwargs)
        return view.model_copy(update={"diagnosis": "reconstruction_mismatch"})

    monkeypatch.setattr(module, "diagnose", labelled)
    view = view_of(subscription.inputs_for("K2"))
    assert any(c.diagnosis.requirements for c in view.controls)
    for o in view.obligations:
        assert o.observed == [] and o.blocks == []
