"""Redemption through PostgreSQL: profile stored, epochs and worker, read-only queries and
the console. Opt-in via INVARIA_TEST_DATABASE_URL."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import psycopg
import pytest

from invaria.console.app import ConsoleApp
from invaria.corpus_loader import Corpus, load_corpus
from invaria.persistence.dsn import with_database
from invaria.persistence.migrate import migrate
from invaria.persistence.provision import prepare_for_migrator
from invaria.persistence.store import READER_ROLE, PgStore
from invaria.persistence.worker import Clocks, reevaluate
from invaria.query.models import AccessProfile
from invaria.query.service import QueryService

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")
CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/redemption-synthetic"
TENANT = "tenant-synthetic-demo"
OP = "RED-0001"
S1, S2, S3, S4 = (
    datetime.fromisoformat(t)
    for t in (
        "2026-10-06T09:00:00Z",
        "2026-10-06T16:00:00Z",
        "2026-10-07T12:00:00Z",
        "2026-10-08T10:00:00Z",
    )
)


def _dsn_for(database: str) -> str:
    assert ADMIN_DSN is not None
    return with_database(ADMIN_DSN, database)


@pytest.fixture
def database() -> Iterator[str]:
    if not ADMIN_DSN:
        pytest.skip("set INVARIA_TEST_DATABASE_URL to an admin PostgreSQL URL to run DB tests")
    name = f"invaria_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    try:
        with psycopg.connect(_dsn_for(name)) as conn:
            prepare_for_migrator(conn)
            migrate(conn)
        yield _dsn_for(name)
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


def feed(store: PgStore, corpus: Corpus, timeline: str, upto: datetime) -> None:
    store.append_identity_links(
        TENANT, [k for k in corpus.identity_links.values() if k.recorded_at <= upto]
    )
    store.append_coverage(
        c
        for c in corpus.coverage.values()
        if c.recorded_at <= upto and re.fullmatch(r"cov-[a-z]+-s\d", c.coverage_id)
    )
    store.append_observations(
        o for o in corpus.journals[timeline].values() if o.recorded_at <= upto
    )


def run(store: PgStore, corpus: Corpus, timeline: str, stages: list[datetime]) -> list[str]:
    store.put_profile(corpus.profile)
    store.register_scope(TENANT, OP, corpus.profile.profile_ref)
    ids = []
    for at in stages:
        feed(store, corpus, timeline, at)
        prepared, publication = reevaluate(store, TENANT, OP, Clocks(at, at, at))
        assert publication.outcome == "PUBLISHED"
        ids.append(prepared.evaluation.result.evaluation_id)
    return ids


def access() -> AccessProfile:
    return AccessProfile.model_validate(
        {
            "schema_version": "1.0",
            "principal_id": "analyst-1",
            "tenant_id": TENANT,
            "scopes": ["operations:read", "evidence:read"],
            "max_items": 50,
        }
    )


def test_pending_missed_then_late_through_the_database(database: str, corpus: Corpus) -> None:
    writer = PgStore(psycopg.connect(database))
    reader = PgStore(psycopg.connect(database), role=READER_ROLE)
    try:
        pending, missed, late = run(writer, corpus, "unpaid-then-late", [S1, S3, S4])
        service = QueryService(reader, access())
        trace = service.trace_operation(OP)
        assert [h.result for h in trace.history] == ["UNKNOWN", "BREAK", "BREAK"]
        assert trace.current is not None and trace.current.evaluation_id == late

        deadline = "redemption.payment_deadline"
        view = service.get_conclusion_as_known_at(OP, S3.isoformat(), S3.isoformat())
        (c,) = [c for c in view.controls if c.control_id == deadline]
        assert (view.evaluation_id, c.reason_code) == (missed, "PAYMENT_MISSED")
        assert view.versions.engine_ref == "invaria-redemption-engine@0.11.0"

        not_due = service.get_missing_evidence(pending, deadline)
        assert not_due.reason_code == "PAYMENT_NOT_DUE" and "not due yet" in not_due.note

        change = service.explain_conclusion_change(missed, late)
        assert change.replay_consistent and change.added_evidence == ["obs-PY-LATE"]
        changed = {c.control_id for c in change.changed_controls}
        assert changed == {"redemption.cash_vs_expected", deadline}

        short = service.explain_discrepancy(late, "redemption.cash_vs_expected")
        assert short.replay_consistent and short.left is not None
        assert short.left.atoms == "1000000"
    finally:
        writer.conn.close()
        reader.conn.close()


def test_cancelled_request_in_the_query_layer_and_console(database: str, corpus: Corpus) -> None:
    writer = PgStore(psycopg.connect(database))
    reader = PgStore(psycopg.connect(database), role=READER_ROLE)
    try:
        (evaluation_id,) = run(writer, corpus, "cancelled", [S2])
        service = QueryService(reader, access())
        conclusion = service.get_evaluation(evaluation_id)
        assert (conclusion.result, conclusion.operation_state) == ("MATCH", "cancelled")
        token = "c" * 40
        app = ConsoleApp(service, token=token, allowed_hosts={"127.0.0.1:1"})
        captured: dict[str, str] = {}

        def start(status: str, headers: list[tuple[str, str]]) -> None:
            captured["status"] = status

        body = b"".join(
            app(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": f"/evaluations/{evaluation_id}",
                    "QUERY_STRING": "",
                    "HTTP_HOST": "127.0.0.1:1",
                    "HTTP_AUTHORIZATION": f"Bearer {token}",
                    "wsgi.errors": None,
                },
                start,
            )
        ).decode()
        assert captured["status"].startswith("200")
        assert "<dt>Lifecycle</dt>" in body and ">cancelled</span>" in body
        assert "EXTINGUISHED_BY_CANCELLATION" in body
    finally:
        writer.conn.close()
        reader.conn.close()


def test_a_new_price_reopens_the_epoch(database: str, corpus: Corpus) -> None:
    """Prices carry no operation_ref; the dependency predicate must still match them."""
    writer = PgStore(psycopg.connect(database))
    try:
        run(writer, corpus, "main", [S2])
        before = writer.scope_head(TENANT, OP).epoch
        price = corpus.journals["main"]["obs-PR1"].model_copy(
            update={
                "observation_id": "obs-PR-NEW",
                "recorded_at": datetime.fromisoformat("2026-10-06T17:00:00Z"),
            }
        )
        writer.append_observations([price])
        assert writer.scope_head(TENANT, OP).epoch == before + 1
        current = writer.current_evaluation(TENANT, OP)
        assert current is not None and current.currency == "stale"
    finally:
        writer.conn.close()


S0 = datetime.fromisoformat("2026-10-05T11:30:00Z")


@pytest.mark.parametrize(
    ("timeline", "stages", "results", "states"),
    [
        ("cancel-known-late", [S3, S4], ["BREAK", "MATCH"], [None, "cancelled"]),
        ("cancel-retracted", [S0, S2], ["MATCH", "MATCH"], ["cancelled", None]),
    ],
    ids=["cancellation-known-late", "cancellation-retracted"],
)
def test_corrections_change_the_present_and_keep_the_past(
    database: str,
    corpus: Corpus,
    timeline: str,
    stages: list[datetime],
    results: list[str],
    states: list[str | None],
) -> None:
    writer = PgStore(psycopg.connect(database))
    reader = PgStore(psycopg.connect(database), role=READER_ROLE)
    try:
        before, after = run(writer, corpus, timeline, stages)
        service = QueryService(reader, access())
        trace = service.trace_operation(OP)
        assert [h.result for h in trace.history] == results
        assert [h.currency for h in trace.history] == ["superseded", "current"]
        past = service.get_conclusion_as_known_at(OP, stages[0].isoformat(), stages[0].isoformat())
        assert (past.evaluation_id, past.result, past.operation_state) == (
            before,
            results[0],
            states[0],
        )
        now = service.get_evaluation(after)
        assert (now.result, now.operation_state) == (results[1], states[1])
        change = service.explain_conclusion_change(before, after)
        assert change.replay_consistent
    finally:
        writer.conn.close()
        reader.conn.close()
