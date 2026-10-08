"""Serve the console on 127.0.0.1 only, single-threaded (one database connection).

The request log never contains the query string, so the sign-in token is not logged.
"""

from __future__ import annotations

import secrets
import sys
from typing import Any, TextIO
from urllib.parse import urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from invaria.console.app import ConsoleApp
from invaria.query.service import QueryService

HOST = "127.0.0.1"


class _QuietHandler(WSGIRequestHandler):
    timeout = 10  # single-threaded: an idle or slow client must not block the console

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        path = urlsplit(getattr(self, "path", "")).path
        sys.stderr.write(f"{self.command} {path} {code}\n")

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("console: malformed request rejected\n")


def build(service: QueryService, port: int, token: str | None = None) -> tuple[WSGIServer, str]:
    """Bind to loopback; return the server and the sign-in URL (it carries the token)."""
    token = token or secrets.token_urlsafe(32)
    server = make_server(HOST, port, lambda e, s: [], handler_class=_QuietHandler)
    bound = server.server_port
    app = ConsoleApp(
        service,
        token=token,
        allowed_hosts={f"{HOST}:{bound}", f"localhost:{bound}"},
        cookie_name=f"invaria_console_{bound}",
    )
    server.set_app(app)
    return server, f"http://{HOST}:{bound}/login?token={token}"


def serve(service: QueryService, port: int, out: TextIO = sys.stdout) -> None:
    server, url = build(service, port)
    out.write(f"Invaria evidence console (read-only). Open: {url}\n")
    out.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
