"""Changing the target database keeps every other connection parameter (no server needed).

The credentials below are FAKE: nothing connects anywhere.
"""

from __future__ import annotations

import pytest
from psycopg.conninfo import conninfo_to_dict

from invaria.persistence.dsn import with_database

FAKE = "fake-password-not-a-secret"


@pytest.mark.parametrize(
    "dsn",
    [
        # A managed-server URL: TLS and channel binding live in the query string.
        f"postgresql://tester:{FAKE}@ep-example-123456.eu-central-1.aws.example.invalid:5432"
        "/neondb?sslmode=require&channel_binding=require",
        # Percent-encoded password, options and a TLS root certificate.
        "postgresql://tester:p%40ss%2Fw%3Ard@db.example.invalid/admin"
        "?sslmode=verify-full&sslrootcert=/etc/ssl/root.crt&options=-c%20search_path%3Dx",
        # Several hosts and ports.
        f"postgresql://tester:{FAKE}@a.example.invalid:5433,b.example.invalid:5434/admin"
        "?target_session_attrs=read-write&connect_timeout=7",
        # Key/value form.
        f"host=127.0.0.1 port=55432 user=postgres password={FAKE} dbname=postgres "
        "sslmode=disable application_name=invaria-tests",
        # No database named: the target is added, nothing else.
        f"postgresql://tester:{FAKE}@db.example.invalid?sslmode=require",
    ],
)
def test_only_the_database_changes(dsn: str) -> None:
    before = conninfo_to_dict(dsn)
    after = conninfo_to_dict(with_database(dsn, "invaria_test_0123abcd"))
    assert after == {**before, "dbname": "invaria_test_0123abcd"}


def test_tls_parameters_survive_a_url_query() -> None:
    dsn = f"postgresql://tester:{FAKE}@db.example.invalid/neondb?sslmode=require&channel_binding=require"
    after = conninfo_to_dict(with_database(dsn, "other"))
    assert (after["sslmode"], after["channel_binding"]) == ("require", "require")
    assert after["password"] == FAKE  # carried over, never printed


def test_a_database_name_is_not_parsed_as_parameters() -> None:
    dsn = f"postgresql://tester:{FAKE}@db.example.invalid/admin?sslmode=require"
    after = conninfo_to_dict(with_database(dsn, "odd name?sslmode=disable"))
    assert after["dbname"] == "odd name?sslmode=disable"
    assert after["sslmode"] == "require"
