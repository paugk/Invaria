"""Analytics in the local console against PostgreSQL.

Expectations were written before the code: the HTML pages
present exactly what /v1 returns for the same report and context; no page writes; the
browser walk is opt-in (``INVARIA_BROWSER_E2E=1``) like the console's.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest

from invaria.console.app import SECURITY_HEADERS, ConsoleApp
from invaria.console.pages import href
from invaria.console.server import build
from invaria.corpus_loader import Corpus, load_corpus
from invaria.persistence import events as ev
from invaria.persistence.backup import state_digest
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
EMPTY_OPS = ["SUB-0101", "SUB-0102", "SUB-0103"]
TOKEN = "t" * 40
HOST = "127.0.0.1:8765"
HORIZON = "2026-10-20T00:00:00Z"
ALL = ("operations:read", "reports:read", "events:read", "evidence:read")
CHROME = next((p for p in ("google-chrome", "chromium") if shutil.which(p)), None)


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
                # The operator's explicit full disclosure policies.
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
    report_id: str
    conns: list[psycopg.Connection[Any]]

    def service(
        self, *scopes: str, limits: EventFeedLimits | None = None, **kw: Any
    ) -> QueryService:
        conn = psycopg.connect(self.dsn)
        self.conns.append(conn)
        return QueryService(
            PgStore(conn, role=READER_ROLE), access(*scopes, **kw), event_limits=limits
        )

    def app(self, *scopes: str, limits: EventFeedLimits | None = None, **kw: Any) -> ConsoleApp:
        return ConsoleApp(
            self.service(*(scopes or ALL), limits=limits, **kw),
            token=TOKEN,
            allowed_hosts={HOST},
        )

    def new_evidence(self) -> None:
        """Later facts: one operation reopened and published again, events derived."""
        later = clocks(self.corpus, "K3")
        self.store.invalidate(TENANT, [EMPTY_OPS[0]], "later evidence")
        reevaluate(self.store, TENANT, EMPTY_OPS[0], later)
        self.store.invalidate(TENANT, [OP], "later evidence")
        reevaluate(self.store, TENANT, OP, later)
        ev.derive(self.store, limit=1000)

    def digest(self) -> Any:
        with psycopg.connect(self.dsn) as conn:  # every table and sequence, as admin
            with conn.transaction():
                return state_digest(conn)


@pytest.fixture
def w(dsn: str, corpus: Corpus) -> Iterator[World]:
    """SUB-0001 published K1, K2, K3 (BREAK); three operations published without evidence
    (UNKNOWN); one registered, never evaluated; a registered report; derived events. The
    other tenant has one operation and its own report."""
    store = PgStore(psycopg.connect(dsn))
    profile = corpus.profile.profile_ref
    store.put_profile(corpus.profile)
    store.register_scope(TENANT, OP, profile)
    for scenario in ("K1", "K2", "K3"):
        upto = clocks(corpus, scenario).known_at
        cov = {x for s in ("K1", "K2", "K3") for x in corpus.scenarios[s].snapshot.coverage_ids}
        store.append_identity_links(
            TENANT, [k for k in corpus.identity_links.values() if k.recorded_at <= upto]
        )
        store.append_coverage(
            x for x in corpus.coverage.values() if x.coverage_id in cov and x.recorded_at <= upto
        )
        store.append_observations(
            o for o in corpus.journals["main"].values() if o.recorded_at <= upto
        )
        reevaluate(store, TENANT, OP, clocks(corpus, scenario))
    for op in EMPTY_OPS:
        store.register_scope(TENANT, op, profile)
        reevaluate(store, TENANT, op, clocks(corpus, "K3"))
    store.register_scope(TENANT, "SUB-0300", profile)  # registered, never evaluated
    store.register_scope(OTHER, "B-0001", profile)
    reevaluate(store, OTHER, "B-0001", clocks(corpus, "K2"))
    conns: list[psycopg.Connection[Any]] = []
    registrar = psycopg.connect(dsn)
    conns.append(registrar)
    _, registration = QueryService(
        PgStore(registrar), access("operations:read", "reports:register")
    ).register_integrity_report(HORIZON)
    other = psycopg.connect(dsn)
    conns.append(other)
    QueryService(
        PgStore(other), access("operations:read", "reports:register", tenant=OTHER)
    ).register_integrity_report(HORIZON)
    ev.derive(store, limit=1000)
    world = World(dsn, corpus, store, registration.report_id, conns)
    yield world
    for conn in world.conns:
        conn.close()
    store.conn.close()


def get(app: ConsoleApp, path: str) -> tuple[int, dict[str, str], str]:
    target, _, query = path.partition("?")
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": target,
        "QUERY_STRING": query,
        "HTTP_HOST": HOST,
        "HTTP_AUTHORIZATION": f"Bearer {TOKEN}",
        "wsgi.errors": io.StringIO(),
    }
    captured: list[tuple[str, list[tuple[str, str]]]] = []
    body = b"".join(app(environ, lambda status, headers: captured.append((status, headers))))
    status, headers = captured[0]
    return int(status.split()[0]), dict(headers), body.decode("utf-8")


class Rows(HTMLParser):
    """Collects data-* attributes of table rows, links and structural facts."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, str]] = []
        self.links: list[str] = []
        self.tags: list[str] = []
        self.forms: list[dict[str, str]] = []
        self.inputs: list[str] = []
        self.labels: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        self.tags.append(tag)
        if tag == "tr" and any(k.startswith("data-") for k in a):
            self.rows.append(a)
        if tag == "a" and "href" in a:
            self.links.append(a["href"])
        if tag == "form":
            self.forms.append(a)
        if tag == "input" and a.get("type") in ("checkbox", "radio"):
            self.inputs.append(a.get("id", ""))
        if tag == "label":
            self.labels.append(a.get("for", ""))


def parse(html: str) -> Rows:
    rows = Rows()
    rows.feed(html)
    return rows


def link_to(rows: Rows, prefix: str) -> list[str]:
    return [link for link in rows.links if link.startswith(prefix)]


# ----------------------------------------------------- HTML equals API and the report


def test_the_page_shows_the_same_figures_and_cases_as_the_api(w: World) -> None:
    app = w.app()
    status, _, api = get(app, f"/v1/integrity-reports/{w.report_id}")
    summary = json.loads(api)
    status, _, html = get(app, f"/reports/{w.report_id}")
    assert status == 200
    shown = {r["data-metric"]: r for r in parse(html).rows if "data-metric" in r}
    expected = [
        m
        for section in ("distribution", "discrepancies", "blockers")
        for m in summary[section]["metrics"]
        if not m["metric_id"].startswith("A.status:")
    ]
    assert expected and set(shown) == {m["metric_id"] for m in expected}
    for m in expected:
        row = shown[m["metric_id"]]
        assert row["data-numerator"] == str(m["numerator"])
        assert row["data-denominator"] == (
            "" if m["denominator"] is None else str(m["denominator"])
        )
        assert row["data-cases"] == m["cases_key"]
        # the link actually followed leads to that pinned segment
        assert href("reports", w.report_id, "cases", key=m["cases_key"]) in parse(html).links
    for status_name, count in summary["distribution"]["statuses"].items():
        assert f'<th scope="row">{status_name}</th>' in html and f">{count}<" in html
    # Every figure's cases link leads to the pinned segment that produced it.
    for m in expected:
        if m["kind"] != "ratio" and m["metric_id"] != "A.included":
            continue
        _, _, api_cases = get(
            app, f"/v1/integrity-reports/{w.report_id}/cases?key={m['cases_key']}"
        )
        _, _, html_cases = get(app, f"/reports/{w.report_id}/cases?key={m['cases_key']}")
        api_ops = [c["operation_ref"] for c in json.loads(api_cases)["cases"]]
        html_ops = [r["data-operation"] for r in parse(html_cases).rows]
        assert html_ops == api_ops, m["metric_id"]
        if "operation" in m["numerator_definition"]:
            assert len(api_ops) == m["numerator"], m["metric_id"]  # counts operations
        else:  # a figure of another unit lists the operations that contain it
            assert len(api_ops) <= m["numerator"], m["metric_id"]


def test_discrepancy_groups_keep_their_unit_operations_versus_incidents(w: World) -> None:
    app = w.app()
    summary = json.loads(get(app, f"/v1/integrity-reports/{w.report_id}")[2])
    (group,) = summary["discrepancies"]["groups"]
    _, _, html = get(app, f"/reports/{w.report_id}")
    assert "Operations (unit of the cases)" in html and "Control incidents" in html
    assert "not time unpaid" in html and "never a loss" in html
    _, _, cases = get(app, f"/reports/{w.report_id}/cases?key={group['cases_key']}")
    rows = parse(cases).rows
    assert len(rows) == group["operations"]
    assert cases.count("<strong>discrepancy</strong>") == group["incidents"]


def test_a_zero_denominator_is_not_calculable_never_zero_percent(w: World) -> None:
    conn = psycopg.connect(w.dsn)
    w.conns.append(conn)
    service = QueryService(PgStore(conn), access("operations:read", "reports:register"))
    _, registration = service.register_integrity_report(
        HORIZON, profile_refs=["nonexistent-profile@1.0.0"]
    )
    _, _, html = get(w.app(), f"/reports/{registration.report_id}")
    assert "not calculable (denominator is zero)" in html
    assert "0.00 %" not in html and ">0 %<" not in html


def test_financial_results_and_technical_situation_are_separate(w: World) -> None:
    _, _, html = get(w.app(), f"/reports/{w.report_id}")
    financial = html.split('id="financial-h"')[1].split('id="situation-h"')[0]
    situation = html.split('id="situation-h"')[1].split('id="discrepancies-h"')[0]
    for status_name in ("registered_not_evaluated", "stale_publication", "engine_retired"):
        assert status_name not in financial
    assert "registered_not_evaluated" in situation
    assert "UNKNOWN is not non-payment" in financial

    def metrics(part: str) -> set[str]:
        return {r["data-metric"] for r in parse(part).rows if "data-metric" in r}

    assert metrics(financial) == {
        "A.match_share_of_admitted",
        "A.break_share_of_admitted",
        "A.unknown_share_of_admitted",
        "A.conclusive_share_of_admitted",
        "A.match_share_of_conclusive",
        "A.match_share_of_included",
    }
    assert metrics(situation) == {
        "A.included",
        "A.admitted_share",
        "A.technical_error_share_of_admitted",
    }
    assert "never as financial UNKNOWN" in situation
    assert "Diagnosed blocks" in html and "Not applicable is not satisfied" in html


# -------------------------------------------------------- pinned versus current views


def test_the_report_pages_do_not_change_with_new_evidence(w: World) -> None:
    app = w.app()
    pages = [f"/reports/{w.report_id}", f"/reports/{w.report_id}/cases?key=result:BREAK"]
    before = [get(app, p)[2] for p in pages]
    w.new_evidence()
    assert [get(w.app(), p)[2] for p in pages] == before


def test_pinned_cases_are_separated_from_current_details(w: World) -> None:
    app = w.app()
    _, _, cases = get(app, f"/reports/{w.report_id}/cases?key=result:BREAK")
    assert "Pinned in the report" in cases and "Current view" in cases
    (evaluation,) = link_to(parse(cases), "/evaluations/")
    w.new_evidence()
    _, _, current = get(w.app(), evaluation)
    assert "superseded" in current or "stale" in current  # today's currency, labelled
    _, _, again = get(w.app(), f"/reports/{w.report_id}/cases?key=result:BREAK")
    assert again == cases and "result-break" in again  # the pinned result stays


# ---------------------------------------------------------------------- authorization


def test_every_page_reuses_the_authorization_of_the_api(w: World) -> None:
    no_reports = w.app("operations:read", "events:read")
    for path in ("/reports", f"/reports/{w.report_id}"):
        assert get(no_reports, path)[0] == 403
    no_operations = w.app("reports:read")
    assert get(no_operations, f"/reports/{w.report_id}")[0] == 200
    assert get(no_operations, f"/reports/{w.report_id}/cases?key=included")[0] == 403
    assert get(w.app("operations:read", "reports:read"), "/events?start=beginning")[0] == 403
    other = w.app(tenant=OTHER)
    assert get(other, f"/reports/{w.report_id}")[0] == 404  # same answer as a missing id
    mine = parse(get(other, "/reports")[2])
    assert not [link for link in mine.links if w.report_id in link]  # never listed


# ------------------------------------------------------------------------------ events


def follow(app: ConsoleApp, path: str) -> tuple[list[str], list[str]]:
    """Walk the events pages through their own "continue" links until caught up."""
    events: list[str] = []
    pages: list[str] = []
    for _ in range(200):
        status, _, html = get(app, path)
        assert status == 200, html
        pages.append(html)
        events += [r["data-event"] for r in parse(html).rows if "data-event" in r]
        if "Reached the last observed event" in html:
            return events, pages
        (path,) = [link for link in link_to(parse(html), "/events?cursor=")]
    raise AssertionError("never caught up")


def test_events_need_an_explicit_start_and_continue_by_cursor(w: World) -> None:
    app = w.app(max_items=2)
    status, _, intro = get(app, "/events")
    assert status == 200 and "Nothing is shown until you choose where to start" in intro
    assert "data-event" not in intro
    events, pages = follow(app, "/events?start=beginning")
    api_ids: list[str] = []
    page = json.loads(get(app, "/v1/events?start=beginning")[2])
    while True:
        api_ids += [i["event"]["event_id"] for i in page["items"]]
        if page["caught_up"]:
            break
        page = json.loads(get(app, f"/v1/events?cursor={page['next_cursor']}")[2])
    assert events == api_ids  # the HTML walk shows exactly the API's events, in order
    assert "not the end of the feed" in pages[-1] and "flow ended" not in pages[-1].lower()
    assert all("no live update" in p for p in pages)
    assert "Fact recorded at" in pages[0] and "Derived at" in pages[0]
    assert "(not a statement that it is still current)" in "".join(pages)
    status, _, now = get(app, "/events?start=now")
    assert "Position fixed at the last event observed now" in now
    filtered = get(app, "/events?start=beginning&type_report=1")[2]
    assert "<code>integrity_report.registered</code>" in filtered
    assert "<code>operation.scope_reopened</code>" not in filtered


def test_an_empty_page_from_the_work_limit_offers_to_continue(w: World) -> None:
    app = w.app("reports:read", "events:read", limits=EventFeedLimits(max_examined=2))
    _, _, html = get(app, "/events?start=beginning")
    assert "No event shown on this page. That is not the end: continue." in html
    assert link_to(parse(html), "/events?cursor=")
    events, _ = follow(app, "/events?start=beginning")
    assert len(events) == 1  # the one report, reached through empty pages


def test_a_cursor_reset_is_explained_with_explicit_options(w: World) -> None:
    app = w.app()
    _, _, html = get(app, "/events?start=beginning")
    (cursor_link,) = link_to(parse(html), "/events?cursor=")
    with psycopg.connect(w.dsn, autocommit=True) as admin:
        admin.execute("INSERT INTO invaria.feed_generations (reason) VALUES ('restore:test')")
    status, _, reset = get(app, cursor_link)
    assert status == 409 and "CURSOR_RESET" in reset and "data-event" not in reset
    options = link_to(parse(reset), "/events?start=")
    assert {parse_qs(urlsplit(o).query)["start"][0] for o in options} == {"beginning", "now"}


def test_permission_widening_and_volume_are_disclosed(w: World) -> None:
    narrow = w.app("operations:read", "events:read", max_items=1)
    _, _, first = get(narrow, "/events?start=beginning")
    (cursor_link,) = link_to(parse(first), "/events?cursor=")
    _, _, widened = get(w.app(), cursor_link)
    assert "Newly readable types integrity_report.registered" in widened
    assert "read them from the beginning" in widened
    assert "positions and volume" in widened


def test_historical_events_are_labelled_as_provenance(w: World) -> None:
    w.store.invalidate(TENANT, [OP], "old row")
    with psycopg.connect(w.dsn, autocommit=True) as admin:
        admin.execute(
            "UPDATE invaria.outbox SET payload = payload - 'payload_version' - "
            "'prior_publication' WHERE event_seq = (SELECT max(event_seq) FROM invaria.outbox)"
        )
    ev.derive(w.store, limit=1000)
    _, _, html = get(w.app(), "/events?start=beginning&limit=50")
    assert "historical (provenance: written before pinned references" in html


# ------------------------------------------------------------- safety and no writes


def test_parameters_are_validated_and_escaped(w: World) -> None:
    app = w.app()
    for path in (
        f"/reports/{w.report_id}/cases?key=<script>alert(1)</script>",
        "/events?start=<img src=x onerror=alert(1)>",
        "/events?start=beginning&types=<b>bold</b>",
        "/reports/<svg onload=alert(1)>",
        "/events?cursor=%22%3E%3Cscript%3E",
        "/events?start=beginning&evil=1",
    ):
        status, _, html = get(app, path)
        assert status in (404, 422), path
        assert "<script>" not in html and "<img" not in html and "<svg" not in html
        assert "<b>bold" not in html


def test_navigation_never_writes(w: World) -> None:
    before = w.digest()
    app = w.app(max_items=2)
    for path in (
        "/reports",
        f"/reports/{w.report_id}",
        f"/reports/{w.report_id}/cases?key=included",
        f"/reports/{w.report_id}/cases?key=result:BREAK",
        "/events",
        "/events?start=beginning",
        "/events?start=now",
        f"/operations/{OP}",
    ):
        follow_up = get(app, path)
        assert follow_up[0] == 200, path
    follow(app, "/events?start=beginning")
    assert w.digest() == before  # no report, evaluation, event or acknowledgement


def test_headers_csp_and_no_script_on_every_new_page(w: World) -> None:
    app = w.app()
    for path in (
        "/reports",
        f"/reports/{w.report_id}",
        f"/reports/{w.report_id}/cases?key=included",
        "/events",
        "/events?start=beginning",
    ):
        status, headers, html = get(app, path)
        assert status == 200
        for name, value in SECURITY_HEADERS:
            assert headers[name] == value, (path, name)
        rows = parse(html)
        assert "script" not in rows.tags and "<script" not in html
        assert rows.tags.count("h1") == 1 and "nav" in rows.tags
        assert all(f.get("method") == "get" for f in rows.forms)
        assert set(rows.inputs) <= set(rows.labels)  # every box has its label
        assert "<th scope=" in html or "<table" not in html


# --------------------------------------------------------------- browser (opt-in)


@pytest.mark.browser
def test_the_walk_in_a_real_browser(w: World, tmp_path: Path) -> None:
    if os.environ.get("INVARIA_BROWSER_E2E") != "1" or CHROME is None:
        pytest.skip("set INVARIA_BROWSER_E2E=1 with Chrome or Chromium installed")
    service = w.service(*ALL, max_items=5)
    server, login = build(service, 0, TOKEN)
    app = server.get_app()
    assert app is not None
    import hashlib
    import hmac

    cookie = (
        f"invaria_console_{server.server_port}="
        + hmac.new(TOKEN.encode(), b"invaria-console-session", hashlib.sha256).hexdigest()
    )

    def signed_in(environ: dict[str, Any], start_response: Any) -> Any:
        environ["HTTP_COOKIE"] = cookie  # the token never appears in a URL or a capture
        return app(environ, start_response)

    server.set_app(signed_in)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = login.split("/login", 1)[0]
    out = Path(os.environ.get("INVARIA_BROWSER_SHOTS", tmp_path))
    out.mkdir(parents=True, exist_ok=True)

    def chrome(*args: str) -> str:
        assert CHROME is not None
        return subprocess.run(
            [
                CHROME,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                f"--user-data-dir={tmp_path / 'profile'}",
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout

    try:
        summary = chrome("--dump-dom", f"{base}/reports/{w.report_id}")
        assert "Financial result of admitted conclusions" in summary
        group = service.get_integrity_report_summary(w.report_id).discrepancies.groups[0].cases_key
        cases = chrome("--dump-dom", f"{base}/reports/{w.report_id}/cases?key={group}")
        (evaluation,) = link_to(parse(cases), "/evaluations/")
        walk = [
            ("1-reports", "/reports"),
            ("2-summary", f"/reports/{w.report_id}"),
            ("3-discrepancy-cases", f"/reports/{w.report_id}/cases?key={group}"),
            ("4-evaluation", evaluation),
            ("5-events", "/events?start=beginning"),
        ]
        for name, url in walk:
            shot = out / f"{name}.png"
            chrome(
                f"--screenshot={shot}",
                "--window-size=1280,2400",
                "--hide-scrollbars",
                f"{base}{url}",
            )
            assert shot.stat().st_size > 10_000
        w.new_evidence()
        after = chrome("--dump-dom", f"{base}/reports/{w.report_id}")
        assert after == summary  # 6. the original report is still pinned
        chrome(
            f"--screenshot={out / '6-summary-after-new-evidence.png'}",
            "--window-size=1280,2400",
            "--hide-scrollbars",
            f"{base}/reports/{w.report_id}",
        )
        chrome(
            f"--screenshot={out / '7-events-mobile.png'}",
            "--window-size=390,1800",
            "--hide-scrollbars",
            f"{base}/events?start=beginning",
        )
    finally:
        server.shutdown()
        server.server_close()
