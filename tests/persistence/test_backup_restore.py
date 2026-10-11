"""Logical backup and restore against a real PostgreSQL.

Opt-in: INVARIA_TEST_DATABASE_URL (admin URL) and INVARIA_TEST_PG_CONTAINER (the container
whose pg_dump/pg_restore are used). The clean-cluster test also needs
INVARIA_TEST_PG_IMAGE (the image pinned by digest); it starts and removes its own
disposable container on 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from invaria.contracts.observation import Observation
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.evaluate import evaluate
from invaria.persistence.backup import (
    ROLES,
    BackupError,
    BackupManifest,
    PgTools,
    check_invariants,
    create_backup,
    read_manifest,
    reproduce_all,
    restore_backup,
    state_digest,
    verify_restore,
    write_manifest,
)
from invaria.persistence.dsn import with_database
from invaria.persistence.events import derive, events_of
from invaria.persistence.migrate import migrate
from invaria.persistence.provision import prepare_for_migrator
from invaria.persistence.store import APP_ROLE, READER_ROLE, PgStore
from invaria.persistence.worker import Clocks, reevaluate
from invaria.query.models import AccessProfile
from invaria.query.service import QueryError, QueryService

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")
CONTAINER = os.environ.get("INVARIA_TEST_PG_CONTAINER")
IMAGE = os.environ.get("INVARIA_TEST_PG_IMAGE")
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/corpus"
TENANT = "tenant-synthetic-demo"
ACCESS = AccessProfile.model_validate_json(
    json.dumps(
        {
            "schema_version": "1.0",
            "principal_id": "operator-1",
            "tenant_id": TENANT,
            "scopes": [
                "operations:read",
                "reports:read",
                "reports:register",
                "disclosures:issue",
                "disclosures:revoke",
            ],
            "max_items": 50,
            "disclosure_policies": [
                "full-operations@1",
                "full-evidence@1",
                "full-report-index@1",
                "full-report-summary@1",
            ],
        }
    )
)


def _dsn_for(admin: str, database: str) -> str:
    return with_database(admin, database)


def _drop(admin: str, *names: str) -> None:
    with psycopg.connect(admin, autocommit=True) as conn:
        for name in names:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def insert_historical_evaluation(conn: psycopg.Connection[Any], evaluation: Any) -> None:
    """A conclusion recorded when its engine was current, as a restore brings it back.
    ``save_evaluation`` refuses engines that are not current."""
    conn.execute(
        "INSERT INTO invaria.evaluations (tenant_id, evaluation_id, snapshot_id, result, "
        "engine_ref, document) VALUES (%s, %s, %s, %s, %s, %s)",
        (
            TENANT,
            evaluation.evaluation_id,
            evaluation.snapshot_id,
            evaluation.result,
            evaluation.versions.engine_ref,
            Jsonb(evaluation.model_dump(mode="json")),
        ),
    )
    conn.commit()


@pytest.fixture(scope="module")
def subscription() -> Corpus:
    return load_corpus(FIXTURES / "subscription-synthetic")


@pytest.fixture(scope="module")
def redemption() -> Corpus:
    return load_corpus(FIXTURES / "redemption-synthetic")


def _clocks(corpus: Corpus, scenario: str) -> Clocks:
    s = corpus.scenarios[scenario].snapshot
    return Clocks(s.valid_at, s.known_at, s.evaluation_clock)


def _feed(store: PgStore, corpus: Corpus, timeline: str, upto: datetime, certs: set[str]) -> None:
    store.append_identity_links(
        TENANT, [k for k in corpus.identity_links.values() if k.recorded_at <= upto]
    )
    store.append_coverage(
        c for c in corpus.coverage.values() if c.coverage_id in certs and c.recorded_at <= upto
    )
    store.append_observations(
        o for o in corpus.journals[timeline].values() if o.recorded_at <= upto
    )


def populate(dsn: str, subscription: Corpus, redemption: Corpus) -> None:
    """Synthetic only: subscription K1-K3, redemption pending-missed-late, a stale scope
    and a partially acknowledged outbox."""
    with psycopg.connect(dsn) as conn:
        store = PgStore(conn)
        store.put_profile(subscription.profile)
        store.register_scope(TENANT, "SUB-0001", subscription.profile.profile_ref)
        sub_certs = {
            c for s in ("K1", "K2", "K3") for c in subscription.scenarios[s].snapshot.coverage_ids
        }
        for scenario in ("K1", "K2", "K3"):
            clocks = _clocks(subscription, scenario)
            _feed(store, subscription, "main", clocks.known_at, sub_certs)
            reevaluate(store, TENANT, "SUB-0001", clocks)
        store.put_profile(redemption.profile)
        store.register_scope(TENANT, "RED-0001", redemption.profile.profile_ref)
        red_certs = {c for c in redemption.coverage if c.count("-") == 2}
        for scenario in ("RD-PENDING", "RD-MISSED", "RD-LATE"):
            clocks = _clocks(redemption, scenario)
            _feed(store, redemption, "unpaid-then-late", clocks.known_at, red_certs)
            reevaluate(store, TENANT, "RED-0001", clocks)
        b2 = subscription.journals["main"]["obs-B2"].model_dump(mode="json")
        b2.update(observation_id="obs-B3", recorded_at="2026-10-09T00:00:00Z", supersedes="obs-B2")
        b2["source"]["revision"] = 3
        store.append_observations([Observation.model_validate_json(json.dumps(b2))])
        for event in store.outbox_pending("console", limit=3):
            store.outbox_ack("console", event.event_seq)
        # One registered integrity report, computed by the service.
        _, registration = QueryService(store, ACCESS).register_integrity_report(
            "2026-10-20T00:00:00Z"
        )
        assert registration.created
        # Two views issued to a reviewer, the second one revoked.
        operator = QueryService(store, ACCESS)
        for _ in range(2):
            operator.issue_disclosure(
                registration.report_id, "reviewer-1", "aggregate-outcome-counts@1"
            )
        (last,) = store.conn.execute(
            "SELECT view_id FROM invaria.disclosure_issuances ORDER BY issued_at DESC LIMIT 1"
        ).fetchone()  # type: ignore[misc]
        operator.revoke_disclosure(str(last))
        # The domain events derived so far (all pending rows).
        derived = derive(store, limit=1000)
        assert derived.errors == [] and derived.pending == 0 and derived.derived > 0


@pytest.fixture
def tools() -> PgTools:
    if not ADMIN_DSN or not CONTAINER:
        pytest.skip("set INVARIA_TEST_DATABASE_URL and INVARIA_TEST_PG_CONTAINER")
    return PgTools(("docker", "exec", "-i", CONTAINER))


@dataclass
class Source:
    admin: str
    name: str
    dsn: str


@pytest.fixture
def source(tools: PgTools, subscription: Corpus, redemption: Corpus) -> Iterator[Source]:
    assert ADMIN_DSN is not None
    name = f"invaria_src_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    dsn = _dsn_for(ADMIN_DSN, name)
    with psycopg.connect(dsn) as conn:
        prepare_for_migrator(conn)
        migrate(conn)
    populate(dsn, subscription, redemption)
    created: list[str] = []
    yield Source(ADMIN_DSN, name, dsn)
    _drop(ADMIN_DSN, name, *created)


@pytest.fixture
def targets() -> Iterator[list[str]]:
    names: list[str] = []
    yield names
    if ADMIN_DSN:
        _drop(ADMIN_DSN, *names)


def backup(source: Source, tools: PgTools, tmp_path: Path) -> tuple[BackupManifest, Path, Path]:
    manifest, dump = create_backup(
        source.admin,
        source.name,
        tools,
        tmp_path / "dump",
        backup_id="bk-test",
        container_image="test",
        created_at="test",
    )
    manifest_path = tmp_path / "manifest" / "manifest.json"
    manifest_path.parent.mkdir()
    write_manifest(manifest, manifest_path)
    return read_manifest(manifest_path), dump, manifest_path


def new_target(targets: list[str]) -> str:
    name = f"invaria_rst_{uuid.uuid4().hex[:10]}"
    targets.append(name)
    return name


# --------------------------------------------------------------------- tests


def test_restore_reproduces_every_evaluation(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str]
) -> None:
    manifest, dump, _ = backup(source, tools, tmp_path)
    assert manifest.engines_required == [
        "invaria-engine@0.10.0",
        "invaria-redemption-engine@0.11.0",
    ]
    assert manifest.tables["evaluations"]["rows"] == 6
    target = new_target(targets)
    restore_backup(source.admin, dump, manifest, target, tools)
    report = verify_restore(source.admin, target, manifest, dump)
    assert report.status == "RESTORE_VERIFIED", report.reasons
    assert (report.evaluations, report.reproduced) == (6, 6)
    # The registered report keeps its bytes, id, references and readings.
    assert manifest.tables["integrity_reports"]["rows"] == 1
    views = []
    for dsn in (source.dsn, _dsn_for(source.admin, target)):
        with psycopg.connect(dsn) as conn:
            service = QueryService(PgStore(conn, role=READER_ROLE), ACCESS)
            (item,) = service.list_integrity_reports().items
            views.append(
                (
                    item,
                    service.get_integrity_report_summary(item.report_id),
                    service.registered_integrity_cases(item.report_id, "included"),
                    conn.execute(
                        "SELECT document, document_sha256 FROM invaria.integrity_reports"
                    ).fetchall(),
                )
            )
    assert views[0] == views[1]
    # Issuances, policies (by reference) and revocations survive: the same view is
    # served and the revoked one is still the same 404.
    assert manifest.tables["disclosure_issuances"]["rows"] == 2
    assert manifest.tables["disclosure_revocations"]["rows"] == 1
    reviewer = ACCESS.model_copy(
        update={
            "principal_id": "reviewer-1",
            "scopes": ["disclosures:read"],
            "disclosure_policies": ["aggregate-outcome-counts@1"],
        }
    )
    served = []
    for dsn in (source.dsn, _dsn_for(source.admin, target)):
        with psycopg.connect(dsn) as conn:
            service = QueryService(PgStore(conn, role=READER_ROLE), reviewer)
            (issued,) = service.list_disclosures().items
            (revoked,) = conn.execute(
                "SELECT view_id FROM invaria.disclosure_revocations"
            ).fetchone()  # type: ignore[misc]
            with pytest.raises(QueryError) as caught:
                service.get_disclosure(str(revoked))
            served.append(
                (
                    issued,
                    service.get_disclosure(issued.view_id),
                    caught.value.code,
                    conn.execute(
                        "SELECT i.*, r.revoked_by, r.revoked_at FROM "
                        "invaria.disclosure_issuances i LEFT JOIN "
                        "invaria.disclosure_revocations r USING (view_id) ORDER BY view_id"
                    ).fetchall(),
                )
            )
    assert served[0] == served[1] and served[0][2] == "NOT_FOUND"
    # Events, counters and derivation state survive; nothing is derived twice
    # and numbering continues without a gap after the restore.
    assert manifest.tables["domain_events"]["rows"] > 0
    with psycopg.connect(_dsn_for(source.admin, target)) as conn:
        restored = PgStore(conn, role=APP_ROLE)
        before = events_of(restored, TENANT)
        with psycopg.connect(source.dsn) as src:
            assert events_of(PgStore(src, role=READER_ROLE), TENANT) == before
        again = derive(restored, limit=1000)
        assert (again.derived, again.not_public, again.pending) == (0, 0, 0)
        restored.invalidate(TENANT, ["SUB-0001"], "after restore")
        assert derive(restored, limit=1000).derived == 1
        after = events_of(restored, TENANT)
        assert [r[0] for r in after] == list(range(1, len(before) + 2))


def test_the_cut_is_the_exported_snapshot_despite_concurrent_writes(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str], subscription: Corpus
) -> None:
    class WritingTools(PgTools):
        def run(self, args: Any, *, stdin: bytes | None = None) -> bytes:
            if args[0] == "pg_dump" and any(a.startswith("--snapshot=") for a in args):
                with psycopg.connect(source.dsn) as conn:  # a writer during the dump
                    b1 = subscription.journals["main"]["obs-B1"].model_dump(mode="json")
                    b1.update(observation_id="obs-LATE", tenant_id="tenant-other")
                    PgStore(conn).append_observations(
                        [Observation.model_validate_json(json.dumps(b1))]
                    )
            return super().run(args, stdin=stdin)

    manifest, dump, _ = backup(source, WritingTools(tools.prefix), tmp_path)
    with psycopg.connect(source.dsn) as conn, conn.transaction():
        after, _ = state_digest(conn)
    assert after["observations"]["rows"] == manifest.tables["observations"]["rows"] + 1
    target = new_target(targets)
    restore_backup(source.admin, dump, manifest, target, tools)
    report = verify_restore(source.admin, target, manifest, dump)
    assert report.status == "RESTORE_VERIFIED", report.reasons
    with psycopg.connect(_dsn_for(source.admin, target)) as conn:
        row = conn.execute(
            "SELECT count(*) FROM invaria.observations WHERE observation_id = 'obs-LATE'"
        ).fetchone()
    assert row is not None and row[0] == 0


def test_restore_never_overwrites_an_existing_database(
    source: Source, tools: PgTools, tmp_path: Path
) -> None:
    manifest, dump, _ = backup(source, tools, tmp_path)
    with psycopg.connect(source.dsn) as conn, conn.transaction():
        before, _ = state_digest(conn)
    with pytest.raises(BackupError, match="never overwrites"):
        restore_backup(source.admin, dump, manifest, source.name, tools)
    with psycopg.connect(source.dsn) as conn, conn.transaction():
        assert state_digest(conn)[0] == before


def test_tampered_dump_is_refused_and_detected(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str]
) -> None:
    manifest, dump, _ = backup(source, tools, tmp_path)
    original = dump.read_bytes()
    dump.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(BackupError, match="sha256"):
        restore_backup(source.admin, dump, manifest, new_target(targets), tools)
    dump.write_bytes(original)
    target = new_target(targets)
    restore_backup(source.admin, dump, manifest, target, tools)
    dump.write_bytes(original + b"x")
    report = verify_restore(source.admin, target, manifest, dump)
    assert report.status == "RESTORE_MISMATCH"
    assert "dump sha256 differs from the manifest" in report.reasons


def test_restored_state_must_match_the_manifest_cut(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str]
) -> None:
    manifest, dump, _ = backup(source, tools, tmp_path)
    target = new_target(targets)
    restore_backup(source.admin, dump, manifest, target, tools)
    with psycopg.connect(_dsn_for(source.admin, target), autocommit=True) as admin:
        admin.execute("DELETE FROM invaria.outbox_acks")  # admin tampering after restore
    report = verify_restore(source.admin, target, manifest)
    assert report.status == "RESTORE_MISMATCH"
    assert any(r.startswith("table outbox_acks") for r in report.reasons)


def test_migrations_are_checked_against_the_trusted_code(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str]
) -> None:
    manifest, dump, _ = backup(source, tools, tmp_path)
    target = new_target(targets)
    restore_backup(source.admin, dump, manifest, target, tools)
    with psycopg.connect(_dsn_for(source.admin, target), autocommit=True) as admin:
        admin.execute(
            "UPDATE invaria.schema_migrations SET sha256 = repeat('0', 64) "
            "WHERE version = '0002_revision'"
        )
    forged = manifest.model_copy(
        update={"migrations": {**manifest.migrations, "0002_revision": "0" * 64}}
    )
    report = verify_restore(source.admin, target, forged)
    assert report.status == "RESTORE_MISMATCH"
    assert any("differ from the trusted code" in r for r in report.reasons)


def test_a_missing_engine_is_incomplete_never_substituted(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str], redemption: Corpus
) -> None:
    with psycopg.connect(source.dsn) as conn:
        store = PgStore(conn)
        snapshot_id = conn.execute(
            "SELECT snapshot_id FROM invaria.snapshots WHERE operation_ref = 'RED-0001' LIMIT 1"
        ).fetchone()
        assert snapshot_id is not None
        conn.commit()
        old = evaluate(
            store.load_inputs(TENANT, str(snapshot_id[0])),
            engine_ref="invaria-redemption-engine@0.3.0",
        )
        insert_historical_evaluation(conn, old.result)
    manifest, dump, _ = backup(source, tools, tmp_path)
    target = new_target(targets)
    restore_backup(source.admin, dump, manifest, target, tools)
    report = verify_restore(source.admin, target, manifest, dump)
    assert report.status == "RESTORE_INCOMPLETE"
    assert (report.evaluations, report.reproduced) == (7, 6)
    assert any("blocked by policy" in r or "superseded" in r for r in report.not_reproducible)


def test_effective_privileges_after_restore(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str], subscription: Corpus
) -> None:
    manifest, dump, _ = backup(source, tools, tmp_path)
    target = new_target(targets)
    restore_backup(source.admin, dump, manifest, target, tools)
    dsn = _dsn_for(source.admin, target)
    _assert_privileges(lambda role: _set_role(dsn, role))
    # The worker needs only SELECT and INSERT: publication and acknowledgement are rows.
    with psycopg.connect(dsn) as conn:
        store = PgStore(conn, role=APP_ROLE)
        clocks = Clocks(
            datetime.fromisoformat("2026-10-01T17:00:00Z"),
            datetime.fromisoformat("2026-10-09T00:00:00Z"),
            datetime.fromisoformat("2026-10-09T00:00:00Z"),
        )
        _, publication = reevaluate(store, TENANT, "SUB-0001", clocks)
        assert publication.outcome == "PUBLISHED"
        event = store.outbox_pending("console", limit=1)[0]
        assert store.outbox_ack("console", event.event_seq)


def _set_role(dsn: str, role: str) -> psycopg.Connection[Any]:
    conn = psycopg.connect(dsn)
    conn.execute(f"SET ROLE {role}")
    conn.commit()
    return conn


def _assert_privileges(connect: Any) -> None:
    """Allowed and denied operations, executed for real under each role."""
    checks = {
        APP_ROLE: {
            "SELECT count(*) FROM invaria.evaluations": True,
            "INSERT INTO invaria.knowledge_cuts (tenant_id, known_at) VALUES ('t', now())": True,
            "UPDATE invaria.evaluations SET result = 'MATCH'": False,
            "DELETE FROM invaria.outbox_acks": False,
            "TRUNCATE invaria.publications": False,
            "SELECT count(*) FROM invaria.integrity_reports": True,
            "UPDATE invaria.integrity_reports SET registered_by = 'x'": False,
            "DELETE FROM invaria.integrity_reports": False,
            "INSERT INTO invaria.integrity_reports (tenant_id, report_id, report_sha256, "
            "report_format, definition, selection_policy, evidence_horizon, registered_by, "
            "document, document_sha256, registered_at) SELECT 't', 'ir-' || repeat('a', 64), "
            "repeat('a', 64), 'f', 'd', 'p', now(), 'x', '\\x7b7d'::bytea, "
            "encode(sha256('\\x7b7d'::bytea), 'hex'), now()": False,
            "SELECT count(*) FROM invaria.disclosure_revocations": True,
            "UPDATE invaria.disclosure_issuances SET recipient_principal = 'x'": False,
            "DELETE FROM invaria.disclosure_issuances": False,
            "INSERT INTO invaria.disclosure_issuances (view_id, tenant_id, recipient_principal, "
            "policy_ref, view_format, report_id, issued_by) SELECT 'dv-' || repeat('a', 32), "
            "tenant_id, 'r', policy_ref, view_format, report_id, 'o' FROM "
            "invaria.disclosure_issuances LIMIT 1": False,
        },
        READER_ROLE: {
            "SELECT count(*) FROM invaria.evaluations": True,
            "SELECT count(*) FROM invaria.schema_migrations": False,
            "INSERT INTO invaria.knowledge_cuts (tenant_id, known_at) VALUES ('t', now())": False,
            "UPDATE invaria.publications SET epoch = 0": False,
            "SELECT count(*) FROM invaria.integrity_reports": True,
            "DELETE FROM invaria.integrity_reports": False,
            "SELECT count(*) FROM invaria.disclosure_issuances": True,
            "DELETE FROM invaria.disclosure_revocations": False,
        },
    }
    for role, statements in checks.items():
        for statement, allowed in statements.items():
            conn = connect(role)
            try:
                if allowed:
                    conn.execute(statement)
                else:
                    with pytest.raises(errors.InsufficientPrivilege):
                        conn.execute(statement)
            finally:
                conn.rollback()
                conn.close()


# ------------------------------------------------------- clean disposable cluster


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def clean_cluster() -> Iterator[tuple[str, PgTools]]:
    """A brand-new PostgreSQL from the pinned image: no Invaria roles exist yet."""
    if not IMAGE:
        pytest.skip("set INVARIA_TEST_PG_IMAGE to the pinned postgres image to run this test")
    name = f"invaria-pg-restore-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    password = secrets.token_urlsafe(24)
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-p",
            f"127.0.0.1:{port}:5432",
            IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    admin = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(admin, connect_timeout=2) as conn:
                    conn.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        yield admin, PgTools(("docker", "exec", "-i", name))
    finally:
        subprocess.run(["docker", "stop", name], check=False, capture_output=True)


def test_clean_cluster_rebuilds_roles_and_restores(
    source: Source, tools: PgTools, tmp_path: Path, clean_cluster: tuple[str, PgTools]
) -> None:
    admin, cluster_tools = clean_cluster
    with psycopg.connect(admin) as conn:
        assert not conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(ROLES),)
        ).fetchall()
    manifest, dump, _ = backup(source, tools, tmp_path)
    created = restore_backup(admin, dump, manifest, "invaria_restored", cluster_tools)
    assert sorted(created) == sorted(ROLES)
    report = verify_restore(admin, "invaria_restored", manifest, dump)
    assert report.status == "RESTORE_VERIFIED", report.reasons
    # Real logins: a member role per NOLOGIN role, password generated here and discarded.
    password = secrets.token_urlsafe(24)
    with psycopg.connect(admin, autocommit=True) as conn:
        for role in (APP_ROLE, READER_ROLE):
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN INHERIT PASSWORD {} IN ROLE {}").format(
                    sql.Identifier(f"{role}_login"), sql.Literal(password), sql.Identifier(role)
                )
            )

    def login(role: str) -> psycopg.Connection[Any]:
        params = {**conninfo_to_dict(admin), "user": f"{role}_login", "password": password}
        return psycopg.connect(make_conninfo("", **{**params, "dbname": "invaria_restored"}))

    _assert_privileges(login)


def _restored(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str]
) -> tuple[BackupManifest, str]:
    manifest, dump, _ = backup(source, tools, tmp_path)
    target = new_target(targets)
    restore_backup(source.admin, dump, manifest, target, tools)
    return manifest, target


def test_a_rewound_sequence_that_would_reuse_ids_is_detected(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str]
) -> None:
    manifest, target = _restored(source, tools, tmp_path, targets)
    with psycopg.connect(_dsn_for(source.admin, target), autocommit=True) as admin:
        admin.execute("SELECT setval('invaria.outbox_event_seq_seq', 1)")
    report = verify_restore(source.admin, target, manifest)
    assert report.status == "RESTORE_MISMATCH"
    assert any("would reuse ids" in r for r in report.reasons)


@pytest.mark.parametrize(
    "change",
    [
        "GRANT UPDATE ON invaria.publications TO invaria_app",
        # A lost (or widened) column-level INSERT grant is detected too.
        "REVOKE INSERT (document) ON invaria.integrity_reports FROM invaria_app",
        "GRANT INSERT (registered_at) ON invaria.integrity_reports TO invaria_app",
    ],
)
def test_changed_effective_privileges_are_detected(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str], change: str
) -> None:
    manifest, target = _restored(source, tools, tmp_path, targets)
    expected = (
        "integrity_reports:INSERT(definition,document,document_sha256,evidence_horizon,"
        "registered_by,report_format,report_id,report_sha256,selection_policy,store_read_at,"
        "tenant_id)"
    )
    assert expected in manifest.privileges["invaria_app"]
    with psycopg.connect(_dsn_for(source.admin, target), autocommit=True) as admin:
        admin.execute(change)
    report = verify_restore(source.admin, target, manifest)
    assert report.status == "RESTORE_MISMATCH"
    assert "effective privileges of invaria_app differ from the manifest" in report.reasons


def test_broken_invariants_and_replays_are_reported(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str]
) -> None:
    _, target = _restored(source, tools, tmp_path, targets)
    dsn = _dsn_for(source.admin, target)
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("DELETE FROM invaria.outbox_acks")
        # An event pins its source row (foreign key); remove it first.
        admin.execute(
            "DELETE FROM invaria.domain_events WHERE source_event_seq = (SELECT min(event_seq) "
            "FROM invaria.outbox WHERE event_type = 'evaluation.published')"
        )
        admin.execute(
            "DELETE FROM invaria.outbox WHERE event_seq = (SELECT min(event_seq) FROM "
            "invaria.outbox WHERE event_type = 'evaluation.published')"
        )
        assert any("no evaluation.published event" in p for p in check_invariants(admin))
        admin.execute(
            "UPDATE invaria.evaluations SET document = jsonb_set(document, "
            "'{assumptions,0}', '\"tampered\"') WHERE engine_ref = 'invaria-engine@0.10.0'"
        )
    with psycopg.connect(dsn) as conn:
        total, reproduced, missing, mismatches = reproduce_all(PgStore(conn, role=READER_ROLE))
    assert (total, missing) == (6, []) and reproduced == 3 and len(mismatches) == 3


def test_a_rewritten_registered_report_fails_the_restore_verification(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str]
) -> None:
    # Rewritten in the source before the backup: the dump and its manifest agree with each
    # other, so only the re-verification of the registered report can catch it.
    with psycopg.connect(source.dsn, autocommit=True) as admin:
        admin.execute(
            "UPDATE invaria.integrity_reports SET document = document || '\\x20'::bytea, "
            "document_sha256 = encode(sha256(document || '\\x20'::bytea), 'hex')"
        )
    manifest, target = _restored(source, tools, tmp_path, targets)
    report = verify_restore(source.admin, target, manifest)
    assert report.status == "RESTORE_MISMATCH"
    assert [r for r in report.reasons if "integrity report" in r] == [
        r for r in report.reasons if r.endswith(": INTEGRITY_ERROR")
    ]
    assert len(report.reasons) == 1, report.reasons


def test_a_gap_in_event_numbering_fails_the_restore_verification(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str]
) -> None:
    with psycopg.connect(source.dsn, autocommit=True) as admin:  # tampered before the backup
        admin.execute(
            "UPDATE invaria.domain_event_counters SET last_seq = last_seq + 1 WHERE tenant_id = %s",
            (TENANT,),
        )
    manifest, target = _restored(source, tools, tmp_path, targets)
    report = verify_restore(source.admin, target, manifest)
    assert report.status == "RESTORE_MISMATCH"
    assert any("domain event numbering" in r for r in report.reasons), report.reasons


EVENTS_ACCESS = ACCESS.model_copy(
    update={"scopes": ["events:read", "operations:read", "reports:read"]}
)


def _events(dsn: str, **kwargs: Any) -> Any:
    with psycopg.connect(dsn) as conn:
        service = QueryService(PgStore(conn, role=READER_ROLE), EVENTS_ACCESS)
        try:
            return service.list_events(**kwargs)
        except QueryError as error:
            return error.code


def _to_head(dsn: str) -> Any:
    page = _events(dsn, start="beginning")
    while not page.caught_up:
        page = _events(dsn, cursor=page.next_cursor)
    return page


def test_a_restored_feed_refuses_cursors_of_the_original_history(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str]
) -> None:
    manifest, dump, _ = backup(source, tools, tmp_path)
    before = _to_head(source.dsn)  # a client at the head of the backed-up history
    with psycopg.connect(source.dsn) as conn:  # the source goes on after the backup
        store = PgStore(conn, role=APP_ROLE)
        store.invalidate(TENANT, ["SUB-0001"], "after the backup")
        derive(store, limit=1000)
    far = _to_head(source.dsn)
    assert far.continuation_position > before.continuation_position
    target = new_target(targets)
    restore_backup(source.admin, dump, manifest, target, tools)
    restored = _dsn_for(source.admin, target)
    assert verify_restore(source.admin, target, manifest, dump).status == "RESTORE_VERIFIED"
    # Both cursors belong to the original history: an explicit restart, never a
    # continuation that looks valid.
    assert _events(restored, cursor=far.next_cursor) == "CURSOR_RESET"
    assert _events(restored, cursor=before.next_cursor) == "CURSOR_RESET"
    again = _to_head(restored)
    assert again.generation_id != before.generation_id
    first = _events(restored, start="beginning", limit=50)
    original = _events(source.dsn, start="beginning", limit=50)
    restored_ids = [i.event.event_id for i in first.items]
    assert restored_ids == [i.event.event_id for i in original.items][: len(restored_ids)]
    with psycopg.connect(restored) as conn:  # numbering continues after the restore
        store = PgStore(conn, role=APP_ROLE)
        store.invalidate(TENANT, ["SUB-0001"], "after the restore")
        derive(store, limit=1000)
    after = _events(restored, cursor=again.next_cursor)
    assert [i.tenant_seq for i in after.items] == [again.continuation_position + 1]


def test_a_manual_restore_without_the_tool_is_only_partly_detected(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str]
) -> None:
    """Declared limit: a restore that skips the supported tool keeps the old generation.
    A cursor ahead of the restored head is refused (CURSOR_BEYOND_FEED); one at or below
    it is accepted, and the restore is not detected."""
    manifest, dump, _ = backup(source, tools, tmp_path)
    near = _events(source.dsn, start="beginning", limit=1)
    with psycopg.connect(source.dsn) as conn:
        store = PgStore(conn, role=APP_ROLE)
        store.invalidate(TENANT, ["SUB-0001"], "after the backup")
        derive(store, limit=1000)
    far = _to_head(source.dsn)
    target = new_target(targets)
    restore_backup(source.admin, dump, manifest, target, tools)
    restored = _dsn_for(source.admin, target)
    with psycopg.connect(restored, autocommit=True) as admin:  # as if the tool were skipped
        admin.execute(
            "DELETE FROM invaria.feed_generations WHERE ordinal = "
            "(SELECT max(ordinal) FROM invaria.feed_generations)"
        )
    assert _events(restored, cursor=far.next_cursor) == "CURSOR_BEYOND_FEED"
    accepted = _events(restored, cursor=near.next_cursor)
    assert not isinstance(accepted, str)  # undetected: documented, not claimed
    assert verify_restore(source.admin, target, manifest).status == "RESTORE_MISMATCH"
