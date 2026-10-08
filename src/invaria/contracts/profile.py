"""Operation profile: what a subscription must show, from whom, and how it is compared.

A profile is a versioned declaration, not code. The synthetic subscription profile
and the synthetic redemption profile are fixtures; their choices are made
for a demo fund, not policies of real funds.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from invaria.contracts.base import (
    Contract,
    Identifier,
    NonEmptyText,
    SchemaVersion,
    VersionRef,
    check_strict_json,
)
from invaria.contracts.coverage import CoverageLevel
from invaria.contracts.identity import InstrumentRef, StellarClassicRepresentation
from invaria.contracts.observation import FactType
from invaria.contracts.quantity import Quantity, Unit

SourceKind = Literal[
    "order_management", "bank", "transfer_agent", "stellar_ledger", "pricing_agent"
]

UnsupportedCapability = Literal[
    "fees",
    "partial_fill",
    "multiple_cash_payments",
    "variable_nav",
    "fx_conversion",
    "rounding",
    "omnibus_account",
    "distributor_delivery",
    "sac_events",
    "soroban_custom_token",
    "redemption",
    "amount_time_matching",
]


def _absent_if_none(value: object) -> bool:
    return value is None


class SourceAuthority(Contract):
    """Authority is per fact type. No source is a universal source of truth.

    ``mapping_ref`` is the mapping of the source's authoritative records;
    ``additional_mapping_refs`` (fund-subscription-testnet@1.4.0 on) are the other mappings
    the profile admits for this source (a chain source also writes chain effects and the
    movements only one route reads). A new evaluation is refused when an observation of a
    declared source was produced by a mapping the profile does not admit for it: it is
    never relabelled nor read as if another mapping had produced it. Absent,
    the source admits only ``mapping_ref``; it is omitted when serialized, so existing
    profiles stay byte-identical.
    """

    source_id: Identifier
    kind: SourceKind
    authoritative_for: Annotated[list[FactType], Field(min_length=1)]
    mapping_ref: VersionRef
    additional_mapping_refs: Annotated[list[VersionRef], Field(min_length=1)] | None = Field(
        default=None, exclude_if=_absent_if_none
    )
    parser_ref: VersionRef
    description: NonEmptyText

    @model_validator(mode="after")
    def _admitted(self) -> Self:
        extra = self.additional_mapping_refs
        if extra is not None:
            if extra != sorted(set(extra)):
                raise ValueError("additional_mapping_refs must be sorted and unique")
            if self.mapping_ref in extra:
                raise ValueError("additional_mapping_refs repeats mapping_ref")
        return self

    @property
    def admitted_mapping_refs(self) -> frozenset[str]:
        """Every mapping the profile admits for the records of this source."""
        return frozenset({self.mapping_ref, *(self.additional_mapping_refs or ())})


def admitted_mapping_refs(sources: list[SourceAuthority]) -> list[str]:
    """Sorted union of the mappings a profile admits: a snapshot's ``mapping_refs``."""
    return sorted({m for s in sources for m in s.admitted_mapping_refs})


# ``records_bearing_on_operation``: a quarantined record blocks the controls only
# if the certificate cannot show it is foreign to the operation. Absent (profiles without a
# quarantine scope) means every quarantined record blocks; it is omitted when serialized, so
# existing profiles stay byte-identical.
QuarantineScope = Literal["records_bearing_on_operation"]


class QuarantinePolicy(Contract):
    """What a quarantined record of the chain coverage can and cannot do (profiles
    fund-subscription-testnet@1.2.0 and fund-redemption-synthetic@1.5.0 on).

    Declared explicitly so that ``quarantined_records_allowed: false`` keeps the meaning it
    had in the profiles that predate this policy, where a quarantined record bearing on the
    operation blocks every comparison of its source:

    - ``evidence``: a quarantined record never credits fulfilment;
    - ``blocking``: its existence makes UNKNOWN only the controls whose conclusion resolving
      it could change; a control it cannot change is not blocked automatically;
    - ``certificates``: a quarantine stays active until a later certificate of the same chain
      scope, including its route and ledger range, explicitly supersedes it; the absence of
      a record in a certificate of another scope resolves nothing;
    - ``classification``: a record's ``nature`` and ``chain_operation`` are relied on to set
      a record aside only when the snapshot's own artifacts support them;
    - ``unapproved_parties``: a party that cannot be or is not approved for the account
      (a contract, an account without IdentityLink) is not thereby foreign; an effect or
      record naming only such parties is outside the declared scope unless an
      ExecutionLink, an explicit association or an unresolved participant brings it in.
    """

    evidence: Literal["never_credits_fulfilment"]
    blocking: Literal["only_controls_whose_conclusion_it_could_change"]
    certificates: Literal["active_until_explicit_supersession_within_chain_scope"]
    classification: Literal["relied_on_only_when_supported_by_the_snapshot"]
    unapproved_parties: Literal["outside_declared_scope_unless_linked_associated_or_unresolved"]


class CoverageRequirement(Contract):
    source_id: Identifier
    fact_types: Annotated[list[FactType], Field(min_length=1)]
    min_level: CoverageLevel
    must_cover: Literal["order_valid_time_to_valid_at"]
    gaps_allowed: Literal[False]
    quarantined_records_allowed: Literal[False]
    quarantine_scope: QuarantineScope | None = Field(default=None, exclude_if=_absent_if_none)
    quarantine_policy: QuarantinePolicy | None = Field(default=None, exclude_if=_absent_if_none)
    # An absence (or the completeness of a comparison) on this chain source needs a
    # certificate whose chain scope is declared and sufficient; absent when None, so earlier
    # profiles stay byte-identical and keep their promise.
    absence_needs_chain_scope: Literal[True] | None = Field(
        default=None, exclude_if=_absent_if_none
    )
    # Affirming that the observed chain records are complete (an equal
    # or short comparison) needs coverage of the interval with a coherent chain scope of the
    # representation the control uses; absent when None (earlier profiles keep their promise).
    completeness_needs_coverage: Literal[True] | None = Field(
        default=None, exclude_if=_absent_if_none
    )

    @model_validator(mode="after")
    def _scope_only_chain_movements(self) -> Self:
        if self.quarantine_scope is not None and self.fact_types != ["token_movement"]:
            raise ValueError("quarantine_scope applies only to a token_movement requirement")
        if self.quarantine_policy is not None and self.quarantine_scope is None:
            raise ValueError("quarantine_policy needs quarantine_scope")
        if self.absence_needs_chain_scope and self.fact_types != ["token_movement"]:
            raise ValueError(
                "absence_needs_chain_scope applies only to a token_movement requirement"
            )
        if self.completeness_needs_coverage and not self.absence_needs_chain_scope:
            raise ValueError("completeness_needs_coverage needs absence_needs_chain_scope")
        return self


class Pricing(Contract):
    cash_unit: Unit
    cash_scale: Annotated[int, Field(ge=0, le=38)]
    price_per_unit: Quantity
    fees: Literal["none"]
    rounding: Literal["reject"]
    tolerance: Literal["none"]


# How a movement to a muxed (M) sub-account is attributed (profile
# fund-subscription-testnet@1.3.0 on): only by an IdentityLink naming that M address; a
# link to its base account does not attribute it, and a sub-account no link attributes to
# the account is never counted, though it is not thereby foreign. Absent (earlier profiles):
# a muxed movement is never attributed nor counted, and it weighs as the quarantined record
# the earlier mapping made of it. Omitted when serialized, so earlier profiles stay
# identical.
MuxedAccountLink = Literal["identity_link_to_the_muxed_address_only"]


class Correlation(Contract):
    operation_key: Literal["order_ref"]
    institutional_link: Literal["operation_ref_equals_order_ref"]
    chain_link: Literal["explicit_execution_link_only"]
    account_link: Literal["approved_identity_link"]
    muxed_account_link: MuxedAccountLink | None = Field(default=None, exclude_if=_absent_if_none)


class ControlSpec(Contract):
    control_id: Identifier
    mandatory: bool
    requires: Annotated[list[FactType], Field(min_length=1)]
    comparison: Literal["exact_equal"]
    left: NonEmptyText
    right: NonEmptyText
    failure_code: Literal["CASH_AMOUNT_MISMATCH", "UNITS_MISMATCH", "ORDER_TERMS_INCONSISTENT"]
    when_missing: Literal["UNKNOWN"]
    on_conflict: Literal["UNKNOWN"]
    on_unsupported: Literal["UNKNOWN"]


class EvidencePolicy(Contract):
    absent: Literal["UNKNOWN"]
    conflicting_authoritative: Literal["UNKNOWN"]
    correction: Literal["latest_revision_known_at_snapshot"]
    retraction_without_replacement: Literal["UNKNOWN_LOSS_OF_SUPPORT"]
    duplicate_same_content: Literal["single_effect"]
    duplicate_different_content: Literal["SOURCE_CONFLICT"]
    failed_chain_transaction: Literal["no_economic_effect"]
    technical_error: Literal["UNKNOWN"]


def _scoped_absence_on_the_chain_authority(
    sources: Sequence[SourceAuthority],
    requirements: Sequence[CoverageRequirement | RedemptionCoverageRequirement],
) -> None:
    """``absence_needs_chain_scope`` binds only the requirement of the ledger source
    authoritative for ``token_movement``; declared anywhere else it would gate the engines
    without ever being applied."""
    kinds = {s.source_id: s for s in sources}
    for req in requirements:
        if not req.absence_needs_chain_scope:
            continue
        source = kinds.get(req.source_id)
        if (
            source is None
            or source.kind != "stellar_ledger"
            or "token_movement" not in source.authoritative_for
        ):
            raise ValueError(
                "absence_needs_chain_scope belongs to the requirement of the stellar_ledger "
                "source authoritative for token_movement"
            )


class OperationProfile(Contract):
    schema_version: SchemaVersion
    profile_ref: VersionRef
    operation_type: Literal["subscription"]
    synthetic: bool
    disclaimer: NonEmptyText
    instrument: InstrumentRef
    representations: Annotated[list[StellarClassicRepresentation], Field(min_length=1)]
    representation_ratio: Literal["1:1"]
    pricing: Pricing
    correlation: Correlation
    sources: Annotated[list[SourceAuthority], Field(min_length=1)]
    coverage_requirements: list[CoverageRequirement]
    controls: Annotated[list[ControlSpec], Field(min_length=1)]
    rules_ref: VersionRef
    result_precedence: Literal["BREAK>UNKNOWN>MATCH"]
    evidence_policy: EvidencePolicy
    unsupported: list[UnsupportedCapability]

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        instrument = self.instrument
        if self.synthetic != instrument.synthetic:
            raise ValueError("profile and instrument must agree on synthetic")
        for rep in self.representations:
            if rep.instrument_id != instrument.instrument_id:
                raise ValueError("representation points to another instrument")
            if rep.amount_scale != instrument.scale:
                raise ValueError("1:1 representation requires equal scale")
            if self.synthetic and rep.network != "stellar:testnet":
                raise ValueError("a synthetic profile may only reference stellar:testnet")
        price = self.pricing.price_per_unit
        if price.unit != self.pricing.cash_unit or price.scale != self.pricing.cash_scale:
            raise ValueError("price_per_unit must be expressed in cash_unit at cash_scale")

        source_ids = [source.source_id for source in self.sources]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source_id must be unique")
        authority: dict[FactType, str] = {}
        for source in self.sources:
            for fact in source.authoritative_for:
                if fact in authority:
                    raise ValueError(f"two sources claim authority for {fact}")
                authority[fact] = source.source_id

        control_ids = [control.control_id for control in self.controls]
        if len(set(control_ids)) != len(control_ids):
            raise ValueError("control_id must be unique")
        if not any(control.mandatory for control in self.controls):
            raise ValueError("MATCH needs at least one mandatory control")
        for control in self.controls:
            for fact in control.requires:
                if fact not in authority:
                    raise ValueError(f"control {control.control_id} needs a source for {fact}")

        covered = {
            (req.source_id, fact) for req in self.coverage_requirements for fact in req.fact_types
        }
        for req in self.coverage_requirements:
            if req.source_id not in authority.values():
                raise ValueError(f"coverage requirement for unknown source {req.source_id}")
        for claimed_fact, source_id in authority.items():
            if (source_id, claimed_fact) not in covered:
                raise ValueError(f"no coverage requirement for {source_id}/{claimed_fact}")
        if len(set(self.unsupported)) != len(self.unsupported):
            raise ValueError("unsupported capabilities must be unique")
        _scoped_absence_on_the_chain_authority(self.sources, self.coverage_requirements)
        return self

    def authority_for(self, fact_type: FactType) -> str:
        for source in self.sources:
            if fact_type in source.authoritative_for:
                return source.source_id
        raise KeyError(fact_type)


# ------------------------------------------------------------------ redemption

RedemptionUnsupported = Literal[
    "fees",
    "partial_execution",
    "multiple_cash_payments",
    "fx_conversion",
    "rounding",
    "business_day_calendar",
    "transfer_retirement",
    "automatic_reversal",
    "omnibus_account",
    "sac_events",
    "soroban_custom_token",
    "amount_time_matching",
    "business_reactivation",
    "position_age_window",
    "burn_before_acceptance",
]
RedemptionFailure = Literal[
    "PRICE_MISMATCH",
    "UNITS_EXCEED_POSITION",
    "CASH_AMOUNT_MISMATCH",
    "UNITS_MISMATCH",
    "PAYMENT_LATE",
    "PAYMENT_MISSED",
    "SETTLED_DESPITE_CANCELLATION",
    "PAYMENT_TO_WRONG_ACCOUNT",
]


class RedemptionPricing(Contract):
    cash_unit: Unit
    cash_scale: Annotated[int, Field(ge=0, le=38)]
    price_fact: Literal["price_approved"]
    expected_payment: Literal["units_times_approved_price"]
    fees: Literal["none"]
    rounding: Literal["inexact_is_unknown"]
    tolerance: Literal["none"]


class RedemptionCorrelation(Contract):
    operation_key: Literal["request_ref"]
    institutional_link: Literal["operation_ref_equals_request_ref"]
    price_link: Literal["explicit_price_ref"]
    chain_link: Literal["explicit_execution_link_only"]
    account_link: Literal["approved_identity_link"]
    # Bank and TA name accounts in one namespace (DEMO-A guarantee): a linked payment to
    # another account is then a demonstrated wrong recipient; otherwise it is uncertain.
    account_namespace: Literal["shared_pseudonymous_account_ref", "per_source"]
    unlinked_payment_candidate: Literal["payment_ref_equals_request_settlement_ref"]
    # Whether the TA's settlement_ref and the bank's payment_ref share a namespace and
    # meaning; only then can a matching unlinked payment be a candidate (never a link).
    settlement_ref_namespace: Literal["bank_payment_ref", "incompatible"]


class TaJournal(Contract):
    """The TA journal whose source-assigned sequence orders acceptance, positions and
    their changes. A guarantee of this synthetic fixture, not of future real sources."""

    journal_id: Identifier
    ordering: Literal["source_assigned_strict_total_order"]
    position_semantics: Literal["state_after_its_sequence"]
    scope: Literal["synthetic_demo_a_only"]


class PaymentDeadline(Contract):
    """Exact elapsed hours from the request's acceptance, in UTC; no business calendar."""

    anchor: Literal["accepted_at"]
    hours: Annotated[int, Field(ge=1, le=8760)]
    calendar: Literal["none_exact_utc_hours"]
    on_time: Literal["effective_at_or_before_due"]
    judged_with: Literal["snapshot_evaluation_clock"]
    absence_after_due: Literal["BREAK_only_with_sufficient_bank_coverage"]
    absence_coverage: Literal["half_open_interval_must_contain_the_due_instant"]
    absence_requires: Literal["clock_and_economic_cut_after_due"]
    time_precision: Literal["second"]
    late_payment: Literal["breach_persists"]
    declared_due_mismatch: Literal["UNKNOWN_DEADLINE_DATA_CONFLICT_judge_computed_due"]


class RedemptionQuantity(Contract):
    request: Literal["fixed_quantity_settled_in_full"]
    position: Literal["available_immediately_before_acceptance_by_ta_sequence"]
    position_reconstruction: Literal["journal_changes_with_ta_coverage_no_age_window"]
    retirement: Literal["burn_to_issuer"]
    burn_before_acceptance: Literal["UNKNOWN_does_not_fulfil_but_contradicts_a_cancellation"]


class CancellationPolicy(Contract):
    authorized_by: Annotated[list[Identifier], Field(min_length=1)]
    valid_when: Literal["effective_at_or_after_acceptance"]
    effect: Literal["extinguishes_pending_settlement_obligations"]
    settlement_despite_cancellation: Literal["BREAK_review_required"]
    automatic_reversal: Literal[False]
    evidence_retraction: Literal["not_a_cancellation"]
    effective_after_due: Literal["keeps_a_demonstrated_breach"]
    retracted_cancellation: Literal["no_cancellation_in_force_with_ta_coverage"]
    reactivation: Literal["UNKNOWN_unsupported"]
    foreign_cancellation: Literal["out_of_scope"]
    contradictory_association: Literal["UNKNOWN_keep_conflict"]
    request_retraction: Literal["LOSS_OF_SUPPORT_in_dependent_controls_only"]
    invalid_retraction: Literal["UNKNOWN_INVALID_RETRACTION_keep_both"]


class RedemptionCoverageRequirement(Contract):
    source_id: Identifier
    fact_types: Annotated[list[FactType], Field(min_length=1)]
    min_level: CoverageLevel
    must_cover: Literal["interval_named_by_each_control"]
    scope: Literal["none", "account", "account_and_currency"]
    filters: Literal["only_those_keeping_every_record_of_the_predicate"]
    gaps_allowed: Literal[False]
    quarantined_records_allowed: Literal[False]
    quarantine_scope: QuarantineScope | None = Field(default=None, exclude_if=_absent_if_none)
    quarantine_policy: QuarantinePolicy | None = Field(default=None, exclude_if=_absent_if_none)
    # An absence (or the completeness of a comparison) on this chain source needs a
    # certificate whose chain scope is declared and sufficient; absent when None, so earlier
    # profiles stay byte-identical and keep their promise.
    absence_needs_chain_scope: Literal[True] | None = Field(
        default=None, exclude_if=_absent_if_none
    )
    # Affirming that the observed chain records are complete (an equal
    # or short comparison) needs coverage of the interval with a coherent chain scope of the
    # representation the control uses; absent when None (earlier profiles keep their promise).
    completeness_needs_coverage: Literal[True] | None = Field(
        default=None, exclude_if=_absent_if_none
    )

    @model_validator(mode="after")
    def _scope_only_chain_movements(self) -> Self:
        if self.quarantine_scope is not None and self.fact_types != ["token_movement"]:
            raise ValueError("quarantine_scope applies only to a token_movement requirement")
        if self.quarantine_policy is not None and self.quarantine_scope is None:
            raise ValueError("quarantine_policy needs quarantine_scope")
        if self.absence_needs_chain_scope and self.fact_types != ["token_movement"]:
            raise ValueError(
                "absence_needs_chain_scope applies only to a token_movement requirement"
            )
        if self.completeness_needs_coverage and not self.absence_needs_chain_scope:
            raise ValueError("completeness_needs_coverage needs absence_needs_chain_scope")
        return self


class RedemptionControlSpec(Contract):
    control_id: Identifier
    mandatory: bool
    requires: Annotated[list[FactType], Field(min_length=1)]
    comparison: Literal[
        "exact_equal", "at_most", "deadline", "data_consistency", "validity", "no_settlement"
    ]
    applies: Literal["always", "unless_cancelled", "cancellation"]
    # Positive evidence needs provenance, authority, effective time and revision
    # resolution; demonstrating absence or rebuilding a position needs interval coverage.
    coverage: Literal["positive_evidence_only", "absence_interval", "reconstruction_interval"]
    left: NonEmptyText
    right: NonEmptyText
    failure_codes: list[RedemptionFailure]


REDEMPTION_CONTROLS = (
    "redemption.declared_due_consistency",
    "redemption.price_vs_approved",
    "redemption.units_within_position",
    "redemption.cash_vs_expected",
    "redemption.payment_deadline",
    "redemption.ta_units_vs_request",
    "redemption.burn_vs_request",
    "redemption.cancellation_valid",
    "redemption.no_settlement_after_cancellation",
)
CONTROL_COVERAGE = {
    "redemption.declared_due_consistency": "positive_evidence_only",
    "redemption.price_vs_approved": "positive_evidence_only",
    "redemption.units_within_position": "reconstruction_interval",
    "redemption.cash_vs_expected": "positive_evidence_only",
    "redemption.payment_deadline": "absence_interval",
    "redemption.ta_units_vs_request": "positive_evidence_only",
    "redemption.burn_vs_request": "positive_evidence_only",
    "redemption.cancellation_valid": "absence_interval",
    "redemption.no_settlement_after_cancellation": "absence_interval",
}
REQUIRED_SCOPE = {
    "bank": "account_and_currency",
    "transfer_agent": "account",
    "pricing_agent": "none",
    "stellar_ledger": "none",
}


class RedemptionProfile(Contract):
    """What a redemption must show, from whom, and how it is judged."""

    schema_version: SchemaVersion
    profile_ref: VersionRef
    operation_type: Literal["redemption"]
    synthetic: bool
    disclaimer: NonEmptyText
    instrument: InstrumentRef
    representations: Annotated[list[StellarClassicRepresentation], Field(min_length=1)]
    representation_ratio: Literal["1:1"]
    pricing: RedemptionPricing
    correlation: RedemptionCorrelation
    payment_deadline: PaymentDeadline
    ta_journal: TaJournal
    quantity: RedemptionQuantity
    cancellation: CancellationPolicy
    sources: Annotated[list[SourceAuthority], Field(min_length=1)]
    coverage_requirements: list[RedemptionCoverageRequirement]
    controls: Annotated[list[RedemptionControlSpec], Field(min_length=1)]
    rules_ref: VersionRef
    result_precedence: Literal["BREAK>UNKNOWN>MATCH"]
    evidence_policy: EvidencePolicy
    unsupported: list[RedemptionUnsupported]

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        instrument = self.instrument
        if self.synthetic != instrument.synthetic:
            raise ValueError("profile and instrument must agree on synthetic")
        for rep in self.representations:
            if rep.instrument_id != instrument.instrument_id:
                raise ValueError("representation points to another instrument")
            if rep.amount_scale != instrument.scale:
                raise ValueError("1:1 representation requires equal scale")
            if self.synthetic and rep.network != "stellar:testnet":
                raise ValueError("a synthetic profile may only reference stellar:testnet")
        source_ids = [source.source_id for source in self.sources]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source_id must be unique")
        authority: dict[FactType, str] = {}
        for source in self.sources:
            for fact in source.authoritative_for:
                if fact in authority:
                    raise ValueError(f"two sources claim authority for {fact}")
                authority[fact] = source.source_id
        expected_authority: dict[FactType, SourceKind] = {
            "redemption_accepted": "transfer_agent",
            "redemption_cancelled": "transfer_agent",
            "position_held": "transfer_agent",
            "position_changed": "transfer_agent",
            "redemption_reactivated": "transfer_agent",
            "units_registered": "transfer_agent",
            "cash_settled": "bank",
            "price_approved": "pricing_agent",
            "token_movement": "stellar_ledger",
        }
        kinds = {s.source_id: s.kind for s in self.sources}
        for fact, kind in expected_authority.items():
            if fact not in authority:
                raise ValueError(f"no authoritative source for {fact}")
            if kinds[authority[fact]] != kind:
                raise ValueError(f"{fact} must come from a {kind} source")
        if "order_accepted" in authority:
            raise ValueError("a redemption profile has no order_accepted fact")
        control_ids = tuple(control.control_id for control in self.controls)
        if control_ids != REDEMPTION_CONTROLS:
            raise ValueError(f"controls must be exactly {list(REDEMPTION_CONTROLS)}, in order")
        if not all(control.mandatory for control in self.controls):
            raise ValueError("every redemption control is mandatory in this profile")
        for control in self.controls:
            if control.coverage != CONTROL_COVERAGE[control.control_id]:
                raise ValueError(
                    f"{control.control_id} coverage must be {CONTROL_COVERAGE[control.control_id]}"
                )
        for control in self.controls:
            for fact in control.requires:
                if fact not in authority:
                    raise ValueError(f"control {control.control_id} needs a source for {fact}")
        covered = {
            (req.source_id, fact) for req in self.coverage_requirements for fact in req.fact_types
        }
        for claimed_fact, source_id in authority.items():
            if (source_id, claimed_fact) not in covered:
                raise ValueError(f"no coverage requirement for {source_id}/{claimed_fact}")
        for req in self.coverage_requirements:
            if req.source_id not in kinds:
                raise ValueError(f"coverage requirement for unknown source {req.source_id}")
            if req.scope != REQUIRED_SCOPE[kinds[req.source_id]]:
                raise ValueError(
                    f"coverage of {req.source_id} must be scoped "
                    f"{REQUIRED_SCOPE[kinds[req.source_id]]}"
                )
        if len(set(self.unsupported)) != len(self.unsupported):
            raise ValueError("unsupported capabilities must be unique")
        _scoped_absence_on_the_chain_authority(self.sources, self.coverage_requirements)
        return self

    def authority_for(self, fact_type: FactType) -> str:
        for source in self.sources:
            if fact_type in source.authoritative_for:
                return source.source_id
        raise KeyError(fact_type)


Profile = OperationProfile | RedemptionProfile
AnyProfile = Annotated[Profile, Field(discriminator="operation_type")]
_ANY_PROFILE: TypeAdapter[Profile] = TypeAdapter(AnyProfile)


def parse_profile(text: str) -> Profile:
    """Parse any operation profile (strict JSON), choosing the type by ``operation_type``."""
    check_strict_json(text)
    return _ANY_PROFILE.validate_json(text)
