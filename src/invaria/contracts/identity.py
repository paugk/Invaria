"""Instrument, Stellar representation and identity links.

An instrument is the economic right; a representation is its technical expression on a
network. They are never identified by ticker alone.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from invaria.contracts.base import Contract, Identifier, NonEmptyText, UtcDatetime
from invaria.contracts.quantity import MAX_SCALE, Unit
from invaria.contracts.stellar import NETWORK_PASSPHRASES, StellarAccountId, StellarNetwork


class InstrumentRef(Contract):
    instrument_id: Identifier
    unit: Unit
    scale: Annotated[int, Field(ge=0, le=MAX_SCALE)]
    description: NonEmptyText
    synthetic: bool


class StellarClassicRepresentation(Contract):
    """A Stellar Classic asset: network + asset code + issuer.

    Classic amounts carry 7 decimals. Access through the Stellar Asset Contract (SAC) is
    the same underlying asset and must never be counted as a second issuance; the
    synthetic profile does not ingest SAC events at all (declared unsupported).
    """

    representation_id: Identifier
    instrument_id: Identifier
    standard: Literal["stellar_classic"]
    network: StellarNetwork
    network_passphrase: NonEmptyText
    asset_code: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]{1,12}$")]
    issuer: StellarAccountId
    amount_scale: Literal[7]

    @model_validator(mode="after")
    def _passphrase_matches_network(self) -> Self:
        if NETWORK_PASSPHRASES[self.network] != self.network_passphrase:
            raise ValueError("network_passphrase does not match network")
        return self


class IdentityLink(Contract):
    """Approved link between an institutional account reference and a chain address."""

    schema_version: Literal["1.0"]
    link_id: Identifier
    account_ref: Identifier
    network: StellarNetwork
    address: StellarAccountId
    valid_from: UtcDatetime
    valid_to: UtcDatetime | None
    approval_ref: Identifier
    recorded_at: UtcDatetime

    @model_validator(mode="after")
    def _interval(self) -> Self:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self
