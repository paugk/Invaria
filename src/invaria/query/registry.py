"""Registered integrity reports: exact bytes, verification, summary and cursors.

Pure functions, no I/O. The registry stores the bytes the service serialized; reading
verifies them before anything is served:

* ``document`` = UTF-8 of ``json.dumps(report.model_dump(mode="json"), sort_keys=True,
  separators=(",", ":"), ensure_ascii=False)``: the canonical form of the integrity report, with the
  ``report_sha256`` key included and optional absent fields omitted (format 1.0 has no
  ``temporal``). ``report_sha256`` is the sha256 of the same serialization without that key
  (``analytics.report_sha256``, unchanged for formats 1.0 and 1.1). These are Invaria's own
  canonical bytes, not RFC 8785: no format is re-hashed under another canonicalization.
* The database never re-serializes them (``bytea``), so jsonb normalization is irrelevant.

What a registered report proves: that this service computed and stored those bytes under
this deployment's store and roles. It is not a signature, it does not authenticate the
issuer to third parties, it does not protect against an administrator who can rewrite the
whole database, and it does not show that the population covers all real activity.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from invaria.contracts.base import StrictJsonError, parse_contract
from invaria.query import analytics
from invaria.query.models import (
    BlockersSummary,
    DiscrepanciesSummary,
    DistributionSummary,
    IntegrityReport,
    IntegrityReportSummaryView,
)

REPORT_ID = re.compile(r"^ir-[a-f0-9]{64}$")
SUPPORTED = frozenset(
    (r.report_format, r.definition, r.selection_policy)
    for r in (analytics.RULES_V1, analytics.RULES_V2)
)
# Default ceiling for a document at registration (measured).
DEFAULT_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
# Hard ceiling enforced by the table's CHECK (64 MiB): a configured limit cannot exceed it.
STORE_MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_CURSOR_CHARS = 1024  # longest key (~220 chars) encodes to ~450
CURSOR_VERSION = 1
CASES_ORDER = "operation_ref"
LIST_ORDER = "registered_at_desc,report_id_desc"

REGISTRY_LIMITATIONS = [
    "The registry gives a stable reference under trust in this service and its store: not a "
    "digital signature, not independent authenticity, and no protection against an "
    "administrator able to rewrite the whole database.",
    "report_sha256 checks the document's internal consistency; it does not authenticate its "
    "issuer.",
    "The population is the registered accessible operations at the report's store read; it "
    "does not show that all real activity was registered.",
    "Publication state is the one present at the store read; the evidence horizon does not "
    "reconstruct what was published then. Ages are knowledge time, not economic time pending.",
    "UNKNOWN is not non-payment; a retired engine's publication is counted apart and is not "
    "an UNKNOWN; a proven discrepancy can coexist with unresolved requirements.",
    "No automatic deletion or expiry: this local decision does not settle an institutional "
    "retention policy.",
]


class RegistryError(Exception):
    """A registered report cannot be served. ``code`` is the query error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def canonical_bytes(report: IntegrityReport) -> bytes:
    text = json.dumps(
        report.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return text.encode("utf-8")


def report_id(report_sha256: str) -> str:
    return f"ir-{report_sha256}"


@dataclass(frozen=True)
class ReportRecord:
    """The row of a registered report (columns plus the stored bytes)."""

    tenant_id: str
    report_id: str
    report_sha256: str
    report_format: str
    definition: str
    selection_policy: str
    evidence_horizon: datetime
    store_read_at: datetime | None
    registered_by: str
    document: bytes
    document_sha256: str
    registered_at: datetime | None = None


def record_for(report: IntegrityReport) -> ReportRecord:
    """The row to insert for a report this service just computed."""
    if analytics.report_sha256(report) != report.report_sha256:
        raise ValueError("the report does not match its report_sha256")
    document = canonical_bytes(report)
    return ReportRecord(
        tenant_id=report.scope.tenant_id,
        report_id=report_id(report.report_sha256),
        report_sha256=report.report_sha256,
        report_format=report.report_format,
        definition=report.definition,
        selection_policy=report.selection_policy,
        evidence_horizon=report.as_of,
        store_read_at=report.store_read.read_at,
        registered_by=report.scope.principal_id,
        document=document,
        document_sha256=hashlib.sha256(document).hexdigest(),
    )


def verify(record: ReportRecord, tenant_id: str) -> IntegrityReport:
    """The stored report, or ``RegistryError``. Order matters: corruption of the bytes
    first, then a format this code does not support (a limitation, never corruption), then
    agreement between bytes, columns and the report's own hash and figures."""
    if hashlib.sha256(record.document).hexdigest() != record.document_sha256:
        raise RegistryError("INTEGRITY_ERROR", "the stored report fails its integrity check")
    try:
        head = json.loads(record.document.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise RegistryError(
            "INTEGRITY_ERROR", "the stored report fails its integrity check"
        ) from error
    declared = (record.report_format, record.definition, record.selection_policy)
    if not isinstance(head, dict) or declared != tuple(
        head.get(k) for k in ("report_format", "definition", "selection_policy")
    ):
        raise RegistryError("INTEGRITY_ERROR", "the stored report fails its integrity check")
    if declared not in SUPPORTED:
        raise RegistryError(
            "UNSUPPORTED_REPORT_VERSION",
            "this version of Invaria cannot read the report's format "
            f"{record.report_format} ({record.definition}, {record.selection_policy})",
        )
    try:
        report = parse_contract(IntegrityReport, record.document.decode("utf-8"))
    except (UnicodeDecodeError, StrictJsonError, ValidationError, ValueError) as error:
        raise RegistryError(
            "INTEGRITY_ERROR", "the stored report fails its integrity check"
        ) from error
    columns = (
        record.tenant_id,
        record.report_id,
        record.report_sha256,
        record.report_format,
        record.definition,
        record.selection_policy,
        record.evidence_horizon,
        record.store_read_at,
    )
    document = (
        report.scope.tenant_id,
        report_id(report.report_sha256),
        report.report_sha256,
        report.report_format,
        report.definition,
        report.selection_policy,
        report.as_of,
        report.store_read.read_at,
    )
    if (
        record.tenant_id != tenant_id
        or columns != document
        or analytics.report_sha256(report) != report.report_sha256
        or canonical_bytes(report) != record.document
        or analytics.consistency_problems(report)
    ):
        raise RegistryError("INTEGRITY_ERROR", "the stored report fails its integrity check")
    return report


@dataclass(frozen=True)
class Pins:
    """What a report pins in the store, for one existence check at read and registration."""

    operations: list[str]
    epochs: list[tuple[str, int]]
    evaluations: list[tuple[str, str]]  # (operation_ref, evaluation_id)
    publications: list[tuple[str, int, str]]  # (operation_ref, epoch, evaluation_id)


def pins(report: IntegrityReport) -> Pins:
    evaluations: set[tuple[str, str]] = set()
    publications: set[tuple[str, int, str]] = set()
    epochs: set[tuple[str, int]] = set()
    for m in report.members:
        if m.scope_epoch is not None:
            epochs.add((m.operation_ref, m.scope_epoch))
        if m.latest_stored_evaluation_id is not None:
            evaluations.add((m.operation_ref, m.latest_stored_evaluation_id))
        if m.selected is not None:
            evaluations.add((m.operation_ref, m.selected.evaluation_id))
        for ref in m.published_history:
            evaluations.add((m.operation_ref, ref.evaluation_id))
            publications.add((m.operation_ref, ref.epoch, ref.evaluation_id))
    return Pins(
        operations=sorted(m.operation_ref for m in report.members),
        epochs=sorted(epochs),
        evaluations=sorted(evaluations),
        publications=sorted(publications),
    )


def summary(report: IntegrityReport, registered_at: datetime) -> IntegrityReportSummaryView:
    d, x, b = report.distribution, report.discrepancies, report.blockers
    return IntegrityReportSummaryView(
        schema_version="1.0",
        report_id=report_id(report.report_sha256),
        report_sha256=report.report_sha256,
        report_format=report.report_format,
        definition=report.definition,
        selection_policy=report.selection_policy,
        selection_description=report.selection_description,
        population=report.scope.population,
        isolation=report.scope.isolation,
        evidence_horizon=report.as_of,
        store_read=report.store_read,
        temporal=report.temporal,
        registered_at=registered_at,
        profile_filter=report.profile_filter,
        max_replays=report.max_replays,
        diagnostic_engines=report.diagnostic_engines,
        members=len(report.members),
        distribution=DistributionSummary(
            included=d.included,
            admitted=d.admitted,
            match_count=d.match_count,
            break_count=d.break_count,
            unknown_count=d.unknown_count,
            technical_error=d.technical_error,
            statuses=d.statuses,
            stored_evaluations=d.stored_evaluations,
            evaluations_not_selected=d.evaluations_not_selected,
            metrics=d.metrics,
        ),
        segments=report.segments,
        discrepancies=DiscrepanciesSummary(
            operations_with_discrepancy=x.operations_with_discrepancy,
            control_discrepancies=x.control_discrepancies,
            groups=x.groups,
            metrics=x.metrics,
        ),
        blockers=BlockersSummary(
            complete=b.complete,
            max_replays=b.max_replays,
            evaluations_requiring_diagnosis=b.evaluations_requiring_diagnosis,
            replayed=b.replayed,
            not_processed=len(b.not_processed),
            operations_with_block=b.operations_with_block,
            blocked_requirements=b.blocked_requirements,
            groups=b.groups,
            not_evaluated=len(b.not_evaluated),
            diagnosis_unavailable=len(b.diagnosis_unavailable),
            reconstruction_mismatch=len(b.reconstruction_mismatch),
            metrics=b.metrics,
        ),
        exclusions=report.exclusions,
        case_keys=analytics.case_keys(report),
        report_limitations=report.limitations,
        registry_limitations=REGISTRY_LIMITATIONS,
    )


# ------------------------------------------------------------------------------ cursors
# Unsigned: a cursor grants nothing. It only names a position inside a population the
# request is already authorized for; every page is re-authorized and re-verified.


def _encode(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(cursor: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= MAX_CURSOR_CHARS:
        raise RegistryError("VALIDATION_ERROR", "invalid cursor")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
        raise RegistryError("VALIDATION_ERROR", "invalid cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise RegistryError("VALIDATION_ERROR", "invalid cursor") from error
    if not isinstance(payload, dict) or set(payload) != keys or payload.get("v") != CURSOR_VERSION:
        raise RegistryError("VALIDATION_ERROR", "invalid cursor")
    return payload


def cases_cursor(report_id_: str, key: str, offset: int) -> str:
    return _encode({"v": CURSOR_VERSION, "r": report_id_, "k": key, "s": CASES_ORDER, "o": offset})


def cases_offset(cursor: str, report_id_: str, key: str, total: int) -> int:
    """The offset a cursor names, if it was issued for this report, key and order."""
    p = _decode(cursor, {"v", "r", "k", "s", "o"})
    offset = p["o"]
    if (
        p["r"] != report_id_
        or p["k"] != key
        or p["s"] != CASES_ORDER
        or not isinstance(offset, int)
        or isinstance(offset, bool)
        or not 0 < offset < total
    ):
        raise RegistryError("VALIDATION_ERROR", "the cursor does not belong to this request")
    return offset


def list_cursor(registered_at: datetime, report_id_: str) -> str:
    return _encode(
        {"v": CURSOR_VERSION, "s": LIST_ORDER, "t": registered_at.isoformat(), "r": report_id_}
    )


def list_position(cursor: str) -> tuple[datetime, str]:
    p = _decode(cursor, {"v", "s", "t", "r"})
    if p["s"] != LIST_ORDER or not isinstance(p["r"], str) or not REPORT_ID.fullmatch(p["r"]):
        raise RegistryError("VALIDATION_ERROR", "invalid cursor")
    try:
        moment = datetime.fromisoformat(p["t"]) if isinstance(p["t"], str) else None
    except ValueError:
        moment = None
    if moment is None or moment.utcoffset() is None:
        raise RegistryError("VALIDATION_ERROR", "invalid cursor")
    return moment, p["r"]
