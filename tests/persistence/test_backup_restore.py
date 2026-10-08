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
from invaria.persistence.migrate import migrate
from invaria.persistence.provision import prepare_for_migrator
from invaria.persistence.store import APP_ROLE, READER_ROLE, PgStore
from invaria.persistence.worker import Clocks, reevaluate

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")
CONTAINER = os.environ.get("INVARIA_TEST_PG_CONTAINER")
IMAGE = os.environ.get("INVARIA_TEST_PG_IMAGE")
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/corpus"
TENANT = "tenant-synthetic-demo"


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
        },
        READER_ROLE: {
            "SELECT count(*) FROM invaria.evaluations": True,
            "SELECT count(*) FROM invaria.schema_migrations": False,
            "INSERT INTO invaria.knowledge_cuts (tenant_id, known_at) VALUES ('t', now())": False,
            "UPDATE invaria.publications SET epoch = 0": False,
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


def test_changed_effective_privileges_are_detected(
    source: Source, tools: PgTools, tmp_path: Path, targets: list[str]
) -> None:
    manifest, target = _restored(source, tools, tmp_path, targets)
    with psycopg.connect(_dsn_for(source.admin, target), autocommit=True) as admin:
        admin.execute("GRANT UPDATE ON invaria.publications TO invaria_app")
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
