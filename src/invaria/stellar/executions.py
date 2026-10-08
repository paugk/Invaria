"""Economic identity of on-chain executions across representations and targets.

One execution can reach the store as several records: a clawback as its Classic operation
and its SAC event, each seen from the holder's target and from the issuer's. Records of
canonical effects (the same record key for every target of the asset) integrate once per
record; this module groups the remaining representations, so that a query or an
aggregation never counts them as additional executions.

Scope: effects whose identity is one affected party of one operation (``clawback``, the
claimable balance effects, ``contract_transfer``, ``pool_transfer``). Path payments and
DEX fills are reconciled per operation and account (``correspondence.jsonl``); they have
no per-effect identity and are not grouped here. Pure: no I/O. No product query
aggregates chain effects today (the engines only list the ones bearing on an operation and
never sum them); this is the identity such a query must use.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from invaria.contracts.observation import (
    ChainEffectPayload,
    ChainParty,
    Observation,
    party_identity,
)
from invaria.engine.common import resolve_records

GROUPED = frozenset(
    {
        "clawback",
        "claimable_balance_created",
        "claimable_balance_claimed",
        "contract_transfer",
        "pool_transfer",
    }
)


@dataclass(frozen=True)
class Execution:
    """One execution and every distinct record that represents it.

    ``key``: network, transaction, operation, effect kind, instrument, the affected party's
    typed identity, and its occurrence among that party's effects of the operation (a
    contract call may claw back one holder twice; a Classic operation has one).
    ``status``: ``single`` (one representation), ``corroborated`` (Classic and SAC agree on
    party, direction and amount) or ``conflict`` (they do not, or one record has two
    contents; every copy is kept and neither is chosen)."""

    key: tuple[str, str, int, str, str, str, int]
    representations: tuple[Observation, ...]
    status: Literal["single", "corroborated", "conflict"]


def _affected(payload: ChainEffectPayload) -> ChainParty:
    if payload.account is not None:
        return ChainParty(kind="account", id=payload.account)
    assert payload.holder is not None
    return payload.holder


def _ordinal(observation: Observation) -> int:
    """The SAC event ordinal of a canonical record key (``<tx>:<op>:sac:<n>:<kind>``)."""
    parts = observation.source.record_key.split(":")
    return int(parts[3]) if len(parts) > 3 and parts[2] == "sac" else 0


def executions(observations: Sequence[Observation]) -> list[Execution]:
    """Group the grouped chain effects of ``observations`` into executions.

    Records are first resolved per (source, record key): the same canonical record seen
    by two targets or two runs is one record, and one with two contents makes its execution
    a conflict. Within an operation, the n-th effect of a
    party on one representation is the same execution as the n-th on the other."""
    candidates = [
        o
        for o in observations
        if isinstance(o.payload, ChainEffectPayload) and o.payload.effect_kind in GROUPED
    ]
    # Resolved per (source, record key) like the engines: one record however many targets
    # or runs saw it; a withdrawn one counts for nothing; two contents keep both copies.
    kept: list[Observation] = []
    copies: dict[str, list[Observation]] = {}
    for state, found in resolve_records(candidates):
        if state == "withdrawn":
            continue
        kept.append(found[0])
        if state == "conflict":
            copies[found[0].observation_id] = list(found)
    by_side: dict[tuple[str, str, int, str, str, str, str], list[Observation]] = {}
    for o in kept:
        payload = o.payload
        assert isinstance(payload, ChainEffectPayload)
        chain = payload.chain
        party = party_identity(chain.network, _affected(payload))
        side: tuple[str, str, int, str, str, str, str] = (
            chain.network,
            chain.tx_hash,
            chain.operation_index,
            payload.effect_kind,
            o.instrument_id,
            party,
            payload.representation,
        )
        by_side.setdefault(side, []).append(o)
    grouped: dict[tuple[str, str, int, str, str, str, int], list[Observation]] = {}
    for side, found in sorted(by_side.items()):
        for occurrence, o in enumerate(sorted(found, key=_ordinal)):
            grouped.setdefault((*side[:6], occurrence), []).append(o)
    result = []
    for key, found in sorted(grouped.items()):
        in_conflict = any(o.observation_id in copies for o in found)
        found = [c for o in found for c in copies.get(o.observation_id, [o])]
        payloads = [o.payload for o in found]
        assert all(isinstance(p, ChainEffectPayload) for p in payloads)
        views = {
            (p.direction, p.units.atoms if p.units else None)
            for p in payloads
            if isinstance(p, ChainEffectPayload)
        }
        status: Literal["single", "corroborated", "conflict"]
        if in_conflict or len(views) > 1:
            status = "conflict"
        else:
            status = "single" if len(found) == 1 else "corroborated"
        result.append(
            Execution(key, tuple(sorted(found, key=lambda o: o.source.record_key)), status)
        )
    return result
