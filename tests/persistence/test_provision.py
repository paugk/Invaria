"""Provisioning for a migrator without superuser (``persistence/provision.py``).

Opt-in like the other PostgreSQL tests (INVARIA_TEST_DATABASE_URL, an admin URL). Each test
creates a disposable NON-superuser migrator (LOGIN, CREATEDB, CREATEROLE) holding only ADMIN
on the three Invaria roles, so the path a managed server takes is exercised even when the
admin is a superuser. Role changes made to test refusals are reverted; the migrator, its
database and its login roles are dropped. Passwords are generated here and never printed.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from invaria.persistence.migrate import available_migrations, migrate
from invaria.persistence.provision import ROLES, ProvisionError, prepare_for_migrator

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")


def _as(dsn: str, **change: str) -> str:
    return make_conninfo("", **{**conninfo_to_dict(dsn), **change})


@dataclass
class Migrator:
    name: str
    database: str
    dsn: str

    def connect(self, **kwargs: Any) -> psycopg.Connection[Any]:
        return psycopg.connect(self.dsn, **kwargs)


@pytest.fixture
def migrator() -> Iterator[Migrator]:
    if not ADMIN_DSN:
        pytest.skip("set INVARIA_TEST_DATABASE_URL to an admin PostgreSQL URL to run DB tests")
    tag = uuid.uuid4().hex[:10]
    name, database, password = f"invaria_mig_{tag}", f"invaria_prov_{tag}", secrets.token_hex(24)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        for role in ROLES:  # as the migrations create them
            admin.execute(
                sql.SQL(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {r}) "
                    "THEN CREATE ROLE {i} NOLOGIN; END IF; END $$"
                ).format(r=sql.Literal(role), i=sql.Identifier(role))
            )
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN CREATEDB CREATEROLE PASSWORD {}").format(
                sql.Identifier(name), sql.Literal(password)
            )
        )
        for role in ROLES:  # may grant, but is not yet a member it can act as
            admin.execute(
                sql.SQL("GRANT {} TO {} WITH ADMIN TRUE, SET FALSE, INHERIT FALSE").format(
                    sql.Identifier(role), sql.Identifier(name)
                )
            )
    own = _as(ADMIN_DSN, user=name, password=password)
    with psycopg.connect(own, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        yield Migrator(name, database, _as(own, dbname=database))
    finally:
        with psycopg.connect(own, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )
            for (login,) in conn.execute(
                "SELECT rolname FROM pg_roles WHERE rolname LIKE %s", (f"%\\_login\\_{tag}",)
            ).fetchall():
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def roles_state(conn: psycopg.Connection[Any], migrator: str) -> dict[str, Any]:
    """Attributes and memberships of the three roles and of the migrator, and the schema."""
    names = [*ROLES, migrator]
    return {
        "attributes": conn.execute(
            "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, "
            "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = ANY(%s) ORDER BY 1",
            (names,),
        ).fetchall(),
        "memberships": conn.execute(
            "SELECT g.rolname, m.rolname, a.admin_option, a.set_option, a.inherit_option "
            "FROM pg_auth_members a JOIN pg_roles g ON g.oid = a.roleid "
            "JOIN pg_roles m ON m.oid = a.member WHERE m.rolname = ANY(%s) ORDER BY 1, 2, 3",
            (names,),
        ).fetchall(),
        "schema": conn.execute(
            "SELECT nspowner::regrole::text, nspacl::text FROM pg_namespace "
            "WHERE nspname = 'invaria'"
        ).fetchall(),
    }


def test_a_non_superuser_migrator_applies_the_migrations_unchanged(migrator: Migrator) -> None:
    with migrator.connect(autocommit=True) as conn:
        superuser = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        assert superuser.fetchone() == (False,)
        actions = prepare_for_migrator(conn)
        assert actions == [
            "migrator member of invaria_owner (SET, INHERIT)",
            "migrator member of invaria_app (SET)",
            "migrator member of invaria_reader (SET)",
            "created schema invaria owned by invaria_owner",
        ]
    with migrator.connect() as conn:
        assert migrate(conn) == [
            "0001_initial",
            "0002_revision",
            "0003_reader",
            "0004_integrity_reports",
            "0005_domain_events",
            "0006_feed_generations",
            "0007_disclosures",
        ]
        stored = dict(conn.execute("SELECT version, sha256 FROM invaria.schema_migrations"))
        assert stored == {v: hashlib.sha256(b).hexdigest() for v, b in available_migrations()}
    with migrator.connect(autocommit=True) as conn:
        before = roles_state(conn, migrator.name)
        assert prepare_for_migrator(conn) == []  # repeating grants nothing more
        assert prepare_for_migrator(conn) == []
        assert roles_state(conn, migrator.name) == before
        # Only the migrator gained memberships; the three roles are members of nothing.
        assert [m for m in before["memberships"] if m[1] in ROLES] == []


def _check(conn: psycopg.Connection[Any], statement: str, allowed: bool | str) -> None:
    try:
        if allowed == "fk":  # privileges pass; the row is refused by its foreign key
            with pytest.raises(errors.ForeignKeyViolation):
                conn.execute(statement)
        elif allowed:
            conn.execute(statement)
        else:
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(statement)
    finally:
        conn.rollback()


REPORT_INSERT = (
    "INSERT INTO invaria.integrity_reports (tenant_id, report_id, report_sha256, report_format, "
    "definition, selection_policy, evidence_horizon, registered_by, document, document_sha256) "
    "SELECT 't', 'ir-' || repeat('a', 64), repeat('a', 64), 'f', 'd', 'p', now(), 'x', "
    "'\\x7b7d'::bytea, encode(sha256('\\x7b7d'::bytea), 'hex')"
)
ISSUANCE_INSERT = (
    "INSERT INTO invaria.disclosure_issuances (tenant_id, recipient_principal, policy_ref, "
    "view_format, report_id, issued_by) VALUES ('t', 'r', 'p@1', 'f', 'ir-x', 'o')"
)
EVENT_INSERT_WITH_SEQ = (
    "INSERT INTO invaria.domain_events (tenant_seq, tenant_id, event_id, event_type, "
    "type_version, operation_ref, epoch, source_event_seq, payload, payload_sha256) "
    "SELECT 1, 't', 'ev-' || repeat('a', 64), 'operation.scope_reopened', 1, 'op', 1, 1, "
    "'\\x7b7d'::bytea, encode(sha256('\\x7b7d'::bytea), 'hex')"
)
APP: dict[str, bool | str] = {
    "SELECT count(*) FROM invaria.evaluations": True,
    "INSERT INTO invaria.knowledge_cuts (tenant_id, known_at) VALUES ('t', now())": True,
    "UPDATE invaria.evaluations SET result = 'MATCH'": False,
    "DELETE FROM invaria.outbox_acks": False,
    "TRUNCATE invaria.publications": False,
    "INSERT INTO invaria.schema_migrations (version, sha256) VALUES ('x', repeat('0', 64))": False,
    "CREATE TABLE invaria.extra (x int)": False,
    # The registry is append-only and the store clock sets registered_at.
    REPORT_INSERT: True,
    REPORT_INSERT.replace("document_sha256)", "document_sha256, registered_at)").replace(
        "'hex')", "'hex'), now() - interval '1 day'"
    ): False,
    "UPDATE invaria.integrity_reports SET registered_by = 'x'": False,
    "DELETE FROM invaria.integrity_reports": False,
    "TRUNCATE invaria.integrity_reports": False,
    # Events are append-only and numbered by the database, never by the app.
    "SELECT count(*) FROM invaria.domain_events": True,
    EVENT_INSERT_WITH_SEQ: False,
    "UPDATE invaria.domain_events SET payload = payload": False,
    "DELETE FROM invaria.domain_events": False,
    "TRUNCATE invaria.domain_events": False,
    "SELECT count(*) FROM invaria.domain_event_counters": False,
    "INSERT INTO invaria.domain_event_counters VALUES ('t', 5)": False,
    "UPDATE invaria.domain_event_counters SET last_seq = 9": False,
    "SELECT invaria.assign_domain_event_seq()": False,
    # The feed generation is read, never written, by the application.
    "SELECT count(*) FROM invaria.feed_generations": True,
    "INSERT INTO invaria.feed_generations (reason) VALUES ('restore:x')": False,
    "UPDATE invaria.feed_generations SET reason = 'initial'": False,
    "DELETE FROM invaria.feed_generations": False,
    # Issuances and revocations are append-only; the store sets ids and times.
    # (The INSERT fails on its foreign key, after the privilege check: allowed means
    # "not refused for privileges", see _check_fk.)
    ISSUANCE_INSERT: "fk",
    ISSUANCE_INSERT.replace("(tenant_id,", "(view_id, tenant_id,").replace(
        "VALUES ('t',", "VALUES ('dv-' || repeat('a', 32), 't',"
    ): False,
    ISSUANCE_INSERT.replace("issued_by)", "issued_by, issued_at)").replace(
        "'o')", "'o', now())"
    ): False,
    "INSERT INTO invaria.disclosure_revocations (view_id, tenant_id, revoked_by) "
    "VALUES ('dv-x', 't', 'o')": "fk",
    "INSERT INTO invaria.disclosure_revocations (view_id, tenant_id, revoked_by, revoked_at) "
    "VALUES ('dv-x', 't', 'o', now())": False,
    "SELECT count(*) FROM invaria.disclosure_issuances": True,
    "UPDATE invaria.disclosure_issuances SET policy_ref = 'x@1'": False,
    "DELETE FROM invaria.disclosure_revocations": False,
    "TRUNCATE invaria.disclosure_issuances": False,
}
READER: dict[str, bool | str] = {
    "SELECT count(*) FROM invaria.evaluations": True,
    "SELECT count(*) FROM invaria.schema_migrations": False,
    "INSERT INTO invaria.knowledge_cuts (tenant_id, known_at) VALUES ('t', now())": False,
    "UPDATE invaria.publications SET epoch = 0": False,
    "CREATE TABLE invaria.extra (x int)": False,
    "SELECT count(*) FROM invaria.integrity_reports": True,
    REPORT_INSERT: False,
    "UPDATE invaria.integrity_reports SET registered_by = 'x'": False,
    "DELETE FROM invaria.integrity_reports": False,
    "SELECT count(*) FROM invaria.domain_events": True,
    "SELECT count(*) FROM invaria.domain_event_counters": True,
    EVENT_INSERT_WITH_SEQ.replace("tenant_seq, ", "").replace("1, 't'", "'t'"): False,
    "UPDATE invaria.domain_event_counters SET last_seq = 9": False,
    "SELECT count(*) FROM invaria.feed_generations": True,
    "INSERT INTO invaria.feed_generations (reason) VALUES ('restore:x')": False,
    "SELECT count(*) FROM invaria.disclosure_issuances": True,
    "SELECT count(*) FROM invaria.disclosure_revocations": True,
    ISSUANCE_INSERT: False,
    "DELETE FROM invaria.disclosure_revocations": False,
}


def test_effective_permissions_after_set_role_and_for_application_logins(
    migrator: Migrator,
) -> None:
    with migrator.connect(autocommit=True) as conn:
        prepare_for_migrator(conn)
    with migrator.connect() as conn:
        migrate(conn)
    # The migrator's session after SET ROLE: the application roles' privileges, nothing more.
    for role, statements in (("invaria_app", APP), ("invaria_reader", READER)):
        with migrator.connect() as conn:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            conn.commit()
            for statement, allowed in statements.items():
                _check(conn, statement, allowed)
    # The application logs in as a member of invaria_app (or invaria_reader) only: it has
    # those privileges and cannot become the owner, the other role or the migrator.
    tag = migrator.name.removeprefix("invaria_mig_")
    password = secrets.token_hex(24)
    with migrator.connect(autocommit=True) as conn:
        for role in ("invaria_app", "invaria_reader"):
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                    sql.Identifier(f"{role}_login_{tag}"),
                    sql.Literal(password),
                    sql.Identifier(role),
                )
            )
    for role, statements in (("invaria_app", APP), ("invaria_reader", READER)):
        dsn = _as(migrator.dsn, user=f"{role}_login_{tag}", password=password)
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            conn.commit()
            for statement, allowed in statements.items():
                _check(conn, statement, allowed)
            for target in {"invaria_owner", "invaria_app", "invaria_reader", migrator.name} - {
                role
            }:
                with pytest.raises(errors.InsufficientPrivilege):
                    conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(target)))
                conn.rollback()
            reach = conn.execute(
                "SELECT pg_has_role(session_user, 'invaria_owner', 'MEMBER')"
            ).fetchone()
            assert reach == (False,)


@pytest.mark.parametrize(
    ("change", "revert", "problem"),
    [
        (
            "ALTER ROLE invaria_owner LOGIN",
            "ALTER ROLE invaria_owner NOLOGIN",
            "invaria_owner can log in",
        ),
        (
            "ALTER ROLE invaria_app CREATEDB",
            "ALTER ROLE invaria_app NOCREATEDB",
            "invaria_app has CREATEDB",
        ),
        (
            "GRANT invaria_owner TO invaria_app",
            "REVOKE invaria_owner FROM invaria_app",
            "invaria_app is a member of invaria_owner",
        ),
        (
            "GRANT invaria_app TO invaria_reader",
            "REVOKE invaria_app FROM invaria_reader",
            "invaria_reader is a member of invaria_app",
        ),
        (
            "REVOKE invaria_owner FROM {migrator}",
            "GRANT invaria_owner TO {migrator} WITH ADMIN TRUE, SET FALSE, INHERIT FALSE",
            "the migrator can neither SET ROLE invaria_owner nor grant it (ADMIN)",
        ),
    ],
)
def test_incompatible_roles_are_refused_and_nothing_changes(
    migrator: Migrator, change: str, revert: str, problem: str
) -> None:
    assert ADMIN_DSN is not None
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(change.format(migrator=migrator.name))
        try:
            with migrator.connect(autocommit=True) as conn:
                before = roles_state(conn, migrator.name)
                with pytest.raises(ProvisionError, match=re.escape(problem)):
                    prepare_for_migrator(conn)
                assert roles_state(conn, migrator.name) == before  # nothing granted or created
        finally:
            admin.execute(revert.format(migrator=migrator.name))
