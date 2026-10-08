"""Building blocks shared by the subscription and redemption engines.

Neutral, public interface: evaluation inputs and outputs, per-record revision resolution,
fact views, control results, snapshot integrity checks and evaluation ids. No profile
semantics live here; each engine keeps its own rules. Pure: no clock, network or files.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from typing import Any, Literal

from invaria.contracts.coverage import (
    ROUTE_INCLUDES,
    CoverageCertificate,
    QuarantinedRecord,
    UncoveredEffect,
    scope_coherence_problem,
    supersession_problem,
)
from invaria.contracts.evaluation import ControlResult, EvaluationResult, ReasonCode, SnapshotRef
from invaria.contracts.identity import IdentityLink, StellarClassicRepresentation
from invaria.contracts.observation import (
    ChainEffectPayload,
    FactType,
    Observation,
    TokenMovementPayload,
    effect_parties,
    is_muxed,
    movement_addresses,
)
from invaria.contracts.profile import (
    ControlSpec,
    OperationProfile,
    RedemptionControlSpec,
    RedemptionProfile,
    admitted_mapping_refs,
)
from invaria.contracts.quantity import Quantity
from invaria.contracts.stellar import base_account
from invaria.engine.versions import BLOCKED_ENGINES, CURRENT_ENGINES, RETIRED_ENGINES

LEVEL_RANK = {"provider_claimed": 0, "internally_checked": 1, "independently_verified": 2}


@dataclass(frozen=True)
class EvaluationInputs:
    snapshot: SnapshotRef
    profile: OperationProfile | RedemptionProfile
    observations: Mapping[str, Observation]
    coverage: Mapping[str, CoverageCertificate]
    identity_links: Mapping[str, IdentityLink]


@dataclass(frozen=True)
class Operands:
    left: Quantity
    right: Quantity


@dataclass(frozen=True)
class Evaluation:
    result: EvaluationResult
    effective_observation_ids: tuple[str, ...]
    operands: Mapping[str, Operands] = field(default_factory=dict)


class Undecided(Exception):
    def __init__(self, reason: ReasonCode, detail: str, refs: Sequence[str] = ()) -> None:
        super().__init__(detail)
        self.reason: ReasonCode = reason
        self.detail = detail
        self.refs = tuple(refs)


ViewStatus = Literal[
    "asserted", "absent", "conflict", "withdrawn", "ambiguous", "unsupported", "early"
]


@dataclass(frozen=True)
class FactView:
    fact_type: FactType
    status: ViewStatus
    observations: tuple[Observation, ...]
    refs: tuple[str, ...]
    detail: str


def record_content(o: Observation) -> tuple[Any, ...]:
    """What makes two copies of a record the same content. The payload is compared as its
    canonical JSON: equal payloads give equal JSON (datetimes are UTC), and a payload with
    a list (a path payment's path, a claimable balance's claimants) stays usable as a key;
    comparing the model itself raised TypeError for those (corrected defect)."""
    return (
        o.kind,
        o.fact_type,
        o.instrument_id,
        o.representation_id,
        o.operation_ref,
        o.valid_time,
        None if o.payload is None else o.payload.model_dump_json(),
    )


def resolve_records(observations: Sequence[Observation]) -> list[tuple[str, list[Observation]]]:
    """Per source record: ('asserted'|'withdrawn'|'conflict', observations involved)."""
    records: dict[tuple[str, str], list[Observation]] = {}
    for o in observations:
        records.setdefault((o.source.source_id, o.source.record_key), []).append(o)
    resolved: list[tuple[str, list[Observation]]] = []
    for key in sorted(records):
        revisions = records[key]
        top = max(o.source.revision for o in revisions)
        latest = sorted(
            (o for o in revisions if o.source.revision == top),
            key=lambda o: (o.recorded_at, o.observation_id),
        )
        distinct: dict[tuple[Any, ...], Observation] = {}
        for o in latest:
            distinct.setdefault(record_content(o), o)
        if len(distinct) > 1:
            resolved.append(("conflict", latest))
        else:
            representative = latest[0]
            state = "withdrawn" if representative.kind == "retraction" else "asserted"
            resolved.append((state, [representative]))
    return resolved


def institutional_view(
    fact_type: FactType, members: Sequence[Observation], single: str | None
) -> FactView:
    resolved = resolve_records(members)
    conflicts = [o for state, obs in resolved if state == "conflict" for o in obs]
    if conflicts:
        ids = tuple(o.observation_id for o in conflicts)
        return FactView(fact_type, "conflict", (), ids, "same record and revision differ")
    asserted = tuple(o for state, obs in resolved if state == "asserted" for o in obs)
    withdrawn = tuple(
        o.observation_id for state, obs in resolved if state == "withdrawn" for o in obs
    )
    if not asserted:
        if withdrawn:
            return FactView(
                fact_type,
                "withdrawn",
                (),
                withdrawn,
                "the only supporting record was withdrawn by its source",
            )
        return FactView(fact_type, "absent", (), (), "no observation in the snapshot")
    if len(asserted) > 1 and single is not None:
        ids = tuple(o.observation_id for o in asserted)
        return FactView(fact_type, "unsupported", asserted, ids, f"unsupported: {single}")
    return FactView(fact_type, "asserted", asserted, tuple(o.observation_id for o in asserted), "")


def control_result(
    spec: ControlSpec | RedemptionControlSpec,
    status: Literal["PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE"],
    reason: ReasonCode,
    detail: str,
    refs: Sequence[str],
    delta: Quantity | None = None,
) -> ControlResult:
    return ControlResult(
        control_id=spec.control_id,
        mandatory=spec.mandatory,
        status=status,
        reason_code=reason,
        delta=delta,
        reason=detail,
        evidence_refs=list(dict.fromkeys(refs)),
    )


def snapshot_problem(inputs: EvaluationInputs) -> str | None:
    snapshot, profile = inputs.snapshot, inputs.profile
    if snapshot.profile_ref != profile.profile_ref:
        return f"snapshot profile {snapshot.profile_ref} != {profile.profile_ref}"
    if snapshot.rules_ref != profile.rules_ref:
        return f"snapshot rules {snapshot.rules_ref} != {profile.rules_ref}"
    if snapshot.mapping_refs != admitted_mapping_refs(profile.sources):
        return "snapshot mapping_refs differ from the mappings the profile admits"
    stores: list[tuple[list[str], Mapping[str, Any], str]] = [
        (snapshot.observation_ids, inputs.observations, "observation"),
        (snapshot.coverage_ids, inputs.coverage, "coverage"),
        (snapshot.identity_link_ids, inputs.identity_links, "identity link"),
    ]
    for ids, store, kind in stores:
        for member in ids:
            if member not in store:
                return f"{kind} {member} is a snapshot member but missing from the store"
            if store[member].recorded_at > snapshot.known_at:
                return f"{kind} {member} was recorded after known_at"
    return None


def evidence_mapping_refs(inputs: EvaluationInputs) -> list[str]:
    """The mappings that produced the snapshot's observations of the profile's sources:
    what the evaluation states as the provenance of its evidence. An
    observation of a source the profile does not declare is never read, so it is no
    evidence of this evaluation."""
    declared = {s.source_id for s in inputs.profile.sources}
    members = (inputs.observations.get(i) for i in inputs.snapshot.observation_ids)
    return sorted(
        {o.provenance.mapping_ref for o in members if o and o.source.source_id in declared}
    )


def provenance_problem(inputs: EvaluationInputs) -> str | None:
    """A new evaluation reads only evidence its profile admits: each
    observation of a declared source must have been produced by a mapping the profile
    admits for that source. Otherwise the combination is refused; the observation is never
    relabelled nor read as if another mapping had produced it. Replay does not apply this:
    it reproduces a recorded conclusion with its original evidence and provenance."""
    admitted = {s.source_id: s.admitted_mapping_refs for s in inputs.profile.sources}
    refused: dict[tuple[str, str], list[str]] = {}
    for member in inputs.snapshot.observation_ids:
        o = inputs.observations[member]  # snapshot_problem ran first: every member exists
        allowed = admitted.get(o.source.source_id)
        if allowed is not None and o.provenance.mapping_ref not in allowed:
            refused.setdefault((o.source.source_id, o.provenance.mapping_ref), []).append(member)
    if not refused:
        return None
    parts = [
        f"{len(ids)} observation(s) of source {source} (e.g. {ids[0]}) were produced by "
        f"mapping {mapping}, which {inputs.profile.profile_ref} does not admit for that "
        f"source (admits: {', '.join(sorted(admitted[source]))})"
        for (source, mapping), ids in sorted(refused.items())
    ]
    return (
        "evidence provenance not admitted by the profile: "
        + "; ".join(parts)
        + "; nothing is relabelled or read with another mapping: evaluate it with a profile "
        "that admits that mapping"
    )


def chain_effects_bearing(
    members: Sequence[Observation],
    source: str,
    instrument_id: str,
    operation_ref: str,
    relevant: set[str] | None,
    *,
    unresolved_bears: bool = False,
) -> tuple[str, ...]:
    """Chain effects of ``source`` that bear on the operation.

    Linked to the operation (``operation_ref``, which only an explicit association sets;
    the Stellar adapter sets none, and a linked operation reaches evaluations through its
    quarantined record, which lists the link beside its known addresses), or
    involving a ``relevant`` address;
    ``relevant`` None (the account is not known) means every one bears on it. A conflicting
    copy bears on it as much as an asserted one. With ``unresolved_bears`` (subscription
    0.4.0 and redemption 0.6.0) an effect naming a participant the evidence does
    not identify bears on it too: unknown participants are never ruled out.
    """
    effects = [
        o
        for o in members
        if o.fact_type == "chain_effect"
        and o.source.source_id == source
        and o.instrument_id == instrument_id
    ]
    bearing = []
    for state, observations in resolve_records(effects):
        if state == "withdrawn":
            continue
        for o in observations:
            payload = o.payload
            assert isinstance(payload, ChainEffectPayload)
            # Every identified technical participant (``effect_parties``): for the effects
            # of increments 1 and 2 exactly the account, counterparty, path payment ends
            # and DEX submitter, as before. An unresolved participant has no address: it
            # bears only under ``unresolved_bears``; the adapter also quarantines its
            # record with unknown parties.
            named = effect_parties(payload)
            parties = {p.id for p in named if p.id is not None}
            unknown = unresolved_bears and any(p.kind == "unresolved" for p in named)
            if (
                unknown
                or relevant is None
                or o.operation_ref == operation_ref
                or relevant & parties
            ):
                bearing.append(o.observation_id)
    return tuple(sorted(bearing))


def by_address(record: QuarantinedRecord, relevant: set[str] | None) -> bool:
    return (
        relevant is None
        or record.addresses is None
        # A muxed address (M) is not compared with the base accounts: unknown, so it counts.
        or any(address.startswith("M") for address in record.addresses)
        or bool(relevant.intersection(record.addresses))
    )


def bears_by_address(addresses: Set[str] | Sequence[str], relevant: set[str] | None) -> bool:
    """Whether ledger addresses bear on an account (subscription 0.7.0 and
    redemption 0.8.0): ``relevant`` None, an approved address, or a muxed sub-account (M)
    whose base account is approved: who holds that sub-account is unresolved, and an
    unresolved identity never shows a movement to be foreign. The base account of an
    approved sub-account is not thereby approved, nor is a sibling sub-account."""
    if relevant is None:
        return True
    return any(
        address in relevant or (address.startswith("M") and base_account(address) in relevant)
        for address in addresses
    )


def by_address_muxed(record: QuarantinedRecord, relevant: set[str] | None) -> bool:
    """``by_address`` with muxed addresses decoded: an M address is compared as
    itself and through its base account, instead of counting for every operation."""
    return record.addresses is None or bears_by_address(record.addresses, relevant)


def muxed_movements(
    members: Sequence[Observation], source: str, instrument_id: str
) -> list[Observation]:
    """Successful, asserted movements of ``source`` that name a muxed sub-account.
    A conflicting copy is left to the token view, which reports the conflict."""
    found = []
    for state, observations in resolve_records(
        [
            o
            for o in members
            if o.fact_type == "token_movement"
            and o.source.source_id == source
            and o.instrument_id == instrument_id
        ]
    ):
        if state != "asserted":
            continue
        for o in observations:
            payload = o.payload
            if (
                isinstance(payload, TokenMovementPayload)
                and payload.chain.tx_successful
                and is_muxed(payload)
            ):
                found.append(o)
    return found


def quarantine_affecting(certificate: CoverageCertificate, relevant: set[str] | None) -> int:
    """Quarantined records of a scoping certificate that can bear on the operation: parties
    unknown, or including a ``relevant`` address. ``relevant`` None counts every one.

    The rule of the engines before the per-control quarantine (subscription up to 0.4.0,
    redemption 0.5.0 and 0.6.0). A record an ExecutionLink names counts for every operation,
    as the unknown parties the adapter wrote for it until subscription 0.5.0 did; the record
    now keeps its addresses and lists the link apart (``execution_links``), and this reading
    gives the same count."""
    assert certificate.quarantined_records is not None
    return sum(
        1
        for record in certificate.quarantined_records
        if record.execution_links is not None or by_address(record, relevant)
    )


def quarantine_considered(
    certificate: CoverageCertificate, relevant: set[str] | None, operation_ref: str
) -> list[QuarantinedRecord]:
    """Quarantined records of a scoping certificate that bear on this operation:
    by their parties as in ``quarantine_affecting``, or because an ExecutionLink names their
    ledger operation as this operation's execution. A link to another operation does not
    make a record with known, foreign addresses relevant here."""
    assert certificate.quarantined_records is not None
    return [
        record
        for record in certificate.quarantined_records
        if by_address(record, relevant) or operation_ref in (record.execution_links or ())
    ]


# ------------------------------------------------------------- active certificates


@dataclass(frozen=True)
class ActiveCoverage:
    """The certificates of one source and fact type that still state something.

    ``active``: not replaced by a valid, explicit supersession among the given certificates.
    ``superseded``: replaced id -> the id replacing it. ``refused``: supersession claims
    that do not hold (another scope, a narrower route or range, recorded earlier), with
    why; a refused claim leaves the older certificate active.
    """

    active: tuple[CoverageCertificate, ...]
    superseded: Mapping[str, str]
    refused: tuple[str, ...]

    def records(self) -> list[tuple[CoverageCertificate, QuarantinedRecord]]:
        """Every quarantined record of the active certificates, each distinct copy once."""
        seen: set[tuple[str, str]] = set()
        found = []
        for certificate in self.active:
            for record in certificate.quarantined_records or ():
                key = (record.locator, record.model_dump_json())
                if key not in seen:
                    seen.add(key)
                    found.append((certificate, record))
        return found


def active_coverage(certificates: Sequence[CoverageCertificate]) -> ActiveCoverage:
    """Which certificates still state their quarantine and coverage.

    Never "the most recent one": a certificate stops applying only when another one of the
    same source, instrument and chain scope explicitly supersedes it, with a route that
    includes its route and a ledger range that contains its range. The order in which
    certificates were recorded or are given does not change the result.
    """
    ordered = sorted(certificates, key=lambda c: (c.recorded_at, c.coverage_id))
    superseded: dict[str, str] = {}
    refused: list[str] = []
    for newer in ordered:
        for older_id in (entry.coverage_id for entry in newer.supersedes or ()):
            older = next((c for c in ordered if c.coverage_id == older_id), None)
            if older is None:
                continue  # not in the snapshot: nothing to replace here
            problem = supersession_problem(newer, older)
            if problem is None:
                superseded[older_id] = newer.coverage_id
            else:
                refused.append(f"{newer.coverage_id} does not supersede {older_id}: {problem}")
    active = tuple(c for c in ordered if c.coverage_id not in superseded)
    return ActiveCoverage(active, superseded, tuple(refused))


def covers_absence(certificate: CoverageCertificate, effects: Sequence[UncoveredEffect]) -> bool:
    """Whether ``certificate`` can support the absence of ``effects``: never for an
    effect its chain scope declares not covered (by role and route: a holder's routes and
    the issuer's Horizon route do not see a claimable balance clawback; no route sees a
    third party's claim of a balance unless the target is the claimant)."""
    declared = set(certificate.chain_scope.not_covered or ()) if certificate.chain_scope else set()
    return not declared.intersection(effects)


# The routes an absence can rest on. A Horizon-only certificate does not see the
# movements Horizon does not list for the account (a claim of a claimable balance by the
# account itself, the fill of its own offer: SAC events only), which no
# declarable ``not_covered`` effect names; only a route that includes the SAC events does.
ABSENCE_ROUTES = frozenset(
    route for route, included in ROUTE_INCLUDES.items() if "rpc_sac_events" in included
)


def absence_scope(
    certificates: Sequence[CoverageCertificate],
    representations: Sequence[StellarClassicRepresentation],
    addresses: Set[str] | None,
    effects: Sequence[UncoveredEffect],
) -> tuple[str | None, tuple[CoverageCertificate, ...]]:
    """Whether these chain certificates can show an absence on the chain source:
    the reason they cannot (None when they can) and the certificates that count.

    A certificate counts only if its chain scope is declared, names the network and asset of
    one of the profile's representations, was read through a route that includes the SAC
    events (``ABSENCE_ROUTES``), declares what it leaves out of sight (``not_covered``,
    possibly empty: a missing declaration is never read as "nothing") and leaves none of
    ``effects`` out. Together they must read every address approved for the account (base
    accounts of G or M addresses); the issuer's certificate is not enough, since it does not
    see what only the holder's routes see. ``addresses`` None: the account is not known."""
    if not certificates:
        return "no chain certificate meets the coverage", ()
    usable: list[CoverageCertificate] = []
    reasons: list[str] = []
    for certificate in certificates:
        scope = certificate.chain_scope
        if scope is None:
            reasons.append(f"{certificate.coverage_id} declares no chain scope")
            continue
        if not any(
            (r.network, r.asset_code, r.issuer)
            == (scope.network, scope.asset_code, scope.asset_issuer)
            for r in representations
        ):
            reasons.append(
                f"{certificate.coverage_id} reads {scope.asset_code}:{scope.asset_issuer} on "
                f"{scope.network}, not a representation of the instrument"
            )
            continue
        if scope.route not in ABSENCE_ROUTES:
            reasons.append(
                f"{certificate.coverage_id} reads only {scope.route}, which does not see the "
                "movements Horizon does not list for the account (SAC events)"
            )
            continue
        if scope.not_covered is None:
            reasons.append(
                f"{certificate.coverage_id} does not declare what it leaves out of sight "
                "(not_covered)"
            )
            continue
        unseen = sorted(set(scope.not_covered).intersection(effects))
        if unseen:
            reasons.append(f"{certificate.coverage_id} declares out of sight: {', '.join(unseen)}")
            continue
        usable.append(certificate)
    read = {c.chain_scope.account for c in usable if c.chain_scope is not None}
    if addresses is None:
        reasons.append("the operation's account is not known, so its addresses are not either")
    elif not addresses:
        reasons.append("no address is approved for the operation's account")
    else:
        missing = sorted({base_account(a) for a in addresses} - read)
        if not missing:
            needed = {base_account(a) for a in addresses}
            return None, tuple(
                c for c in usable if c.chain_scope is not None and c.chain_scope.account in needed
            )
        reasons.append(f"no usable certificate reads {', '.join(missing)}")
    return "; ".join(reasons), ()


def strict_absence_scope(
    certificates: Sequence[CoverageCertificate],
    representations: Sequence[StellarClassicRepresentation],
    addresses: Mapping[str, Set[str]] | None,
    effects: Sequence[UncoveredEffect],
) -> tuple[str | None, tuple[CoverageCertificate, ...]]:
    """With ``completeness_needs_coverage``: whether these chain certificates
    can show that the records of ``representations`` are complete, with the reason they
    cannot (None when they can) and the certificates to cite.

    A certificate counts only if its chain scope is declared, was read through a route that
    includes the SAC events, declares ``not_covered`` coherently with what its route and role
    can observe (``scope_coherence_problem``: an empty declaration never covers what the route
    does not see) and leaves none of ``effects`` out. Each representation the control uses
    needs counting certificates of its own network, asset code and issuer (never another
    representation's) that read every address approved for the account on that network
    (``addresses``: network -> addresses; None when the account is not known)."""
    if not certificates:
        return "no chain certificate meets the coverage", ()
    usable: list[CoverageCertificate] = []
    reasons: list[str] = []
    for certificate in certificates:
        scope = certificate.chain_scope
        cid = certificate.coverage_id
        if scope is None:
            reasons.append(f"{cid} declares no chain scope")
        elif scope.route not in ABSENCE_ROUTES:
            reasons.append(
                f"{cid} reads only {scope.route}, which does not see the movements Horizon "
                "does not list for the account (SAC events)"
            )
        elif scope.not_covered is None:
            reasons.append(f"{cid} does not declare what it leaves out of sight (not_covered)")
        elif (incoherent := scope_coherence_problem(scope)) is not None:
            reasons.append(f"{cid} {incoherent}")
        elif unseen := sorted(set(scope.not_covered).intersection(effects)):
            reasons.append(f"{cid} declares out of sight: {', '.join(unseen)}")
        else:
            usable.append(certificate)
    if addresses is None:
        reasons.append("the operation's account is not known, so its addresses are not either")
        return "; ".join(reasons), ()
    failed = False
    cited: list[CoverageCertificate] = []
    for rep in representations:
        label = f"{rep.representation_id} ({rep.asset_code}:{rep.issuer} on {rep.network})"
        mine = [
            c
            for c in usable
            if c.chain_scope is not None
            and (c.chain_scope.network, c.chain_scope.asset_code, c.chain_scope.asset_issuer)
            == (rep.network, rep.asset_code, rep.issuer)
        ]
        needed = {base_account(a) for a in addresses.get(rep.network, set())}
        if not mine:
            reasons.append(f"no usable certificate reads representation {label}")
            failed = True
        elif not needed:
            reasons.append(f"no address on {rep.network} is approved for the operation's account")
            failed = True
        elif missing := sorted(
            needed - {c.chain_scope.account for c in mine if c.chain_scope is not None}
        ):
            reasons.append(f"no usable certificate of {label} reads {', '.join(missing)}")
            failed = True
        else:
            cited += [c for c in mine if c.chain_scope and c.chain_scope.account in needed]
    if failed or not representations:
        return "; ".join(reasons) or "no representation to cover", ()
    return None, tuple(cited)


def addresses_by_network(
    links: Sequence[IdentityLink], account: str | None
) -> dict[str, set[str]] | None:
    """The addresses approved for ``account`` (any window), per network; None when the
    account is not known."""
    if account is None:
        return None
    found: dict[str, set[str]] = {}
    for link in links:
        if link.account_ref == account:
            found.setdefault(link.network, set()).add(link.address)
    return found


def scope_shortfall(
    certificates: Sequence[CoverageCertificate],
    representations: Sequence[StellarClassicRepresentation],
    addresses: Set[str] | None,
    effects: Sequence[UncoveredEffect],
) -> str | None:
    """Why these chain certificates cannot show an absence (``absence_scope``), or None."""
    return absence_scope(certificates, representations, addresses, effects)[0]


# Adapter reasons compatible with an identified clawback whose correspondence or origin is
# incomplete: anything else on the record contradicts that nature.
CLAWBACK_REASONS = frozenset(
    {
        "CORRESPONDENCE_CONFLICT",
        "CLAWBACK_CLASSIC_UNAVAILABLE",
        "CLAWBACK_SAC_NATIVE",
        "EFFECT_NOT_A_MOVEMENT",
        "UNSUPPORTED_OPERATION",
    }
)


def _operation_of(o: Observation) -> tuple[str, str, bool] | None:
    """(``tx:op``, network, successful) of a chain observation, else None."""
    payload = o.payload
    if isinstance(payload, (TokenMovementPayload, ChainEffectPayload)):
        chain = payload.chain
        return f"{chain.tx_hash}:{chain.operation_index}", chain.network, chain.tx_successful
    return None


def _parties_of(o: Observation) -> tuple[set[str], bool]:
    """(identified ledger parties, whether an unresolved participant is named)."""
    payload = o.payload
    if isinstance(payload, TokenMovementPayload):
        # Base accounts and any muxed sub-account (identical to before for other movements).
        return movement_addresses(payload), False
    assert isinstance(payload, ChainEffectPayload)
    named = effect_parties(payload)
    return {p.id for p in named if p.id is not None}, any(p.kind == "unresolved" for p in named)


@dataclass(frozen=True)
class ChainEvidence:
    """The snapshot's own chain observations of one source and instrument, per ledger
    operation, on the profile's networks; withdrawn records are left out."""

    by_operation: Mapping[str, tuple[Observation, ...]]
    by_locator: Mapping[str, str]
    elsewhere: frozenset[str]

    def located(self, locator: str) -> str | None:
        """The ledger operation of the observation read from the same raw record."""
        if locator in self.by_locator:
            return self.by_locator[locator]
        for raw, operation in self.by_locator.items():
            if locator.startswith(raw + "#") or raw.startswith(locator + "#"):
                return operation
        return None


def chain_evidence(
    members: Sequence[Observation], source: str, instrument_id: str, networks: Set[str]
) -> ChainEvidence:
    by_operation: dict[str, list[Observation]] = {}
    by_locator: dict[str, str] = {}
    elsewhere: set[str] = set()
    for state, observations in resolve_records(
        [o for o in members if o.fact_type in ("token_movement", "chain_effect")]
    ):
        if state == "withdrawn":
            continue
        for o in observations:
            located = _operation_of(o)
            if located is None:
                continue
            operation, network, _ = located
            if (
                o.source.source_id != source
                or o.instrument_id != instrument_id
                or (network not in networks)
            ):
                elsewhere.add(operation)
                continue
            by_operation.setdefault(operation, []).append(o)
            by_locator[o.provenance.raw_locator] = operation
    return ChainEvidence(
        {k: tuple(v) for k, v in by_operation.items()}, by_locator, frozenset(elsewhere)
    )


@dataclass(frozen=True)
class RecordSupport:
    """What the snapshot supports of one quarantined record.

    ``operation``: its ledger operation, only when observations of the snapshot (same
    source, instrument and network) are on it and none read from the same raw record says
    otherwise; None means unknown. ``clawback``: an identified clawback the snapshot backs
    (a successful clawback effect on that operation, nothing else there, reasons and parties
    consistent). ``bears``: the snapshot shows a party of that operation the record omits
    that is relevant (or unknown): the record bears on the operation whatever its addresses
    say. ``notes``: each contradiction or missing support, for the explanation.

    An internal consistency check between artifacts of the snapshot, not an independent
    verification of the ledger: the observations are as trustworthy as the source.
    """

    operation: str | None
    clawback: bool
    bears: bool
    notes: tuple[str, ...]


def record_support(
    record: QuarantinedRecord, evidence: ChainEvidence, relevant: set[str] | None
) -> RecordSupport:
    notes: list[str] = []
    claimed = record.chain_operation
    located = evidence.located(record.locator)
    # A correspondence check names its operation in its locator (``correspondence#<target>:
    # <tx>:<op>@<evidence>``): it must be the one claimed (Stellar review B2).
    named = None
    if record.locator.startswith("correspondence#"):
        parts = record.locator.split("#", 1)[1].split("@", 1)[0].split(":")
        named = ":".join(parts[-2:]) if len(parts) >= 3 else None
    operation: str | None = None
    if claimed is None:
        pass
    elif named is not None and named != claimed:
        notes.append(f"its locator names {named}, not {claimed}")
    elif located is not None and located != claimed:
        notes.append(f"its locator is the raw record of {located}, not of {claimed}")
    elif claimed not in evidence.by_operation:
        notes.append(
            f"no observation of the snapshot on {claimed} for this source, asset and network"
            + (" (only of another one)" if claimed in evidence.elsewhere else "")
        )
    else:
        operation = claimed
    # Parties the snapshot shows on the ledger operations the record is tied to.
    shown: set[str] = set()
    unresolved = False
    for op in {o for o in (claimed, located) if o is not None}:
        for o in evidence.by_operation.get(op, ()):
            parties, unknown = _parties_of(o)
            shown |= parties
            unresolved = unresolved or unknown
    omitted: set[str] = set()
    if record.addresses is not None:
        omitted = shown - set(record.addresses)
        if omitted:
            notes.append("the snapshot shows parties it omits: " + ", ".join(sorted(omitted)))
        if unresolved:
            notes.append("the snapshot names an unresolved participant it does not")
    bears = record.addresses is not None and (
        unresolved or relevant is None or bool(omitted & relevant)
    )
    clawback = False
    if record.nature == "clawback_identified":
        observations = evidence.by_operation.get(operation or "", ())
        clawbacks = [
            o
            for o in observations
            if isinstance(o.payload, ChainEffectPayload) and o.payload.effect_kind == "clawback"
        ]
        if operation is None:
            notes.append("an identified clawback on a ledger operation the snapshot does not back")
        elif not clawbacks:
            notes.append(f"no clawback effect of the snapshot on {operation}")
        elif len(clawbacks) != len(observations):
            notes.append(f"the snapshot holds another kind of record on {operation}")
        elif not all(
            isinstance(o.payload, ChainEffectPayload) and o.payload.chain.tx_successful
            for o in clawbacks
        ):
            notes.append(f"the clawback transaction on {operation} did not succeed")
        elif not set(record.reasons) <= CLAWBACK_REASONS:
            notes.append(
                "its reasons contradict an identified clawback: "
                + ", ".join(sorted(set(record.reasons) - CLAWBACK_REASONS))
            )
        elif omitted or unresolved or record.addresses is None:
            notes.append("its parties do not match the clawback's")
        elif relevant is None or (set(record.addresses) - shown) & relevant:
            # It names a relevant party the clawback does not involve (review B2).
            notes.append("it names a relevant party the snapshot's clawback does not involve")
        else:
            clawback = True
    return RecordSupport(operation, clawback, bears, tuple(notes))


def engine_problem(
    engine_ref: str, runnable: Mapping[str, object], operation_type: str, replaying: bool
) -> str | None:
    """Why ``engine_ref`` cannot run here, or None. A retired engine runs only to replay a
    recorded conclusion; nothing else is ever substituted."""
    if engine_ref in BLOCKED_ENGINES:
        return f"engine {engine_ref} is blocked by policy: {BLOCKED_ENGINES[engine_ref]}"
    if engine_ref not in runnable:
        return f"engine {engine_ref} does not evaluate {operation_type} profiles"
    if engine_ref not in CURRENT_ENGINES and not replaying:
        return (
            f"engine {engine_ref} is retired for new evaluations "
            f"({RETIRED_ENGINES.get(engine_ref, 'not current')}); it only replays recorded "
            "conclusions"
        )
    return None


def evaluation_id(snapshot: SnapshotRef, profile_ref: str, engine_ref: str) -> str:
    material = json.dumps(
        {
            "snapshot": snapshot.model_dump(mode="json"),
            "profile": profile_ref,
            "engine": engine_ref,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "eval-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
