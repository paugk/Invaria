"""Invalidation and idempotency against a real PostgreSQL: epochs, CAS publication, outbox."""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors

from invaria.contracts.evaluation import EvaluationResult
from invaria.contracts.observation import Observation
from invaria.corpus_loader import Corpus, load_corpus
from invaria.persistence.migrate import migrate
from invaria.persistence.store import (
    ImmutableConflict,
    LateRecord,
    OutboxEvent,
    PgStore,
    Publication,
)
from invaria.persistence.worker import Clocks, drain, handle_event, prepare, reevaluate

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")


def _dsn_for(database: str) -> str:
    assert ADMIN_DSN is not None
    base, _, _ = ADMIN_DSN.rpartition("/")
    return f"{base}/{database}"


@pytest.fixture
def database() -> Iterator[str]:
    """A fresh, migrated database per test; dropped afterwards."""
    if not ADMIN_DSN:
        pytest.skip("set INVARIA_TEST_DATABASE_URL to an admin PostgreSQL URL to run DB tests")
    name = f"invaria_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    try:
        with psycopg.connect(_dsn_for(name)) as conn:
            migrate(conn)
        yield _dsn_for(name)
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def connect(database: str) -> Iterator[Any]:
    opened: list[psycopg.Connection[Any]] = []

    def factory() -> PgStore:
        conn = psycopg.connect(database)
        opened.append(conn)
        return PgStore(conn)

    yield factory
    for conn in opened:
        conn.close()


CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"
TENANT = "tenant-synthetic-demo"
OP = "SUB-0001"
MAIN_SCENARIOS = ("K1", "K2", "K3")


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


def clocks(corpus: Corpus, scenario_id: str) -> Clocks:
    s = corpus.scenarios[scenario_id].snapshot
    return Clocks(s.valid_at, s.known_at, s.evaluation_clock)


def feed(store: PgStore, corpus: Corpus, upto: datetime, timeline: str = "main") -> None:
    """Deliver everything recorded up to ``upto``; earlier items are idempotent redeliveries."""
    coverage_ids = {c for s in MAIN_SCENARIOS for c in corpus.scenarios[s].snapshot.coverage_ids}
    store.append_identity_links(
        TENANT, [k for k in corpus.identity_links.values() if k.recorded_at <= upto]
    )
    store.append_coverage(
        c
        for c in corpus.coverage.values()
        if c.coverage_id in coverage_ids and c.recorded_at <= upto
    )
    store.append_observations(
        o for o in corpus.journals[timeline].values() if o.recorded_at <= upto
    )


def start(store: PgStore, corpus: Corpus) -> None:
    store.put_profile(corpus.profile)
    assert store.register_scope(TENANT, OP, corpus.profile.profile_ref).epoch == 1


def variant(o: Observation, **changes: Any) -> Observation:
    document = o.model_dump(mode="json")
    for path, value in changes.items():
        target = document
        *parents, leaf = path.split("__")
        for key in parents:
            target = target[key]
        target[leaf] = value
    return Observation.model_validate_json(json.dumps(document))


def statuses(result: EvaluationResult) -> list[tuple[str, str, str]]:
    return [(c.control_id, c.status, c.reason_code) for c in result.controls]


def expected(corpus: Corpus, scenario_id: str) -> list[tuple[str, str, str]]:
    return [
        (c.control_id, c.status, c.reason_code)
        for c in corpus.scenarios[scenario_id].expected.controls
    ]


# ----------------------------------------------------- lifecycle: C18 and correction


def test_absence_then_evidence_then_correction(connect: Any, corpus: Corpus) -> None:
    store: PgStore = connect()
    start(store, corpus)

    feed(store, corpus, clocks(corpus, "K1").known_at)
    k1, pub1 = reevaluate(store, TENANT, OP, clocks(corpus, "K1"))
    assert pub1.outcome == "PUBLISHED"
    assert k1.evaluation.result.result == "UNKNOWN"
    assert statuses(k1.evaluation.result) == expected(corpus, "K1")  # cash and TA absent
    e1 = k1.evaluation.result.evaluation_id
    assert store.currency(TENANT, e1) == "current"

    # C18: new observations match the absence predicates of K1 -> invalidated.
    feed(store, corpus, clocks(corpus, "K2").known_at)
    assert store.currency(TENANT, e1) == "stale"
    last_epoch, cause, detail = store.epochs(TENANT, OP)[-1]
    assert cause == "evidence_appended" and "obs-R1" in detail["observation_ids"]
    k2, pub2 = reevaluate(store, TENANT, OP, clocks(corpus, "K2"))
    assert (pub2.outcome, pub2.epoch) == ("PUBLISHED", last_epoch)
    assert k2.evaluation.result.result == "MATCH"
    e2 = k2.evaluation.result.evaluation_id
    assert (store.currency(TENANT, e1), store.currency(TENANT, e2)) == ("superseded", "current")

    # Correction B2 supersedes B1: MATCH becomes stale, then BREAK.
    feed(store, corpus, clocks(corpus, "K3").known_at)
    assert store.currency(TENANT, e2) == "stale"
    k3, _ = reevaluate(store, TENANT, OP, clocks(corpus, "K3"))
    assert k3.evaluation.result.result == "BREAK"
    assert statuses(k3.evaluation.result) == expected(corpus, "K3")
    view = store.current_evaluation(TENANT, OP)
    assert view is not None and view.evaluation_id == k3.evaluation.result.evaluation_id

    # History preserved: every earlier conclusion is still stored, unchanged.
    assert store.load_evaluation(TENANT, e1) == k1.evaluation.result
    assert store.load_evaluation(TENANT, e2) == k2.evaluation.result
    assert store.currency(TENANT, e2) == "superseded"


def test_unrelated_evidence_opens_no_epoch(connect: Any, corpus: Corpus) -> None:
    store: PgStore = connect()
    start(store, corpus)
    feed(store, corpus, clocks(corpus, "K2").known_at)
    k2, _ = reevaluate(store, TENANT, OP, clocks(corpus, "K2"))
    epoch = store.scope_head(TENANT, OP).epoch
    b1 = corpus.journals["main"]["obs-B1"]
    later = "2026-10-03T00:00:00Z"
    unrelated = [
        variant(
            b1,
            observation_id="obs-other-op",
            operation_ref="SUB-0002",
            source__record_key="SUB-0002",
            recorded_at=later,
        ),
        variant(
            b1,
            observation_id="obs-other-instrument",
            instrument_id="syn:fund:OTHER:class-a",
            source__record_key="OTHER-1",
            recorded_at=later,
        ),
        variant(
            b1,
            observation_id="obs-not-authoritative",
            source__source_id="oms-synthetic",
            source__record_key="OMS-CASH-1",
            recorded_at=later,
        ),
    ]
    assert store.append_observations(unrelated) == 3
    assert store.append_observations([b1]) == 0  # identical redelivery
    assert store.scope_head(TENANT, OP).epoch == epoch
    assert store.currency(TENANT, k2.evaluation.result.evaluation_id) == "current"

    moved = variant(
        b1,
        observation_id="obs-B1-moved",
        operation_ref="SUB-0002",
        source__revision=2,
        supersedes="obs-B1",
        recorded_at=later,
    )
    store.append_observations([moved])  # takes cash away from SUB-0001
    assert store.scope_head(TENANT, OP).epoch == epoch + 1


# -------------------------------------------------------------- C17: obsolete epoch


def test_obsolete_worker_cannot_publish(connect: Any, corpus: Corpus) -> None:
    store: PgStore = connect()
    start(store, corpus)
    feed(store, corpus, clocks(corpus, "K2").known_at)
    slow = prepare(store, TENANT, OP, clocks(corpus, "K2"))  # epoch read, result computed
    feed(store, corpus, clocks(corpus, "K3").known_at)  # B2 arrives meanwhile
    late = store.publish(TENANT, slow.evaluation.result.evaluation_id, slow.epoch)
    assert late.outcome == "NOT_CURRENT" and "not current" in late.detail
    assert store.current_evaluation(TENANT, OP) is None
    slow_id = slow.evaluation.result.evaluation_id
    assert store.publish_attempts(TENANT, slow_id) == [(slow.epoch, "NOT_CURRENT", late.detail)]
    assert store.currency(TENANT, slow_id) == "superseded"
    assert store.load_evaluation(TENANT, slow_id) == slow.evaluation.result  # attempt kept

    fresh, pub = reevaluate(store, TENANT, OP, clocks(corpus, "K3"))
    assert pub.outcome == "PUBLISHED" and fresh.evaluation.result.result == "BREAK"


def test_snapshot_older_than_the_epoch_evidence_is_refused(connect: Any, corpus: Corpus) -> None:
    store: PgStore = connect()
    start(store, corpus)
    feed(store, corpus, clocks(corpus, "K2").known_at)
    stale_view, pub = reevaluate(store, TENANT, OP, clocks(corpus, "K1"))  # known_at too early
    assert pub.outcome == "NOT_CURRENT" and "predates" in pub.detail
    assert stale_view.evaluation.result.result == "UNKNOWN"
    assert store.current_evaluation(TENANT, OP) is None


# ----------------------------------------------------- C19: rules / engine change


def test_rule_change_creates_a_new_evaluation(connect: Any, corpus: Corpus) -> None:
    store: PgStore = connect()
    start(store, corpus)
    feed(store, corpus, clocks(corpus, "K2").known_at)
    first, _ = reevaluate(store, TENANT, OP, clocks(corpus, "K2"))
    e1 = first.evaluation.result.evaluation_id

    v2 = corpus.profile.model_copy(
        update={
            "profile_ref": "fund-subscription-synthetic@1.1.0",
            "rules_ref": "subscription-synthetic-rules@1.1.0",
        }
    )
    store.put_profile(v2)
    head = store.register_scope(TENANT, OP, v2.profile_ref)
    assert store.epochs(TENANT, OP)[-1][1] == "profile_changed"
    assert store.currency(TENANT, e1) == "stale"
    second, pub = reevaluate(store, TENANT, OP, clocks(corpus, "K2"))
    assert pub.outcome == "PUBLISHED" and pub.epoch == head.epoch
    assert second.evaluation.result.evaluation_id != e1
    assert second.evaluation.result.versions.profile_ref == v2.profile_ref
    assert store.currency(TENANT, e1) == "superseded"
    assert store.load_evaluation(TENANT, e1).versions.profile_ref == corpus.profile.profile_ref

    store.invalidate(TENANT, None, "engine upgrade to invaria-engine@0.2.0")
    third, pub3 = reevaluate(
        store, TENANT, OP, clocks(corpus, "K2"), engine_ref="invaria-engine@0.2.0"
    )
    assert pub3.outcome == "PUBLISHED"
    assert third.evaluation.result.versions.engine_ref == "invaria-engine@0.2.0"
    assert store.currency(TENANT, second.evaluation.result.evaluation_id) == "superseded"


# ------------------------------------------------------- outbox and duplicates


def test_outbox_redelivery_is_idempotent(connect: Any, corpus: Corpus) -> None:
    store: PgStore = connect()
    start(store, corpus)
    feed(store, corpus, clocks(corpus, "K2").known_at)
    k2 = clocks(corpus, "K2")

    handled = drain(store, "worker-a", lambda _event: k2)
    published = [p for _, p in handled if p is not None]
    assert [p.outcome for p in published] == ["PUBLISHED"]  # older epochs skipped
    assert store.outbox_pending("worker-a") == [
        e for e in store.outbox_pending("worker-a") if e.event_type == "evaluation.published"
    ]
    drain(store, "worker-a", lambda _event: k2)
    assert store.outbox_pending("worker-a") == []

    feed(store, corpus, clocks(corpus, "K3").known_at)
    *obsolete, event = store.outbox_pending("worker-a")  # coverage, then B2: two epochs
    k3 = clocks(corpus, "K3")
    assert obsolete and all(handle_event(store, e, lambda _e: k3) is None for e in obsolete)
    for e in obsolete:
        store.outbox_ack("worker-a", e.event_seq)
    first = handle_event(store, event, lambda _e: k3)
    # crash before the acknowledgement: the event is delivered again
    assert event in store.outbox_pending("worker-a")
    again = handle_event(store, event, lambda _e: k3)
    assert first is not None and again is not None
    assert (first.outcome, again.outcome) == ("PUBLISHED", "ALREADY_PUBLISHED")
    assert first.evaluation_id == again.evaluation_id
    assert store.outbox_ack("worker-a", event.event_seq) is True
    assert store.outbox_ack("worker-a", event.event_seq) is False
    # another consumer still sees every event: acknowledgements are per consumer
    assert len(store.outbox_pending("audit")) == len(outbox_rows(store))


def outbox_rows(store: PgStore) -> list[OutboxEvent]:
    return store.outbox_pending("nobody-acks", limit=10_000)


def test_failed_batch_leaves_no_epoch_and_no_event(connect: Any, corpus: Corpus) -> None:
    store: PgStore = connect()
    start(store, corpus)
    feed(store, corpus, clocks(corpus, "K2").known_at)
    epoch, events = store.scope_head(TENANT, OP).epoch, len(outbox_rows(store))
    b1 = corpus.journals["main"]["obs-B1"]
    fresh = variant(
        b1,
        observation_id="obs-new-cash",
        source__record_key="SUB-0001-extra",
        recorded_at="2026-10-03T00:00:00Z",
    )
    conflicting = variant(b1, payload__amount__atoms="1")
    with pytest.raises(ImmutableConflict):
        store.append_observations([fresh, conflicting])
    assert store.scope_head(TENANT, OP).epoch == epoch
    assert len(outbox_rows(store)) == events
    assert "obs-new-cash" not in {o.observation_id for o in store.journal(TENANT)}


# --------------------------------------------------------------- concurrency


def _in_thread(fn: Any) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []
    thread = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    thread.start()
    return thread, out


def test_open_invalidation_blocks_publication(connect: Any, corpus: Corpus) -> None:
    writer, worker = connect(), connect()
    start(writer, corpus)
    feed(writer, corpus, clocks(corpus, "K2").known_at)
    prepared = prepare(worker, TENANT, OP, clocks(corpus, "K2"))
    b2 = corpus.journals["main"]["obs-B2"]
    with writer.conn.transaction():
        writer.append_observations([b2])  # epoch opened, not yet committed
        thread, out = _in_thread(
            lambda: worker.publish(TENANT, prepared.evaluation.result.evaluation_id, prepared.epoch)
        )
        thread.join(timeout=0.5)
        assert thread.is_alive()  # waits for the invalidation to commit or roll back
    thread.join(timeout=10)
    (publication,) = out
    assert isinstance(publication, Publication) and publication.outcome == "NOT_CURRENT"


def test_two_workers_on_the_same_epoch_publish_once(connect: Any, corpus: Corpus) -> None:
    a, b = connect(), connect()
    start(a, corpus)
    feed(a, corpus, clocks(corpus, "K2").known_at)
    k2 = clocks(corpus, "K2")
    later = Clocks(k2.valid_at, k2.known_at, k2.evaluation_clock + timedelta(minutes=1))
    pa, pb = prepare(a, TENANT, OP, k2), prepare(b, TENANT, OP, later)
    assert pa.epoch == pb.epoch
    assert pa.evaluation.result.evaluation_id != pb.evaluation.result.evaluation_id
    barrier = threading.Barrier(2)

    def publish(store: PgStore, prepared: Any) -> Publication:
        barrier.wait()
        return store.publish(TENANT, prepared.evaluation.result.evaluation_id, prepared.epoch)

    ta, oa = _in_thread(lambda: publish(a, pa))
    tb, ob = _in_thread(lambda: publish(b, pb))
    ta.join(timeout=10)
    tb.join(timeout=10)
    outcomes = sorted([oa[0].outcome, ob[0].outcome])
    assert outcomes == ["NOT_CURRENT", "PUBLISHED"]


def test_knowledge_cut_is_consistent_and_closed(connect: Any, corpus: Corpus) -> None:
    writer, builder = connect(), connect()
    writer.put_profile(corpus.profile)
    feed(writer, corpus, clocks(corpus, "K2").known_at)
    k2 = corpus.scenarios["K2"].snapshot
    r1 = corpus.journals["main"]["obs-R1"]
    just_before = variant(
        r1,
        observation_id="obs-R1-redelivered-late",
        source__record_key="SUB-0001-late",
        recorded_at="2026-10-01T17:59:00Z",
    )

    def build(snapshot_id: str) -> Any:
        return builder.build_snapshot(
            snapshot_id=snapshot_id,
            tenant_id=TENANT,
            operation_ref=OP,
            profile_ref=k2.profile_ref,
            valid_at=k2.valid_at,
            known_at=k2.known_at,
            evaluation_clock=k2.evaluation_clock,
        )

    with writer.conn.transaction():
        writer.append_observations([just_before])  # in flight while the cut is closed
        thread, out = _in_thread(lambda: build("cut-1"))
        thread.join(timeout=0.5)
        assert thread.is_alive()  # the build waits instead of missing a commit in flight
    thread.join(timeout=10)
    assert "obs-R1-redelivered-late" in out[0].observation_ids

    too_late = variant(
        just_before, observation_id="obs-too-late", recorded_at="2026-10-01T17:58:00Z"
    )
    with pytest.raises(LateRecord):
        writer.append_observations([too_late])
    assert build("cut-2").observation_ids == out[0].observation_ids  # stable rebuild
    assert writer.append_observations([just_before]) == 0  # redelivery is still fine


# ----------------------------------------------------------------- append-only


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE invaria.scope_epochs SET epoch = epoch + 1",
        "DELETE FROM invaria.scope_epochs",
        "DELETE FROM invaria.publications",
        "UPDATE invaria.publish_attempts SET outcome = 'PUBLISHED'",
        "DELETE FROM invaria.outbox",
        "TRUNCATE invaria.outbox_acks",
        "DELETE FROM invaria.knowledge_cuts",
    ],
)
def test_revision_history_is_append_only(connect: Any, corpus: Corpus, statement: str) -> None:
    store: PgStore = connect()
    start(store, corpus)
    with pytest.raises(errors.InsufficientPrivilege):
        store.conn.execute(statement)
    store.conn.rollback()
    assert store.scope_head(TENANT, OP).epoch == 1


def test_public_reads_leave_no_transaction_open(connect: Any, corpus: Corpus) -> None:
    """A read that left a transaction open would keep the next advisory lock forever."""
    store: PgStore = connect()
    start(store, corpus)
    feed(store, corpus, clocks(corpus, "K2").known_at)
    prepared, _ = reevaluate(store, TENANT, OP, clocks(corpus, "K2"))
    result = prepared.evaluation.result
    reads: list[Callable[[], object]] = [
        lambda: store.load_profile(corpus.profile.profile_ref),
        lambda: store.load_snapshot(TENANT, result.snapshot_id),
        lambda: store.load_inputs(TENANT, result.snapshot_id),
        lambda: store.load_evaluation(TENANT, result.evaluation_id),
        lambda: store.currency(TENANT, result.evaluation_id),
        lambda: store.scope_head(TENANT, OP),
        lambda: store.outbox_pending("w"),
    ]
    for read in reads:
        read()
        assert store.conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    with pytest.raises(KeyError):
        store.load_evaluation(TENANT, "eval-missing")
    assert store.conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
