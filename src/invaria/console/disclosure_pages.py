"""Pages of disclosed views. No scripts; every value is escaped.

They present only what ``QueryService.list_disclosures`` and ``get_disclosure`` return: no
link, identifier, date or figure of the original report, and nothing computed here."""

from __future__ import annotations

from invaria.console.html import el, join
from invaria.console.pages import NavItems, href, page, section, table
from invaria.query.models import AggregateDisclosureView, DisclosureListView

ROOT = ("Disclosures", "/disclosures")
WITHHELD_TEXT = {
    "source_report_identifiers_and_hashes": "identifiers and hashes of the source report",
    "access_scope_tenant_and_principals": "tenant and principals",
    "operations_and_evaluations": "operations and evaluations",
    "financial_profiles_and_engines": "financial profiles and engines",
    "assets_amounts_addresses_and_evidence": "assets, amounts, addresses and evidence",
    "dates_times_and_ages": "dates, times and ages",
    "filters_and_segments": "filters and segments",
    "case_keys_and_links": "case keys and links to the original",
    "other_report_sections": "every other section of the report",
}


def list_page(view: DisclosureListView, nav_items: NavItems, home: str) -> bytes:
    rows: list[list[object]] = [
        [el("a", item.view_id, href=href("disclosures", item.view_id)), item.policy_ref]
        for item in view.items
    ]
    body = join(
        el(
            "p",
            "Views issued to the principal of this console's access profile. Each is a "
            "selection from a report, not the report.",
        ),
        table("Disclosed views", ["View", "Policy"], rows)
        if rows
        else el("p", "No view has been issued to this principal, or all were revoked."),
        el("p", "More views exist than are listed here.") if view.truncated else None,
    )
    return page("Disclosed views", body, nav="disclosures", nav_items=nav_items, home=home)


def view_page(view: AggregateDisclosureView, nav_items: NavItems, home: str) -> bytes:
    o, x = view.outcomes, view.outside_outcomes
    figures = table(
        "Disclosed counts (exact; each with its denominator)",
        ["Figure", "Count", "Out of", "Denominator"],
        [
            ["MATCH", str(o.match_count), str(o.denominator), o.denominator_definition],
            ["BREAK", str(o.break_count), str(o.denominator), o.denominator_definition],
            ["UNKNOWN", str(o.unknown_count), str(o.denominator), o.denominator_definition],
            [
                "Outside MATCH, BREAK and UNKNOWN (not a result)",
                str(x.operations),
                str(x.denominator),
                x.denominator_definition,
            ],
        ],
        class_="disclosed",
    )
    body = join(
        section(
            "what",
            "What this view is",
            el("p", el("strong", view.statements[0])),
            el(
                "dl",
                el("dt", "Policy"),
                el("dd", f"{view.policy.policy_id}, version {view.policy.version}"),
                el("dt", "View"),
                el("dd", f"{view.view_id} ({view.view_format})"),
                el("dt", "Counting definition"),
                el("dd", view.counting_definition),
                el("dt", "Included operations"),
                el("dd", f"{view.included_operations} ({view.included_definition})"),
                el("dt", "Outside the three results"),
                el("dd", x.definition),
            ),
        ),
        section("figures", "Counts", figures),
        section(
            "withheld",
            "Not disclosed under this policy",
            el("p", view.withheld_meaning),
            el("ul", [el("li", WITHHELD_TEXT[c]) for c in view.withheld]),
        ),
        section("statements", "How to read it", el("ul", [el("li", s) for s in view.statements])),
    )
    return page(
        "Disclosed view",
        body,
        crumbs=[],
        nav="disclosures",
        root=ROOT,
        nav_items=nav_items,
        home=home,
    )
