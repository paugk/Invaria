"""Dependencies of an operation's evaluation, as predicates over future evidence.

Pure: derived only from the profile and the operation. Predicates (not member lists) are
what make absence invalidatable: a new observation that matches a predicate can change an
evaluation that was UNKNOWN because that evidence was missing. Matching over-approximates
what the evaluator reads; reprocessing too much is safe, missing a dependency is not.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.observation import FactType, Observation
from invaria.contracts.profile import Profile

DEPENDENCIES_REF = "invaria-dependencies@0.1.0"
# Facts whose unlinked observations the evaluator reads: token movements (ambiguity) and,
# for redemptions, bank payments to the account (ambiguity). Approved prices are read
# whatever their operation_ref, since the request names them by price_ref.
UNLINKED_FACTS: frozenset[FactType] = frozenset({"token_movement"})
UNLINKED_REDEMPTION_FACTS: frozenset[FactType] = frozenset({"token_movement", "cash_settled"})
# Approved prices (named by price_ref) and TA journal changes of the account (position
# reconstruction) are read whatever their operation_ref.
ANY_OPERATION_FACTS: frozenset[FactType] = frozenset({"price_approved", "position_changed"})


@dataclass(frozen=True)
class ObservationPredicate:
    """Authoritative observations of one fact type for the operation.

    ``unlinked_too`` also matches observations without ``operation_ref``: unlinked token
    movements to the investor's address make the evaluator report ambiguity.
    """

    fact_type: FactType
    source_id: str
    instrument_id: str
    representation_ids: frozenset[str] | None
    unlinked_too: bool
    any_operation: bool = False  # approved prices are named by price_ref, not by operation


@dataclass(frozen=True)
class CoveragePredicate:
    source_id: str
    fact_type: FactType
    instrument_id: str


@dataclass(frozen=True)
class Dependencies:
    operation_ref: str
    profile_ref: str
    observations: tuple[ObservationPredicate, ...]
    coverage: tuple[CoveragePredicate, ...]
    any_identity_link: bool = True  # conservative: links are matched through the order

    def matches_observation(self, o: Observation, operation_refs: Iterable[str | None]) -> bool:
        """``operation_refs``: the observation's own ref plus the refs of what it supersedes."""
        refs = set(operation_refs)
        for p in self.observations:
            if (o.fact_type, o.source.source_id, o.instrument_id) != (
                p.fact_type,
                p.source_id,
                p.instrument_id,
            ):
                continue
            if p.representation_ids is not None and o.representation_id not in p.representation_ids:
                continue
            if p.any_operation:
                return True
            if self.operation_ref in refs or (p.unlinked_too and None in refs):
                return True
        return False

    def matches_coverage(self, c: CoverageCertificate) -> bool:
        return any(
            c.source_id == p.source_id
            and c.instrument_id == p.instrument_id
            and p.fact_type in c.fact_types
            for p in self.coverage
        )


def dependencies(profile: Profile, operation_ref: str) -> Dependencies:
    instrument = profile.instrument.instrument_id
    representations = frozenset(r.representation_id for r in profile.representations)
    fact_types: list[FactType] = sorted({f for c in profile.controls for f in c.requires})
    redemption = profile.operation_type == "redemption"
    unlinked = UNLINKED_REDEMPTION_FACTS if redemption else UNLINKED_FACTS
    observations = tuple(
        ObservationPredicate(
            fact_type=f,
            source_id=profile.authority_for(f),
            instrument_id=instrument,
            representation_ids=representations if f == "token_movement" else None,
            unlinked_too=f in unlinked,
            any_operation=f in ANY_OPERATION_FACTS,
        )
        for f in fact_types
    )
    coverage = tuple(
        sorted(
            {
                CoveragePredicate(source_id=r.source_id, fact_type=f, instrument_id=instrument)
                for r in profile.coverage_requirements
                for f in r.fact_types
            },
            key=lambda p: (p.source_id, p.fact_type),
        )
    )
    return Dependencies(
        operation_ref=operation_ref,
        profile_ref=profile.profile_ref,
        observations=observations,
        coverage=coverage,
    )
