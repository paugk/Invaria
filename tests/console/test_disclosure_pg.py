"""Controlled disclosure, first output, over PostgreSQL and the console.

Criteria A1-A12, written before the code. Two tenants share one disposable database;
the worker evaluates and publishes as in production; requests go through the WSGI
application in process, one ``ConsoleApp`` per access profile (as one console process per
profile would). The operator's full summary keeps its bytes; the reviewer reads only the
allowlisted aggregates of views issued to it, and nothing else by any existing channel.
"""

from __future__ import annotations

import io
import json
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from invaria.cli import main
from invaria.console.app import ConsoleApp
from invaria.corpus_loader import Corpus, load_corpus
from invaria.mcp_server import TOOL_NAMES
from invaria.persistence.backup import check_invariants, state_digest
from invaria.persistence.dsn import with_database
from invaria.persistence.migrate import migrate
from invaria.persistence.provision import prepare_for_migrator
from invaria.persistence.store import READER_ROLE, PgStore
from invaria.persistence.worker import Clocks, reevaluate
from invaria.query import disclosure, registry
from invaria.query.models import AccessProfile
from invaria.query.service import QueryError, QueryService

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")
CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"
TENANT = "tenant-synthetic-demo"
OTHER = "tenant-other"
OP = "SUB-0001"
EMPTY_OPS = [f"SUB-01{i:02d}" for i in range(1, 8)]
TOKEN = "t" * 40
HOST = "127.0.0.1:8765"
HORIZON = "2026-10-20T00:00:00Z"
AGG = disclosure.AGGREGATE_POLICY
FULL = disclosure.FULL_SUMMARY_POLICY
# The operator's explicit full policies: the four families.
OPERATOR_POLICIES = [
    disclosure.FULL_OPERATIONS_POLICY,
    disclosure.FULL_EVIDENCE_POLICY,
    disclosure.FULL_REPORT_INDEX_POLICY,
    FULL,
]
OPERATOR_SCOPES = [
    "operations:read",
    "reports:read",
    "reports:register",
    "events:read",
    "evidence:read",
    "disclosures:issue",
    "disclosures:revoke",
]


def profile(
    principal: str, scopes: list[str], policies: list[str], tenant: str = TENANT
) -> AccessProfile:
    return AccessProfile.model_validate_json(
        json.dumps(
            {
                "schema_version": "1.0",
                "principal_id": principal,
                "tenant_id": tenant,
                "scopes": scopes,
                "max_items": 50,
                "disclosure_policies": policies,
            }
        )
    )


OPERATOR = profile("operator-1", OPERATOR_SCOPES, OPERATOR_POLICIES)
REVIEWER = profile("reviewer-1", ["disclosures:read"], [AGG])


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


@dataclass
class World:
    dsn: str
    corpus: Corpus
    conns: list[psycopg.Connection[Any]]

    def service(self, access: AccessProfile, *, write: bool = False) -> QueryService:
        conn = psycopg.connect(self.dsn)
        self.conns.append(conn)
        store = PgStore(conn) if write else PgStore(conn, role=READER_ROLE)
        return QueryService(store, access)

    def app(self, access: AccessProfile) -> ConsoleApp:
        return ConsoleApp(self.service(access), token=TOKEN, allowed_hosts={HOST})


@pytest.fixture
def w(database: str, corpus: Corpus) -> Iterator[World]:
    """Tenant A: SUB-0001 published K1-K3 (BREAK) and SUB-0101..0107 published UNKNOWN.
    Tenant B: one operation of its own. Synthetic only."""
    writer = PgStore(psycopg.connect(database))
    ref = corpus.profile.profile_ref
    writer.put_profile(corpus.profile)
    writer.register_scope(TENANT, OP, ref)
    for scenario in ("K1", "K2", "K3"):
        feed(writer, corpus, clocks(corpus, scenario).known_at)
        reevaluate(writer, TENANT, OP, clocks(corpus, scenario))
    for op in EMPTY_OPS:
        writer.register_scope(TENANT, op, ref)
        reevaluate(writer, TENANT, op, clocks(corpus, "K3"))
    writer.register_scope(TENANT, "SUB-0199", ref)  # registered, never evaluated
    writer.register_scope(OTHER, "B-0001", ref)
    reevaluate(writer, OTHER, "B-0001", clocks(corpus, "K2"))
    writer.conn.close()
    world = World(database, corpus, [])
    yield world
    for conn in world.conns:
        conn.close()


def register(w: World, tenant: str = TENANT) -> str:
    access = profile("operator-1", OPERATOR_SCOPES, OPERATOR_POLICIES, tenant)
    _, registration = w.service(access, write=True).register_integrity_report(HORIZON)
    return registration.report_id


def issue(w: World, report_id: str, recipient: str = "reviewer-1", **kw: Any) -> str:
    access = kw.pop("access", OPERATOR)
    return w.service(access, write=True).issue_disclosure(report_id, recipient, AGG).view_id


def call(app: ConsoleApp, path: str, **headers: str) -> tuple[int, dict[str, str], bytes]:
    target, _, query = path.partition("?")
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": target,
        "QUERY_STRING": query,
        "HTTP_HOST": HOST,
        "HTTP_AUTHORIZATION": f"Bearer {TOKEN}",
        "wsgi.errors": io.StringIO(),
        **{f"HTTP_{k.upper()}": v for k, v in headers.items()},
    }
    seen: list[tuple[str, list[tuple[str, str]]]] = []
    body = b"".join(app(environ, lambda status, hs: seen.append((status, hs))))
    status, hs = seen[0]
    return int(status.split()[0]), dict(hs), body


def error_of(body: bytes) -> tuple[str, str]:
    e = json.loads(body)["error"]
    return e["code"], e["message"]


def original_secrets(w: World, report_id: str) -> list[str]:
    """Everything of the original a reviewer must never see."""
    with psycopg.connect(w.dsn) as conn:
        sha, doc_sha, registered = conn.execute(
            "SELECT report_sha256, document_sha256, registered_at FROM invaria.integrity_reports "
            "WHERE report_id = %s",
            (report_id,),
        ).fetchone()  # type: ignore[misc]
        evaluations = [r[0] for r in conn.execute("SELECT evaluation_id FROM invaria.evaluations")]
    return [
        report_id,
        sha,
        doc_sha,
        registered.astimezone(UTC).isoformat()[:19],
        TENANT,
        "operator-1",
        OP,
        *EMPTY_OPS,
        *evaluations,
        w.corpus.profile.profile_ref,
        "invaria-engine",
        "cases_key",
        "/reports/",
        "/v1/integrity-reports",
        "/operations",
        "/evaluations",
    ]


def assert_no_leak(secrets: list[str], *responses: tuple[int, dict[str, str], bytes]) -> None:
    for _, headers, body in responses:
        text = body.decode("utf-8") + json.dumps(headers)
        for secret in secrets:
            assert secret not in text, secret
        assert not re.search(r"[0-9a-f]{64}", text)


def digest(w: World, *, skip: tuple[str, ...] = ()) -> dict[str, Any]:
    with psycopg.connect(w.dsn) as conn, conn.transaction():
        tables, sequences = state_digest(conn)
    return {"tables": {k: v for k, v in tables.items() if k not in skip}, "seq": sequences}


# -------------------------------------------------- A2, A3, A10: the reviewer's view


def test_the_reviewer_reads_exactly_the_allowlist_with_the_original_semantics(w: World) -> None:
    report_id = register(w)
    full = w.service(OPERATOR).get_integrity_report_summary(report_id)
    issued = w.service(OPERATOR, write=True).issue_disclosure(report_id, "reviewer-1", AGG)
    assert re.fullmatch(r"dv-[0-9a-f]{32}", issued.view_id)
    assert issued.view_id[3:] not in report_id  # random, independent of the report hash
    reviewer = w.app(REVIEWER)
    status, headers, body = call(reviewer, f"/v1/disclosures/{issued.view_id}")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    view = json.loads(body)
    assert disclosure.allowlist_problems(view, disclosure.AGGREGATE_ALLOWLIST) == []
    assert view == issued.preview.model_dump(mode="json")  # what the operator saw at issue
    d = full.distribution
    assert view["included_operations"] == d.included == 9
    assert view["outcomes"] == {
        "match_count": d.match_count,
        "break_count": d.break_count,
        "unknown_count": d.unknown_count,
        "denominator": d.admitted,
        "denominator_definition": disclosure.ADMITTED_DEFINITION,
    }
    assert (d.match_count, d.break_count, d.unknown_count) == (0, 1, 7)
    outside = view["outside_outcomes"]
    assert outside["operations"] == d.included - d.admitted == 1
    assert outside["operations"] == sum(v for k, v in d.statuses.items() if k != "admitted")
    assert view["withheld"] == list(disclosure.WITHHELD)
    assert view["statements"][0].startswith("This view contains selected information")
    html = call(reviewer, f"/disclosures/{issued.view_id}")
    assert html[0] == 200
    page = html[2].decode("utf-8")
    assert "does not allow reproducing the evaluation" in page and "not a result" in page
    assert 'href="/reports' not in page and 'href="/events' not in page
    assert 'href="/"' not in page  # no link to the operations page either
    assert_no_leak(
        original_secrets(w, report_id),
        (status, headers, body),
        html,
        call(reviewer, "/v1/disclosures"),
    )
    listing = json.loads(call(reviewer, "/v1/disclosures")[2])
    assert listing["items"] == [{"view_id": issued.view_id, "policy_ref": AGG}]


def test_withheld_categories_are_the_same_for_different_reports(w: World) -> None:
    first = register(w)
    a = json.loads(call(w.app(REVIEWER), f"/v1/disclosures/{issue(w, first)}")[2])
    with psycopg.connect(w.dsn) as conn:  # another population: one more published UNKNOWN
        store = PgStore(conn)
        store.register_scope(TENANT, "SUB-0150", w.corpus.profile.profile_ref)
        reevaluate(store, TENANT, "SUB-0150", clocks(w.corpus, "K3"))
    access = profile("operator-1", OPERATOR_SCOPES, OPERATOR_POLICIES)
    _, again = w.service(access, write=True).register_integrity_report("2026-10-21T00:00:00Z")
    assert again.report_id != first
    b = json.loads(call(w.app(REVIEWER), f"/v1/disclosures/{issue(w, again.report_id)}")[2])
    assert a["included_operations"] + 1 == b["included_operations"]
    fixed = ("withheld", "withheld_meaning", "statements", "counting_definition", "policy")
    assert {k: a[k] for k in fixed} == {k: b[k] for k in fixed}


# ---------------------------------------------- A7: isolation and revocation, one 404


def test_absent_foreign_and_revoked_views_are_one_404(w: World) -> None:
    report_id = register(w)
    view_id = issue(w, report_id)
    other_report = register(w, OTHER)
    other_access = profile("operator-1", OPERATOR_SCOPES, OPERATOR_POLICIES, OTHER)
    foreign_tenant_view = issue(w, other_report, access=other_access)
    second = profile("reviewer-2", ["disclosures:read"], [AGG])
    same_name_other_tenant = profile("reviewer-1", ["disclosures:read"], [AGG], OTHER)
    answers = {
        "absent": call(w.app(REVIEWER), "/v1/disclosures/dv-" + "0" * 32),
        "another recipient": call(w.app(second), f"/v1/disclosures/{view_id}"),
        "another tenant": call(w.app(same_name_other_tenant), f"/v1/disclosures/{view_id}"),
        "foreign view": call(w.app(REVIEWER), f"/v1/disclosures/{foreign_tenant_view}"),
    }
    assert call(w.app(REVIEWER), f"/v1/disclosures/{view_id}")[0] == 200
    # An operator of another tenant cannot revoke it: same NOT_FOUND, and it stays readable.
    with pytest.raises(QueryError) as caught:
        w.service(other_access, write=True).revoke_disclosure(view_id)
    assert caught.value.code == "NOT_FOUND"
    assert call(w.app(REVIEWER), f"/v1/disclosures/{view_id}")[0] == 200
    revoked = w.service(OPERATOR, write=True).revoke_disclosure(view_id)
    assert revoked.revoked_now
    assert not w.service(OPERATOR, write=True).revoke_disclosure(view_id).revoked_now
    answers["revoked"] = call(w.app(REVIEWER), f"/v1/disclosures/{view_id}")
    assert {(s, error_of(b)) for s, _, b in answers.values()} == {
        (404, ("NOT_FOUND", "disclosure not found"))
    }
    assert call(w.app(REVIEWER), f"/disclosures/{view_id}")[0] == 404
    assert json.loads(call(w.app(REVIEWER), "/v1/disclosures")[2])["items"] == []
    assert call(w.app(REVIEWER), "/v1/disclosures/not-an-id")[0] == 422
    assert_no_leak(original_secrets(w, report_id), *answers.values())


def test_a_revocation_after_the_consistency_point_does_not_withdraw_that_response(
    w: World,
) -> None:
    view_id = issue(w, register(w))
    service = w.service(REVIEWER)
    read = service.store.disclosure_for

    def read_then_revoke(*args: Any) -> Any:
        found = read(*args)  # the consistency point: issuance, revocation and report
        w.service(OPERATOR, write=True).revoke_disclosure(view_id)
        return found

    service.store.disclosure_for = read_then_revoke  # type: ignore[method-assign, assignment]
    assert service.get_disclosure(view_id).view_id == view_id  # already being built
    service.store.disclosure_for = read  # type: ignore[method-assign]
    with pytest.raises(QueryError) as caught:
        service.get_disclosure(view_id)  # the next read sees the revocation
    assert caught.value.code == "NOT_FOUND"


# ------------------------------------------------------ A5, A9: policies, fail closed


def test_unassigned_unknown_and_missing_policies_are_refused(w: World) -> None:
    report_id = register(w)
    view_id = issue(w, report_id)
    secrets = original_secrets(w, report_id)
    for policies in ([], [FULL], ["retired-policy@1"], ["aggregate-outcome-counts@2"]):
        reviewer = profile("reviewer-1", ["disclosures:read"], policies)
        answer = call(w.app(reviewer), f"/v1/disclosures/{view_id}")
        assert (answer[0], error_of(answer[2])[0]) == (403, "DISCLOSURE_POLICY_UNAVAILABLE")
        assert_no_leak(secrets, answer)
    # The operator's full summary: same bytes as the registry summary with the explicit full
    # policy; refused without it (also in HTML), on that route only.
    full_bytes = call(w.app(OPERATOR), f"/v1/integrity-reports/{report_id}")
    with psycopg.connect(w.dsn) as conn:
        record = PgStore(conn, role=READER_ROLE).load_integrity_report(TENANT, report_id)
    assert record is not None and record.registered_at is not None
    expected = registry.summary(
        registry.verify(record, TENANT), record.registered_at.astimezone(UTC)
    )
    assert full_bytes[0] == 200 and full_bytes[2] == expected.model_dump_json().encode("utf-8")
    for policies in ([], ["retired-policy@1"], [AGG]):
        # Every other family assigned: only the summary is refused (one decision per
        # output, the listing has its own policy).
        others = [disclosure.FULL_OPERATIONS_POLICY, disclosure.FULL_REPORT_INDEX_POLICY]
        bare = profile("operator-1", OPERATOR_SCOPES, [*policies, *others])
        for path in (f"/v1/integrity-reports/{report_id}", f"/reports/{report_id}"):
            assert call(w.app(bare), path)[0] == 403, (policies, path)
        assert call(w.app(bare), "/v1/integrity-reports")[0] == 200  # only that output
    # An issuance whose policy this code does not know is never served, and the restore
    # verification reports it.
    with psycopg.connect(w.dsn, autocommit=True) as admin:
        admin.execute(
            "INSERT INTO invaria.disclosure_issuances (tenant_id, recipient_principal, "
            "policy_ref, view_format, report_id, issued_by) VALUES (%s, 'reviewer-1', "
            "'retired-policy@1', 'invaria-disclosure-view@1', %s, 'admin')",
            (TENANT, report_id),
        )
        problems = check_invariants(admin)
    assert any("retired-policy@1" in p for p in problems)
    # The listing needs an assigned restricted policy of the catalogue.
    stale = json.loads(
        call(
            w.app(profile("reviewer-1", ["disclosures:read"], ["retired-policy@1", AGG])),
            "/v1/disclosures",
        )[2]
    )["items"]
    only_unknown = profile("reviewer-1", ["disclosures:read"], ["retired-policy@1"])
    listing = call(w.app(only_unknown), "/v1/disclosures")
    assert (listing[0], error_of(listing[2])[0]) == (403, "DISCLOSURE_POLICY_UNAVAILABLE")
    retired = next(i["view_id"] for i in stale if i["policy_ref"] == "retired-policy@1")
    answer = call(
        w.app(profile("reviewer-1", ["disclosures:read"], ["retired-policy@1"])),
        f"/v1/disclosures/{retired}",
    )
    assert (answer[0], error_of(answer[2])[0]) == (403, "DISCLOSURE_POLICY_UNAVAILABLE")


def test_issuing_needs_its_own_scope_plus_reading_and_an_issuable_policy(w: World) -> None:
    report_id = register(w)
    cases = (
        ([s for s in OPERATOR_SCOPES if s != "disclosures:issue"], "disclosures:issue"),
        ([s for s in OPERATOR_SCOPES if s != "reports:read"], "reports:read"),
    )
    for scopes, missing in cases:
        service = w.service(profile("operator-1", scopes, OPERATOR_POLICIES), write=True)
        with pytest.raises(QueryError) as caught:
            service.issue_disclosure(report_id, "reviewer-1", AGG)
        assert caught.value.code == "FORBIDDEN" and missing in caught.value.message
    no_revoke = profile(
        "operator-1", [s for s in OPERATOR_SCOPES if s != "disclosures:revoke"], OPERATOR_POLICIES
    )
    view_id = issue(w, report_id)
    with pytest.raises(QueryError) as caught:
        w.service(no_revoke, write=True).revoke_disclosure(view_id)
    assert caught.value.code == "FORBIDDEN"
    for policy in (FULL, "unknown-policy@1"):
        with pytest.raises(QueryError) as caught:
            w.service(OPERATOR, write=True).issue_disclosure(report_id, "reviewer-1", policy)
        assert caught.value.code == "VALIDATION_ERROR"
    with pytest.raises(QueryError) as caught:  # another tenant's report: never confirmed
        w.service(OPERATOR, write=True).issue_disclosure(register(w, OTHER), "reviewer-1", AGG)
    assert caught.value.code == "NOT_FOUND"
    reviewer = w.service(REVIEWER, write=True)
    for attempt in (
        lambda: reviewer.issue_disclosure(report_id, "reviewer-1", AGG),
        lambda: reviewer.revoke_disclosure(view_id),
    ):
        with pytest.raises(QueryError) as caught:
            attempt()
        assert caught.value.code == "FORBIDDEN"
    with psycopg.connect(w.dsn) as conn:
        assert conn.execute("SELECT count(*) FROM invaria.disclosure_issuances").fetchone() == (1,)


# ---------------------------------------------- A6: no other way to the original


def test_disclosures_read_grants_no_other_channel(w: World) -> None:
    report_id = register(w)
    view_id = issue(w, report_id)
    with psycopg.connect(w.dsn) as conn:
        evaluation_id = conn.execute(
            "SELECT min(evaluation_id) FROM invaria.evaluations"
        ).fetchone()[0]  # type: ignore[index]
        evidence_id = conn.execute(
            "SELECT min(observation_id) FROM invaria.observations"
        ).fetchone()[0]  # type: ignore[index]
    app = w.app(REVIEWER)
    secrets = original_secrets(w, report_id)
    refused = []
    for path in (
        "/v1/operations",
        f"/v1/operations/{OP}",
        f"/v1/operations/{OP}/timeline",
        f"/v1/operations/{OP}/coverage",
        f"/v1/operations/{OP}/conclusion?valid_at=2026-10-01T00:00:00Z&known_at=2026-10-09T00:00:00Z",
        f"/v1/evaluations/{evaluation_id}",
        f"/v1/evaluations/{evaluation_id}/controls/c/discrepancy",
        f"/v1/evaluation-comparisons?left={evaluation_id}&right={evaluation_id}",
        f"/v1/evidence/{evidence_id}",
        "/v1/events?start=beginning",
        "/v1/integrity-reports",
        f"/v1/integrity-reports/{report_id}",
        f"/v1/integrity-reports/{report_id}/cases?key=included",
        "/",
        f"/operations/{OP}",
        f"/evaluations/{evaluation_id}",
        "/reports",
        f"/reports/{report_id}",
        f"/reports/{report_id}/cases?key=included",
        "/events?start=beginning",
    ):
        answer = call(app, path)
        assert answer[0] == 403, path
        refused.append(answer)
    assert_no_leak(secrets, *refused)
    page = refused[-1][2].decode("utf-8")  # an HTML refusal: navigation to disclosures only
    assert 'href="/disclosures"' in page and 'href="/reports"' not in page
    # The MCP tools delegate to these use cases: all refused for this profile.
    service = w.service(REVIEWER)
    args = {
        "trace_operation": (OP,),
        "get_conclusion_as_known_at": (OP, "2026-10-01T00:00:00Z", "2026-10-09T00:00:00Z"),
        "get_missing_evidence": (evaluation_id, "c"),
        "explain_discrepancy": (evaluation_id, "c"),
        "explain_conclusion_change": (evaluation_id, evaluation_id),
        "get_evidence": (evidence_id,),
        "get_coverage": (OP,),
    }
    assert set(args) == set(TOOL_NAMES)
    for tool, values in args.items():
        with pytest.raises(QueryError) as caught:
            getattr(service, tool)(*values)
        assert caught.value.code == "FORBIDDEN", tool
    # The view itself is still served: the refusals are of the other channels.
    assert call(app, f"/v1/disclosures/{view_id}")[0] == 200


def test_the_client_cannot_widen_what_it_receives(w: World) -> None:
    report_id = register(w)
    view_id = issue(w, report_id)
    app = w.app(REVIEWER)
    plain = call(app, f"/v1/disclosures/{view_id}")[2]
    for query in ("policy=full-report-summary@1", "principal=operator-1", "scopes=reports:read"):
        assert call(app, f"/v1/disclosures/{view_id}?{query}")[0] == 422
    spoofed = call(
        app,
        f"/v1/disclosures/{view_id}",
        x_invaria_principal="operator-1",
        x_invaria_scopes="reports:read",
        x_invaria_policy=FULL,
    )
    assert spoofed[0] == 200 and spoofed[2] == plain  # headers are ignored
    for path in (
        f"/v1/disclosures/{view_id}/report",
        f"/v1/disclosures/{view_id}/..",
        f"/v1/disclosures/{report_id}",
        f"/v1/disclosures/{view_id.upper()}",
    ):
        assert call(app, path)[0] in (404, 422), path
    # The operator, who can read the report, cannot read the view as the recipient.
    assert call(w.app(OPERATOR), f"/v1/disclosures/{view_id}")[0] == 403


# ---------------------------------------------- A1, A8: original intact, no writes


def test_reads_write_nothing_and_the_original_stays_intact(w: World) -> None:
    report_id = register(w)
    untouched = digest(w, skip=("disclosure_issuances", "disclosure_revocations"))
    view_id = issue(w, report_id)
    issue(w, report_id, recipient="reviewer-2")
    before_reads = digest(w)
    app = w.app(REVIEWER)
    for path in (
        f"/v1/disclosures/{view_id}",
        "/v1/disclosures",
        f"/disclosures/{view_id}",
        "/disclosures",
        f"/v1/integrity-reports/{report_id}",
    ):
        call(app, path)
    w.service(REVIEWER).get_disclosure(view_id)
    assert digest(w) == before_reads  # reading writes nothing, not even an issuance
    w.service(OPERATOR, write=True).revoke_disclosure(view_id)
    after = digest(w, skip=("disclosure_issuances", "disclosure_revocations"))
    # Report bytes and hashes, evaluations, publications, outbox and domain events: equal.
    assert after == untouched
    with psycopg.connect(w.dsn) as conn:
        rows = conn.execute(
            "SELECT recipient_principal, policy_ref, view_format, report_id, issued_by "
            "FROM invaria.disclosure_issuances ORDER BY recipient_principal"
        ).fetchall()
        revocations = conn.execute(
            "SELECT view_id, tenant_id, revoked_by FROM invaria.disclosure_revocations"
        ).fetchall()
    assert rows == [
        ("reviewer-1", AGG, disclosure.VIEW_FORMAT, report_id, "operator-1"),
        ("reviewer-2", AGG, disclosure.VIEW_FORMAT, report_id, "operator-1"),
    ]
    assert revocations == [(view_id, TENANT, "operator-1")]


def test_a_corrupted_original_is_refused_without_naming_it(w: World) -> None:
    report_id = register(w)
    view_id = issue(w, report_id)
    with psycopg.connect(w.dsn, autocommit=True) as admin:
        admin.execute(
            "UPDATE invaria.integrity_reports SET document = document || '\\x20'::bytea, "
            "document_sha256 = encode(sha256(document || '\\x20'::bytea), 'hex')"
        )
    answer = call(w.app(REVIEWER), f"/v1/disclosures/{view_id}")
    assert (answer[0], error_of(answer[2])) == (
        500,
        ("INTEGRITY_ERROR", "the disclosed report cannot be served"),
    )
    assert_no_leak(original_secrets(w, report_id), answer)


# ------------------------------------------------------------- CLI of the operator


def test_cli_issues_and_revokes(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report_id = register(w)
    operator = tmp_path / "operator.json"
    operator.write_text(OPERATOR.model_dump_json(), "utf-8")
    monkeypatch.setenv("INVARIA_DATABASE_URL", w.dsn)
    args = ["disclosure", "issue", "--access-profile", str(operator), "--report", report_id]
    assert main([*args, "--recipient", "reviewer-1", "--policy", AGG, "--json"]) == 0
    issued = json.loads(capsys.readouterr().out)
    view_id = issued["view_id"]
    assert issued["preview"] == json.loads(call(w.app(REVIEWER), f"/v1/disclosures/{view_id}")[2])
    assert main([*args, "--recipient", "reviewer-1", "--policy", FULL]) == 2
    assert "not an issuable policy" in capsys.readouterr().err
    revoke = ["disclosure", "revoke", "--access-profile", str(operator), "--view", view_id]
    assert main(revoke) == 0
    assert capsys.readouterr().out.startswith(f"revoked: {view_id}")
    assert main(revoke) == 0
    assert capsys.readouterr().out.startswith(f"already revoked: {view_id}")
    assert call(w.app(REVIEWER), f"/v1/disclosures/{view_id}")[0] == 404
