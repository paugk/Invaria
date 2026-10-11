"""Derivation of domain events from the outbox. Local, one-shot, resumable.

Per outbox row without an acknowledgement of the ``domain-events`` consumer:

1. **Plan, without locks.** Read the fact and the documents it pins (all append-only and
   never updated by the application roles) and build the event. Stored documents are
   compared without engines; blocks need a requirement-diagnosis replay, bounded by ``Limits``. The
   tenant write lock is never taken, so no replay ever runs while producers wait.
2. **Record, in one short transaction.** Lock the source row (advisory, per
   ``event_seq``), re-check under it that no event or acknowledgement exists, re-validate
   that the pinned fact rows still exist, insert the event (the database trigger assigns
   ``tenant_seq`` under the per-tenant counter row lock) and the acknowledgement. Commit.

Lock order: producers take the tenant write lock and never touch the derivation locks;
the deriver takes the source lock, then (through the trigger) the tenant counter row, and
never the tenant write lock. One source per transaction. Hence no lock cycle.

Errors: a per-event limit (too many UNKNOWN controls, ``statement_timeout``, comparison
disabled with ``max_replays_per_run=0``) or a known diagnostic limitation still yields the
event, with ``blocks`` not comparable and a structured reason. The per-run replay budget
never does: when it runs out the run stops (``paused``) and the source stays pending.
A transient infrastructure error (``psycopg.OperationalError`` other than a cancellation
by ``statement_timeout``: lost connection, ``lock_timeout``, operator cancellation) aborts
that source only: no event, no acknowledgement, no number, retried by the next run. Any
other database error is reported and leaves the source pending; it never becomes a
``not_comparable`` reason.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg import errors

from invaria import domain_events as de
from invaria.contracts.evaluation import EvaluationResult
from invaria.contracts.events import (
    EvaluationPublishedEvent,
    PublicationRef,
    ReportRegisteredEvent,
    ScopeReopenedEvent,
    SnapshotTimes,
    canonical_bytes,
)
from invaria.engine.common import Evaluation, EvaluationInputs
from invaria.engine.evaluate import replay
from invaria.persistence.store import OUTBOX_PAYLOAD_VERSION, PgStore

CONSUMER = "domain-events"
Event = ScopeReopenedEvent | EvaluationPublishedEvent | ReportRegisteredEvent
Outcome = Literal["derived", "not_public", "already_derived"]


@dataclass(frozen=True)
class Source:
    event_seq: int
    tenant_id: str
    event_type: str
    operation_ref: str | None
    epoch: int | None
    dedup_key: str
    payload: dict[str, Any]
    inserted_at: datetime

    @property
    def pinned(self) -> bool:
        return self.payload.get("payload_version") == OUTBOX_PAYLOAD_VERSION


@dataclass
class RunResult:
    derived: int = 0
    not_public: int = 0
    already_derived: int = 0
    errors: list[str] = field(default_factory=list)
    pending: int = 0
    replays: int = 0
    limited: int = 0
    paused: bool = False  # the replay budget ran out: run again to continue

    def as_dict(self) -> dict[str, Any]:
        return {
            "derived": self.derived,
            "not_public": self.not_public,
            "already_derived": self.already_derived,
            "errors": self.errors,
            "pending": self.pending,
            "replays": self.replays,
            "limited": self.limited,
            "paused": self.paused,
        }


class SourceInconsistent(Exception):
    """The fact a source row names is not in the store as named: no event is derived."""


# ----------------------------------------------------------------------- reads (plan)


def _one(store: PgStore, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
    row = store.conn.execute(sql, params).fetchone()
    store.conn.commit()
    return None if row is None else tuple(row)


def load_source(store: PgStore, event_seq: int) -> Source:
    row = _one(
        store,
        "SELECT event_seq, tenant_id, event_type, operation_ref, epoch, dedup_key, payload, "
        "inserted_at FROM invaria.outbox WHERE event_seq = %s",
        (event_seq,),
    )
    if row is None:
        raise SourceInconsistent(f"outbox row {event_seq} not found")
    return Source(
        int(row[0]),
        str(row[1]),
        str(row[2]),
        None if row[3] is None else str(row[3]),
        None if row[4] is None else int(row[4]),
        str(row[5]),
        dict(row[6]),
        row[7],
    )


def _publication(store: PgStore, tenant: str, op: str, epoch: int) -> tuple[str, datetime] | None:
    row = _one(
        store,
        "SELECT evaluation_id, inserted_at FROM invaria.publications WHERE tenant_id = %s "
        "AND operation_ref = %s AND epoch = %s",
        (tenant, op, epoch),
    )
    return None if row is None else (str(row[0]), row[1])


def _history_prior(store: PgStore, tenant: str, op: str, epoch: int) -> PublicationRef | None:
    row = _one(
        store,
        "SELECT epoch, evaluation_id FROM invaria.publications WHERE tenant_id = %s "
        "AND operation_ref = %s AND epoch < %s ORDER BY epoch DESC LIMIT 1",
        (tenant, op, epoch),
    )
    return None if row is None else PublicationRef(epoch=int(row[0]), evaluation_id=str(row[1]))


def _pinned_ref(value: Any) -> PublicationRef | None:
    if value is None:
        return None
    return PublicationRef(epoch=int(value["epoch"]), evaluation_id=str(value["evaluation_id"]))


def _prior(store: PgStore, s: Source, key: str) -> tuple[PublicationRef | None, str | None]:
    """The publication the fact refers to, pinned (newer rows) or read back from history
    (older rows), and why it cannot be named unambiguously, if so."""
    assert s.operation_ref is not None and s.epoch is not None
    from_history = _history_prior(store, s.tenant_id, s.operation_ref, s.epoch)
    if not s.pinned:
        # Stable by construction: once epoch e exists no publication below e can appear.
        return from_history, None
    pinned = _pinned_ref(s.payload.get(key))
    if pinned != from_history:
        return None, "the pinned publication differs from the publication history"
    return pinned, None


STATEMENT_TIMEOUT = "canceling statement due to statement timeout"


def _loader(store: PgStore, tenant: str, limits: de.Limits) -> de.Loader:
    """Load the pinned inputs of one diagnosis under the run's timeouts.

    Only a statement cancelled by ``statement_timeout`` (the configured execution budget)
    is a policy limit: the comparison is then not comparable. A lock wait beyond
    ``lock_timeout`` (contention with another session) and any other cancellation are
    transient: they propagate, so nothing of the source is written and it is retried.
    The aborted transaction is rolled back before anything else runs on the connection."""

    def load(doc: EvaluationResult) -> tuple[EvaluationInputs, Evaluation]:
        saved = _one(
            store,
            "SELECT current_setting('statement_timeout'), current_setting('lock_timeout')",
            (),
        )
        store.conn.execute(
            "SELECT set_config('statement_timeout', %s, false), "
            "set_config('lock_timeout', %s, false)",
            (str(limits.statement_timeout_ms), str(limits.lock_timeout_ms)),
        )
        try:
            inputs = store.load_inputs(tenant, doc.snapshot_id)
        except errors.QueryCanceled as error:
            store.conn.rollback()
            if error.diag.message_primary == STATEMENT_TIMEOUT:
                raise de.DiagnosisLimit("statement_timeout") from error
            raise  # cancelled for another reason (e.g. by an operator): transient
        except errors.LockNotAvailable:
            store.conn.rollback()
            raise  # lock contention: transient, retried by the next run
        finally:
            if not store.conn.closed and not store.conn.broken:
                store.conn.rollback()
                store.conn.execute(
                    "SELECT set_config('statement_timeout', %s, false), "
                    "set_config('lock_timeout', %s, false)",
                    (saved[0], saved[1]) if saved else ("0", "0"),
                )
                store.conn.commit()
        return inputs, replay(inputs, doc.versions.engine_ref)

    return load


def plan(store: PgStore, s: Source, budget: de.Budget) -> Event | None:
    """The event a source row produces, or None when it has no public type."""
    if s.event_type == "scope.invalidated":
        return _plan_reopened(store, s)
    if s.event_type == "evaluation.published":
        return _plan_published(store, s, budget)
    if s.event_type == "integrity_report.registered":
        return _plan_report(store, s)
    raise SourceInconsistent(f"unknown outbox type {s.event_type}")


def _plan_reopened(store: PgStore, s: Source) -> ScopeReopenedEvent | None:
    assert s.operation_ref is not None and s.epoch is not None
    row = _one(
        store,
        "SELECT cause, profile_ref, knowledge_floor, document, inserted_at "
        "FROM invaria.scope_epochs WHERE tenant_id = %s AND operation_ref = %s AND epoch = %s",
        (s.tenant_id, s.operation_ref, s.epoch),
    )
    if row is None:
        raise SourceInconsistent(f"epoch {s.epoch} of {s.operation_ref} not found")
    cause, profile_ref, floor, document, opened_at = row
    if cause == "registered":
        return None  # registering a scope loses no conclusion: no public event in v1
    prior, problem = _prior(store, s, "prior_publication")
    counts = None
    if cause == "evidence_appended":
        counts = (
            len(document.get("observation_ids", [])),
            len(document.get("coverage_ids", [])),
            len(document.get("identity_link_ids", [])),
        )
    return de.scope_reopened(
        dedup_key=s.dedup_key,
        operation_ref=s.operation_ref,
        epoch=s.epoch,
        cause=str(cause),
        profile_ref=str(profile_ref),
        previous_profile_ref=document.get("previous_profile_ref")
        if cause == "profile_changed"
        else None,
        evidence_counts=counts,
        knowledge_floor=floor if cause == "evidence_appended" else None,
        prior=prior,
        pinned=s.pinned,
        reopened_at=opened_at,
        unidentifiable=problem is not None,
    )


def _plan_published(store: PgStore, s: Source, budget: de.Budget) -> EvaluationPublishedEvent:
    assert s.operation_ref is not None and s.epoch is not None
    found = _publication(store, s.tenant_id, s.operation_ref, s.epoch)
    if found is None or found[0] != s.payload.get("evaluation_id"):
        raise SourceInconsistent(f"publication {s.operation_ref}@{s.epoch} not as named")
    evaluation_id, published_at = found
    current = store.load_evaluation(s.tenant_id, evaluation_id)
    snapshot = store.load_snapshot(s.tenant_id, current.snapshot_id)
    previous_ref, problem = _prior(store, s, "previous_publication")
    previous: EvaluationResult | None = None
    if problem is None and previous_ref is not None:
        prior_row = _publication(store, s.tenant_id, s.operation_ref, previous_ref.epoch)
        if prior_row is None or prior_row[1] > published_at:
            problem = "the previous publication is not recorded before this one"
        else:
            try:
                previous = store.load_evaluation(s.tenant_id, previous_ref.evaluation_id)
            except KeyError:
                problem = "the previous evaluation is not stored"
    change = de.compare(
        previous,
        current,
        _loader(store, s.tenant_id, budget.limits),
        budget,
        unidentifiable=problem,
    )
    return de.evaluation_published(
        dedup_key=s.dedup_key,
        epoch=s.epoch,
        current=current,
        snapshot=SnapshotTimes(
            snapshot_id=snapshot.snapshot_id,
            valid_at=snapshot.valid_at,
            known_at=snapshot.known_at,
            evaluation_clock=snapshot.evaluation_clock,
        ),
        previous=None if problem is not None else previous_ref,
        change=change,
        pinned=s.pinned,
        published_at=published_at,
    )


def _plan_report(store: PgStore, s: Source) -> ReportRegisteredEvent:
    report_id = str(s.payload.get("report_id"))
    record = store.load_integrity_report(s.tenant_id, report_id)
    if record is None or record.report_sha256 != s.payload.get("report_sha256"):
        raise SourceInconsistent(f"integrity report {report_id} not as named")
    assert record.registered_at is not None
    return de.report_registered(
        dedup_key=s.dedup_key,
        report_id=record.report_id,
        report_sha256=record.report_sha256,
        report_format=record.report_format,
        definition=record.definition,
        selection_policy=record.selection_policy,
        evidence_horizon=record.evidence_horizon,
        store_read_at=record.store_read_at,
        registered_at=record.registered_at,
    )


# --------------------------------------------------------------------- record (write)


def _before_ack(store: PgStore, s: Source) -> None:
    """Hook between the event insert and the acknowledgement (fault injection in tests)."""


def record(store: PgStore, s: Source, event: Event | None) -> Outcome:
    with store.conn.transaction():
        store.conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"invaria-derive:{s.event_seq}",),
        )
        done = store.conn.execute(
            "SELECT EXISTS (SELECT 1 FROM invaria.outbox_acks WHERE consumer_id = %s "
            "AND event_seq = %s) OR EXISTS (SELECT 1 FROM invaria.domain_events "
            "WHERE source_event_seq = %s)",
            (CONSUMER, s.event_seq, s.event_seq),
        ).fetchone()
        if done and done[0]:
            return "already_derived"
        if event is not None:
            payload = canonical_bytes(event)
            subject = event.subject
            store.conn.execute(
                "INSERT INTO invaria.domain_events (tenant_id, event_id, event_type, "
                "type_version, operation_ref, epoch, report_id, source_event_seq, payload, "
                "payload_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "encode(sha256(%s), 'hex'))",
                (
                    s.tenant_id,
                    event.event_id,
                    event.type,
                    event.type_version,
                    getattr(subject, "operation_ref", None),
                    getattr(event.data, "epoch", None),
                    getattr(subject, "report_id", None),
                    s.event_seq,
                    payload,
                    payload,
                ),
            )
        _before_ack(store, s)
        store.conn.execute(
            "INSERT INTO invaria.outbox_acks (consumer_id, event_seq) VALUES (%s, %s)",
            (CONSUMER, s.event_seq),
        )
    return "not_public" if event is None else "derived"


# ------------------------------------------------------------------------------ run


def pending_count(store: PgStore) -> int:
    row = _one(
        store,
        "SELECT count(*) FROM invaria.outbox o WHERE NOT EXISTS (SELECT 1 FROM "
        "invaria.outbox_acks a WHERE a.consumer_id = %s AND a.event_seq = o.event_seq)",
        (CONSUMER,),
    )
    return int(row[0]) if row else 0


def derive(
    store: PgStore,
    *,
    limit: int = 100,
    limits: de.Limits | None = None,
    planner: Callable[[PgStore, Source, de.Budget], Event | None] = plan,
) -> RunResult:
    """Derive up to ``limit`` pending sources, in ``event_seq`` order, each in its own
    transaction. Re-running resumes; a derived source is never derived again."""
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be 1..10000")
    budget = de.Budget(limits or de.Limits())
    result = RunResult()
    for pending in store.outbox_pending(CONSUMER, limit):
        try:
            source = load_source(store, pending.event_seq)
            outcome = record(store, source, planner(store, source, budget))
        except de.BudgetExhausted:
            store.conn.rollback()
            result.paused = True
            break
        except (SourceInconsistent, KeyError) as error:
            store.conn.rollback()
            result.errors.append(f"{pending.event_seq}: {type(error).__name__}: {error}")
            continue
        except psycopg.OperationalError as error:  # transient: retried by the next run
            if not store.conn.closed:
                store.conn.rollback()
            result.errors.append(f"{pending.event_seq}: transient: {type(error).__name__}")
            continue
        except psycopg.Error as error:  # a database defect: never turned into an event
            if not store.conn.closed:
                store.conn.rollback()
            result.errors.append(f"{pending.event_seq}: database: {type(error).__name__}")
            continue
        if outcome == "derived":
            result.derived += 1
        elif outcome == "not_public":
            result.not_public += 1
        else:
            result.already_derived += 1
    result.replays, result.limited = budget.replays, budget.limited
    result.pending = pending_count(store)
    return result


def events_of(store: PgStore, tenant_id: str) -> list[tuple[int, datetime, str, bytes]]:
    """(tenant_seq, derived_at, payload_sha256, payload) of one tenant, in tenant_seq order."""
    rows = store.conn.execute(
        "SELECT tenant_seq, derived_at, payload_sha256, payload FROM invaria.domain_events "
        "WHERE tenant_id = %s ORDER BY tenant_seq",
        (tenant_id,),
    ).fetchall()
    store.conn.commit()
    return [(int(r[0]), r[1], str(r[2]), bytes(r[3])) for r in rows]


def record_json(row: tuple[int, datetime, str, bytes]) -> dict[str, Any]:
    seq, derived_at, sha, payload = row
    return {
        "tenant_seq": seq,
        "derived_at": derived_at.isoformat(),
        "payload_sha256": sha,
        "event": json.loads(payload),
    }
