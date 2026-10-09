"""Pure evaluation of a redemption snapshot (``fund-redemption-synthetic@1.3.0``).

No clock, network or filesystem: the payment deadline is judged with the snapshot's
explicit evaluation clock, and a fact counts only if effective at both that clock and the
economic cut (valid_at). Positive evidence needs provenance, authority and revision
resolution; demonstrating absence or rebuilding a position needs interval coverage.

A cancellation is a business fact (TA), distinct from retracting evidence; a valid one
extinguishes the pending settlement obligations (NOT_APPLICABLE with reason) but never a
breach already demonstrated. The lifecycle is reported apart from the result.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from invaria.contracts.coverage import CoverageCertificate, QuarantinedRecord, UncoveredEffect
from invaria.contracts.evaluation import (
    ControlResult,
    EvaluationResult,
    EvaluationVersions,
    OperationState,
    ReasonCode,
    aggregate,
)
from invaria.contracts.observation import (
    CashPayload,
    FactType,
    Observation,
    PositionChangePayload,
    PositionPayload,
    PricePayload,
    RedemptionCancellationPayload,
    RedemptionRequestPayload,
    TokenMovementPayload,
    UnitsPayload,
    is_muxed,
    movement_addresses,
    movement_sender,
)
from invaria.contracts.profile import (
    RedemptionControlSpec,
    RedemptionCoverageRequirement,
    RedemptionProfile,
)
from invaria.contracts.quantity import Quantity
from invaria.engine.common import (
    LEVEL_RANK,
    Evaluation,
    EvaluationInputs,
    FactView,
    Operands,
    RecordSupport,
    Undecided,
    absence_scope,
    active_coverage,
    addresses_by_network,
    bears_by_address,
    by_address,
    by_address_muxed,
    chain_effects_bearing,
    chain_evidence,
    control_result,
    covers_absence,
    engine_problem,
    evaluation_id,
    evidence_mapping_refs,
    institutional_view,
    provenance_problem,
    quarantine_affecting,
    record_support,
    resolve_records,
    snapshot_problem,
    strict_absence_scope,
)
from invaria.engine.trace import ControlTrace, Step, Tracer
from invaria.engine.versions import (
    DIAGNOSTIC_ENGINES,
    REDEMPTION_ENGINE_0_4_0,
    REDEMPTION_ENGINE_0_5_0,
    REDEMPTION_ENGINE_0_6_0,
    REDEMPTION_ENGINE_0_7_0,
    REDEMPTION_ENGINE_0_8_0,
    REDEMPTION_ENGINE_0_9_0,
    REDEMPTION_ENGINE_0_10_0,
    profile_problem,
)
from invaria.engine.versions import REDEMPTION_ENGINE_REF as REDEMPTION_ENGINE_REF


@dataclass(frozen=True)
class _Semantics:
    """What each engine label this module runs does.

    ``inv013``: chain effects bearing on the request and scoped quarantine (0.5.0 on).
    ``unresolved_bears``: an effect naming an unidentified participant bears (0.6.0 on).
    ``adr014``: under a profile declaring a quarantine policy, the chain quarantine leaves
    UNKNOWN the burn comparison and the settlement conclusions it could change; chain
    certificates stay active until explicitly superseded within their scope; records are
    contrasted with the snapshot (0.7.0 on).
    ``muxed_identity``: a movement naming a muxed sub-account is never counted as a burn
    (no redemption profile declares how one is attributed) and weighs as the quarantined
    record its earlier mapping made of it (0.8.0 on). Without it such a movement,
    which those engines never received, makes the burn view unsupported.
    ``closure`` (0.9.0 on): a movement set aside is weighed
    by what it could change (an unlinked one beside an execution linked to the
    request can never be a burn of it; under a profile that declares its quarantine
    policy a linked burn from an address without an approved link is set aside and
    weighed instead of blocking every comparison); the evaluation states the mappings of
    its evidence; and an absence of settlement needs a chain certificate that does not
    declare a claimable balance clawback or claim out of its sight (decision of
    2026-10-08).
    ``scoped_absence`` (0.10.0 on): under a profile that declares
    ``absence_needs_chain_scope``, an absence of settlement needs chain certificates whose
    declared scope is sufficient (``scope_shortfall``); a certificate without one, or one
    that does not declare what it leaves out of sight, cannot show it.
    ``completeness`` (0.11.0 on): under a profile that also declares
    ``completeness_needs_coverage``, an equal or short burn comparison needs coverage of
    [accepted_at, valid_at] showing that the burns observed are all the burns, and every
    chain scope must be coherent with its route and role and belong to the representation
    used.
    """

    inv013: bool
    unresolved_bears: bool
    adr014: bool = False
    muxed_identity: bool = False
    closure: bool = False
    scoped_absence: bool = False
    completeness: bool = False


# The current engine, and the retired ones kept for replay only (compatibility).
_SEMANTICS = {
    REDEMPTION_ENGINE_REF: _Semantics(
        inv013=True,
        unresolved_bears=True,
        adr014=True,
        muxed_identity=True,
        closure=True,
        scoped_absence=True,
        completeness=True,
    ),
    REDEMPTION_ENGINE_0_10_0: _Semantics(
        inv013=True,
        unresolved_bears=True,
        adr014=True,
        muxed_identity=True,
        closure=True,
        scoped_absence=True,
    ),
    REDEMPTION_ENGINE_0_9_0: _Semantics(
        inv013=True, unresolved_bears=True, adr014=True, muxed_identity=True, closure=True
    ),
    REDEMPTION_ENGINE_0_8_0: _Semantics(
        inv013=True, unresolved_bears=True, adr014=True, muxed_identity=True
    ),
    REDEMPTION_ENGINE_0_7_0: _Semantics(inv013=True, unresolved_bears=True, adr014=True),
    REDEMPTION_ENGINE_0_6_0: _Semantics(inv013=True, unresolved_bears=True),
    REDEMPTION_ENGINE_0_5_0: _Semantics(inv013=True, unresolved_bears=False),
    REDEMPTION_ENGINE_0_4_0: _Semantics(inv013=False, unresolved_bears=False),
}
FACT_ORDER: tuple[FactType, ...] = (
    "redemption_accepted",
    "price_approved",
    "position_held",
    "position_changed",
    "redemption_cancelled",
    "redemption_reactivated",
    "cash_settled",
    "units_registered",
    "token_movement",
)
SETTLEMENT_FACTS: tuple[FactType, ...] = ("cash_settled", "token_movement")
# Effects of the asset that could be the retirement or the settlement of a request without
# being a burn the profile admits (decision of 2026-10-08): an absence of
# settlement is shown only by a chain certificate that does not declare them out of sight.
SETTLEMENT_EFFECTS: tuple[UncoveredEffect, ...] = (
    "claimable_balance_clawback",
    "claimable_balance_claim",
)
VALIDITY = "redemption.cancellation_valid"
NO_SETTLEMENT = "redemption.no_settlement_after_cancellation"
CancellationState = Literal["none", "cancelled", "undecided"]
_REASON_BY_STATUS: dict[str, ReasonCode] = {
    "conflict": "SOURCE_CONFLICT",
    "withdrawn": "LOSS_OF_SUPPORT",
    "ambiguous": "AMBIGUOUS_MATCH",
    "unsupported": "UNSUPPORTED_CAPABILITY",
    "early": "BURN_BEFORE_ACCEPTANCE_UNSUPPORTED",
}


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _declares_policy(profile: RedemptionProfile) -> bool:
    return any(r.quarantine_policy is not None for r in profile.coverage_requirements)


def _assumptions(
    profile: RedemptionProfile,
    inv013: bool = True,
    unresolved_bears: bool = True,
    adr014: bool = False,
    muxed_identity: bool = False,
    closure: bool = False,
    scoped_absence: bool = False,
    completeness: bool = False,
) -> list[str]:
    hours = profile.payment_deadline.hours
    extra = [
        "Chain effects are never evidence of payment, delivery or retirement; one linked to "
        "the request or involving an address approved for its account makes an equal or "
        "short burn comparison, or an absence of settlement, UNKNOWN; proven settlement and "
        "an excess of linked burns stay a FAIL.",
        "Quarantined records count only when the profile scopes them to records bearing on "
        "the request; then a record counts if its parties are unknown or include such an "
        "address.",
    ]
    base = [
        "Evaluation uses only the snapshot members; nothing is fetched or recomputed from sources.",
        "Per source record the highest revision wins; identical redeliveries count once.",
        "Positive evidence needs provenance, authority, effective time and revision "
        "resolution, not continuous coverage. Demonstrating absence or rebuilding a position "
        "needs the most recently recorded certificate for the interval: half-open [start, "
        "end), scoped to the account (and currency for cash), without gaps or filters.",
        "A fact counts only if it is a snapshot member effective at both the evaluation clock "
        "and the economic cut (valid_at).",
        "Comparisons are exact in atoms; no tolerance, fees or rounding; an expected payment "
        "that is not exact at the cash scale is UNKNOWN.",
        f"The payment is due {hours} exact hours after accepted_at (UTC, no business "
        "calendar), judged with the snapshot's evaluation clock; a declared due time that "
        "disagrees is a data conflict, and the computed one is used.",
        "The available position is the TA's, immediately before acceptance by the "
        "source-assigned sequence of one declared TA journal, reconstructed from journal "
        "changes under coverage; no age window.",
        "A linked payment to another account is a wrong recipient when bank and TA share an "
        "account namespace; an unlinked payment is ignored unless it carries the settlement "
        "reference the TA instructed.",
        "Retirement is a burn to the issuer after acceptance. Price, payment, burn and "
        "registry are linked by explicit references only, never by amount, memo or time.",
        "A valid cancellation extinguishes pending settlement obligations but not a breach "
        "already demonstrated; settlement despite it is a BREAK for review and nothing is "
        "reversed automatically.",
    ]
    unresolved = [
        "A chain effect naming a participant the evidence does not identify (e.g. a "
        "claimable balance whose history could not be read) cannot be shown foreign: "
        "it bears on the request like one involving an approved address."
    ]
    declared = _declares_policy(profile)
    integrity = [
        "For the chain coverage whose quarantine the profile scopes, a certificate is not "
        "replaced for being older: it stays active until a later certificate of the same "
        "source, network, target, account and asset, with a route including its route, a "
        "ledger range containing its range, no gaps and no lower level, explicitly supersedes "
        "it. The quarantines of the active certificates add up; "
        + (
            "an absence is met by an active certificate."
            if declared
            else "an absence is judged on the most recent certificate, as the profile promised."
        ),
        "The parties of a quarantined record are contrasted with the snapshot's observations "
        "of its ledger operation: a relevant or unresolved party the record omits makes it "
        "bear on the request whatever its addresses say.",
    ]
    policy = (
        [
            "The profile declares its quarantine policy: a quarantined record never credits "
            "a retirement or a settlement, and it makes UNKNOWN only what resolving it could "
            "change. A record bearing on the request on the ledger operation of a counted "
            "burn, or on one the snapshot does not support, leaves the burn comparison "
            "UNKNOWN and a settlement proven only by that burn UNKNOWN; any other one "
            "(an identified clawback included, since it could be the real retirement) leaves "
            "an equal or short burn comparison UNKNOWN, an excess of burns a FAIL; any one "
            "leaves the absence of settlement UNKNOWN. A record's ledger operation is relied "
            "on only when the snapshot's observations of that operation support it."
        ]
        if _declares_policy(profile)
        else [
            "The profile declares no quarantine policy: the chain quarantine is read only "
            "where absence is argued (no settlement after a cancellation); an equal burn is "
            "positive evidence and does not consult it, as in the profile's promise."
        ]
    )
    if adr014:
        base = [
            a.replace(
                "needs the most recently recorded certificate for the interval",
                "needs the most recently recorded certificate for the interval (for the chain "
                "coverage whose quarantine the profile scopes, see below)",
            )
            for a in base
        ]
    muxed = [
        "No redemption profile declares how a muxed sub-account is attributed: a movement "
        "naming one is never counted as a burn, whatever its base account (a burn to a "
        "sub-account of the issuer included). Bearing on the request (an ExecutionLink, an "
        "approved address or a sub-account of an approved base account, or an unknown "
        "account; under a profile that does not scope the on-chain quarantine, any muxed "
        "movement of the source), it makes an equal or short burn comparison and an absence "
        "of settlement UNKNOWN, as an unresolved effect does; an excess of burns and proven "
        "settlement stay a FAIL. Under the profiles that predate this rule an equal burn "
        "becomes UNKNOWN where the quarantined record of the earlier mapping did not consult "
        "it: the muxed movement could be a further retirement.",
        "A transaction memo is context of the whole transaction: it never links an "
        "observation to a request, and a memo id is never read as a muxed sub-account id.",
    ]
    if closure:
        muxed[0] = (
            "No redemption profile declares how a muxed sub-account is attributed: a movement "
            "naming one is never counted as a burn, whatever its base account (a burn to a "
            "sub-account of the issuer included). A movement set aside weighs only by what it "
            "could change: linked to the request by an ExecutionLink, it is a further "
            "execution and makes an equal or short burn comparison and an absence of "
            "settlement UNKNOWN; without a link it can never be a burn of the request and "
            "bears only while no execution is linked to it (it could be the burn whose link "
            "is missing) and it names an approved address or a sub-account of an approved "
            "base account, or always when the account is unknown (under a profile that does "
            "not scope the on-chain quarantine, any muxed movement of the source). Two readings "
            "of one execution share its record key: identical, they count once; different, "
            "they are a source conflict. Under a profile that declares its quarantine "
            "policy, a linked burn from an address without an approved link is weighed the "
            "same way. An excess of burns and proven settlement stay a FAIL."
        )
    closing = [
        "Only evidence produced by a mapping the profile admits for its source is evaluated, "
        "and the evaluation states those mappings (evidence_mapping_refs); evidence produced "
        "by another mapping is refused, never relabelled nor read with another mapping.",
        "The absence of settlement is about any retirement or settlement, not only about a "
        "burn the profile admits: a chain certificate that declares a claimable balance "
        "clawback or claim out of its sight (chain_scope.not_covered) cannot show it, since "
        "such an effect could be that retirement or settlement; the absence is then UNKNOWN "
        "(INSUFFICIENT_COVERAGE). Such an effect is never evidence of compliance either.",
    ]
    return (
        base
        + (extra if inv013 else [])
        + (unresolved if inv013 and unresolved_bears else [])
        + (integrity + policy if adr014 else [])
        + (muxed if muxed_identity else [])
        + (closing if closure else [])
        + (
            [SCOPED_ABSENCE]
            if scoped_absence
            and any(r.absence_needs_chain_scope for r in profile.coverage_requirements)
            else []
        )
        + (
            [COMPLETENESS]
            if completeness
            and any(r.completeness_needs_coverage for r in profile.coverage_requirements)
            else []
        )
    )


COMPLETENESS = (
    "The profile declares that affirming the observed chain records complete needs coverage "
    "(INV-015 M3, B1, B3; ADR-008 I-B as updated): observing a burn needs no complete "
    "coverage, affirming that the burns observed are all the burns does. An equal or short "
    "burn comparison is concluded only with a chain certificate covering [accepted_at, "
    "valid_at] whose scope is coherent with what its route and role observe and belongs to "
    "the representation of the burns, reading the addresses approved on its network; "
    "otherwise it is UNKNOWN (INSUFFICIENT_COVERAGE). An excess of burns and proven settlement "
    "stay a FAIL. With coherent chain certificates the absence of settlement is never shown: "
    "every route and role leaves the claim of a claimable balance by a third party out of sight."
)


SCOPED_ABSENCE = (
    "The profile declares that an absence on the chain source needs a sufficient chain scope "
    "(INV-015): the absence of settlement is shown only by chain certificates that declare "
    "their scope (network and asset of a representation of the instrument, a route that "
    "includes the SAC events), what they leave out of sight (none of a claimable balance "
    "clawback or claim), and that read every address approved for the request's account (the "
    "issuer's certificate is not enough); otherwise it is UNKNOWN "
    "(INSUFFICIENT_COVERAGE). Missing exclusions are never read as complete coverage; proven "
    "settlement stays a FAIL."
)


class _Redemption:
    def __init__(
        self,
        inputs: EvaluationInputs,
        *,
        inv013: bool = True,
        unresolved_bears: bool = True,
        adr014: bool = True,
        muxed_identity: bool = True,
        closure: bool = True,
        scoped_absence: bool = True,
        completeness: bool = True,
    ) -> None:
        """The flags select the semantics of an engine label (``_Semantics``); retired labels
        run as compatibility implementations, not as their historical code."""
        self.muxed_identity = muxed_identity
        self.closure = closure
        self.scoped_absence = scoped_absence
        self.completeness = completeness
        # Muxed movements set aside (never burns) that bear on the request (0.8.0).
        self.muxed_bearing: tuple[str, ...] = ()
        self.unresolved_bears = unresolved_bears
        self.adr014 = adr014
        assert isinstance(inputs.profile, RedemptionProfile)
        self.inv013 = inv013
        self.profile: RedemptionProfile = inputs.profile
        self.snapshot = inputs.snapshot
        self.clock = inputs.snapshot.evaluation_clock
        # A fact counts only if it is a snapshot member effective at both the evaluation
        # clock and the economic cut (valid_at).
        self.cut = min(self.clock, inputs.snapshot.valid_at)
        self.members = [inputs.observations[i] for i in self.snapshot.observation_ids]
        self.by_id = {o.observation_id: o for o in self.members}
        self.certificates = [inputs.coverage[i] for i in self.snapshot.coverage_ids]
        self.links = [inputs.identity_links[i] for i in self.snapshot.identity_link_ids]
        self.views: dict[FactType, FactView] = {}
        self.tracer = Tracer()  # the checks each control ran

    # ------------------------------------------------------------- evidence

    def _authoritative(self, fact_type: FactType) -> list[Observation]:
        """Authoritative members effective at the clock and the economic cut."""
        source = self.profile.authority_for(fact_type)
        return [
            o
            for o in self.members
            if o.fact_type == fact_type
            and o.source.source_id == source
            and o.instrument_id == self.profile.instrument.instrument_id
            and o.valid_time <= self.cut
        ]

    def view(self, fact_type: FactType) -> FactView:
        if fact_type not in self.views:
            self.views[fact_type] = self._build_view(fact_type)
        return self.views[fact_type]

    def _build_view(self, fact_type: FactType) -> FactView:
        candidates = self._authoritative(fact_type)
        if fact_type == "token_movement":
            return self._burn_view(candidates)
        if fact_type == "price_approved":
            request = self._request_or_none()
            if request is None:
                return FactView(fact_type, "absent", (), (), "no request names a price_ref")
            named = [
                o
                for o in candidates
                if (
                    isinstance(o.payload, PricePayload) and o.payload.price_ref == request.price_ref
                )
                or (o.kind == "retraction" and self._retracts_price(o, candidates, request))
            ]
            return institutional_view(fact_type, named, "multiple_prices")
        if fact_type == "position_changed":
            return self._changes_view(candidates)
        linked = [o for o in candidates if o.operation_ref == self.snapshot.operation_ref]
        single = {
            "cash_settled": "multiple_cash_payments",
            "units_registered": "partial_execution",
        }.get(fact_type, "multiple_records")
        view = institutional_view(fact_type, linked, single)
        if fact_type != "cash_settled" or view.status != "absent":
            return view
        request = self._request_or_none()
        settlement_ref = request.settlement_ref if request else None
        compatible = self.profile.correlation.settlement_ref_namespace == "bank_payment_ref"
        if settlement_ref and compatible:
            # Only a relevant candidate (the reference the TA instructed for this request)
            # makes a payment without our link ambiguous; sharing the account is not enough
            # (I-I, D-9). A payment linked to another operation that carries our
            # reference is a collision and stays visible; nothing is associated.
            candidates_by_ref = [
                o.observation_id
                for o in candidates
                if o.operation_ref != self.snapshot.operation_ref
                and isinstance(o.payload, CashPayload)
                and o.payload.payment_ref == settlement_ref
            ]
            if candidates_by_ref:
                return FactView(
                    fact_type,
                    "ambiguous",
                    (),
                    tuple(candidates_by_ref),
                    f"{len(candidates_by_ref)} payment(s) carry the request's settlement_ref "
                    f"{settlement_ref} without an approved link to the request",
                )
        return view

    def _changes_view(self, candidates: Sequence[Observation]) -> FactView:
        """Journal changes of the request's account, whatever operation they belong to."""
        request = self._request_or_none()
        account = request.account_ref if request else None
        mine = [
            o
            for o in candidates
            if o.kind == "retraction"
            or (isinstance(o.payload, PositionChangePayload) and o.payload.account_ref == account)
        ]
        resolved = resolve_records(mine)
        conflicts = [
            o.observation_id for state, obs in resolved if state == "conflict" for o in obs
        ]
        if conflicts:
            return FactView(
                "position_changed", "conflict", (), tuple(conflicts), "journal entry conflict"
            )
        asserted = tuple(
            o
            for state, obs in resolved
            if state == "asserted"
            for o in obs
            if isinstance(o.payload, PositionChangePayload)
        )
        if not asserted:
            return FactView("position_changed", "absent", (), (), "no journal change")
        return FactView(
            "position_changed", "asserted", asserted, tuple(o.observation_id for o in asserted), ""
        )

    @staticmethod
    def _retracts_price(
        retraction: Observation,
        candidates: Sequence[Observation],
        request: RedemptionRequestPayload,
    ) -> bool:
        """A price retraction counts when it withdraws a revision of the named price."""
        return any(
            o.observation_id == retraction.supersedes
            and isinstance(o.payload, PricePayload)
            and o.payload.price_ref == request.price_ref
            for o in candidates
        )

    def _request_or_none(self) -> RedemptionRequestPayload | None:
        view = self.view("redemption_accepted")
        if view.status != "asserted":
            return None
        payload = view.observations[0].payload
        return payload if isinstance(payload, RedemptionRequestPayload) else None

    def _linked(self, account: str | None, address: str, network: str, at: datetime) -> bool:
        return account is not None and any(
            link.account_ref == account
            and link.address == address
            and link.network == network
            and link.valid_from <= at
            and (link.valid_to is None or at < link.valid_to)
            for link in self.links
        )

    def _burn_view(self, candidates: Sequence[Observation]) -> FactView:
        operation = self.snapshot.operation_ref
        representations = {r.representation_id: r for r in self.profile.representations}
        issuers = {r.issuer for r in representations.values()}
        request = self._request_or_none()
        account = request.account_ref if request else None
        relevant = [o for o in candidates if o.representation_id in representations]
        resolved = resolve_records(relevant)
        conflicts = [o for state, obs in resolved if state == "conflict" for o in obs]
        if conflicts:
            return FactView(
                "token_movement",
                "conflict",
                (),
                tuple(o.observation_id for o in conflicts),
                "same chain record delivered with different content",
            )
        asserted = [o for state, obs in resolved if state == "asserted" for o in obs]
        successful = [
            (o, o.payload)
            for o in asserted
            if isinstance(o.payload, TokenMovementPayload) and o.payload.chain.tx_successful
        ]
        muxed = [(o, p) for o, p in successful if is_muxed(p)]
        if muxed and not self.muxed_identity:
            return FactView(
                "token_movement",
                "unsupported",
                (),
                tuple(o.observation_id for o, _ in muxed),
                "unsupported: muxed_account (this engine never received a muxed movement; "
                "its adapter quarantined them)",
            )
        aside = list(muxed)
        if self.closure and _declares_policy(self.profile):
            # 0.9.0: a linked burn from an address without an approved link is set aside and
            # weighed like an unattributed muxed movement.
            aside += [
                (o, p)
                for o, p in successful
                if not is_muxed(p)
                and o.operation_ref == operation
                and p.to_address == representations[o.representation_id or ""].issuer
                and not self._linked(account, p.from_address, p.chain.network, o.valid_time)
            ]
        kept = {o.observation_id for o, _ in aside}
        successful = [(o, p) for o, p in successful if o.observation_id not in kept]
        if aside:
            # Never a burn: set aside and weighed by what it could change.
            self.muxed_bearing = self._muxed_bearing(aside, account, successful)
        explicit = [(o, p) for o, p in successful if o.operation_ref == operation]
        linked_aside = tuple(o.observation_id for o, _ in aside if o.operation_ref == operation)
        if not explicit and self.closure and linked_aside:
            return FactView(
                "token_movement",
                "ambiguous",
                (),
                linked_aside,
                "the execution(s) linked to the request name a muxed sub-account or an address "
                "without an approved link to the account; no redemption profile attributes "
                "them, so they are never a burn",
            )
        if not explicit:
            unlinked = [
                o.observation_id
                for o, p in successful
                if o.operation_ref is None
                and p.to_address in issuers
                and self._linked(account, p.from_address, p.chain.network, o.valid_time)
            ]
            if unlinked:
                return FactView(
                    "token_movement",
                    "ambiguous",
                    (),
                    tuple(unlinked),
                    "movements from the investor address lack an execution link",
                )
            linked_muxed = tuple(o.observation_id for o, _ in muxed if o.operation_ref == operation)
            if linked_muxed:
                return FactView(
                    "token_movement",
                    "ambiguous",
                    (),
                    linked_muxed,
                    "the execution linked to the request names a muxed sub-account; no "
                    "redemption profile declares how one is attributed, so it is never a burn",
                )
            return FactView(
                "token_movement", "absent", (), (), "no successful burn linked to the request"
            )
        ids = tuple(o.observation_id for o, _ in explicit)
        for o, p in explicit:
            if p.to_address != representations[o.representation_id or ""].issuer:
                return FactView(
                    "token_movement", "unsupported", (), ids, "unsupported: transfer_retirement"
                )
            if not self._linked(account, p.from_address, p.chain.network, o.valid_time):
                return FactView(
                    "token_movement",
                    "ambiguous",
                    (),
                    ids,
                    "burn source address has no approved identity link to the account",
                )
        if request is not None and any(o.valid_time < request.accepted_at for o, _ in explicit):
            return FactView(
                "token_movement",
                "early",
                tuple(o for o, _ in explicit),
                ids,
                f"burn effective before accepted_at {_iso(request.accepted_at)}; the link is "
                "kept but the burn does not fulfil the retirement",
            )
        return FactView("token_movement", "asserted", tuple(o for o, _ in explicit), ids, "")

    def _muxed_bearing(
        self,
        muxed: Sequence[tuple[Observation, TokenMovementPayload]],
        account: str | None,
        counted: Sequence[tuple[Observation, TokenMovementPayload]] = (),
    ) -> tuple[str, ...]:
        """Movements set aside that bear on the request: named by an ExecutionLink
        of it, or naming an approved address or a sub-account of an approved base account;
        with the account unknown, every one. Under a profile that does not scope its
        on-chain quarantine every one bears, as every quarantined record did.

        0.9.0: an unlinked movement beside an execution linked to the request can never
        be a burn of it, as an unlinked movement from an approved G address beside a linked
        burn. Representations of one execution share its record key and are resolved before
        (identical: once; different: a source conflict)."""
        relevant = self._relevant(account)
        unscoped = self._token_requirement().quarantine_scope is None
        operation = self.snapshot.operation_ref
        issuers = {r.issuer for r in self.profile.representations}
        if unscoped or not self.closure:
            return tuple(
                sorted(
                    o.observation_id
                    for o, p in muxed
                    if unscoped
                    or o.operation_ref == operation
                    or bears_by_address(movement_addresses(p), relevant)
                )
            )
        linked_any = any(o.operation_ref == operation for o, _ in (*muxed, *counted))
        bearing = []
        for o, p in muxed:
            if o.operation_ref == operation:
                bearing.append(o.observation_id)  # a further execution of the request
            elif (
                not linked_any
                # Could it be the burn whose link is missing? As for a G address: to the
                # issuer, from an approved address or a sub-account of one.
                and p.to_address in issuers
                and bears_by_address([movement_sender(p)], relevant)
            ):
                bearing.append(o.observation_id)  # relevance unresolved
        return tuple(sorted(bearing))

    def _relevant(self, account: str | None) -> set[str] | None:
        """Addresses approved for the request's account (any window); None when the
        account is not known, so that nothing can be shown foreign to the request."""
        if account is None:
            return None
        return {link.address for link in self.links if link.account_ref == account}

    def _bearing(self) -> tuple[str, ...]:
        """Chain effects bearing on the request (0.5.0). They never fulfil anything and the
        burn view is kept as it is: they only stop a result that resolving them could
        change."""
        if not self.inv013:
            return ()
        request = self._request_or_none()
        self.view("token_movement")  # sets the muxed movements set aside (0.8.0)
        return tuple(
            sorted(
                {
                    *self._chain_effects_bearing(request.account_ref if request else None),
                    *self.muxed_bearing,
                }
            )
        )

    def _unresolved_effect(self, refs: list[str]) -> Undecided:
        bearing = self._bearing()
        if bearing and set(bearing) <= set(self.muxed_bearing):
            # Only movements set aside: a token movement no profile attributes, not a chain
            # effect (domain review B5).
            what = (
                "naming a muxed sub-account or coming from an address without an approved link"
                if self.closure
                else "naming a muxed sub-account"
            )
            return Undecided(
                "AMBIGUOUS_MATCH",
                f"token_movement: movement(s) {what} ({', '.join(bearing)}) bear on the "
                "request; no redemption profile attributes them, so they are never a burn, "
                "and resolving them could change this result",
                (*refs, *bearing),
            )
        return Undecided(
            "UNSUPPORTED_CAPABILITY",
            "token_movement: on-chain effect bearing on the request "
            f"({', '.join(bearing)}); it is not admitted as payment, delivery or retirement, "
            "and resolving it could change this result",
            (*refs, *bearing),
        )

    def _chain_effects_bearing(self, account: str | None) -> tuple[str, ...]:
        return chain_effects_bearing(
            [o for o in self.members if o.valid_time <= self.cut],
            self.profile.authority_for("token_movement"),
            self.profile.instrument.instrument_id,
            self.snapshot.operation_ref,
            self._relevant(account),
            unresolved_bears=self.unresolved_bears,
        )

    # ------------------------------------------------------------- coverage

    @staticmethod
    def _raise_for_status(view: FactView) -> None:
        if view.status in _REASON_BY_STATUS:
            raise Undecided(
                _REASON_BY_STATUS[view.status], f"{view.fact_type}: {view.detail}", view.refs
            )

    def _certificate(
        self, fact_type: FactType, start: datetime, end: datetime, *, include_end: bool = False
    ) -> str:
        """The applicable certificate must cover [start, end) — or [start, end] when
        ``include_end`` — at the required level and scope, without gaps or filters."""
        source = self.profile.authority_for(fact_type)
        requirement = next(
            r
            for r in self.profile.coverage_requirements
            if r.source_id == source and fact_type in r.fact_types
        )
        certs: list[CoverageCertificate] = [
            c
            for c in self.certificates
            if c.source_id == source
            and fact_type in c.fact_types
            and c.instrument_id == self.profile.instrument.instrument_id
        ]
        if not certs:
            raise Undecided(
                "INSUFFICIENT_COVERAGE", f"{fact_type}: no coverage certificate from {source}"
            )
        if self.adr014 and fact_type == "token_movement" and requirement.quarantine_scope:
            return self._active_certificate(certs, requirement, start, end, include_end)
        latest = max(certs, key=lambda c: (c.recorded_at, c.coverage_id))
        if latest.records_quarantined > 0:
            scoped = (
                self.inv013
                and requirement.quarantine_scope == "records_bearing_on_operation"
                and latest.quarantined_records is not None
            )
            if not scoped:
                raise Undecided(
                    "QUARANTINED_INPUT",
                    f"{fact_type}: {latest.records_quarantined} record(s) quarantined in "
                    f"{latest.coverage_id}",
                    (latest.coverage_id,),
                )
            # Addresses scope only the chain movements; any other source counts every
            # quarantined record.
            request = self._request_or_none()
            relevant = (
                self._relevant(request.account_ref if request else None)
                if fact_type == "token_movement"
                else None
            )
            affecting = quarantine_affecting(latest, relevant)
            if affecting:
                raise Undecided(
                    "QUARANTINED_INPUT",
                    f"{fact_type}: {affecting} of {latest.records_quarantined} quarantined "
                    f"record(s) in {latest.coverage_id} involve the request's addresses or "
                    f"unknown parties",
                    (latest.coverage_id,),
                )
        problems = self._coverage_problems(latest, requirement, fact_type, start, end, include_end)
        if problems:
            raise Undecided(
                "INSUFFICIENT_COVERAGE",
                f"{fact_type}: {latest.coverage_id} " + "; ".join(problems),
                (latest.coverage_id,),
            )
        if fact_type == "token_movement":
            self._absence_in_sight([latest], requirement)
        return latest.coverage_id

    def _coverage_problems(
        self,
        certificate: CoverageCertificate,
        requirement: RedemptionCoverageRequirement,
        fact_type: FactType,
        start: datetime,
        end: datetime,
        include_end: bool,
    ) -> list[str]:
        problems = []
        if certificate.gaps:
            problems.append("declares gaps")
        if LEVEL_RANK[certificate.level] < LEVEL_RANK[requirement.min_level]:
            problems.append(f"level {certificate.level} < {requirement.min_level}")
        interval = certificate.interval
        reaches = interval.end > end if include_end else interval.end >= end
        if interval.start > start or not reaches:
            bracket = "]" if include_end else ")"
            problems.append(f"interval does not cover [{_iso(start)}, {_iso(end)}{bracket}")
        problems.extend(self._scope_problems(certificate, requirement.scope, fact_type))
        return problems

    # ------------------------------------------------------------- burn completeness

    def _burns_complete(self, spec: RedemptionControlSpec, result: ControlResult) -> ControlResult:
        """An equal or short burn comparison affirms that the burns observed are all the
        burns of the request: it needs a chain certificate covering
        [accepted_at, valid_at] (a fact counts while effective at the cut, so the end is
        included) whose scope is coherent and of the representation of the burns, reading
        the account's approved addresses. Otherwise UNKNOWN; an excess never comes here."""
        request = self._request_or_none()
        assert request is not None  # the comparison was made
        requirement = self._token_requirement()
        certs = self._token_certificates()
        start, end = request.accepted_at, self.snapshot.valid_at
        reason = "the burns observed cannot be shown to be all the burns of the request"
        refs = tuple(result.evidence_refs)
        if not certs:
            raise Undecided(
                "INSUFFICIENT_COVERAGE",
                f"token_movement: no coverage certificate from {requirement.source_id}; {reason}",
                refs,
            )
        # Every (active) certificate of the source, newest first: one per representation or
        # address may be needed.
        active = active_coverage(certs).active if self.adr014 else tuple(certs)
        pool = sorted(active, key=lambda c: (c.recorded_at, c.coverage_id))[::-1]
        meeting = [
            c
            for c in pool
            if not self._coverage_problems(c, requirement, "token_movement", start, end, True)
        ]
        if not meeting:
            problems = self._coverage_problems(
                pool[0], requirement, "token_movement", start, end, True
            )
            raise Undecided(
                "INSUFFICIENT_COVERAGE",
                f"token_movement: {pool[0].coverage_id} " + "; ".join(problems) + f"; {reason}",
                (*refs, pool[0].coverage_id),
            )
        # B1: every representation of the instrument, each with its own coverage, since an
        # unseen burn could be in any of them.
        shortfall, usable = strict_absence_scope(
            meeting,
            list(self.profile.representations),
            addresses_by_network(self.links, request.account_ref),
            (),
        )
        if shortfall is not None:
            raise Undecided(
                "INSUFFICIENT_COVERAGE",
                f"token_movement: {shortfall}; {reason}, so an equal or short comparison is "
                "not concluded",
                (*refs, *(c.coverage_id for c in meeting)),
            )
        cited = sorted({c.coverage_id for c in usable})
        return control_result(
            spec,
            result.status,
            result.reason_code,
            f"{result.reason}; the burns observed are complete per {', '.join(cited)}",
            [*refs, *(c for c in cited if c not in refs)],
            result.delta,
        )

    # ------------------------------------------------------------- active certificates

    def _token_certificates(self) -> list[CoverageCertificate]:
        source = self.profile.authority_for("token_movement")
        return [
            c
            for c in self.certificates
            if c.source_id == source
            and "token_movement" in c.fact_types
            and c.instrument_id == self.profile.instrument.instrument_id
        ]

    def _token_requirement(self) -> RedemptionCoverageRequirement:
        source = self.profile.authority_for("token_movement")
        return next(
            r
            for r in self.profile.coverage_requirements
            if r.source_id == source and "token_movement" in r.fact_types
        )

    def _bearing_records(
        self, certs: list[CoverageCertificate]
    ) -> tuple[str, int, tuple[str, ...], list[tuple[QuarantinedRecord, RecordSupport]]]:
        """(label, records quarantined, refs, records bearing on the request with what the
        snapshot supports of them) over the active chain certificates. A
        certificate quarantining records without listing their parties is UNKNOWN here."""
        coverage = active_coverage(certs)
        label = ", ".join(c.coverage_id for c in coverage.active)
        refs = tuple(c.coverage_id for c in coverage.active)
        quarantined = sum(c.records_quarantined for c in coverage.active)
        unlisted = [
            c for c in coverage.active if c.records_quarantined and c.quarantined_records is None
        ]
        if unlisted:
            raise Undecided(
                "QUARANTINED_INPUT",
                f"token_movement: {quarantined} record(s) quarantined in {label}, "
                f"{', '.join(c.coverage_id for c in unlisted)} without their parties",
                refs,
            )
        request = self._request_or_none()
        relevant = self._relevant(request.account_ref if request else None)
        # Support is checked against every chain observation of the snapshot: one effective
        # after the economic cut still backs a record's ledger operation (review M4); the cut
        # only decides what counts as economic evidence.
        evidence = chain_evidence(
            self.members,
            self.profile.authority_for("token_movement"),
            self.profile.instrument.instrument_id,
            {r.network for r in self.profile.representations},
        )
        policy = self._token_requirement().quarantine_policy is not None
        operation = self.snapshot.operation_ref
        found = []
        for _certificate, record in coverage.records():
            support = record_support(record, evidence, relevant)
            linked = (
                operation in (record.execution_links or ())
                if policy
                else record.execution_links is not None
            )
            by_parties = (
                by_address_muxed(record, relevant)
                if self.muxed_identity
                else by_address(record, relevant)
            )
            if by_parties or linked or support.bears:
                found.append((record, support))
        return label, quarantined, refs, found

    def _active_certificate(
        self,
        certs: list[CoverageCertificate],
        requirement: RedemptionCoverageRequirement,
        start: datetime,
        end: datetime,
        include_end: bool,
    ) -> str:
        """Absence of settlement on the chain (0.7.0): no record of an active certificate may
        bear on the request (it could be a settlement), and an active certificate must meet
        the coverage."""
        label, quarantined, refs, bearing = self._bearing_records(certs)
        if bearing:
            notes = [f"{r.locator}: {n}" for r, s in bearing for n in s.notes]
            raise Undecided(
                "QUARANTINED_INPUT",
                f"token_movement: {len(bearing)} of {quarantined} quarantined record(s) in "
                f"{label} bear on the request; the absence of settlement cannot be shown"
                + "".join(f"; {note}" for note in notes),
                refs,
            )
        coverage = active_coverage(certs)
        newest_first = sorted(coverage.active, key=lambda c: (c.recorded_at, c.coverage_id))[::-1]
        if requirement.quarantine_policy is None:
            # Without a declared policy the coverage is the most recent certificate's, as
            # the profile promised (review M1); only the quarantine is the union.
            newest_first = newest_first[:1]
        meeting = [
            c
            for c in newest_first
            if not self._coverage_problems(
                c, requirement, "token_movement", start, end, include_end
            )
        ]
        if meeting:
            return self._absence_in_sight(meeting, requirement).coverage_id
        latest = newest_first[0]
        problems = self._coverage_problems(
            latest, requirement, "token_movement", start, end, include_end
        )
        raise Undecided(
            "INSUFFICIENT_COVERAGE",
            f"token_movement: {latest.coverage_id} " + "; ".join(problems),
            (latest.coverage_id,),
        )

    def _absence_in_sight(
        self,
        meeting: Sequence[CoverageCertificate],
        requirement: RedemptionCoverageRequirement,
    ) -> CoverageCertificate:
        """The first certificate meeting the coverage that can also show the absence of a
        settlement other than an admitted burn (0.9.0, as decided on 2026-10-08):
        one whose chain scope declares a claimable balance clawback or claim out of its
        sight cannot, since such an effect could be the retirement or settlement. "No burn
        the profile admits" is not "no retirement or settlement occurred"."""
        if not self.closure:
            return meeting[0]
        if self.scoped_absence and requirement.absence_needs_chain_scope:
            # Only a declared, sufficient chain scope shows an absence; missing
            # exclusions are never read as complete coverage.
            request = self._request_or_none()
            account = request.account_ref if request else None
            if self.completeness and requirement.completeness_needs_coverage:
                # B1 and B3: no movement is counted, so every representation of the
                # instrument, each with a coherent scope of its own.
                shortfall, usable = strict_absence_scope(
                    meeting,
                    list(self.profile.representations),
                    addresses_by_network(self.links, account),
                    SETTLEMENT_EFFECTS,
                )
            else:
                shortfall, usable = absence_scope(
                    meeting,
                    self.profile.representations,
                    self._relevant(account),
                    SETTLEMENT_EFFECTS,
                )
            if shortfall is None:
                # Cite a certificate that can show the absence, never one that cannot.
                return usable[0]
            raise Undecided(
                "INSUFFICIENT_COVERAGE",
                f"token_movement: no burn the profile admits is linked to the request, "
                f"but {shortfall}; without a sufficient declared chain scope the chain "
                "coverage cannot show that no retirement or settlement occurred (that "
                "is not evidence of compliance either)",
                tuple(c.coverage_id for c in meeting),
            )
        for certificate in meeting:
            if covers_absence(certificate, SETTLEMENT_EFFECTS):
                return certificate
        unseen = sorted(
            {
                effect
                for c in meeting
                if c.chain_scope is not None
                for effect in c.chain_scope.not_covered or ()
                if effect in SETTLEMENT_EFFECTS
            }
        )
        raise Undecided(
            "INSUFFICIENT_COVERAGE",
            "token_movement: no burn the profile admits is linked to the request, but "
            f"{', '.join(c.coverage_id for c in meeting)} declare(s) out of sight for their "
            f"target and route: {', '.join(unseen)} (chain_scope.not_covered); such an effect "
            "could be the retirement or settlement, so the absence of settlement cannot be "
            "shown (it is not evidence of compliance either)",
            tuple(c.coverage_id for c in meeting),
        )

    def _quarantine_on_burns(
        self, burns: Sequence[Observation]
    ) -> tuple[
        str, int, tuple[str, ...], list[QuarantinedRecord], list[QuarantinedRecord], list[str]
    ]:
        """Records bearing on the request split by what they could change (0.7.0, profile
        policy): (label, quarantined, refs, on a counted burn or an unsupported operation,
        on another operation, notes)."""
        label, quarantined, refs, bearing = self._bearing_records(self._token_certificates())
        burned = {
            f"{o.payload.chain.tx_hash}:{o.payload.chain.operation_index}"
            for o in burns
            if isinstance(o.payload, TokenMovementPayload)
        }
        undermining = [r for r, s in bearing if s.operation is None or s.operation in burned]
        other = [r for r, s in bearing if not (s.operation is None or s.operation in burned)]
        notes = [f"{r.locator}: {n}" for r, s in bearing for n in s.notes]
        return label, quarantined, refs, undermining, other, notes

    def _policy(self) -> bool:
        return (
            self.adr014
            and self.inv013
            and self._token_requirement().quarantine_policy is not None
            and bool(self._token_certificates())
        )

    def _scope_problems(
        self, cert: CoverageCertificate, scope: str, fact_type: FactType
    ) -> list[str]:
        """The export must cover the evaluated predicate: the request's account (and the
        cash currency), and keep every record of ``fact_type`` for them (D-2)."""
        if scope == "none":
            return []
        request = self._request_or_none()
        if cert.scope is None:
            return ["declares no account scope"]
        problems = []
        if request is None or request.account_ref not in cert.scope.accounts:
            problems.append("does not cover the request's account")
        if scope == "account_and_currency" and (
            cert.scope.currencies is None
            or self.profile.pricing.cash_unit not in cert.scope.currencies
        ):
            problems.append(f"does not cover currency {self.profile.pricing.cash_unit}")
        for f in cert.scope.filters:
            keeps = (
                (
                    f.field == "account_ref"
                    and request is not None
                    and request.account_ref in f.values
                )
                or (
                    f.field == "currency"
                    and scope == "account_and_currency"
                    and self.profile.pricing.cash_unit in f.values
                )
                or (f.field == "fact_type" and fact_type in f.values)
            )
            if not keeps:
                problems.append(
                    f"filter on {f.field} in {f.values} may exclude records this control needs"
                )
        return problems

    def _present(self, fact_type: FactType, refs: list[str]) -> tuple[Observation, ...]:
        """Positive evidence of one fact (MISSING_EVIDENCE when absent).

        A positive observation needs provenance, authority, effective time within the cut
        and revision resolution; it does not need continuous coverage (I-B).
        """
        view = self.view(fact_type)
        self._raise_for_status(view)
        if view.status == "absent":
            raise Undecided("MISSING_EVIDENCE", f"{fact_type}: {view.detail}")
        refs.extend(view.refs)
        return view.observations

    def request_observation(self, refs: list[str]) -> Observation:
        (o,) = self._present("redemption_accepted", refs)
        assert isinstance(o.payload, RedemptionRequestPayload)
        payload = o.payload
        instrument, pricing = self.profile.instrument, self.profile.pricing
        if (payload.units.unit, payload.units.scale) != (instrument.unit, instrument.scale):
            raise Undecided("UNSUPPORTED_CAPABILITY", "request units not in instrument unit/scale")
        if int(payload.units.atoms) <= 0:
            raise Undecided("UNSUPPORTED_CAPABILITY", "request units must be positive")
        price = payload.price_per_unit
        if (price.unit, price.scale) != (pricing.cash_unit, pricing.cash_scale):
            raise Undecided("UNSUPPORTED_CAPABILITY", "request price not in cash unit/scale")
        return o

    def request(self, refs: list[str]) -> RedemptionRequestPayload:
        payload = self.request_observation(refs).payload
        assert isinstance(payload, RedemptionRequestPayload)
        return payload

    def due(self, request: RedemptionRequestPayload) -> datetime:
        return request.accepted_at + timedelta(hours=self.profile.payment_deadline.hours)

    def acceptance_problem(self, refs: list[str]) -> str | None:
        """None when accepted_at is credited: the TA record is effective at accepted_at."""
        o = self.request_observation(refs)
        assert isinstance(o.payload, RedemptionRequestPayload)
        if o.payload.accepted_at.microsecond:
            return "accepted_at is finer than the declared precision (second)"
        if o.valid_time != o.payload.accepted_at:
            return (
                f"acceptance time uncertain: record effective {_iso(o.valid_time)} but "
                f"accepted_at {_iso(o.payload.accepted_at)}"
            )
        return None

    def price(self, refs: list[str]) -> Quantity:
        (o,) = self._present("price_approved", refs)
        assert isinstance(o.payload, PricePayload)
        return o.payload.price_per_unit

    def expected_payment(self, request: RedemptionRequestPayload, price: Quantity) -> Quantity:
        pricing = self.profile.pricing
        if (price.unit, price.scale) != (pricing.cash_unit, pricing.cash_scale):
            raise Undecided("UNSUPPORTED_CAPABILITY", "approved price not in cash unit/scale")
        product = int(request.units.atoms) * int(price.atoms)
        divisor = 10**request.units.scale
        if product % divisor:
            raise Undecided(
                "INEXACT_AMOUNT",
                f"{request.units.to_decimal_text()} units x {price.to_decimal_text()} "
                f"{price.unit} is not exact at scale {price.scale}; nothing is rounded",
            )
        return Quantity(atoms=str(product // divisor), scale=price.scale, unit=price.unit)

    # ------------------------------------------------------------- position

    def available_position(self, request: RedemptionRequestPayload, refs: list[str]) -> Quantity:
        """Available units immediately before acceptance, ordered by TA journal sequence.

        holding - reserved at the snapshot, plus the journal changes strictly between the
        snapshot and the acceptance. Equal timestamps never order entries; equal sequences
        or times that contradict the sequence are ambiguous. The request's own reservation
        is after its acceptance and is never subtracted.
        """
        (p,) = self._present("position_held", refs)
        pos = p.payload
        assert isinstance(pos, PositionPayload)
        if pos.account_ref != request.account_ref:
            raise Undecided("AMBIGUOUS_MATCH", "position is for another account", refs)
        kind = (self.profile.instrument.unit, self.profile.instrument.scale)
        if (pos.units.unit, pos.units.scale) != kind or (
            pos.reserved.unit,
            pos.reserved.scale,
        ) != kind:
            raise Undecided("UNSUPPORTED_CAPABILITY", "position not in instrument unit/scale")
        journal = self.profile.ta_journal.journal_id
        if request.journal_id != journal or pos.journal_id != journal:
            raise Undecided(
                "AMBIGUOUS_SEQUENCE",
                f"acceptance ({request.journal_id}) and position ({pos.journal_id}) must both "
                f"come from the declared TA journal {journal}; sequences of different journals "
                "are not comparable",
                refs,
            )
        if pos.sequence == request.sequence:
            raise Undecided(
                "AMBIGUOUS_SEQUENCE", "position and acceptance share a journal sequence", refs
            )
        if pos.sequence > request.sequence or pos.as_of > request.accepted_at:
            raise Undecided(
                "POSITION_NOT_BEFORE_ACCEPTANCE",
                f"position at sequence {pos.sequence} ({_iso(pos.as_of)}) is not before the "
                f"acceptance at sequence {request.sequence} ({_iso(request.accepted_at)})",
                refs,
            )
        view = self.view("position_changed")
        self._raise_for_status(view)
        between: list[PositionChangePayload] = []
        for o in view.observations:
            change = o.payload
            assert isinstance(change, PositionChangePayload)
            if change.journal_id != journal:
                raise Undecided(
                    "AMBIGUOUS_SEQUENCE",
                    f"journal entry {change.change_ref} belongs to journal {change.journal_id}; "
                    "it cannot be ordered against the declared TA journal",
                    (*refs, o.observation_id),
                )
            if change.sequence in (pos.sequence, request.sequence):
                raise Undecided(
                    "AMBIGUOUS_SEQUENCE",
                    f"journal entry {change.change_ref} shares a sequence with the position "
                    "or the acceptance",
                    (*refs, o.observation_id),
                )
            in_sequence = pos.sequence < change.sequence < request.sequence
            strictly_inside = pos.as_of < change.effective_at < request.accepted_at
            outside = change.effective_at < pos.as_of or change.effective_at > request.accepted_at
            if (in_sequence and outside) or (strictly_inside and not in_sequence):
                raise Undecided(
                    "AMBIGUOUS_SEQUENCE",
                    f"journal entry {change.change_ref}: time and sequence disagree",
                    (*refs, o.observation_id),
                )
            if in_sequence:
                if change.request_ref == self.snapshot.operation_ref:
                    raise Undecided(
                        "AMBIGUOUS_SEQUENCE",
                        f"journal entry {change.change_ref} moves this request before its "
                        "acceptance",
                        (*refs, o.observation_id),
                    )
                between.append(change)
                refs.append(o.observation_id)
        refs.append(self._certificate("position_changed", pos.as_of, request.accepted_at))
        holding = int(pos.units.atoms) + sum(int(c.holding_delta.atoms) for c in between)
        reserved = int(pos.reserved.atoms) + sum(int(c.reserved_delta.atoms) for c in between)
        return Quantity(atoms=str(holding - reserved), scale=kind[1], unit=kind[0])

    # ---------------------------------------------------------- cancellation

    def cancellation(
        self, validity: RedemptionControlSpec, settlement: RedemptionControlSpec
    ) -> tuple[CancellationState, ControlResult, ControlResult]:
        """Cancellation state, with the validity and no-settlement control results."""
        validity_trace = self.tracer[validity.control_id]
        settlement_trace = self.tracer[settlement.control_id]
        refs: list[str] = []
        try:
            with validity_trace.step("fact:redemption_accepted", refs):
                request = self.request(refs)
            with validity_trace.step("reactivation", refs):
                reactivation = self.view("redemption_reactivated")
                self._raise_for_status(reactivation)
            with validity_trace.step("retractions", refs) as step:
                view = self.view("redemption_cancelled")
                retractions = self._cancellation_retractions()
                for retraction in retractions:
                    self._check_retraction(retraction)
                if not retractions:
                    step.state("not_applicable", None, "no retraction of a cancellation")
            none: ControlResult | None = None
            with validity_trace.step("cancellation", refs) as step:
                step.fact = "redemption_cancelled"
                self._raise_for_status_unless_withdrawn(view)
                if view.status in ("absent", "withdrawn"):
                    detail = "no cancellation"
                    if view.status == "withdrawn":
                        detail = self._retraction_detail(view, refs)
                    refs.append(
                        self._certificate(
                            "redemption_cancelled", request.accepted_at, self.snapshot.valid_at
                        )
                    )
                    none = step.result(
                        control_result(
                            validity,
                            "NOT_APPLICABLE",
                            "NO_CANCELLATION",
                            f"{detail}, with TA coverage from acceptance to valid_at",
                            refs,
                        )
                    )
                else:
                    if reactivation.status == "asserted":
                        raise Undecided(
                            "REACTIVATION_UNSUPPORTED",
                            "the cancelled request was reactivated; business reactivation is "
                            "outside this profile",
                            reactivation.refs,
                        )
                    (o,) = self._present("redemption_cancelled", refs)
                    cancel = o.payload
                    assert isinstance(cancel, RedemptionCancellationPayload)
                    if cancel.account_ref != request.account_ref:
                        # Linked to this request yet for another account: a contradictory
                        # association. Keep the conflict visible; never drop or apply it.
                        raise Undecided(
                            "AMBIGUOUS_MATCH",
                            "cancellation is linked to this request but names another account",
                            (o.observation_id,),
                        )
                    problems = []
                    if cancel.authorized_by not in self.profile.cancellation.authorized_by:
                        problems.append(f"authorized_by {cancel.authorized_by} is not an authority")
                    if cancel.cancelled_at < request.accepted_at:
                        problems.append("effective before the request was accepted")
                    if cancel.cancelled_at != o.valid_time:
                        problems.append("effective time differs from the record's valid_time")
                    if problems:
                        raise Undecided(
                            "INVALID_CANCELLATION", "cancellation: " + "; ".join(problems)
                        )
                    valid = step.result(
                        control_result(
                            validity,
                            "PASS",
                            "EXACT_MATCH",
                            f"cancellation by {cancel.authorized_by} effective "
                            f"{_iso(cancel.cancelled_at)} (recorded {_iso(o.recorded_at)})",
                            refs,
                        )
                    )
            if none is not None:
                settlement_trace.record(
                    "cancellation_in_force", "not_applicable", "NO_CANCELLATION", NO_CANCELLATION
                )
                return ("none", none, _not_applicable(settlement, NO_CANCELLATION))
        except Undecided as undecided:
            settlement_trace.record(
                "cancellation_in_force", "not_applicable", "NO_CANCELLATION", NO_VALID_CANCELLATION
            )
            return (
                "undecided",
                control_result(
                    validity,
                    "UNKNOWN",
                    undecided.reason,
                    undecided.detail,
                    [*refs, *undecided.refs],
                ),
                _not_applicable(settlement, NO_VALID_CANCELLATION),
            )
        except Exception as error:  # a technical failure must never become PASS
            settlement_trace.record(
                "cancellation_in_force", "not_applicable", "NO_CANCELLATION", NO_VALID_CANCELLATION
            )
            return (
                "undecided",
                control_result(
                    validity,
                    "UNKNOWN",
                    "EVALUATION_ERROR",
                    f"{type(error).__name__}: {error}",
                    refs,
                ),
                _not_applicable(settlement, NO_VALID_CANCELLATION),
            )
        settlement_trace.record(
            "cancellation_in_force", "satisfied", None, "a valid cancellation is in force"
        )
        return "cancelled", valid, self._no_settlement(settlement, request, list(refs))

    def _raise_for_status_unless_withdrawn(self, view: FactView) -> None:
        if view.status != "withdrawn":
            self._raise_for_status(view)

    def _retraction_detail(self, view: FactView, refs: list[str]) -> str:
        """A valid retraction withdraws the cancellation revision it names."""
        for retraction_id in view.refs:
            self._check_retraction(self.by_id[retraction_id])
            refs.extend((self.by_id[retraction_id].supersedes or "", retraction_id))
        return "cancellation retracted as an evidence correction"

    def _check_retraction(self, retraction: Observation) -> None:
        """A retraction must name a cancellation assertion kept in the snapshot, of the same
        source and record and an earlier revision. Otherwise it is invalid or irresolvable:
        it does not withdraw anything, and both pieces of evidence stay visible."""
        target = self.by_id.get(retraction.supersedes or "")
        if (
            target is None
            or target.fact_type != "redemption_cancelled"
            or target.kind != "assertion"
            or target.source.source_id != retraction.source.source_id
            or target.source.record_key != retraction.source.record_key
            or target.source.revision >= retraction.source.revision
        ):
            refs = tuple(i for i in (retraction.observation_id, retraction.supersedes) if i)
            raise Undecided(
                "INVALID_RETRACTION",
                f"retraction {retraction.observation_id} does not resolve to a cancellation "
                "revision it may withdraw; nothing is withdrawn",
                refs,
            )

    def _cancellation_retractions(self) -> list[Observation]:
        return [
            o
            for o in self._authoritative("redemption_cancelled")
            if o.kind == "retraction" and o.operation_ref == self.snapshot.operation_ref
        ]

    def _no_settlement(
        self, spec: RedemptionControlSpec, request: RedemptionRequestPayload, refs: list[str]
    ) -> ControlResult:
        """Any linked, successful payment or burn effective within the cut is a BREAK
        for review, even before the cancellation. Proven settlement wins over another
        undecided view; absence needs bank and chain coverage of the interval.
        """
        trace = self.tracer[spec.control_id]
        try:
            with trace.step("settlement", refs) as step:
                settled: list[str] = []
                undecided: FactView | None = None
                for fact_type in SETTLEMENT_FACTS:
                    view = self.view(fact_type)
                    # An early burn does not fulfil the retirement, but it did happen: linked,
                    # successful and effective, it contradicts a cancellation (D-7).
                    observations = (
                        view.observations
                        if view.status in ("asserted", "unsupported", "early")
                        else ()
                    )
                    if fact_type == "cash_settled" and any(
                        isinstance(o.payload, CashPayload)
                        and o.payload.account_ref != request.account_ref
                        for o in observations
                    ):
                        self._recipient_must_be_credited(refs)
                    if observations:
                        # Positive evidence: linked, successful, effective within the cut.
                        settled.extend(view.refs)
                    elif view.status != "absent" and undecided is None:
                        undecided = view
                if settled and self._policy():
                    cash = self.view("cash_settled")
                    burns = self.view("token_movement")
                    proven_by_cash = cash.status in ("asserted", "unsupported") and bool(
                        cash.observations
                    )
                    if not proven_by_cash and burns.observations:
                        label, quarantined, crefs, undermining, _other, notes = (
                            self._quarantine_on_burns(burns.observations)
                        )
                        if undermining:
                            # The only settlement shown is a burn whose own ledger operation a
                            # quarantined record puts in doubt (attribution, execution, nature).
                            raise Undecided(
                                "QUARANTINED_INPUT",
                                f"token_movement: {len(undermining)} of {quarantined} "
                                f"quarantined record(s) in {label} are on the ledger operation "
                                "of the burn that is the only settlement shown, or on one the "
                                "snapshot does not support: whether it settled is in doubt"
                                + "".join(f"; {note}" for note in notes),
                                (*settled, *crefs),
                            )
                if settled:
                    return step.result(
                        control_result(
                            spec,
                            "FAIL",
                            "SETTLED_DESPITE_CANCELLATION",
                            "payment or burn linked to a cancelled request; review required, "
                            "nothing is reversed automatically",
                            [*refs, *settled],
                        )
                    )
                if undecided is not None:
                    self._raise_for_status(undecided)
            with trace.step("chain_effects", refs) as step:
                if self._bearing():
                    # Proven settlement above stays a BREAK; an absence cannot be shown while
                    # an effect on the request is unresolved.
                    raise self._unresolved_effect(refs)
                step.state("not_applicable", None, "no on-chain effect bears on the request")
            with trace.step("absence_coverage", refs) as step:
                for fact_type in SETTLEMENT_FACTS:
                    step.fact = fact_type
                    refs.append(
                        self._certificate(fact_type, request.accepted_at, self.snapshot.valid_at)
                    )
                step.state("satisfied", "EXACT_MATCH", NO_SETTLEMENT_SHOWN)
        except Undecided as undecided_error:
            return control_result(
                spec,
                "UNKNOWN",
                undecided_error.reason,
                undecided_error.detail,
                [*refs, *undecided_error.refs],
            )
        except Exception as error:  # a technical failure must never become PASS nor raise
            return control_result(
                spec, "UNKNOWN", "EVALUATION_ERROR", f"{type(error).__name__}: {error}", refs
            )
        return control_result(spec, "PASS", "EXACT_MATCH", NO_SETTLEMENT_SHOWN, refs)

    def _recipient_must_be_credited(self, refs: list[str]) -> None:
        """A linked payment to another account is a demonstrated wrong recipient only when
        bank and TA name accounts in one namespace; otherwise the identity is uncertain."""
        if self.profile.correlation.account_namespace != "shared_pseudonymous_account_ref":
            raise Undecided(
                "AMBIGUOUS_MATCH",
                "payment names another account and account identities are not comparable",
                refs,
            )

    def cancelled_after_due(self) -> bool:
        """By effective time: a cancellation after the due time keeps a missed deadline."""
        request = self._request_or_none()
        view = self.view("redemption_cancelled")
        if request is None or view.status != "asserted":
            return False
        payload = view.observations[0].payload
        return isinstance(
            payload, RedemptionCancellationPayload
        ) and payload.cancelled_at > self.due(request)

    # ------------------------------------------------------------- controls

    def control(self, spec: RedemptionControlSpec) -> tuple[ControlResult, Operands | None]:
        # Each check runs in a trace step: the step records what the existing code
        # concluded and cites; it never changes the order or the decision.
        trace = self.tracer[spec.control_id]
        refs: list[str] = []
        try:
            with trace.step("request_support", refs):
                self._request_withdrawal(spec)
            result, ops = self._decide(spec, refs, trace)
            if spec.control_id == "redemption.burn_vs_request" and result.status in (
                "PASS",
                "FAIL",
            ):
                excess = result.delta is not None and int(result.delta.atoms) > 0
                with trace.step("quarantine", refs) as step:
                    if not self._policy():
                        step.state(
                            "not_applicable",
                            None,
                            "the profile declares no quarantine policy for the burns, or no "
                            "chain certificate is in the snapshot",
                        )
                    else:
                        weighed = self._weigh_burn_quarantine(spec, result)
                        if weighed is not result:
                            step.state(
                                "undetermined",
                                "QUARANTINED_INPUT",
                                "quarantined records bearing on the request could only add to "
                                "this excess",
                            )
                            step.cite(
                                *(r for r in weighed.evidence_refs if r not in result.evidence_refs)
                            )
                        result = weighed
                with trace.step("chain_effects", refs) as step:
                    bearing = self._bearing()
                    if bearing and not excess:
                        # Equal or short: an unresolved effect could be the rest of the
                        # retirement. An excess of linked burns stays a FAIL: no effect can
                        # undo a burn.
                        raise self._unresolved_effect([*refs, *result.evidence_refs])
                    if bearing:
                        step.state(
                            "undetermined",
                            "UNSUPPORTED_CAPABILITY",
                            f"on-chain effect(s) or unattributed movement(s) bearing on the "
                            f"request ({', '.join(bearing)}) could only add to this excess",
                        )
                        step.aside(bearing)
                    else:
                        step.state(
                            "not_applicable", None, "no on-chain effect bears on the request"
                        )
                with trace.step("completeness", refs) as step:
                    if excess:
                        step.state(
                            "not_applicable",
                            None,
                            "an excess of burns does not depend on the completeness of the "
                            "burns observed",
                        )
                    elif not (
                        self.completeness and self._token_requirement().completeness_needs_coverage
                    ):
                        step.state(
                            "not_applicable",
                            None,
                            "the profile does not require coverage to affirm that the burns "
                            "observed are complete",
                        )
                    else:
                        step.fact = "token_movement"
                        before = set(result.evidence_refs)
                        result = self._burns_complete(spec, result)
                        step.cite(*(r for r in result.evidence_refs if r not in before))
            return result, ops
        except Undecided as undecided:
            return control_result(
                spec, "UNKNOWN", undecided.reason, undecided.detail, [*refs, *undecided.refs]
            ), None
        except Exception as error:  # a technical failure must never become PASS
            return control_result(
                spec, "UNKNOWN", "EVALUATION_ERROR", f"{type(error).__name__}: {error}", refs
            ), None

    def _weigh_burn_quarantine(
        self, spec: RedemptionControlSpec, result: ControlResult
    ) -> ControlResult:
        """A burn passes or fails as positive evidence; under a declared quarantine policy
        (0.7.0) a quarantined record bearing on the request leaves UNKNOWN what it could
        change: on the counted burn's ledger operation (or an unsupported one), any result;
        on another operation, an equal or short burn (it could be another retirement, an
        identified clawback included); an excess stays a FAIL. A clawback is never counted
        as an accepted burn."""
        burns = self.view("token_movement").observations
        label, quarantined, refs, undermining, other, notes = self._quarantine_on_burns(burns)
        noted = "".join(f"; {note}" for note in notes)
        excess = result.delta is not None and int(result.delta.atoms) > 0
        if undermining:
            raise Undecided(
                "QUARANTINED_INPUT",
                f"token_movement: {len(undermining)} of {quarantined} quarantined record(s) in "
                f"{label} bear on the request and are on the ledger operation of a counted "
                "burn, or on one the snapshot does not support: the counted burns are in "
                f"doubt{noted}",
                (*result.evidence_refs, *refs),
            )
        if other and not excess:
            raise Undecided(
                "QUARANTINED_INPUT",
                f"token_movement: {len(other)} of {quarantined} quarantined record(s) in "
                f"{label} bear on the request and could be another retirement ("
                + ", ".join(sorted({r.nature or "unresolved" for r in other}))
                + f"); resolving them could change this result{noted}",
                (*result.evidence_refs, *refs),
            )
        if other:
            return result.model_copy(
                update={
                    "reason": f"{result.reason}; quarantined records bearing on the request in "
                    f"{label} weighed: {len(other)} could only add to an excess{noted}",
                    "evidence_refs": list(dict.fromkeys([*result.evidence_refs, *refs])),
                }
            )
        return result

    def _request_withdrawal(self, spec: RedemptionControlSpec) -> None:
        """A withdrawn request is LOSS_OF_SUPPORT in this dependent control (I-G),
        but conflicts of its other facts stay visible and still-supported evidence (payments,
        burns) is cited, never erased."""
        view = self.view("redemption_accepted")
        if view.status != "withdrawn":
            return
        others = [f for f in spec.requires if f != "redemption_accepted"]
        for fact_type in others:
            other = self.view(fact_type)
            if other.status == "conflict":
                raise Undecided("SOURCE_CONFLICT", f"{fact_type}: {other.detail}", other.refs)
        supported = [
            i for f in others if self.view(f).status == "asserted" for i in self.view(f).refs
        ]
        detail = "redemption_accepted: the supporting record was withdrawn by its source"
        if supported:
            detail += "; still supported: " + ", ".join(supported)
        raise Undecided("LOSS_OF_SUPPORT", detail, (*view.refs, *supported))

    def _past_due(self, due: datetime) -> bool:
        """Absence can be judged only once both the clock and the economic cut passed due."""
        return self.clock > due and self.snapshot.valid_at > due

    def _not_due(self, due: datetime) -> Undecided:
        return Undecided(
            "PAYMENT_NOT_DUE",
            f"no payment yet; due {_iso(due)}, clock {_iso(self.clock)}, economic cut "
            f"{_iso(self.snapshot.valid_at)}",
        )

    def _compare(
        self,
        spec: RedemptionControlSpec,
        left: Quantity,
        right: Quantity,
        refs: list[str],
        failure: ReasonCode,
        *,
        at_most: bool = False,
    ) -> tuple[ControlResult, Operands | None]:
        if (left.unit, left.scale) != (right.unit, right.scale):
            raise Undecided(
                "UNSUPPORTED_CAPABILITY",
                f"operands differ in unit/scale: {left.unit}/{left.scale} vs "
                f"{right.unit}/{right.scale}",
                refs,
            )
        a, b = int(left.atoms), int(right.atoms)
        text = f"{left.to_decimal_text()} vs {right.to_decimal_text()} {left.unit}"
        ops = Operands(left, right)
        if a == b or (at_most and a < b):
            word = "within" if at_most and a < b else "equal"
            return control_result(spec, "PASS", "EXACT_MATCH", f"{word}: {text}", refs), ops
        delta = Quantity(atoms=str(a - b), scale=left.scale, unit=left.unit)
        return control_result(
            spec,
            "FAIL",
            failure,
            f"{text}; delta {delta.to_decimal_text()} {delta.unit}",
            refs,
            delta,
        ), ops

    def _due_text(self, request: RedemptionRequestPayload) -> str:
        return (
            f"declared payment_due_at {_iso(request.payment_due_at)}; computed "
            f"{_iso(self.due(request))} ({self.profile.payment_deadline.hours}h, "
            f"{self.profile.rules_ref})"
        )

    def _decide(
        self, spec: RedemptionControlSpec, refs: list[str], trace: ControlTrace
    ) -> tuple[ControlResult, Operands | None]:
        control = spec.control_id
        with trace.step("fact:redemption_accepted", refs):
            request = self.request(refs)
        if control == "redemption.declared_due_consistency":
            with trace.step("acceptance_time", refs):
                problem = self.acceptance_problem(refs)
                if problem is not None:
                    raise Undecided("DEADLINE_DATA_CONFLICT", problem, refs)
            with trace.step("declared_due", refs) as step:
                if request.payment_due_at != self.due(request):
                    raise Undecided(
                        "DEADLINE_DATA_CONFLICT",
                        f"{self._due_text(request)}; the computed due time is used",
                        refs,
                    )
                return step.result(
                    control_result(spec, "PASS", "EXACT_MATCH", self._due_text(request), refs)
                ), None
        if control == "redemption.price_vs_approved":
            with trace.step("fact:price_approved", refs):
                price = self.price(refs)
            with trace.step("comparison", refs) as step:
                return _traced(
                    step,
                    self._compare(spec, request.price_per_unit, price, refs, "PRICE_MISMATCH"),
                )
        if control == "redemption.units_within_position":
            with trace.step("position", refs) as step:
                step.fact = "position_changed"
                available = self.available_position(request, refs)
            with trace.step("comparison", refs) as step:
                return _traced(
                    step,
                    self._compare(
                        spec, request.units, available, refs, "UNITS_EXCEED_POSITION", at_most=True
                    ),
                )
        if control == "redemption.cash_vs_expected":
            with trace.step("fact:price_approved", refs):
                price = self.price(refs)
            with trace.step("expected_amount", refs) as step:
                expected = self.expected_payment(request, price)
                step.left = expected  # units x approved price, exact
            with trace.step("fact:cash_settled", refs):
                cash = self._payment(request, refs)
            with trace.step("recipient", refs) as step:
                if cash.account_ref != request.account_ref:
                    self._recipient_must_be_credited(refs)
                    return step.result(
                        control_result(
                            spec,
                            "FAIL",
                            "PAYMENT_TO_WRONG_ACCOUNT",
                            f"payment linked to the request went to {cash.account_ref}, not to "
                            f"the requesting account {request.account_ref}",
                            refs,
                        )
                    ), None
            with trace.step("comparison", refs) as step:
                return _traced(
                    step, self._compare(spec, cash.amount, expected, refs, "CASH_AMOUNT_MISMATCH")
                )
        if control == "redemption.payment_deadline":
            return self._deadline(spec, request, refs, trace), None
        if control == "redemption.ta_units_vs_request":
            with trace.step("fact:units_registered", refs):
                (o,) = self._present("units_registered", refs)
                assert isinstance(o.payload, UnitsPayload)
            with trace.step("account", refs):
                if o.payload.account_ref != request.account_ref:
                    raise Undecided(
                        "AMBIGUOUS_MATCH", "registry entry is for another account", refs
                    )
            with trace.step("comparison", refs) as step:
                return _traced(
                    step,
                    self._compare(spec, o.payload.units, request.units, refs, "UNITS_MISMATCH"),
                )
        if control == "redemption.burn_vs_request":
            with trace.step("fact:token_movement", refs):
                burns = self._present("token_movement", refs)
                units = [
                    o.payload.units for o in burns if isinstance(o.payload, TokenMovementPayload)
                ]
                kinds = {(q.unit, q.scale) for q in units}
                if len(kinds) != 1:
                    raise Undecided(
                        "UNSUPPORTED_CAPABILITY", "burns in different units/scales", refs
                    )
                ((unit, scale),) = kinds
                total = Quantity(
                    atoms=str(sum(int(q.atoms) for q in units)), scale=scale, unit=unit
                )
            with trace.step("comparison", refs) as step:
                return _traced(
                    step, self._compare(spec, total, request.units, refs, "UNITS_MISMATCH")
                )
        with trace.step("admission", refs, quiet=True):
            raise Undecided("EVALUATION_ERROR", f"control {control} is not implemented")

    def _payment(self, request: RedemptionRequestPayload, refs: list[str]) -> CashPayload:
        view = self.view("cash_settled")
        self._raise_for_status(view)
        if view.status == "absent":
            if not self._past_due(self.due(request)):
                raise self._not_due(self.due(request))
            raise Undecided("MISSING_EVIDENCE", "cash_settled: no payment in the snapshot")
        (o,) = self._present("cash_settled", refs)
        assert isinstance(o.payload, CashPayload)
        return o.payload

    def _deadline(
        self,
        spec: RedemptionControlSpec,
        request: RedemptionRequestPayload,
        refs: list[str],
        trace: ControlTrace,
    ) -> ControlResult:
        with trace.step("acceptance_time", refs):
            problem = self.acceptance_problem(refs)
            if problem is not None:
                raise Undecided("DEADLINE_DATA_CONFLICT", problem, refs)
        with trace.step("deadline", refs) as step:
            step.fact = "cash_settled"
            due = self.due(request)  # the profile's rule, never the declared value
            view = self.view("cash_settled")
            self._raise_for_status(view)
            wrong_recipient = ""
            if view.status == "asserted":
                (o,) = self._present("cash_settled", refs)
                assert isinstance(o.payload, CashPayload)
                if o.valid_time.microsecond:
                    raise Undecided(
                        "UNSUPPORTED_CAPABILITY",
                        f"payment time {o.valid_time.isoformat()} is finer than the declared "
                        "precision (second)",
                        refs,
                    )
                if o.payload.account_ref == request.account_ref:
                    paid = o.valid_time
                    text = f"paid {_iso(paid)}; {self._due_text(request)}"
                    if paid <= due:
                        return step.result(
                            control_result(spec, "PASS", "EXACT_MATCH", f"on time: {text}", refs)
                        )
                    return step.result(
                        control_result(
                            spec,
                            "FAIL",
                            "PAYMENT_LATE",
                            f"late by {paid - due}: {text}; a later payment does not erase the "
                            "breach",
                            refs,
                        )
                    )
                # Paid to another account: the requesting account was not paid by it.
                self._recipient_must_be_credited(refs)
                wrong_recipient = f"; the linked payment went to {o.payload.account_ref}"
            if not self._past_due(due):
                raise self._not_due(due)
            refs.append(
                self._certificate("cash_settled", request.accepted_at, due, include_end=True)
            )
            return step.result(
                control_result(
                    spec,
                    "FAIL",
                    "PAYMENT_MISSED",
                    f"no payment to {request.account_ref} by {_iso(due)} (clock "
                    f"{_iso(self.clock)}, economic cut {_iso(self.snapshot.valid_at)}), with bank "
                    f"coverage of [{_iso(request.accepted_at)}, {_iso(due)}]{wrong_recipient}; "
                    f"{self._due_text(request)}",
                    refs,
                )
            )


NO_CANCELLATION = "no cancellation in force"
NO_VALID_CANCELLATION = "no valid cancellation in force"
NO_SETTLEMENT_SHOWN = (
    "no payment or burn linked to the cancelled request, with bank and chain coverage from "
    "acceptance to valid_at"
)
ADMITTED = "snapshot, profile, engine and evidence provenance admitted"


def _traced(
    step: Step, outcome: tuple[ControlResult, Operands | None]
) -> tuple[ControlResult, Operands | None]:
    """Record a comparison the engine made in this step; returns it unchanged."""
    result, ops = outcome
    step.result(result)
    if ops is not None:
        step.compare(ops.left, ops.right, result.delta)
    return outcome


def _not_applicable(spec: RedemptionControlSpec, detail: str) -> ControlResult:
    return control_result(spec, "NOT_APPLICABLE", "NO_CANCELLATION", detail, [])


def evaluate_redemption(
    inputs: EvaluationInputs, *, engine_ref: str | None = None, replaying: bool = False
) -> Evaluation:
    """A new evaluation with the current engine, or (``replaying``) the reproduction of a
    recorded conclusion under its engine label, current or retired."""
    assert isinstance(inputs.profile, RedemptionProfile)
    engine_ref = engine_ref or REDEMPTION_ENGINE_REF
    snapshot, profile = inputs.snapshot, inputs.profile
    problem = snapshot_problem(inputs)
    if problem is None:
        problem = engine_problem(engine_ref, _SEMANTICS, "redemption", replaying)
    if problem is None:
        problem = profile_problem(
            engine_ref,
            profile.profile_ref,
            _declares_policy(profile),
            admits_more_mappings=any(s.additional_mapping_refs for s in profile.sources),
            needs_chain_scope=any(
                r.absence_needs_chain_scope for r in profile.coverage_requirements
            ),
            needs_completeness=any(
                r.completeness_needs_coverage for r in profile.coverage_requirements
            ),
        )
    if problem is None and (
        not replaying or (engine_ref in _SEMANTICS and _SEMANTICS[engine_ref].closure)
    ):
        # Admission is part of the semantics of 0.9.0 on: it refuses on replay too, so a
        # recorded refusal reproduces; a retired engine replays as it was.
        problem = provenance_problem(inputs)
    controls: list[ControlResult] = []
    operands: dict[str, Operands] = {}
    effective: tuple[str, ...] = ()
    state: OperationState | None = None
    tracer = Tracer()
    if problem is not None:
        controls = [
            control_result(spec, "UNKNOWN", "EVALUATION_ERROR", problem, [])
            for spec in profile.controls
        ]
        for spec in profile.controls:
            tracer[spec.control_id].record("admission", "undetermined", "EVALUATION_ERROR", problem)
    else:
        semantics = _SEMANTICS[engine_ref]
        evaluator = _Redemption(
            inputs,
            inv013=semantics.inv013,
            unresolved_bears=semantics.unresolved_bears,
            adr014=semantics.adr014,
            muxed_identity=semantics.muxed_identity,
            closure=semantics.closure,
            scoped_absence=semantics.scoped_absence,
            completeness=semantics.completeness,
        )
        tracer = evaluator.tracer
        for spec in profile.controls:
            tracer[spec.control_id].record("admission", "satisfied", None, ADMITTED)
        specs = {c.control_id: c for c in profile.controls}
        cancelled, validity, no_settlement = evaluator.cancellation(
            specs[VALIDITY], specs[NO_SETTLEMENT]
        )
        decided = {VALIDITY: validity, NO_SETTLEMENT: no_settlement}
        if cancelled == "cancelled":
            state = "cancelled"
        for spec in profile.controls:
            if spec.control_id in decided:
                controls.append(decided[spec.control_id])
                continue
            breach_kept = (
                spec.control_id == "redemption.payment_deadline" and evaluator.cancelled_after_due()
            )
            if spec.applies == "unless_cancelled" and cancelled == "cancelled" and not breach_kept:
                extinguished = control_result(
                    spec,
                    "NOT_APPLICABLE",
                    "EXTINGUISHED_BY_CANCELLATION",
                    "pending obligation extinguished by a valid cancellation",
                    [],
                )
                tracer[spec.control_id].record(
                    "applicability",
                    "not_applicable",
                    "EXTINGUISHED_BY_CANCELLATION",
                    extinguished.reason,
                )
                controls.append(extinguished)
                continue
            if spec.applies == "unless_cancelled":
                tracer[spec.control_id].record(
                    "applicability",
                    "satisfied",
                    None,
                    "a cancellation after the due time keeps the missed deadline"
                    if breach_kept
                    else NO_VALID_CANCELLATION,
                )
            outcome, ops = evaluator.control(spec)
            controls.append(outcome)
            if ops is not None:
                operands[spec.control_id] = ops
        for fact_type in FACT_ORDER:
            view = evaluator.view(fact_type)
            if view.status == "asserted":
                effective += tuple(o.observation_id for o in view.observations)
    result = EvaluationResult(
        schema_version="1.0",
        evaluation_id=evaluation_id(snapshot, profile.profile_ref, engine_ref),
        snapshot_id=snapshot.snapshot_id,
        operation_ref=snapshot.operation_ref,
        result=aggregate(controls),
        controls=controls,
        versions=EvaluationVersions(
            casm="1.0",
            profile_ref=profile.profile_ref,
            rules_ref=profile.rules_ref,
            mapping_refs=list(snapshot.mapping_refs),
            engine_ref=engine_ref,
            evidence_mapping_refs=(
                evidence_mapping_refs(inputs)
                if engine_ref in _SEMANTICS and _SEMANTICS[engine_ref].closure
                else None
            ),
        ),
        evaluation_clock=snapshot.evaluation_clock,
        assumptions=_assumptions(
            profile,
            _SEMANTICS[engine_ref].inv013 if engine_ref in _SEMANTICS else True,
            _SEMANTICS[engine_ref].unresolved_bears if engine_ref in _SEMANTICS else True,
            _SEMANTICS[engine_ref].adr014 if engine_ref in _SEMANTICS else True,
            _SEMANTICS[engine_ref].muxed_identity if engine_ref in _SEMANTICS else True,
            _SEMANTICS[engine_ref].closure if engine_ref in _SEMANTICS else True,
            _SEMANTICS[engine_ref].scoped_absence if engine_ref in _SEMANTICS else True,
            _SEMANTICS[engine_ref].completeness if engine_ref in _SEMANTICS else True,
        ),
        operation_state=state,
    )
    if state == "cancelled" and result.result == "MATCH":
        # A cancelled MATCH rests on explicit, passing cancellation controls, never on
        # every obligation being not applicable.
        assert validity.status == "PASS" and no_settlement.status == "PASS"
    checks = tracer.frozen() if engine_ref in DIAGNOSTIC_ENGINES else {}
    return Evaluation(result, tuple(sorted(set(effective))), operands, checks)
