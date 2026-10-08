"""Server-rendered pages of the evidence console. No scripts; every value is escaped.

Each conclusion shows result, currency, economic cut (valid_at), knowledge time
(known_at), evaluation clock and coverage *before* any reason or narrative (cap. 26).
Result and currency are always written as text, never conveyed by colour alone.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

from invaria.console.html import Safe, el, join
from invaria.contracts.observation import (
    TokenMovementPayload,
    movement_receiver,
    movement_sender,
)
from invaria.contracts.quantity import Quantity
from invaria.query.models import (
    ComparisonView,
    ConclusionView,
    CoverageReport,
    CoverageView,
    DiscrepancyView,
    EvidenceView,
    MissingEvidenceView,
    OperationsView,
    TimelineView,
    TraceView,
)

TITLE = "Invaria evidence console"
CURRENCY_TEXT = {
    "current": "current: published for the latest revision epoch",
    "stale": "stale: newer evidence arrived after publication",
    "superseded": "superseded: a later evaluation is published",
    "not_published": "not published",
}
CAUSE_TEXT = {
    "registered": "operation registered",
    "evidence_appended": "evidence appended",
    "profile_changed": "profile changed",
    "explicit": "explicit invalidation",
}


# ------------------------------------------------------------------ helpers


def href(*segments: str, **params: str) -> str:
    path = "/" + "/".join(quote(s, safe="") for s in segments)
    return f"{path}?{urlencode(params)}" if params else path


def quantity(q: Quantity | None) -> Safe:
    if q is None:
        return join("—")
    return el("span", f"{q.to_decimal_text()} {q.unit}", class_="qty")


def when(moment: datetime | None) -> Safe:
    if moment is None:
        return join("—")
    text = moment.isoformat().replace("+00:00", "Z")
    return el("time", text, datetime=text)


def result_badge(result: str | None) -> Safe:
    if result is None:
        return el("span", "no evaluation", class_="badge")
    return el("span", result, class_=f"badge result-{result.lower()}")


def currency_badge(currency: str | None) -> Safe:
    if currency is None:
        return join("—")
    return el("span", currency, class_=f"badge currency-{currency.replace('_', '-')}")


def evidence_link(evidence_id: str) -> Safe:
    return el("a", evidence_id, href=href("evidence", evidence_id))


def evidence_list(ids: list[str]) -> Safe:
    if not ids:
        return join("none")
    return el("ul", [el("li", evidence_link(i)) for i in ids], class_="inline")


def table(caption: str, head: list[str], rows: list[list[object]], **attrs: str) -> Safe:
    return el(
        "table",
        el("caption", caption),
        el("thead", el("tr", [el("th", h, scope="col") for h in head])),
        el(
            "tbody",
            [
                el("tr", el("th", row[0], scope="row"), [el("td", c) for c in row[1:]])
                for row in rows
            ],
        ),
        **attrs,
    )


def section(slug: str, heading: str, *body: object) -> Safe:
    return el("section", el("h2", heading, id=f"{slug}-h"), *body, aria_labelledby=f"{slug}-h")


def limitations(items: list[str]) -> Safe:
    return section("limits", "Limitations", el("ul", [el("li", i) for i in items]))


def page(
    title: str,
    body: object,
    *,
    crumbs: list[tuple[str, str]] | None = None,
    nav: str = "operations",
) -> bytes:
    trail = [("Operations", "/"), *(crumbs or [])]
    breadcrumb = el(
        "nav",
        el(
            "ol",
            [el("li", el("a", label, href=link)) for label, link in trail],
            el("li", el("span", title, aria_current="page")),
        ),
        aria_label="Breadcrumb",
        class_="crumbs",
    )
    document = join(
        Safe("<!doctype html>"),
        el(
            "html",
            el(
                "head",
                el("meta", charset="utf-8"),
                el("meta", name="viewport", content="width=device-width, initial-scale=1"),
                el("title", f"{title} · {TITLE}"),
                el("link", rel="stylesheet", href="/static/console.css"),
            ),
            el(
                "body",
                el("a", "Skip to content", href="#main", class_="skip"),
                el(
                    "header",
                    el(
                        "p",
                        el("a", "Invaria", href="/"),
                        " ",
                        el("span", "evidence console · read-only", class_="tag"),
                        class_="brand",
                    ),
                    el(
                        "nav",
                        el(
                            "ul",
                            el(
                                "li",
                                el(
                                    "a",
                                    "Operations",
                                    href="/",
                                    aria_current="page" if nav == "operations" else None,
                                ),
                            ),
                        ),
                        aria_label="Primary",
                    ),
                    class_="top",
                ),
                el(
                    "main",
                    breadcrumb if crumbs is not None else None,
                    el("h1", title),
                    body,
                    id="main",
                    tabindex="-1",
                ),
                el(
                    "footer",
                    el(
                        "p",
                        "Read-only. Answers come from stored, closed snapshots and evaluations; "
                        "no source is queried and nothing here can change a result.",
                    ),
                ),
            ),
            lang="en",
        ),
    )
    return document.encode("utf-8")


# ------------------------------------------------------------- conclusions


def coverage_summary(coverage: list[CoverageView]) -> str:
    if not coverage:
        return "no coverage certificate in the snapshot"
    below = [c for c in coverage if c.meets_required_level is False]
    gaps = sum(c.gaps for c in coverage)
    text = f"{len(coverage) - len(below)} of {len(coverage)} certificates meet the required level"
    if below:
        text += "; below: " + ", ".join(f"{c.source_id} ({c.level})" for c in below)
    if gaps:
        text += f"; {gaps} gap(s)"
    return text


def conditions(view: ConclusionView, slug: str = "conditions") -> Safe:
    """Result and the conditions it rests on, before any narrative."""
    c = view.currency
    epochs = (
        f" (scope epoch {c.scope_epoch}, published epoch {c.published_epoch})"
        if c.published_epoch is not None
        else ""
    )
    lifecycle = (
        join(
            el("span", view.operation_state, class_="badge"),
            " business lifecycle, reported apart from the financial result",
        )
        if view.operation_state
        else "open"
    )
    rows = [
        ("Result", result_badge(view.result)),
        ("Lifecycle", lifecycle),
        ("Currency", join(currency_badge(c.currency), " ", CURRENCY_TEXT[c.currency], epochs)),
        ("Economic cut (valid_at)", when(view.snapshot.valid_at)),
        ("Known at (known_at)", when(view.snapshot.known_at)),
        ("Evaluation clock", when(view.snapshot.evaluation_clock)),
        ("Coverage", coverage_summary(view.coverage)),
        ("Snapshot", el("code", view.snapshot.snapshot_id)),
        (
            "Versions",
            join(
                el("code", view.versions.profile_ref),
                ", ",
                el("code", view.versions.rules_ref),
                ", ",
                el("code", view.versions.engine_ref),
            ),
        ),
    ]
    return section(
        slug,
        "Conclusion and conditions",
        el("dl", [join(el("dt", k), el("dd", v)) for k, v in rows], class_="conditions"),
    )


def controls_table(view: ConclusionView) -> Safe:
    rows: list[list[object]] = [
        [
            el(
                "a",
                c.control_id,
                href=href("evaluations", view.evaluation_id, "controls", c.control_id),
            ),
            "mandatory" if c.mandatory else "optional",
            el("span", c.status, class_=f"badge status-{c.status.lower().replace('_', '-')}"),
            el("code", c.reason_code),
            quantity(c.delta),
            c.reason,
        ]
        for c in view.controls
    ]
    return section(
        "controls",
        "Controls",
        table(
            "Per-control results (kept even when the global result is BREAK)",
            ["Control", "Mandatory", "Status", "Reason code", "Delta", "Reason"],
            rows,
        ),
    )


def coverage_table(certificates: list[CoverageView], caption: str) -> Safe:
    rows: list[list[object]] = [
        [
            evidence_link(c.coverage_id),
            c.source_id,
            ", ".join(c.fact_types),
            c.level,
            c.required_level or "—",
            {True: "yes", False: "no", None: "—"}[c.meets_required_level],
            join(when(c.interval.start), " to ", when(c.interval.end)),
            str(c.gaps),
            f"{c.records_received} / {c.records_quarantined}",
        ]
        for c in certificates
    ]
    return table(
        caption,
        [
            "Certificate",
            "Source",
            "Fact types",
            "Level",
            "Required",
            "Meets required",
            "Interval",
            "Gaps",
            "Received / quarantined",
        ],
        rows,
    )


# ------------------------------------------------------------------- pages


def operations_page(view: OperationsView) -> bytes:
    options = [
        el("option", label, value=value, selected=(view.result_filter or "") == value)
        for value, label in (
            ("", "Any"),
            ("MATCH", "MATCH"),
            ("BREAK", "BREAK"),
            ("UNKNOWN", "UNKNOWN"),
        )
    ]
    form = el(
        "form",
        el("label", "Result", for_="result"),
        el("select", options, id="result", name="result"),
        el("button", "Filter", type="submit"),
        method="get",
        action="/",
        class_="filters",
    )
    rows: list[list[object]] = [
        [
            el("a", i.operation_ref, href=href("operations", i.operation_ref)),
            result_badge(i.result),
            currency_badge(i.currency),
            when(i.known_at),
            str(i.evaluations),
            "yes" if i.watched else "no",
        ]
        for i in view.items
    ]
    body = join(
        form,
        table(
            "Operations and their published conclusion",
            ["Operation", "Result", "Currency", "Known at", "Evaluations", "Watched"],
            rows,
        )
        if rows
        else el("p", "No operation matches."),
        el("p", "The list is truncated.", class_="note") if view.truncated else None,
        limitations(view.limitations),
    )
    return page("Operations", body, nav="operations")


def timeline_list(view: TimelineView) -> Safe:
    items = []
    for entry in view.entries:
        if entry.kind == "evidence":
            items.append(
                el(
                    "li",
                    el(
                        "p",
                        when(entry.at) if entry.at else "at registration",
                        " · ",
                        el("strong", f"Epoch {entry.epoch}: {CAUSE_TEXT[entry.cause]}"),
                    ),
                    evidence_list(entry.evidence_ids) if entry.evidence_ids else None,
                    class_="tl-evidence",
                )
            )
        else:
            compare = (
                el(
                    "a",
                    "Compare with previous evaluation",
                    href=href(
                        "compare", left=entry.previous_evaluation_id, right=entry.evaluation_id
                    ),
                )
                if entry.previous_evaluation_id
                else None
            )
            items.append(
                el(
                    "li",
                    el(
                        "p",
                        when(entry.at),
                        " · ",
                        el("strong", "Evaluation "),
                        result_badge(entry.result),
                        " ",
                        currency_badge(entry.currency),
                    ),
                    el(
                        "p",
                        el("a", entry.evaluation_id, href=href("evaluations", entry.evaluation_id)),
                        " · valid_at ",
                        when(entry.valid_at),
                        " · engine ",
                        el("code", entry.engine_ref),
                        join(" · ", compare) if compare else None,
                    ),
                    class_="tl-evaluation",
                )
            )
    return section(
        "timeline",
        "Timeline",
        el(
            "p",
            "In knowledge order: the evidence that opened each revision epoch, and the "
            "evaluations it produced.",
            class_="note",
        ),
        el("ol", items, class_="timeline"),
        el("p", "Older entries are not shown (truncated).", class_="note")
        if view.truncated
        else None,
    )


def operation_page(trace: TraceView, timeline: TimelineView) -> bytes:
    op = trace.operation_ref
    evaluations = [e for e in timeline.entries if e.kind == "evaluation"]
    choices = [(e.evaluation_id, f"{e.result} · known {e.at.isoformat()}") for e in evaluations]
    left = choices[-2][0] if len(choices) > 1 else (choices[0][0] if choices else "")
    right = choices[-1][0] if choices else ""

    def select(name: str, label: str, chosen: str) -> Safe:
        return join(
            el("label", label, for_=f"cmp-{name}"),
            el(
                "select",
                [el("option", text, value=v, selected=v == chosen) for v, text in choices],
                id=f"cmp-{name}",
                name=name,
            ),
        )

    compare_form = section(
        "compare",
        "Compare two evaluations",
        el(
            "form",
            select("left", "Left (before)", left),
            select("right", "Right (after)", right),
            el("button", "Compare", type="submit"),
            method="get",
            action="/compare",
            class_="filters",
        )
        if len(choices) > 1
        else el("p", "Only one evaluation is stored."),
    )
    known_form = section(
        "known",
        "Conclusion as known at",
        el(
            "form",
            el("label", "Economic cut (valid_at)", for_="valid_at"),
            el(
                "input",
                id="valid_at",
                name="valid_at",
                type="text",
                required=True,
                aria_describedby="time-hint",
                value=trace.current.snapshot.valid_at.isoformat().replace("+00:00", "Z")
                if trace.current
                else None,
            ),
            el("label", "Known at (known_at)", for_="known_at"),
            el(
                "input",
                id="known_at",
                name="known_at",
                type="text",
                required=True,
                aria_describedby="time-hint",
            ),
            el("button", "Show", type="submit"),
            el("p", "ISO 8601 UTC ending in Z, e.g. 2026-10-01T18:00:00Z.", id="time-hint"),
            method="get",
            action=href("operations", op, "as-known"),
            class_="filters",
        ),
    )
    legs_rows: list[list[object]] = [
        [leg.fact_type, leg.authoritative_source, evidence_list(leg.observation_ids)]
        for leg in trace.legs
    ]
    body = join(
        conditions(trace.current)
        if trace.current
        else el("p", "No evaluation is stored for this operation yet."),
        el(
            "p",
            el("a", "Open this evaluation", href=href("evaluations", trace.current.evaluation_id)),
            " · ",
            el("a", "Coverage", href=href("operations", op, "coverage")),
        )
        if trace.current
        else None,
        timeline_list(timeline),
        compare_form,
        known_form,
        section(
            "legs",
            "Legs",
            table(
                "Authoritative source and observations per fact type",
                ["Fact type", "Authoritative source", "Observations"],
                legs_rows,
            ),
        )
        if legs_rows
        else None,
        limitations(trace.limitations),
    )
    return page(f"Operation {op}", body, crumbs=[])


def conclusion_page(view: ConclusionView, heading: str | None = None) -> bytes:
    body = join(
        conditions(view),
        controls_table(view),
        section(
            "coverage",
            "Coverage applied",
            coverage_table(view.coverage, "Coverage certificate applied per source and fact type"),
        ),
        limitations(view.limitations),
    )
    return page(
        heading or f"Evaluation {view.evaluation_id}",
        body,
        crumbs=[(f"Operation {view.operation_ref}", href("operations", view.operation_ref))],
    )


def control_page(d: DiscrepancyView, m: MissingEvidenceView) -> bytes:
    replay = (
        "yes: replaying the stored snapshot reproduces the recorded evaluation"
        if d.replay_consistent
        else "NO: replaying the stored snapshot does not reproduce the recorded evaluation; "
        "operands are withheld"
    )
    facts = [
        ("Status", el("span", d.status, class_=f"badge status-{d.status.lower()}")),
        ("Reason code", el("code", d.reason_code)),
        ("Economic cut (valid_at)", when(d.snapshot.valid_at)),
        ("Known at (known_at)", when(d.snapshot.known_at)),
        (
            "Currency",
            join(currency_badge(d.currency.currency), " ", CURRENCY_TEXT[d.currency.currency]),
        ),
        ("Left", join(quantity(d.left), " — ", d.left_definition)),
        ("Right", join(quantity(d.right), " — ", d.right_definition)),
        ("Delta (left - right)", quantity(d.delta)),
        ("Replay consistent", replay),
        ("Evidence", evidence_list(d.evidence_refs)),
    ]
    req_rows: list[list[object]] = [
        [
            r.fact_type,
            r.authoritative_source,
            el("code", r.mapping_ref),
            r.min_coverage_level,
            r.must_cover,
            "yes" if r.gaps_allowed else "no",
        ]
        for r in m.requirements
    ]
    body = join(
        section(
            "operands",
            "Operands and delta",
            el("dl", [join(el("dt", k), el("dd", v)) for k, v in facts], class_="conditions"),
        ),
        section(
            "missing",
            "What would decide it",
            el("p", "Missing evidence: ", el("strong", "yes" if m.missing else "no"), ". ", m.note),
            table(
                "Required evidence per fact type",
                [
                    "Fact type",
                    "Authoritative source",
                    "Mapping",
                    "Minimum coverage",
                    "Must cover",
                    "Gaps allowed",
                ],
                req_rows,
            ),
        ),
    )
    return page(
        f"Control {d.control_id}",
        body,
        crumbs=[(f"Evaluation {d.evaluation_id}", href("evaluations", d.evaluation_id))],
    )


def comparison_page(view: ComparisonView) -> bytes:
    left, right, change = view.left, view.right, view.change

    def side(v: ConclusionView) -> dict[str, object]:
        return {
            "Evaluation": el("a", v.evaluation_id, href=href("evaluations", v.evaluation_id)),
            "Result": result_badge(v.result),
            "Currency": currency_badge(v.currency.currency),
            "Economic cut (valid_at)": when(v.snapshot.valid_at),
            "Known at (known_at)": when(v.snapshot.known_at),
            "Evaluation clock": when(v.snapshot.evaluation_clock),
            "Coverage": coverage_summary(v.coverage),
            "Engine": el("code", v.versions.engine_ref),
            "Profile": el("code", v.versions.profile_ref),
        }

    a, b = side(left), side(right)
    summary = table(
        "Conclusions side by side",
        ["Field", "Left", "Right"],
        [[k, a[k], b[k]] for k in a],
    )
    controls = table(
        "Controls side by side",
        ["Control", "Mandatory", "Left", "Right", "Changed"],
        [
            [
                c.control_id,
                "mandatory" if c.mandatory else "optional",
                c.left_status or "absent",
                c.right_status or "absent",
                el("strong", "changed") if c.changed else "same",
            ]
            for c in view.controls
        ],
    )
    changes = (
        el(
            "ul",
            [
                el("li", el("code", c.control_id), ": ", c.before, " → ", c.after)
                for c in change.changed_controls
            ],
        )
        if change.changed_controls
        else el("p", "No control changed.")
    )
    body = join(
        section("summary", "Summary", summary),
        section("side", "Controls", controls),
        section(
            "delta",
            "What changed (left → right)",
            el(
                "p",
                result_badge(change.from_result),
                " → ",
                result_badge(change.to_result),
                ". Replay consistent: ",
                el("strong", "yes" if change.replay_consistent else "NO"),
                ".",
            ),
            el("h3", "Changed controls"),
            changes,
            el("h3", "Effective evidence added"),
            evidence_list(change.added_evidence),
            el("h3", "Effective evidence removed"),
            evidence_list(change.removed_evidence),
        ),
        limitations(change.limitations),
    )
    return page(
        "Comparison",
        body,
        crumbs=[(f"Operation {left.operation_ref}", href("operations", left.operation_ref))],
    )


def coverage_page(report: CoverageReport) -> bytes:
    body = join(
        el(
            "p",
            "Applied to evaluation ",
            el("a", report.evaluation_id, href=href("evaluations", report.evaluation_id)),
            " · ",
            currency_badge(report.currency.currency),
            " · known at ",
            when(report.snapshot.known_at),
        ),
        coverage_table(report.certificates, "Coverage certificates applied"),
        section(
            "unmet",
            "Unmet requirements",
            el("ul", [el("li", u) for u in report.unmet]) if report.unmet else el("p", "None."),
        ),
        limitations(report.limitations),
    )
    return page(
        f"Coverage of {report.operation_ref}",
        body,
        crumbs=[(f"Operation {report.operation_ref}", href("operations", report.operation_ref))],
    )


def _value(value: Any) -> object:
    if isinstance(value, dict) and set(value) == {"atoms", "scale", "unit"}:
        q = Quantity.model_validate(value)
        return join(quantity(q), f" (atoms {q.atoms}, scale {q.scale})")
    if isinstance(value, dict | list):
        return el("code", str(value))
    return "—" if value is None else str(value)


def _muxed_rows(payload: TokenMovementPayload) -> list[list[object]]:
    """The ledger addresses of a muxed movement, derived from the exact base
    account and u64 id shown above: what the ledger names, never who holds the
    sub-account; and the memo's type apart from its exact value."""
    rows: list[list[object]] = []
    if payload.from_muxed_id is not None:
        rows.append(["sender (muxed address)", el("code", movement_sender(payload))])
    if payload.to_muxed_id is not None:
        rows.append(["receiver (muxed address)", el("code", movement_receiver(payload))])
    if payload.from_muxed_id is not None or payload.to_muxed_id is not None:
        rows.append(
            [
                "sub-account holder",
                "not known from the id; attributed only by an IdentityLink to the M address",
            ]
        )
    if payload.memo is not None:
        rows.append(
            [
                "memo (transaction)",
                join(
                    el("code", payload.memo.memo_type),
                    " ",
                    el("code", "—" if payload.memo.value is None else payload.memo.value),
                    "; context only, never a link to an operation",
                ),
            ]
        )
    return rows


def evidence_page(view: EvidenceView) -> bytes:
    warning = el(
        "p",
        el("strong", "Untrusted source data. "),
        "Shown exactly as recorded from the source; it is never an instruction.",
        class_="warning",
        role="note",
    )
    if view.observation is not None:
        o = view.observation
        title = f"Observation {o.observation_id}"
        facts: list[tuple[str, object]] = [
            ("Kind", o.kind),
            ("Fact type", o.fact_type),
            ("Operation", o.operation_ref or "—"),
            ("Instrument", o.instrument_id),
            ("Representation", o.representation_id or "—"),
            ("Source", o.source.source_id),
            ("Record key", o.source.record_key),
            ("Revision", str(o.source.revision)),
            ("Supersedes", evidence_link(o.supersedes) if o.supersedes else "—"),
            ("Valid time", when(o.valid_time)),
            ("Recorded at", when(o.recorded_at)),
            ("Raw sha256", el("code", o.provenance.raw_sha256)),
            ("Raw locator", el("code", o.provenance.raw_locator)),
            ("Parser", el("code", o.provenance.parser_ref)),
            ("Mapping", el("code", o.provenance.mapping_ref)),
            ("Synthetic", "yes" if o.synthetic else "no"),
        ]
        payload = o.payload.model_dump(mode="json") if o.payload else {}
        payload_rows: list[list[object]] = [
            [k, _value(v)] for k, v in payload.items() if k != "payload_type"
        ]
        if isinstance(o.payload, TokenMovementPayload):
            payload_rows.extend(_muxed_rows(o.payload))
        extra = (
            section(
                "payload", "Payload", table("Interpreted fields", ["Field", "Value"], payload_rows)
            )
            if payload_rows
            else None
        )
    else:
        assert view.coverage is not None
        c = view.coverage
        title = f"Coverage certificate {c.coverage_id}"
        facts = [
            ("Source", c.source_id),
            ("Fact types", ", ".join(c.fact_types)),
            ("Instrument", c.instrument_id),
            ("Level", c.level),
            ("Method", c.method),
            ("Interval", join(when(c.interval.start), " to ", when(c.interval.end))),
            ("Gaps", str(len(c.gaps))),
            ("Records received", str(c.records_received)),
            ("Records quarantined", str(c.records_quarantined)),
            ("Raw sha256", el("ul", [el("li", el("code", h)) for h in c.raw_sha256])),
            ("Recorded at", when(c.recorded_at)),
            ("Synthetic", "yes" if c.synthetic else "no"),
        ]
        extra = None
    body = join(
        warning,
        section(
            "provenance",
            "Provenance",
            el("dl", [join(el("dt", k), el("dd", v)) for k, v in facts], class_="conditions"),
        ),
        extra,
        limitations(view.limitations),
    )
    return page(title, body, crumbs=[])


def error_page(status: int, code: str, message: str) -> bytes:
    body = join(
        el("p", el("strong", code), ": ", message),
        el("p", "Open the address printed when the console started to sign in.")
        if code == "UNAUTHENTICATED"
        else None,
        el("p", el("a", "Back to operations", href="/")),
    )
    return page(f"Error {status}", body)


CSS = b"""
:root {
  color-scheme: light;
  --bg: #ffffff; --fg: #1a1c1f; --muted: #4a5059; --line: #d5d9de; --panel: #f4f6f8;
  --link: #0b57b0; --focus: #b35c00;
  --match-bg: #e3f4e8; --match-fg: #145c2c;
  --break-bg: #fbe4e4; --break-fg: #8a1c1c;
  --unknown-bg: #fff2d6; --unknown-fg: #6b4a00;
  --warn-bg: #fff2d6; --warn-fg: #4d3500;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #15171a; --fg: #e8eaed; --muted: #b0b6bf; --line: #3a3f46; --panel: #1f2226;
    --link: #8cb8ff; --focus: #ffb366;
    --match-bg: #133d22; --match-fg: #b6ecc6;
    --break-bg: #4a1717; --break-fg: #ffc9c9;
    --unknown-bg: #45370f; --unknown-fg: #ffe3a3;
    --warn-bg: #45370f; --warn-fg: #ffe3a3;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color: var(--link); }
a:focus-visible, button:focus-visible, select:focus-visible, input:focus-visible,
main:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
.skip { position: absolute; left: -9999px; }
.skip:focus { left: 16px; top: 8px; background: var(--bg); padding: 4px 8px; z-index: 1; }
.top { display: flex; flex-wrap: wrap; gap: 8px 24px; align-items: baseline;
  padding: 12px 16px; border-bottom: 1px solid var(--line); }
.brand { margin: 0; font-weight: 700; }
.brand .tag { font-weight: 400; color: var(--muted); }
.top ul, .crumbs ol, ul.inline { list-style: none; margin: 0; padding: 0;
  display: flex; flex-wrap: wrap; gap: 4px 12px; }
.crumbs li + li::before { content: "/"; margin-right: 12px; color: var(--muted); }
[aria-current="page"] { font-weight: 700; }
main { max-width: 72rem; margin: 0 auto; padding: 16px; }
footer { max-width: 72rem; margin: 0 auto; padding: 16px; color: var(--muted);
  border-top: 1px solid var(--line); }
h1 { font-size: 1.6rem; margin: 8px 0 16px; overflow-wrap: anywhere; }
h2 { font-size: 1.25rem; margin: 24px 0 8px; }
h3 { font-size: 1.05rem; margin: 16px 0 4px; }
.note { color: var(--muted); }
dl.conditions { display: grid; grid-template-columns: minmax(10rem, max-content) 1fr;
  gap: 4px 16px; margin: 0; padding: 12px; background: var(--panel);
  border: 1px solid var(--line); border-radius: 6px; }
dl.conditions dt { font-weight: 600; }
@media (max-width: 480px) {
  dl.conditions { grid-template-columns: 1fr; }
  dl.conditions dd { margin-bottom: 6px; }
}
dl.conditions dd { margin: 0; overflow-wrap: anywhere; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; display: block; overflow-x: auto; }
caption { text-align: left; font-weight: 600; padding: 4px 0; }
th, td { border: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }
thead th { background: var(--panel); }
code { font-family: ui-monospace, "SFMono-Regular", Menlo, monospace; font-size: 0.9em;
  overflow-wrap: anywhere; }
.qty { font-variant-numeric: tabular-nums; font-weight: 600; white-space: nowrap; }
.badge { display: inline-block; padding: 0 6px; border-radius: 4px; font-weight: 700;
  border: 1px solid currentColor; }
.result-match, .status-pass { background: var(--match-bg); color: var(--match-fg); }
.result-break, .status-fail { background: var(--break-bg); color: var(--break-fg); }
.result-unknown, .status-unknown { background: var(--unknown-bg); color: var(--unknown-fg); }
.currency-stale { background: var(--unknown-bg); color: var(--unknown-fg); }
.warning { background: var(--warn-bg); color: var(--warn-fg); padding: 8px 12px;
  border-radius: 6px; }
.filters { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; }
.filters p { flex-basis: 100%; margin: 0; color: var(--muted); }
input, select, button { font: inherit; padding: 4px 8px; }
ol.timeline { list-style: none; margin: 0; padding: 0 0 0 16px;
  border-left: 3px solid var(--line); }
ol.timeline li { margin: 0 0 12px; padding-left: 8px; }
ol.timeline p { margin: 0; }
.tl-evidence { color: var(--muted); }
"""
