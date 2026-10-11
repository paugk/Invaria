"""Registered integrity reports over the console's REST ``/v1`` and PostgreSQL.

Expectations (criteria 1-12) were written before the code.
Two tenants share one disposable database; the worker evaluates and publishes as in
production; requests go through the WSGI application in process (the server binding is
the console's and unchanged).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from invaria.cli import main
from invaria.console.app import ConsoleApp
from invaria.corpus_loader import Corpus, load_corpus
from invaria.persistence.dsn import with_database
from invaria.persistence.migrate import migrate
from invaria.persistence.provision import prepare_for_migrator
from invaria.persistence.store import READER_ROLE, PgStore
from invaria.persistence.worker import Clocks, reevaluate
from invaria.query import analytics, registry
from invaria.query.models import AccessProfile, IntegrityReport, StoreReadView
from invaria.query.service import QueryError, QueryService

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")
CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"
TENANT = "tenant-synthetic-demo"
OTHER = "tenant-other"
OP = "SUB-0001"
EMPTY_OPS = [f"SUB-01{i:02d}" for i in range(1, 8)]  # no evidence: published UNKNOWN
TOKEN = "t" * 40
HOST = "127.0.0.1:8765"


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


def feed(store: PgStore, corpus: Corpus, upto: datetime) -> None:
    coverage = {c for s in ("K1", "K2", "K3") for c in corpus.scenarios[s].snapshot.coverage_ids}
    store.append_identity_links(
        TENANT, [k for k in corpus.identity_links.values() if k.recorded_at <= upto]
    )
    store.append_coverage(
        c for c in corpus.coverage.values() if c.coverage_id in coverage and c.recorded_at <= upto
    )
    store.append_observations(o for o in corpus.journals["main"].values() if o.recorded_at <= upto)


def access(tenant: str = TENANT, scopes: list[str] | None = None, **extra: Any) -> AccessProfile:
    return AccessProfile.model_validate_json(
        json.dumps(
            {
                "schema_version": "1.0",
                "principal_id": "analyst-1",
                "tenant_id": tenant,
                "scopes": scopes or ["operations:read", "reports:read", "reports:register"],
                "max_items": 50,
                # The operator's explicit full disclosure policies.
                "disclosure_policies": [
                    "full-operations@1",
                    "full-evidence@1",
                    "full-report-index@1",
                    "full-report-summary@1",
                ],
                **extra,
            }
        )
    )


@dataclass
class World:
    dsn: str
    corpus: Corpus
    writer: PgStore
    conns: list[psycopg.Connection[Any]]

    def service(self, profile: AccessProfile | None = None, *, write: bool = False) -> QueryService:
        conn = psycopg.connect(self.dsn)
        self.conns.append(conn)
        store = PgStore(conn) if write else PgStore(conn, role=READER_ROLE)
        return QueryService(store, profile or access())

    def app(self, profile: AccessProfile | None = None) -> ConsoleApp:
        return ConsoleApp(self.service(profile), token=TOKEN, allowed_hosts={HOST})


@pytest.fixture
def w(database: str, corpus: Corpus) -> Iterator[World]:
    """Tenant A: SUB-0001 published K1, K2, K3 (BREAK); SUB-0101..0107 published without
    evidence (UNKNOWN, blocked). Tenant B: one published operation of its own."""
    writer = PgStore(psycopg.connect(database))
    profile = corpus.profile.profile_ref
    writer.put_profile(corpus.profile)
    writer.register_scope(TENANT, OP, profile)
    for scenario in ("K1", "K2", "K3"):
        feed(writer, corpus, clocks(corpus, scenario).known_at)
        _, publication = reevaluate(writer, TENANT, OP, clocks(corpus, scenario))
        assert publication.outcome == "PUBLISHED"
    for op in EMPTY_OPS:
        writer.register_scope(TENANT, op, profile)
        _, publication = reevaluate(writer, TENANT, op, clocks(corpus, "K3"))
        assert publication.outcome == "PUBLISHED"
    writer.register_scope(OTHER, "B-0001", profile)
    _, publication = reevaluate(writer, OTHER, "B-0001", clocks(corpus, "K2"))
    assert publication.outcome == "PUBLISHED"
    w = World(database, corpus, writer, [])
    yield w
    for conn in w.conns:
        conn.close()
    writer.conn.close()


HORIZON = "2026-10-20T00:00:00Z"


def register(w: World, tenant: str = TENANT) -> tuple[IntegrityReport, str]:
    report, registration = w.service(access(tenant), write=True).register_integrity_report(HORIZON)
    assert registration.created
    return report, registration.report_id


def get(app: ConsoleApp, path: str, **headers: str) -> tuple[int, dict[str, Any]]:
    target, _, query = path.partition("?")
    environ = {
        "REQUEST_METHOD": headers.pop("method", "GET"),
        "PATH_INFO": target,
        "QUERY_STRING": query,
        "HTTP_HOST": HOST,
        "HTTP_AUTHORIZATION": f"Bearer {TOKEN}",
        "wsgi.errors": io.StringIO(),
        **{f"HTTP_{k.upper()}": v for k, v in headers.items()},
    }
    captured: list[str] = []
    body = b"".join(app(environ, lambda status, _headers: captured.append(status)))
    return int(captured[0].split()[0]), (json.loads(body) if body else {})


def rows(w: World) -> int:
    with psycopg.connect(w.dsn) as conn:
        found = conn.execute("SELECT count(*) FROM invaria.integrity_reports").fetchone()
    assert found is not None
    return int(found[0])


# ------------------------------------------------------------------ 1. registration


def test_registration_needs_its_scope_and_stores_what_the_service_computed(w: World) -> None:
    no_scope = w.service(access(scopes=["operations:read", "reports:read"]), write=True)
    with pytest.raises(QueryError) as error:
        no_scope.register_integrity_report(HORIZON)
    assert error.value.code == "FORBIDDEN" and rows(w) == 0
    report, report_id = register(w)
    assert report_id == "ir-" + report.report_sha256
    with psycopg.connect(w.dsn) as conn:
        row = conn.execute(
            "SELECT document, registered_by, registered_at, evidence_horizon, store_read_at "
            "FROM invaria.integrity_reports"
        ).fetchone()
    assert row is not None
    assert bytes(row[0]) == registry.canonical_bytes(report)  # exactly the computed result
    assert row[1] == "analyst-1"
    # Three separate times: registration (store clock), evidence horizon, store read.
    assert row[3] == report.as_of and row[4] == report.store_read.read_at
    assert row[2] >= row[4] and row[2] != row[3]


def test_registering_the_same_document_twice_is_one_resource(w: World) -> None:
    service = w.service(write=True)
    report = service.integrity_report(HORIZON)
    first = service._register(report, registry.DEFAULT_MAX_DOCUMENT_BYTES)
    again = service._register(report, registry.DEFAULT_MAX_DOCUMENT_BYTES)
    assert (first.created, again.created) == (True, False)
    assert (first.report_id, first.registered_at) == (again.report_id, again.registered_at)
    assert rows(w) == 1


def test_a_document_above_the_limit_or_of_another_tenant_is_not_registered(w: World) -> None:
    service = w.service(write=True)
    with pytest.raises(QueryError) as error:
        service.register_integrity_report(HORIZON, max_document_bytes=1024)
    assert error.value.code == "VALIDATION_ERROR" and rows(w) == 0
    foreign = w.service(access(OTHER), write=True).integrity_report(HORIZON)
    with pytest.raises(QueryError) as error:
        service._register(foreign, registry.DEFAULT_MAX_DOCUMENT_BYTES)
    assert error.value.code == "FORBIDDEN" and rows(w) == 0


def test_the_reader_role_cannot_register(w: World) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        w.service().register_integrity_report(HORIZON)  # invaria_reader: no INSERT
    assert rows(w) == 0


def test_the_cli_registers_only_what_it_computes(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INVARIA_DATABASE_URL", w.dsn)
    allowed = tmp_path / "a.json"
    allowed.write_text(access().model_dump_json(), "utf-8")
    denied = tmp_path / "d.json"
    denied.write_text(access(scopes=["operations:read"]).model_dump_json(), "utf-8")
    base = ["analytics", "report", "--evidence-horizon", HORIZON, "--register"]
    assert main([*base, "--access-profile", str(denied)]) == 2
    assert "FORBIDDEN" in capsys.readouterr().err and rows(w) == 0
    assert main([*base, "--access-profile", str(allowed)]) == 0
    out = capsys.readouterr().out
    assert "registered: ir-" in out and "not a signature" in out and rows(w) == 1


# ------------------------------------------------------------ 2. listing and summary


def test_listing_and_summary_are_per_tenant_and_never_confirm_other_ids(w: World) -> None:
    report, report_id = register(w)
    _, other_id = register(w, OTHER)
    app = w.app(access(scopes=["reports:read"]))
    status, listing = get(app, "/v1/integrity-reports")
    assert status == 200 and [i["report_id"] for i in listing["items"]] == [report_id]
    assert listing["next_cursor"] is None
    status, summary = get(app, f"/v1/integrity-reports/{report_id}")
    assert status == 200 and summary["report_sha256"] == report.report_sha256
    text = json.dumps(summary)
    assert "analyst-1" not in text and "access_scopes" not in text and OP not in text
    for metric in summary["distribution"]["metrics"]:
        assert {"numerator", "denominator", "cases_key"} <= set(metric)
    assert summary["temporal"]["publication_state_reconstructed_at_horizon"] is False
    absent = "ir-" + "0" * 64
    other = get(app, f"/v1/integrity-reports/{other_id}")
    missing = get(app, f"/v1/integrity-reports/{absent}")
    assert other[0] == missing[0] == 404
    assert other[1]["error"]["message"].replace(other_id, "X") == missing[1]["error"][
        "message"
    ].replace(absent, "X")
    assert get(w.app(access(scopes=["operations:read"])), "/v1/integrity-reports")[0] == 403
    assert get(app, f"/v1/integrity-reports/{report_id}", method="HEAD")[0] == 200


def test_the_client_cannot_declare_tenant_identity_or_scopes(w: World) -> None:
    _, report_id = register(w)
    _, other_id = register(w, OTHER)
    reader = w.app(access(scopes=["reports:read"]))
    assert get(reader, "/v1/integrity-reports?tenant_id=" + OTHER)[0] == 422
    assert get(reader, f"/v1/integrity-reports/{report_id}?scopes=operations:read")[0] == 422
    spoofed = get(
        reader,
        f"/v1/integrity-reports/{other_id}",
        x_tenant_id=OTHER,
        x_invaria_scopes="reports:read",
        x_principal_id="admin",
    )
    assert spoofed[0] == 404
    denied = get(reader, f"/v1/integrity-reports/{report_id}/cases?key=included", x_scopes="x")
    assert denied[0] == 403  # cases need operations:read too, whatever a header says
    assert get(reader, "/v1/integrity-reports/not-an-id")[0] == 422
    assert get(reader, "/v1/integrity-reports", method="POST")[0] == 405


def test_a_listing_pages_with_a_position_cursor(w: World) -> None:
    ids = {register(w)[1] for _ in range(3)}
    app = w.app(access(scopes=["reports:read"], max_items=2))
    status, first = get(app, "/v1/integrity-reports?limit=2")
    assert status == 200 and len(first["items"]) == 2 and first["next_cursor"]
    _, second = get(app, f"/v1/integrity-reports?cursor={first['next_cursor']}&limit=2")
    seen = [i["report_id"] for i in first["items"] + second["items"]]
    assert sorted(seen) == sorted(ids) and second["next_cursor"] is None
    assert get(app, "/v1/integrity-reports?limit=3")[0] == 422
    assert get(app, "/v1/integrity-reports?cursor=")[0] == 422
    assert get(app, "/v1/integrity-reports?cursor=garbage!")[0] == 422


# -------------------------------------------------------------------- 3. cases pages


def walk(app: ConsoleApp, report_id: str, key: str, limit: int) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    path = f"/v1/integrity-reports/{report_id}/cases?key={key}&limit={limit}"
    while True:
        status, page = get(app, path)
        assert status == 200, page
        cases += page["cases"]
        if "next_cursor" not in page:
            return cases
        path = f"/v1/integrity-reports/{report_id}/cases?key={key}&cursor={page['next_cursor']}"
        path += f"&limit={limit}"


def test_pages_cover_each_figure_once_in_order_and_count_what_it_counts(w: World) -> None:
    report, report_id = register(w)
    app = w.app()
    _, summary = get(app, f"/v1/integrity-reports/{report_id}")
    assert summary["discrepancies"]["groups"] and summary["blockers"]["groups"]
    figures = {
        m["cases_key"]: m["numerator"]
        for s in ("distribution", "discrepancies", "blockers")
        for m in summary[s]["metrics"]
        if m["metric_id"].startswith("A.")
        or m["metric_id"].endswith(
            (
                "operations_with_discrepancy_share_of_admitted",
                "operations_with_block_share_of_admitted",
            )
        )
    }
    for key in summary["case_keys"]:
        cases = walk(app, report_id, key, 3)
        ops = [c["operation_ref"] for c in cases]
        assert ops == [m.operation_ref for m in analytics.members_for(report, key)], key
        assert len(set(ops)) == len(ops) and ops == sorted(ops)
        if key in figures:
            assert len(ops) == figures[key], key
    for group in summary["discrepancies"]["groups"]:  # operations and incidents differ
        cases = walk(app, report_id, group["cases_key"], 2)
        assert len(cases) == group["operations"]
        incidents = [
            r
            for c in cases
            for r in c["reasons"]
            if r["kind"] == "discrepancy" and r["control_id"] == group["control_id"]
        ]
        assert len(incidents) == group["incidents"]
    for group in summary["blockers"]["groups"]:  # operations and requirements differ
        cases = walk(app, report_id, group["cases_key"], 2)
        assert len(cases) == group["operations"]
        blocks = [
            r
            for c in cases
            for r in c["reasons"]
            if r["kind"] == "block" and (r["category"] or "uncategorised") == group["category"]
        ]
        assert len(blocks) == group["requirements"]


def test_a_cursor_cannot_leave_its_report_key_or_population(w: World) -> None:
    _, report_id = register(w)
    _, other_id = register(w, OTHER)
    app = w.app()
    _, first = get(app, f"/v1/integrity-reports/{report_id}/cases?key=included&limit=2")
    cursor = first["next_cursor"]
    _, foreign = get(
        w.app(access(OTHER)), f"/v1/integrity-reports/{other_id}/cases?key=included&limit=1"
    )
    for path in (
        f"/v1/integrity-reports/{report_id}/cases?key=result:UNKNOWN&cursor={cursor}",
        f"/v1/integrity-reports/{other_id}/cases?key=included&cursor={cursor}",  # 404 first
        f"/v1/integrity-reports/{report_id}/cases?key=included&cursor={cursor}x",
        f"/v1/integrity-reports/{report_id}/cases?key=included&cursor={'A' * 1100}",
        f"/v1/integrity-reports/{report_id}/cases?key=included&cursor="
        + registry.cases_cursor(report_id, "included", 999),
        f"/v1/integrity-reports/{report_id}/cases?key=nope",
        f"/v1/integrity-reports/{report_id}/cases",
    ):
        status, body = get(app, path)
        assert status in (404, 422), path
        assert OTHER not in json.dumps(body) and "B-0001" not in json.dumps(body)
    # A cursor issued for another tenant's report (or forged with its id) is refused here.
    assert "next_cursor" not in foreign  # one operation: no next page to steal
    forged = registry.cases_cursor(other_id, "included", 1)
    path = f"/v1/integrity-reports/{report_id}/cases?key=included&cursor={forged}"
    assert get(app, path)[0] == 422
    # A rewritten offset only moves inside the same pinned, authorized population.
    status, moved = get(
        app,
        f"/v1/integrity-reports/{report_id}/cases?key=included&limit=2&cursor="
        + registry.cases_cursor(report_id, "included", 5),
    )
    assert status == 200 and all(c["operation_ref"] in [OP, *EMPTY_OPS] for c in moved["cases"])


# ---------------------------------------------------------------------- 4. stability


def test_a_registered_report_does_not_change_with_later_evidence_epochs_or_engines(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, report_id = register(w)
    app = w.app()
    pages = [f"/v1/integrity-reports/{report_id}"] + [
        f"/v1/integrity-reports/{report_id}/cases?key={k}"
        for k in ("included", "result:BREAK", "result:UNKNOWN", "block:any")
    ]
    before = [get(app, p) for p in pages]
    # Later: a new operation evaluated, two scopes reopened, one re-published on new clocks.
    later = clocks(w.corpus, "K3")
    shifted = Clocks(
        later.valid_at,
        later.known_at + timedelta(days=1),
        later.evaluation_clock + timedelta(days=1),
    )
    w.writer.register_scope(TENANT, "SUB-0200", w.corpus.profile.profile_ref)
    assert reevaluate(w.writer, TENANT, "SUB-0200", shifted)[1].outcome == "PUBLISHED"
    w.writer.invalidate(TENANT, EMPTY_OPS[:2], "test: reopened after registration")
    assert reevaluate(w.writer, TENANT, EMPTY_OPS[0], shifted)[1].outcome == "PUBLISHED"
    assert [get(app, p) for p in pages] == before
    # The engine of every publication retired since: still the registered content.
    monkeypatch.setattr("invaria.query.analytics.engine_status", lambda ref: "retired")
    monkeypatch.setattr("invaria.query.service.engine_status", lambda ref: "retired")
    assert [get(w.app(), p) for p in pages] == before
    fresh = w.service().integrity_report(HORIZON)  # a new computation does see the change
    assert fresh.distribution.statuses["engine_retired_requires_reevaluation"] > 0


# -------------------------------------------------------- 5. versions and corruption


def legacy_1_0(service: QueryService) -> IntegrityReport:
    """A format-1.0 report of the same store, as the 1.0 code produced it (no temporal)."""
    state = service._integrity_state()
    as_of = datetime(2026, 10, 20, tzinfo=UTC)
    rules = analytics.RULES_V1
    members, exclusions = analytics.select_members(state, as_of, None, rules)
    sections = analytics.compute_sections(
        members, state.documents, as_of, service._diagnoser, 100, rules
    )
    current = service.integrity_report(HORIZON)
    draft = IntegrityReport(
        schema_version="1.0",
        report_format=rules.report_format,  # type: ignore[arg-type]
        definition=rules.definition,  # type: ignore[arg-type]
        selection_policy=rules.selection_policy,  # type: ignore[arg-type]
        selection_description=rules.selection_description,
        scope=current.scope,
        as_of=as_of,
        store_read=StoreReadView(
            isolation="repeatable_read_read_only",
            read_at=state.read_at,
            closed_knowledge_cut=None,
            as_of_within_closed_cut=False,
        ),
        profile_filter=None,
        max_replays=100,
        diagnostic_engines=current.diagnostic_engines,
        members=members,
        distribution=sections.distribution,
        segments=sections.segments,
        discrepancies=sections.discrepancies,
        blockers=sections.blockers,
        exclusions=exclusions,
        limitations=analytics.limitations_for(members, sections),
        report_sha256="0" * 64,
    )
    return draft.model_copy(update={"report_sha256": analytics.report_sha256(draft)})


def test_a_format_1_0_report_is_read_and_paged_with_its_rules(w: World) -> None:
    service = w.service(write=True)
    old = legacy_1_0(service)
    assert analytics.rules_of(old) is analytics.RULES_V1
    registration = service._register(old, registry.DEFAULT_MAX_DOCUMENT_BYTES)
    app = w.app()
    status, summary = get(app, f"/v1/integrity-reports/{registration.report_id}")
    assert status == 200 and summary["selection_policy"] == "current-publication@1"
    assert summary["temporal"] is None
    assert "status:engine_retired_requires_reevaluation" not in summary["case_keys"]
    cases = walk(app, registration.report_id, "included", 4)
    assert len(cases) == summary["members"]


def admin(w: World, statement: str, *params: Any) -> None:
    with psycopg.connect(w.dsn, autocommit=True) as conn:  # the test database's superuser
        conn.execute(statement, params)


def test_an_unknown_format_is_a_version_limit_and_corruption_is_an_integrity_error(
    w: World,
) -> None:
    report, report_id = register(w)
    data = json.loads(registry.canonical_bytes(report))
    data.update(report_format="invaria-integrity-report@9.0", definition="x@9.0.0")
    data["report_sha256"] = analytics.report_sha256(data)
    future = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    admin(
        w,
        "INSERT INTO invaria.integrity_reports (tenant_id, report_id, report_sha256, "
        "report_format, definition, selection_policy, evidence_horizon, store_read_at, "
        "registered_by, document, document_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, "
        "'x', %s, %s)",
        TENANT,
        "ir-" + data["report_sha256"],
        data["report_sha256"],
        data["report_format"],
        data["definition"],
        data["selection_policy"],
        report.as_of,
        report.store_read.read_at,
        future,
        hashlib.sha256(future).hexdigest(),
    )
    app = w.app()
    status, body = get(app, f"/v1/integrity-reports/ir-{data['report_sha256']}")
    assert (status, body["error"]["code"]) == (501, "UNSUPPORTED_REPORT_VERSION")
    # Corruption: bytes rewritten by an administrator, keeping the table's own CHECK.
    tampered = registry.canonical_bytes(report).replace(b'"max_replays":100', b'"max_replays":99')
    admin(
        w,
        "UPDATE invaria.integrity_reports SET document = %s, document_sha256 = %s "
        "WHERE report_id = %s",
        tampered,
        hashlib.sha256(tampered).hexdigest(),
        report_id,
    )
    for path in (
        f"/v1/integrity-reports/{report_id}",
        f"/v1/integrity-reports/{report_id}/cases?key=included",
    ):
        status, body = get(app, path)
        assert (status, body["error"]["code"]) == (500, "INTEGRITY_ERROR")
        assert set(body) == {"error"} and "max_replays" not in json.dumps(body)


def test_a_missing_reference_serves_nothing_and_names_nothing(w: World) -> None:
    _, report_id = register(w)
    app = w.app()
    victim = EMPTY_OPS[3]
    with psycopg.connect(w.dsn) as conn:  # an incomplete restore, simulated as superuser
        conn.execute("SET session_replication_role = replica")
        found = conn.execute(
            "SELECT evaluation_id FROM invaria.publications WHERE tenant_id = %s "
            "AND operation_ref = %s",
            (TENANT, victim),
        ).fetchone()
        assert found is not None
        conn.execute("DELETE FROM invaria.publications WHERE evaluation_id = %s", (found[0],))
        conn.execute("DELETE FROM invaria.evaluations WHERE evaluation_id = %s", (found[0],))
        conn.commit()
    for path in (
        f"/v1/integrity-reports/{report_id}",
        f"/v1/integrity-reports/{report_id}/cases?key=included&limit=1",  # page without it
    ):
        status, body = get(app, path)
        assert (status, body["error"]["code"]) == (409, "REPORT_INCONSISTENT_WITH_STORE")
        assert victim not in json.dumps(body) and str(found[0]) not in json.dumps(body)


def test_a_pinned_evaluation_with_other_content_fails_its_page(w: World) -> None:
    report, report_id = register(w)
    member = next(m for m in report.members if m.operation_ref == OP)
    assert member.selected is not None
    admin(
        w,
        "UPDATE invaria.evaluations SET document = jsonb_set(document, "
        "'{controls,0,reason}', '\"edited by an administrator\"') WHERE evaluation_id = %s",
        member.selected.evaluation_id,
    )
    status, body = get(w.app(), f"/v1/integrity-reports/{report_id}/cases?key=result:BREAK")
    assert (status, body["error"]["code"]) == (409, "REPORT_INCONSISTENT_WITH_STORE")
    assert "differs from its pinned sha256" in body["error"]["message"]  # not "missing" only
    assert member.selected.evaluation_id not in body["error"]["message"]
    # The summary checks presence only: it is still served (documented limit).
    assert get(w.app(), f"/v1/integrity-reports/{report_id}")[0] == 200


def test_a_report_whose_references_are_missing_is_not_registered(w: World) -> None:
    service = w.service(write=True)
    report = service.integrity_report(HORIZON)
    victim = next(m for m in report.members if m.operation_ref == EMPTY_OPS[2])
    assert victim.selected is not None
    with psycopg.connect(w.dsn) as conn:  # a store that lost a pinned row, as superuser
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "DELETE FROM invaria.publications WHERE evaluation_id = %s",
            (victim.selected.evaluation_id,),
        )
        conn.commit()
    with pytest.raises(QueryError) as error:
        service._register(report, registry.DEFAULT_MAX_DOCUMENT_BYTES)
    assert error.value.code == "REPORT_INCONSISTENT_WITH_STORE" and rows(w) == 0
    assert victim.selected.evaluation_id not in error.value.message
