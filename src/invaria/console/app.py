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

from invaria.console import analytics_pages, disclosure_pages, html, pages
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
    # Registered integrity reports.
    "REPORT_INCONSISTENT_WITH_STORE": 409,  # a pinned reference is missing: nothing served
    "INTEGRITY_ERROR": 500,  # the stored document fails its checks (corruption)
    "UNSUPPORTED_REPORT_VERSION": 501,  # a format this code cannot read (not corruption)
    # Event feed.
    "CURSOR_RESET": 409,  # the cursor's feed generation is not this one (restored history)
    "CURSOR_BEYOND_FEED": 409,  # same generation, position ahead of the head
    "UNSUPPORTED_EVENT_VERSION": 501,
    "EVENT_EXCEEDS_MAX_BYTES": 422,  # recoverable: retry with a larger max_bytes
    # Controlled disclosure: no policy of the access profile covers this output.
    "DISCLOSURE_POLICY_UNAVAILABLE": 403,
}
_MAX_BYTES = re.compile(r"[1-9][0-9]{0,8}")
_LIMIT = re.compile(r"[1-9][0-9]{0,3}")


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
        # Navigation and internal links follow what this access profile can obtain;
        # the profile is fixed for the process, so this is computed once.
        self.nav_items, self.home = pages.nav_for(service.allows)
        self.link_filter = pages.link_filter(service.allows)

    # ----------------------------------------------------------------- WSGI

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> list[bytes]:
        path = environ.get("PATH_INFO") or "/"
        api = path == "/v1" or path.startswith("/v1/")
        request_id = uuid.uuid4().hex[:16]
        links = html.LINK_FILTER.set(self.link_filter)
        try:
            response = self._handle(environ, path, api)
        except (HttpError, QueryError) as error:
            response = self._error(error.code, error.message, api, request_id, error)
        except Exception as error:  # never leak a traceback or skip the security headers
            environ["wsgi.errors"].write(f"console: internal error {type(error).__name__}\n")
            response = self._error("INTERNAL_ERROR", "internal error", api, request_id, error)
        finally:
            html.LINK_FILTER.reset(links)
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
        details = getattr(error, "details", None)
        extra = [("Allow", "GET, HEAD")] if code == "METHOD_NOT_ALLOWED" else []
        if api:
            payload: dict[str, Any] = {
                "code": code,
                "message": message,
                "retryable": retryable,
                "request_id": request_id,
            }
            if details:
                payload["details"] = details
            body = json.dumps({"error": payload}, sort_keys=True).encode("utf-8")
            return Response(status, "application/json; charset=utf-8", body, extra)
        return Response(
            status,
            "text/html; charset=utf-8",
            pages.error_page(status, code, message, self.nav_items, self.home),
            extra,
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
    def _optional(query: dict[str, str], name: str) -> str | None:
        if name not in query:
            return None
        if not query[name]:
            raise HttpError("VALIDATION_ERROR", f"query parameter {name!r} is empty")
        return query[name]

    @classmethod
    def _limit(cls, query: dict[str, str]) -> int | None:
        value = cls._optional(query, "limit")
        if value is None:
            return None
        if not _LIMIT.fullmatch(value):
            raise HttpError("VALIDATION_ERROR", "limit must be a positive integer")
        return int(value)

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
            [("Location", self.home), ("Set-Cookie", cookie)],
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
            case ["events"]:
                self._params(q, "start", "cursor", "types", "limit", "max_bytes")
                max_bytes = self._optional(q, "max_bytes")
                if max_bytes is not None and not _MAX_BYTES.fullmatch(max_bytes):
                    raise HttpError("VALIDATION_ERROR", "max_bytes must be a positive integer")
                return self._json(
                    svc.list_events(
                        self._optional(q, "start"),
                        self._optional(q, "cursor"),
                        self._optional(q, "types"),
                        self._limit(q),
                        None if max_bytes is None else int(max_bytes),
                    )
                )
            case ["integrity-reports"]:
                self._params(q, "cursor", "limit")
                return self._json(
                    svc.list_integrity_reports(self._optional(q, "cursor"), self._limit(q))
                )
            case ["integrity-reports", report_id]:
                self._params(q)
                return self._json(svc.get_integrity_report_summary(report_id))
            case ["integrity-reports", report_id, "cases"]:
                self._params(q, "key", "cursor", "limit")
                return self._json(
                    svc.registered_integrity_cases(
                        report_id,
                        self._required(q, "key"),
                        self._optional(q, "cursor"),
                        self._limit(q),
                    )
                )
            # Controlled disclosure: views issued to this principal only.
            case ["disclosures"]:
                self._params(q)
                return self._json(svc.list_disclosures())
            case ["disclosures", view_id]:
                self._params(q)
                return self._json(svc.get_disclosure(view_id))
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
            # Analytics: the same use cases as /v1, presented; nothing computed here.
            case ["reports"]:
                self._params(q, "cursor")
                return self._page(
                    analytics_pages.reports_page(
                        svc.list_integrity_reports(self._optional(q, "cursor"))
                    )
                )
            case ["reports", report_id]:
                self._params(q)
                return self._page(
                    analytics_pages.summary_page(svc.get_integrity_report_summary(report_id))
                )
            case ["reports", report_id, "cases"]:
                self._params(q, "key", "cursor")
                key = self._required(q, "key")
                cases = svc.registered_integrity_cases(report_id, key, self._optional(q, "cursor"))
                summary = svc.get_integrity_report_summary(report_id)
                return self._page(analytics_pages.cases_page(summary, cases, key))
            case ["events"]:
                return self._events_page(q)
            case ["disclosures"]:
                self._params(q)
                return self._page(
                    disclosure_pages.list_page(svc.list_disclosures(), self.nav_items, self.home)
                )
            case ["disclosures", view_id]:
                self._params(q)
                return self._page(
                    disclosure_pages.view_page(
                        svc.get_disclosure(view_id), self.nav_items, self.home
                    )
                )
        raise HttpError("NOT_FOUND", "no such page")

    def _events_page(self, q: dict[str, str]) -> Response:
        boxes = {name: value for name, value, _ in analytics_pages.EVENT_TYPES}
        self._params(q, "start", "cursor", "types", "limit", *boxes)
        start, cursor = self._optional(q, "start"), self._optional(q, "cursor")
        if start is None and cursor is None:
            if q:
                raise HttpError("VALIDATION_ERROR", "choose where to start: start or cursor")
            return self._page(analytics_pages.events_intro_page())
        types = self._optional(q, "types")
        chosen = [boxes[name] for name in boxes if name in q]
        if chosen:
            if types is not None:
                raise HttpError("VALIDATION_ERROR", "use either types or the type boxes")
            types = ",".join(chosen)
        try:
            view = self.service.list_events(start, cursor, types, self._limit(q))
        except QueryError as error:
            if error.code in ("CURSOR_RESET", "CURSOR_BEYOND_FEED"):
                return Response(
                    409,
                    "text/html; charset=utf-8",
                    analytics_pages.cursor_problem_page(error.code, error.message, types),
                    [],
                )
            raise
        return self._page(analytics_pages.events_page(view))
