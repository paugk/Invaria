"""Observation: an immutable statement by one source, with provenance.

Corrections are new revisions that point to the revision they replace (``supersedes``).
Retractions are revisions without payload. Nothing is edited in place.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from invaria.contracts.base import (
    Contract,
    Identifier,
    SchemaVersion,
    Sha256Hex,
    UtcDatetime,
    VersionRef,
)
from invaria.contracts.quantity import Quantity
from invaria.contracts.stellar import StellarAccountId, StellarNetwork

FactType = Literal["order_accepted", "cash_settled", "units_registered", "token_movement"]


class SourceRecord(Contract):
    """Idempotency identity of a source record: same key + revision = same statement."""

    source_id: Identifier
    record_key: Identifier
    revision: Annotated[int, Field(ge=1)]


class Provenance(Contract):
    raw_sha256: Sha256Hex
    raw_locator: Annotated[str, Field(min_length=1, max_length=512)]
    parser_ref: VersionRef
    mapping_ref: VersionRef


class OrderPayload(Contract):
    payload_type: Literal["order_accepted"]
    account_ref: Identifier
    units: Quantity
    cash_amount: Quantity
    price_per_unit: Quantity


class CashPayload(Contract):
    payload_type: Literal["cash_settled"]
    account_ref: Identifier
    payment_ref: Identifier
    amount: Quantity


class UnitsPayload(Contract):
    payload_type: Literal["units_registered"]
    account_ref: Identifier
    units: Quantity


class ChainCoordinates(Contract):
    network: StellarNetwork
    ledger: Annotated[int, Field(ge=1)]
    tx_hash: Sha256Hex
    operation_index: Annotated[int, Field(ge=0)]
    tx_successful: bool


class TokenMovementPayload(Contract):
    payload_type: Literal["token_movement"]
    from_address: StellarAccountId
    to_address: StellarAccountId
    units: Quantity
    chain: ChainCoordinates


Payload = Annotated[
    OrderPayload | CashPayload | UnitsPayload | TokenMovementPayload,
    Field(discriminator="payload_type"),
]


class Observation(Contract):
    schema_version: SchemaVersion
    observation_id: Identifier
    tenant_id: Identifier
    kind: Literal["assertion", "retraction"]
    fact_type: FactType
    instrument_id: Identifier
    representation_id: Identifier | None
    operation_ref: Identifier | None
    source: SourceRecord
    valid_time: UtcDatetime
    recorded_at: UtcDatetime
    provenance: Provenance
    supersedes: Identifier | None
    payload: Payload | None
    synthetic: bool

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.kind == "retraction":
            if self.payload is not None:
                raise ValueError("a retraction carries no payload")
            if self.supersedes is None:
                raise ValueError("a retraction must name the revision it withdraws")
        else:
            if self.payload is None:
                raise ValueError("an assertion requires a payload")
            if self.payload.payload_type != self.fact_type:
                raise ValueError("payload_type must equal fact_type")
        if self.supersedes == self.observation_id:
            raise ValueError("an observation cannot supersede itself")
        if self.supersedes is not None and self.source.revision < 2:
            raise ValueError("a superseding observation must have revision >= 2")
        if self.fact_type == "token_movement" and self.representation_id is None:
            raise ValueError("token_movement requires representation_id")
        return self
