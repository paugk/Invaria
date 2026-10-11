"""Server-rendered Analytics pages: registered integrity reports,
their figures and cases, and the event feed. No scripts; every value is escaped.

These pages only present what the query service returns for /v1:
the same summary, the same pinned cases, the same feed pages. They compute no figure of
their own; percentages, denominators and statuses come from the report as registered.
"""

from __future__ import annotations

from typing import Any

from invaria.console.html import Safe, el, join
from invaria.console.pages import (
    href,
    limitations,
    page,
    quantity,
    result_badge,
    section,
    table,
    when,
)
from invaria.query.analytics import STATUS_MEANING
from invaria.query.models import (
    CasesView,
    EventPageView,
    IntegrityReportListView,
    IntegrityReportSummaryView,
    MetricView,
)

EVENT_TYPES = (
    (
        "type_reopened",
        "operation.scope_reopened",
        "Scope reopened (earlier publication lost currency)",
    ),
    ("type_published", "operation.evaluation_published", "Evaluation published (with change)"),
    ("type_report", "integrity_report.registered", "Integrity report registered"),
)
STOP_TEXT = {
    "end_observed": "Reached the last observed event. This is not the end of the feed: new "
    "events may be derived at any time; continue later with the cursor.",
    "work_limit": "This request examined as many positions as it may. More may follow, even "
    "if this page is empty: continue.",
    "item_limit": "The page is full. Continue for the next events.",
    "byte_limit": "The response size limit was reached before the next authorized event; it "
    "was not skipped. Continue.",
    "start_now": "Position fixed at the last event observed now. Only events derived after "
    "it will be shown when you continue.",
}
# Figures about financial results of admitted conclusions. Everything else of section A
# (population, admission, technical errors, selection statuses) is technical or about
# publication and is shown apart, never mixed with MATCH/BREAK/UNKNOWN.
FINANCIAL_METRICS = (
    "A.match_share_of_admitted",
    "A.break_share_of_admitted",
    "A.unknown_share_of_admitted",
    "A.conclusive_share_of_admitted",
    "A.match_share_of_conclusive",
    "A.match_share_of_included",
)
PINNED_NOTE = (
    "Pinned in the report: these figures and cases were fixed when the report was computed "
    "and registered. They do not change when new evidence arrives."
)
CURRENT_NOTE = (
    "Current view: operation and evaluation pages show the store as it is now (for example "
    "today's currency), not the state pinned in the report."
)


def crumbs_reports(*more: tuple[str, str]) -> list[tuple[str, str]]:
    return list(more)


def _report_page(title: str, body: object, *more: tuple[str, str]) -> bytes:
    return page(
        title, body, crumbs=list(more), nav="reports", root=("Integrity reports", "/reports")
    )


def _short(report_id: str) -> str:
    return report_id[:15] + "…"


def _age(seconds: int | None) -> str:
    if seconds is None:
        return "undetermined"
    return (
        f"{seconds // 86400} d {seconds % 86400 // 3600} h of knowledge time before the "
        "evidence horizon (not time unpaid, not time open)"
    )


def _value(m: MetricView) -> object:
    if m.kind == "count":
        return el("span", str(m.numerator), class_="qty")
    if m.value_percent is None:
        reason = "denominator is zero" if m.undefined_reason == "denominator_zero" else "incomplete"
        return el("span", f"not calculable ({reason})", class_="badge")
    return el("span", f"{m.value_percent} %", class_="qty")


def metric_rows(report_id: str, metrics: list[MetricView]) -> Safe:
    rows = []
    for m in metrics:
        cases = href("reports", report_id, "cases", key=m.cases_key)
        rows.append(
            el(
                "tr",
                el("th", m.description, scope="row"),
                el(
                    "td",
                    el("span", str(m.numerator), class_="qty"),
                    el("br"),
                    m.numerator_definition,
                ),
                el(
                    "td",
                    "—"
                    if m.denominator is None
                    else join(
                        el("span", str(m.denominator), class_="qty"),
                        el("br"),
                        m.denominator_definition or "",
                    ),
                ),
                el(
                    "td",
                    _value(m),
                    el("br") if m.unknown_excluded else None,
                    f"{m.unknown_excluded} UNKNOWN shown apart, not in this denominator"
                    if m.unknown_excluded
                    else None,
                ),
                el("td", el("a", "cases", href=cases, aria_label=f"cases of {m.description}")),
                data_metric=m.metric_id,
                data_numerator=str(m.numerator),
                data_denominator="" if m.denominator is None else str(m.denominator),
                data_cases=m.cases_key,
            )
        )
    return join(*rows)


def metric_table(caption: str, report_id: str, metrics: list[MetricView]) -> Safe:
    return el(
        "table",
        el("caption", caption),
        el(
            "thead",
            el(
                "tr",
                [
                    el("th", h, scope="col")
                    for h in ("Figure", "Numerator", "Denominator", "Value", "Cases")
                ],
            ),
        ),
        el("tbody", metric_rows(report_id, metrics)),
    )


# ------------------------------------------------------------------------------ list


def reports_page(view: IntegrityReportListView) -> bytes:
    rows: list[list[object]] = [
        [
            el("a", _short(i.report_id), href=href("reports", i.report_id)),
            when(i.registered_at),
            when(i.evidence_horizon),
            when(i.store_read_at),
            f"{i.definition} · {i.selection_policy}",
            i.report_format,
        ]
        for i in view.items
    ]
    body = join(
        el(
            "p",
            "Reports are computed and registered with the CLI (analytics report --register). "
            "Opening these pages never computes, registers or re-evaluates anything.",
            class_="note",
        ),
        table(
            "Registered integrity reports, newest registration first",
            [
                "Report",
                "Registered at (store clock)",
                "Evidence horizon",
                "Publication state read at",
                "Definition · policy",
                "Format",
            ],
            rows,
        )
        if rows
        else el("p", "No registered report in this access scope."),
        el(
            "p",
            el(
                "a",
                "Next page",
                href=href("reports", cursor=view.next_cursor),
                rel="next",
            ),
        )
        if view.next_cursor
        else None,
        limitations(view.limitations),
    )
    return page("Integrity reports", body, nav="reports", crumbs=None)


# --------------------------------------------------------------------------- summary


def summary_page(v: IntegrityReportSummaryView) -> bytes:
    rid = v.report_id
    read_at = (
        v.temporal.publication_state_read_at if v.temporal is not None else v.store_read.read_at
    )
    d, x, b = v.distribution, v.discrepancies, v.blockers
    panel = el(
        "dl",
        el("dt", "Report"),
        el("dd", el("code", rid)),
        el("dt", "Definition and selection policy"),
        el("dd", f"{v.definition} · {v.selection_policy} ({v.report_format})"),
        el("dt", "Evidence horizon"),
        el(
            "dd",
            when(v.evidence_horizon),
            " — admits snapshots known up to it (knowledge time; no economic-time filter)",
        ),
        el("dt", "Publication state read at"),
        el(
            "dd",
            when(read_at),
            " — the publication state is the one present at this store read; it is not "
            "reconstructed at the horizon",
        ),
        el("dt", "Registered at"),
        el("dd", when(v.registered_at), " (store clock)"),
        el("dt", "Population"),
        el(
            "dd",
            f"{v.members} operations included ({v.population}); "
            f"profile filter: {', '.join(v.profile_filter) if v.profile_filter else 'none'}",
        ),
        el("dt", "Selection"),
        el("dd", v.selection_description),
        class_="conditions",
    )
    exclusions = (
        table(
            "Excluded before counting",
            ["Reason", "Operations", "Meaning"],
            [[e.reason, str(e.operations), e.description] for e in v.exclusions],
        )
        if v.exclusions
        else el("p", "No exclusion.")
    )
    statuses = table(
        "Selection status of every included operation (not financial results)",
        ["Status", "Operations", "Meaning", "Cases"],
        [
            [
                s,
                el("span", str(n), class_="qty"),
                STATUS_MEANING.get(s, ""),
                el("a", "cases", href=href("reports", rid, "cases", key=f"status:{s}")),
            ]
            for s, n in d.statuses.items()
        ],
    )
    financial = [m for m in d.metrics if m.metric_id in FINANCIAL_METRICS]
    situation = [
        m
        for m in d.metrics
        if m.metric_id not in FINANCIAL_METRICS and not m.metric_id.startswith("A.status:")
    ]
    groups = (
        table(
            "FAIL controls by profile and control",
            [
                "Control",
                "Operations (unit of the cases)",
                "Control incidents",
                "Reason codes",
                "Oldest FAIL run",
                "Cases",
            ],
            [
                [
                    f"{g.control_id} ({g.profile_ref}{', mandatory' if g.mandatory else ''})",
                    el("span", str(g.operations), class_="qty"),
                    el("span", str(g.incidents), class_="qty"),
                    ", ".join(f"{r.reason_code} {r.incidents}" for r in g.reason_codes),
                    _age(g.oldest_age_seconds),
                    el("a", "cases", href=href("reports", rid, "cases", key=g.cases_key)),
                ]
                for g in x.groups
            ],
        )
        if x.groups
        else el("p", "No FAIL control among admitted conclusions.")
    )
    blocks = (
        table(
            "Diagnosed blocks by category",
            ["Category", "Operations (unit of the cases)", "Blocked requirements", "Cases"],
            [
                [
                    g.category,
                    el("span", str(g.operations), class_="qty"),
                    el("span", str(g.requirements), class_="qty"),
                    el("a", "cases", href=href("reports", rid, "cases", key=g.cases_key)),
                ]
                for g in b.groups
            ],
        )
        if b.groups
        else el("p", "No diagnosed block.")
    )
    segments = table(
        "Admitted conclusions by profile and engine",
        ["Profile", "Engine", "Engine status", "Admitted", "MATCH", "BREAK", "UNKNOWN"],
        [
            [
                s.profile_ref,
                s.engine_ref,
                s.engine_status,
                str(s.admitted),
                str(s.match_count),
                str(s.break_count),
                str(s.unknown_count),
            ]
            for s in v.segments
        ],
    )
    body = join(
        panel,
        el("p", PINNED_NOTE, class_="warning"),
        section("population", "Population and exclusions", exclusions),
        section(
            "financial",
            "Financial result of admitted conclusions",
            el(
                "p",
                f"{d.admitted} admitted of {d.included} included: MATCH {d.match_count}, "
                f"BREAK {d.break_count}, UNKNOWN {d.unknown_count}. UNKNOWN is not "
                "non-payment: a requirement could not be decided. A BREAK amount is the "
                "control's stored difference, never a loss and never summed across assets.",
            ),
            metric_table("Financial figures (each with numerator and denominator)", rid, financial),
        ),
        section(
            "situation",
            "Technical and publication situation",
            el(
                "p",
                "Operations not evaluated, stale publications or retired engines are counted "
                "here, never as financial UNKNOWN. Technical errors are reported apart.",
                class_="note",
            ),
            statuses,
            metric_table("Technical and publication figures", rid, situation),
            el(
                "p",
                f"Stored evaluations: {d.stored_evaluations}; not selected: "
                f"{d.evaluations_not_selected}; with a technical error: {d.technical_error}.",
            ),
        ),
        section(
            "discrepancies",
            "Discrepancies (FAIL controls)",
            el(
                "p",
                f"{x.operations_with_discrepancy} operations with at least one FAIL control; "
                f"{x.control_discrepancies} control incidents. A discrepancy can coexist with "
                "insufficient evidence in other controls of the same operation. The age of a "
                "FAIL run is knowledge time, not economic time unpaid.",
            ),
            groups,
            metric_table("Discrepancy figures", rid, x.metrics),
        ),
        section(
            "blocks",
            "Diagnosed blocks (UNKNOWN controls)",
            el(
                "p",
                f"Diagnosis {'complete' if b.complete else 'INCOMPLETE'}: {b.replayed} replays "
                f"(limit {b.max_replays}); not processed {b.not_processed}; requirements not "
                f"evaluated {b.not_evaluated}; engines without diagnosis "
                f"{b.diagnosis_unavailable}; replays that do not reproduce "
                f"{b.reconstruction_mismatch}. Not applicable is not satisfied.",
            ),
            blocks,
            metric_table("Block figures", rid, b.metrics),
        ),
        section("segments", "Segments", segments),
        section(
            "integrity",
            "What this report is",
            el("ul", [el("li", t) for t in v.registry_limitations]),
        ),
        limitations(v.report_limitations),
    )
    return _report_page(f"Integrity report {_short(rid)}", body)


# ----------------------------------------------------------------------------- cases


def _reason(r: Any) -> Safe:
    parts: list[object] = [el("strong", r.kind)]
    for value in (r.control_id, r.requirement_id, r.status, r.reason_code, r.category):
        if value:
            parts += [" · ", str(value)]
    if r.delta is not None:
        parts += [" · stored control delta ", quantity(r.delta), " (not a loss; never summed)"]
    if r.age is not None:
        parts += [
            " · FAIL run of ",
            str(r.age.publications_in_run),
            " publication(s), ",
            _age(r.age.age_seconds),
            f", stopped by {r.age.run_stopped_by}",
        ]
    if r.required_from_source:
        parts += [" · requires evidence from ", r.required_from_source]
    if r.evidence_refs:
        parts += [
            " · cited evidence ",
            join(
                *[
                    join(", " if i else "", el("a", e, href=href("evidence", e)))
                    for i, e in enumerate(r.evidence_refs)
                ]
            ),
            " (opening it needs evidence:read)",
        ]
    return el("li", *parts)


def cases_page(report: IntegrityReportSummaryView, view: CasesView, key: str) -> bytes:
    rid = report.report_id
    rows = []
    for c in view.cases:
        sel = c.selected
        rows.append(
            el(
                "tr",
                el(
                    "th",
                    el("a", c.operation_ref, href=href("operations", c.operation_ref)),
                    scope="row",
                ),
                el("td", c.status),
                el("td", result_badge(c.result) if sel else "—"),
                el(
                    "td",
                    join(
                        el("a", sel.evaluation_id, href=href("evaluations", sel.evaluation_id)),
                        el("br"),
                        f"epoch {sel.published_epoch}, known_at ",
                        when(sel.known_at),
                        f", {sel.engine_ref} ({sel.engine_status} when pinned)",
                    )
                    if sel
                    else "no selected evaluation",
                ),
                el("td", el("ul", [_reason(r) for r in c.reasons]) if c.reasons else "—"),
                data_operation=c.operation_ref,
            )
        )
    shown_from = view.offset + 1 if view.cases else 0
    body = join(
        el(
            "dl",
            el("dt", "Report"),
            el("dd", el("a", el("code", rid), href=href("reports", rid))),
            el("dt", "Figure"),
            el("dd", el("code", key)),
            el("dt", "Criterion"),
            el("dd", view.cases[0].criterion if view.cases else "—"),
            el("dt", "Operations behind the figure"),
            el(
                "dd",
                f"{view.total_operations} (showing {shown_from} to "
                f"{view.offset + len(view.cases)})",
            ),
            el("dt", "Evidence horizon"),
            el("dd", when(report.evidence_horizon)),
            class_="conditions",
        ),
        el("p", PINNED_NOTE, class_="warning"),
        el("p", CURRENT_NOTE, class_="note"),
        el(
            "table",
            el("caption", "Pinned cases (one row per operation)"),
            el(
                "thead",
                el(
                    "tr",
                    [
                        el("th", h, scope="col")
                        for h in (
                            "Operation (current view)",
                            "Selection status",
                            "Pinned result",
                            "Pinned evaluation (its page shows today's currency)",
                            "Why it is in this figure",
                        )
                    ],
                ),
            ),
            el("tbody", *rows),
            class_="cases",
        )
        if rows
        else el("p", "No case behind this figure."),
        el(
            "p",
            el(
                "a",
                "Next page",
                href=href("reports", rid, "cases", key=key, cursor=view.next_cursor),
                rel="next",
            ),
        )
        if view.next_cursor
        else None,
        limitations(view.limitations),
    )
    return _report_page(f"Cases of {key}", body, (_short(rid), href("reports", rid)))


# ---------------------------------------------------------------------------- events


def _types_param(view: EventPageView) -> dict[str, str]:
    return {"types": ",".join(view.filter_types)}


def start_form(selected: tuple[str, ...] = ()) -> Safe:
    boxes = []
    for name, value, label in EVENT_TYPES:
        boxes.append(
            el(
                "p",
                el(
                    "input",
                    type="checkbox",
                    id=name,
                    name=name,
                    value="1",
                    checked=(not selected or value in selected),
                ),
                " ",
                el("label", label, for_=name),
            )
        )
    return el(
        "form",
        el(
            "fieldset",
            el("legend", "Where to start (explicit)"),
            el(
                "p",
                el(
                    "input",
                    type="radio",
                    id="start-beginning",
                    name="start",
                    value="beginning",
                    checked=True,
                ),
                " ",
                el("label", "From the beginning", for_="start-beginning"),
            ),
            el(
                "p",
                el("input", type="radio", id="start-now", name="start", value="now"),
                " ",
                el("label", "From now (only events derived afterwards)", for_="start-now"),
            ),
        ),
        el("fieldset", el("legend", "Event types"), *boxes),
        el("p", el("button", "Show events", type="submit")),
        method="get",
        action="/events",
        class_="filters",
    )


def events_intro_page() -> bytes:
    body = join(
        el(
            "p",
            "The event feed lists derived domain events in derivation order. Nothing is shown "
            "until you choose where to start. There is no live update: continue or reload "
            "manually.",
        ),
        start_form(),
        el(
            "p",
            "events:read reveals the feed's positions and volume, including events of types "
            "whose content you cannot read. Reading each type also needs its resource "
            "permission (operations:read or reports:read).",
            class_="note",
        ),
    )
    return page("Events", body, nav="events", crumbs=None)


def _event_row(item: Any) -> Safe:
    e = item.event
    data = e.data
    if e.subject.kind == "operation":
        subject = el("a", e.subject.operation_ref, href=href("operations", e.subject.operation_ref))
    else:
        subject = el("a", _short(e.subject.report_id), href=href("reports", e.subject.report_id))
    detail: list[object] = []
    if e.type == "operation.scope_reopened":
        detail.append(f"epoch {data.epoch} opened ({data.cause})")
        if data.prior_publication is not None:
            p = data.prior_publication
            detail += [
                "; earlier publication ",
                el("a", p.evaluation_id, href=href("evaluations", p.evaluation_id)),
                f" (epoch {p.epoch}) "
                + ("lost currency here" if p.current_until_reopening else "was already stale"),
            ]
        elif data.prior_publication_basis == "unidentifiable":
            detail.append("; earlier publication not identifiable")
    elif e.type == "operation.evaluation_published":
        detail += [
            f"epoch {data.epoch}: ",
            el("a", data.evaluation_id, href=href("evaluations", data.evaluation_id)),
            " result at publication ",
            result_badge(data.result),
            " (not a statement that it is still current)",
        ]
        if e.categories:
            detail.append("; changes: " + ", ".join(e.categories))
    else:
        detail.append(f"evidence horizon {data.evidence_horizon.isoformat()}")
    return el(
        "tr",
        el("th", str(item.tenant_seq), scope="row"),
        el(
            "td",
            "derived ",
            when(item.derived_at),
            el("br"),
            "fact recorded ",
            when(e.provenance.fact_recorded_at),
        ),
        el("td", el("code", e.type), el("br"), subject),
        el(
            "td",
            e.provenance.source_payload,
            " (provenance: written before pinned references; references read from history)"
            if e.provenance.source_payload == "historical"
            else None,
        ),
        el("td", *detail),
        data_event=e.event_id,
    )


def events_page(view: EventPageView) -> bytes:
    params = _types_param(view)
    notices = [
        el(
            "li",
            (
                f"Newly readable types {', '.join(n.types)}: events of these types up to "
                f"position {n.examined_without_delivery_through} were examined without being "
                "shown. They are not recovered automatically: "
            )
            if n.kind == "readable_types_widened"
            else f"No longer readable: {', '.join(n.types)}; they are no longer shown.",
            el(
                "a",
                "read them from the beginning",
                href=href("events", start="beginning", types=",".join(n.types)),
            )
            if n.kind == "readable_types_widened"
            else None,
        )
        for n in view.notices
    ]
    rows = [_event_row(i) for i in view.items]
    body = join(
        el(
            "dl",
            el("dt", "Feed generation"),
            el("dd", el("code", view.generation_id)),
            el("dt", "Examined positions"),
            el(
                "dd",
                f"{view.examined_after + 1 if view.examined else view.examined_after} to "
                f"{view.continuation_position} ({view.examined} examined, {view.delivered} shown)",
            ),
            el("dt", "Last observed position"),
            el("dd", str(view.observed_head)),
            el("dt", "Readable types now"),
            el("dd", ", ".join(view.readable_types) or "none"),
            el("dt", "State"),
            el(
                "dd",
                STOP_TEXT["end_observed"]
                if view.caught_up and view.stop_reason != "start_now"
                else STOP_TEXT[view.stop_reason]
                if view.caught_up
                else join("More to read: ", STOP_TEXT[view.stop_reason]),
            ),
            class_="conditions",
        ),
        el(
            "section",
            el("h2", "Permission changes", id="notices-h"),
            el("ul", *notices),
            aria_labelledby="notices-h",
            class_="warning",
        )
        if notices
        else None,
        el(
            "table",
            el("caption", "Events in derivation order (tenant_seq); not causal order"),
            el(
                "thead",
                el(
                    "tr",
                    [
                        el("th", h, scope="col")
                        for h in (
                            "Order (tenant_seq)",
                            "Derived at / Fact recorded at",
                            "Type and subject",
                            "Provenance",
                            "What it says",
                        )
                    ],
                ),
            ),
            el("tbody", *rows),
            class_="events",
        )
        if rows
        else el(
            "p",
            "No event shown on this page."
            + ("" if view.caught_up else " That is not the end: continue."),
        ),
        el(
            "p",
            el(
                "a",
                "Continue from here" if not view.caught_up else "Check for newer events",
                href=href("events", cursor=view.next_cursor, **params),
                rel="next",
            ),
        ),
        el("p", "There is no live update: continue or reload manually.", class_="note"),
        el("h2", "Start again"),
        start_form(tuple(view.filter_types)),
        limitations(view.limitations),
    )
    return page("Events", body, nav="events", crumbs=None)


def cursor_problem_page(code: str, message: str, types: str | None) -> bytes:
    extra = {"types": types} if types else {}
    explanation = (
        "This cursor belongs to another generation of the feed: the history was restored or "
        "replaced since it was issued. Continuing would not be valid. Choose explicitly:"
        if code == "CURSOR_RESET"
        else "This cursor is ahead of the feed (for example after a restore made without "
        "the supported tool). Choose explicitly:"
    )
    body = join(
        el("p", el("strong", code), ": ", message),
        el("p", explanation),
        el(
            "ul",
            el(
                "li",
                el(
                    "a",
                    "Read from the beginning (events may repeat; deduplicate by event_id)",
                    href=href("events", start="beginning", **extra),
                ),
            ),
            el(
                "li",
                el(
                    "a",
                    "Start from now (earlier events are not shown)",
                    href=href("events", start="now", **extra),
                ),
            ),
        ),
    )
    return page("Events: restart needed", body, nav="events", crumbs=None)
