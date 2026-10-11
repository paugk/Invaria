"""Registered integrity reports, pure: bytes, verification, summary, cursors.

Expectations were written before the code. The 1.0 report
is the pinned fixture generated before policy @2; a 1.1 report is built from an empty
population (real 1.1 reports are exercised against PostgreSQL in tests/console).
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from invaria.contracts.base import parse_contract
from invaria.query import analytics, registry
from invaria.query.analytics import IntegrityState
from invaria.query.models import IntegrityReport, ReportScopeView

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/analytics/integrity-report-1.0.json"
T0 = datetime(2026, 10, 1, tzinfo=UTC)


def report_1_0() -> IntegrityReport:
    return parse_contract(IntegrityReport, FIXTURE.read_text("utf-8"))


def report_1_1() -> IntegrityReport:
    state = IntegrityState("tenant-a", [], [], [], [], [], {}, T0, None)
    return analytics.build_report(
        state,
        as_of=T0,
        profile_refs=None,
        max_replays=10,
        diagnoser=lambda d: (_ for _ in ()).throw(AssertionError("no replay expected")),
        scope=ReportScopeView(
            tenant_id="tenant-a",
            principal_id="analyst-1",
            access_scopes=["operations:read", "reports:register"],
            population="registered_accessible_operations",
            isolation=analytics.ISOLATION,
        ),
    )


def tenant_of(report: IntegrityReport) -> str:
    return report.scope.tenant_id


@pytest.mark.parametrize("make", [report_1_0, report_1_1])
def test_the_stored_bytes_are_the_canonical_report_and_the_id_its_hash(make: Any) -> None:
    report = make()
    record = registry.record_for(report)
    expected = json.dumps(
        report.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert record.document == expected
    without = report.model_dump(mode="json")
    del without["report_sha256"]
    hashed = json.dumps(without, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert hashlib.sha256(hashed.encode("utf-8")).hexdigest() == report.report_sha256
    assert record.report_id == "ir-" + report.report_sha256
    assert record.document_sha256 == hashlib.sha256(record.document).hexdigest()
    assert record.registered_by == report.scope.principal_id
    assert registry.verify(record, tenant_of(report)) == report


def test_format_1_0_bytes_omit_temporal_and_keep_the_pinned_hash() -> None:
    report = report_1_0()
    assert b'"temporal"' not in registry.record_for(report).document
    assert report.report_format == "invaria-integrity-report@1.0"


def tamper(record: registry.ReportRecord, document: bytes) -> registry.ReportRecord:
    return replace(record, document=document, document_sha256=hashlib.sha256(document).hexdigest())


def code_of(record: registry.ReportRecord, tenant: str = "tenant-a") -> str:
    with pytest.raises(registry.RegistryError) as error:
        registry.verify(record, tenant)
    return error.value.code


def test_corrupted_bytes_are_an_integrity_error() -> None:
    record = registry.record_for(report_1_1())
    flipped = record.document.replace(b'"max_replays":10', b'"max_replays":11')
    assert flipped != record.document
    assert code_of(replace(record, document=flipped)) == "INTEGRITY_ERROR"


def test_a_coherent_rewrite_of_bytes_and_hashes_is_caught_by_the_columns_or_figures() -> None:
    record = registry.record_for(report_1_1())
    data = json.loads(record.document)
    data["distribution"]["included"] = 1  # figure no longer matches the members
    data["report_sha256"] = analytics.report_sha256(data)
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    rewritten = tamper(record, raw)
    assert code_of(rewritten) == "INTEGRITY_ERROR"  # column hash differs
    sha = data["report_sha256"]
    consistent_columns = replace(rewritten, report_sha256=sha, report_id="ir-" + sha)
    assert code_of(consistent_columns) == "INTEGRITY_ERROR"  # the figures disagree


def test_non_canonical_bytes_are_refused_even_with_the_same_content() -> None:
    report = report_1_1()
    record = registry.record_for(report)
    pretty = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True).encode()
    assert code_of(tamper(record, pretty)) == "INTEGRITY_ERROR"


def test_an_unknown_format_is_unsupported_not_corruption() -> None:
    record = registry.record_for(report_1_1())
    future = tamper(
        replace(
            record,
            report_format="invaria-integrity-report@9.0",
            definition="integrity-analytics@9.0.0",
        ),
        record.document.replace(b"@1.1", b"@9.0"),
    )
    assert code_of(future) == "UNSUPPORTED_REPORT_VERSION"
    # Corrupted bytes are reported as corruption first, whatever the format says.
    broken = replace(future, document=future.document + b" ")
    assert code_of(broken) == "INTEGRITY_ERROR"


def test_another_tenant_or_a_mismatched_column_is_an_integrity_error() -> None:
    record = registry.record_for(report_1_1())
    assert code_of(record, tenant="tenant-b") == "INTEGRITY_ERROR"
    assert code_of(replace(record, evidence_horizon=T0.replace(day=2))) == "INTEGRITY_ERROR"
    assert code_of(replace(record, definition="integrity-analytics@1.0.0")) == "INTEGRITY_ERROR"


def test_the_summary_keeps_figures_and_drops_cases_and_access_data() -> None:
    report = report_1_0()
    registered = datetime(2026, 10, 10, tzinfo=UTC)
    view = registry.summary(report, registered)
    text = view.model_dump_json()
    for member in report.members:
        assert member.operation_ref not in text  # no member lists, no case identifiers
        if member.selected is not None:
            assert member.selected.evaluation_id not in text
    assert report.scope.principal_id not in text and "access_scopes" not in text
    assert view.members == len(report.members)
    assert view.distribution.metrics == report.distribution.metrics  # numerators, denominators
    assert view.discrepancies.groups == report.discrepancies.groups
    assert view.blockers.not_processed == len(report.blockers.not_processed)
    assert view.case_keys == analytics.case_keys(report)
    assert view.registered_at == registered and view.evidence_horizon == report.as_of
    assert view.temporal is None  # format 1.0 has no temporal block: not invented
    assert view.report_limitations == report.limitations


# ---------------------------------------------------------------------------- cursors

RID = "ir-" + "a" * 64


def edited(cursor: str, **changes: Any) -> str:
    raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    payload = {**json.loads(raw), **changes}
    for key in [k for k, v in payload.items() if v is None]:
        del payload[key]
    data = json.dumps(payload).encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def test_a_cases_cursor_is_bound_to_report_key_order_and_version() -> None:
    cursor = registry.cases_cursor(RID, "result:BREAK", 5)
    assert registry.cases_offset(cursor, RID, "result:BREAK", 10) == 5
    wrong = [
        edited(cursor, r="ir-" + "b" * 64),
        edited(cursor, k="result:MATCH"),
        edited(cursor, s="evaluation_id"),
        edited(cursor, v=2),
        edited(cursor, o=0),
        edited(cursor, o=10),
        edited(cursor, o=-1),
        edited(cursor, o=True),
        edited(cursor, o="5"),
        edited(cursor, extra=1),
        edited(cursor, s=None),
        cursor + "!",
        "x" * 1100,
        "",
        "bm90IGpzb24",
    ]
    for bad in wrong:
        with pytest.raises(registry.RegistryError) as error:
            registry.cases_offset(bad, RID, "result:BREAK", 10)
        assert error.value.code == "VALIDATION_ERROR"
        assert RID not in error.value.message and "BREAK" not in error.value.message


def test_a_list_cursor_names_a_position_only() -> None:
    moment = datetime(2026, 10, 10, 12, tzinfo=UTC)
    cursor = registry.list_cursor(moment, RID)
    assert registry.list_position(cursor) == (moment, RID)
    for bad in (
        edited(cursor, r="SUB-0001"),
        edited(cursor, t="yesterday"),
        edited(cursor, t="2026-10-10T12:00:00"),
        edited(cursor, s="report_id"),
        registry.cases_cursor(RID, "included", 1),
    ):
        with pytest.raises(registry.RegistryError):
            registry.list_position(bad)
