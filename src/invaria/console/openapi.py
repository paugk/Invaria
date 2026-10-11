"""OpenAPI 3.1 description of the console's read-only REST ``/v1`` (generated, not served).

Usage: ``uv run python -m invaria.console.openapi [path]`` (default
``docs/api/console-v1.openapi.json``). The committed file is a derived artifact: a test fails
if it drifts from the models or from the routes ``ConsoleApp`` dispatches.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from invaria.contracts.base import Contract, NonEmptyText
from invaria.query import disclosure
from invaria.query.models import (
    AggregateDisclosureView,
    CasesView,
    ComparisonView,
    ConclusionView,
    CoverageReport,
    DisclosureListView,
    DiscrepancyView,
    EventPageView,
    EvidenceView,
    IntegrityReportListView,
    IntegrityReportSummaryView,
    MissingEvidenceView,
    OperationsView,
    TimelineView,
    TraceView,
)

DEFAULT_PATH = Path("docs/api/console-v1.openapi.json")
ErrorCode = Literal[
    "VALIDATION_ERROR",
    "UNAUTHENTICATED",
    "FORBIDDEN",
    "NOT_FOUND",
    "METHOD_NOT_ALLOWED",
    "MISDIRECTED_REQUEST",
    "SOURCE_UNAVAILABLE",
    "INTERNAL_ERROR",
    "REPORT_INCONSISTENT_WITH_STORE",
    "INTEGRITY_ERROR",
    "UNSUPPORTED_REPORT_VERSION",
    "CURSOR_RESET",
    "CURSOR_BEYOND_FEED",
    "UNSUPPORTED_EVENT_VERSION",
    "EVENT_EXCEEDS_MAX_BYTES",
    "DISCLOSURE_POLICY_UNAVAILABLE",
]


class ApiError(Contract):
    code: ErrorCode
    message: NonEmptyText
    retryable: bool
    request_id: NonEmptyText
    # Only some errors carry it, e.g. EVENT_EXCEEDS_MAX_BYTES: required_bytes, position.
    details: dict[str, int] | None = None


class ApiErrorBody(Contract):
    error: ApiError


@dataclass(frozen=True)
class Param:
    name: str
    description: str
    required: bool = False
    pattern: str | None = None


@dataclass(frozen=True)
class Route:
    path: str
    operation_id: str  # the QueryService method the route calls
    response: type[BaseModel]
    scopes: tuple[str, ...]
    summary: str
    query: tuple[Param, ...] = ()
    errors: tuple[int, ...] = (404,)
    path_params: dict[str, str] = field(default_factory=dict)


OP = {"op": "operation_ref"}
CURSOR = Param("cursor", "opaque cursor of the next page, as returned (unsigned; grants nothing)")
LIMIT = Param("limit", "page size, 1..max_items of the access profile", pattern="^[1-9][0-9]{0,3}$")
REPORT = {"report_id": "ir-<report_sha256>; never grants access by itself"}

ROUTES: tuple[Route, ...] = (
    Route(
        "/v1/operations",
        "list_operations",
        OperationsView,
        ("operations:read",),
        "Operations of the tenant with their published result",
        (Param("result", "MATCH, BREAK or UNKNOWN"),),
        errors=(),
    ),
    Route(
        "/v1/operations/{op}",
        "trace_operation",
        TraceView,
        ("operations:read",),
        "Trace of an operation",
        path_params=OP,
    ),
    Route(
        "/v1/operations/{op}/timeline",
        "get_timeline",
        TimelineView,
        ("operations:read",),
        "Epochs, snapshots and evaluations of an operation",
        path_params=OP,
    ),
    Route(
        "/v1/operations/{op}/conclusion",
        "get_conclusion_as_known_at",
        ConclusionView,
        ("operations:read",),
        "Conclusion as known at a knowledge time",
        (
            Param("valid_at", "ISO UTC ending in Z", required=True),
            Param("known_at", "ISO UTC ending in Z", required=True),
        ),
        path_params=OP,
    ),
    Route(
        "/v1/operations/{op}/coverage",
        "get_coverage",
        CoverageReport,
        ("operations:read",),
        "Coverage of the published evaluation",
        path_params=OP,
    ),
    Route(
        "/v1/evaluations/{evaluation_id}",
        "get_evaluation",
        ConclusionView,
        ("operations:read",),
        "A stored evaluation",
        path_params={"evaluation_id": "evaluation identifier"},
    ),
    Route(
        "/v1/evaluations/{evaluation_id}/controls/{control_id}/discrepancy",
        "explain_discrepancy",
        DiscrepancyView,
        ("operations:read",),
        "Operands of a control",
        path_params={"evaluation_id": "evaluation identifier", "control_id": "control"},
    ),
    Route(
        "/v1/evaluations/{evaluation_id}/controls/{control_id}/missing-evidence",
        "get_missing_evidence",
        MissingEvidenceView,
        ("operations:read",),
        "Evidence a control needs",
        path_params={"evaluation_id": "evaluation identifier", "control_id": "control"},
    ),
    Route(
        "/v1/evaluation-comparisons",
        "compare_evaluations",
        ComparisonView,
        ("operations:read",),
        "Compare two evaluations",
        (Param("left", "evaluation id", required=True), Param("right", "evaluation id", True)),
    ),
    Route(
        "/v1/evidence/{evidence_id}",
        "get_evidence",
        EvidenceView,
        ("evidence:read",),
        "One observation (source content is untrusted)",
        path_params={"evidence_id": "observation identifier"},
    ),
    Route(
        "/v1/events",
        "list_events",
        EventPageView,
        ("events:read",),
        "One page of the tenant's event feed by cursor, in tenant_seq (derivation) order. "
        "Each type's content also needs its resource scope (operations:read or reports:read); "
        "events:read reveals the feed's positions and volume",
        (
            Param("start", "beginning or now; exactly one of start or cursor"),
            Param("cursor", "next_cursor of a previous page (authenticated; grants nothing)"),
            Param("types", "comma-separated event types; normalized and bound to the cursor"),
            LIMIT,
            Param("max_bytes", "response budget for the items", pattern="^[1-9][0-9]{0,8}$"),
        ),
        errors=(409, 422, 500, 501),
    ),
    Route(
        "/v1/integrity-reports",
        "list_integrity_reports",
        IntegrityReportListView,
        ("reports:read",),
        "Registered integrity reports of the tenant, newest registration first (not a stable "
        "snapshot across pages)",
        (CURSOR, LIMIT),
        errors=(),
    ),
    Route(
        "/v1/integrity-reports/{report_id}",
        "get_integrity_report_summary",
        IntegrityReportSummaryView,
        ("reports:read",),
        "Summary of a registered report: figures, versions and times, without its cases. "
        "Checks the stored document and that its pinned references exist; the content of "
        "pinned evaluations is checked only when their cases are read. Needs the disclosure "
        "policy full-report-summary@1 in the access profile; otherwise 403 "
        "DISCLOSURE_POLICY_UNAVAILABLE",
        errors=(404, 409, 500, 501),
        path_params=REPORT,
    ),
    Route(
        "/v1/integrity-reports/{report_id}/cases",
        "registered_integrity_cases",
        CasesView,
        ("reports:read", "operations:read"),
        "One page of the pinned operations behind a figure, in operation order",
        (Param("key", "a cases_key listed by the summary", required=True), CURSOR, LIMIT),
        errors=(404, 409, 500, 501),
        path_params=REPORT,
    ),
    Route(
        "/v1/disclosures",
        "list_disclosures",
        DisclosureListView,
        ("disclosures:read",),
        "Views issued to the principal of this access profile, not revoked",
        errors=(),
    ),
    Route(
        "/v1/disclosures/{view_id}",
        "get_disclosure",
        AggregateDisclosureView,
        ("disclosures:read",),
        "A view issued to this principal: the allowlisted aggregates of a registered report, "
        "projected from the immutable report under the policy version fixed at issue. Needs "
        "that policy in the access profile. Carries no identifier, hash or date of the "
        "original; cannot be verified independently of this service",
        errors=(404, 409, 500, 501),
        path_params={"view_id": "dv-<32 hex>; random and opaque, never grants access"},
    ),
)

_ERRORS = {
    401: "UNAUTHENTICATED: missing or invalid console token",
    403: "FORBIDDEN: the access profile lacks a required scope. "
    "DISCLOSURE_POLICY_UNAVAILABLE: no disclosure policy of the access profile covers this "
    "output; refused before anything is read",
    404: "NOT_FOUND: absent, or of another tenant (existence is never confirmed); for a "
    "disclosed view, also issued to another principal or revoked",
    405: "METHOD_NOT_ALLOWED: only GET and HEAD",
    409: "REPORT_INCONSISTENT_WITH_STORE: a pinned reference is missing or, for cases, a "
    "pinned evaluation of the page differs from its sha256; nothing is served. "
    "CURSOR_RESET: the cursor belongs to another feed generation (restored history). "
    "CURSOR_BEYOND_FEED: same generation, position ahead of the feed",
    421: "MISDIRECTED_REQUEST: unexpected Host header",
    422: "VALIDATION_ERROR: invalid parameter, cursor, key or limit. EVENT_EXCEEDS_MAX_BYTES: "
    "the next authorized event does not fit max_bytes (details.required_bytes); retry larger",
    500: "INTEGRITY_ERROR (stored document or event fails its checks) or INTERNAL_ERROR",
    501: "UNSUPPORTED_REPORT_VERSION or UNSUPPORTED_EVENT_VERSION: a stored version this "
    "code cannot read",
    503: "SOURCE_UNAVAILABLE: the evidence store is unavailable (retryable)",
}


def document() -> dict[str, Any]:
    models = sorted({r.response for r in ROUTES} | {ApiErrorBody}, key=lambda m: m.__name__)
    _, defs = models_json_schema(
        [(m, "serialization") for m in models], ref_template="#/components/schemas/{model}"
    )
    paths: dict[str, Any] = {}
    for r in ROUTES:
        parameters = [
            {
                "name": n,
                "in": "path",
                "required": True,
                "description": d,
                "schema": {"type": "string"},
            }
            for n, d in r.path_params.items()
        ] + [
            {
                "name": p.name,
                "in": "query",
                "required": p.required,
                "description": p.description,
                "schema": {"type": "string", **({"pattern": p.pattern} if p.pattern else {})},
            }
            for p in r.query
        ]
        responses: dict[str, Any] = {
            "200": {
                "description": "OK",
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{r.response.__name__}"}
                    }
                },
            }
        }
        for status in sorted({401, 403, 405, 421, 422, 503, *r.errors}):
            responses[str(status)] = {
                "description": _ERRORS[status],
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/ApiErrorBody"}}
                },
            }
        paths[r.path] = {
            "get": {
                "operationId": r.operation_id,
                "summary": r.summary,
                "x-invaria-scopes": list(r.scopes),
                # The disclosure requirement, from the same table QueryService uses.
                "x-invaria-disclosure": {
                    "mode": disclosure.USE_CASES[r.operation_id].mode,
                    "policies": list(disclosure.USE_CASES[r.operation_id].policies),
                },
                "parameters": parameters,
                "responses": responses,
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Invaria local evidence console (read-only)",
            "version": "1.0.0",
            "description": (
                "Local, read-only REST served by `invaria console serve` on 127.0.0.1. Tenant, "
                "principal and scopes come from the operator's access profile, never from the "
                "request. Only GET and HEAD. Not an institutional API."
            ),
        },
        "servers": [{"url": "http://127.0.0.1:{port}", "variables": {"port": {"default": "8765"}}}],
        "security": [{"consoleToken": []}],
        "paths": paths,
        "components": {
            "schemas": defs["$defs"],
            "securitySchemes": {
                "consoleToken": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "per-process token printed at startup (or its session cookie)",
                }
            },
        },
    }


def render() -> str:
    return json.dumps(document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
