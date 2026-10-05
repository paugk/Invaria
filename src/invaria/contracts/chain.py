"""Contracts for chain ingestion: what to read, explicit execution links, checkpoints."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from invaria.contracts.base import Contract, Identifier, NonEmptyText, Sha256Hex, UtcDatetime
from invaria.contracts.quantity import Unit
from invaria.contracts.stellar import StellarAccountId, StellarNetwork

ContractId = Annotated[str, StringConstraints(pattern=r"^C[A-Z2-7]{55}$")]
IngestPath = Literal["horizon_payments", "rpc_sac_events"]


class ChainTarget(Contract):
    """One Classic asset (by network + code + issuer) observed for one account."""

    schema_version: Literal["1.0"]
    target_id: Identifier
    tenant_id: Identifier
    network: StellarNetwork
    asset_code: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]{1,12}$")]
    asset_issuer: StellarAccountId
    account: StellarAccountId
    unit: Unit
    instrument_id: Identifier
    representation_id: Identifier
    source_id: Identifier
    expected_sac_contract_id: ContractId | None
    synthetic: bool
    description: NonEmptyText


class ExecutionLink(Contract):
    """Explicit, approved statement that a chain effect executes a given operation.

    The only way an observation gets an ``operation_ref``: memo text, amount or timing
    never link an on-chain effect to an institutional operation.
    """

    schema_version: Literal["1.0"]
    network: StellarNetwork
    tx_hash: Sha256Hex
    operation_index: Annotated[int, Field(ge=0)]
    ordinal: Annotated[int, Field(ge=0)]
    operation_ref: Identifier
    approval_ref: Identifier
    recorded_at: UtcDatetime


class OrdinalState(Contract):
    tx_hash: Sha256Hex
    operation_index: Annotated[int, Field(ge=0)]
    next_ordinal: Annotated[int, Field(ge=0)]


class IngestionCheckpoint(Contract):
    """Resume point; written only after the page it covers has been persisted."""

    schema_version: Literal["1.0"]
    target_id: Identifier
    path: IngestPath
    start_ledger: Annotated[int, Field(ge=1)]
    end_ledger: Annotated[int, Field(ge=1)]
    cursor: Annotated[str, StringConstraints(pattern=r"^[0-9-]{1,64}$")]
    pages: Annotated[int, Field(ge=0)]
    records_received: Annotated[int, Field(ge=0)]
    records_quarantined: Annotated[int, Field(ge=0)]
    page_sha256: list[Sha256Hex]
    ordinal_state: OrdinalState | None
    complete: bool

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.end_ledger < self.start_ledger:
            raise ValueError("end_ledger must be >= start_ledger")
        return self


class ExecutionLinkSet(Contract):
    schema_version: Literal["1.0"]
    links: list[ExecutionLink]
