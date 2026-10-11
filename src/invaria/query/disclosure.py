"""Controlled disclosure, first output: the summary of a registered
integrity report.

Pure: no I/O. The catalogue is versioned code: a policy is immutable once listed, and a
change is a new version. A policy restricts what an authorized principal receives; it never
grants a scope. The projection copies the original report's figures (never recomputes a
financial result) and is checked against the policy's allowlist before it is serialized.

It extends to every interactive query: ``USE_CASES`` is the one table of
what each ``QueryService`` use case requires (scopes, full policies, or an explicit special
mode), resolved in ``QueryService._call`` after the scope and before anything is read. A use
case without an entry is refused. Event types require their policy too (``EVENT_TYPES``).
Exports and operational outputs (logs, backups, telemetry) are not covered yet."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from invaria.query.models import (
    AggregateDisclosureView,
    DisclosedOutcomes,
    DisclosedOutside,
    DisclosurePolicyName,
    IntegrityReport,
    WithheldCategory,
)

CATALOGUE_REF = "disclosure-catalogue@1.1.0"
VIEW_FORMAT: Literal["invaria-disclosure-view@1"] = "invaria-disclosure-view@1"
FULL_SUMMARY_POLICY = "full-report-summary@1"
AGGREGATE_POLICY = "aggregate-outcome-counts@1"
# Full policies of the interactive queries.
FULL_OPERATIONS_POLICY = "full-operations@1"
FULL_EVIDENCE_POLICY = "full-evidence@1"
FULL_REPORT_INDEX_POLICY = "full-report-index@1"
POLICY_MESSAGE = "no disclosure policy of this access profile covers this output"

WITHHELD: tuple[WithheldCategory, ...] = (
    "source_report_identifiers_and_hashes",
    "access_scope_tenant_and_principals",
    "operations_and_evaluations",
    "financial_profiles_and_engines",
    "assets_amounts_addresses_and_evidence",
    "dates_times_and_ages",
    "filters_and_segments",
    "case_keys_and_links",
    "other_report_sections",
)
WITHHELD_MEANING = (
    "Fixed categories this policy never discloses. The list is the same for every view of "
    "this policy: it says nothing about whether the original report contains such data."
)
STATEMENTS = (
    "This view contains selected information from the original report. It does not allow "
    "reproducing the evaluation or independently verifying its correspondence with the "
    "original.",
    "It is not the complete report: the other sections of the report and every identifier "
    "are not disclosed under this policy. A withheld category is not missing evidence and "
    "not a zero.",
    "Population: the original report's, the operations registered and accessible to its "
    "issuer when the report was computed, possibly narrowed by filters this view does not "
    "disclose. It does not show that all real activity was registered or covered by "
    "evidence.",
    "MATCH, BREAK and UNKNOWN are stored conclusions under the evidence and coverage their "
    "evaluations admitted. UNKNOWN is not non-payment and BREAK is not a loss. Operations "
    "outside those results have no admitted current conclusion: that count is not a result.",
    "The counts are those of a fixed report and do not change with later evidence; this view "
    "does not say whether they are still current.",
    "Counts are exact. In small populations they can reveal information about individual "
    "operations; this policy gives no general protection against inference, including by "
    "comparing several views.",
    "The view identifier is random and opaque: it neither authenticates the view nor grants "
    "access to it. The view carries no hash, identifier or signature of the original; it is "
    "a statement of this service, under trust in its operator and store.",
)
COUNTING_DEFINITION = (
    "integrity-analytics@1.1.0 with selection policy current-publication@2: each registered "
    "operation is counted once; a conclusion is admitted only if it is the scope's current "
    "publication, from a current engine and within the report's evidence horizon."
)
INCLUDED_DEFINITION = "operations in the original report's population"
ADMITTED_DEFINITION = "operations with an admitted current conclusion (MATCH + BREAK + UNKNOWN)"
OUTSIDE_DEFINITION = (
    "included operations without an admitted current conclusion: not evaluated, not "
    "published, stale, published after the evidence horizon, or recorded with an engine that "
    "is not admitted or must be re-evaluated"
)

# The exact shape a view of this policy may have: every key, at every level. Anything else
# in the serialized view is a disclosure error, and nothing is served.
AGGREGATE_ALLOWLIST: dict[str, Any] = {
    "view_format": None,
    "view_id": None,
    "policy": {"policy_id": None, "version": None},
    "resource_kind": None,
    "modality": None,
    "counting_definition": None,
    "included_operations": None,
    "included_definition": None,
    "outcomes": {
        "match_count": None,
        "break_count": None,
        "unknown_count": None,
        "denominator": None,
        "denominator_definition": None,
    },
    "outside_outcomes": {
        "operations": None,
        "definition": None,
        "denominator": None,
        "denominator_definition": None,
    },
    "withheld": None,
    "withheld_meaning": None,
    "statements": None,
}


# Pinned field scopes of the full policies (recomputed by tests/query/test_disclosure_boundary.py).
FIELDS_SHA256: dict[str, str] = {
    FULL_OPERATIONS_POLICY: "0608c2cfce90bef9f53dbf60bd2451c440dffe865b150f14c97d7798eabedd4f",
    FULL_EVIDENCE_POLICY: "2a0e8f68373e6e490f6665581b232252aa7c1342aa0fe2906682c437336ca0de",
    FULL_REPORT_INDEX_POLICY: "f8927c1bd22db29b008da5ae86116bc9d86dcc9c7739bb5bf0817579de2064a2",
    FULL_SUMMARY_POLICY: "1720d389bba98dcb8859fb066fa82737623bd591b500cd006a267e4c4fea791e",
}


@dataclass(frozen=True)
class DisclosurePolicy:
    """One immutable catalogue entry. ``modality`` ``full`` returns the existing contract
    unchanged; ``selected_aggregates`` returns an ``AggregateDisclosureView``."""

    policy_ref: str
    resource_kind: Literal[
        "integrity_report_summary", "operation_data", "evidence", "integrity_report_index"
    ]
    modality: Literal["full", "selected_aggregates"]
    issuable: bool
    declared_purpose: str
    # (definition, selection_policy) the projection's texts are written for; None: any.
    supported_reports: frozenset[tuple[str, str]] | None
    # Full policies: sha256 of the field paths (names and nesting, not descriptions or
    # types) of every contract they deliver. A test recomputes it: a new field changes the
    # scope of the policy and needs an explicit review, and a new version if it reveals
    # more (D-029-10). None for projections: their allowlist is the contract.
    fields_sha256: str | None = None


CATALOGUE: dict[str, DisclosurePolicy] = {
    p.policy_ref: p
    for p in (
        DisclosurePolicy(
            policy_ref=FULL_SUMMARY_POLICY,
            resource_kind="integrity_report_summary",
            modality="full",
            issuable=False,  # the operator's own view; never issued to a third party
            declared_purpose="internal operation by the administrator of this environment",
            supported_reports=None,
            fields_sha256=FIELDS_SHA256[FULL_SUMMARY_POLICY],
        ),
        DisclosurePolicy(
            policy_ref=FULL_OPERATIONS_POLICY,
            resource_kind="operation_data",
            modality="full",
            issuable=False,
            declared_purpose="internal operation by the administrator of this environment",
            supported_reports=None,
            fields_sha256=FIELDS_SHA256[FULL_OPERATIONS_POLICY],
        ),
        DisclosurePolicy(
            policy_ref=FULL_EVIDENCE_POLICY,
            resource_kind="evidence",
            modality="full",
            issuable=False,
            declared_purpose="internal operation by the administrator of this environment",
            supported_reports=None,
            fields_sha256=FIELDS_SHA256[FULL_EVIDENCE_POLICY],
        ),
        DisclosurePolicy(
            policy_ref=FULL_REPORT_INDEX_POLICY,
            resource_kind="integrity_report_index",
            modality="full",
            issuable=False,
            declared_purpose="internal operation by the administrator of this environment",
            supported_reports=None,
            fields_sha256=FIELDS_SHA256[FULL_REPORT_INDEX_POLICY],
        ),
        DisclosurePolicy(
            policy_ref=AGGREGATE_POLICY,
            resource_kind="integrity_report_summary",
            modality="selected_aggregates",
            issuable=True,
            declared_purpose=(
                "external review of the aggregate outcome counts of a fixed report "
                "(declared purpose, not a technical guarantee on later use)"
            ),
            supported_reports=frozenset({("integrity-analytics@1.1.0", "current-publication@2")}),
        ),
    )
}


class DisclosureError(Exception):
    """``code``: ``POLICY`` (no usable policy), ``UNSUPPORTED`` (the policy's texts do not
    describe this report's definition) or ``INCONSISTENT`` (the report's figures do not add
    up as its definition says). Messages never name the report."""

    def __init__(self, code: Literal["POLICY", "UNSUPPORTED", "INCONSISTENT"], message: str):
        super().__init__(message)
        self.code = code


def validate_catalogue(catalogue: dict[str, DisclosurePolicy]) -> list[str]:
    problems = []
    for ref, p in catalogue.items():
        name, _, version = ref.partition("@")
        if ref != p.policy_ref or not name or not version.isdigit() or version.startswith("0"):
            problems.append(f"{ref}: malformed policy_ref")
        if p.modality == "full" and (p.issuable or p.supported_reports is not None):
            problems.append(f"{ref}: a full policy is never issued and supports every report")
        if (p.modality == "full") != (p.fields_sha256 is not None):
            problems.append(f"{ref}: a full policy pins its field scope; a projection does not")
        if p.modality == "selected_aggregates" and not p.supported_reports:
            problems.append(f"{ref}: a projection must name the reports its texts describe")
        if not p.declared_purpose:
            problems.append(f"{ref}: no declared purpose")
    return problems


_PROBLEMS = validate_catalogue(CATALOGUE)
if _PROBLEMS:  # pragma: no cover - a broken catalogue never loads
    raise RuntimeError(f"invalid {CATALOGUE_REF}: {_PROBLEMS}")


def policy(policy_ref: str, assigned: list[str]) -> DisclosurePolicy:
    """The catalogue policy, only if this principal's access profile assigns it."""
    found = CATALOGUE.get(policy_ref)
    if found is None or policy_ref not in assigned:
        raise DisclosureError("POLICY", POLICY_MESSAGE)
    return found


def issuable(policy_ref: str) -> DisclosurePolicy:
    found = CATALOGUE.get(policy_ref)
    if found is None or not found.issuable:
        raise DisclosureError("POLICY", f"{policy_ref} is not an issuable policy of the catalogue")
    return found


def supports(p: DisclosurePolicy, report: IntegrityReport) -> bool:
    return p.supported_reports is None or (
        (report.definition, report.selection_policy) in p.supported_reports
    )


def project(p: DisclosurePolicy, report: IntegrityReport, view_id: str) -> AggregateDisclosureView:
    """Copy the allowed aggregates of a verified report. Nothing financial is recomputed:
    the outside count is the report's own ``included - admitted``, checked against the sum
    of its non-admitted statuses."""
    if p.modality != "selected_aggregates" or p.resource_kind != "integrity_report_summary":
        raise DisclosureError("POLICY", "this policy does not project a restricted view")
    if not supports(p, report):
        raise DisclosureError("UNSUPPORTED", "the policy does not describe this report format")
    d = report.distribution
    outside = d.included - d.admitted
    if (
        d.match_count + d.break_count + d.unknown_count != d.admitted
        or d.statuses.get("admitted") != d.admitted
        or sum(v for k, v in d.statuses.items() if k != "admitted") != outside
        or outside < 0
    ):
        raise DisclosureError("INCONSISTENT", "the report's distribution does not add up")
    name, _, version = p.policy_ref.partition("@")
    view = AggregateDisclosureView(
        view_format=VIEW_FORMAT,
        view_id=view_id,
        policy=DisclosurePolicyName(policy_id=name, version=int(version)),
        resource_kind="integrity_report_summary",
        modality="selected_aggregates",
        counting_definition=COUNTING_DEFINITION,
        included_operations=d.included,
        included_definition=INCLUDED_DEFINITION,
        outcomes=DisclosedOutcomes(
            match_count=d.match_count,
            break_count=d.break_count,
            unknown_count=d.unknown_count,
            denominator=d.admitted,
            denominator_definition=ADMITTED_DEFINITION,
        ),
        outside_outcomes=DisclosedOutside(
            operations=outside,
            definition=OUTSIDE_DEFINITION,
            denominator=d.included,
            denominator_definition=INCLUDED_DEFINITION,
        ),
        withheld=list(WITHHELD),
        withheld_meaning=WITHHELD_MEANING,
        statements=list(STATEMENTS),
    )
    problems = allowlist_problems(view.model_dump(mode="json"), AGGREGATE_ALLOWLIST)
    if problems:
        raise DisclosureError("POLICY", "the projection exceeds its policy")
    return view


def allowlist_problems(document: Any, allowed: dict[str, Any], path: str = "") -> list[str]:
    """Keys of ``document`` outside ``allowed`` (recursively), or allowed keys missing."""
    if not isinstance(document, dict):
        return [f"{path or '<root>'}: not an object"]
    problems = [f"{path}{k}: not allowed" for k in document if k not in allowed]
    problems += [f"{path}{k}: missing" for k in allowed if k not in document]
    for key, nested in allowed.items():
        if nested is not None and key in document:
            problems += allowlist_problems(document[key], nested, f"{path}{key}.")
    return problems


# ------------------------------------------- the common boundary (group A)

Mode = Literal["policies", "restricted_view", "per_event_type", "administrative"]


@dataclass(frozen=True)
class Requirement:
    """What one ``QueryService`` use case requires before anything is read.

    ``policies``: every listed full policy (all of them, D-029-2). ``restricted_view``: the
    use case serves views issued under an assigned restricted policy and checks that policy
    itself. ``per_event_type``: the feed checks each type (``EVENT_TYPES``).
    ``administrative``: delivers no content of the resource (a revocation); it grants no
    other read. ``scopes``: the use case's own scope checks, listed so the requirement can be
    evaluated without calling it (navigation, links, startup warnings)."""

    scopes: tuple[str, ...]
    mode: Mode
    policies: tuple[str, ...] = ()
    reason: str = ""


_OPS = (FULL_OPERATIONS_POLICY,)


def _full(*scopes: str, policies: tuple[str, ...] = _OPS) -> Requirement:
    return Requirement(scopes=scopes, mode="policies", policies=policies)


USE_CASES: dict[str, Requirement] = {
    # Operation data: conclusions, controls, evaluations, coverage, diagnoses, obligations.
    "trace_operation": _full("operations:read"),
    "get_conclusion_as_known_at": _full("operations:read"),
    "get_missing_evidence": _full("operations:read"),
    "diagnose_control": _full("operations:read"),
    "get_operation_obligations": _full("operations:read"),
    "explain_discrepancy": _full("operations:read"),
    "explain_conclusion_change": _full("operations:read"),
    "get_coverage": _full("operations:read"),
    "list_operations": _full("operations:read"),
    "get_evaluation": _full("operations:read"),
    "get_timeline": _full("operations:read"),
    "compare_evaluations": _full("operations:read"),
    # Computed integrity reports carry their members (operations and evaluations).
    "integrity_report": _full("operations:read"),
    "integrity_cases": _full("operations:read"),
    "reproduce_integrity_report": _full("operations:read"),
    # Its own check is reports:register; the report it computes and returns goes through
    # integrity_report, which applies operations:read and this same policy again.
    "register_integrity_report": _full("reports:register"),
    # Evidence: observations and certificates (untrusted source content).
    "get_evidence": _full("evidence:read", policies=(FULL_EVIDENCE_POLICY,)),
    # Registered reports.
    "list_integrity_reports": _full("reports:read", policies=(FULL_REPORT_INDEX_POLICY,)),
    "get_integrity_report_summary": _full("reports:read", policies=(FULL_SUMMARY_POLICY,)),
    "registered_integrity_cases": _full(
        "reports:read", "operations:read", policies=(FULL_SUMMARY_POLICY, FULL_OPERATIONS_POLICY)
    ),
    # Disclosure: only what can be seen is disclosed (D-029-3).
    "issue_disclosure": _full("disclosures:issue", "reports:read", policies=(FULL_SUMMARY_POLICY,)),
    "revoke_disclosure": Requirement(
        scopes=("disclosures:revoke",),
        mode="administrative",
        reason="returns only the view identifier and the revocation time; no report content",
    ),
    "list_disclosures": Requirement(
        scopes=("disclosures:read",),
        mode="restricted_view",
        reason="lists views issued to this principal; needs an assigned restricted policy",
    ),
    "get_disclosure": Requirement(
        scopes=("disclosures:read",),
        mode="restricted_view",
        reason="checks the issuance's own policy against the assigned ones",
    ),
    # Event feed (group B): each type needs its scope and its policy; the feed's positions
    # and volume stay revealed by events:read (an accepted local decision).
    "list_events": Requirement(
        scopes=("events:read",),
        mode="per_event_type",
        reason="readable types are computed per request from scopes and policies",
    ),
}

# Event type -> (scope, full policies). Event content (subject, result, controls, links)
# is the same information as the use cases of that resource.
EVENT_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "operation.scope_reopened": ("operations:read", _OPS),
    "operation.evaluation_published": ("operations:read", _OPS),
    "integrity_report.registered": ("reports:read", (FULL_SUMMARY_POLICY,)),
}


def validate_requirements(
    use_cases: dict[str, Requirement], event_types: dict[str, tuple[str, tuple[str, ...]]]
) -> list[str]:
    """Every policy a requirement names is a full policy of the catalogue: a restricted
    projection never stands for a full output. Exemptions state their reason."""
    problems = []
    named = [(n, r.policies) for n, r in use_cases.items()] + [
        (t, ps) for t, (_, ps) in event_types.items()
    ]
    for name, policies in named:
        for ref in policies:
            found = CATALOGUE.get(ref)
            if found is None or found.modality != "full":
                problems.append(f"{name}: {ref} is not a full policy of {CATALOGUE_REF}")
    for name, req in use_cases.items():
        if (req.mode == "policies") != bool(req.policies) or not req.scopes:
            problems.append(f"{name}: a policies requirement names policies and scopes")
        if req.mode != "policies" and not req.reason:
            problems.append(f"{name}: an exemption states its reason")
    return problems


def _known_full(policy_ref: str, assigned: list[str]) -> bool:
    # validate_requirements guarantees every required policy is a full catalogue entry.
    return policy_ref in CATALOGUE and policy_ref in assigned


def require(use_case: str, assigned: list[str]) -> Requirement:
    """The requirement of ``use_case`` if this profile's policies satisfy it; else
    ``DisclosureError("POLICY")``. A use case without an entry is refused (fail closed)."""
    req = USE_CASES.get(use_case)
    if req is None:
        raise DisclosureError("POLICY", POLICY_MESSAGE)
    if req.mode == "policies" and not all(_known_full(p, assigned) for p in req.policies):
        raise DisclosureError("POLICY", POLICY_MESSAGE)
    if req.mode == "restricted_view" and not any(
        (found := CATALOGUE.get(p)) is not None and found.issuable for p in assigned
    ):
        raise DisclosureError("POLICY", POLICY_MESSAGE)
    return req


def allows(use_case: str, scopes: list[str], assigned: list[str]) -> bool:
    """Whether a profile could obtain ``use_case`` (scopes and policies); for navigation,
    links and warnings. Each use case still checks for itself when called."""
    req = USE_CASES.get(use_case)
    if req is None or not all(s in scopes for s in req.scopes):
        return False
    try:
        require(use_case, assigned)
    except DisclosureError:
        return False
    if req.mode == "per_event_type":
        return bool(readable_event_types(scopes, assigned, tuple(EVENT_TYPES)))
    return True


def readable_event_types(
    scopes: list[str], assigned: list[str], requested: tuple[str, ...]
) -> tuple[str, ...]:
    """Requested types whose scope and every policy this profile holds now. Never read from
    a cursor: a cursor only names what was readable when it was issued."""
    return tuple(
        t
        for t in requested
        if EVENT_TYPES[t][0] in scopes and all(_known_full(p, assigned) for p in EVENT_TYPES[t][1])
    )


def startup_warnings(scopes: list[str], assigned: list[str]) -> list[str]:
    """For the operator's terminal (never the client): which assigned policies this code
    does not know, and which scopes of the profile lack the policies their outputs need.
    Names only: no principal, tenant or other profile content."""
    notes = [
        f"assigned disclosure policy {p} is unknown to {CATALOGUE_REF}; it covers nothing"
        for p in assigned
        if p not in CATALOGUE
    ]
    refused: dict[tuple[str, str], int] = {}
    for req in USE_CASES.values():
        if req.mode != "policies" or not all(s in scopes for s in req.scopes):
            continue
        for p in req.policies:
            if not _known_full(p, assigned):
                key = (",".join(req.scopes), p)
                refused[key] = refused.get(key, 0) + 1
    for (scope, p), count in sorted(refused.items()):
        notes.append(f"scope {scope} without disclosure policy {p}: {count} outputs refused")
    for event_type, (scope, policies) in EVENT_TYPES.items():
        if "events:read" in scopes and scope in scopes:
            missing = [p for p in policies if not _known_full(p, assigned)]
            if missing:
                notes.append(f"event type {event_type} not delivered without {missing[0]}")
    return notes


# ------------------------------------------- field scope of the full policies (D-029-10)


def field_paths(model: type[Any]) -> set[str]:
    """Every field path a contract can deliver, nested ones included (``a.b[].c``), from its
    own JSON schema (serialization mode): names and nesting only. Descriptions, titles and
    types are left out, so documentation or representation changes do not count as new
    information; a new or renamed field does."""
    schema = model.model_json_schema(mode="serialization")
    defs: dict[str, Any] = schema.get("$defs", {})
    out: set[str] = set()

    def walk(node: Any, prefix: str, seen: frozenset[str]) -> None:
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            if name in seen:
                out.add(f"{prefix}<recursive {name}>")
                return
            walk(defs[name], prefix, seen | {name})
            return
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for sub in node.get(key, []):
                walk(sub, prefix, seen)
        if "items" in node:
            walk(node["items"], f"{prefix}[]", seen)
        extra = node.get("additionalProperties")
        if isinstance(extra, dict):
            walk(extra, f"{prefix}{{}}", seen)
        for name, sub in (node.get("properties") or {}).items():
            path = f"{prefix}.{name}"
            out.add(path)
            walk(sub, path, seen)

    walk(schema, model.__name__, frozenset())
    return out


def fields_sha256(paths: set[str]) -> str:
    return hashlib.sha256("\n".join(sorted(paths)).encode("utf-8")).hexdigest()


_REQUIREMENT_PROBLEMS = validate_requirements(USE_CASES, EVENT_TYPES)
if _REQUIREMENT_PROBLEMS:  # pragma: no cover - a broken table never loads
    raise RuntimeError(f"invalid disclosure requirements: {_REQUIREMENT_PROBLEMS}")
