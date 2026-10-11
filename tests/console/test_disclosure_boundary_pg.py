"""The common disclosure boundary of the interactive queries over PostgreSQL and
the real channels: REST, HTML, MCP (in-memory session) and the event feed.

Acceptance criteria written before the code. One
disposable database; the worker evaluates and publishes and the derivation writes events as
in production; one ``ConsoleApp`` or MCP server per access profile, as one process each.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from invaria.console.app import ConsoleApp
from invaria.corpus_loader import Corpus, load_corpus
from invaria.mcp_server import build_server
from invaria.persistence.backup import state_digest
from invaria.persistence.dsn import with_database
from invaria.persistence.events import derive
from invaria.persistence.migrate import migrate
from invaria.persistence.provision import prepare_for_migrator
from invaria.persistence.store import READER_ROLE, PgStore
from invaria.persistence.worker import Clocks, reevaluate
from invaria.query import disclosure
from invaria.query.models import AccessProfile
from invaria.query.service import AuditLog, QueryService

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")
CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"
TENANT, OTHER, OP = "tenant-synthetic-demo", "tenant-other", "SUB-0001"
TOKEN, HOST = "t" * 40, "127.0.0.1:8765"
OPS = disclosure.FULL_OPERATIONS_POLICY
EVI = disclosure.FULL_EVIDENCE_POLICY
IDX = disclosure.FULL_REPORT_INDEX_POLICY
SUM = disclosure.FULL_SUMMARY_POLICY
AGG = disclosure.AGGREGATE_POLICY
ALL = [OPS, EVI, IDX, SUM]
SCOPES = [
    "operations:read",
    "evidence:read",
    "reports:read",
    "reports:register",
    "events:read",
    "disclosures:issue",
    "disclosures:revoke",
]


def profile(policies: list[str], scopes: list[str] = SCOPES, **kw: str) -> AccessProfile:
    return AccessProfile.model_validate_json(
        json.dumps(
            {
                "schema_version": "1.0",
                "principal_id": kw.get("principal", "operator-1"),
                "tenant_id": kw.get("tenant", TENANT),
                "scopes": scopes,
                "max_items": 50,
                "disclosure_policies": policies,
            }
        )
    )


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


@pytest.fixture
def database() -> Iterator[str]:
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
    s = corpus.scenarios[scenario].snapshot
    return Clocks(s.valid_at, s.known_at, s.evaluation_clock)


@dataclass
class World:
    dsn: str
    corpus: Corpus
    conns: list[psycopg.Connection[Any]]
    report_id: str
    view_id: str
    evaluations: list[str]
    failing: str
    evidence_id: str
    other_evaluation: str

    def service(
        self, access: AccessProfile, *, write: bool = False, audit: AuditLog | None = None
    ) -> QueryService:
        conn = psycopg.connect(self.dsn)
        self.conns.append(conn)
        return QueryService(
            PgStore(conn) if write else PgStore(conn, role=READER_ROLE), access, audit
        )

    def app(self, access: AccessProfile, audit: AuditLog | None = None) -> ConsoleApp:
        return ConsoleApp(self.service(access, audit=audit), token=TOKEN, allowed_hosts={HOST})


@pytest.fixture
def w(database: str, corpus: Corpus) -> Iterator[World]:
    """Tenant A: SUB-0001 K1-K3 (BREAK), SUB-0101..0103 (UNKNOWN), SUB-0199 registered only,
    one registered report and its derived events, one view issued to reviewer-1.
    Tenant B: one operation and its own report."""
    store = PgStore(psycopg.connect(database))
    ref = corpus.profile.profile_ref
    store.put_profile(corpus.profile)
    cov = {c for s in ("K1", "K2", "K3") for c in corpus.scenarios[s].snapshot.coverage_ids}
    store.register_scope(TENANT, OP, ref)
    for scenario in ("K1", "K2", "K3"):
        upto = clocks(corpus, scenario).known_at
        store.append_identity_links(
            TENANT, [k for k in corpus.identity_links.values() if k.recorded_at <= upto]
        )
        store.append_coverage(
            c for c in corpus.coverage.values() if c.coverage_id in cov and c.recorded_at <= upto
        )
        store.append_observations(
            o for o in corpus.journals["main"].values() if o.recorded_at <= upto
        )
        reevaluate(store, TENANT, OP, clocks(corpus, scenario))
    for i in range(1, 4):
        store.register_scope(TENANT, f"SUB-01{i:02d}", ref)
        reevaluate(store, TENANT, f"SUB-01{i:02d}", clocks(corpus, "K3"))
    store.register_scope(TENANT, "SUB-0199", ref)
    store.register_scope(OTHER, "B-0001", ref)
    reevaluate(store, OTHER, "B-0001", clocks(corpus, "K2"))
    operator = QueryService(store, profile(ALL))
    _, registration = operator.register_integrity_report("2026-10-20T00:00:00Z")
    QueryService(store, profile(ALL, tenant=OTHER)).register_integrity_report(
        "2026-10-20T00:00:00Z"
    )
    assert derive(store, limit=1000).errors == []
    view = operator.issue_disclosure(registration.report_id, "reviewer-1", AGG)
    trace = operator.trace_operation(OP)
    assert trace.current is not None
    failing = next(c.control_id for c in trace.current.controls if c.status == "FAIL")
    rows = store.conn.execute(
        "SELECT e.evaluation_id FROM invaria.evaluations e JOIN invaria.snapshots s USING "
        "(tenant_id, snapshot_id) WHERE s.tenant_id = %s AND s.operation_ref = %s "
        "ORDER BY s.known_at",
        (TENANT, OP),
    ).fetchall()
    other = store.conn.execute(
        "SELECT min(evaluation_id) FROM invaria.evaluations WHERE tenant_id = %s", (OTHER,)
    ).fetchone()
    evidence = store.conn.execute(
        "SELECT min(observation_id) FROM invaria.observations WHERE tenant_id = %s", (TENANT,)
    ).fetchone()
    store.conn.commit()
    assert other is not None and evidence is not None
    world = World(
        database,
        corpus,
        [store.conn],
        registration.report_id,
        view.view_id,
        [str(r[0]) for r in rows],
        failing,
        str(evidence[0]),
        str(other[0]),
    )
    yield world
    for conn in world.conns:
        conn.close()


def call(app: ConsoleApp, path: str) -> tuple[int, dict[str, str], bytes]:
    target, _, query = path.partition("?")
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": target,
        "QUERY_STRING": query,
        "HTTP_HOST": HOST,
        "HTTP_AUTHORIZATION": f"Bearer {TOKEN}",
        "wsgi.errors": io.StringIO(),
    }
    seen: list[tuple[str, list[tuple[str, str]]]] = []
    body = b"".join(app(environ, lambda status, hs: seen.append((status, hs))))
    return int(seen[0][0].split()[0]), dict(seen[0][1]), body


def code(body: bytes) -> str:
    return str(json.loads(body)["error"]["code"])


def routes(w: World) -> list[tuple[str, tuple[str, ...]]]:
    """Every group-A REST route and HTML page of the matrix, with the policies it needs."""
    ev, last, first = w.evaluations[-1], w.evaluations[-1], w.evaluations[0]
    when = "valid_at=2026-10-01T17:00:00Z&known_at=2026-10-01T18:00:00Z"
    rid = w.report_id
    return [
        ("/v1/operations", (OPS,)),
        (f"/v1/operations/{OP}", (OPS,)),
        (f"/v1/operations/{OP}/timeline", (OPS,)),
        (f"/v1/operations/{OP}/conclusion?{when}", (OPS,)),
        (f"/v1/operations/{OP}/coverage", (OPS,)),
        (f"/v1/evaluations/{ev}", (OPS,)),
        (f"/v1/evaluations/{ev}/controls/{w.failing}/discrepancy", (OPS,)),
        (f"/v1/evaluations/{ev}/controls/{w.failing}/missing-evidence", (OPS,)),
        (f"/v1/evaluation-comparisons?left={first}&right={last}", (OPS,)),
        (f"/v1/evidence/{w.evidence_id}", (EVI,)),
        ("/v1/integrity-reports", (IDX,)),
        (f"/v1/integrity-reports/{rid}", (SUM,)),
        (f"/v1/integrity-reports/{rid}/cases?key=included", (SUM, OPS)),
        ("/", (OPS,)),
        (f"/operations/{OP}", (OPS,)),
        (f"/operations/{OP}/coverage", (OPS,)),
        (f"/operations/{OP}/as-known?{when}", (OPS,)),
        (f"/evaluations/{ev}", (OPS,)),
        (f"/evaluations/{ev}/controls/{w.failing}", (OPS,)),
        (f"/compare?left={first}&right={last}", (OPS,)),
        (f"/evidence/{w.evidence_id}", (EVI,)),
        ("/reports", (IDX,)),
        (f"/reports/{rid}", (SUM,)),
        (f"/reports/{rid}/cases?key=included", (SUM, OPS)),
    ]


# ------------------------------------------ the matrix: every route, every policy


def test_every_route_needs_exactly_its_policies(w: World) -> None:
    full = w.app(profile(ALL))
    matrix = routes(w)
    assert len([p for p, _ in matrix if p.startswith("/v1/")]) == 13
    for path, needed in matrix:
        assert call(full, path)[0] == 200, path
        for missing in needed:  # D-029-2: every policy is required
            app = w.app(profile([p for p in ALL if p != missing]))
            status, headers, body = call(app, path)
            assert status == 403, (path, missing)
            if path.startswith("/v1/"):
                assert code(body) == "DISCLOSURE_POLICY_UNAVAILABLE"
            text = body.decode("utf-8")
            assert disclosure.POLICY_MESSAGE in text
            assert w.report_id not in text + json.dumps(headers), path
        # A policy of another family never stands in for the missing one.
        unrelated = [p for p in ALL if p not in needed] + [AGG]
        assert call(w.app(profile(unrelated)), path)[0] == 403, path


def test_a_policy_without_its_scope_gives_nothing_and_disclosures_read_inherits_nothing(
    w: World,
) -> None:
    only_policies = w.app(profile([*ALL, AGG], scopes=["disclosures:read"], principal="reviewer-1"))
    for path, _ in routes(w):
        status, _, body = call(only_policies, path)
        assert status == 403, path
        if path.startswith("/v1/"):
            assert code(body) == "FORBIDDEN", path
    # The restricted view is unchanged: the reviewer reads its view and nothing else.
    reviewer = w.app(profile([AGG], scopes=["disclosures:read"], principal="reviewer-1"))
    assert call(reviewer, f"/v1/disclosures/{w.view_id}")[0] == 200
    assert call(reviewer, "/v1/events?start=beginning")[0] == 403


def test_tenant_isolation_holds_with_every_policy(w: World) -> None:
    other = w.app(profile(ALL, tenant=OTHER))
    for path in (
        f"/v1/operations/{OP}",
        f"/v1/evaluations/{w.evaluations[-1]}",
        f"/v1/evidence/{w.evidence_id}",
        f"/v1/integrity-reports/{w.report_id}",
    ):
        status, _, body = call(other, path)
        assert (status, code(body)) == (404, "NOT_FOUND"), path
    mine = w.app(profile(ALL))
    assert call(mine, f"/v1/evaluations/{w.other_evaluation}")[0] == 404


# ------------------------------------------------------- MCP, the same boundary


def mcp(w: World, access: AccessProfile, tool: str, args: dict[str, str]) -> Any:
    async def run() -> Any:
        async with create_connected_server_and_client_session(
            build_server(w.service(access))
        ) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(run())


def test_mcp_tools_apply_the_same_boundary_and_expose_the_same_model(w: World) -> None:
    ev = w.evaluations[-1]
    tools = {
        "trace_operation": ({"operation_ref": OP}, f"/v1/operations/{OP}", OPS),
        "get_conclusion_as_known_at": (
            {
                "operation_ref": OP,
                "valid_at": "2026-10-01T17:00:00Z",
                "known_at": "2026-10-01T18:00:00Z",
            },
            f"/v1/operations/{OP}/conclusion?valid_at=2026-10-01T17:00:00Z"
            "&known_at=2026-10-01T18:00:00Z",
            OPS,
        ),
        "get_missing_evidence": (
            {"evaluation_id": ev, "control_id": w.failing},
            f"/v1/evaluations/{ev}/controls/{w.failing}/missing-evidence",
            OPS,
        ),
        "explain_discrepancy": (
            {"evaluation_id": ev, "control_id": w.failing},
            f"/v1/evaluations/{ev}/controls/{w.failing}/discrepancy",
            OPS,
        ),
        "explain_conclusion_change": ({"old_id": w.evaluations[0], "new_id": ev}, None, OPS),
        "get_evidence": ({"evidence_id": w.evidence_id}, f"/v1/evidence/{w.evidence_id}", EVI),
        "get_coverage": ({"operation_ref": OP}, f"/v1/operations/{OP}/coverage", OPS),
    }
    rest = w.app(profile(ALL))
    for tool, (args, path, policy) in tools.items():
        granted = mcp(w, profile(ALL), tool, args)
        assert not granted.isError, tool
        if path is not None:  # the MCP envelope differs; the authorized model is the same
            assert granted.structuredContent == json.loads(call(rest, path)[2]), tool
        refused = mcp(w, profile([p for p in ALL if p != policy]), tool, args)
        assert refused.isError, tool
        text = refused.content[0].text
        assert "DISCLOSURE_POLICY_UNAVAILABLE: " + disclosure.POLICY_MESSAGE in text, tool
        assert OP not in text and w.evidence_id not in text


# ------------------------------------------------- links and navigation (precision 2)


def hrefs(body: bytes) -> list[str]:
    return [h for h in re.findall(r'href="([^"]+)"', body.decode("utf-8")) if h.startswith("/")]


def test_kept_links_always_lead_to_an_available_page(w: World) -> None:
    """Crawl every page available to partial profiles: a kept link never leads to a
    refusal, and the navigation lists only available destinations."""
    partial = {
        "index only": [IDX],
        "index and summary": [IDX, SUM],
        "operations only": [OPS],
        "operations and evidence": [OPS, EVI],
        "summary and operations": [SUM, OPS],
    }
    for label, policies in partial.items():
        app = w.app(profile(policies))
        pages = [
            p
            for p, needed in routes(w)
            if not p.startswith("/v1/") and set(needed) <= set(policies)
        ]
        assert pages, label
        for page in pages:
            status, _, body = call(app, page)
            assert status == 200, (label, page)
            for link in hrefs(body):
                if link.startswith("/static/"):
                    continue
                target = call(app, link.replace("&amp;", "&"))
                assert target[0] == 200, (label, page, link, target[0])


def test_identity_links_of_an_epoch_are_text_not_links_to_evidence(w: World) -> None:
    """The timeline named identity links (`link-…`) as /evidence/{id}, a page that
    only serves observations and coverage certificates, so the link gave 404."""
    full = call(w.app(profile([OPS, EVI])), f"/operations/{OP}")[2].decode("utf-8")
    links = [k for k in w.corpus.identity_links if k in full]
    assert links  # the operation's epochs do name identity links
    for policies in ([OPS, EVI], [OPS]):
        app = w.app(profile(policies))
        status, _, body = call(app, f"/operations/{OP}")
        page = body.decode("utf-8")
        assert status == 200
        for link_id in links:
            assert f'href="/evidence/{link_id}"' not in page
            assert f"<code>{link_id}</code>" in page  # authorized identifier, kept as text
        epochs = [s.split("</ul>", 1)[0] for s in page.split('class="tl-evidence"')[1:]]
        in_timeline = [h for e in epochs for h in hrefs(e.encode()) if h.startswith("/evidence/")]
        evidence = [h for h in hrefs(body) if h.startswith("/evidence/")]
        if EVI in policies:
            assert in_timeline  # observations and certificates of an epoch keep their links ...
            assert all(call(app, h)[0] == 200 for h in evidence)
        else:
            assert evidence == []  # ... only when the profile can open them


def test_an_authorized_identifier_stays_as_text_when_its_page_is_unavailable(w: World) -> None:
    rid = w.report_id
    index_only = w.app(profile([IDX], scopes=["reports:read"]))
    page = call(index_only, "/reports")[2].decode("utf-8")
    assert rid[:15] in page  # the listing authorizes the identifier ...
    assert f'href="/reports/{rid}"' not in page  # ... but not a link to a refused summary
    summary_only = w.app(profile([IDX, SUM], scopes=["reports:read"]))
    summary = call(summary_only, f"/reports/{rid}")[2].decode("utf-8")
    assert "included" in summary  # case keys are part of the authorized summary
    assert "/cases" not in summary  # the cases need operations: no link
    nav = re.search(r'<nav aria-label="Primary">.*?</nav>', summary, re.S)
    assert nav is not None and 'href="/reports"' in nav.group(0)
    assert 'href="/"' not in nav.group(0) and 'href="/events"' not in nav.group(0)
    # Unavailable destinations are not listed at all, not even as plain text.
    assert "Operations" not in nav.group(0) and "Events" not in nav.group(0)
    assert "Integrity reports" in nav.group(0)


def test_data_not_authorized_in_the_source_view_is_omitted_with_its_link(w: World) -> None:
    """A restricted view does not authorize identifiers of the original: no identifier and
    no link to it. An events page shows only readable types: the subjects and
    links of the others are not rendered."""
    reviewer = w.app(profile([AGG], scopes=["disclosures:read"], principal="reviewer-1"))
    view = call(reviewer, f"/disclosures/{w.view_id}")[2].decode("utf-8")
    assert w.report_id not in view and "/reports" not in view and OP not in view
    reports_events = w.app(
        profile([SUM], scopes=["events:read", "operations:read", "reports:read"])
    )
    page = call(reports_events, "/events?start=beginning")[2].decode("utf-8")
    assert "integrity_report.registered" in page or "Integrity report registered" in page
    assert OP not in page and "/operations/" not in page and "/evaluations/" not in page


# ---------------------------------------------------- the feed and policy changes


def feed(app: ConsoleApp, query: str) -> dict[str, Any]:
    status, _, body = call(app, f"/v1/events?{query}")
    assert status == 200, body
    return dict(json.loads(body))


def drain(app: ConsoleApp, query: str) -> dict[str, Any]:
    """Follow the cursor to the observed end; items concatenated, notices of every page."""
    page = feed(app, query)
    items, notices = list(page["items"]), list(page["notices"])
    while not page["caught_up"]:
        page = feed(app, f"cursor={page['next_cursor']}&limit=50")
        items += page["items"]
        notices += page["notices"]
    return {**page, "items": items, "notices": notices}


def test_feed_types_follow_current_policies_never_the_cursor(w: World) -> None:
    scopes = ["events:read", "operations:read", "reports:read"]
    full, no_ops, report_only = (
        w.app(profile([OPS, SUM], scopes=scopes)),
        w.app(profile([SUM], scopes=scopes)),
        w.app(profile([SUM], scopes=scopes)),
    )
    everything = drain(full, "start=beginning&limit=50")
    types = {e["event"]["type"] for e in everything["items"]}
    assert "operation.evaluation_published" in types and "integrity_report.registered" in types
    # Same scopes, policy withdrawn: a cursor issued before the change authorizes nothing.
    first = feed(full, "start=beginning&limit=1")
    after = drain(no_ops, f"cursor={first['next_cursor']}&limit=50")
    assert {e["event"]["type"] for e in after["items"]} == {"integrity_report.registered"}
    assert [n["kind"] for n in after["notices"]] == ["readable_types_narrowed"]
    assert set(after["notices"][0]["types"]) == {
        "operation.scope_reopened",
        "operation.evaluation_published",
    }
    # Same scopes, policy added: examined-without-delivery is reported, not replayed.
    start = drain(report_only, "start=beginning&limit=50")
    assert {e["event"]["type"] for e in start["items"]} == {"integrity_report.registered"}
    widened = feed(full, f"cursor={start['next_cursor']}&limit=50")
    notice = widened["notices"][0]
    assert notice["kind"] == "readable_types_widened"
    assert notice["examined_without_delivery_through"] == start["continuation_position"]
    # Without any policy no type is readable: positions are still examined (the volume stays
    # revealed by events:read) and nothing is delivered.
    none = w.app(profile([], scopes=scopes))
    empty = drain(none, "start=beginning&limit=50")
    assert (
        empty["items"] == []
        and empty["continuation_position"] == everything["continuation_position"]
    )


def test_pages_without_deliverable_events_advance_and_skip_nothing_authorized(
    w: World,
) -> None:
    scopes = ["events:read", "operations:read", "reports:read"]
    full = w.app(profile([OPS, SUM], scopes=scopes))
    report_only = w.app(profile([SUM], scopes=scopes))
    all_events = drain(full, "start=beginning&limit=50")["items"]
    report_seq = [
        e["tenant_seq"] for e in all_events if e["event"]["type"] == "integrity_report.registered"
    ]
    assert report_seq and report_seq[0] > 1  # operation events come before it
    page = feed(report_only, "start=beginning&limit=1")
    seen: list[int] = []
    while True:  # one item per page: the report event is never passed over
        seen += [e["tenant_seq"] for e in page["items"]]
        if page["caught_up"]:
            break
        page = feed(report_only, f"cursor={page['next_cursor']}&limit=1")
    assert seen == report_seq


# --------------------------------------------- errors, caches, no business writes


def test_refusals_and_every_response_carry_no_store_and_no_identifiers(w: World) -> None:
    rid = w.report_id
    apps = {"full": w.app(profile(ALL)), "none": w.app(profile([]))}
    for app in apps.values():
        for path in (
            f"/v1/integrity-reports/{rid}",
            f"/reports/{rid}",
            "/v1/operations",
            "/v1/evidence/obs-unknown",
            "/v1/operations/x?bad=1",
            "/nowhere",
        ):
            status, headers, body = call(app, path)
            assert headers["Cache-Control"] == "no-store", (path, status)
            assert "ETag" not in headers and "Last-Modified" not in headers
            if status == 403:
                assert rid not in body.decode("utf-8")
    # Refusal before existence: a missing and an existing report give the same answer.
    missing = call(apps["none"], "/v1/integrity-reports/ir-" + "f" * 64)
    present = call(apps["none"], f"/v1/integrity-reports/{rid}")
    assert (missing[0], json.loads(missing[2])["error"]["message"]) == (
        present[0],
        json.loads(present[2])["error"]["message"],
    )


def test_reading_writes_no_business_data_only_the_requested_audit(w: World, tmp_path: Path) -> None:
    def digest() -> Any:
        with psycopg.connect(w.dsn) as conn, conn.transaction():
            return state_digest(conn)

    before = digest()
    log = tmp_path / "audit.jsonl"
    app = w.app(profile(ALL), AuditLog(log))
    for path, _ in routes(w):
        call(app, path)
    call(app, "/v1/events?start=beginning")
    call(w.app(profile([]), AuditLog(log)), "/v1/operations")
    assert digest() == before  # the database is unchanged; the audit is a local file
    lines = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
    assert {line["status"] for line in lines} >= {"ok", "DISCLOSURE_POLICY_UNAVAILABLE"}
    assert all(set(line) == {"at", "principal", "tool", "args_sha256", "status"} for line in lines)


def test_removing_a_policy_takes_effect_for_a_new_process_only(w: World) -> None:
    """D-029-6: policies are read when a process starts. A process started before the
    change keeps serving until it is restarted; the new one refuses."""
    before = w.app(profile(ALL))
    after = w.app(profile([EVI, IDX, SUM]))
    assert call(before, f"/v1/operations/{OP}")[0] == 200
    assert call(after, f"/v1/operations/{OP}")[0] == 403
    assert call(before, f"/v1/operations/{OP}")[0] == 200  # declared limit, not dynamic
