"""Domain events against a real PostgreSQL: derivation, order, faults, history.

Expectations were written before the code.
The worker evaluates and publishes as in production; the deriver runs as the application
role. Two tenants share one disposable database.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors

from invaria.contracts.events import canonical_bytes, event_id, parse_event
from invaria.corpus_loader import Corpus, load_corpus
from invaria.domain_events import Budget, Limits
from invaria.persistence import events as ev
from invaria.persistence.dsn import with_database
from invaria.persistence.migrate import migrate
from invaria.persistence.provision import prepare_for_migrator
from invaria.persistence.store import PgStore
from invaria.persistence.worker import Clocks, drain, reevaluate
from invaria.query.models import AccessProfile
from invaria.query.service import QueryService

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")
CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"
TENANT = "tenant-synthetic-demo"
OTHER = "tenant-other"
OP = "SUB-0001"
FORBIDDEN_TEXT = (TENANT, OTHER, "analyst", "obs-", "delta", "principal", '"currency"', "/home")


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


@pytest.fixture
def dsn() -> Iterator[str]:
    if not ADMIN_DSN:
        pytest.skip("set INVARIA_TEST_DATABASE_URL to an admin PostgreSQL URL to run DB tests")
    name = f"invaria_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    try:
        with psycopg.connect(with_database(ADMIN_DSN, name)) as conn:
            prepare_for_migrator(conn)
            migrate(conn)
        yield with_database(ADMIN_DSN, name)
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


# Before any evidence of the corpus is recorded: publishing here closes an earlier cut, so
# the corpus evidence can still be appended afterwards (knowledge cuts).
BEFORE = datetime(2026, 8, 31, tzinfo=UTC)


def clocks(corpus: Corpus, scenario: str) -> Clocks:
    if scenario == "EMPTY":
        return Clocks(BEFORE, BEFORE, BEFORE)
    s = corpus.scenarios[scenario].snapshot
    return Clocks(s.valid_at, s.known_at, s.evaluation_clock)


@dataclass
class World:
    dsn: str
    corpus: Corpus
    store: PgStore
    conns: list[psycopg.Connection[Any]]

    def new_store(self) -> PgStore:
        conn = psycopg.connect(self.dsn)
        self.conns.append(conn)
        return PgStore(conn)

    def feed(self, scenario: str) -> None:
        upto = clocks(self.corpus, scenario).known_at
        c = self.corpus
        cov = {x for s in ("K1", "K2", "K3") for x in c.scenarios[s].snapshot.coverage_ids}
        self.store.append_identity_links(
            TENANT, [k for k in c.identity_links.values() if k.recorded_at <= upto]
        )
        self.store.append_coverage(
            x for x in c.coverage.values() if x.coverage_id in cov and x.recorded_at <= upto
        )
        self.store.append_observations(
            o for o in c.journals["main"].values() if o.recorded_at <= upto
        )

    def publish(self, scenario: str, tenant: str = TENANT, op: str = OP) -> None:
        _, publication = reevaluate(self.store, tenant, op, clocks(self.corpus, scenario))
        assert publication.outcome == "PUBLISHED"

    def derive_all(self, **kwargs: Any) -> ev.RunResult:
        total = ev.RunResult()
        for _ in range(100):
            result = ev.derive(self.store, **kwargs)
            total.derived += result.derived
            total.not_public += result.not_public
            total.already_derived += result.already_derived
            total.errors += result.errors
            if result.pending == 0 or result.errors:
                total.pending = result.pending
                return total
        raise AssertionError("derivation did not finish")

    def events(self, tenant: str = TENANT) -> list[tuple[int, Any]]:
        return [(seq, parse_event(raw)) for seq, _, _, raw in ev.events_of(self.store, tenant)]

    def scalar(self, sql: str, *params: Any) -> Any:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def counter(self, tenant: str = TENANT) -> int | None:
        value = self.scalar(
            "SELECT last_seq FROM invaria.domain_event_counters WHERE tenant_id = %s", tenant
        )
        return None if value is None else int(value)

    def acks(self, consumer: str) -> set[int]:
        with psycopg.connect(self.dsn) as conn:
            rows = conn.execute(
                "SELECT event_seq FROM invaria.outbox_acks WHERE consumer_id = %s", (consumer,)
            ).fetchall()
        return {int(r[0]) for r in rows}


@pytest.fixture
def w(dsn: str, corpus: Corpus) -> Iterator[World]:
    store = PgStore(psycopg.connect(dsn))
    store.put_profile(corpus.profile)
    store.register_scope(TENANT, OP, corpus.profile.profile_ref)
    world = World(dsn, corpus, store, [])
    yield world
    for conn in world.conns:
        conn.close()
    store.conn.close()


def of_type(events: list[tuple[int, Any]], kind: str) -> list[Any]:
    return [e for _, e in events if e.type == kind]


def access(*scopes: str) -> AccessProfile:
    return AccessProfile.model_validate_json(
        json.dumps(
            {
                "schema_version": "1.0",
                "principal_id": "analyst-1",
                "tenant_id": TENANT,
                "scopes": list(scopes),
                "max_items": 50,
                # Explicit full disclosure policies: no implicit policy exists.
                "disclosure_policies": [
                    "full-operations@1",
                    "full-evidence@1",
                    "full-report-index@1",
                    "full-report-summary@1",
                ],
            }
        )
    )


def register_report(w: World) -> Any:
    service = QueryService(w.new_store(), access("operations:read", "reports:register"))
    return service.register_integrity_report("2026-10-20T00:00:00Z")


# ------------------------------------------------------------------ contract and types


def test_the_three_types_their_bytes_ids_and_numbering(w: World) -> None:
    w.publish("EMPTY")  # epoch 1, no evidence yet: UNKNOWN
    w.feed("K1")
    w.publish("K1")
    report, registration = register_report(w)
    result = w.derive_all()
    assert result.errors == [] and result.not_public == 1  # the registration epoch
    rows = ev.events_of(w.store, TENANT)
    assert [r[0] for r in rows] == list(range(1, len(rows) + 1))  # no gaps
    assert w.counter() == len(rows) == result.derived
    kinds = {parse_event(r[3]).type for r in rows}
    assert kinds == {
        "operation.scope_reopened",
        "operation.evaluation_published",
        "integrity_report.registered",
    }
    with psycopg.connect(w.dsn) as conn:
        sources: dict[str, str] = dict(
            conn.execute(
                "SELECT d.event_id, o.dedup_key FROM invaria.domain_events d "
                "JOIN invaria.outbox o ON o.event_seq = d.source_event_seq"
            ).fetchall()
        )
        pending = conn.execute(
            "SELECT count(*) FROM invaria.outbox o WHERE NOT EXISTS (SELECT 1 FROM "
            "invaria.outbox_acks a WHERE a.consumer_id = 'domain-events' "
            "AND a.event_seq = o.event_seq)"
        ).fetchone()
    assert pending == (0,)
    for _seq, derived_at, sha, raw in rows:
        event = parse_event(raw)
        assert canonical_bytes(event) == raw  # stored bytes are the canonical form
        assert sha == hashlib.sha256(raw).hexdigest()
        assert event.event_id == event_id(event.type, sources[event.event_id])
        assert event.provenance.source_payload == "pinned"
        assert event.provenance.fact_recorded_at <= derived_at
        text = raw.decode()
        assert not [t for t in FORBIDDEN_TEXT if t in text], text
    (registered,) = of_type(w.events(), "integrity_report.registered")
    assert registered.subject.report_id == registration.report_id
    assert registered.data.report_sha256 == report.report_sha256
    assert registered.provenance.fact_recorded_at == registration.registered_at


def test_a_reopening_is_an_event_before_any_new_evaluation(w: World) -> None:
    w.publish("EMPTY")
    w.derive_all()
    w.feed("K1")  # opens one or more epochs; nobody re-evaluates
    w.derive_all()
    reopened = of_type(w.events(), "operation.scope_reopened")
    assert reopened and len(of_type(w.events(), "operation.evaluation_published")) == 1
    first, *later = sorted(reopened, key=lambda e: e.data.epoch)
    assert first.data.epoch == 2 and first.data.cause == "evidence_appended"
    assert first.categories == ["currency_lost"]
    assert first.data.prior_publication is not None
    assert (
        first.data.prior_publication.epoch,
        first.data.prior_publication.current_until_reopening,
    ) == (1, True)
    assert first.data.evidence_counts is not None and first.data.knowledge_floor is not None
    for e in later:  # epoch 1 stays the latest publication, already stale
        assert e.data.prior_publication.epoch == 1 and e.categories == []


def test_first_publication_then_blocks_then_result_changes(w: World) -> None:
    w.publish("EMPTY")  # no evidence: every control UNKNOWN
    for scenario in ("K1", "K2", "K3"):
        w.feed(scenario)
        w.publish(scenario)
    assert w.derive_all().errors == []
    published = sorted(
        of_type(w.events(), "operation.evaluation_published"), key=lambda e: e.data.epoch
    )
    first, k1, k2, k3 = published
    assert first.categories == ["first_publication"] and first.data.previous_publication is None
    assert k1.data.change.result.status == "unchanged"  # UNKNOWN stays UNKNOWN
    assert k1.data.change.blocks.status == "changed" and "blocks_changed" in k1.categories
    assert k1.data.change.blocks.removed  # fewer requirements are blocked
    assert (k2.data.change.result.previous, k2.data.change.result.current) == ("UNKNOWN", "MATCH")
    assert (k3.data.change.result.previous, k3.data.change.result.current) == ("MATCH", "BREAK")
    for before, after in pairwise(published):
        assert after.data.previous_publication.epoch == before.data.epoch  # explicit causality
        assert after.data.previous_publication.evaluation_id == before.data.evaluation_id


# --------------------------------------------------------- comparison not available


def test_limits_and_known_limitations_still_produce_the_event(w: World) -> None:
    w.publish("EMPTY")
    w.feed("K1")
    w.publish("K1")
    assert w.derive_all(limits=Limits(max_replays_per_run=0)).errors == []
    latest = of_type(w.events(), "operation.evaluation_published")[-1]
    nc = latest.data.change.blocks.not_comparable
    assert nc is not None and (nc.reason, nc.detail) == ("limit_reached", "max_replays_per_run=0")
    assert "comparison_incomplete" in latest.categories
    assert latest.data.change.result.status == "unchanged"  # the rest is still compared


def test_a_spent_replay_budget_pauses_instead_of_degrading_the_event(w: World) -> None:
    w.publish("EMPTY")
    w.feed("K1")
    w.publish("K1")  # two replays: both sides have UNKNOWN controls
    w.store.invalidate(TENANT, [OP], "test")
    w.publish("K1")  # two more
    with pytest.raises(ValueError):
        Limits(max_replays_per_run=1)  # could never finish a comparison
    paused = ev.derive(w.store, limit=1000, limits=Limits(max_replays_per_run=2))
    assert paused.paused and paused.pending == 1 and paused.errors == []
    assert len(of_type(w.events(), "operation.evaluation_published")) == 2
    resumed = ev.derive(w.store, limit=1000)
    assert not resumed.paused and resumed.pending == 0
    latest = of_type(w.events(), "operation.evaluation_published")[-1]
    assert latest.data.change.blocks.status == "unchanged"  # compared (K1 again), not degraded
    assert latest.data.change.blocks.not_comparable is None


def test_a_transient_error_derives_nothing_and_is_retried(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w.publish("EMPTY")
    w.feed("K1")
    w.publish("K1")
    original = PgStore.load_inputs

    def lost(*_: Any) -> Any:
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(PgStore, "load_inputs", lost)
    result = ev.derive(w.store, limit=1000)
    assert [e for e in result.errors if "transient" in e]
    before = len(ev.events_of(w.store, TENANT))
    assert result.pending == 1 and w.counter() == before  # no event, no ack, no number
    monkeypatch.setattr(PgStore, "load_inputs", original)
    assert w.derive_all().errors == []
    rows = ev.events_of(w.store, TENANT)
    assert [r[0] for r in rows] == list(range(1, before + 2))
    assert of_type(w.events(), "operation.evaluation_published")[-1].data.change.blocks.status in (
        "changed",
        "unchanged",
    )


def test_a_mismatching_replay_and_a_blocked_engine(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w.publish("EMPTY")
    w.feed("K1")
    w.publish("K1")
    first = w.scalar(
        "SELECT evaluation_id FROM invaria.publications WHERE tenant_id = %s AND epoch = 1",
        TENANT,
    )
    with psycopg.connect(w.dsn, autocommit=True) as admin:  # history altered by an admin
        admin.execute(
            "UPDATE invaria.evaluations SET document = jsonb_set(document, '{controls,1,reason}', "
            "'\"edited\"') WHERE evaluation_id = %s",
            (first,),
        )
    w.derive_all()
    nc = of_type(w.events(), "operation.evaluation_published")[-1].data.change.blocks.not_comparable
    assert nc is not None and (nc.reason, nc.side) == ("reconstruction_mismatch", "previous")
    # A blocked engine is never replayed.
    w.store.invalidate(TENANT, [OP], "test")
    w.publish("K1")
    monkeypatch.setattr("invaria.domain_events.engine_status", lambda ref: "blocked")

    def forbidden(*_: Any) -> Any:
        raise AssertionError("a blocked engine was replayed")

    monkeypatch.setattr("invaria.persistence.events.replay", forbidden)
    result = ev.derive(w.store, limit=1000)
    assert result.errors == [] and result.replays == 0
    nc = of_type(w.events(), "operation.evaluation_published")[-1].data.change.blocks.not_comparable
    assert nc is not None and nc.reason == "engine_blocked"


# ------------------------------------------------------------------- reports


def test_a_repeated_registration_produces_one_event(w: World) -> None:
    w.publish("K1")
    service = QueryService(w.new_store(), access("operations:read", "reports:register"))
    report = service.integrity_report("2026-10-20T00:00:00Z")
    assert service._register(report, 10_000_000).created
    assert not service._register(report, 10_000_000).created
    assert (
        w.scalar(
            "SELECT count(*) FROM invaria.outbox WHERE event_type = 'integrity_report.registered'"
        )
        == 1
    )
    w.derive_all()
    assert len(of_type(w.events(), "integrity_report.registered")) == 1


# ------------------------------------------------------- concurrency and faults


def test_two_derivers_on_the_same_source(w: World) -> None:
    w.publish("K1")
    w.derive_all()
    w.store.invalidate(TENANT, [OP], "test")
    (pending,) = w.store.outbox_pending(ev.CONSUMER)
    stores = [w.new_store(), w.new_store()]
    planned = [ev.plan(s, ev.load_source(s, pending.event_seq), Budget(Limits())) for s in stores]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def run(i: int) -> None:
        source = ev.load_source(stores[i], pending.event_seq)
        barrier.wait()
        outcomes.append(ev.record(stores[i], source, planned[i]))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["already_derived", "derived"]
    assert (
        w.scalar(
            "SELECT count(*) FROM invaria.domain_events WHERE source_event_seq = %s",
            pending.event_seq,
        )
        == 1
    )
    assert w.counter() == len(ev.events_of(w.store, TENANT))


def test_concurrent_producers_and_derivers_in_one_and_two_tenants(w: World) -> None:
    profile = w.corpus.profile.profile_ref
    ops = {TENANT: ["SUB-0101", "SUB-0102"], OTHER: ["B-0001", "B-0002"]}
    for tenant, names in ops.items():
        for op in names:
            w.store.register_scope(tenant, op, profile)
    failures: list[BaseException] = []
    producing = threading.Event()
    producing.set()

    def produce(tenant: str, op: str) -> None:
        try:
            store = w.new_store()
            for _ in range(4):
                store.invalidate(tenant, [op], "load")
                reevaluate(store, tenant, op, clocks(w.corpus, "K3"))
        except BaseException as error:  # surfaced below
            failures.append(error)

    def register_reports() -> None:  # same tenant lock as publication (derivation order)
        try:
            service = QueryService(w.new_store(), access("operations:read", "reports:register"))
            for _ in range(3):
                service.register_integrity_report("2026-10-20T00:00:00Z")
        except BaseException as error:
            failures.append(error)

    def derive_loop() -> None:
        try:
            store = w.new_store()
            while producing.is_set():
                ev.derive(store, limit=5)
        except BaseException as error:
            failures.append(error)

    producers = [
        threading.Thread(target=produce, args=(t, op)) for t, names in ops.items() for op in names
    ] + [threading.Thread(target=register_reports)]
    derivers = [threading.Thread(target=derive_loop) for _ in range(2)]
    for t in [*producers, *derivers]:
        t.start()
    for t in producers:
        t.join()
    producing.clear()
    for t in derivers:
        t.join()
    assert failures == []
    assert w.derive_all().errors == []
    with psycopg.connect(w.dsn) as conn:
        dup = conn.execute(
            "SELECT count(*) FROM (SELECT event_seq FROM invaria.outbox_acks WHERE "
            "consumer_id = 'domain-events' GROUP BY event_seq HAVING count(*) > 1) x"
        ).fetchone()
        unacked = conn.execute(
            "SELECT count(*) FROM invaria.outbox o WHERE NOT EXISTS (SELECT 1 FROM "
            "invaria.outbox_acks a WHERE a.consumer_id = 'domain-events' "
            "AND a.event_seq = o.event_seq)"
        ).fetchone()
    assert dup == (0,) and unacked == (0,)
    for tenant in ops:
        rows = ev.events_of(w.store, tenant)
        assert [r[0] for r in rows] == list(range(1, len(rows) + 1)), tenant
        assert w.counter(tenant) == len(rows)
        outbox = w.scalar(
            "SELECT count(*) FROM invaria.outbox o LEFT JOIN invaria.scope_epochs s USING "
            "(tenant_id, operation_ref, epoch) WHERE o.tenant_id = %s AND NOT "
            "(o.event_type = 'scope.invalidated' AND s.cause = 'registered')",
            tenant,
        )
        assert len(rows) == outbox
        reports = of_type(w.events(tenant), "integrity_report.registered")
        assert len(reports) == (3 if tenant == TENANT else 0)
        for op in ops[tenant]:  # causality is in the payload, not in tenant_seq
            published = sorted(
                (
                    e
                    for _, e in w.events(tenant)
                    if e.type == "operation.evaluation_published" and e.subject.operation_ref == op
                ),
                key=lambda e: e.data.epoch,
            )
            assert len(published) == 4
            for before, after in pairwise(published):
                assert after.data.previous_publication.evaluation_id == before.data.evaluation_id


def test_a_crash_before_commit_leaves_nothing_and_after_commit_nothing_twice(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w.publish("K1")
    w.derive_all()
    before = len(ev.events_of(w.store, TENANT))
    w.store.invalidate(TENANT, [OP], "test")

    def crash(*_: Any) -> None:
        raise RuntimeError("process died between the event and its acknowledgement")

    monkeypatch.setattr(ev, "_before_ack", crash)
    with pytest.raises(RuntimeError):
        ev.derive(w.store, limit=10)
    w.store.conn.rollback()
    assert len(ev.events_of(w.store, TENANT)) == before and w.counter() == before
    assert ev.pending_count(w.store) == 1
    monkeypatch.undo()
    assert ev.derive(w.store, limit=10).derived == 1
    assert [r[0] for r in ev.events_of(w.store, TENANT)] == list(range(1, before + 2))
    # After the commit the source is done: a repeated record (a crash after commit, then
    # a retry) neither duplicates nor numbers anything.
    source_seq = max(w.acks(ev.CONSUMER))
    source = ev.load_source(w.store, source_seq)
    assert ev.record(w.store, source, None) == "already_derived"
    again = ev.derive(w.store, limit=10)
    assert (again.derived, again.pending) == (0, 0) and w.counter() == before + 1


def test_rollbacks_and_duplicate_sources_never_advance_the_counter(w: World) -> None:
    w.publish("K1")
    w.derive_all()
    n = w.counter()
    assert n is not None
    w.store.invalidate(TENANT, [OP], "test")
    (pending,) = w.store.outbox_pending(ev.CONSUMER)
    derived_source = int(w.scalar("SELECT min(source_event_seq) FROM invaria.domain_events"))
    app = w.new_store()

    def insert(source: int) -> None:
        app.conn.execute(
            "INSERT INTO invaria.domain_events (tenant_id, event_id, event_type, type_version, "
            "operation_ref, epoch, report_id, source_event_seq, payload, payload_sha256) "
            "VALUES (%s, %s, 'operation.scope_reopened', 1, %s, 1, NULL, %s, '\\x7b7d', "
            "encode(sha256('\\x7b7d'::bytea), 'hex'))",
            (TENANT, "ev-" + uuid.uuid4().hex * 2, OP, source),
        )

    insert(pending.event_seq)  # takes number n + 1 ...
    app.conn.rollback()  # ... and gives it back
    with pytest.raises(errors.UniqueViolation):
        insert(derived_source)  # a duplicate source fails the whole transaction
    app.conn.rollback()
    assert w.counter() == n
    assert ev.derive(w.store, limit=10).derived == 1
    assert [r[0] for r in ev.events_of(w.store, TENANT)] == list(range(1, n + 2))


def test_a_source_that_becomes_visible_late_is_still_derived(w: World) -> None:
    profile = w.corpus.profile.profile_ref
    w.store.register_scope(OTHER, "B-0001", profile)
    w.publish("K3", OTHER, "B-0001")
    w.publish("K1")
    w.derive_all()
    late = w.new_store()
    with late.conn.transaction():  # lower event_seq, committed after a higher one
        late.invalidate(OTHER, ["B-0001"], "late")
        w.store.invalidate(TENANT, [OP], "early")
        first = ev.derive(w.store, limit=100)
        assert first.derived == 1  # only the committed row is visible
    assert ev.pending_count(w.store) == 1
    second = ev.derive(w.store, limit=100)
    assert second.derived == 1 and second.pending == 0
    other = of_type(w.events(OTHER), "operation.scope_reopened")
    assert [e.data.epoch for e in other] == [2]


# ------------------------------------------------------------------ history


def test_backfill_of_historical_rows_is_resumable_and_honest(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w.publish("EMPTY")
    for scenario in ("K1", "K2"):
        w.feed(scenario)
        w.publish(scenario)
    register_report(w)
    with psycopg.connect(w.dsn) as conn:
        pinned = {
            int(seq): payload
            for seq, payload in conn.execute("SELECT event_seq, payload FROM invaria.outbox")
        }
    # Simulate rows written before pinned references: no marker and no pinned references, and a
    # report registered before 0005 (it never had an outbox row).
    with psycopg.connect(w.dsn, autocommit=True) as admin:
        admin.execute(
            "UPDATE invaria.outbox SET payload = payload - 'payload_version' - "
            "'prior_publication' - 'previous_publication'"
        )
        admin.execute("DELETE FROM invaria.outbox WHERE event_type = 'integrity_report.registered'")
    other_acks = sorted(pinned)[:2]
    for seq in other_acks:
        w.store.outbox_ack("console", seq)
    first = ev.derive(w.store, limit=2)  # an interrupted run: two rows only
    assert first.derived + first.not_public == 2 and first.pending > 0
    calls = {"n": 0}

    def crash_once(*_: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("interrupted")

    monkeypatch.setattr(ev, "_before_ack", crash_once)
    with pytest.raises(RuntimeError):
        ev.derive(w.store, limit=1000)
    w.store.conn.rollback()
    monkeypatch.undo()
    assert w.derive_all().errors == []
    again = ev.derive(w.store, limit=1000)
    assert (again.derived, again.not_public, again.pending) == (0, 0, 0)
    events = w.events()
    assert [s for s, _ in events] == list(range(1, len(events) + 1))
    assert all(e.provenance.source_payload == "historical" for _, e in events)
    assert of_type(events, "integrity_report.registered") == []  # never fabricated
    with psycopg.connect(w.dsn) as conn:
        by_event: dict[str, int] = dict(
            conn.execute("SELECT event_id, source_event_seq FROM invaria.domain_events").fetchall()
        )
    for _, e in events:  # history gives the same publication the fact had pinned
        original = pinned[by_event[e.event_id]]
        if e.type == "operation.evaluation_published":
            named = e.data.previous_publication
            expected = original["previous_publication"]
        else:
            named = e.data.prior_publication
            expected = original["prior_publication"]
        assert (
            None if named is None else {"epoch": named.epoch, "evaluation_id": named.evaluation_id}
        ) == expected
    assert w.acks("console") == set(other_acks)  # other consumers untouched


def test_other_consumers_keep_their_own_acknowledgements(w: World) -> None:
    w.publish("EMPTY")
    w.feed("K1")
    handled = drain(w.store, "worker", lambda event: clocks(w.corpus, "K1"))
    assert handled
    worker_before = w.acks("worker")
    assert w.derive_all().errors == []
    assert w.acks("worker") == worker_before
    assert w.acks(ev.CONSUMER) >= worker_before  # its own acknowledgements, independently
    # The worker still sees what is new for it (its own publications), and nothing else.
    again = drain(w.store, "worker", lambda event: clocks(w.corpus, "K1"))
    assert all(e.event_type == "evaluation.published" and p is None for e, p in again)
    assert w.store.outbox_pending("worker") == []


def test_isolation_between_tenants(w: World) -> None:
    profile = w.corpus.profile.profile_ref
    w.store.register_scope(OTHER, "B-0001", profile)
    w.publish("K3", OTHER, "B-0001")
    w.publish("K1")
    w.derive_all()
    for tenant in (TENANT, OTHER):
        rows = ev.events_of(w.store, tenant)
        assert [r[0] for r in rows] == list(range(1, len(rows) + 1))
        for _, _, _, raw in rows:
            assert TENANT not in raw.decode() and OTHER not in raw.decode()
    assert {e.subject.operation_ref for _, e in w.events(OTHER)} == {"B-0001"}


def test_a_pinned_reference_that_contradicts_history_is_never_trusted(w: World) -> None:
    w.publish("EMPTY")
    w.feed("K1")
    w.publish("K1")
    with psycopg.connect(w.dsn, autocommit=True) as admin:  # altered outbox payloads
        forged = '{"epoch": 7, "evaluation_id": "eval-forged"}'
        admin.execute(
            "UPDATE invaria.outbox SET payload = jsonb_set(payload, '{previous_publication}', "
            "%s::jsonb) WHERE event_type = 'evaluation.published' "
            "AND payload->'previous_publication' <> 'null'::jsonb",
            (forged,),
        )
        admin.execute(
            "UPDATE invaria.outbox SET payload = jsonb_set(payload, '{prior_publication}', "
            "%s::jsonb) WHERE event_type = 'scope.invalidated' AND epoch = 2",
            (forged,),
        )
    assert w.derive_all().errors == []  # the events exist: a publication is never held back
    latest = of_type(w.events(), "operation.evaluation_published")[-1]
    assert latest.data.previous_publication is None  # never invented
    assert latest.data.change.basis == "previous_unidentifiable"
    assert "comparison_incomplete" in latest.categories
    reopened = next(e for e in of_type(w.events(), "operation.scope_reopened") if e.data.epoch == 2)
    assert reopened.data.prior_publication_basis == "unidentifiable"
    assert reopened.data.prior_publication is None and reopened.categories == []


# ------------------------------------------- real timeouts (PostgreSQL, not simulated)


def one_pending_comparison(w: World) -> int:
    """A publication whose comparison needs two diagnostic loads, alone in the outbox."""
    w.publish("EMPTY")
    w.derive_all()
    w.feed("K1")
    w.derive_all()
    w.publish("K1")
    (pending,) = w.store.outbox_pending(ev.CONSUMER)
    assert pending.event_type == "evaluation.published"
    return pending.event_seq


@contextmanager
def observations_locked(w: World) -> Iterator[psycopg.Connection[Any]]:
    """Another session holds ACCESS EXCLUSIVE on a table only the diagnostic load reads,
    so that load really waits in the server."""
    with psycopg.connect(w.dsn) as blocker:
        blocker.execute("LOCK TABLE invaria.observations IN ACCESS EXCLUSIVE MODE")
        try:
            yield blocker
        finally:
            blocker.rollback()


def state(w: World) -> tuple[int, int | None, int]:
    return (len(ev.events_of(w.store, TENANT)), w.counter(), len(w.acks(ev.CONSUMER)))


def settings(w: World) -> tuple[Any, ...]:
    row = w.store.conn.execute(
        "SELECT current_setting('statement_timeout'), current_setting('lock_timeout')"
    ).fetchone()
    w.store.conn.commit()
    assert row is not None
    return tuple(row)


def test_a_real_statement_timeout_is_a_policy_limit_and_commits_a_whole_event(w: World) -> None:
    source = one_pending_comparison(w)
    events, counter, acks = state(w)
    with observations_locked(w):
        started = time.monotonic()
        result = ev.derive(
            w.store, limit=10, limits=Limits(statement_timeout_ms=300, lock_timeout_ms=0)
        )
        waited = time.monotonic() - started
    assert waited >= 0.3  # the server really cancelled the waiting statement
    assert result.errors == [] and result.derived == 1 and result.pending == 0
    # The aborted transaction was rolled back first: the event, its number and its
    # acknowledgement were committed together, in a clean transaction.
    assert state(w) == (events + 1, (counter or 0) + 1, acks + 1)
    assert w.store.conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert settings(w) == ("0", "0")  # the session timeouts are restored
    latest = of_type(w.events(), "operation.evaluation_published")[-1]
    nc = latest.data.change.blocks.not_comparable
    assert nc is not None and (nc.reason, nc.detail, nc.side) == (
        "limit_reached",
        "statement_timeout",
        "previous",
    )
    assert latest.data.change.result.status == "unchanged"  # documents still compared
    assert source in w.acks(ev.CONSUMER)


def test_a_real_lock_wait_is_transient_writes_nothing_and_the_next_run_continues(
    w: World,
) -> None:
    source = one_pending_comparison(w)
    before = state(w)
    with observations_locked(w):
        result = ev.derive(
            w.store, limit=10, limits=Limits(statement_timeout_ms=5000, lock_timeout_ms=200)
        )
    assert result.errors == [f"{source}: transient: LockNotAvailable"]
    assert result.derived == 0 and result.pending == 1
    assert state(w) == before  # no event, no number, no acknowledgement
    assert w.store.conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert settings(w) == ("0", "0")
    retry = ev.derive(w.store, limit=10)  # the contention is gone
    assert (retry.derived, retry.pending, retry.errors) == (1, 0, [])
    latest = of_type(w.events(), "operation.evaluation_published")[-1]
    assert latest.data.change.blocks.not_comparable is None  # compared, not degraded
    assert [r[0] for r in ev.events_of(w.store, TENANT)] == list(range(1, before[0] + 2))


def test_an_operator_cancellation_is_transient_not_a_limit(w: World) -> None:
    source = one_pending_comparison(w)
    before = state(w)
    results: list[ev.RunResult] = []
    with observations_locked(w) as blocker:
        deriver = w.new_store()
        thread = threading.Thread(
            target=lambda: results.append(
                ev.derive(
                    deriver, limit=10, limits=Limits(statement_timeout_ms=30000, lock_timeout_ms=0)
                )
            )
        )
        thread.start()
        for _ in range(200):  # wait until the load is waiting for the lock, then cancel it
            row = blocker.execute(
                "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
            ).fetchone()
            if row:
                blocker.execute("SELECT pg_cancel_backend(%s)", (row[0],))
                break
            time.sleep(0.02)
        else:
            raise AssertionError("the diagnostic load never waited")
        thread.join(timeout=20)
    (result,) = results
    assert result.errors == [f"{source}: transient: QueryCanceled"]
    assert state(w) == before and result.pending == 1


def test_a_database_defect_is_reported_never_turned_into_not_comparable(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = one_pending_comparison(w)
    before = state(w)

    def defect(*_: Any) -> Any:
        raise errors.UndefinedColumn("column does not exist")

    monkeypatch.setattr(PgStore, "load_inputs", defect)
    result = ev.derive(w.store, limit=10)
    assert result.errors == [f"{source}: database: UndefinedColumn"]
    assert state(w) == before and result.pending == 1
