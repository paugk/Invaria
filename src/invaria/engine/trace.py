"""The checks an engine actually ran for each control, recorded while it evaluates.

A diagnostic side channel of ``Evaluation``, never part of ``EvaluationResult``: the
canonical document, its id, hashes and bundles do not change. A step wraps existing engine
code without reordering or changing any decision: it records what that code concluded
(an ``Undecided`` it raised, a technical error, or the outcome the engine states) and the
references the engine cited meanwhile. A check the engine did not reach is simply absent;
the reader reports it as not evaluated, never runs it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal

from invaria.contracts.evaluation import ControlResult, ReasonCode
from invaria.contracts.quantity import Quantity
from invaria.engine.common import Undecided

CheckOutcome = Literal["satisfied", "contradicted", "undetermined", "not_applicable"]

_TRACED = "_invaria_traced"  # set on an exception once a step has recorded it
_OUTCOME_BY_STATUS: dict[str, CheckOutcome] = {
    "PASS": "satisfied",
    "FAIL": "contradicted",
    "UNKNOWN": "undetermined",
    "NOT_APPLICABLE": "not_applicable",
}


@dataclass(frozen=True)
class CheckRecord:
    """One check as the engine ran it. ``reason_code`` is the engine's own code for this
    check (None for a satisfied intermediate check); ``refs`` what the engine cited for it;
    ``set_aside`` what it weighed without counting."""

    requirement_id: str
    outcome: CheckOutcome
    reason_code: ReasonCode | None
    detail: str
    refs: tuple[str, ...] = ()
    set_aside: tuple[str, ...] = ()
    left: Quantity | None = None
    right: Quantity | None = None
    delta: Quantity | None = None
    # The fact whose evidence or coverage the check was reading when it concluded, where the
    # engine states it (a ``fact:`` check names it in its id).
    fact: str | None = None


@dataclass
class Step:
    """The handle of an open step. The engine states an explicit outcome when the check
    decides by returning (a FAIL, a not-applicable branch, a weighing); otherwise a step
    that ends normally is satisfied and one that raises is undetermined."""

    requirement_id: str
    cited: list[str]
    start: int
    outcome: CheckOutcome | None = None
    reason_code: ReasonCode | None = None
    detail: str = ""
    extra_refs: list[str] = field(default_factory=list)
    set_aside: list[str] = field(default_factory=list)
    left: Quantity | None = None
    right: Quantity | None = None
    delta: Quantity | None = None
    fact: str | None = None  # the fact being read; kept as the step concludes

    def cite(self, *refs: str) -> None:
        self.extra_refs.extend(refs)

    def aside(self, refs: Sequence[str]) -> None:
        self.set_aside.extend(refs)

    def compare(self, left: Quantity, right: Quantity, delta: Quantity | None = None) -> None:
        self.left, self.right, self.delta = left, right, delta

    def state(
        self, outcome: CheckOutcome, reason: ReasonCode | None = None, detail: str = ""
    ) -> None:
        self.outcome, self.reason_code, self.detail = outcome, reason, detail

    def result(self, control: ControlResult) -> ControlResult:
        """Record a control result the engine built in this step; returns it unchanged."""
        self.state(_OUTCOME_BY_STATUS[control.status], control.reason_code, control.reason)
        self.cite(*control.evidence_refs)
        if control.delta is not None:
            self.delta = control.delta
        return control

    def _refs(self, raised: Sequence[str] = ()) -> tuple[str, ...]:
        return tuple(dict.fromkeys([*self.cited[self.start :], *self.extra_refs, *raised]))


class ControlTrace:
    """The ordered checks of one control. ``cited`` is the list the engine already uses to
    collect the control's evidence references; a step records what was added to it."""

    def __init__(self) -> None:
        self.records: list[CheckRecord] = []

    def record(
        self,
        requirement_id: str,
        outcome: CheckOutcome,
        reason: ReasonCode | None,
        detail: str,
        refs: Sequence[str] = (),
        set_aside: Sequence[str] = (),
        fact: str | None = None,
    ) -> None:
        self.records.append(
            CheckRecord(
                requirement_id,
                outcome,
                reason,
                detail,
                tuple(dict.fromkeys(refs)),
                tuple(dict.fromkeys(set_aside)),
                fact=fact,
            )
        )

    @contextmanager
    def step(
        self, requirement_id: str, cited: list[str] | None = None, *, quiet: bool = False
    ) -> Iterator[Step]:
        """Record the check run inside the block. ``quiet``: record only a failure (for the
        inputs a later check uses, so that an error there is still attributed to it)."""
        cited = [] if cited is None else cited
        handle = Step(requirement_id, cited, len(cited))
        try:
            yield handle
        except Undecided as undecided:
            if not getattr(undecided, _TRACED, False):  # an inner step already recorded it
                setattr(undecided, _TRACED, True)
                self._append(
                    handle, "undetermined", undecided.reason, undecided.detail, undecided.refs
                )
            raise
        except Exception as error:  # the engine turns it into EVALUATION_ERROR
            if not getattr(error, _TRACED, False):
                setattr(error, _TRACED, True)
                self._append(
                    handle, "undetermined", "EVALUATION_ERROR", f"{type(error).__name__}: {error}"
                )
            raise
        else:
            if not quiet:
                self._append(
                    handle, handle.outcome or "satisfied", handle.reason_code, handle.detail
                )

    def _append(
        self,
        handle: Step,
        outcome: CheckOutcome,
        reason: ReasonCode | None,
        detail: str,
        raised: Sequence[str] = (),
    ) -> None:
        self.records.append(
            CheckRecord(
                handle.requirement_id,
                outcome,
                reason,
                detail,
                handle._refs(raised),
                tuple(dict.fromkeys(handle.set_aside)),
                handle.left,
                handle.right,
                handle.delta,
                handle.fact,
            )
        )


class Tracer:
    """One trace per control of an evaluation."""

    def __init__(self) -> None:
        self.controls: dict[str, ControlTrace] = {}

    def __getitem__(self, control_id: str) -> ControlTrace:
        return self.controls.setdefault(control_id, ControlTrace())

    def frozen(self) -> dict[str, tuple[CheckRecord, ...]]:
        return {cid: tuple(trace.records) for cid, trace in self.controls.items()}


# ----------------------------------------------------------------- requirement catalogue
# What each check verifies, and when it applies, in the engines' own terms. The plan of a
# control lists the checks it may run, in the order the engine runs them: a check absent
# from the trace was not reached (not evaluated), never failed.

_SUBSCRIPTION: dict[str, tuple[str, str]] = {
    "admission": (
        "Snapshot, profile, engine and evidence provenance are admitted for evaluation",
        "always",
    ),
    "operands": (
        "The compared quantities can be computed in one unit and scale, without rounding",
        "once every required fact is admitted",
    ),
    "comparison": (
        "The observed quantities are equal; on its own this does not affirm that the "
        "observed set is complete",
        "once the operands are computed",
    ),
    "quarantine": (
        "Quarantined chain records bearing on the operation cannot change the result",
        "token controls, when a coverage certificate quarantines records bearing on the operation",
    ),
    "attribution": (
        "Movements whose destination no IdentityLink attributes to the account cannot change "
        "the result",
        "token controls, when such a movement bears on the operation",
    ),
    "completeness": (
        "The chain coverage shows that no further delivery reached the account",
        "token controls, when the profile declares absence_needs_chain_scope; needed for an "
        "equal or short comparison",
    ),
    "chain_effects": (
        "On-chain effects bearing on the operation cannot change the result",
        "token controls, when such an effect is in the snapshot",
    ),
}

_REDEMPTION: dict[str, tuple[str, str]] = {
    "admission": _SUBSCRIPTION["admission"],
    "applicability": (
        "The obligation is not extinguished by a valid cancellation",
        "controls that apply unless the request is cancelled",
    ),
    "request_support": (
        "The redemption request is still supported by its source (not withdrawn)",
        "controls other than the cancellation ones",
    ),
    "acceptance_time": (
        "The acceptance time is credited: the TA record is effective at accepted_at, at the "
        "declared precision",
        "deadline controls",
    ),
    "declared_due": (
        "The declared payment due time equals the one the profile computes",
        "redemption.declared_due_consistency",
    ),
    "position": (
        "The available position immediately before acceptance is known from the declared "
        "TA journal, with coverage of its changes",
        "redemption.units_within_position",
    ),
    "expected_amount": (
        "The expected payment (units x approved price) is exact at the cash scale",
        "redemption.cash_vs_expected",
    ),
    "recipient": (
        "The linked payment credited the requesting account",
        "redemption.cash_vs_expected",
    ),
    "deadline": (
        "The requesting account was paid by the due time, or its absence is shown with bank "
        "coverage once the due time passed",
        "redemption.payment_deadline",
    ),
    "account": (
        "The registry entry is for the requesting account",
        "redemption.ta_units_vs_request",
    ),
    "comparison": (
        "The observed quantities compare as the control requires (equal, or within the "
        "position); on its own this does not affirm that the observed set is complete",
        "once both operands are admitted",
    ),
    "quarantine": (
        "Quarantined chain records bearing on the request cannot change the burn comparison",
        "redemption.burn_vs_request, under a declared quarantine policy",
    ),
    "chain_effects": (
        "On-chain effects or unattributed movements bearing on the request cannot change the "
        "result",
        "burn and no-settlement controls, when such an effect is in the snapshot",
    ),
    "completeness": (
        "The chain coverage shows that the burns observed are all the burns of the request",
        "redemption.burn_vs_request, when the profile declares completeness_needs_coverage; "
        "needed for an equal or short comparison",
    ),
    "reactivation": (
        "The reactivation records of the request resolve without conflict or withdrawal",
        "redemption.cancellation_valid",
    ),
    "retractions": (
        "Each retraction of a cancellation resolves to a revision it may withdraw",
        "redemption.cancellation_valid, when a retraction is linked to the request",
    ),
    "cancellation": (
        "The cancellation is valid under the profile (authority, timing, account), or its "
        "absence is shown with TA coverage",
        "redemption.cancellation_valid",
    ),
    "cancellation_in_force": (
        "A valid cancellation is in force",
        "redemption.no_settlement_after_cancellation",
    ),
    "settlement": (
        "No payment or burn linked to the cancelled request is effective within the cut",
        "redemption.no_settlement_after_cancellation, with a cancellation in force",
    ),
    "absence_coverage": (
        "Bank and chain coverage from acceptance to valid_at shows the absence of settlement",
        "redemption.no_settlement_after_cancellation, when no settlement is shown",
    ),
}

_REDEMPTION_PLAN: dict[str, tuple[str, ...]] = {
    "redemption.declared_due_consistency": ("acceptance_time", "declared_due"),
    "redemption.price_vs_approved": ("fact:price_approved", "comparison"),
    "redemption.units_within_position": ("position", "comparison"),
    "redemption.cash_vs_expected": (
        "fact:price_approved",
        "expected_amount",
        "fact:cash_settled",
        "recipient",
        "comparison",
    ),
    "redemption.payment_deadline": ("acceptance_time", "deadline"),
    "redemption.ta_units_vs_request": ("fact:units_registered", "account", "comparison"),
    "redemption.burn_vs_request": (
        "fact:token_movement",
        "comparison",
        "quarantine",
        "chain_effects",
        "completeness",
    ),
}
_CANCELLATION_PLAN: dict[str, tuple[str, ...]] = {
    "redemption.cancellation_valid": (
        "fact:redemption_accepted",
        "reactivation",
        "retractions",
        "cancellation",
    ),
    "redemption.no_settlement_after_cancellation": (
        "cancellation_in_force",
        "settlement",
        "chain_effects",
        "absence_coverage",
    ),
}


def control_plan(
    operation_type: str, control_id: str, requires: Sequence[str], applies: str | None
) -> tuple[str, ...]:
    """The checks a control may run, in the engine's order."""
    if operation_type == "redemption":
        if control_id in _CANCELLATION_PLAN:
            return ("admission", *_CANCELLATION_PLAN[control_id])
        head = ("admission", "applicability") if applies == "unless_cancelled" else ("admission",)
        return (
            *head,
            "request_support",
            "fact:redemption_accepted",
            *_REDEMPTION_PLAN.get(control_id, ()),
        )
    tail = (
        ("quarantine", "attribution", "completeness", "chain_effects")
        if "token_movement" in requires
        else ()
    )
    return ("admission", *(f"fact:{f}" for f in requires), "operands", "comparison", *tail)


def describe(operation_type: str, requirement_id: str) -> tuple[str, str]:
    """(description, applies_when) of a check."""
    if requirement_id.startswith("fact:"):
        fact = requirement_id.removeprefix("fact:")
        if operation_type == "redemption":
            return (
                f"Positive evidence of {fact} from its authoritative source, linked and "
                "effective within the cut (no continuous coverage needed)",
                "when the control uses this fact",
            )
        return (
            f"Authoritative {fact} evidence linked to the operation is admitted, with the "
            "coverage the profile requires for its source",
            "every control that requires this fact",
        )
    catalogue = _REDEMPTION if operation_type == "redemption" else _SUBSCRIPTION
    return catalogue[requirement_id]
