"""Reevaluation worker: read the epoch, close a snapshot, evaluate, save, publish (CAS).

No wall clock: ``valid_at``, ``known_at`` and the evaluation clock are always explicit.
The epoch is read *before* the snapshot is built; if anything changes meanwhile, the
publication is refused (NOT_CURRENT) and the attempt is kept. Handlers are idempotent, so
an outbox event delivered twice yields the same evaluation and ALREADY_PUBLISHED.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from invaria.engine.evaluate import Evaluation, evaluate
from invaria.engine.versions import CURRENT_ENGINES
from invaria.persistence.store import OutboxEvent, PgStore, Publication


@dataclass(frozen=True)
class Clocks:
    valid_at: datetime
    known_at: datetime
    evaluation_clock: datetime


@dataclass(frozen=True)
class Prepared:
    tenant_id: str
    operation_ref: str
    epoch: int
    evaluation: Evaluation


def snapshot_id_for(operation_ref: str, epoch: int, clocks: Clocks) -> str:
    material = json.dumps(
        [
            operation_ref,
            epoch,
            clocks.valid_at.isoformat(),
            clocks.known_at.isoformat(),
            clocks.evaluation_clock.isoformat(),
        ]
    )
    return f"snap-e{epoch}-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def prepare(
    store: PgStore,
    tenant_id: str,
    operation_ref: str,
    clocks: Clocks,
    *,
    engine_ref: str | None = None,
) -> Prepared:
    if engine_ref is not None and engine_ref not in CURRENT_ENGINES:
        # New evaluations use current engines only; retired ones only replay.
        raise ValueError(f"engine {engine_ref} is not selectable for new evaluations")
    head = store.scope_head(tenant_id, operation_ref)  # first: the epoch this work is for
    snapshot = store.build_snapshot(
        snapshot_id=snapshot_id_for(operation_ref, head.epoch, clocks),
        tenant_id=tenant_id,
        operation_ref=operation_ref,
        profile_ref=head.profile_ref,
        valid_at=clocks.valid_at,
        known_at=clocks.known_at,
        evaluation_clock=clocks.evaluation_clock,
    )
    evaluation = evaluate(store.load_inputs(tenant_id, snapshot.snapshot_id), engine_ref=engine_ref)
    store.save_evaluation(tenant_id, evaluation.result)
    return Prepared(tenant_id, operation_ref, head.epoch, evaluation)


def reevaluate(
    store: PgStore,
    tenant_id: str,
    operation_ref: str,
    clocks: Clocks,
    *,
    engine_ref: str | None = None,
) -> tuple[Prepared, Publication]:
    prepared = prepare(store, tenant_id, operation_ref, clocks, engine_ref=engine_ref)
    publication = store.publish(tenant_id, prepared.evaluation.result.evaluation_id, prepared.epoch)
    return prepared, publication


ClocksFor = Callable[[OutboxEvent], Clocks]


def handle_event(store: PgStore, event: OutboxEvent, clocks_for: ClocksFor) -> Publication | None:
    """Reevaluate on ``scope.invalidated``; an event for an already superseded epoch is
    skipped, since the newer epoch has its own event."""
    if event.event_type != "scope.invalidated":
        return None
    if store.scope_head(event.tenant_id, event.operation_ref).epoch > event.epoch:
        return None
    _, publication = reevaluate(store, event.tenant_id, event.operation_ref, clocks_for(event))
    return publication


def drain(
    store: PgStore, consumer_id: str, clocks_for: ClocksFor, *, limit: int = 100
) -> list[tuple[OutboxEvent, Publication | None]]:
    """Process pending events, acknowledging each only after it was handled."""
    handled = []
    for event in store.outbox_pending(consumer_id, limit):
        handled.append((event, handle_event(store, event, clocks_for)))
        store.outbox_ack(consumer_id, event.event_seq)
    return handled
