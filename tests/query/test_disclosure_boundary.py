"""The common disclosure boundary of the interactive queries, pure part: the
requirement table against the real use cases and channels, refusal before any read, the
requirements themselves, startup warnings and the pinned field scope of the full policies.
Routes, feed and links over PostgreSQL are in ``tests/console/test_disclosure_boundary_pg.py``.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
import typing
from pathlib import Path
from typing import Any

import pytest

from invaria import cli
from invaria.console import app as console_app
from invaria.contracts.base import parse_contract
from invaria.contracts.events import (
    EvaluationPublishedEvent,
    ReportRegisteredEvent,
    ScopeReopenedEvent,
)
from invaria.mcp_server import TOOL_NAMES, build_server
from invaria.query import disclosure, feed
from invaria.query.models import AccessProfile, AggregateDisclosureView, IntegrityReport
from invaria.query.service import QueryError, QueryService

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "tests/fixtures/disclosure/full-policy-fields.json"
PINNED_REPORT = ROOT / "tests/fixtures/analytics/integrity-report-1.0.json"
FULL = [
    disclosure.FULL_OPERATIONS_POLICY,
    disclosure.FULL_EVIDENCE_POLICY,
    disclosure.FULL_REPORT_INDEX_POLICY,
    disclosure.FULL_SUMMARY_POLICY,
]
ALL_SCOPES = [
    "operations:read",
    "evidence:read",
    "reports:read",
    "reports:register",
    "events:read",
    "disclosures:issue",
    "disclosures:revoke",
    "disclosures:read",
]
EVENT_MODELS = {
    "operation.scope_reopened": ScopeReopenedEvent,
    "operation.evaluation_published": EvaluationPublishedEvent,
    "integrity_report.registered": ReportRegisteredEvent,
}


def profile(scopes: list[str], policies: list[str]) -> AccessProfile:
    return parse_contract(
        AccessProfile,
        json.dumps(
            {
                "schema_version": "1.0",
                "principal_id": "p-1",
                "tenant_id": "t-1",
                "scopes": scopes,
                "max_items": 10,
                "disclosure_policies": policies,
            }
        ),
    )


def use_case_sources() -> dict[str, str]:
    """Public QueryService methods and their source."""
    return {
        name: inspect.getsource(member)
        for name, member in inspect.getmembers(QueryService, inspect.isfunction)
        if not name.startswith("_")
    }


# ------------------------------------------------ inventory against the real code


def test_every_use_case_has_one_explicit_decision_and_its_own_scopes() -> None:
    calls: dict[str, set[str]] = {}
    for name, source in use_case_sources().items():
        if name == "allows":
            continue
        tree = ast.parse(inspect.cleandoc("\n" + source) if source[0] == " " else source)
        found: list[tuple[str, list[str]]] = []  # (method called, its literal arguments)
        for n in ast.walk(tree):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("_call", "_require")
            ):
                literals = [str(a.value) for a in n.args if isinstance(a, ast.Constant)]
                found.append((n.func.attr, literals))
        assert any(kind == "_call" for kind, _ in found), f"{name} bypasses _call"
        scopes: set[str] = set()
        for kind, literals in found:
            if kind == "_call":
                assert literals[0] == name, f"{name} records itself as {literals[0]}"
                scopes.add(literals[1])
            else:
                scopes.add(literals[0])
        calls[name] = scopes
    # One explicit decision per use case, no more and no fewer; scopes as the code checks.
    assert set(calls) == set(disclosure.USE_CASES)
    for name, scopes in calls.items():
        assert scopes == set(disclosure.USE_CASES[name].scopes), name
    for name, req in disclosure.USE_CASES.items():
        if req.mode == "policies":
            assert req.policies and all(p in disclosure.CATALOGUE for p in req.policies), name
        else:
            assert not req.policies and req.reason, name  # an exemption states its reason


def _calls_in(function: Any, owner: str) -> set[str]:
    source = inspect.cleandoc("\n" + inspect.getsource(function))
    return set(re.findall(rf"\b{owner}\.([a-z_]+)\(", source))


def test_every_channel_reaches_data_only_through_the_bounded_use_cases() -> None:
    app_calls = set()
    for method in (
        console_app.ConsoleApp._api,
        console_app.ConsoleApp._html,
        console_app.ConsoleApp._events_page,
    ):
        app_calls |= _calls_in(method, "svc") | _calls_in(method, "self.service")
    assert app_calls and app_calls <= set(disclosure.USE_CASES), app_calls - set(
        disclosure.USE_CASES
    )
    # MCP tools are named after the use case they call; nothing else is registered.
    assert set(TOOL_NAMES) <= set(disclosure.USE_CASES)
    mcp_source = inspect.getsource(build_server)
    assert set(re.findall(r"service\.([a-z_]+)\(", mcp_source)) == set(TOOL_NAMES)
    cli_calls = _calls_in(cli.cmd_analytics, "service") | _calls_in(cli.cmd_disclosure, "service")
    assert cli_calls <= set(disclosure.USE_CASES), cli_calls
    # No channel touches the store or the database directly.
    for module in (console_app, cli, inspect.getmodule(build_server)):
        source = inspect.getsource(module)  # type: ignore[arg-type]
        assert ".store." not in source and "service.store" not in source, module
    for function in (cli.cmd_analytics, cli.cmd_disclosure, cli.cmd_console_serve):
        assert "PgStore(" not in inspect.getsource(function) or function is cli.cmd_console_serve


# ------------------------------------------------- refusal before any read


class Untouchable:
    """A store that fails the test if anything is read from it."""

    touched: list[str]

    def __init__(self) -> None:
        object.__setattr__(self, "touched", [])

    def __getattr__(self, name: str) -> Any:
        self.touched.append(name)
        raise AssertionError(f"the store was used: {name}")


def arguments() -> dict[str, tuple[Any, ...]]:
    report = parse_contract(IntegrityReport, PINNED_REPORT.read_text("utf-8"))
    ev = "eval-" + "a" * 32
    return {
        "trace_operation": ("SUB-0001",),
        "get_conclusion_as_known_at": ("SUB-0001", "2026-10-01T17:00:00Z", "2026-10-01T18:00:00Z"),
        "get_missing_evidence": (ev, "c"),
        "diagnose_control": (ev, "c"),
        "get_operation_obligations": ("SUB-0001",),
        "explain_discrepancy": (ev, "c"),
        "explain_conclusion_change": (ev, ev),
        "get_coverage": ("SUB-0001",),
        "list_operations": (),
        "get_evaluation": (ev,),
        "get_timeline": ("SUB-0001",),
        "compare_evaluations": (ev, ev),
        "integrity_report": ("2026-10-20T00:00:00Z",),
        "integrity_cases": (report, "included"),
        "reproduce_integrity_report": (report,),
        "register_integrity_report": ("2026-10-20T00:00:00Z",),
        "get_evidence": ("obs-B1",),
        "list_integrity_reports": (),
        "get_integrity_report_summary": ("ir-" + "a" * 64,),
        "registered_integrity_cases": ("ir-" + "a" * 64, "included"),
        "issue_disclosure": ("ir-" + "a" * 64, "reviewer-1", disclosure.AGGREGATE_POLICY),
        "list_disclosures": (),
        "get_disclosure": ("dv-" + "0" * 32,),
    }


@pytest.mark.parametrize("policies", [[], ["unknown-policy@1"], [disclosure.AGGREGATE_POLICY]])
def test_without_a_policy_every_bounded_use_case_refuses_before_reading(
    policies: list[str],
) -> None:
    args = arguments()
    assert set(args) == {
        n
        for n, r in disclosure.USE_CASES.items()
        if r.mode != "per_event_type" and r.mode != "administrative"
    }
    for name, values in args.items():
        req = disclosure.USE_CASES[name]
        if req.mode == "restricted_view" and policies == [disclosure.AGGREGATE_POLICY]:
            continue  # the restricted view is exactly what this policy serves
        store = Untouchable()
        service = QueryService(store, profile(ALL_SCOPES, policies))  # type: ignore[arg-type]
        with pytest.raises(QueryError) as caught:
            getattr(service, name)(*values)
        assert caught.value.code == "DISCLOSURE_POLICY_UNAVAILABLE", name
        assert caught.value.message == disclosure.POLICY_MESSAGE
        assert store.touched == [], (name, store.touched)


def test_a_scope_is_checked_first_and_a_policy_grants_no_scope() -> None:
    store = Untouchable()
    service = QueryService(store, profile(["disclosures:read"], FULL))  # type: ignore[arg-type]
    with pytest.raises(QueryError) as caught:
        service.list_operations()
    assert caught.value.code == "FORBIDDEN" and store.touched == []


def test_an_undecided_use_case_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(disclosure.USE_CASES, "list_operations")
    store = Untouchable()
    service = QueryService(store, profile(ALL_SCOPES, FULL))  # type: ignore[arg-type]
    with pytest.raises(QueryError) as caught:
        service.list_operations()
    assert caught.value.code == "DISCLOSURE_POLICY_UNAVAILABLE" and store.touched == []


# ------------------------------------------------------------- requirements


def test_requirements_need_every_policy_known_full_and_assigned() -> None:
    cases = disclosure.USE_CASES
    assert disclosure.require("list_operations", [disclosure.FULL_OPERATIONS_POLICY])
    refused = [
        ("list_operations", []),
        ("list_operations", ["full-operations@2"]),  # unknown version
        ("list_operations", [disclosure.AGGREGATE_POLICY]),  # restricted, not full
        ("list_operations", [disclosure.FULL_SUMMARY_POLICY]),  # another family
        ("registered_integrity_cases", [disclosure.FULL_OPERATIONS_POLICY]),  # one of two
        ("registered_integrity_cases", [disclosure.FULL_SUMMARY_POLICY]),
        ("issue_disclosure", [disclosure.FULL_REPORT_INDEX_POLICY]),
        ("list_disclosures", FULL),  # full policies do not give restricted views
        ("not_a_use_case", FULL),
    ]
    for name, assigned in refused:
        with pytest.raises(disclosure.DisclosureError):
            disclosure.require(name, assigned)
    assert disclosure.require("registered_integrity_cases", FULL)
    assert disclosure.require("revoke_disclosure", []).mode == "administrative"
    assert cases["issue_disclosure"].policies == (disclosure.FULL_SUMMARY_POLICY,)


def test_the_requirement_table_names_only_full_catalogue_policies() -> None:
    assert disclosure.validate_requirements(disclosure.USE_CASES, disclosure.EVENT_TYPES) == []
    broken = {
        **disclosure.USE_CASES,
        "list_operations": disclosure.Requirement(
            scopes=("operations:read",), mode="policies", policies=(disclosure.AGGREGATE_POLICY,)
        ),
        "get_timeline": disclosure.Requirement(scopes=("operations:read",), mode="policies"),
        "get_coverage": disclosure.Requirement(scopes=("operations:read",), mode="administrative"),
    }
    events = {**disclosure.EVENT_TYPES, "x": ("operations:read", ("full-operations@9",))}
    assert len(disclosure.validate_requirements(broken, events)) == 4


def test_allows_needs_scope_and_policy_and_disclosures_read_inherits_nothing() -> None:
    reviewer = (["disclosures:read"], [disclosure.AGGREGATE_POLICY])
    assert disclosure.allows("get_disclosure", *reviewer)
    assert not any(
        disclosure.allows(n, *reviewer)
        for n in disclosure.USE_CASES
        if not n.endswith("disclosure") and n != "list_disclosures"
    )
    assert not disclosure.allows("list_operations", ["operations:read"], [])  # scope only
    assert not disclosure.allows("list_operations", [], FULL)  # policy only
    assert disclosure.allows("list_operations", ["operations:read"], FULL)
    # The feed is available only if at least one type is readable now.
    assert not disclosure.allows("list_events", ["events:read", "operations:read"], [])
    assert disclosure.allows("list_events", ["events:read", "operations:read"], FULL)


def test_event_types_need_their_scope_and_policy_now() -> None:
    types = tuple(disclosure.EVENT_TYPES)
    assert set(types) == set(feed.RESOURCE_SCOPE)
    assert all(disclosure.EVENT_TYPES[t][0] == feed.RESOURCE_SCOPE[t] for t in types)
    scopes = ["events:read", "operations:read", "reports:read"]
    assert feed.readable_types(scopes, [], types) == ()
    assert feed.readable_types(scopes, [disclosure.FULL_OPERATIONS_POLICY], types) == (
        "operation.scope_reopened",
        "operation.evaluation_published",
    )
    assert feed.readable_types(scopes, FULL, types) == types
    assert feed.readable_types(["events:read"], FULL, types) == ()  # policies grant no scope


def test_startup_warnings_name_scopes_and_policies_only() -> None:
    notes = disclosure.startup_warnings(
        ["operations:read", "evidence:read", "reports:read", "events:read"],
        ["full-operations@2", disclosure.FULL_REPORT_INDEX_POLICY],
    )
    text = "\n".join(notes)
    assert "full-operations@2 is unknown" in text
    assert "scope operations:read without disclosure policy full-operations@1" in text
    assert "scope evidence:read without disclosure policy full-evidence@1: 1 outputs" in text
    assert "full-report-summary@1" in text and "full-report-index@1 " not in text
    assert "event type operation.evaluation_published not delivered" in text
    assert "p-1" not in text and "t-1" not in text
    assert disclosure.startup_warnings(ALL_SCOPES, [*FULL, disclosure.AGGREGATE_POLICY]) == []


def test_the_console_and_mcp_print_the_warnings_to_the_operator_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._disclosure_warnings(profile(["operations:read"], []))
    captured = capsys.readouterr()
    assert captured.out == "" and "full-operations@1" in captured.err
    for name in ("cmd_console_serve", "cmd_mcp_serve"):
        assert "_disclosure_warnings(access)" in inspect.getsource(getattr(cli, name))


# ----------------------------------------------------- field scope (D-029-10)


def policy_models() -> dict[str, set[type[Any]]]:
    """The contracts each full policy delivers: the return types of its use cases (both
    elements of a tuple) and the event types it makes readable."""
    models: dict[str, set[type[Any]]] = {p: set() for p in FULL}
    for name, req in disclosure.USE_CASES.items():
        hints = typing.get_type_hints(getattr(QueryService, name))
        returned = hints["return"]
        found = typing.get_args(returned) if typing.get_origin(returned) is tuple else (returned,)
        for p in req.policies:
            models[p] |= set(found)
    for event_type, (_, policies) in disclosure.EVENT_TYPES.items():
        for p in policies:
            models[p].add(EVENT_MODELS[event_type])
    return models


def current_fields() -> dict[str, list[str]]:
    return {
        p: sorted(set().union(*(disclosure.field_paths(m) for m in models)))
        for p, models in policy_models().items()
    }


def test_new_fields_of_a_full_policy_need_an_explicit_review() -> None:
    """A field added, renamed or nested anew in any contract a full policy delivers changes
    the policy's pinned scope. Descriptions, titles and types do not. To accept a change
    that reveals nothing new (or after creating a new policy version), regenerate with
    INVARIA_REGENERATE_DISCLOSURE_FIELDS=1 and update FIELDS_SHA256: an explicit decision."""
    fields = current_fields()
    if os.environ.get("INVARIA_REGENERATE_DISCLOSURE_FIELDS") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(fields, indent=1, sort_keys=True) + "\n", "utf-8")
    pinned = json.loads(SNAPSHOT.read_text("utf-8"))
    for policy, paths in fields.items():
        added = sorted(set(paths) - set(pinned[policy]))
        removed = sorted(set(pinned[policy]) - set(paths))
        assert not added and not removed, (policy, added[:10], removed[:10])
        assert disclosure.CATALOGUE[policy].fields_sha256 == disclosure.fields_sha256(set(paths))


def test_field_paths_include_nested_fields_and_ignore_descriptions() -> None:
    from pydantic import BaseModel, Field

    class Inner(BaseModel):
        amount: int

    class Outer(BaseModel):
        items: list[Inner]
        note: str = Field(description="v1")

    class Documented(BaseModel):
        items: list[Inner]
        note: str = Field(description="different words, same information")

    class Widened(BaseModel):
        items: list[Inner]
        note: str
        account: str

    assert disclosure.field_paths(Outer) == {"Outer.items", "Outer.items[].amount", "Outer.note"}
    same = {p.replace("Documented", "Outer") for p in disclosure.field_paths(Documented)}
    assert same == disclosure.field_paths(Outer)
    assert "Widened.account" in disclosure.field_paths(Widened)


def test_the_restricted_view_contract_is_exactly_its_allowlist() -> None:
    """The restricted projection never receives a new field automatically: its contract and
    its allowlist must name the same paths."""

    def paths(allowed: dict[str, Any], prefix: str = "AggregateDisclosureView") -> set[str]:
        out = set()
        for key, nested in allowed.items():
            out.add(f"{prefix}.{key}")
            if nested is not None:
                out |= paths(nested, f"{prefix}.{key}")
        return out

    assert disclosure.field_paths(AggregateDisclosureView) == paths(disclosure.AGGREGATE_ALLOWLIST)
