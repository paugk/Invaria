"""Versioned SQL migrations with immutability check (sha256 of each applied file)."""

from __future__ import annotations

import hashlib
from importlib import resources

import psycopg

MIGRATIONS_PACKAGE = "invaria.persistence.migrations"
_LOCK_KEY = 0x1A7A_0006


class MigrationTampered(Exception):
    """An applied migration's content changed; schema history must not be rewritten."""


def available_migrations() -> list[tuple[str, bytes]]:
    files = resources.files(MIGRATIONS_PACKAGE)
    names = sorted(f.name for f in files.iterdir() if f.name.endswith(".sql"))
    return [(name.removesuffix(".sql"), files.joinpath(name).read_bytes()) for name in names]


def migrate(
    conn: psycopg.Connection[tuple[object, ...]], migrations: list[tuple[str, bytes]] | None = None
) -> list[str]:
    """Apply pending migrations in order; return the versions applied now."""
    applied_now: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        conn.execute("CREATE SCHEMA IF NOT EXISTS invaria")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS invaria.schema_migrations ("
            " version text PRIMARY KEY, sha256 char(64) NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT clock_timestamp())"
        )
        rows = conn.execute("SELECT version, sha256 FROM invaria.schema_migrations").fetchall()
        done: dict[str, str] = {str(row[0]): str(row[1]) for row in rows}
        for version, sql in migrations or available_migrations():
            digest = hashlib.sha256(sql).hexdigest()
            if version in done:
                if done[version] != digest:
                    raise MigrationTampered(f"migration {version} changed after being applied")
                continue
            conn.execute(sql.decode("utf-8"))
            conn.execute(
                "INSERT INTO invaria.schema_migrations (version, sha256) VALUES (%s, %s)",
                (version, digest),
            )
            applied_now.append(version)
    return applied_now
