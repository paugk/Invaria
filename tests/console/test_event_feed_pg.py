"""Event feed by cursor over the console's REST /v1 and PostgreSQL.

Expectations were written before the code. Events are
produced by the domain-event deriver from facts written by the worker; two tenants share one
disposable database; requests go through the WSGI application in process.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from invaria.console.app import ConsoleApp
from invaria.contracts.events import canonical_bytes, parse_event
from invaria.corpus_loader import Corpus, load_corpus
from invaria.persistence import events as ev
from invaria.persistence.dsn import with_database
from invaria.persistence.migrate import migrate
from invaria.persistence.provision import prepare_for_migrator
from invaria.persistence.store import READER_ROLE, PgStore
from invaria.persistence.worker import Clocks, reevaluate
from invaria.query.models import AccessProfile
from invaria.query.service import EventFeedLimits, QueryService

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")
CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"
TENANT = "tenant-synthetic-demo"
OTHER = "tenant-other"
OP = "SUB-0001"
TOKEN = "t" * 40
HOST = "127.0.0.1:8765"
BEFORE = datetime(2026, 8, 31, tzinfo=UTC)
ALL = ("events:read", "operations:read", "reports:read")
REPORT = "integrity_report.registered"
OPERATION_TYPES = {"operation.scope_reopened", "operation.evaluation_published"}


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


def clocks(corpus: Corpus, scenario: str) -> Clocks:
    if scenario == "EMPTY":
        return Clocks(BEFORE, BEFORE, BEFORE)
    s = corpus.scenarios[scenario].snapshot
    return Clocks(s.valid_at, s.known_at, s.evaluation_clock)


def access(*scopes: str, tenant: str = TENANT, max_items: int = 50) -> AccessProfile:
    return AccessProfile.model_validate_json(
        json.dumps(
            {
                "schema_version": "1.0",
                "principal_id": "analyst-1",
                "tenant_id": tenant,
                "scopes": list(scopes),
                "max_items": max_items,
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


@dataclass
class World:
    dsn: str
    corpus: Corpus
    store: PgStore
    conns: list[psycopg.Connection[Any]]

    def connect(self) -> psycopg.Connection[Any]:
        conn = psycopg.connect(self.dsn)
        self.conns.append(conn)
        return conn

    def app(
        self, *scopes: str, tenant: str = TENANT, limits: EventFeedLimits | None = None, **kw: Any
    ) -> ConsoleApp:
        service = QueryService(
            PgStore(self.connect(), role=READER_ROLE),
            access(*scopes, tenant=tenant, **kw),
            event_limits=limits,
        )
        return ConsoleApp(service, token=TOKEN, allowed_hosts={HOST})

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

    def register_report(self) -> None:
        service = QueryService(
            PgStore(self.connect()), access("operations:read", "reports:register")
        )
        service.register_integrity_report("2026-10-20T00:00:00Z")

    def derive(self) -> None:
        while True:
            result = ev.derive(self.store, limit=1000)
            assert result.errors == []
            if result.pending == 0:
                return

    def stored(self, tenant: str = TENANT) -> list[tuple[int, str, str]]:
        """(tenant_seq, event_type, event_id) of a tenant, from the store."""
        rows = self.store.conn.execute(
            "SELECT tenant_seq, event_type, event_id FROM invaria.domain_events "
            "WHERE tenant_id = %s ORDER BY tenant_seq",
            (tenant,),
        ).fetchall()
        self.store.conn.commit()
        return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]

    def admin(self, statement: str, *params: Any) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(statement, params)


@pytest.fixture
def w(dsn: str, corpus: Corpus) -> Iterator[World]:
    """Tenant A: SUB-0001 history (reopenings and publications) and one registered report.
    Tenant B: one operation of its own."""
    store = PgStore(psycopg.connect(dsn))
    store.put_profile(corpus.profile)
    store.register_scope(TENANT, OP, corpus.profile.profile_ref)
    world = World(dsn, corpus, store, [])
    world.publish("EMPTY")
    world.feed("K1")
    world.publish("K1")
    world.register_report()
    store.register_scope(OTHER, "B-0001", corpus.profile.profile_ref)
    world.publish("K3", OTHER, "B-0001")
    world.derive()
    yield world
    for conn in world.conns:
        conn.close()
    store.conn.close()


def get(app: ConsoleApp, path: str) -> tuple[int, dict[str, Any]]:
    target, _, query = path.partition("?")
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": target,
        "QUERY_STRING": query,
        "HTTP_HOST": HOST,
        "HTTP_AUTHORIZATION": f"Bearer {TOKEN}",
        "wsgi.errors": io.StringIO(),
    }
    captured: list[str] = []
    body = b"".join(app(environ, lambda status, _h: captured.append(status)))
    return int(captured[0].split()[0]), json.loads(body)


def walk(
    app: ConsoleApp, first: str, extra: str = "", pages: int = 1000
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every page from ``first`` until caught up; returns (items, pages)."""
    items: list[dict[str, Any]] = []
    seen: list[dict[str, Any]] = []
    status, page = get(app, f"/v1/events?{first}{extra}")
    for _ in range(pages):
        assert status == 200, page
        seen.append(page)
        items += page["items"]
        if page["caught_up"]:
            return items, seen
        status, page = get(app, f"/v1/events?cursor={page['next_cursor']}{extra}")
    raise AssertionError("the feed did not reach its head")


def ids(items: list[dict[str, Any]]) -> list[str]:
    return [i["event"]["event_id"] for i in items]


# ------------------------------------------------------------- types and permissions


def test_each_type_needs_its_resource_permission_and_every_item_verifies(w: World) -> None:
    stored = w.stored()
    assert {t for _, t, _ in stored} == {*OPERATION_TYPES, REPORT}
    full, _ = walk(w.app(*ALL), "start=beginning")
    assert [(i["tenant_seq"], i["event"]["type"], i["event"]["event_id"]) for i in full] == stored
    for item in full:  # payload_sha256 is the sha256 of the canonical event bytes
        raw = canonical_bytes(parse_event(json.dumps(item["event"]).encode()))
        assert hashlib.sha256(raw).hexdigest() == item["payload_sha256"]
    ops, _ = walk(w.app("events:read", "operations:read"), "start=beginning")
    assert {i["event"]["type"] for i in ops} == OPERATION_TYPES
    reports, _ = walk(w.app("events:read", "reports:read"), "start=beginning")
    assert {i["event"]["type"] for i in reports} == {REPORT}
    none, pages = walk(w.app("events:read"), "start=beginning")
    assert none == [] and pages[-1]["continuation_position"] == len(stored)  # volume visible
    status, body = get(w.app("operations:read", "reports:read"), "/v1/events?start=beginning")
    assert (status, body["error"]["code"]) == (403, "FORBIDDEN")


def test_tenants_are_isolated_and_a_cursor_does_not_travel(w: World) -> None:
    mine, _ = walk(w.app(*ALL), "start=beginning")
    theirs, pages = walk(w.app(*ALL, tenant=OTHER), "start=beginning")
    assert set(ids(mine)).isdisjoint(ids(theirs))
    assert {i["event"]["subject"]["operation_ref"] for i in theirs} == {"B-0001"}
    cursor = pages[-1]["next_cursor"]
    status, body = get(w.app(*ALL), f"/v1/events?cursor={cursor}")
    assert (status, body["error"]["code"]) == (422, "VALIDATION_ERROR")
    assert OTHER not in json.dumps(body) and "B-0001" not in json.dumps(body)


# --------------------------------------------------------------- start and filters


def test_start_beginning_now_and_cursor(w: World) -> None:
    app = w.app(*ALL)
    for bad in ("", "start=beginning&cursor=x", "start=yesterday", "start="):
        status, body = get(app, f"/v1/events?{bad}")
        assert (status, body["error"]["code"]) == (422, "VALIDATION_ERROR"), bad
    status, now = get(app, "/v1/events?start=now")
    head = len(w.stored())
    assert now["items"] == [] and now["stop_reason"] == "start_now"
    assert now["continuation_position"] == now["observed_head"] == head
    assert (now["examined_after"], now["examined"], now["delivered"]) == (head, 0, 0)
    w.store.invalidate(TENANT, [OP], "after now")
    w.derive()
    status, after = get(app, f"/v1/events?cursor={now['next_cursor']}")
    assert [i["tenant_seq"] for i in after["items"]] == [head + 1]  # only what came after


def test_filters_are_normalized_and_bound_to_the_cursor(w: World) -> None:
    app = w.app(*ALL)
    messy = "operation.scope_reopened,integrity_report.registered,operation.scope_reopened"
    status, page = get(app, f"/v1/events?start=beginning&limit=1&types={messy}")
    assert status == 200
    assert page["filter_types"] == ["integrity_report.registered", "operation.scope_reopened"]
    same = "integrity_report.registered,operation.scope_reopened"
    items, _ = walk(app, "start=beginning", f"&types={same}")
    assert {i["event"]["type"] for i in items} == {REPORT, "operation.scope_reopened"}
    cursor = page["next_cursor"]
    assert get(app, f"/v1/events?cursor={cursor}&types={same}")[0] == 200  # same set
    for bad in ("", "&types=operation.evaluation_published", "&types=nope", "&types="):
        status, body = get(app, f"/v1/events?cursor={cursor}{bad}")
        assert (status, body["error"]["code"]) == (422, "VALIDATION_ERROR"), bad


# --------------------------------------------------------- cursor manipulation


def forge(cursor: str, **changes: Any) -> str:
    outer = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    body = {**json.loads(outer["b"]), **changes}
    outer["b"] = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(json.dumps(outer).encode()).decode().rstrip("=")


def test_a_manipulated_or_foreign_generation_cursor_is_refused(w: World) -> None:
    app = w.app(*ALL)
    _, page = get(app, "/v1/events?start=beginning&limit=1")
    cursor = page["next_cursor"]
    for bad in (
        forge(cursor, p=0),
        forge(cursor, r=["integrity_report.registered"]),  # a false permission history
        forge(cursor, f=["integrity_report.registered"]),
        forge(cursor, v=2),
        cursor[:-2],
        cursor + "!",
        "A" * 2000,
    ):
        status, body = get(app, f"/v1/events?cursor={bad}")
        assert (status, body["error"]["code"]) == (422, "VALIDATION_ERROR")
    w.admin("INSERT INTO invaria.feed_generations (reason) VALUES ('restore:test')")
    status, body = get(app, f"/v1/events?cursor={cursor}")
    assert (status, body["error"]["code"]) == (409, "CURSOR_RESET")
    assert get(w.app(*ALL), "/v1/events?start=beginning")[0] == 200  # restart works


# ------------------------------------------------------------ permission changes


def test_revocation_applies_next_request_and_widening_only_forward(w: World) -> None:
    narrow = w.app("events:read", "operations:read")
    _, first = get(narrow, "/v1/events?start=beginning&limit=1")
    assert first["readable_types"] == sorted(OPERATION_TYPES)
    wide = w.app(*ALL)
    status, page = get(wide, f"/v1/events?cursor={first['next_cursor']}")
    assert status == 200
    (notice,) = page["notices"]
    assert notice == {
        "kind": "readable_types_widened",
        "types": [REPORT],
        "examined_without_delivery_through": first["continuation_position"],
    }
    # The report is after the first page: it is delivered going forward.
    assert REPORT in {i["event"]["type"] for i in walk(wide, f"cursor={first['next_cursor']}")[0]}
    # Re-reading earlier events of the widened type needs an explicit restart.
    again, _ = walk(wide, "start=beginning", f"&types={REPORT}")
    assert [i["event"]["type"] for i in again] == [REPORT]
    # Revocation: the next request already excludes the type, with a notice.
    _, full = get(wide, "/v1/events?start=beginning&limit=1")
    status, revoked = get(narrow, f"/v1/events?cursor={full['next_cursor']}")
    assert revoked["notices"][0]["kind"] == "readable_types_narrowed"
    rest, _ = walk(narrow, f"cursor={full['next_cursor']}")
    assert REPORT not in {i["event"]["type"] for i in rest}


# ------------------------------------------------------------- limits without loss


def test_empty_pages_advance_under_the_work_limit(w: World) -> None:
    for _ in range(9):  # many operation events after the report
        w.store.invalidate(TENANT, [OP], "load")
    w.derive()
    app = w.app("events:read", "reports:read", limits=EventFeedLimits(max_examined=3))
    items, pages = walk(app, "start=beginning")
    assert [i["event"]["type"] for i in items] == [REPORT]
    empty = [p for p in pages if not p["items"]]
    assert empty and all(
        p["stop_reason"] == "work_limit" and not p["caught_up"] for p in empty[:-1]
    )
    assert all(p["examined"] <= 3 for p in pages)
    assert pages[-1]["stop_reason"] == "end_observed"
    assert len(pages) <= len(w.stored()) // 3 + 2  # bounded, never stuck


def test_item_and_byte_limits_never_lose_an_authorized_event(w: World) -> None:
    stored = [e for _, _, e in w.stored()]
    one, pages = walk(w.app(*ALL, max_items=1), "start=beginning")
    assert ids(one) == stored and all(p["delivered"] <= 1 for p in pages)
    _, sample = get(w.app(*ALL), "/v1/events?start=beginning&limit=1")
    size = len(json.dumps(sample["items"][0]))
    budget = max(4096, size + size // 2)  # room for one item, rarely two
    small, pages = walk(w.app(*ALL), "start=beginning", f"&max_bytes={budget}")
    assert ids(small) == stored
    assert any(p["stop_reason"] == "byte_limit" for p in pages)


def test_a_single_event_larger_than_max_bytes_is_an_explicit_recoverable_error(w: World) -> None:
    limits = EventFeedLimits(min_max_bytes=100)
    app = w.app(*ALL, limits=limits)
    _, first = get(app, "/v1/events?start=beginning&limit=1")
    cursor = first["next_cursor"]
    for _ in range(2):  # same answer every time: not skipped, not an empty loop
        status, body = get(app, f"/v1/events?cursor={cursor}&max_bytes=200")
        assert (status, body["error"]["code"]) == (422, "EVENT_EXCEEDS_MAX_BYTES")
        assert body["error"]["details"]["position"] == first["continuation_position"]
        required = body["error"]["details"]["required_bytes"]
    status, page = get(app, f"/v1/events?cursor={cursor}&max_bytes={required}")
    assert status == 200 and page["items"][0]["tenant_seq"] == first["continuation_position"] + 1


# ------------------------------------------------- concurrency, backfill and re-reads


def test_events_derived_while_reading_are_all_delivered_once_by_event_id(w: World) -> None:
    app = w.app(*ALL, max_items=2)
    delivered: list[str] = []
    errors: list[BaseException] = []
    done = threading.Event()

    def produce() -> None:
        try:
            store = PgStore(w.connect())
            for _ in range(15):
                store.invalidate(TENANT, [OP], "concurrent")
                ev.derive(store, limit=10)
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    thread = threading.Thread(target=produce)
    _, page = get(app, "/v1/events?start=beginning")
    thread.start()
    while True:
        assert "items" in page, page
        delivered += ids(page["items"])
        finished = done.is_set()
        if finished and page["caught_up"]:
            break
        _, page = get(app, f"/v1/events?cursor={page['next_cursor']}")
    thread.join()
    assert errors == []
    w.derive()  # whatever the producer left pending
    rest, _ = walk(app, f"cursor={page['next_cursor']}")
    delivered += ids(rest)
    assert delivered == [e for _, _, e in w.stored()]  # every one, once, in order


def test_a_historical_event_derived_after_start_now_is_delivered(w: World) -> None:
    w.store.invalidate(TENANT, [OP], "fact before the reader started")
    w.admin(
        "UPDATE invaria.outbox SET payload = payload - 'payload_version' - 'prior_publication' "
        "WHERE event_seq = (SELECT max(event_seq) FROM invaria.outbox)"
    )  # an outbox row written before pinned references existed
    app = w.app(*ALL)
    _, now = get(app, "/v1/events?start=now")
    w.derive()  # derived after the reader fixed its position
    _, page = get(app, f"/v1/events?cursor={now['next_cursor']}")
    (item,) = page["items"]
    assert item["event"]["provenance"]["source_payload"] == "historical"
    assert item["tenant_seq"] == now["continuation_position"] + 1


def test_a_client_interrupted_before_saving_rereads_and_deduplicates(w: World) -> None:
    app = w.app(*ALL, max_items=2)
    _, first = get(app, "/v1/events?start=beginning")
    saved = first["next_cursor"]  # processed and saved
    _, second = get(app, f"/v1/events?cursor={saved}")  # processed, crash before saving
    _, again = get(app, f"/v1/events?cursor={saved}")
    assert ids(again["items"]) == ids(second["items"])  # same page: deduplicate by event_id
    seen = set(ids(first["items"])) | set(ids(second["items"]))
    assert set(ids(again["items"])) <= seen


# ------------------------------------------------------------ integrity and versions


def rewrite_event(w: World, seq: int, edit: Any) -> None:
    row = w.store.conn.execute(
        "SELECT payload FROM invaria.domain_events WHERE tenant_id = %s AND tenant_seq = %s",
        (TENANT, seq),
    ).fetchone()
    w.store.conn.commit()
    assert row is not None
    data = json.loads(bytes(row[0]))
    edit(data)
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    w.admin(
        "UPDATE invaria.domain_events SET payload = %s, payload_sha256 = %s "
        "WHERE tenant_id = %s AND tenant_seq = %s",
        raw,
        hashlib.sha256(raw).hexdigest(),
        TENANT,
        seq,
    )


def test_a_corrupt_event_fails_the_page_and_is_never_passed_over(w: World) -> None:
    app = w.app(*ALL, max_items=1)
    _, first = get(app, "/v1/events?start=beginning")
    original = w.store.conn.execute(
        "SELECT payload FROM invaria.domain_events WHERE tenant_id = %s AND tenant_seq = 2",
        (TENANT,),
    ).fetchone()
    w.store.conn.commit()
    assert original is not None
    w.admin(  # tampering that keeps the table's own checks: non-canonical bytes
        "UPDATE invaria.domain_events SET payload = payload || '\\x20'::bytea, "
        "payload_sha256 = encode(sha256(payload || '\\x20'::bytea), 'hex') "
        "WHERE tenant_id = %s AND tenant_seq = 2",
        TENANT,
    )
    for _ in range(2):  # the same cursor fails again: the event is not passed over
        status, body = get(app, f"/v1/events?cursor={first['next_cursor']}")
        assert (status, body["error"]["code"]) == (500, "INTEGRITY_ERROR")
        assert set(body) == {"error"}
    w.admin(
        "UPDATE invaria.domain_events SET payload = %s, payload_sha256 = %s "
        "WHERE tenant_id = %s AND tenant_seq = 2",
        bytes(original[0]),
        hashlib.sha256(bytes(original[0])).hexdigest(),
        TENANT,
    )  # repaired by an administrator: reading resumes at the same position
    rewrite_event(w, 3, lambda d: d.update(type_version=2))
    status, two = get(app, f"/v1/events?cursor={first['next_cursor']}")
    assert status == 200 and two["items"][0]["tenant_seq"] == 2
    status, body = get(app, f"/v1/events?cursor={two['next_cursor']}")
    assert (status, body["error"]["code"]) == (501, "UNSUPPORTED_EVENT_VERSION")


def test_a_coherent_rewrite_by_an_administrator_is_not_detectable(w: World) -> None:
    """Declared limit: payload_sha256 is not a signature. Bytes and hash rewritten together
    in canonical form pass every check of the feed."""
    rewrite_event(w, 2, lambda d: d["data"].update(epoch=99))
    items, _ = walk(w.app(*ALL), "start=beginning")
    assert items[1]["event"]["data"]["epoch"] == 99


# ---------------------------------------------------- generation and process restart


def test_the_generation_survives_a_process_restart(w: World) -> None:
    _, a = get(w.app(*ALL), "/v1/events?start=beginning&limit=1")
    _, b = get(w.app(*ALL), f"/v1/events?cursor={a['next_cursor']}")  # a new process
    assert a["generation_id"] == b["generation_id"] and b["items"]
