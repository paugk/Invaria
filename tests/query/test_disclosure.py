"""Controlled disclosure, pure part: the policy catalogue, the projection of a
report's aggregates and the output allowlist. No database: the routes, isolation and
revocation are in ``tests/console/test_disclosure_pg.py``."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest

from invaria.contracts.base import parse_contract
from invaria.query import disclosure
from invaria.query.models import AccessProfile, IntegrityReport

PINNED_1_0 = Path(__file__).resolve().parents[1] / "fixtures/analytics/integrity-report-1.0.json"
VIEW_ID = "dv-" + "1" * 32


def report_1_0() -> IntegrityReport:
    return parse_contract(IntegrityReport, PINNED_1_0.read_text("utf-8"))


def as_1_1(report: IntegrityReport, **distribution: Any) -> IntegrityReport:
    """The pinned report under the definition the aggregate policy describes. Only the
    distribution is read by the projection; the rest of the report is not reinterpreted."""
    d = report.distribution.model_copy(update=distribution)
    return report.model_copy(
        update={
            "definition": "integrity-analytics@1.1.0",
            "selection_policy": "current-publication@2",
            "distribution": d,
        }
    )


AGGREGATE = disclosure.CATALOGUE[disclosure.AGGREGATE_POLICY]


def test_the_catalogue_is_valid_versioned_and_minimal() -> None:
    # 1.1.0 (D-029-8) adds three full policies; the two of 1.0.0 are unchanged.
    assert disclosure.CATALOGUE_REF == "disclosure-catalogue@1.1.0"
    assert sorted(disclosure.CATALOGUE) == [
        "aggregate-outcome-counts@1",
        "full-evidence@1",
        "full-operations@1",
        "full-report-index@1",
        "full-report-summary@1",
    ]
    assert (AGGREGATE.resource_kind, AGGREGATE.fields_sha256) == ("integrity_report_summary", None)
    assert disclosure.validate_catalogue(disclosure.CATALOGUE) == []
    full = disclosure.CATALOGUE[disclosure.FULL_SUMMARY_POLICY]
    assert (full.modality, full.issuable) == ("full", False)
    assert (AGGREGATE.modality, AGGREGATE.issuable) == ("selected_aggregates", True)
    # Every entry must stay well-formed: these mutations are refused.
    broken = {
        "x@0": dataclasses.replace(AGGREGATE, policy_ref="x@0"),
        "full@1": dataclasses.replace(full, policy_ref="full@1", issuable=True),
        "agg@2": dataclasses.replace(AGGREGATE, policy_ref="agg@2", supported_reports=None),
        "p@1": dataclasses.replace(AGGREGATE, policy_ref="p@1", declared_purpose=""),
    }
    assert len(disclosure.validate_catalogue(broken)) == 4


def test_a_policy_serves_only_when_known_and_assigned() -> None:
    assert disclosure.policy(disclosure.AGGREGATE_POLICY, [disclosure.AGGREGATE_POLICY])
    for ref, assigned in (
        (disclosure.AGGREGATE_POLICY, []),  # known, not assigned
        (disclosure.AGGREGATE_POLICY, [disclosure.FULL_SUMMARY_POLICY]),
        ("retired-policy@1", ["retired-policy@1"]),  # assigned, unknown to the code
        ("aggregate-outcome-counts@2", ["aggregate-outcome-counts@2"]),  # unknown version
    ):
        with pytest.raises(disclosure.DisclosureError) as caught:
            disclosure.policy(ref, assigned)
        assert caught.value.code == "POLICY"
    assert disclosure.issuable(disclosure.AGGREGATE_POLICY) is AGGREGATE
    for ref in (disclosure.FULL_SUMMARY_POLICY, "unknown@1"):
        with pytest.raises(disclosure.DisclosureError):
            disclosure.issuable(ref)


def test_the_projection_copies_the_report_aggregates_and_nothing_else() -> None:
    report = as_1_1(report_1_0())
    view = disclosure.project(AGGREGATE, report, VIEW_ID)
    d = report.distribution
    assert view.included_operations == d.included == 4
    o = view.outcomes
    assert (o.match_count, o.break_count, o.unknown_count, o.denominator) == (0, 1, 2, 3)
    assert (o.denominator, view.outside_outcomes.denominator) == (d.admitted, d.included)
    # included - admitted, equal to the report's non-admitted statuses (here, one operation
    # registered and not evaluated): a count, not a result.
    assert view.outside_outcomes.operations == 1
    assert view.policy.model_dump() == {"policy_id": "aggregate-outcome-counts", "version": 1}
    document = view.model_dump(mode="json")
    assert disclosure.allowlist_problems(document, disclosure.AGGREGATE_ALLOWLIST) == []
    text = json.dumps(document)
    for secret in (
        report.report_sha256,
        report.scope.tenant_id,
        report.scope.principal_id,
        *[m.operation_ref for m in report.members],
        *[m.selected.evaluation_id for m in report.members if m.selected],
    ):
        assert secret not in text
    assert not re.search(r"[0-9a-f]{64}", text)
    assert not re.search(r"\d{4}-\d{2}-\d{2}", text)  # no dates
    for word in ("fund-", "invaria-engine", "cases_key", "/v1", "/reports", '"segments"'):
        assert word not in text
    assert view.statements[0] == (
        "This view contains selected information from the original report. It does not allow "
        "reproducing the evaluation or independently verifying its correspondence with the "
        "original."
    )


def test_withheld_categories_are_fixed_and_independent_of_the_content() -> None:
    a = disclosure.project(AGGREGATE, as_1_1(report_1_0()), VIEW_ID)
    b = disclosure.project(
        AGGREGATE,
        as_1_1(
            report_1_0(),
            included=9,
            admitted=9,
            match_count=9,
            break_count=0,
            unknown_count=0,
            statuses={
                **report_1_0().distribution.statuses,
                "admitted": 9,
                "registered_not_evaluated": 0,
            },
        ),
        "dv-" + "2" * 32,
    )
    assert a.withheld == b.withheld == list(disclosure.WITHHELD)
    fixed = ("withheld_meaning", "statements", "counting_definition", "included_definition")
    assert all(getattr(a, f) == getattr(b, f) for f in fixed)
    assert a.outside_outcomes.definition == b.outside_outcomes.definition


def test_reports_the_policy_does_not_describe_or_that_do_not_add_up_are_refused() -> None:
    with pytest.raises(disclosure.DisclosureError) as caught:
        disclosure.project(AGGREGATE, report_1_0(), VIEW_ID)  # definition 1.0.0
    assert caught.value.code == "UNSUPPORTED"
    for bad in (
        {"match_count": 1},  # MATCH + BREAK + UNKNOWN != admitted
        {"included": 5},  # outside differs from the non-admitted statuses
        {"admitted": 2},
    ):
        with pytest.raises(disclosure.DisclosureError) as caught:
            disclosure.project(AGGREGATE, as_1_1(report_1_0(), **bad), VIEW_ID)
        assert caught.value.code == "INCONSISTENT"
    full = disclosure.CATALOGUE[disclosure.FULL_SUMMARY_POLICY]
    with pytest.raises(disclosure.DisclosureError):
        disclosure.project(full, as_1_1(report_1_0()), VIEW_ID)


def test_the_allowlist_finds_extra_missing_and_nested_keys() -> None:
    view = disclosure.project(AGGREGATE, as_1_1(report_1_0()), VIEW_ID).model_dump(mode="json")
    extra = {**view, "report_sha256": "x"}
    nested = {**view, "outcomes": {**view["outcomes"], "evaluation_id": "e"}}
    missing = {k: v for k, v in view.items() if k != "statements"}
    allowed = disclosure.AGGREGATE_ALLOWLIST
    assert disclosure.allowlist_problems(extra, allowed) == ["report_sha256: not allowed"]
    assert disclosure.allowlist_problems(nested, allowed) == ["outcomes.evaluation_id: not allowed"]
    assert disclosure.allowlist_problems(missing, allowed) == ["statements: missing"]


def test_access_profiles_name_policies_explicitly_and_never_grant_scopes() -> None:
    base = {"schema_version": "1.0", "principal_id": "p", "tenant_id": "t", "max_items": 5}
    old = parse_contract(AccessProfile, json.dumps({**base, "scopes": ["reports:read"]}))
    assert old.disclosure_policies == []  # no implicit policy: the summary route refuses it
    reviewer = parse_contract(
        AccessProfile,
        json.dumps(
            {
                **base,
                "scopes": ["disclosures:read"],
                "disclosure_policies": ["aggregate-outcome-counts@1"],
            }
        ),
    )
    assert reviewer.scopes == ["disclosures:read"]
    with pytest.raises(ValueError):
        parse_contract(
            AccessProfile,
            json.dumps({**base, "scopes": ["reports:read"], "disclosure_policies": ["Bad Ref"]}),
        )


def test_the_projection_is_refused_when_it_exceeds_its_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The output check is wired into the projection, not only available: with a narrower
    allowlist than the view, nothing is returned (defence in depth over the closed contract)."""
    narrower = {k: v for k, v in disclosure.AGGREGATE_ALLOWLIST.items() if k != "statements"}
    monkeypatch.setattr(disclosure, "AGGREGATE_ALLOWLIST", narrower)
    with pytest.raises(disclosure.DisclosureError) as caught:
        disclosure.project(AGGREGATE, as_1_1(report_1_0()), VIEW_ID)
    assert caught.value.code == "POLICY"
