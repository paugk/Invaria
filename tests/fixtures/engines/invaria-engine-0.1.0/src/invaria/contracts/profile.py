"""Operation profile: what a subscription must show, from whom, and how it is compared.

A profile is a versioned declaration, not code. The repository ships one synthetic profile; its
choices are fixtures for a demo fund, not policies of real funds.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from invaria.contracts.base import (
    Contract,
    Identifier,
    NonEmptyText,
    SchemaVersion,
    VersionRef,
)
from invaria.contracts.coverage import CoverageLevel
from invaria.contracts.identity import InstrumentRef, StellarClassicRepresentation
from invaria.contracts.observation import FactType
from invaria.contracts.quantity import Quantity, Unit

SourceKind = Literal["order_management", "bank", "transfer_agent", "stellar_ledger"]

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


class SourceAuthority(Contract):
    """Authority is per fact type. No source is a universal source of truth."""

    source_id: Identifier
    kind: SourceKind
    authoritative_for: Annotated[list[FactType], Field(min_length=1)]
    mapping_ref: VersionRef
    parser_ref: VersionRef
    description: NonEmptyText


class CoverageRequirement(Contract):
    source_id: Identifier
    fact_types: Annotated[list[FactType], Field(min_length=1)]
    min_level: CoverageLevel
    must_cover: Literal["order_valid_time_to_valid_at"]
    gaps_allowed: Literal[False]
    quarantined_records_allowed: Literal[False]


class Pricing(Contract):
    cash_unit: Unit
    cash_scale: Annotated[int, Field(ge=0, le=38)]
    price_per_unit: Quantity
    fees: Literal["none"]
    rounding: Literal["reject"]
    tolerance: Literal["none"]


class Correlation(Contract):
    operation_key: Literal["order_ref"]
    institutional_link: Literal["operation_ref_equals_order_ref"]
    chain_link: Literal["explicit_execution_link_only"]
    account_link: Literal["approved_identity_link"]


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
        return self

    def authority_for(self, fact_type: FactType) -> str:
        for source in self.sources:
            if fact_type in source.authoritative_for:
                return source.source_id
        raise KeyError(fact_type)
