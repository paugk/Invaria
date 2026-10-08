"""Connection strings: change the target database and nothing else."""

from __future__ import annotations

from psycopg.conninfo import conninfo_to_dict, make_conninfo


def with_database(dsn: str, database: str) -> str:
    """The same connection parameters (host, port, user, TLS, options...) for ``database``.

    Parsed with the driver, so a URL query (``sslmode``, ``channel_binding``...) or a
    key/value string keeps every parameter; only ``dbname`` changes. Never log the result:
    it can carry a password.
    """
    params = conninfo_to_dict(dsn)
    params["dbname"] = database
    return make_conninfo("", **params)
