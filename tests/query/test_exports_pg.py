"""Controlled export of a computed integrity report against a real PostgreSQL.

Expectations, written before the code: permissions before anything, the exact bytes of
before, an append-only record without paths, the failure model and the CLI's messages.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest

from invaria.cli import main
from invaria.contracts.base import parse_contract
from invaria.corpus_loader import Corpus, load_corpus
from invaria.export_writer import ExportTarget
from invaria.persistence.dsn import with_database
from invaria.persistence.migrate import migrate
from invaria.persistence.provision import prepare_for_migrator
from invaria.persistence.store import EXPORTER_ROLE, READER_ROLE, PgStore
from invaria.persistence.worker import Clocks, reevaluate
from invaria.query import disclosure
from invaria.query.models import AccessProfile, IntegrityReport
from invaria.query.service import ExportError, QueryError, QueryService, report_file_bytes

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")
CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"
TENANT, OTHER, OP = "tenant-synthetic-demo", "tenant-other", "SUB-0001"
HORIZON = "2026-10-20T00:00:00Z"
OPS, EXPORT = disclosure.FULL_OPERATIONS_POLICY, disclosure.EXPORT_REPORT_FILE_POLICY


def profile(
    scopes: list[str] | None = None, policies: list[str] | None = None, tenant: str = TENANT
) -> AccessProfile:
    return AccessProfile.model_validate_json(
        json.dumps(
            {
                "schema_version": "1.0",
                "principal_id": "analyst-1",
                "tenant_id": tenant,
                "scopes": ["operations:read", "exports:create"] if scopes is None else scopes,
                "max_items": 50,
                "disclosure_policies": [OPS, EXPORT] if policies is None else policies,
            }
        )
    )


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
    exporter: PgStore
    reader: PgStore

    def service(self, access: AccessProfile) -> QueryService:
        return QueryService(self.exporter, access)


@pytest.fixture
def w(database: str) -> Iterator[World]:
    """Tenant A: SUB-0001 published K1 then K2. Tenant B: B-0001 published K2."""
    corpus = load_corpus(CORPUS_DIR)
    writer = PgStore(psycopg.connect(database))
    writer.put_profile(corpus.profile)
    coverage = {c for s in ("K1", "K2") for c in corpus.scenarios[s].snapshot.coverage_ids}
    writer.register_scope(TENANT, OP, corpus.profile.profile_ref)
    for scenario in ("K1", "K2"):
        upto = clocks(corpus, scenario).known_at
        writer.append_identity_links(
            TENANT, [k for k in corpus.identity_links.values() if k.recorded_at <= upto]
        )
        writer.append_coverage(
            c
            for c in corpus.coverage.values()
            if c.coverage_id in coverage and c.recorded_at <= upto
        )
        writer.append_observations(
            o for o in corpus.journals["main"].values() if o.recorded_at <= upto
        )
        assert reevaluate(writer, TENANT, OP, clocks(corpus, scenario))[1].outcome == "PUBLISHED"
    writer.register_scope(OTHER, "B-0001", corpus.profile.profile_ref)
    assert reevaluate(writer, OTHER, "B-0001", clocks(corpus, "K2"))[1].outcome == "PUBLISHED"
    writer.conn.close()
    exporter = PgStore(psycopg.connect(database), role=EXPORTER_ROLE)
    reader = PgStore(psycopg.connect(database), role=READER_ROLE)
    yield World(database, exporter, reader)
    exporter.conn.close()
    reader.conn.close()


def business_digest(dsn: str) -> dict[str, str]:
    """md5 of every table except the export record: nothing else may change."""
    with psycopg.connect(dsn) as conn:
        tables = [
            str(r[0])
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'invaria' "
                "AND tablename NOT IN ('export_attempts', 'export_outcomes') ORDER BY 1"
            )
        ]
        return {
            t: str(
                conn.execute(
                    f"SELECT md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
                    f"FROM invaria.{t} x"
                ).fetchone()[0]  # type: ignore[index]
            )
            for t in tables
        }


def raw_record_text(dsn: str) -> str:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT row_to_json(a)::text FROM invaria.export_attempts a UNION ALL "
            "SELECT row_to_json(o)::text FROM invaria.export_outcomes o"
        ).fetchall()
    return "\n".join(str(r[0]) for r in rows)


def files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


# ------------------------------------------------------------------ permissions


@pytest.mark.parametrize(
    ("scopes", "policies", "code"),
    [
        (["operations:read"], [OPS, EXPORT], "FORBIDDEN"),  # query allowed, export not
        (["operations:read", "exports:create"], [OPS], "DISCLOSURE_POLICY_UNAVAILABLE"),
        (["exports:create"], [OPS, EXPORT], "FORBIDDEN"),
        (["operations:read", "exports:create"], [EXPORT], "DISCLOSURE_POLICY_UNAVAILABLE"),
        (
            ["operations:read", "exports:create"],
            [EXPORT, disclosure.AGGREGATE_POLICY],
            "DISCLOSURE_POLICY_UNAVAILABLE",
        ),
        (
            ["operations:read", "exports:create"],
            ["export-integrity-report-file@2", OPS],
            "DISCLOSURE_POLICY_UNAVAILABLE",
        ),
    ],
)
def test_every_permission_is_needed_before_anything_happens(
    w: World, tmp_path: Path, scopes: list[str], policies: list[str], code: str
) -> None:
    before = business_digest(w.dsn)
    target = ExportTarget(tmp_path / "report.json")
    with pytest.raises(QueryError) as caught:
        w.service(profile(scopes, policies)).export_integrity_report_file(HORIZON, None, 10, target)
    assert caught.value.code == code
    assert target.dir_fd is None  # the destination was never opened
    assert files(tmp_path) == []
    assert w.reader.export_records(TENANT) == []
    assert business_digest(w.dsn) == before


def test_query_permission_alone_still_reads_the_report(w: World) -> None:
    report = QueryService(w.reader, profile(["operations:read"], [OPS])).integrity_report(HORIZON)
    assert report.scope.tenant_id == TENANT


# ------------------------------------------------------------- the export itself


def test_an_authorized_export_writes_the_same_bytes_and_records_no_path(
    w: World, tmp_path: Path
) -> None:
    before = business_digest(w.dsn)
    out = tmp_path / "secret-name-7f3a" / "report.json"
    out.parent.mkdir()
    report, view = w.service(profile()).export_integrity_report_file(
        HORIZON, None, 10, ExportTarget(out)
    )
    content = out.read_bytes()
    # The format of before, byte for byte; no envelope or export metadata.
    assert content == (json.dumps(report.model_dump(mode="json"), indent=2) + "\n").encode()
    assert content == report_file_bytes(report)
    assert parse_contract(IntegrityReport, content.decode()) == report
    assert view.content_sha256 == hashlib.sha256(content).hexdigest()
    assert (view.content_bytes, view.report_sha256) == (len(content), report.report_sha256)
    assert view.policy_refs == [EXPORT, OPS]
    assert view.completed_at >= view.attempted_at
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert files(out.parent) == ["report.json"]
    (record,) = w.reader.export_records(TENANT)
    assert record["export_id"] == view.export_id and record["outcome"] == "completed"
    assert record["principal_id"] == "analyst-1" and record["failure"] is None
    assert (record["content_sha256"], record["content_bytes"]) == (
        view.content_sha256,
        len(content),
    )
    raw = raw_record_text(w.dsn)
    assert "secret-name-7f3a" not in raw and str(tmp_path) not in raw and "report.json" not in raw
    assert business_digest(w.dsn) == before  # evaluations and results unchanged


def test_tenants_are_isolated_in_the_content_and_the_record(w: World, tmp_path: Path) -> None:
    report, _ = w.service(profile(tenant=OTHER)).export_integrity_report_file(
        HORIZON, None, 10, ExportTarget(tmp_path / "b.json")
    )
    assert report.scope.tenant_id == OTHER
    assert {m.operation_ref for m in report.members} == {"B-0001"}
    assert TENANT not in (tmp_path / "b.json").read_text()
    assert w.reader.export_records(TENANT) == []
    assert len(w.reader.export_records(OTHER)) == 1


def test_the_exported_file_is_read_by_cases_and_reproduce(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "report.json"
    w.service(profile()).export_integrity_report_file(HORIZON, None, 10, ExportTarget(out))
    access = tmp_path / "access.json"
    access.write_text(profile(["operations:read"], [OPS]).model_dump_json(), "utf-8")
    monkeypatch.setenv("INVARIA_DATABASE_URL", w.dsn)
    common = ["--access-profile", str(access), "--report", str(out)]
    assert main(["analytics", "reproduce", *common]) == 0
    assert capsys.readouterr().out.startswith("REPRODUCED")
    assert main(["analytics", "cases", *common, "--key", "status:admitted"]) == 0


def test_the_file_fields_are_the_pinned_scope_of_the_export_policy(
    w: World, tmp_path: Path
) -> None:
    out = tmp_path / "report.json"
    w.service(profile()).export_integrity_report_file(HORIZON, None, 10, ExportTarget(out))
    pinned = json.loads(
        (Path(__file__).parents[1] / "fixtures/disclosure/full-policy-fields.json").read_text()
    )[EXPORT]

    def paths(node: Any, prefix: str) -> set[str]:
        """Field paths of a JSON value in the pinned notation (``a.b[].c``). A dict whose
        path has no pinned children is a leaf or a map keyed by data (e.g. ``statuses``)."""
        if isinstance(node, list):
            return set().union(set(), *(paths(item, f"{prefix}[]") for item in node))
        if not isinstance(node, dict) or not any(p.startswith(prefix + ".") for p in pinned):
            return set()
        found: set[str] = set()
        for key, value in node.items():
            found |= {f"{prefix}.{key}"} | paths(value, f"{prefix}.{key}")
        return found

    delivered = paths(json.loads(out.read_text()), "IntegrityReport")
    allowed = {p for p in pinned if p.startswith("IntegrityReport.")}
    assert delivered and delivered <= allowed, sorted(delivered - allowed)[:5]


# ------------------------------------------------------------------ failure model


def test_an_existing_destination_is_refused_before_computing_or_recording(
    w: World, tmp_path: Path
) -> None:
    out = tmp_path / "report.json"
    out.write_bytes(b"keep me")
    (tmp_path / "dangling.json").symlink_to(tmp_path / "nowhere.json")
    for path in (out, tmp_path / "dangling.json"):
        with pytest.raises(ExportError) as caught:
            w.service(profile()).export_integrity_report_file(HORIZON, None, 10, ExportTarget(path))
        assert caught.value.code == "EXPORT_DESTINATION" and caught.value.export_id is None
        assert str(tmp_path) not in caught.value.message
    assert out.read_bytes() == b"keep me" and not (tmp_path / "nowhere.json").exists()
    assert w.reader.export_records(TENANT) == []


def test_a_failure_to_record_the_attempt_writes_nothing(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(*args: Any, **kwargs: Any) -> Any:
        raise psycopg.OperationalError("connection lost")

    monkeypatch.setattr(w.exporter, "record_export_attempt", down)
    with pytest.raises(QueryError) as caught:
        w.service(profile()).export_integrity_report_file(
            HORIZON, None, 10, ExportTarget(tmp_path / "report.json")
        )
    assert caught.value.code == "SOURCE_UNAVAILABLE"
    assert files(tmp_path) == []


@pytest.mark.parametrize(
    ("call", "error", "failure"),
    [
        ("write", OSError(errno.ENOSPC, "No space left on device"), "write_error"),
        ("write", KeyboardInterrupt(), "interrupted"),
        ("link", PermissionError(errno.EPERM, "no hard links"), "publish_error"),
    ],
)
def test_a_failure_before_publishing_is_recorded_as_failed(
    w: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    call: str,
    error: BaseException,
    failure: str,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(os, call, fail)
    with pytest.raises(ExportError) as caught:
        w.service(profile()).export_integrity_report_file(
            HORIZON, None, 10, ExportTarget(tmp_path / "report.json")
        )
    monkeypatch.undo()
    assert (caught.value.code, caught.value.failure) == ("EXPORT_FAILED", failure)
    assert caught.value.outcome_recorded
    assert files(tmp_path) == []
    (record,) = w.reader.export_records(TENANT)
    assert (record["export_id"], record["outcome"], record["failure"]) == (
        caught.value.export_id,
        "failed",
        failure,
    )


def test_a_destination_created_by_another_process_before_publishing(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "report.json"
    real_link = os.link

    def someone_first(*args: Any, **kwargs: Any) -> None:
        out.write_bytes(b"another process")
        real_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", someone_first)
    with pytest.raises(ExportError) as caught:
        w.service(profile()).export_integrity_report_file(HORIZON, None, 10, ExportTarget(out))
    monkeypatch.undo()
    assert caught.value.failure == "destination_exists"
    assert out.read_bytes() == b"another process" and files(tmp_path) == ["report.json"]
    assert w.reader.export_records(TENANT)[0]["failure"] == "destination_exists"


def test_a_failure_to_record_the_failure_keeps_the_attempt_without_outcome(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise OSError(errno.ENOSPC, "No space left on device")

    def down(*args: Any, **kwargs: Any) -> Any:
        raise psycopg.OperationalError("connection lost")

    monkeypatch.setattr(os, "write", fail)
    monkeypatch.setattr(w.exporter, "record_export_outcome", down)
    with pytest.raises(ExportError) as caught:
        w.service(profile()).export_integrity_report_file(
            HORIZON, None, 10, ExportTarget(tmp_path / "report.json")
        )
    monkeypatch.undo()
    assert caught.value.code == "EXPORT_FAILED" and not caught.value.outcome_recorded
    assert "could not be recorded" in caught.value.message
    assert files(tmp_path) == []
    (record,) = w.reader.export_records(TENANT)
    assert record["outcome"] is None  # never turned into a failure afterwards


@pytest.mark.parametrize("fault", ["record", "interrupt"])
def test_a_published_file_without_recorded_completion_is_reported_as_such(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    out = tmp_path / "report.json"
    real = w.exporter.record_export_outcome

    def lost(tenant: str, export_id: str, outcome: str, failure: str | None) -> Any:
        # Only the completion is lost: any later attempt to record something else would
        # succeed, so the test sees it if the service turned the attempt into a failure.
        if outcome != "completed":
            return real(tenant, export_id, outcome, failure)
        if fault == "interrupt":
            raise KeyboardInterrupt
        raise psycopg.OperationalError("connection lost")

    monkeypatch.setattr(w.exporter, "record_export_outcome", lost)
    with pytest.raises(ExportError) as caught:
        w.service(profile()).export_integrity_report_file(HORIZON, None, 10, ExportTarget(out))
    monkeypatch.undo()
    assert caught.value.code == "EXPORT_UNCONFIRMED" and caught.value.export_id
    assert "may have been created complete" in caught.value.message
    assert str(tmp_path) not in caught.value.message
    assert out.exists() and files(tmp_path) == ["report.json"]  # complete, temporary gone
    (record,) = w.reader.export_records(TENANT)
    assert record["export_id"] == caught.value.export_id and record["outcome"] is None
    # A retry is a new attempt that refuses the existing file: never resumes or overwrites.
    with pytest.raises(ExportError) as again:
        w.service(profile()).export_integrity_report_file(HORIZON, None, 10, ExportTarget(out))
    assert again.value.code == "EXPORT_DESTINATION"
    assert len(w.reader.export_records(TENANT)) == 1


def test_the_record_is_append_only_for_the_export_role(w: World, tmp_path: Path) -> None:
    w.service(profile()).export_integrity_report_file(
        HORIZON, None, 10, ExportTarget(tmp_path / "r.json")
    )
    (record,) = w.reader.export_records(TENANT)
    with pytest.raises(psycopg.errors.UniqueViolation):
        w.exporter.record_export_outcome(TENANT, record["export_id"], "failed", "write_error")
    w.exporter.conn.rollback()
    for statement in (
        "UPDATE invaria.export_outcomes SET outcome = 'failed'",
        "DELETE FROM invaria.export_attempts",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            w.exporter.conn.execute(statement)
        w.exporter.conn.rollback()


# ---------------------------------------------------------------------------- CLI


def run(w: World, tmp_path: Path, access: AccessProfile, *extra: str) -> int:
    path = tmp_path / f"access-{uuid.uuid4().hex[:6]}.json"
    path.write_text(access.model_dump_json(), "utf-8")
    return main(["analytics", "report", "--access-profile", str(path), "--as-of", HORIZON, *extra])


def test_cli_walk(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INVARIA_DATABASE_URL", w.dsn)
    out = tmp_path / "report.json"
    reader_only = profile(["operations:read"], [OPS])
    assert run(w, tmp_path, reader_only) == 0  # the query is allowed
    capsys.readouterr()
    assert run(w, tmp_path, reader_only, "--out", str(out)) == 2  # the export is not
    err = capsys.readouterr().err
    assert "exports:create" in err and "nothing was written" in err and not out.exists()
    assert run(w, tmp_path, profile(), "--out", str(out)) == 0
    text = capsys.readouterr().out
    assert "A.match_share_of_conclusive" in text  # the report, printed as before
    assert "completed" in text and "does not prove delivery" in text
    assert run(w, tmp_path, profile(), "--out", str(out)) == 2  # never overwritten
    assert "EXPORT_DESTINATION" in capsys.readouterr().err
    assert run(w, tmp_path, profile(), "--out", str(out), "--register") == 2
    assert "cannot be combined" in capsys.readouterr().err
    (record,) = w.reader.export_records(TENANT)
    assert record["outcome"] == "completed"


def test_cli_explains_an_unconfirmed_export(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INVARIA_DATABASE_URL", w.dsn)

    def lost(*args: Any, **kwargs: Any) -> Any:
        raise psycopg.OperationalError("connection lost")

    monkeypatch.setattr(PgStore, "record_export_outcome", lost)
    out = tmp_path / "report.json"
    assert run(w, tmp_path, profile(), "--out", str(out)) == 1
    err = capsys.readouterr().err
    assert "EXPORT_UNCONFIRMED" in err and "may have been created complete" in err
    assert "nothing was written" not in err and out.exists()


def test_json_on_stdout_stays_a_query_output(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INVARIA_DATABASE_URL", w.dsn)
    assert run(w, tmp_path, profile(["operations:read"], [OPS]), "--json") == 0
    parse_contract(IntegrityReport, capsys.readouterr().out)
    assert w.reader.export_records(TENANT) == []  # a redirection is outside the record
