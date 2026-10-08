"""Provisioning that lets a migrator without superuser apply the migrations unchanged.

Migration ``0001`` transfers every table to ``invaria_owner`` (``ALTER TABLE ... OWNER TO``).
PostgreSQL allows that to a non-superuser only if it can ``SET ROLE`` to the new owner and
the new owner has ``CREATE`` on the schema. The migration runner creates the schema as the
migrator, and nothing grants ``invaria_owner`` CREATE on it before the transfer, so on a
managed server (e.g. Neon) the first migration stops there. Migrations are immutable
(sha256), so the requirement is met beforehand:

- the three NOLOGIN roles exist, as the migrations would create them;
- the migrator is a member of ``invaria_owner`` (SET and INHERIT: it acts as the owner, as
  a superuser would) and of ``invaria_app`` and ``invaria_reader`` (SET only: the store
  connects as the migrator and switches with ``SET ROLE``);
- the database has the schema ``invaria`` already owned by ``invaria_owner``.

These are the MIGRATOR's privileges. The application should log in as a role that is only a
member of ``invaria_app`` (or ``invaria_reader``): such a role cannot become the owner.
Nothing is granted to ``invaria_app``, ``invaria_reader`` or PUBLIC: their final privileges
are the ones the migrations grant.

Before acting, existing roles are checked: a role with attributes or memberships the
migrations would never give it (an owner that can log in, an application role that is a
member of the owner, a role that can create roles...) is refused with ``ProvisionError``,
for a superuser too, and nothing is changed. For a superuser nothing else is done.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg import sql

OWNER_ROLE = "invaria_owner"
SWITCH_ROLES = ("invaria_app", "invaria_reader")
ROLES = (OWNER_ROLE, *SWITCH_ROLES)
SCHEMA = "invaria"
# Attributes the migrations never give these roles: any of them widens what they can do.
_FORBIDDEN = ("rolsuper", "rolcreaterole", "rolcreatedb", "rolreplication", "rolbypassrls")


class ProvisionError(Exception):
    """The server's roles are incompatible with the migrations; nothing was changed."""


def role_problems(conn: psycopg.Connection[Any]) -> list[str]:
    """Why the existing roles cannot be used as the migrations expect (empty: they can)."""
    problems: list[str] = []
    me = conn.execute("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
    row = me.fetchone()
    assert row is not None
    migrator, superuser = str(row[0]), bool(row[1])
    if migrator in ROLES:
        problems.append(f"the migrator must not be {migrator} itself")
    existing = {
        str(r[0]): r
        for r in conn.execute(
            f"SELECT rolname, rolcanlogin, {', '.join(_FORBIDDEN)} FROM pg_roles "
            "WHERE rolname = ANY(%s)",
            (list(ROLES),),
        ).fetchall()
    }
    for role, attrs in sorted(existing.items()):
        for name, value in zip(_FORBIDDEN, attrs[2:], strict=True):
            if value:
                problems.append(f"{role} has {name.removeprefix('rol').upper()}")
        if role == OWNER_ROLE and attrs[1]:
            problems.append(f"{role} can log in (the owner must be NOLOGIN)")
    for member in SWITCH_ROLES:
        for target in ROLES:
            if member == target or member not in existing or target not in existing:
                continue
            reach = conn.execute(
                "SELECT pg_has_role(%s, %s, 'MEMBER')", (member, target)
            ).fetchone()
            if reach and reach[0]:
                problems.append(f"{member} is a member of {target}")
    if not superuser:
        for role in sorted(existing):
            granted = conn.execute(
                "SELECT pg_has_role(current_user, %s, 'SET') OR EXISTS ("
                " SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid"
                " WHERE r.rolname = %s AND m.admin_option AND m.member ="
                " (SELECT oid FROM pg_roles WHERE rolname = current_user))",
                (role, role),
            ).fetchone()
            if not (granted and granted[0]):
                problems.append(f"the migrator can neither SET ROLE {role} nor grant it (ADMIN)")
    return problems


def prepare_for_migrator(conn: psycopg.Connection[Any]) -> list[str]:
    """Run on the target database, before ``migrate``. Returns the actions taken (empty for
    a superuser, and when already prepared). Raises ``ProvisionError`` before changing
    anything if the existing roles are incompatible."""
    with conn.transaction():
        problems = role_problems(conn)
        if problems:
            raise ProvisionError("; ".join(problems))
        row = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        superuser = row.fetchone()
        if superuser is not None and superuser[0]:
            return []
        actions: list[str] = []
        for role in ROLES:
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                continue
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            actions.append(f"created role {role}")
        for role, inherit in ((OWNER_ROLE, True), *((r, False) for r in SWITCH_ROLES)):
            if conn.execute(
                "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
                "WHERE r.rolname = %s AND m.member = (SELECT oid FROM pg_roles "
                "WHERE rolname = current_user) AND m.set_option AND m.inherit_option = %s",
                (role, inherit),
            ).fetchone():
                continue
            conn.execute(
                sql.SQL("GRANT {} TO CURRENT_USER WITH SET TRUE, INHERIT {}").format(
                    sql.Identifier(role), sql.SQL("TRUE" if inherit else "FALSE")
                )
            )
            actions.append(f"migrator member of {role} (SET{', INHERIT' if inherit else ''})")
        owner = conn.execute(
            "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = %s", (SCHEMA,)
        ).fetchone()
        if owner is None:
            conn.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                    sql.Identifier(SCHEMA), sql.Identifier(OWNER_ROLE)
                )
            )
            actions.append(f"created schema {SCHEMA} owned by {OWNER_ROLE}")
        elif owner[0] != OWNER_ROLE:
            raise ProvisionError(f"schema {SCHEMA} exists and is owned by {owner[0]}")
    return actions
