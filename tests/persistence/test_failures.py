"""Injected failures in the worker and the outbox, against a real PostgreSQL.

Transaction boundaries (persistence/worker.py): (1) build_snapshot closes the snapshot and
the knowledge cut; (2) save_evaluation persists the evaluation; (3) publish records the
attempt and, if current, the publication and its outbox event, atomically. Persisting an
evaluation never makes it current; only (3) does.

Stable identities make every retry idempotent: snapshot_id = f(operation, epoch, clocks),
evaluation_id = f(snapshot, profile, engine) (the logical evaluation key: one row per
snapshot and engine), publication = (tenant, operation, epoch), outbox dedup_key, and
ack = (consumer, event). Delivery is at least once; the consumer's effect is idempotent.

After every failure the invariants are checked before and after resuming: history
preserved (append-only), events recoverable, and no obsolete evaluation shown as current.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from invaria.contracts.observation import Observation
from invaria.corpus_loader import Corpus, load_corpus
from invaria.persistence.backup import HistorySnapshot, check_invariants
from invaria.persistence.dsn import with_database
from invaria.persistence.migrate import migrate
from invaria.persistence.provision import prepare_for_migrator
from invaria.persistence.store import PgStore
from invaria.persistence.worker import Clocks, handle_event, prepare, reevaluate

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")
CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"
TENANT = "tenant-synthetic-demo"
OP = "SUB-0001"


def _dsn_for(database: str) -> str:
    assert ADMIN_DSN is not None
    return with_database(ADMIN_DSN, database)


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


def clocks(corpus: Corpus, scenario: str) -> Clocks:
    s = corpus.scenarios[scenario].snapshot
    return Clocks(s.valid_at, s.known_at, s.evaluation_clock)


def feed(store: PgStore, corpus: Corpus, upto: datetime) -> None:
    certs = {c for s in ("K1", "K2", "K3") for c in corpus.scenarios[s].snapshot.coverage_ids}
    store.append_identity_links(
        TENANT, [k for k in corpus.identity_links.values() if k.recorded_at <= upto]
    )
    store.append_coverage(
        c for c in corpus.coverage.values() if c.coverage_id in certs and c.recorded_at <= upto
    )
    store.append_observations(o for o in corpus.journals["main"].values() if o.recorded_at <= upto)


class World:
    """K2 published; the K3 correction is recorded, so a newer epoch awaits evaluation."""

    def __init__(self, dsn: str, corpus: Corpus) -> None:
        self.dsn = dsn
        self.corpus = corpus
        self.connections: list[psycopg.Connection[Any]] = []
        store = self.store()
        store.put_profile(corpus.profile)
        store.register_scope(TENANT, OP, corpus.profile.profile_ref)
        for scenario in ("K1", "K2"):
            feed(store, corpus, clocks(corpus, scenario).known_at)
            reevaluate(store, TENANT, OP, clocks(corpus, scenario))
        feed(store, corpus, clocks(corpus, "K3").known_at)
        self.k3 = clocks(corpus, "K3")

    def store(self) -> PgStore:
        conn = psycopg.connect(self.dsn)
        self.connections.append(conn)
        return PgStore(conn)

    def admin(self) -> psycopg.Connection[Any]:
        conn = psycopg.connect(self.dsn, autocommit=True)
        self.connections.append(conn)
        return conn

    def counts(self) -> dict[str, int]:
        conn = self.admin()
        tables = ("snapshots", "evaluations", "publications", "publish_attempts", "outbox")
        counts = {}
        for table in tables:
            row = conn.execute(f"SELECT count(*) FROM invaria.{table}").fetchone()
            assert row is not None
            counts[table] = int(row[0])
        return counts

    def check(self, before: HistorySnapshot | None = None) -> HistorySnapshot:
        """Invariants: cross-table consistency, append-only history, honest currency."""
        conn = self.admin()
        assert check_invariants(conn) == []
        now = HistorySnapshot.take(conn)
        if before is not None:
            assert now.lost_since(before) == {}
        view = self.store().current_evaluation(TENANT, OP)
        assert view is not None
        expected = "current" if view.published_epoch == view.scope_epoch else "stale"
        assert view.currency == expected
        return now


@pytest.fixture
def world(corpus: Corpus) -> Iterator[World]:
    if not ADMIN_DSN:
        pytest.skip("set INVARIA_TEST_DATABASE_URL to an admin PostgreSQL URL to run DB tests")
    name = f"invaria_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    try:
        with psycopg.connect(_dsn_for(name)) as conn:
            prepare_for_migrator(conn)
            migrate(conn)
        w = World(_dsn_for(name), corpus)
        yield w
        for conn in w.connections:
            conn.close()
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def fail_once(store: PgStore, method: str, *, after: bool = False) -> None:
    """Make ``store.method`` fail once: before running, or after its commit (lost reply)."""
    original: Callable[..., Any] = getattr(store, method)
    state = {"armed": True}

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not state["armed"]:
            return original(*args, **kwargs)
        state["armed"] = False
        if after:
            original(*args, **kwargs)  # committed by the server ...
            raise psycopg.OperationalError("connection lost after commit")  # ... reply lost
        raise RuntimeError(f"crash before {method}")

    setattr(store, method, wrapper)


def stale_before_resume(world: World) -> None:
    view = world.store().current_evaluation(TENANT, OP)
    assert view is not None and view.currency == "stale"  # K2 is never shown as current


# --------------------------------------------------------------------- cases


def test_crash_after_closing_the_snapshot_before_saving(world: World) -> None:
    before = world.check()
    store = world.store()
    fail_once(store, "save_evaluation")
    with pytest.raises(RuntimeError):
        reevaluate(store, TENANT, OP, world.k3)
    after_crash = world.check(before)
    stale_before_resume(world)
    counts = world.counts()
    _, publication = reevaluate(world.store(), TENANT, OP, world.k3)
    assert publication.outcome == "PUBLISHED"
    resumed = world.counts()
    assert resumed["snapshots"] == counts["snapshots"]  # the closed snapshot is reused
    assert resumed["evaluations"] == counts["evaluations"] + 1
    world.check(after_crash)


def test_crash_after_saving_before_publishing(world: World) -> None:
    before = world.check()
    store = world.store()
    fail_once(store, "publish")
    with pytest.raises(RuntimeError):
        reevaluate(store, TENANT, OP, world.k3)
    after_crash = world.check(before)
    stale_before_resume(world)  # persisted is not current
    counts = world.counts()
    _, publication = reevaluate(world.store(), TENANT, OP, world.k3)
    assert publication.outcome == "PUBLISHED"
    resumed = world.counts()
    assert resumed["evaluations"] == counts["evaluations"]  # same logical evaluation
    assert resumed["publications"] == counts["publications"] + 1
    world.check(after_crash)


def test_publication_refused_by_a_newer_epoch(world: World) -> None:
    before = world.check()
    store = world.store()
    prepared = prepare(store, TENANT, OP, world.k3)
    b2 = world.corpus.journals["main"]["obs-B2"].model_dump(mode="json")
    b2.update(observation_id="obs-B3", recorded_at="2026-10-03T00:00:00Z", supersedes="obs-B2")
    b2["source"]["revision"] = 3
    world.store().append_observations([Observation.model_validate_json(json.dumps(b2))])
    refused = store.publish(TENANT, prepared.evaluation.result.evaluation_id, prepared.epoch)
    assert refused.outcome == "NOT_CURRENT"
    after_refusal = world.check(before)
    stale_before_resume(world)
    newer = Clocks(world.k3.valid_at, *(datetime.fromisoformat("2026-10-03T00:00:00Z"),) * 2)
    _, publication = reevaluate(world.store(), TENANT, OP, newer)
    assert publication.outcome == "PUBLISHED"
    final = world.check(after_refusal)
    attempts = {r for r in final.rows["publish_attempts"] if "NOT_CURRENT" in r}
    assert attempts  # the refused attempt is kept, and its evaluation stays stored


def test_consumer_crash_before_ack_redelivers_without_duplicating(world: World) -> None:
    before = world.check()
    store = world.store()
    pending = [e for e in store.outbox_pending("worker") if e.event_type == "scope.invalidated"]
    event = pending[-1]  # the K3 invalidation
    publication = handle_event(store, event, lambda _: world.k3)  # handled ...
    assert publication is not None and publication.outcome == "PUBLISHED"
    # ... and the consumer crashes before acknowledging.
    after_crash = world.check(before)
    counts = world.counts()
    redelivered = [e for e in store.outbox_pending("worker") if e.event_seq == event.event_seq]
    assert redelivered  # at least once: the event is delivered again
    again = handle_event(store, redelivered[0], lambda _: world.k3)
    assert again is not None and again.outcome == "ALREADY_PUBLISHED"
    assert world.counts() == counts  # no duplicated effect
    assert store.outbox_ack("worker", event.event_seq)
    assert not store.outbox_ack("worker", event.event_seq)  # a repeated ack is a no-op
    world.check(after_crash)


def test_connection_lost_inside_the_publication_transaction(world: World) -> None:
    before = world.check()
    store = world.store()
    prepared = prepare(store, TENANT, OP, world.k3)
    killer = world.admin()
    # The server's own pid: behind a proxy (Neon) the protocol's backend_pid is another
    # number, and terminating it would kill nothing.
    assert store.conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    row = store.conn.execute("SELECT pg_backend_pid()").fetchone()
    store.conn.commit()
    assert row is not None
    pid = row[0]
    original = store.conn.execute
    terminated: list[bool] = []

    def execute(query: Any, *args: Any, **kwargs: Any) -> Any:
        if str(query).startswith("INSERT INTO invaria.publications"):
            done = killer.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()
            terminated.append(bool(done and done[0]))
        return original(query, *args, **kwargs)

    store.conn.execute = execute  # type: ignore[method-assign]
    counts = world.counts()
    with pytest.raises(psycopg.OperationalError):
        store.publish(TENANT, prepared.evaluation.result.evaluation_id, prepared.epoch)
    assert terminated == [True]  # the fault really happened, inside the publication
    assert world.counts() == counts  # rolled back: no attempt, publication or event
    after_loss = world.check(before)
    stale_before_resume(world)
    retry = world.store().publish(TENANT, prepared.evaluation.result.evaluation_id, prepared.epoch)
    assert retry.outcome == "PUBLISHED"
    world.check(after_loss)


@pytest.mark.parametrize("method", ["save_evaluation", "publish"])
def test_commit_confirmed_but_reply_lost(world: World, method: str) -> None:
    """The server commits, the client sees an error; the retry finds the same identity."""
    before = world.check()
    store = world.store()
    fail_once(store, method, after=True)
    with pytest.raises(psycopg.OperationalError):
        reevaluate(store, TENANT, OP, world.k3)
    after_loss = world.check(before)
    counts = world.counts()
    prepared, publication = reevaluate(world.store(), TENANT, OP, world.k3)
    expected = "ALREADY_PUBLISHED" if method == "publish" else "PUBLISHED"
    assert publication.outcome == expected
    resumed = world.counts()
    assert resumed["evaluations"] == counts["evaluations"]  # the stored row is the same one
    if method == "publish":
        assert resumed == counts  # no second publication, attempt or event
    world.check(after_loss)
    stored = world.store().load_evaluation(TENANT, prepared.evaluation.result.evaluation_id)
    assert stored == prepared.evaluation.result


def test_ack_committed_but_reply_lost(world: World) -> None:
    store = world.store()
    event = store.outbox_pending("worker")[0]
    fail_once(store, "outbox_ack", after=True)
    with pytest.raises(psycopg.OperationalError):
        store.outbox_ack("worker", event.event_seq)
    assert not store.outbox_ack("worker", event.event_seq)  # already recorded: no duplicate
    rows = (
        world.admin()
        .execute(
            "SELECT count(*) FROM invaria.outbox_acks "
            "WHERE consumer_id = 'worker' AND event_seq = %s",
            (event.event_seq,),
        )
        .fetchone()
    )
    assert rows is not None and rows[0] == 1
    assert event.event_seq not in {e.event_seq for e in store.outbox_pending("worker")}
