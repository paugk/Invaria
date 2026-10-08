"""Pure evaluation of a closed snapshot against an operation profile.

No clock, network or filesystem. The result depends only on the snapshot, the profile and
the member artifacts. Anything that cannot be decided is UNKNOWN with a reason code; a
technical error is UNKNOWN (EVALUATION_ERROR), never PASS.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from invaria.contracts.coverage import CoverageCertificate, QuarantinedRecord, UncoveredEffect
from invaria.contracts.evaluation import (
    ControlResult,
    EvaluationResult,
    EvaluationVersions,
    ReasonCode,
    aggregate,
)
from invaria.contracts.observation import (
    CashPayload,
    ChainEffectPayload,
    FactType,
    Observation,
    OrderPayload,
    TokenMovementPayload,
    UnitsPayload,
    is_muxed,
    movement_addresses,
    movement_receiver,
)
from invaria.contracts.profile import (
    ControlSpec,
    CoverageRequirement,
    OperationProfile,
    RedemptionProfile,
)
from invaria.contracts.quantity import Quantity
from invaria.engine.common import (
    LEVEL_RANK,
    ActiveCoverage,
    ChainEvidence,
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
    engine_problem,
    evaluation_id,
    evidence_mapping_refs,
    institutional_view,
    provenance_problem,
    quarantine_affecting,
    quarantine_considered,
    record_support,
    resolve_records,
    snapshot_problem,
    strict_absence_scope,
)
from invaria.engine.versions import (
    SUBSCRIPTION_ENGINE_0_1_0,
    SUBSCRIPTION_ENGINE_0_2_0,
    SUBSCRIPTION_ENGINE_0_3_0,
    SUBSCRIPTION_ENGINE_0_4_0,
    SUBSCRIPTION_ENGINE_0_5_0,
    SUBSCRIPTION_ENGINE_0_6_0,
    SUBSCRIPTION_ENGINE_0_7_0,
    SUBSCRIPTION_ENGINE_0_8_0,
    SUBSCRIPTION_ENGINE_0_9_0,
    SUBSCRIPTION_ENGINE_REF,
    profile_problem,
)

__all__ = [
    "ENGINE_REF",
    "Evaluation",
    "EvaluationInputs",
    "Operands",
    "evaluate",
    "evaluation_id",
    "replay",
]

ENGINE_REF = SUBSCRIPTION_ENGINE_REF


@dataclass(frozen=True)
class _Semantics:
    """What each engine label this module runs does.

    ``inv013``: chain effects bearing on the operation and scoped quarantine (0.2.0 on).
    ``clawback_neutral``: a clawback never bears on the delivery comparison (0.3.0 on).
    ``unresolved_bears``: an effect naming an unidentified participant bears (0.4.0 on).
    ``bounded_quarantine``: a quarantined record bearing on the operation leaves UNKNOWN
    only what resolving it could change, and an ExecutionLink makes a record relevant to
    the linked operation without erasing its addresses (0.5.0 on).
    ``adr014``: the per-control quarantine only under a profile that declares it
    (``quarantine_policy``); the chain certificates stay active until explicitly
    superseded within their scope; a record's nature, ledger operation and parties are
    contrasted with the snapshot before relying on them (0.6.0 on).
    ``muxed_identity``: a movement naming a muxed sub-account is attributed only as the
    profile declares, and otherwise weighed as the quarantined record its earlier mapping
    made of it (0.7.0 on). Without it a muxed movement, which those engines never
    received, makes the token view unsupported.
    ``weighs_unattributed``: a movement never counted because its destination is not
    attributed is weighed by what it could change (0.8.0 on): linked to the
    operation, always; unlinked, only while no execution is linked to it, and never when an
    IdentityLink attributes its sub-account to another account. Under a profile that
    declares its quarantine policy a linked delivery to an address without an approved
    link is weighed the same way instead of blocking every comparison.
    ``states_provenance``: the evaluation states the mappings that produced its evidence
    (``evidence_mapping_refs``, 0.8.0 on).
    ``scoped_absence``: under a profile that declares ``absence_needs_chain_scope``, an
    equal or short token comparison needs a chain certificate whose declared scope is
    sufficient (``scope_shortfall``); without one it is UNKNOWN and an excess stays a FAIL
    (0.9.0 on).
    ``completeness``: under a profile that also declares ``completeness_needs_coverage``, the
    chain scope must be coherent with what its route and role observe and belong to the
    representation of the movements evaluated (0.10.0 on).
    """

    inv013: bool
    clawback_neutral: bool
    unresolved_bears: bool = False
    bounded_quarantine: bool = False
    adr014: bool = False
    muxed_identity: bool = False
    weighs_unattributed: bool = False
    states_provenance: bool = False
    scoped_absence: bool = False
    completeness: bool = False


# The current engine, and the retired ones it reproduces (compatibility implementations).
_SEMANTICS = {
    SUBSCRIPTION_ENGINE_REF: _Semantics(
        inv013=True,
        clawback_neutral=True,
        unresolved_bears=True,
        bounded_quarantine=True,
        adr014=True,
        muxed_identity=True,
        weighs_unattributed=True,
        states_provenance=True,
        scoped_absence=True,
        completeness=True,
    ),
    SUBSCRIPTION_ENGINE_0_9_0: _Semantics(
        inv013=True,
        clawback_neutral=True,
        unresolved_bears=True,
        bounded_quarantine=True,
        adr014=True,
        muxed_identity=True,
        weighs_unattributed=True,
        states_provenance=True,
        scoped_absence=True,
    ),
    SUBSCRIPTION_ENGINE_0_8_0: _Semantics(
        inv013=True,
        clawback_neutral=True,
        unresolved_bears=True,
        bounded_quarantine=True,
        adr014=True,
        muxed_identity=True,
        weighs_unattributed=True,
        states_provenance=True,
    ),
    SUBSCRIPTION_ENGINE_0_7_0: _Semantics(
        inv013=True,
        clawback_neutral=True,
        unresolved_bears=True,
        bounded_quarantine=True,
        adr014=True,
        muxed_identity=True,
    ),
    SUBSCRIPTION_ENGINE_0_6_0: _Semantics(
        inv013=True,
        clawback_neutral=True,
        unresolved_bears=True,
        bounded_quarantine=True,
        adr014=True,
    ),
    SUBSCRIPTION_ENGINE_0_5_0: _Semantics(
        inv013=True, clawback_neutral=True, unresolved_bears=True, bounded_quarantine=True
    ),
    SUBSCRIPTION_ENGINE_0_4_0: _Semantics(
        inv013=True, clawback_neutral=True, unresolved_bears=True
    ),
    SUBSCRIPTION_ENGINE_0_3_0: _Semantics(inv013=True, clawback_neutral=True),
    SUBSCRIPTION_ENGINE_0_2_0: _Semantics(inv013=True, clawback_neutral=False),
    SUBSCRIPTION_ENGINE_0_1_0: _Semantics(inv013=False, clawback_neutral=False),
}
ASSUMPTIONS_0_1_0 = (
    "Evaluation uses only the snapshot members; nothing is fetched or recomputed from sources.",
    "Per source record the highest revision wins; identical redeliveries count once.",
    "Per source and fact type the most recently recorded coverage certificate applies.",
    "Comparisons are exact in atoms; no tolerance, fees or rounding.",
    "The evaluation clock is recorded; no control in this profile uses deadlines.",
)
ASSUMPTIONS_0_2_0 = (
    *ASSUMPTIONS_0_1_0,
    "Chain effects are never evidence of fulfilment; one linked to the operation or "
    "involving an address approved for its account makes an equal or short token "
    "comparison UNKNOWN; an excess of linked deliveries stays a FAIL.",
    "Quarantined records count only when the profile scopes them to records bearing on the "
    "operation; then a record counts if its parties are unknown or include such an address.",
)
ASSUMPTIONS_0_3_0 = (
    *ASSUMPTIONS_0_2_0,
    "A clawback (a forced withdrawal by the issuer) never bears on the delivery comparison: "
    "it can neither complete nor undo a linked delivery, and by itself proves no breach.",
)
# 0.4.0 states the clawback roles precisely and adds the rule on
# unidentified participants.
ASSUMPTIONS_0_4_0 = (
    *ASSUMPTIONS_0_2_0,
    "A clawback (units taken from the holder under the asset's clawback authority) never "
    "bears on the delivery comparison: it can neither complete nor undo a linked delivery, "
    "and by itself proves no breach.",
    "A chain effect naming a participant the evidence does not identify (e.g. a claimable "
    "balance whose history could not be read) cannot be shown foreign: it bears on the "
    "operation like one involving an approved address.",
)
# 0.5.0 bounds a quarantine to the comparisons its records could change.
ASSUMPTIONS_0_5_0 = (
    *ASSUMPTIONS_0_4_0,
    "A quarantined record bearing on the operation (by its parties, or because an "
    "ExecutionLink names its ledger operation as this operation's execution) leaves UNKNOWN "
    "only what resolving it could change: a record on the ledger operation of a counted "
    "delivery, or on an unknown one, any token comparison; any other record, unless it is an "
    "identified clawback not linked to this operation, an equal or short one, since it could "
    "be a further delivery. An "
    "identified clawback whose correspondence is incomplete changes no delivery comparison; "
    "an excess of linked deliveries stays a FAIL. The records stay quarantined.",
)
# 0.6.0: the assumptions depend on whether the profile declares a quarantine
# policy; both variants share the integrity rules.
_INTEGRITY_0_6_0 = (
    "For a chain coverage whose quarantine the profile scopes, a certificate is not replaced "
    "for being older: it stays active until a later certificate of the same source, network, "
    "target, account and asset, with a route including its route and a ledger range "
    "containing its range, explicitly supersedes it. The quarantines of the active "
    "certificates add up; the coverage is met by an active certificate.",
    "The parties of a quarantined record are contrasted with the snapshot's observations of "
    "its ledger operation: a relevant or unresolved party the record omits makes it bear on "
    "the operation whatever its addresses say.",
    "A party that cannot be or is not approved for the account (a contract, an account "
    "without IdentityLink) is not thereby foreign: an effect or record naming only such "
    "parties is outside the declared scope, unless an ExecutionLink, an explicit "
    "association or an unresolved participant brings it in.",
)
# The two variants for a profile that scopes its chain quarantine: the 0.1.0 assumption on
# the most recent certificate holds only for the other sources, and how the chain coverage
# is met depends on the declared policy.
_LATEST_EXCEPT_CHAIN = (
    "Per source and fact type the most recently recorded coverage certificate applies, "
    "except for the chain coverage whose quarantine the profile scopes."
)


def _scoped(coverage: str) -> tuple[str, ...]:
    base = tuple(
        _LATEST_EXCEPT_CHAIN if a == ASSUMPTIONS_0_1_0[2] else a for a in ASSUMPTIONS_0_4_0
    )
    first = (
        "For the chain coverage whose quarantine the profile scopes, a certificate is not "
        "replaced for being older: it stays active until a later certificate of the same "
        "source, network, target, account and asset, with a route including its route and a "
        "ledger range containing its range, no gaps and no lower level, explicitly "
        "supersedes it. The quarantines of the active certificates add up; " + coverage
    )
    return (*base, first, *_INTEGRITY_0_6_0[1:])


ASSUMPTIONS_POLICY = (
    *_scoped("the coverage is met by an active certificate."),
    "The profile declares its quarantine policy: a quarantined record never credits "
    "fulfilment, and it makes UNKNOWN only what resolving it could change. A record on the "
    "ledger operation of a counted delivery, or on one the snapshot does not support, "
    "leaves every token comparison UNKNOWN; an identified clawback the snapshot supports, on "
    "another ledger operation and not linked to this operation, changes none; any other "
    "record leaves an equal or short comparison UNKNOWN and an excess a FAIL. A record's "
    "nature and ledger operation are relied on only when the snapshot's observations of "
    "that operation (network, asset, transaction, parties, execution) support them; "
    "otherwise it is weighed as unresolved. The records stay quarantined.",
)
ASSUMPTIONS_NO_POLICY = (
    *_scoped("the coverage is judged on the most recent certificate, as the profile promised."),
    "The profile declares no quarantine policy: a quarantined record bearing on the "
    "operation (its parties unknown or including an approved address, or named by any "
    "ExecutionLink) makes every token comparison UNKNOWN, as in the profile's promise.",
)
# A profile that does not scope the on-chain quarantine: every quarantined record of the
# applicable certificate blocks, as before; only the integrity rules are stated.
ASSUMPTIONS_UNSCOPED = (*ASSUMPTIONS_0_4_0, *_INTEGRITY_0_6_0)
ASSUMPTIONS = ASSUMPTIONS_POLICY
_ASSUMPTIONS = {
    SUBSCRIPTION_ENGINE_0_5_0: ASSUMPTIONS_0_5_0,
    SUBSCRIPTION_ENGINE_0_4_0: ASSUMPTIONS_0_4_0,
    SUBSCRIPTION_ENGINE_0_3_0: ASSUMPTIONS_0_3_0,
    SUBSCRIPTION_ENGINE_0_2_0: ASSUMPTIONS_0_2_0,
    SUBSCRIPTION_ENGINE_0_1_0: ASSUMPTIONS_0_1_0,
}


# 0.7.0: muxed sub-accounts and transaction memos.
MEMO_ASSUMPTION = (
    "A transaction memo is context of the whole transaction: it never links an observation "
    "to an operation (only an explicit ExecutionLink does), and a memo id is never read as a "
    "muxed sub-account id, whatever their numeric values."
)
ASSUMPTION_MUXED_DECLARED = (
    "A movement to a muxed sub-account (M) is attributed to the account only by an "
    "IdentityLink naming that M address; a link to its base account does not attribute it, "
    "nor does a link to another sub-account. A muxed movement no link attributes to the "
    "account is never counted; it bears on the operation when an ExecutionLink names it, "
    "when it names an approved address, or a sub-account whose base account is approved "
    "(its holder is then unresolved, and an unresolved holder is never shown foreign), or "
    "always when the account is unknown: an equal or short token comparison is then UNKNOWN, "
    "an excess of counted deliveries stays a FAIL. A movement whose receiver is not muxed is "
    "read by its receiver whatever its sender's sub-account; a counted delivery still comes "
    "from the issuer's base account."
)
ASSUMPTION_MUXED_UNDECLARED = (
    "The profile does not declare how a muxed sub-account is attributed: a movement naming "
    "one is never counted, and it weighs as the quarantined record the earlier mapping made "
    "of it. Bearing on the operation (an ExecutionLink, an approved address or a "
    "sub-account of an approved base account, or an unknown account), it makes an equal or "
    "short token comparison UNKNOWN, and an excess too unless the profile declares its "
    "quarantine policy; under a profile that does not scope the on-chain quarantine, any "
    "muxed movement of the source makes every token comparison UNKNOWN. A quarantined "
    "record naming a muxed address bears through that address or its base account, no "
    "longer for every operation (records of the earlier mapping named base accounts only)."
)


# 0.8.0 (closure of M2 and B2).
ASSUMPTION_PROVENANCE = (
    "Only evidence produced by a mapping the profile admits for its source is evaluated, and "
    "the evaluation states those mappings (evidence_mapping_refs); evidence produced by "
    "another mapping is refused, never relabelled nor read with another mapping."
)
_WEIGHED = (
    "A movement never counted because its destination is not attributed weighs only by "
    "what it could change. Linked to the operation by an ExecutionLink, it is a further "
    "execution: an equal or short token comparison is UNKNOWN and an excess of counted "
    "deliveries stays a FAIL. Without a link it can never count; it bears only while no "
    "execution is linked to the operation (it could be the delivery whose link is "
    "missing) and it names an approved address or a sub-account of an approved base "
    "account, or always when the account is unknown. Two readings of one execution share "
    "its record key: identical, they count once; different, they are a source conflict"
)
ASSUMPTION_MUXED_DECLARED_0_8_0 = (
    "A movement to a muxed sub-account (M) is attributed to the account only by an "
    "IdentityLink naming that M address; a link to its base account or to another "
    "sub-account does not attribute it. "
    + _WEIGHED
    + ", nor, unlinked, when an IdentityLink attributes its sub-account to another account. "
    "Under a profile that declares its quarantine policy, a linked delivery to an address "
    "without an approved link is weighed the same way. A movement whose receiver is not "
    "muxed is read by its receiver whatever its sender's sub-account; a counted delivery "
    "still comes from the issuer's base account."
)
ASSUMPTION_MUXED_UNDECLARED_0_8_0 = (
    "The profile does not declare how a muxed sub-account is attributed: a movement naming "
    "one is never counted. "
    + _WEIGHED
    + "; an excess is a FAIL only where the profile declares its quarantine policy. Under a "
    "profile that does not scope the on-chain quarantine, any muxed movement of the source "
    "makes every token comparison UNKNOWN. A quarantined record naming a muxed address bears "
    "through that address or its base account."
)


ASSUMPTION_SCOPED_ABSENCE = (
    "The profile declares that an absence on the chain source needs a sufficient chain scope "
    "(INV-015): an equal or short token comparison is concluded only with a chain "
    "certificate that declares its scope (network and asset of a representation of the "
    "instrument, a route that includes the SAC events, what it leaves out of sight, and every "
    "address approved for the account); "
    "otherwise it is UNKNOWN (INSUFFICIENT_COVERAGE). Missing exclusions are never read as "
    "complete coverage. An excess of linked deliveries stays a FAIL: further deliveries could "
    "only add to it."
)


ASSUMPTION_COMPLETENESS = (
    "The profile declares that affirming the observed chain records complete needs coverage "
    "(INV-015 M3, B1, B3): observing a delivery needs no complete coverage, affirming that "
    "the deliveries observed are all of them does. The chain scope must be coherent with what "
    "its route and role observe (an empty not_covered never covers what the route does not "
    "see) and belong to the representation of the movements evaluated (network, asset code "
    "and issuer), reading the addresses approved on its network; another representation of "
    "the profile never stands in for it."
)


def _assumptions(engine_ref: str, profile: OperationProfile) -> tuple[str, ...]:
    if engine_ref in _ASSUMPTIONS:
        return _ASSUMPTIONS[engine_ref]
    if _declares_policy(profile):
        base = ASSUMPTIONS_POLICY
    elif any(r.quarantine_scope is not None for r in profile.coverage_requirements):
        base = ASSUMPTIONS_NO_POLICY
    else:
        base = ASSUMPTIONS_UNSCOPED
    semantics = _SEMANTICS.get(engine_ref)
    if semantics is not None and not semantics.muxed_identity:
        return base  # 0.6.0
    declared = profile.correlation.muxed_account_link is not None
    if semantics is not None and not semantics.weighs_unattributed:  # 0.7.0
        muxed = ASSUMPTION_MUXED_DECLARED if declared else ASSUMPTION_MUXED_UNDECLARED
        return (*base, muxed, MEMO_ASSUMPTION)
    muxed = ASSUMPTION_MUXED_DECLARED_0_8_0 if declared else ASSUMPTION_MUXED_UNDECLARED_0_8_0
    found = (*base, muxed, MEMO_ASSUMPTION, ASSUMPTION_PROVENANCE)
    scoped = semantics is not None and semantics.scoped_absence
    if scoped and any(r.absence_needs_chain_scope for r in profile.coverage_requirements):
        found = (*found, ASSUMPTION_SCOPED_ABSENCE)
    complete = semantics is not None and semantics.completeness
    if complete and any(r.completeness_needs_coverage for r in profile.coverage_requirements):
        found = (*found, ASSUMPTION_COMPLETENESS)
    return found


def _states_provenance(engine_ref: str) -> bool:
    return engine_ref in _SEMANTICS and _SEMANTICS[engine_ref].states_provenance


def _declares_policy(profile: OperationProfile | RedemptionProfile) -> bool:
    return any(r.quarantine_policy is not None for r in profile.coverage_requirements)


# The uncovered effects that could add a delivery to the account the
# certificate reads. None can: the clawback of a claimable balance and a third party's claim
# of one created by the account are outflows. The account's own claim is an inflow seen only
# through the SAC events, which is why an absence also needs a route that includes them
# (``ABSENCE_ROUTES``).
SUBSCRIPTION_UNSEEN: tuple[UncoveredEffect, ...] = ()


@dataclass
class _Deferred:
    """Quarantined records bearing on the operation, judged once the operands are known."""

    label: str  # the certificate(s) they come from
    quarantined: int  # records quarantined in them
    refs: tuple[str, ...]
    additive: list[QuarantinedRecord]  # could be a further delivery
    neutral: list[QuarantinedRecord]  # cannot change the comparison
    notes: list[str] = field(default_factory=list)  # classifications not relied on (0.6.0)


class _Evaluator:
    def __init__(
        self,
        inputs: EvaluationInputs,
        *,
        inv013: bool = True,
        clawback_neutral: bool = True,
        unresolved_bears: bool = True,
        bounded_quarantine: bool = True,
        adr014: bool = True,
        muxed_identity: bool = True,
        weighs_unattributed: bool = True,
        scoped_absence: bool = True,
        completeness: bool = True,
    ) -> None:
        """The flags select the semantics of an engine label (``_Semantics``); retired labels
        run as compatibility implementations, not as their historical code."""
        self.muxed_identity = muxed_identity
        self.weighs_unattributed = weighs_unattributed
        self.scoped_absence = scoped_absence
        self.completeness = completeness
        # Why the chain coverage cannot show that no further delivery happened
        # (and the certificate judged), when the profile requires a sufficient chain scope.
        self.unscoped: tuple[str, str] | None = None
        # The certificates meeting the chain coverage (the active ones under a declared
        # policy, else the most recent): those an absence is judged on.
        self.meeting_chain: list[CoverageCertificate] = []
        # Muxed movements set aside (never counted) that bear on the operation (0.7.0).
        self.muxed_bearing: tuple[str, ...] = ()
        self.clawback_neutral = clawback_neutral
        self.unresolved_bears = unresolved_bears
        self.bounded_quarantine = bounded_quarantine
        self.adr014 = adr014
        # Per fact type: records bearing on the operation, judged once the operands are
        # known.
        self.deferred: dict[FactType, _Deferred] = {}
        assert isinstance(inputs.profile, OperationProfile)
        self.inv013 = inv013
        self.inputs = inputs
        self.snapshot = inputs.snapshot
        self.profile: OperationProfile = inputs.profile
        self.members = [inputs.observations[i] for i in self.snapshot.observation_ids]
        self.certificates = [inputs.coverage[i] for i in self.snapshot.coverage_ids]
        self.links = [inputs.identity_links[i] for i in self.snapshot.identity_link_ids]
        self.views: dict[FactType, FactView] = {}
        self.coverage_used: dict[FactType, str] = {}

    # ------------------------------------------------------------- evidence

    def _authoritative(self, fact_type: FactType) -> list[Observation]:
        source = self.profile.authority_for(fact_type)
        return [
            o
            for o in self.members
            if o.fact_type == fact_type
            and o.source.source_id == source
            and o.instrument_id == self.profile.instrument.instrument_id
        ]

    def view(self, fact_type: FactType) -> FactView:
        if fact_type not in self.views:
            self.views[fact_type] = self._build_view(fact_type)
        return self.views[fact_type]

    def _build_view(self, fact_type: FactType) -> FactView:
        operation = self.snapshot.operation_ref
        candidates = self._authoritative(fact_type)
        if fact_type != "token_movement":
            linked = [o for o in candidates if o.operation_ref == operation]
            single = {
                "cash_settled": "multiple_cash_payments",
                "units_registered": "partial_fill",
            }.get(fact_type, "multiple_orders")
            return institutional_view(fact_type, linked, single)
        return self._token_view(candidates)

    def _token_view(self, candidates: Sequence[Observation]) -> FactView:
        operation = self.snapshot.operation_ref
        representations = {r.representation_id: r for r in self.profile.representations}
        order = self.view("order_accepted")
        account = (
            order.observations[0].payload.account_ref
            if (
                order.status == "asserted"
                and isinstance(order.observations[0].payload, OrderPayload)
            )
            else None
        )
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
        movements = [
            (o, o.payload) for o in asserted if isinstance(o.payload, TokenMovementPayload)
        ]
        successful = [(o, p) for o, p in movements if p.chain.tx_successful]
        declared = self.profile.correlation.muxed_account_link is not None

        def linked_address(o: Observation, p: TokenMovementPayload) -> bool:
            # The receiver as the ledger names it: a muxed one is its M address (0.7.0),
            # which only a link to that M address approves.
            receiver = movement_receiver(p) if self.muxed_identity else p.to_address
            return any(
                link.account_ref == account
                and link.address == receiver
                and link.network == p.chain.network
                and link.valid_from <= o.valid_time
                and (link.valid_to is None or o.valid_time < link.valid_to)
                for link in self.links
            )

        muxed = [o.observation_id for o, p in successful if is_muxed(p)]
        linked_aside: tuple[str, ...] = ()
        if muxed and not self.muxed_identity:
            return FactView(
                "token_movement",
                "unsupported",
                (),
                tuple(muxed),
                "unsupported: muxed_account (this engine never received a muxed movement; "
                "its adapter quarantined them)",
            )
        if self.muxed_identity:
            # Attributable: not muxed, or, under a profile that declares it, a movement whose
            # receiver is not muxed (a muxed sender is a sub-account of the issuer, whose
            # base account issues) or is a sub-account an IdentityLink of the account names.
            # Anything else is set aside: never counted, weighed only by what it could
            # change.
            # 0.8.0: under a profile that declares its quarantine policy, a linked delivery to
            # an address without an approved link is weighed the same way.
            weigh_unapproved = self.weighs_unattributed and _declares_policy(self.profile)

            def attributable(o: Observation, p: TokenMovementPayload) -> bool:
                return not is_muxed(p) or (
                    declared and (p.to_muxed_id is None or linked_address(o, p))
                )

            aside = [
                (o, p)
                for o, p in successful
                if not attributable(o, p)
                or (
                    weigh_unapproved
                    and o.operation_ref == operation
                    and p.from_address == representations[o.representation_id or ""].issuer
                    and not linked_address(o, p)
                )
            ]
            kept = {o.observation_id for o, _ in aside}
            successful = [(o, p) for o, p in successful if o.observation_id not in kept]
            self.muxed_bearing = self._muxed_bearing(aside, account, successful)
            if self.weighs_unattributed:
                linked_aside = tuple(
                    o.observation_id for o, _ in aside if o.operation_ref == operation
                )
        explicit = [(o, p) for o, p in successful if o.operation_ref == operation]
        if not explicit and linked_aside:
            return FactView(
                "token_movement",
                "ambiguous",
                (),
                linked_aside,
                "the execution(s) linked to the operation name a destination no IdentityLink "
                "attributes to the account (an unattributed muxed sub-account or an address "
                "without an approved link); they are never counted",
            )
        if not explicit:
            unlinked = [
                o.observation_id
                for o, p in successful
                if o.operation_ref is None and linked_address(o, p)
            ]
            if unlinked:
                return FactView(
                    "token_movement",
                    "ambiguous",
                    (),
                    tuple(unlinked),
                    "movements to the investor address lack an execution link",
                )
            if self.muxed_bearing:
                return FactView(
                    "token_movement",
                    "ambiguous",
                    (),
                    self.muxed_bearing,
                    "a movement naming a muxed sub-account that no IdentityLink attributes to "
                    "the account bears on the operation (linked to it, or naming an approved "
                    "address or a sub-account of one); it is never counted",
                )
            return FactView(
                "token_movement", "absent", (), (), "no successful movement linked to the operation"
            )
        ids = tuple(o.observation_id for o, _ in explicit)
        for o, p in explicit:
            rep = representations[o.representation_id or ""]
            if p.from_address != rep.issuer:
                return FactView(
                    "token_movement", "unsupported", (), ids, "unsupported: distributor_delivery"
                )
            if not linked_address(o, p):
                return FactView(
                    "token_movement",
                    "ambiguous",
                    (),
                    ids,
                    "destination address has no approved identity link",
                )
        return FactView("token_movement", "asserted", tuple(o for o, _ in explicit), ids, "")

    # ------------------------------------------------------------- coverage

    def _check(self, fact_type: FactType, order_time: datetime | None) -> None:
        view = self.view(fact_type)
        reason_by_status: dict[str, ReasonCode] = {
            "conflict": "SOURCE_CONFLICT",
            "withdrawn": "LOSS_OF_SUPPORT",
            "ambiguous": "AMBIGUOUS_MATCH",
            "unsupported": "UNSUPPORTED_CAPABILITY",
        }
        if view.status in reason_by_status:
            raise Undecided(reason_by_status[view.status], f"{fact_type}: {view.detail}", view.refs)
        source = self.profile.authority_for(fact_type)
        requirement = next(
            r
            for r in self.profile.coverage_requirements
            if r.source_id == source and fact_type in r.fact_types
        )
        certs = [
            c
            for c in self.certificates
            if c.source_id == source
            and fact_type in c.fact_types
            and c.instrument_id == self.profile.instrument.instrument_id
        ]
        if (
            self.adr014
            and fact_type == "token_movement"
            and requirement.quarantine_scope is not None
        ):
            latest = self._active_check(certs, requirement, view, order_time)
        else:
            latest = max(certs, key=lambda c: (c.recorded_at, c.coverage_id)) if certs else None
            self._latest_quarantine(latest, requirement, fact_type, view)
        deferred = self.deferred.get(fact_type)
        if deferred is not None and deferred.additive and view.status != "asserted":
            # No delivery to compare: a record that could be one leaves it undecided, as
            # before 0.5.0 (the quarantine is reported ahead of the missing evidence).
            raise self._quarantine_undecided(fact_type, deferred)
        if view.status == "absent":
            raise Undecided("MISSING_EVIDENCE", f"{fact_type}: {view.detail}")
        if latest is None:
            raise Undecided(
                "INSUFFICIENT_COVERAGE",
                f"{fact_type}: no coverage certificate from {source}",
                view.refs,
            )
        problems = self._coverage_problems(latest, requirement, view, order_time)
        if problems and deferred is not None and deferred.additive:
            # The reason order of earlier engines: the quarantine comes first.
            raise self._quarantine_undecided(fact_type, deferred)
        if problems:
            raise Undecided(
                "INSUFFICIENT_COVERAGE",
                f"{fact_type}: {latest.coverage_id} " + "; ".join(problems),
                (*view.refs, latest.coverage_id),
            )
        self.coverage_used[fact_type] = latest.coverage_id
        if (
            fact_type == "token_movement"
            and self.scoped_absence
            and requirement.absence_needs_chain_scope
        ):
            candidates = self.meeting_chain or [latest]
            if self.completeness and requirement.completeness_needs_coverage:
                # Every certificate of the source meeting the coverage (one per
                # representation or address may be needed), not only the most recent.
                candidates = (
                    self.meeting_chain
                    or [
                        c
                        for c in certs
                        if not self._coverage_problems(c, requirement, view, order_time)
                    ]
                    or [latest]
                )
                # B1 and B3: a coherent scope of its own for every representation of the
                # instrument, since an unseen delivery could be in any of them.
                shortfall, usable = strict_absence_scope(
                    candidates,
                    list(self.profile.representations),
                    addresses_by_network(self.links, self._account()),
                    SUBSCRIPTION_UNSEEN,
                )
            else:
                shortfall, usable = absence_scope(
                    candidates,
                    self.profile.representations,
                    self._relevant(self._account()),
                    SUBSCRIPTION_UNSEEN,
                )
            if shortfall is not None:
                self.unscoped = (shortfall, latest.coverage_id)
            else:
                # Cite a certificate that can show the absence, never one that cannot.
                self.coverage_used[fact_type] = usable[0].coverage_id

    def _coverage_problems(
        self,
        certificate: CoverageCertificate,
        requirement: CoverageRequirement,
        view: FactView,
        order_time: datetime | None,
    ) -> list[str]:
        problems = []
        if certificate.gaps:
            problems.append("declares gaps")
        if LEVEL_RANK[certificate.level] < LEVEL_RANK[requirement.min_level]:
            problems.append(f"level {certificate.level} < {requirement.min_level}")
        own_times = [o.valid_time for o in view.observations]
        if order_time is None and not own_times:
            return problems  # nothing to cover yet; reported as missing evidence
        start = order_time if order_time is not None else min(own_times)
        if certificate.interval.start > start or certificate.interval.end < self.snapshot.valid_at:
            problems.append("interval does not cover order time to valid_at")
        return problems

    def _latest_quarantine(
        self,
        latest: CoverageCertificate | None,
        requirement: CoverageRequirement,
        fact_type: FactType,
        view: FactView,
    ) -> None:
        """The quarantine of the most recent certificate (every engine before 0.6.0, and
        every source whose quarantine the profile does not scope)."""
        if latest is None or latest.records_quarantined == 0:
            return
        scoped = (
            self.inv013
            and requirement.quarantine_scope == "records_bearing_on_operation"
            and latest.quarantined_records is not None
        )
        if not scoped:
            raise Undecided(
                "QUARANTINED_INPUT",
                f"{fact_type}: {latest.records_quarantined} record(s) quarantined "
                f"in {latest.coverage_id}",
                (latest.coverage_id,),
            )
        if self.bounded_quarantine and fact_type == "token_movement":
            self._bound_quarantine(latest, view)
            return
        affecting = self._affecting(latest, fact_type)
        if affecting:
            raise Undecided(
                "QUARANTINED_INPUT",
                f"{fact_type}: {affecting} of {latest.records_quarantined} quarantined "
                f"record(s) in {latest.coverage_id} involve the operation's addresses "
                f"or unknown parties",
                (latest.coverage_id,),
            )

    def _active_check(
        self,
        certs: list[CoverageCertificate],
        requirement: CoverageRequirement,
        view: FactView,
        order_time: datetime | None,
    ) -> CoverageCertificate | None:
        """0.6.0: the quarantine of every active chain certificate, and the
        certificate that meets the coverage (the most recent active one that does, else the
        most recent active one, whose problems are reported)."""
        coverage = active_coverage(certs)
        if not coverage.active:
            return None
        label = ", ".join(c.coverage_id for c in coverage.active)
        quarantined = sum(c.records_quarantined for c in coverage.active)
        unlisted = [
            c for c in coverage.active if c.records_quarantined and c.quarantined_records is None
        ]
        if unlisted:
            raise Undecided(
                "QUARANTINED_INPUT",
                f"token_movement: {quarantined} record(s) quarantined in {label}, "
                f"{', '.join(c.coverage_id for c in unlisted)} without their parties",
                tuple(c.coverage_id for c in coverage.active),
            )
        if quarantined:
            if requirement.quarantine_policy is not None:
                self._declared_quarantine(coverage, label, quarantined, view)
            else:
                self._promised_quarantine(coverage, label, quarantined)
        newest_first = sorted(coverage.active, key=lambda c: (c.recorded_at, c.coverage_id))[::-1]
        if requirement.quarantine_policy is None:
            # Without a declared policy the coverage is the most recent certificate's, as
            # the profile promised (it is always active: nothing earlier can supersede it);
            # only the quarantine is the union of the active ones.
            return newest_first[0]
        meeting = [
            c for c in newest_first if not self._coverage_problems(c, requirement, view, order_time)
        ]
        self.meeting_chain = meeting
        return meeting[0] if meeting else newest_first[0]

    def _evidence(self) -> ChainEvidence:
        return chain_evidence(
            self.members,
            self.profile.authority_for("token_movement"),
            self.profile.instrument.instrument_id,
            {r.network for r in self.profile.representations},
        )

    def _supported(self, coverage: ActiveCoverage) -> list[tuple[QuarantinedRecord, RecordSupport]]:
        """The records of the active certificates that bear on the operation: by their
        parties, by an ExecutionLink to it, or by a party of their ledger operation the
        snapshot shows and they omit; each with what the snapshot supports of it."""
        relevant = self._relevant(self._account())
        evidence = self._evidence()
        operation = self.snapshot.operation_ref
        found = []
        for _certificate, record in coverage.records():
            support = record_support(record, evidence, relevant)
            linked = operation in (record.execution_links or ())
            if self._by_address(record, relevant) or linked or support.bears:
                found.append((record, support))
        return found

    def _by_address(self, record: QuarantinedRecord, relevant: set[str] | None) -> bool:
        if self.muxed_identity:
            return by_address_muxed(record, relevant)
        return by_address(record, relevant)

    def _promised_quarantine(self, coverage: ActiveCoverage, label: str, quarantined: int) -> None:
        """A profile without a quarantine policy (fund-subscription-testnet@1.1.0): a record
        bearing on the operation blocks every token comparison, as that profile promised
        (the rule of 0.4.0, an ExecutionLink making a record bear on every operation), with
        the record's parties contrasted with the snapshot."""
        relevant = self._relevant(self._account())
        evidence = self._evidence()
        affecting = [
            record
            for _certificate, record in coverage.records()
            if record.execution_links is not None
            or self._by_address(record, relevant)
            or record_support(record, evidence, relevant).bears
        ]
        if affecting:
            raise Undecided(
                "QUARANTINED_INPUT",
                f"token_movement: {len(affecting)} of {quarantined} quarantined record(s) in "
                f"{label} involve the operation's addresses or unknown parties",
                tuple(c.coverage_id for c in coverage.active),
            )

    def _declared_quarantine(
        self, coverage: ActiveCoverage, label: str, quarantined: int, view: FactView
    ) -> None:
        """The quarantine policy the profile declares (fund-subscription-testnet@1.2.0): the
        per-control split on the records of every active certificate, relying on a record's
        nature and ledger operation only where the snapshot supports them."""
        refs = tuple(c.coverage_id for c in coverage.active)
        delivered: set[str] = set()
        for o in view.observations:
            if isinstance(o.payload, TokenMovementPayload):
                delivered.add(f"{o.payload.chain.tx_hash}:{o.payload.chain.operation_index}")
        operation = self.snapshot.operation_ref
        additive: list[QuarantinedRecord] = []
        neutral: list[QuarantinedRecord] = []
        undermining: list[QuarantinedRecord] = []
        notes: list[str] = []
        for record, support in self._supported(coverage):
            notes.extend(f"{record.locator}: {note}" for note in support.notes)
            if support.operation is None or support.operation in delivered:
                undermining.append(record)
            elif support.clawback and operation not in (record.execution_links or ()):
                neutral.append(record)
            else:
                additive.append(record)
        if undermining:
            raise Undecided(
                "QUARANTINED_INPUT",
                f"token_movement: {len(undermining)} of {quarantined} quarantined record(s) in "
                f"{label} bear on the operation and are on the ledger operation of a counted "
                "delivery, or on one the snapshot does not support: the counted deliveries "
                "are in doubt" + "".join(f"; {note}" for note in notes),
                refs,
            )
        if additive or neutral:
            self.deferred["token_movement"] = _Deferred(
                label, quarantined, refs, additive, neutral, notes
            )

    def _bound_quarantine(self, certificate: CoverageCertificate, view: FactView) -> None:
        """Split the quarantined records bearing on the operation by what resolving them
        could change in the delivery comparison (0.5.0).

        - On the ledger operation of a counted delivery, or on an unknown one: the counted
          deliveries themselves are in doubt, in either direction. UNKNOWN now.
        - An identified clawback (``nature``) on another ledger operation, not linked to
          this operation by an ExecutionLink (a link would contradict the chain): the event or
          operation identifies a clawback, its asset and holder are those of the record,
          and only its Classic/SAC correspondence or origin is incomplete. A clawback can
          neither complete nor undo a delivery (0.3.0 on), so it changes no comparison.
        - Anything else (a movement of unresolved nature, execution, asset or participants;
          a contradiction on whether it was a clawback; an identified movement no profile
          admits): it could be a further delivery. Equal or short comparisons become
          UNKNOWN; an excess stays a FAIL, since nothing undoes a delivery.

        A record from an earlier certificate has no ``nature`` and no ``chain_operation``:
        it falls in the first group, as conservative as before.
        """
        account = self._account()
        records = quarantine_considered(
            certificate, self._relevant(account), self.snapshot.operation_ref
        )
        delivered = {
            f"{o.payload.chain.tx_hash}:{o.payload.chain.operation_index}"
            for o in view.observations
            if isinstance(o.payload, TokenMovementPayload)
        }
        undermining = [
            r for r in records if r.chain_operation is None or r.chain_operation in delivered
        ]
        if undermining:
            raise Undecided(
                "QUARANTINED_INPUT",
                f"token_movement: {len(undermining)} of {certificate.records_quarantined} "
                f"quarantined record(s) in {certificate.coverage_id} bear on the operation "
                "and are on the ledger operation of a counted delivery or on an unknown one: "
                "the counted deliveries are in doubt",
                (certificate.coverage_id,),
            )
        operation = self.snapshot.operation_ref

        def neutral_clawback(r: QuarantinedRecord) -> bool:
            # An ExecutionLink naming an identified clawback as this operation's execution
            # contradicts the chain: the linked execution may be another one, so it is
            # weighed as a possible delivery, not as a neutral clawback.
            return r.nature == "clawback_identified" and operation not in (r.execution_links or ())

        additive = [r for r in records if not neutral_clawback(r)]
        neutral = [r for r in records if neutral_clawback(r)]
        if records:
            self.deferred["token_movement"] = _Deferred(
                certificate.coverage_id,
                certificate.records_quarantined,
                (certificate.coverage_id,),
                additive,
                neutral,
            )

    @staticmethod
    def _quarantine_undecided(fact_type: FactType, deferred: _Deferred) -> Undecided:
        records = deferred.additive
        return Undecided(
            "QUARANTINED_INPUT",
            f"{fact_type}: {len(records)} of {deferred.quarantined} quarantined "
            f"record(s) in {deferred.label} bear on the operation and could be a "
            "further delivery ("
            + ", ".join(sorted({r.nature or "unresolved" for r in records}))
            + ")"
            + "".join(f"; {note}" for note in deferred.notes),
            deferred.refs,
        )

    def _muxed_bearing(
        self,
        aside: Sequence[tuple[Observation, TokenMovementPayload]],
        account: str | None,
        counted: Sequence[tuple[Observation, TokenMovementPayload]] = (),
    ) -> tuple[str, ...]:
        """Set-aside movements bearing on the operation: named by an
        ExecutionLink of it, or naming an approved address or a sub-account of an approved
        base account; with the account unknown, every one. Under a profile that does not
        scope its on-chain quarantine every one bears, as every quarantined record did.

        0.8.0 tells apart what the snapshot can show. Another representation of an
        execution never reaches here: representations share the canonical record key
        (``<tx>:<op>:<ordinal>``), so identical ones count once and differing ones are a
        source conflict (``resolve_records``). An unlinked movement beside an execution
        linked to the operation can never count, as an unlinked movement to an approved G
        address beside a linked delivery; an unlinked one whose sub-account an IdentityLink
        attributes to another account is foreign. What remains is either a further
        execution linked to the operation or one whose relevance cannot be resolved."""
        relevant = self._relevant(account)
        source = self.profile.authority_for("token_movement")
        requirement = next(
            r
            for r in self.profile.coverage_requirements
            if r.source_id == source and "token_movement" in r.fact_types
        )
        unscoped = requirement.quarantine_scope is None
        operation = self.snapshot.operation_ref
        if unscoped or not self.weighs_unattributed:
            return tuple(
                sorted(
                    o.observation_id
                    for o, p in aside
                    if unscoped
                    or o.operation_ref == operation
                    or bears_by_address(movement_addresses(p), relevant)
                )
            )
        linked_any = any(o.operation_ref == operation for o, _ in (*aside, *counted))
        bearing = []
        for o, p in aside:
            if o.operation_ref == operation:
                bearing.append(o.observation_id)  # a further execution of the operation
                continue
            if linked_any:
                continue  # unlinked beside a linked execution: it can never count
            if self._attributed_elsewhere(o, p, account):
                continue  # demonstrably foreign
            # Could it be the delivery whose link is missing? As for a G address, by its
            # receiver: a sub-account of an approved base, or an approved address.
            if bears_by_address([movement_receiver(p)], relevant):
                bearing.append(o.observation_id)  # relevance unresolved
        return tuple(sorted(bearing))

    def _attributed_elsewhere(
        self, o: Observation, p: TokenMovementPayload, account: str | None
    ) -> bool:
        """An IdentityLink, valid at the movement, attributes its muxed receiver to another
        account, under a profile that declares how a muxed sub-account is attributed."""
        if (
            account is None
            or p.to_muxed_id is None
            or self.profile.correlation.muxed_account_link is None
        ):
            return False
        receiver = movement_receiver(p)
        return any(
            link.address == receiver
            and link.account_ref != account
            and link.network == p.chain.network
            and link.valid_from <= o.valid_time
            and (link.valid_to is None or o.valid_time < link.valid_to)
            for link in self.links
        )

    def _account(self) -> str | None:
        order = self.view("order_accepted")
        payload = order.observations[0].payload if order.status == "asserted" else None
        return payload.account_ref if isinstance(payload, OrderPayload) else None

    def _relevant(self, account: str | None) -> set[str] | None:
        """Addresses approved for the order's account (any validity window); None when the
        account is not known, so that nothing can be shown foreign to the operation."""
        if account is None:
            return None
        return {link.address for link in self.links if link.account_ref == account}

    def _bearing(self) -> tuple[str, ...]:
        """Chain effects bearing on the operation (0.2.0); the token view is unchanged."""
        if not self.inv013:
            return ()
        order = self.view("order_accepted")
        payload = order.observations[0].payload if order.status == "asserted" else None
        return self._chain_effects_bearing(
            payload.account_ref if isinstance(payload, OrderPayload) else None
        )

    def _chain_effects_bearing(self, account: str | None) -> tuple[str, ...]:
        """A chain effect never satisfies an obligation in this profile, and one bearing on
        the operation is not ignored into a MATCH either. From 0.3.0 a clawback
        never bears on the delivery comparison."""
        members = [
            o
            for o in self.members
            if not (
                self.clawback_neutral
                and isinstance(o.payload, ChainEffectPayload)
                and o.payload.effect_kind == "clawback"
            )
        ]
        return chain_effects_bearing(
            members,
            self.profile.authority_for("token_movement"),
            self.profile.instrument.instrument_id,
            self.snapshot.operation_ref,
            self._relevant(account),
            unresolved_bears=self.unresolved_bears,
        )

    def _affecting(self, certificate: CoverageCertificate, fact_type: FactType) -> int:
        """Quarantined records that can bear on this operation's ``fact_type``. A movement
        linked to an address without approval is already AMBIGUOUS_MATCH before coverage
        is checked, so approved addresses are the scope; without an asserted order every
        record counts."""
        if fact_type != "token_movement":
            return quarantine_affecting(certificate, None)
        order = self.view("order_accepted")
        payload = order.observations[0].payload if order.status == "asserted" else None
        account = payload.account_ref if isinstance(payload, OrderPayload) else None
        return quarantine_affecting(certificate, self._relevant(account))

    # ------------------------------------------------------------- controls

    def order(self) -> OrderPayload:
        payload = self.view("order_accepted").observations[0].payload
        assert isinstance(payload, OrderPayload)
        return payload

    def operands(self, control_id: str) -> Operands:
        order = self.order()
        if control_id == "subscription.order_terms":
            return Operands(_price_times_units(order, self.profile), order.cash_amount)
        if control_id == "subscription.cash_vs_order":
            (cash,) = self.view("cash_settled").observations
            assert isinstance(cash.payload, CashPayload)
            return Operands(cash.payload.amount, order.cash_amount)
        if control_id == "subscription.ta_units_vs_order":
            (ta,) = self.view("units_registered").observations
            assert isinstance(ta.payload, UnitsPayload)
            return Operands(ta.payload.units, order.units)
        if control_id == "subscription.token_units_vs_order":
            units = [
                o.payload.units
                for o in self.view("token_movement").observations
                if isinstance(o.payload, TokenMovementPayload)
            ]
            return Operands(_sum(units), order.units)
        raise Undecided("EVALUATION_ERROR", f"control {control_id} is not implemented")

    def control(self, spec: ControlSpec) -> tuple[ControlResult, Operands | None]:
        order_time: datetime | None = None
        refs: list[str] = []
        try:
            for fact_type in spec.requires:
                self._check(fact_type, order_time)
                view = self.view(fact_type)
                refs.extend(view.refs)
                refs.append(self.coverage_used[fact_type])
                if fact_type == "order_accepted":
                    order_time = view.observations[0].valid_time
            ops = self.operands(spec.control_id)
            if (ops.left.unit, ops.left.scale) != (ops.right.unit, ops.right.scale):
                raise Undecided(
                    "UNSUPPORTED_CAPABILITY",
                    f"operands differ in unit/scale: {ops.left.unit}/"
                    f"{ops.left.scale} vs {ops.right.unit}/{ops.right.scale}",
                    refs,
                )
            bearing = self._bearing() if "token_movement" in spec.requires else ()
            muxed = self.muxed_bearing if "token_movement" in spec.requires else ()
        except Undecided as undecided:
            return control_result(
                spec, "UNKNOWN", undecided.reason, undecided.detail, [*refs, *undecided.refs]
            ), None
        except Exception as error:  # a technical failure must never become PASS
            return control_result(
                spec, "UNKNOWN", "EVALUATION_ERROR", f"{type(error).__name__}: {error}", refs
            ), None
        left, right = int(ops.left.atoms), int(ops.right.atoms)
        deferred = (
            self.deferred.get("token_movement") if "token_movement" in spec.requires else None
        )
        if deferred is not None and deferred.additive and left <= right:
            pending = self._quarantine_undecided("token_movement", deferred)
            return control_result(
                spec,
                "UNKNOWN",
                pending.reason,
                pending.detail + "; resolving them could change this result",
                [*refs, *pending.refs],
            ), None
        considered = ""
        if deferred is not None:
            # The records stay quarantined; the result says they were weighed and why they
            # cannot change it.
            parts = []
            if deferred.neutral:
                parts.append(f"{len(deferred.neutral)} identified clawback(s) change no delivery")
            if deferred.additive:
                parts.append(f"{len(deferred.additive)} could only add to an excess")
            considered = (
                f"; quarantined records bearing on the operation in "
                f"{deferred.label} weighed: {', '.join(parts)}"
                + "".join(f"; {note}" for note in deferred.notes)
            )
            refs = [*refs, *deferred.refs]
        what = (
            "whose destination no IdentityLink attributes to the account"
            if self.weighs_unattributed
            else "naming a muxed sub-account"
        )
        if muxed and (left <= right or not _declares_policy(self.profile)):
            # A movement set aside could be (part of) the delivery: an equal or short
            # comparison is UNKNOWN; an excess of counted deliveries stays a FAIL only where
            # the profile declares its quarantine policy.
            return control_result(
                spec,
                "UNKNOWN",
                "AMBIGUOUS_MATCH",
                f"token_movement: movement(s) {what} ({', '.join(muxed)}) bear on the "
                "operation"
                + ("" if self.weighs_unattributed else " and no IdentityLink attributes them")
                + "; they are never counted, and resolving them could change this result",
                [*refs, *muxed],
            ), None
        if muxed:
            considered += (
                f"; {len(muxed)} movement(s) {what} weighed: they could only add to an excess"
            )
            refs = [*refs, *muxed]
        unscoped = self.unscoped if "token_movement" in spec.requires else None
        if unscoped is not None and left <= right:
            # Equal or short says that no further delivery reached the account,
            # which a certificate without a sufficient declared chain scope cannot show.
            return control_result(
                spec,
                "UNKNOWN",
                "INSUFFICIENT_COVERAGE",
                f"token_movement: {unscoped[0]}; the chain coverage cannot show that no "
                "further delivery reached the account, so an equal or short comparison is "
                "not concluded",
                refs,
            ), None
        if unscoped is not None:
            considered += (
                f"; the chain coverage cannot show the absence of further deliveries "
                f"({unscoped[0]}), which could only add to this excess"
            )
        if bearing and left <= right:
            # Equal or short: an unresolved effect could be the rest of the delivery. An
            # excess of linked deliveries stays a FAIL: no effect can undo a delivery.
            return control_result(
                spec,
                "UNKNOWN",
                "UNSUPPORTED_CAPABILITY",
                "token_movement: on-chain effect bearing on the operation "
                f"({', '.join(bearing)}); it is not admitted as delivery, and resolving it "
                "could change this result",
                [*refs, *bearing],
            ), None
        text = f"{ops.left.to_decimal_text()} vs {ops.right.to_decimal_text()} {ops.left.unit}"
        if left == right:
            return control_result(
                spec, "PASS", "EXACT_MATCH", f"equal: {text}{considered}", refs
            ), ops
        delta = Quantity(atoms=str(left - right), scale=ops.left.scale, unit=ops.left.unit)
        return control_result(
            spec,
            "FAIL",
            spec.failure_code,
            f"{text}; delta {delta.to_decimal_text()} {delta.unit}{considered}",
            refs,
            delta,
        ), ops


def _price_times_units(order: OrderPayload, profile: OperationProfile) -> Quantity:
    units, price = order.units, order.price_per_unit
    instrument = profile.instrument
    if (units.unit, units.scale) != (instrument.unit, instrument.scale):
        raise Undecided("UNSUPPORTED_CAPABILITY", "order units not in instrument unit/scale")
    if (price.unit, price.scale) != (profile.pricing.cash_unit, profile.pricing.cash_scale):
        raise Undecided("UNSUPPORTED_CAPABILITY", "price not in profile cash unit/scale")
    product = int(units.atoms) * int(price.atoms)
    divisor = 10**units.scale
    if product % divisor:
        raise Undecided("UNSUPPORTED_CAPABILITY", "units x price needs rounding (unsupported)")
    return Quantity(atoms=str(product // divisor), scale=price.scale, unit=price.unit)


def _sum(quantities: Sequence[Quantity]) -> Quantity:
    kinds = {(q.unit, q.scale) for q in quantities}
    if len(kinds) != 1:
        raise Undecided("UNSUPPORTED_CAPABILITY", "movements in different units/scales")
    ((unit, scale),) = kinds
    return Quantity(atoms=str(sum(int(q.atoms) for q in quantities)), scale=scale, unit=unit)


def evaluate(inputs: EvaluationInputs, *, engine_ref: str | None = None) -> Evaluation:
    """A new evaluation, with the current engine of the profile's operation type.

    ``engine_ref`` defaults to that engine. A retired engine is not selectable for new
    evaluations (use ``replay`` to reproduce a recorded conclusion), and any other one
    makes every control UNKNOWN (EVALUATION_ERROR) instead of evaluating with the wrong
    semantics.
    """
    return _dispatch(inputs, engine_ref, replaying=False)


def replay(inputs: EvaluationInputs, engine_ref: str) -> Evaluation:
    """Reproduce a recorded conclusion under its engine label, current or retired.

    Reproducing a retired engine's conclusion does not validate it under the current
    semantics; a blocked or unknown engine is never substituted (EVALUATION_ERROR).
    """
    return _dispatch(inputs, engine_ref, replaying=True)


def _dispatch(inputs: EvaluationInputs, engine_ref: str | None, *, replaying: bool) -> Evaluation:
    if isinstance(inputs.profile, RedemptionProfile):
        from invaria.engine.redemption import evaluate_redemption

        return evaluate_redemption(inputs, engine_ref=engine_ref, replaying=replaying)
    engine_ref = engine_ref or ENGINE_REF
    snapshot, profile = inputs.snapshot, inputs.profile
    problem = snapshot_problem(inputs)
    if problem is None:
        problem = engine_problem(engine_ref, _SEMANTICS, profile.operation_type, replaying)
    if problem is None:
        problem = profile_problem(
            engine_ref,
            profile.profile_ref,
            _declares_policy(profile),
            profile.correlation.muxed_account_link is not None,
            any(s.additional_mapping_refs for s in profile.sources),
            any(r.absence_needs_chain_scope for r in profile.coverage_requirements),
            any(r.completeness_needs_coverage for r in profile.coverage_requirements),
        )
    if problem is None and (not replaying or _states_provenance(engine_ref)):
        # Admission is part of the semantics of the engines that state their evidence's
        # provenance (0.8.0 on): they refuse it on replay too, so a recorded refusal
        # reproduces. A retired engine replays its conclusion with the original evidence
        # and provenance, as it did without this rule.
        problem = provenance_problem(inputs)
    controls: list[ControlResult] = []
    operands: dict[str, Operands] = {}
    effective: tuple[str, ...] = ()
    if problem is not None:
        controls = [
            control_result(spec, "UNKNOWN", "EVALUATION_ERROR", problem, [])
            for spec in profile.controls
        ]
    else:
        semantics = _SEMANTICS[engine_ref]
        evaluator = _Evaluator(
            inputs,
            inv013=semantics.inv013,
            clawback_neutral=semantics.clawback_neutral,
            unresolved_bears=semantics.unresolved_bears,
            bounded_quarantine=semantics.bounded_quarantine,
            adr014=semantics.adr014,
            muxed_identity=semantics.muxed_identity,
            weighs_unattributed=semantics.weighs_unattributed,
            scoped_absence=semantics.scoped_absence,
            completeness=semantics.completeness,
        )
        for spec in profile.controls:
            outcome, ops = evaluator.control(spec)
            controls.append(outcome)
            if ops is not None:
                operands[spec.control_id] = ops
        for fact_type in ("order_accepted", "cash_settled", "units_registered", "token_movement"):
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
                if engine_ref in _SEMANTICS and _SEMANTICS[engine_ref].states_provenance
                else None
            ),
        ),
        evaluation_clock=snapshot.evaluation_clock,
        assumptions=list(_assumptions(engine_ref, profile)),
    )
    return Evaluation(result, tuple(sorted(set(effective))), operands)
