"""Coverage certificate: what a source delivered, for which interval, and its gaps.

Absence of a fact can only be argued inside a closed, declared coverage. A hash of what
was received says nothing about what was never received.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from invaria.contracts.base import (
    Contract,
    Identifier,
    NonEmptyText,
    SchemaVersion,
    Sha256Hex,
    UtcDatetime,
)
from invaria.contracts.observation import FactType
from invaria.contracts.quantity import Unit
from invaria.contracts.stellar import StellarAccountId, StellarNetwork

CoverageLevel = Literal["provider_claimed", "internally_checked", "independently_verified"]
LEVEL_ORDER: tuple[CoverageLevel, ...] = (
    "provider_claimed",
    "internally_checked",
    "independently_verified",
)


class TimeInterval(Contract):
    """Half-open interval [start, end) in UTC."""

    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("interval end must be after start")
        return self


class CoverageFilter(Contract):
    """One restriction applied to the export: keep only records whose ``field`` is in
    ``values``. Only filters on the evaluated predicate's own account, currency or fact
    type can be shown to keep every record a control needs."""

    field: Literal["account_ref", "currency", "fact_type", "status", "revision", "other"]
    values: Annotated[list[NonEmptyText], Field(min_length=1)]


class CoverageScope(Contract):
    """What the export covered beyond source, fact types and interval (on the certificate):
    the accounts and currencies, and every filter applied."""

    accounts: Annotated[list[Identifier], Field(min_length=1)]
    currencies: list[Unit] | None
    filters: list[CoverageFilter]


def _absent_if_none(value: object) -> bool:
    return value is None


class LedgerRange(Contract):
    first: Annotated[int, Field(ge=1)]
    last: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.last < self.first:
            raise ValueError("ledger range last must be >= first")
        return self


QuarantineNature = Literal[
    "clawback_identified", "movement_identified", "contradictory", "unresolved"
]


class QuarantinedRecord(Contract):
    """One quarantined input record and the ledger addresses it involves.

    ``addresses`` None means its parties are unknown (e.g. an undecodable record): it then
    affects every operation evaluated on the certificate.

    Optional, absent when None so that earlier certificates stay identical:

    - ``nature``: what the evidence establishes about the record. ``clawback_identified``:
      the event or operation identifies a clawback (the protocol emits it only for one) and
      only its correspondence or origin is incomplete. ``movement_identified``: a movement
      whose kind and parties are identified (a contract or pool movement, a claimable
      balance effect with read participants) and that no current profile admits as
      evidence. ``contradictory``: the sources
      disagree on whether it was a clawback or something else. ``unresolved``: nature,
      execution, asset or participants not resolved. Absent (an earlier certificate) is
      read as ``unresolved``.
    - ``chain_operation``: ``<tx_hash>:<op_index>`` of the ledger operation, when known.
    - ``execution_links``: the operations whose explicit ExecutionLink names that ledger
      operation. The link makes the record relevant to those operations; its addresses
      are kept as they are, never replaced by unknown parties.
    """

    locator: Annotated[str, Field(min_length=1, max_length=512)]
    reasons: Annotated[list[Identifier], Field(min_length=1)]
    # Ledger strkeys (G account, C contract, M muxed, B claimable balance, L pool); never
    # empty: a record with no known party is None.
    addresses: (
        Annotated[
            list[Annotated[str, StringConstraints(pattern=r"^[GCMBL][A-Z2-7]{55,68}$")]],
            Field(min_length=1),
        ]
        | None
    )
    nature: QuarantineNature | None = Field(default=None, exclude_if=_absent_if_none)
    chain_operation: (
        Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}:[0-9]{1,10}$")] | None
    ) = Field(default=None, exclude_if=_absent_if_none)
    execution_links: Annotated[list[Identifier], Field(min_length=1)] | None = Field(
        default=None, exclude_if=_absent_if_none
    )


# The chain route a certificate read. The SAC certificate also carries the records
# of the Horizon run of the same range it depends on, so it includes that route.
ChainRoute = Literal["horizon_payments", "rpc_sac_events"]
ROUTE_INCLUDES: dict[str, frozenset[str]] = {
    "horizon_payments": frozenset({"horizon_payments"}),
    "rpc_sac_events": frozenset({"horizon_payments", "rpc_sac_events"}),
}
# Effects named by a claimable balance id (B…) that a route may not see: the clawback of a
# claimable balance (a holder's routes never see it; the issuer's only through the SAC route)
# and the claim of a balance by a third party (no holder or issuer route sees it unless the
# target is the claimant).
UncoveredEffect = Literal["claimable_balance_clawback", "claimable_balance_claim"]


# What each route sees, by role: the effects a chain scope must
# declare in ``not_covered``. A holder target sees neither the clawback of a claimable
# balance it created nor a third party's claim of it; the issuer sees the clawback only
# through the SAC route and never a third party's claim. Valid for every adapter version
# that writes ``chain_scope`` (the stores below); a new store format must review it.
ROUTE_TABLE_STORE_FORMATS = frozenset({"invaria-ingest-store@7", "invaria-ingest-store@8"})


def route_unseen(scope: ChainCoverageScope) -> frozenset[UncoveredEffect]:
    """The effects ``scope``'s route and role cannot observe."""
    holder = scope.account != scope.asset_issuer
    if holder or scope.route == "horizon_payments":
        return frozenset({"claimable_balance_clawback", "claimable_balance_claim"})
    return frozenset({"claimable_balance_claim"})


def scope_coherence_problem(scope: ChainCoverageScope) -> str | None:
    """Why ``scope``'s declaration is incompatible with what its route and role can observe
    (an empty or partial ``not_covered`` cannot cover what the route never sees), or None.
    A missing declaration is not judged here: it declares nothing."""
    if scope.not_covered is None:
        return None
    missing = sorted(route_unseen(scope) - set(scope.not_covered))
    if not missing:
        return None
    role = "holder" if scope.account != scope.asset_issuer else "issuer"
    return (
        f"declares in sight {', '.join(missing)}, which its route ({scope.route}) and role "
        f"({role}) never observe"
    )


class ChainCoverageScope(Contract):
    """Which chain records a certificate read: network, ingestion target (asset
    and account), route and the ExecutionLinks the run applied (``links_sha256``: they
    decide which notes are quarantined and which records list a link). Two certificates can
    replace one another only within the same network, target, account, asset and links, and
    only towards a route that includes the other's.

    ``not_covered``: effects of the asset this route cannot see for this target (by role and
    route); absence of one of them can never be argued from this certificate. An
    empty list declares that nothing is out of sight; absent (None) declares
    nothing, and under the scoped-absence rule it is never read as "nothing out of sight".
    """

    network: StellarNetwork
    target_id: Identifier
    account: StellarAccountId
    asset_code: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]{1,12}$")]
    asset_issuer: StellarAccountId
    route: ChainRoute
    links_sha256: Sha256Hex
    not_covered: list[UncoveredEffect] | None = Field(default=None, exclude_if=_absent_if_none)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if self.not_covered is not None and len(set(self.not_covered)) != len(self.not_covered):
            raise ValueError("not_covered lists each effect once")
        return self

    def includes(self, other: ChainCoverageScope) -> bool:
        """Same network, target, account, asset and links, and a route including ``other``'s."""
        return self.identity() == other.identity() and other.route in ROUTE_INCLUDES[self.route]

    def identity(self) -> tuple[str, ...]:
        return (
            self.network,
            self.target_id,
            self.account,
            self.asset_code,
            self.asset_issuer,
            self.links_sha256,
        )


class SupersededCertificate(Contract):
    """A certificate a later one replaces, by id and by the sha256 of its canonical JSON
    (``certificate_sha256``): two runs can produce one id with different content, and a
    replacement holds only for the content it names."""

    coverage_id: Identifier
    sha256: Sha256Hex


class CoverageCertificate(Contract):
    schema_version: SchemaVersion
    coverage_id: Identifier
    tenant_id: Identifier
    source_id: Identifier
    fact_types: Annotated[list[FactType], Field(min_length=1)]
    instrument_id: Identifier
    interval: TimeInterval
    ledger_range: LedgerRange | None
    level: CoverageLevel
    method: NonEmptyText
    records_received: Annotated[int, Field(ge=0)]
    records_quarantined: Annotated[int, Field(ge=0)]
    gaps: list[TimeInterval]
    raw_sha256: list[Sha256Hex]
    recorded_at: UtcDatetime
    synthetic: bool
    # Absent from the serialized form when None, so existing certificates stay identical.
    scope: CoverageScope | None = Field(default=None, exclude_if=_absent_if_none)
    # Per quarantined record, when the source can say which addresses it involves; absent
    # otherwise (then every quarantined record affects every operation).
    quarantined_records: list[QuarantinedRecord] | None = Field(
        default=None, exclude_if=_absent_if_none
    )
    # Absent when None so earlier certificates stay identical. ``chain_scope``: the
    # chain records read. ``supersedes``: earlier certificates this one replaces; a claim
    # that engines accept only if this certificate's scope includes theirs and its ledger
    # range contains theirs (``supersession_problem``). Never inferred from recency alone.
    chain_scope: ChainCoverageScope | None = Field(default=None, exclude_if=_absent_if_none)
    supersedes: Annotated[list[SupersededCertificate], Field(min_length=1)] | None = Field(
        default=None, exclude_if=_absent_if_none
    )

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if len(set(self.fact_types)) != len(self.fact_types):
            raise ValueError("fact_types must be unique")
        for gap in self.gaps:
            if gap.start < self.interval.start or gap.end > self.interval.end:
                raise ValueError("gaps must lie inside the covered interval")
        if self.records_quarantined > self.records_received:
            raise ValueError("cannot quarantine more records than received")
        if self.quarantined_records is not None:
            locators = [r.locator for r in self.quarantined_records]
            if len(locators) != self.records_quarantined or len(set(locators)) != len(locators):
                raise ValueError("quarantined_records lists each quarantined record once")
        if self.supersedes is not None:
            if self.chain_scope is None or self.ledger_range is None:
                raise ValueError("only a chain certificate with a ledger range supersedes")
            ids = [entry.coverage_id for entry in self.supersedes]
            if len(set(ids)) != len(ids):
                raise ValueError("supersedes lists each certificate once")
            if self.coverage_id in ids:
                raise ValueError("a certificate cannot supersede itself")
        return self

    @property
    def is_gap_free(self) -> bool:
        return not self.gaps and self.records_quarantined == 0


def certificate_sha256(certificate: CoverageCertificate) -> str:
    """sha256 of the certificate's canonical JSON (sorted keys, no spaces)."""
    document = json.dumps(
        certificate.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def supersession_problem(newer: CoverageCertificate, older: CoverageCertificate) -> str | None:
    """Why ``newer`` cannot replace ``older``, or None.

    It must name ``older`` (id and content sha256) in ``supersedes``; be of the same tenant,
    source, instrument and kind (synthetic or not); cover its fact types; have a chain
    scope including ``older``'s (same network, target, account, asset and links; a route
    that includes ``older``'s) and a ledger range containing ``older``'s; be recorded after
    it (at the same instant, only if its route or range strictly contains ``older``'s);
    declare no gaps and have no lower level. The absence of a record in a certificate of
    another scope, or of a narrower route or range, never resolves it.
    """
    named = {entry.coverage_id: entry.sha256 for entry in newer.supersedes or ()}
    if older.coverage_id not in named:
        return "does not declare that it supersedes it"
    if named[older.coverage_id] != certificate_sha256(older):
        return "it names another content under that id"
    if (newer.tenant_id, newer.source_id, newer.instrument_id, newer.synthetic) != (
        older.tenant_id,
        older.source_id,
        older.instrument_id,
        older.synthetic,
    ):
        return "another tenant, source, instrument or kind"
    if not set(older.fact_types) <= set(newer.fact_types):
        return "does not cover its fact types"
    if newer.chain_scope is None or older.chain_scope is None:
        return "no chain scope to compare"
    if not newer.chain_scope.includes(older.chain_scope):
        return "its scope (network, target, account, asset or route) does not include it"
    if newer.ledger_range is None or older.ledger_range is None:
        return "no ledger range to compare"
    if not (
        newer.ledger_range.first <= older.ledger_range.first
        and older.ledger_range.last <= newer.ledger_range.last
    ):
        return "its ledger range does not contain it"
    if newer.recorded_at < older.recorded_at:
        return "recorded before it"
    if (
        newer.recorded_at == older.recorded_at
        and newer.chain_scope.route == older.chain_scope.route
        and (newer.ledger_range.first, newer.ledger_range.last)
        == (older.ledger_range.first, older.ledger_range.last)
    ):
        return "recorded at the same instant without strictly containing it"
    # A record's absence inside a gap, or at a weaker level, resolves nothing.
    if newer.gaps:
        return "it declares gaps"
    if LEVEL_ORDER.index(newer.level) < LEVEL_ORDER.index(older.level):
        return "its level is lower"
    return None
