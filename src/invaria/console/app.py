"""Local evidence console: read-only REST (``/v1``) and server-rendered HTML, as one WSGI app.

Both surfaces call the consultative ``QueryService`` (tenant and scopes from the operator's
access profile, read-only database role). Only GET and HEAD exist: nothing here writes,
resolves, waives or re-evaluates. Local hardening: a per-process token (cookie or bearer),
an exact ``Host`` allow-list against DNS rebinding, and strict headers (no scripts).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from pydantic import BaseModel

from invaria.console import pages
from invaria.query.service import QueryError

if TYPE_CHECKING:
    from invaria.query.service import QueryService

_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,256}")
SESSION_SECONDS = 8 * 3600
StartResponse = Callable[[str, list[tuple[str, str]]], Any]

SECURITY_HEADERS = [
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
]
STATUS = {
    "VALIDATION_ERROR": 422,
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
    "MISDIRECTED_REQUEST": 421,
    "SOURCE_UNAVAILABLE": 503,
    "INTERNAL_ERROR": 500,
}


class HttpError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes
    headers: list[tuple[str, str]]


def _status_line(status: int) -> str:
    return f"{status} {HTTPStatus(status).phrase}"


class ConsoleApp:
    def __init__(
        self,
        service: QueryService,
        *,
        token: str,
        allowed_hosts: Iterable[str],
        cookie_name: str = "invaria_console",
    ) -> None:
        if not _TOKEN.fullmatch(token):
            raise ValueError("the console token must be 32-256 URL-safe characters")
        self.service = service
        self.token = token.encode("utf-8")
        # The cookie carries a value derived from the token, not the token itself: a cookie
        # seen elsewhere does not reveal the sign-in token (it still grants a session).
        self.session = hmac.new(self.token, b"invaria-console-session", hashlib.sha256)
        self.session_value = self.session.hexdigest().encode("utf-8")
        self.allowed_hosts = frozenset(allowed_hosts)
        self.cookie_name = cookie_name

    # ----------------------------------------------------------------- WSGI

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> list[bytes]:
        path = environ.get("PATH_INFO") or "/"
        api = path == "/v1" or path.startswith("/v1/")
        request_id = uuid.uuid4().hex[:16]
        try:
            response = self._handle(environ, path, api)
        except (HttpError, QueryError) as error:
            response = self._error(error.code, error.message, api, request_id, error)
        except Exception as error:  # never leak a traceback or skip the security headers
            environ["wsgi.errors"].write(f"console: internal error {type(error).__name__}\n")
            response = self._error("INTERNAL_ERROR", "internal error", api, request_id, error)
        headers = [
            ("Content-Type", response.content_type),
            ("Content-Length", str(len(response.body))),
            *SECURITY_HEADERS,
            *response.headers,
        ]
        if response.status >= 400:
            headers.append(("X-Request-Id", request_id))
        start_response(_status_line(response.status), headers)
        if environ.get("REQUEST_METHOD") == "HEAD":
            return [b""]
        return [response.body]

    def _error(
        self, code: str, message: str, api: bool, request_id: str, error: Exception
    ) -> Response:
        status = STATUS.get(code, 500)
        retryable = bool(getattr(error, "retryable", False))
        extra = [("Allow", "GET, HEAD")] if code == "METHOD_NOT_ALLOWED" else []
        if api:
            body = json.dumps(
                {
                    "error": {
                        "code": code,
                        "message": message,
                        "retryable": retryable,
                        "request_id": request_id,
                    }
                },
                sort_keys=True,
            ).encode("utf-8")
            return Response(status, "application/json; charset=utf-8", body, extra)
        return Response(
            status, "text/html; charset=utf-8", pages.error_page(status, code, message), extra
        )

    # -------------------------------------------------------------- plumbing

    def _handle(self, environ: dict[str, Any], path: str, api: bool) -> Response:
        if environ.get("HTTP_HOST") not in self.allowed_hosts:
            raise HttpError("MISDIRECTED_REQUEST", "unexpected Host header")
        if environ.get("REQUEST_METHOD") not in ("GET", "HEAD"):
            raise HttpError("METHOD_NOT_ALLOWED", "the console is read-only: only GET and HEAD")
        query = self._query(environ.get("QUERY_STRING", ""))
        if path == "/static/console.css":
            self._params(query)
            return Response(200, "text/css; charset=utf-8", pages.CSS, [])
        if path == "/login":
            return self._login(query)
        self._authenticate(environ)
        segments = path.strip("/").split("/") if path != "/" else []
        if any(s == "" for s in segments):
            raise HttpError("NOT_FOUND", "no such page")
        if api:
            return self._api(segments[1:], query)
        return self._html(segments, query)

    @staticmethod
    def _query(raw: str) -> dict[str, str]:
        try:
            parsed = parse_qs(raw, keep_blank_values=True, max_num_fields=8)
        except ValueError as error:
            raise HttpError("VALIDATION_ERROR", "too many query parameters") from error
        if any(len(values) != 1 for values in parsed.values()):
            raise HttpError("VALIDATION_ERROR", "a query parameter is repeated")
        return {k: v[0] for k, v in parsed.items()}

    @staticmethod
    def _params(query: dict[str, str], *allowed: str) -> None:
        unknown = sorted(set(query) - set(allowed))
        if unknown:
            raise HttpError("VALIDATION_ERROR", f"unexpected query parameter {unknown[0]!r}")

    @staticmethod
    def _required(query: dict[str, str], name: str) -> str:
        value = query.get(name, "")
        if not value:
            raise HttpError("VALIDATION_ERROR", f"query parameter {name!r} is required")
        return value

    @staticmethod
    def _same(presented: str | None, expected: bytes) -> bool:
        return presented is not None and hmac.compare_digest(presented.encode("utf-8"), expected)

    def _valid(self, presented: str | None) -> bool:
        return self._same(presented, self.token)

    def _login(self, query: dict[str, str]) -> Response:
        self._params(query, "token")
        if not self._valid(query.get("token")):
            raise HttpError("UNAUTHENTICATED", "invalid or missing console token")
        cookie = (
            f"{self.cookie_name}={self.session_value.decode('utf-8')}; HttpOnly; "
            f"SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"
        )
        return Response(
            303,
            "text/plain; charset=utf-8",
            b"Signed in.\n",
            [("Location", "/"), ("Set-Cookie", cookie)],
        )

    def _authenticate(self, environ: dict[str, Any]) -> None:
        auth = environ.get("HTTP_AUTHORIZATION", "")
        if auth.startswith("Bearer ") and self._valid(auth[len("Bearer ") :]):
            return
        for part in environ.get("HTTP_COOKIE", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == self.cookie_name and self._same(value, self.session_value):
                return
        raise HttpError("UNAUTHENTICATED", "sign in with the address printed at startup")

    @staticmethod
    def _json(model: BaseModel) -> Response:
        return Response(
            200, "application/json; charset=utf-8", model.model_dump_json().encode("utf-8"), []
        )

    @staticmethod
    def _page(body: bytes) -> Response:
        return Response(200, "text/html; charset=utf-8", body, [])

    # ---------------------------------------------------------------- routes

    def _api(self, s: list[str], q: dict[str, str]) -> Response:
        svc = self.service
        match s:
            case ["operations"]:
                self._params(q, "result")
                return self._json(svc.list_operations(q.get("result") or None))
            case ["operations", op]:
                self._params(q)
                return self._json(svc.trace_operation(op))
            case ["operations", op, "timeline"]:
                self._params(q)
                return self._json(svc.get_timeline(op))
            case ["operations", op, "conclusion"]:
                self._params(q, "valid_at", "known_at")
                return self._json(
                    svc.get_conclusion_as_known_at(
                        op, self._required(q, "valid_at"), self._required(q, "known_at")
                    )
                )
            case ["operations", op, "coverage"]:
                self._params(q)
                return self._json(svc.get_coverage(op))
            case ["evaluations", evaluation_id]:
                self._params(q)
                return self._json(svc.get_evaluation(evaluation_id))
            case ["evaluations", evaluation_id, "controls", control_id, "discrepancy"]:
                self._params(q)
                return self._json(svc.explain_discrepancy(evaluation_id, control_id))
            case ["evaluations", evaluation_id, "controls", control_id, "missing-evidence"]:
                self._params(q)
                return self._json(svc.get_missing_evidence(evaluation_id, control_id))
            case ["evaluation-comparisons"]:
                self._params(q, "left", "right")
                return self._json(
                    svc.compare_evaluations(self._required(q, "left"), self._required(q, "right"))
                )
            case ["evidence", evidence_id]:
                self._params(q)
                return self._json(svc.get_evidence(evidence_id))
        raise HttpError("NOT_FOUND", "no such resource")

    def _html(self, s: list[str], q: dict[str, str]) -> Response:
        svc = self.service
        match s:
            case []:
                self._params(q, "result")
                return self._page(
                    pages.operations_page(svc.list_operations(q.get("result") or None))
                )
            case ["operations", op]:
                self._params(q)
                return self._page(
                    pages.operation_page(svc.trace_operation(op), svc.get_timeline(op))
                )
            case ["operations", op, "coverage"]:
                self._params(q)
                return self._page(pages.coverage_page(svc.get_coverage(op)))
            case ["operations", op, "as-known"]:
                self._params(q, "valid_at", "known_at")
                view = svc.get_conclusion_as_known_at(
                    op, self._required(q, "valid_at"), self._required(q, "known_at")
                )
                return self._page(pages.conclusion_page(view, f"{op} as known at {q['known_at']}"))
            case ["evaluations", evaluation_id]:
                self._params(q)
                return self._page(pages.conclusion_page(svc.get_evaluation(evaluation_id)))
            case ["evaluations", evaluation_id, "controls", control_id]:
                self._params(q)
                discrepancy = svc.explain_discrepancy(evaluation_id, control_id)
                missing = svc.get_missing_evidence(evaluation_id, control_id)
                return self._page(pages.control_page(discrepancy, missing))
            case ["compare"]:
                self._params(q, "left", "right")
                comparison = svc.compare_evaluations(
                    self._required(q, "left"), self._required(q, "right")
                )
                return self._page(pages.comparison_page(comparison))
            case ["evidence", evidence_id]:
                self._params(q)
                return self._page(pages.evidence_page(svc.get_evidence(evidence_id)))
        raise HttpError("NOT_FOUND", "no such page")
