"""Logical backup, restore and restore verification.

The dump is taken with ``pg_dump`` inside the PostgreSQL container pinned by digest, from
an exported snapshot: the state digest stored in the manifest is computed in that same
snapshot, so it describes exactly the cut the dump contains, even if writers continue.

Roles are server-level objects that ``pg_dump`` does not create. They are rebuilt before
the restore from the NOLOGIN definitions below (no passwords exist or are exported), and
owners and grants then come from the dump itself.

What a restore needs outside PostgreSQL is checked, not assumed: the trusted engine
runtimes of every stored evaluation and the trusted migration files (their hashes come
from this code, never only from the restored database). Raw evidence files are not part
of the database; replay uses the stored canonical documents, so they are not needed
to reproduce evaluations, and R2 is out of scope.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import psycopg
from psycopg import sql
from pydantic import Field

from invaria.bundle.runtime import BLOCKED_ENGINES, TRUSTED_ENGINES
from invaria.contracts.base import Contract, Identifier, NonEmptyText, SchemaVersion, Sha256Hex
from invaria.persistence.dsn import with_database
from invaria.persistence.migrate import available_migrations
from invaria.persistence.store import READER_ROLE, PgStore

SCHEMA = "invaria"
ROLES = ("invaria_owner", "invaria_app", "invaria_reader")
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
EXTERNAL_DEPENDENCIES = (
    "trusted engine runtimes for every stored engine_ref (bundle.runtime.TRUSTED_ENGINES)",
    "trusted migration files (persistence/migrations): expected hashes come from the code",
    "server-level roles invaria_owner, invaria_app, invaria_reader (rebuilt, NOLOGIN)",
    "not needed for R1 replay: raw evidence files (replay uses stored documents; R2 is out "
    "of scope); DSNs and access profiles (configuration outside the repository)",
)
Conn = psycopg.Connection[Any]


class BackupError(Exception):
    """The backup or restore cannot proceed safely (nothing was overwritten)."""


class BackupManifest(Contract):
    """Kept apart from the dump. Hashes detect changes against this manifest; they do not
    prove authenticity if someone can modify both the dump and the manifest."""

    schema_version: SchemaVersion
    backup_id: Identifier
    database: Identifier
    created_at: NonEmptyText  # wall clock, informational only
    pg_dump_version: NonEmptyText
    server_version: NonEmptyText
    container_image: NonEmptyText
    dump_file: NonEmptyText
    dump_sha256: Sha256Hex
    dump_bytes: int
    cut: Literal["exported_snapshot_repeatable_read"]
    tables: dict[str, dict[str, Any]]
    sequences: dict[str, int | None]
    migrations: dict[str, Sha256Hex]
    privileges: dict[str, list[str]]
    engines_required: list[NonEmptyText]
    external_dependencies: list[NonEmptyText]


class RestoreReport(Contract):
    status: Literal["RESTORE_VERIFIED", "RESTORE_INCOMPLETE", "RESTORE_MISMATCH"]
    database: Identifier
    evaluations: int
    reproduced: int
    not_reproducible: list[NonEmptyText]
    reasons: list[NonEmptyText] = Field(default_factory=list)


# ------------------------------------------------------------------ state digest


def _tables(conn: Conn) -> list[tuple[str, list[str]]]:
    rows = conn.execute(
        "SELECT c.relname, array_agg(a.attname ORDER BY array_position(i.indkey, a.attnum)) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_index i ON i.indrelid = c.oid AND i.indisprimary "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
        "WHERE n.nspname = %s AND c.relkind = 'r' GROUP BY c.relname ORDER BY c.relname",
        (SCHEMA,),
    ).fetchall()
    return [(str(r[0]), [str(x) for x in r[1]]) for r in rows]


def state_digest(conn: Conn) -> tuple[dict[str, dict[str, Any]], dict[str, int | None]]:
    """Row count and sha256 of every table (rows as JSON, in primary-key order), and the
    last value of every identity sequence. Read in the caller's transaction."""
    conn.execute("SET LOCAL TIME ZONE 'UTC'")
    tables: dict[str, dict[str, Any]] = {}
    for table, key in _tables(conn):
        query = sql.SQL("SELECT row_to_json(t)::text FROM {}.{} t ORDER BY {}").format(
            sql.Identifier(SCHEMA),
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(k) for k in key),
        )
        digest = hashlib.sha256()
        count = 0
        for (line,) in conn.execute(query):
            digest.update(str(line).encode("utf-8") + b"\n")
            count += 1
        tables[table] = {"rows": count, "sha256": digest.hexdigest()}
    sequences = {
        str(r[0]): (None if r[1] is None else int(r[1]))
        for r in conn.execute(
            "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = %s "
            "ORDER BY sequencename",
            (SCHEMA,),
        ).fetchall()
    }
    return tables, sequences


def privilege_matrix(conn: Conn) -> dict[str, list[str]]:
    """Effective table privileges per role: ``{"role": ["table:PRIV", ...]}``."""
    matrix: dict[str, list[str]] = {}
    tables = [t for t, _ in _tables(conn)]
    for role in ROLES:
        granted = []
        for table in tables:
            for privilege in PRIVILEGES:
                ok = conn.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, f"{SCHEMA}.{table}", privilege),
                ).fetchone()
                if ok and ok[0]:
                    granted.append(f"{table}:{privilege}")
        matrix[role] = granted
    return matrix


def stored_migrations(conn: Conn) -> dict[str, str]:
    rows = conn.execute(f"SELECT version, sha256 FROM {SCHEMA}.schema_migrations").fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def trusted_migrations() -> dict[str, str]:
    return {v: hashlib.sha256(body).hexdigest() for v, body in available_migrations()}


def bootstrap_roles(admin: Conn) -> list[str]:
    """Create the NOLOGIN roles a restore needs, if absent. Returns those created."""
    created = []
    for role in ROLES:
        exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        if exists is None:
            admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            created.append(role)
    return created


# ------------------------------------------------------------------ backup


@dataclass(frozen=True)
class PgTools:
    """How to run pg_dump / pg_restore, e.g. inside the pinned container:
    ``PgTools(("docker", "exec", "-i", "invaria-pg-dev"))``."""

    prefix: Sequence[str]
    user: str = "postgres"

    def run(self, args: Sequence[str], *, stdin: bytes | None = None) -> bytes:
        result = subprocess.run(
            [*self.prefix, *args], input=stdin, capture_output=True, check=False
        )
        if result.returncode != 0:
            raise BackupError(
                f"{args[0]} failed ({result.returncode}): "
                f"{result.stderr.decode('utf-8', 'replace').strip()[:500]}"
            )
        return result.stdout

    def version(self, tool: str) -> str:
        return self.run([tool, "--version"]).decode("utf-8").strip()


def create_backup(
    admin_dsn: str,
    database: str,
    tools: PgTools,
    out_dir: Path,
    *,
    backup_id: str,
    container_image: str,
    created_at: str,
) -> tuple[BackupManifest, Path]:
    """Dump ``database`` from one exported snapshot and describe that exact cut."""
    out_dir.mkdir(parents=True, exist_ok=False)
    dump_path = out_dir / f"{backup_id}.dump"
    with psycopg.connect(_dsn_for(admin_dsn, database)) as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        row = conn.execute("SELECT pg_export_snapshot()").fetchone()
        assert row is not None
        snapshot_name = str(row[0])
        tables, sequences = state_digest(conn)
        migrations = stored_migrations(conn)
        privileges = privilege_matrix(conn)
        engines = sorted(
            str(r[0]) for r in conn.execute(f"SELECT DISTINCT engine_ref FROM {SCHEMA}.evaluations")
        )
        server_version = str(conn.execute("SHOW server_version").fetchone()[0])  # type: ignore[index]
        # pg_dump reads the same snapshot while this transaction stays open.
        dump = tools.run(
            [
                "pg_dump",
                "-U",
                tools.user,
                "-d",
                database,
                "--format=custom",
                f"--snapshot={snapshot_name}",
            ]
        )
        conn.rollback()
    dump_path.write_bytes(dump)
    manifest = BackupManifest(
        schema_version="1.0",
        backup_id=backup_id,
        database=database,
        created_at=created_at,
        pg_dump_version=tools.version("pg_dump"),
        server_version=server_version,
        container_image=container_image,
        dump_file=dump_path.name,
        dump_sha256=hashlib.sha256(dump).hexdigest(),
        dump_bytes=len(dump),
        cut="exported_snapshot_repeatable_read",
        tables=tables,
        sequences=sequences,
        migrations=migrations,
        privileges=privileges,
        engines_required=engines,
        external_dependencies=list(EXTERNAL_DEPENDENCIES),
    )
    return manifest, dump_path


def write_manifest(manifest: BackupManifest, path: Path) -> str:
    """Write the manifest (kept apart from the dump); return its sha256."""
    text = json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_manifest(path: Path) -> BackupManifest:
    return BackupManifest.model_validate_json(path.read_text("utf-8"))


# ------------------------------------------------------------------ restore


def restore_backup(
    admin_dsn: str, dump_path: Path, manifest: BackupManifest, target: str, tools: PgTools
) -> list[str]:
    """Restore into a new, dedicated database. Refuses an existing target or a dump that
    does not match the manifest. Returns the roles that had to be rebuilt."""
    data = dump_path.read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest.dump_sha256:
        raise BackupError("dump does not match the manifest sha256; nothing restored")
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,)).fetchone()
        if exists is not None:
            raise BackupError(f"target database {target} exists; a restore never overwrites")
        created = bootstrap_roles(admin)
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
    tools.run(
        [
            "pg_restore",
            "-U",
            tools.user,
            "-d",
            target,
            "--exit-on-error",
            "--single-transaction",
        ],
        stdin=data,
    )
    return created


# ------------------------------------------------------------------ verification


def check_invariants(conn: Conn) -> list[str]:
    """Cross-table invariants of the revision model, over every tenant."""
    problems: list[str] = []

    def rows(query: str) -> list[tuple[Any, ...]]:
        return conn.execute(query).fetchall()

    for r in rows(
        f"SELECT p.tenant_id, p.operation_ref, p.epoch FROM {SCHEMA}.publications p "
        f"JOIN {SCHEMA}.evaluations e USING (tenant_id, evaluation_id) "
        f"JOIN {SCHEMA}.snapshots s ON s.tenant_id = e.tenant_id AND s.snapshot_id = e.snapshot_id "
        "WHERE s.operation_ref <> p.operation_ref"
    ):
        problems.append(f"publication {r} points to an evaluation of another operation")
    for r in rows(
        f"SELECT p.tenant_id, p.operation_ref, p.epoch FROM {SCHEMA}.publications p "
        f"WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.outbox o WHERE o.dedup_key = "
        "p.tenant_id || '|' || p.operation_ref || '|published|' || p.epoch)"
    ):
        problems.append(f"publication {r} has no evaluation.published event")
    for r in rows(
        f"SELECT e.tenant_id, e.operation_ref, e.epoch FROM {SCHEMA}.scope_epochs e "
        f"WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.outbox o WHERE o.dedup_key = "
        "e.tenant_id || '|' || e.operation_ref || '|invalidated|' || e.epoch)"
    ):
        problems.append(f"epoch {r} has no scope.invalidated event")
    for r in rows(
        f"SELECT p.tenant_id, p.operation_ref, p.epoch FROM {SCHEMA}.publications p "
        f"LEFT JOIN {SCHEMA}.publish_attempts a ON a.tenant_id = p.tenant_id "
        "AND a.evaluation_id = p.evaluation_id AND a.epoch = p.epoch "
        "WHERE a.outcome IS DISTINCT FROM 'PUBLISHED'"
    ):
        problems.append(f"publication {r} lacks its PUBLISHED attempt")
    for r in rows(
        f"SELECT tenant_id, snapshot_id, engine_ref, count(*) FROM {SCHEMA}.evaluations "
        "GROUP BY 1, 2, 3 HAVING count(*) > 1"
    ):
        problems.append(f"logical evaluation duplicated: {r}")
    for r in rows(
        f"SELECT p.tenant_id, p.operation_ref, p.epoch FROM {SCHEMA}.publications p "
        f"JOIN {SCHEMA}.evaluations e USING (tenant_id, evaluation_id) "
        f"JOIN {SCHEMA}.snapshots s ON s.tenant_id = e.tenant_id AND s.snapshot_id = e.snapshot_id "
        f"WHERE s.known_at < (SELECT max(x.knowledge_floor) FROM {SCHEMA}.scope_epochs x "
        "WHERE x.tenant_id = p.tenant_id AND x.operation_ref = p.operation_ref "
        "AND x.epoch <= p.epoch)"
    ):
        problems.append(f"publication {r} rests on a snapshot older than its epoch evidence")
    return problems


def reproduce_all(reader: PgStore) -> tuple[int, int, list[str], list[str]]:
    """Replay every stored evaluation with its exact engine. Returns (total, reproduced,
    not reproducible for lack of a trusted runtime, mismatches)."""
    rows = reader.conn.execute(
        f"SELECT tenant_id, evaluation_id FROM {SCHEMA}.evaluations ORDER BY 1, 2"
    ).fetchall()
    reader.conn.commit()
    reproduced, missing, mismatches = 0, [], []
    for tenant_id, evaluation_id in rows:
        stored = reader.load_evaluation(str(tenant_id), str(evaluation_id))
        ref = stored.versions.engine_ref
        engine = TRUSTED_ENGINES.get(ref)
        if engine is None:
            why = BLOCKED_ENGINES.get(ref, "not available locally")
            missing.append(f"{evaluation_id}: engine {ref} not replayed ({why})")
            continue
        replayed = engine(reader.load_inputs(str(tenant_id), stored.snapshot_id)).result
        if replayed != stored:
            mismatches.append(f"{evaluation_id}: replay with {ref} differs from the stored result")
        else:
            reproduced += 1
    return len(rows), reproduced, missing, mismatches


def verify_restore(
    admin_dsn: str, database: str, manifest: BackupManifest, dump_path: Path | None = None
) -> RestoreReport:
    reasons: list[str] = []
    integrity_failed = False
    if dump_path is not None:
        if hashlib.sha256(dump_path.read_bytes()).hexdigest() != manifest.dump_sha256:
            reasons.append("dump sha256 differs from the manifest")
            integrity_failed = True
    with psycopg.connect(_dsn_for(admin_dsn, database)) as admin:
        with admin.transaction():
            admin.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            stored = stored_migrations(admin)
            trusted = trusted_migrations()
            if stored != trusted:
                reasons.append(
                    f"applied migrations {sorted(stored.items())} differ from the trusted code "
                    f"{sorted(trusted.items())}"
                )
                integrity_failed = True
            if stored != manifest.migrations:
                reasons.append("applied migrations differ from the manifest")
                integrity_failed = True
            tables, sequences = state_digest(admin)
            for name in sorted(set(tables) | set(manifest.tables)):
                if tables.get(name) != manifest.tables.get(name):
                    reasons.append(
                        f"table {name}: {tables.get(name)} != manifest {manifest.tables.get(name)}"
                    )
                    integrity_failed = True
            for problem in _sequence_problems(admin, sequences, manifest.sequences):
                reasons.append(problem)
                integrity_failed = True
            privileges = privilege_matrix(admin)
            for role in ROLES:
                if sorted(privileges.get(role, [])) != sorted(manifest.privileges.get(role, [])):
                    reasons.append(f"effective privileges of {role} differ from the manifest")
                    integrity_failed = True
            for problem in check_invariants(admin):
                reasons.append(f"invariant: {problem}")
                integrity_failed = True
    with psycopg.connect(_dsn_for(admin_dsn, database)) as conn:
        reader = PgStore(conn, role=READER_ROLE)
        total, reproduced, missing, mismatches = reproduce_all(reader)
    reasons.extend(mismatches)
    reasons.extend(missing)
    if mismatches:
        integrity_failed = True
    status: Literal["RESTORE_VERIFIED", "RESTORE_INCOMPLETE", "RESTORE_MISMATCH"]
    if integrity_failed:
        status = "RESTORE_MISMATCH"
    elif missing:
        status = "RESTORE_INCOMPLETE"
    else:
        status = "RESTORE_VERIFIED"
    return RestoreReport(
        status=status,
        database=database,
        evaluations=total,
        reproduced=reproduced,
        not_reproducible=missing,
        reasons=reasons,
    )


def _sequence_problems(
    conn: Conn, restored: dict[str, int | None], recorded: dict[str, int | None]
) -> list[str]:
    """Sequences are not transactional: pg_dump stores their value when it reads them,
    which may be past the exported snapshot if writers continued. Gaps are fine; reuse is
    not. Each restored sequence must reach the manifest value and every id in its table."""
    problems = []
    if set(restored) != set(recorded):
        problems.append(f"sequences {sorted(restored)} != manifest {sorted(recorded)}")
    identity = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND is_identity = 'YES'",
        (SCHEMA,),
    ).fetchall()
    for table, column in identity:
        seq_row = conn.execute(
            "SELECT pg_get_serial_sequence(%s, %s)", (f"{SCHEMA}.{table}", column)
        ).fetchone()
        name = str(seq_row[0]).rpartition(".")[2].strip('"') if seq_row and seq_row[0] else ""
        max_row = conn.execute(
            sql.SQL("SELECT max({}) FROM {}.{}").format(
                sql.Identifier(column), sql.Identifier(SCHEMA), sql.Identifier(table)
            )
        ).fetchone()
        used = int(max_row[0]) if max_row and max_row[0] is not None else 0
        value = restored.get(name) or 0
        if value < used:
            problems.append(f"sequence {name} at {value} would reuse ids up to {used}")
        if value < (recorded.get(name) or 0):
            problems.append(f"sequence {name} at {value} is behind the manifest")
    return problems


def _dsn_for(admin_dsn: str, database: str) -> str:
    return with_database(admin_dsn, database)


@dataclass
class HistorySnapshot:
    """Every row of every table, for append-only checks around a failure."""

    rows: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def take(cls, conn: Conn) -> HistorySnapshot:
        conn.execute("SET TIME ZONE 'UTC'")
        snapshot = cls()
        for table, _ in _tables(conn):
            query = sql.SQL("SELECT row_to_json(t)::text FROM {}.{} t").format(
                sql.Identifier(SCHEMA), sql.Identifier(table)
            )
            snapshot.rows[table] = {str(r[0]) for r in conn.execute(query)}
        conn.commit()
        return snapshot

    def lost_since(self, earlier: HistorySnapshot) -> dict[str, int]:
        """Rows present earlier and missing or changed now (must be empty)."""
        return {
            table: len(rows - self.rows.get(table, set()))
            for table, rows in earlier.rows.items()
            if rows - self.rows.get(table, set())
        }
