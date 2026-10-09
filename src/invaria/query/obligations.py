"""The obligations a profile sets for an operation. Pure: no I/O.

A projection of the evaluation's profile, closed snapshot and stored result, through the
requirement diagnosis of each control. Profiles name controls, not obligations, so an explicit
presentation catalogue, bound to exact profile versions, groups the controls into the
obligations the profile's rules already state; every obligation cites the profile paths it
rests on, read from the evaluation's own profile. Nothing here evaluates evidence, reads a
reason text or produces a result of its own.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from invaria.contracts.evaluation import EvaluationResult
from invaria.engine.common import Evaluation, EvaluationInputs
from invaria.engine.versions import DIAGNOSTIC_ENGINES, engine_status
from invaria.query.diagnostics import LIMITATION_OFFLINE, diagnose
from invaria.query.models import (
    LIMITATION_STORED_ONLY,
    ApplicabilityCheckView,
    ApplicabilityCondition,
    ApplicabilityState,
    ApplicabilityView,
    BlockView,
    CatalogueStatus,
    ControlDiagnosisView,
    CurrencyView,
    DiagnosisBasis,
    NotProjectedView,
    ObligationControlLink,
    ObligationControlView,
    ObligationRole,
    ObligationView,
    ObservedComparisonView,
    OperationObligationsView,
    ProfileBasisView,
    SnapshotView,
    VersionsView,
)

CATALOGUE_REF = "obligation-catalogue@1.0.0"

# The engine check that judges each declared condition in a control.
_CONDITION_CHECK: Mapping[ApplicabilityCondition, str] = {
    "unless_cancelled": "applicability",
    "cancellation": "cancellation_in_force",
}
_MEASURING: frozenset[ObligationRole] = frozenset({"performance", "timeliness"})


@dataclass(frozen=True)
class Link:
    control_id: str
    role: ObligationRole
    relation: str


@dataclass(frozen=True)
class Obligation:
    obligation_id: str
    description: str
    basis: tuple[str, ...]
    condition: ApplicabilityCondition
    condition_basis: tuple[str, ...]
    links: tuple[Link, ...]
    not_derivable: tuple[str, ...]


@dataclass(frozen=True)
class CatalogueEntry:
    profile_refs: frozenset[str]
    obligations: tuple[Obligation, ...]
    not_projected: tuple[tuple[str, str], ...]


# ------------------------------------------------------------------ catalogue 1.0.0

_ORDER_TERMS = Link(
    "subscription.order_terms",
    "term",
    "Checks that the order the obligation is measured against is consistent with the "
    "profile's price; evaluated separately, it does not change this obligation's control.",
)

_SUBSCRIPTION = CatalogueEntry(
    profile_refs=frozenset(
        {"fund-subscription-synthetic@1.2.0", "fund-subscription-testnet@1.6.0"}
    ),
    obligations=(
        Obligation(
            "subscription.cash_settlement",
            "The cash settled for the order equals the order's cash amount.",
            (
                "controls[subscription.cash_vs_order].left",
                "controls[subscription.cash_vs_order].right",
                "controls[subscription.cash_vs_order].comparison",
                "correlation.institutional_link",
            ),
            "always",
            (),
            (
                Link(
                    "subscription.cash_vs_order",
                    "performance",
                    "Compares the settled cash with the order's cash amount.",
                ),
                _ORDER_TERMS,
            ),
            ("The profile declares no payment deadline and does not name who owes the payment.",),
        ),
        Obligation(
            "subscription.unit_registration",
            "The transfer agent's register records the order's units.",
            (
                "controls[subscription.ta_units_vs_order].left",
                "controls[subscription.ta_units_vs_order].right",
                "controls[subscription.ta_units_vs_order].comparison",
            ),
            "always",
            (),
            (
                Link(
                    "subscription.ta_units_vs_order",
                    "performance",
                    "Compares the registered units with the order's units.",
                ),
                _ORDER_TERMS,
            ),
            ("The profile declares no registration deadline.",),
        ),
        Obligation(
            "subscription.token_delivery",
            "The on-chain delivery explicitly linked to the order adds up to the order's units.",
            (
                "controls[subscription.token_units_vs_order].left",
                "controls[subscription.token_units_vs_order].right",
                "controls[subscription.token_units_vs_order].comparison",
                "correlation.chain_link",
                "correlation.account_link",
                "representation_ratio",
            ),
            "always",
            (),
            (
                Link(
                    "subscription.token_units_vs_order",
                    "performance",
                    "Compares the linked delivery with the order's units.",
                ),
                _ORDER_TERMS,
            ),
            (
                "The profile declares no delivery deadline and no order between cash and "
                "token delivery.",
            ),
        ),
    ),
    not_projected=(
        (
            "A deadline for payment, registration or delivery",
            "No subscription profile in the catalogue declares a deadline.",
        ),
        (
            "An order between cash settlement and token delivery",
            "Not declared by the profile.",
        ),
    ),
)

_UNITS_IN_POSITION = Link(
    "redemption.units_within_position",
    "term",
    "Checks that the units requested, which the obligation is measured in, were available "
    "before acceptance; evaluated separately.",
)
_CANCELLATION = Link(
    "redemption.cancellation_valid",
    "applicability_condition",
    "Judges whether a valid cancellation is in force, the condition that extinguishes the "
    "pending obligation.",
)
_UNLESS_CANCELLED = ("cancellation.effect",)

_REDEMPTION = CatalogueEntry(
    profile_refs=frozenset({"fund-redemption-synthetic@1.7.0"}),
    obligations=(
        Obligation(
            "redemption.cash_payment",
            "Pay the requesting account the units requested times the approved price, by "
            "the declared deadline after acceptance.",
            (
                "controls[redemption.cash_vs_expected].left",
                "controls[redemption.cash_vs_expected].right",
                "pricing.expected_payment",
                "controls[redemption.payment_deadline].right",
                "payment_deadline.hours",
                "payment_deadline.on_time",
                "payment_deadline.late_payment",
            ),
            "unless_cancelled",
            (
                "controls[redemption.cash_vs_expected].applies",
                "controls[redemption.payment_deadline].applies",
                *_UNLESS_CANCELLED,
                "cancellation.effective_after_due",
            ),
            (
                Link(
                    "redemption.cash_vs_expected",
                    "performance",
                    "Compares the cash paid to the requesting account with the expected amount.",
                ),
                Link(
                    "redemption.payment_deadline",
                    "timeliness",
                    "Judges the payment's effective time against the deadline.",
                ),
                Link(
                    "redemption.price_vs_approved",
                    "term",
                    "Checks the request's price against the approved price the expected "
                    "amount uses; evaluated separately.",
                ),
                _UNITS_IN_POSITION,
                Link(
                    "redemption.declared_due_consistency",
                    "term",
                    "Checks the declared due time against the computed one the deadline is "
                    "judged with; evaluated separately.",
                ),
                _CANCELLATION,
            ),
            (
                "The due instant is computed by the engine and shown in the payment_deadline "
                "diagnosis; this view does not recompute it.",
            ),
        ),
        Obligation(
            "redemption.register_debit",
            "The transfer agent's register debits the units requested.",
            (
                "controls[redemption.ta_units_vs_request].left",
                "controls[redemption.ta_units_vs_request].right",
                "quantity.request",
            ),
            "unless_cancelled",
            ("controls[redemption.ta_units_vs_request].applies", *_UNLESS_CANCELLED),
            (
                Link(
                    "redemption.ta_units_vs_request",
                    "performance",
                    "Compares the units debited in the register with the units requested.",
                ),
                _UNITS_IN_POSITION,
                _CANCELLATION,
            ),
            ("The profile declares no deadline for the debit.",),
        ),
        Obligation(
            "redemption.token_retirement",
            "Retire the units requested on-chain (burn to the issuer), explicitly linked to "
            "the request.",
            (
                "controls[redemption.burn_vs_request].left",
                "controls[redemption.burn_vs_request].right",
                "quantity.retirement",
                "correlation.chain_link",
            ),
            "unless_cancelled",
            ("controls[redemption.burn_vs_request].applies", *_UNLESS_CANCELLED),
            (
                Link(
                    "redemption.burn_vs_request",
                    "performance",
                    "Compares the linked burns after acceptance with the units requested.",
                ),
                _UNITS_IN_POSITION,
                _CANCELLATION,
            ),
            ("The profile declares no deadline for the burn.",),
        ),
        Obligation(
            "redemption.no_settlement_after_cancellation",
            "After a valid cancellation, neither a payment nor a burn is linked to the request.",
            (
                "controls[redemption.no_settlement_after_cancellation].left",
                "controls[redemption.no_settlement_after_cancellation].right",
                "cancellation.settlement_despite_cancellation",
                "cancellation.automatic_reversal",
            ),
            "cancellation",
            ("controls[redemption.no_settlement_after_cancellation].applies",),
            (
                Link(
                    "redemption.no_settlement_after_cancellation",
                    "performance",
                    "Looks for payments and burns linked to the cancelled request.",
                ),
                Link(
                    "redemption.cancellation_valid",
                    "applicability_condition",
                    "Judges whether a valid cancellation is in force, the condition under "
                    "which this obligation holds.",
                ),
            ),
            (),
        ),
    ),
    not_projected=(
        (
            "A deadline for the register debit or the burn",
            "Only the payment has a declared deadline (payment_deadline).",
        ),
        (
            "An order between payment and burn",
            "Not declared by the profile.",
        ),
        (
            "Reversing a settlement made despite a cancellation",
            "The profile declares no automatic reversal; a settlement despite cancellation "
            "requires review, which is not an obligation to reverse.",
        ),
        (
            "Reserving or releasing the position",
            "The position is an input of a check, not a declared obligation.",
        ),
    ),
)

CATALOGUE: tuple[CatalogueEntry, ...] = (_SUBSCRIPTION, _REDEMPTION)


def catalogue_entry(profile_ref: str) -> CatalogueEntry | None:
    """The entry bound to this exact profile version; never matched by name similarity."""
    return next((e for e in CATALOGUE if profile_ref in e.profile_refs), None)


# ------------------------------------------------------------------ profile paths

_PATH = re.compile(r"^([a-z_]+)(?:\[([a-z0-9_.]+)\])?((?:\.[a-z_]+)*)$")


def resolve(profile: Mapping[str, Any], path: str) -> str | None:
    """The profile's own value at ``path`` as text; None when the path does not resolve
    to a scalar or a list of scalars."""
    match = _PATH.match(path)
    if match is None:
        return None
    head, key, rest = match.groups()
    node: Any = profile.get(head)
    if key is not None:
        node = next(
            (c for c in node or () if isinstance(c, dict) and c.get("control_id") == key), None
        )
    for part in (rest or "").split(".")[1:]:
        node = node.get(part) if isinstance(node, dict) else None
    if isinstance(node, bool):
        return "true" if node else "false"
    if isinstance(node, str | int):
        return str(node) or None
    if isinstance(node, list) and node and all(isinstance(v, str | int) for v in node):
        return ", ".join(str(v) for v in node)
    return None


def catalogue_problems(entry: CatalogueEntry, inputs: EvaluationInputs) -> list[str]:
    """Why the entry does not fit the evaluation's profile: a path that does not resolve,
    a control not linked or not in the profile, or a declared condition that differs."""
    profile = inputs.profile
    data = profile.model_dump(mode="json")
    problems = []
    for obligation in entry.obligations:
        for path in (*obligation.basis, *obligation.condition_basis):
            if resolve(data, path) is None:
                problems.append(f"{obligation.obligation_id}: path {path} does not resolve")
    linked = {link.control_id for o in entry.obligations for link in o.links}
    declared = {c.control_id for c in profile.controls}
    for control_id in sorted(declared ^ linked):
        problems.append(f"control {control_id} is not in both the profile and the catalogue")
    specs = {c.control_id: c for c in profile.controls}
    for obligation in entry.obligations:
        expected = None if obligation.condition == "always" else obligation.condition
        for link in obligation.links:
            spec = specs.get(link.control_id)
            if link.role in _MEASURING and spec is not None:
                if getattr(spec, "applies", None) != expected:
                    problems.append(
                        f"{link.control_id} does not declare the condition {obligation.condition}"
                    )
    return problems


# ------------------------------------------------------------------ projection


def _applicability(
    obligation: Obligation,
    data: Mapping[str, Any],
    diagnoses: Mapping[str, ControlDiagnosisView],
) -> ApplicabilityView:
    declared_by = [
        ProfileBasisView(path=p, value=resolve(data, p) or "") for p in obligation.condition_basis
    ]
    if obligation.condition == "always":
        return ApplicabilityView(
            condition="always", declared_by=declared_by, state="applies", checks=[]
        )
    check_id = _CONDITION_CHECK[obligation.condition]
    checks = []
    for link in obligation.links:
        if link.role not in _MEASURING:
            continue
        view = diagnoses[link.control_id]
        found = next((r for r in view.requirements if r.requirement_id == check_id), None)
        if view.diagnosis != "reconstructed":
            checks.append(
                ApplicabilityCheckView(
                    control_id=link.control_id,
                    requirement_id=check_id,
                    status=None,
                    reason_code=None,
                    explanation=f"no check detail: diagnosis {view.diagnosis}",
                )
            )
        elif found is None:
            checks.append(
                ApplicabilityCheckView(
                    control_id=link.control_id,
                    requirement_id=check_id,
                    status="not_evaluated",
                    reason_code=None,
                    explanation="the engine recorded no check of this condition",
                )
            )
        else:
            checks.append(
                ApplicabilityCheckView(
                    control_id=link.control_id,
                    requirement_id=check_id,
                    status=found.status,
                    reason_code=found.reason_code,
                    explanation=found.explanation,
                )
            )
    return ApplicabilityView(
        condition=obligation.condition,
        declared_by=declared_by,
        state=applicability_state([c.status for c in checks]),
        checks=checks,
    )


def applicability_state(statuses: Sequence[str | None]) -> ApplicabilityState:
    """Presentation rule over the engine's checks of the condition."""
    found = set(statuses)
    if not found or not found <= {"satisfied", "not_applicable"}:
        return "not_determined"
    if found == {"satisfied"}:
        return "applies"
    if found == {"not_applicable"}:
        return "not_applicable"
    return "mixed"


def _obligation(
    obligation: Obligation,
    evaluation: EvaluationResult,
    inputs: EvaluationInputs,
    data: Mapping[str, Any],
    diagnoses: Mapping[str, ControlDiagnosisView],
) -> ObligationView:
    stored = {c.control_id: c for c in evaluation.controls}
    coverage_set = set(inputs.snapshot.coverage_ids)
    links, observed, blocks = [], [], []
    evidence: list[str] = []
    coverage: list[str] = []
    for link in obligation.links:
        control = stored[link.control_id]
        links.append(
            ObligationControlLink(
                control_id=link.control_id,
                role=link.role,
                relation=link.relation,
                status=control.status,
                reason_code=control.reason_code,
                concluded=control.status in ("PASS", "FAIL"),
            )
        )
        for ref in control.evidence_refs:
            target = coverage if ref in coverage_set else evidence
            if ref not in target:
                target.append(ref)
        view = diagnoses[link.control_id]
        if view.diagnosis != "reconstructed":
            continue
        for requirement in view.requirements:
            if (
                link.role == "performance"
                and requirement.requirement_id == "comparison"
                and requirement.status in ("satisfied", "contradicted")
            ):
                observed.append(
                    ObservedComparisonView(
                        control_id=link.control_id,
                        status=requirement.status,
                        scope="decided" if requirement.determined_result else "observed_only",
                        left=requirement.left,
                        right=requirement.right,
                        delta=requirement.delta,
                    )
                )
            if requirement.determined_result and requirement.status == "undetermined":
                blocks.append(
                    BlockView(
                        control_id=link.control_id,
                        role=link.role,
                        requirement_id=requirement.requirement_id,
                        category=requirement.category,
                        reason_code=requirement.reason_code,
                        explanation=requirement.explanation,
                        next_step=requirement.next_step,
                    )
                )
    applicability = _applicability(obligation, data, diagnoses)
    limitations = []
    for item in observed:
        if item.scope == "observed_only":
            limitations.append(
                f"{item.control_id}: the comparison is of observed quantities only; the "
                "control did not conclude, so it is not a conclusion about this obligation."
            )
    if applicability.state == "not_applicable":
        limitations.append(
            "The declared condition does not hold in any control that measures this "
            "obligation: not applicable, which is not fulfilled."
        )
    if applicability.state == "mixed":
        limitations.append(
            "The declared condition holds in some measuring controls and not in others; each "
            "control keeps its stored result."
        )
    unexplained = sorted(
        {
            diagnoses[link.control_id].diagnosis
            for link in obligation.links
            if diagnoses[link.control_id].diagnosis != "reconstructed"
        }
    )
    for basis in unexplained:
        limitations.append(
            f"No check detail ({basis}): the stored control results stand as recorded; "
            "nothing is reconstructed for them."
        )
    return ObligationView(
        obligation_id=obligation.obligation_id,
        description=obligation.description,
        basis=[ProfileBasisView(path=p, value=resolve(data, p) or "") for p in obligation.basis],
        applicability=applicability,
        controls=links,
        observed=observed,
        blocks=blocks,
        evidence_refs=evidence,
        coverage_ids=coverage,
        not_derivable=list(obligation.not_derivable),
        limitations=limitations,
    )


def project_obligations(
    evaluation: EvaluationResult,
    inputs: EvaluationInputs,
    replayed: Evaluation,
    *,
    stored: bool = True,
    currency: CurrencyView | None = None,
) -> OperationObligationsView:
    """The obligations view of a stored evaluation. ``replayed`` is the one replay of its
    recorded engine on its own snapshot, shared by every control's diagnosis."""
    diagnoses = {
        c.control_id: diagnose(evaluation, inputs, replayed, c.control_id, stored=stored)
        for c in evaluation.controls
    }
    engine_ref = evaluation.versions.engine_ref
    consistent = replayed.result == evaluation
    basis: DiagnosisBasis
    if not consistent:
        basis = "reconstruction_mismatch"
    elif engine_ref not in DIAGNOSTIC_ENGINES:
        basis = "unavailable_for_engine"
    else:
        basis = "reconstructed"
    profile_ref = evaluation.versions.profile_ref
    entry = catalogue_entry(profile_ref)
    limitations = [LIMITATION_STORED_ONLY if stored else LIMITATION_OFFLINE]
    catalogue: CatalogueStatus
    obligations: list[ObligationView] = []
    if entry is None:
        catalogue = "unavailable_for_profile"
        limitations.append(
            f"{CATALOGUE_REF} has no entry for {profile_ref}: its obligations are not "
            "projected, and none is inferred from similar profiles or control names. The "
            "stored control results are listed as recorded."
        )
    elif problems := catalogue_problems(entry, inputs):
        catalogue = "mismatch_with_profile"
        limitations.append(
            f"The evaluation's profile does not match the {CATALOGUE_REF} entry for "
            f"{profile_ref} ({'; '.join(problems)}): no obligation is projected."
        )
    else:
        catalogue = "available"
        data = inputs.profile.model_dump(mode="json")
        obligations = [
            _obligation(o, evaluation, inputs, data, diagnoses) for o in entry.obligations
        ]
        limitations.append(
            f"Projected through the presentation catalogue {CATALOGUE_REF}, bound to "
            f"{profile_ref}: the grouping of controls into obligations is the catalogue's; "
            "the rules, results and checks are the profile's, the stored evaluation's and "
            "the engine's. It is not a register of obligations and has no state of its own."
        )
    if basis == "unavailable_for_engine":
        limitations.append(
            f"{engine_ref} is {engine_status(engine_ref)}: no check detail is given for it "
            "; the stored control results stand as recorded."
        )
    elif basis == "reconstruction_mismatch":
        limitations.append(
            "Replaying the recorded engine does not reproduce this evaluation: no check "
            "detail is attributed to it; the stored result and controls stand as recorded."
        )
    limitations.append(
        "UNKNOWN means not determined from the snapshot: it never states that a payment, "
        "registration or delivery did not happen, is owed or failed."
    )
    linked: dict[str, list[str]] = {}
    for obligation in obligations:
        for link in obligation.controls:
            ids = linked.setdefault(link.control_id, [])
            if obligation.obligation_id not in ids:
                ids.append(obligation.obligation_id)
    s = inputs.snapshot
    return OperationObligationsView(
        schema_version="1.0",
        operation_ref=evaluation.operation_ref,
        evaluation_id=evaluation.evaluation_id,
        result=evaluation.result,
        operation_state=evaluation.operation_state,
        snapshot=SnapshotView(
            snapshot_id=s.snapshot_id,
            valid_at=s.valid_at,
            known_at=s.known_at,
            evaluation_clock=s.evaluation_clock,
        ),
        currency=currency,
        versions=VersionsView(
            profile_ref=profile_ref,
            rules_ref=evaluation.versions.rules_ref,
            engine_ref=engine_ref,
            engine_status=engine_status(engine_ref),
        ),
        catalogue_ref=CATALOGUE_REF if entry is not None else None,
        catalogue=catalogue,
        diagnosis=basis,
        replay_consistent=consistent,
        obligations=obligations,
        controls=[
            ObligationControlView(
                control_id=c.control_id,
                mandatory=c.mandatory,
                status=c.status,
                reason_code=c.reason_code,
                concluded=c.status in ("PASS", "FAIL"),
                evidence_refs=list(c.evidence_refs),
                obligations=linked.get(c.control_id, []),
                diagnosis=diagnoses[c.control_id],
            )
            for c in evaluation.controls
        ],
        not_projected=(
            [NotProjectedView(description=d, reason=r) for d, r in entry.not_projected]
            if catalogue == "available" and entry is not None
            else []
        ),
        limitations=limitations,
    )
