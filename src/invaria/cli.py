"""Offline CLI. No network; reads only the given corpus directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from invaria.bundle import build_bundle, verify_bundle
from invaria.bundle.build import canonical_json
from invaria.bundle.signing import TrustPolicy, load_private_key, sign_bundle
from invaria.contracts.base import parse_contract
from invaria.contracts.bundle import TrustStore
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.common import provenance_problem
from invaria.engine.evaluate import Evaluation, evaluate, replay
from invaria.engine.explain import explain, explain_change, export_evaluation
from invaria.query.diagnostics import diagnose
from invaria.query.models import (
    CasesView,
    ControlDiagnosisView,
    IntegrityReport,
    OperationObligationsView,
    ReproductionView,
)
from invaria.query.obligations import project_obligations
from invaria.stellar import cli as stellar_cli
from invaria.stellar.http import ChainUnavailable
from invaria.stellar.sources import DataUnavailable, NetworkMismatch
from invaria.vertical import run_vertical
from invaria.vertical_testnet import run_testnet_vertical

if TYPE_CHECKING:
    from invaria.persistence.backup import PgTools
    from invaria.query.service import QueryService


def _evaluate(corpus: Corpus, scenario_id: str) -> Evaluation:
    if scenario_id not in corpus.scenarios:
        raise KeyError(f"unknown scenario {scenario_id!r}; known: {sorted(corpus.scenarios)}")
    return evaluate(corpus.inputs_for(scenario_id))


def mismatches(corpus: Corpus, scenario_id: str, evaluation: Evaluation) -> list[str]:
    expected = corpus.scenarios[scenario_id].expected
    problems: list[str] = []
    if evaluation.result.result != expected.result:
        problems.append(f"result {evaluation.result.result} != expected {expected.result}")
    actual = {c.control_id: c for c in evaluation.result.controls}
    for control in expected.controls:
        got = actual.get(control.control_id)
        if got is None:
            problems.append(f"{control.control_id} missing")
            continue
        for name in ("status", "reason_code", "delta", "mandatory"):
            if getattr(got, name) != getattr(control, name):
                problems.append(
                    f"{control.control_id}.{name}: {getattr(got, name)!r} != "
                    f"expected {getattr(control, name)!r}"
                )
    if evaluation.result.operation_state != expected.operation_state:
        problems.append(
            f"operation_state {evaluation.result.operation_state!r} != expected "
            f"{expected.operation_state!r}"
        )
    if len(evaluation.result.controls) != len(expected.controls):
        problems.append(
            f"{len(evaluation.result.controls)} controls != expected {len(expected.controls)}"
        )
    if list(evaluation.effective_observation_ids) != expected.effective_observation_ids:
        problems.append(
            f"effective {list(evaluation.effective_observation_ids)} != expected "
            f"{expected.effective_observation_ids}"
        )
    return problems


def cmd_check(corpus: Corpus) -> int:
    failures = 0
    for scenario_id in corpus.scenarios:
        evaluation = _evaluate(corpus, scenario_id)
        problems = mismatches(corpus, scenario_id, evaluation)
        status = "ok" if not problems else "MISMATCH"
        print(f"{scenario_id:<20} {evaluation.result.result:<8} {status}")
        for problem in problems:
            print(f"    {problem}")
        failures += bool(problems)
    print(
        f"{len(corpus.scenarios) - failures}/{len(corpus.scenarios)} scenarios reproduce "
        f"their expected result"
    )
    return 1 if failures else 0


def write_problem(error: Exception, what: str) -> str:
    """The operator's message for a safe-write problem: refused before writing,
    not written, or published without confirmation. Never a verification result."""
    from invaria.safe_write import DestinationError, Unconfirmed, WriteFailure

    if isinstance(error, DestinationError):
        return f"error: {what} not written: {error} (repeat with a new destination)"
    if isinstance(error, WriteFailure):
        extra = (
            "; a temporary of this run was left in place because it holds something it did "
            "not create"
            if error.leftover
            else ""
        )
        return f"error: {what} not written ({error.failure}); nothing under that name{extra}"
    assert isinstance(error, Unconfirmed)
    return (
        f"error: {what} may have been written complete under that name, but it was not "
        f"confirmed ({error}); check it before using it, and repeat with a new destination"
    )


def cmd_evaluate(corpus: Corpus, scenario_id: str, out: Path | None) -> int:
    from invaria.safe_write import DestinationError, NewFile, Unconfirmed, WriteFailure

    target = None if out is None else NewFile(out)
    try:
        if target is not None:
            target.prepare()  # before evaluating: a refused destination costs nothing
        evaluation = _evaluate(corpus, scenario_id)
        print("\n".join(explain(evaluation)))
        if target is not None:
            snapshot = corpus.scenarios[scenario_id].snapshot
            target.publish(export_evaluation(snapshot, evaluation).encode("utf-8"))
            print(f"exported {target.name} in the directory opened for {out} (mode 0600)")
    except (DestinationError, WriteFailure, Unconfirmed) as error:
        print(write_problem(error, "export"), file=sys.stderr)
        return 2
    finally:
        if target is not None:
            target.close()
    return 0


def cmd_diagnose(
    corpus: Corpus, scenario_id: str, control: str | None, engine: str | None, as_json: bool
) -> int:
    """Diagnose the controls of a scenario's evaluation: what the engine checked
    and where it stopped, by replaying the recorded engine on the scenario's snapshot.
    ``engine`` reproduces the evaluation a retired engine recorded instead of evaluating
    with the current one."""
    if scenario_id not in corpus.scenarios:
        raise KeyError(f"unknown scenario {scenario_id!r}; known: {sorted(corpus.scenarios)}")
    inputs = corpus.inputs_for(scenario_id)
    recorded = replay(inputs, engine) if engine else evaluate(inputs)
    evaluation = recorded.result
    ids = [c.control_id for c in evaluation.controls]
    if control is not None and control not in ids:
        raise KeyError(f"unknown control {control!r}; known: {ids}")
    replayed = replay(inputs, evaluation.versions.engine_ref)
    views = [
        diagnose(evaluation, inputs, replayed, c, stored=False) for c in ids if control in (None, c)
    ]
    if as_json:
        print(json.dumps([v.model_dump(mode="json") for v in views], indent=2))
        return 0
    for view in views:
        print("\n".join(diagnosis_lines(scenario_id, view)))
        print()
    return 0


def diagnosis_lines(label: str, view: ControlDiagnosisView) -> list[str]:
    concluded = "concluded" if view.concluded else "not concluded"
    lines = [
        f"{label} {view.control_id}: {view.status} {view.reason_code} ({concluded}); "
        f"diagnosis {view.diagnosis}, {view.engine_ref} ({view.engine_status})"
    ]
    for r in view.requirements:
        mark = "*" if r.determined_result else " "
        category = f" ({r.category})" if r.category else ""
        lines.append(f"  [{r.status}]{mark} {r.requirement_id}{category}: {r.explanation}")
        cited = [
            ("admitted", r.admitted_evidence),
            ("questioned", r.questioned_evidence),
            ("set aside", r.set_aside_evidence),
            ("coverage", r.coverage_ids),
        ]
        refs = "; ".join(f"{name} {', '.join(ids)}" for name, ids in cited if ids)
        if refs:
            lines.append(f"      {refs}")
        if r.left is not None and r.right is not None:
            lines.append(
                f"      {r.left.to_decimal_text()} vs {r.right.to_decimal_text()} {r.left.unit}"
                + (f"; delta {r.delta.to_decimal_text()}" if r.delta is not None else "")
            )
        if r.next_step is not None:
            lines.append(f"      next ({r.next_step.kind}): {r.next_step.description}")
    lines += [f"  limitation: {text}" for text in view.limitations]
    return lines


def cmd_obligations(corpus: Corpus, scenario_id: str, engine: str | None, as_json: bool) -> int:
    """Project the obligations the scenario's profile sets: the controls that
    evaluate them, as the engine concluded, and what the evidence allows to affirm. One
    replay of the recorded engine serves every control's diagnosis."""
    if scenario_id not in corpus.scenarios:
        raise KeyError(f"unknown scenario {scenario_id!r}; known: {sorted(corpus.scenarios)}")
    inputs = corpus.inputs_for(scenario_id)
    evaluation = (replay(inputs, engine) if engine else evaluate(inputs)).result
    replayed = replay(inputs, evaluation.versions.engine_ref)
    view = project_obligations(evaluation, inputs, replayed, stored=False)
    if as_json:
        print(json.dumps(view.model_dump(mode="json"), indent=2))
        return 0
    print("\n".join(obligation_lines(scenario_id, view)))
    return 0


CONDITION_CHECK_MEANING = {
    "satisfied": "condition holds: this control applies",
    "not_applicable": "condition does not hold: this control does not apply",
}


def obligation_lines(label: str, view: OperationObligationsView) -> list[str]:
    lines = [
        f"{label} {view.operation_ref}: stored result {view.result}; {view.versions.profile_ref}, "
        f"{view.versions.engine_ref} ({view.versions.engine_status}); catalogue {view.catalogue}"
        f"{f' ({view.catalogue_ref})' if view.catalogue_ref else ''}; diagnosis {view.diagnosis}",
        f"  snapshot {view.snapshot.snapshot_id}: valid_at {view.snapshot.valid_at.isoformat()}, "
        f"known_at {view.snapshot.known_at.isoformat()}, "
        f"clock {view.snapshot.evaluation_clock.isoformat()}",
    ]
    for o in view.obligations:
        a = o.applicability
        lines.append(f"  {o.obligation_id}: {o.description}")
        lines.append(f"    applicability {a.condition}: {a.state}")
        for check in a.checks:
            # The engine's check of the condition, not the obligation's applicability.
            meaning = CONDITION_CHECK_MEANING.get(check.status or "", "not determined")
            lines.append(
                f"      condition check {check.requirement_id} in {check.control_id}: "
                f"{check.status or 'no detail'} ({meaning})"
            )
        for link in o.controls:
            concluded = "concluded" if link.concluded else "not concluded"
            lines.append(
                f"    [{link.role}] {link.control_id}: {link.status} {link.reason_code} "
                f"({concluded})"
            )
        for item in o.observed:
            if item.left is not None and item.right is not None:
                delta = f"; delta {item.delta.to_decimal_text()}" if item.delta else ""
                lines.append(
                    f"    observed ({item.scope}) {item.control_id}: {item.status}, "
                    f"{item.left.to_decimal_text()} vs {item.right.to_decimal_text()} "
                    f"{item.left.unit}{delta}"
                )
        for block in o.blocks:
            category = f" ({block.category})" if block.category else ""
            lines.append(f"    block {block.control_id} {block.requirement_id}{category}")
            if block.next_step is not None:
                lines.append(f"      next ({block.next_step.kind}): {block.next_step.description}")
        if o.evidence_refs or o.coverage_ids:
            lines.append(
                f"    evidence {', '.join(o.evidence_refs) or '-'}; "
                f"coverage {', '.join(o.coverage_ids) or '-'}"
            )
        lines += [f"    basis {b.path} = {b.value}" for b in o.basis]
        lines += [f"    not derivable: {text}" for text in o.not_derivable]
        lines += [f"    limitation: {text}" for text in o.limitations]
    if not view.obligations:
        for c in view.controls:
            lines.append(f"  {c.control_id}: {c.status} {c.reason_code}")
    lines += [f"  not projected: {n.description}: {n.reason}" for n in view.not_projected]
    lines += [f"  limitation: {text}" for text in view.limitations]
    return lines


def cmd_explain_change(corpus: Corpus, before: str, after: str) -> int:
    change = explain_change(_evaluate(corpus, before), _evaluate(corpus, after))
    print("\n".join(change.lines()))
    return 0


def cmd_demo(corpus: Corpus) -> int:
    timeline, runs = run_vertical(corpus)
    print("\n".join(timeline.log))
    for run in runs:
        print()
        print(f"== {run.label} (known_at {run.snapshot.known_at.isoformat()})")
        print("\n".join(explain(run.evaluation)))
    print()
    print("\n".join(explain_change(runs[1].evaluation, runs[2].evaluation).lines()))
    return 0


def _utc(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    offset = moment.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError(f"time must be UTC with an explicit offset: {text!r}")
    return moment


def cmd_bundle(corpus: Corpus, scenario_id: str, out: Path, r2: bool) -> int:
    from invaria.safe_write import DestinationError, Unconfirmed, WriteFailure

    evaluation = _evaluate(corpus, scenario_id)
    raw_sources = (
        {f"raw/{f.name}": f.read_bytes() for f in sorted((corpus.root / "raw").iterdir())}
        if r2
        else None
    )
    try:
        manifest = build_bundle(
            out,
            corpus.inputs_for(scenario_id),
            evaluation,
            mode=corpus.scenarios[scenario_id].query_mode,
            raw_sources=raw_sources,
            mappings=corpus.mappings if r2 else None,
        )
    except (DestinationError, WriteFailure, Unconfirmed) as error:
        print(write_problem(error, "bundle"), file=sys.stderr)
        return 2
    # The manifest's own bytes: never re-read through the path, which may have changed.
    digest = hashlib.sha256(canonical_json(manifest)).hexdigest()
    print(
        f"bundle {manifest.bundle_id} -> {out.name} in the directory opened for {out} "
        "(new directory 0700, files 0600)"
    )
    print(f"result {manifest.expected_result}; manifest sha256 {digest}")
    print("unsigned: sign it, or share the manifest sha256 through a separate channel")
    return 0


def cmd_sign(bundle: Path, key_file: Path, key_id: str, signed_at: str) -> int:
    report = verify_bundle(bundle)  # never sign what does not reproduce
    if report.status != "REPRODUCED":
        print(f"error: refusing to sign: bundle is {report.status}", file=sys.stderr)
        for reason in report.reasons:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    signature = sign_bundle(
        bundle, load_private_key(key_file), key_id=key_id, signed_at=_utc(signed_at)
    )
    print(f"signed manifest {signature.manifest_sha256} with {key_id} at {signed_at}")
    return 0


def cmd_verify(
    bundle: Path,
    level: str,
    expected: str | None,
    trust_store: Path | None,
    trust_at: str | None,
    min_signatures: int,
    engine_policy: str = "any",
) -> int:
    store = (
        parse_contract(TrustStore, trust_store.read_text("utf-8"))
        if trust_store is not None
        else None
    )
    policy = TrustPolicy(at=_utc(trust_at) if trust_at else None, min_signatures=min_signatures)
    report = verify_bundle(
        bundle,
        level="R2" if level == "R2" else "R1",
        expected_manifest_sha256=expected,
        trust_store=store,
        policy=policy,
        engine_policy="current" if engine_policy == "current" else "any",
    )
    print(
        f"status {report.status} | financial result {report.financial_result} | "
        f"level {report.level} | trust {report.trust} | "
        f"engine {report.engine_status or 'not reached'}"
    )
    implementation = (
        f", {report.engine_implementation} implementation" if report.engine_implementation else ""
    )
    print(
        f"manifest sha256 {report.manifest_sha256} | engine {report.local_engine_ref} "
        f"[{report.engine_status or 'not reached'}{implementation}] "
        f"(source {report.engine_source_sha256[:16]}) | "
        f"artifacts checked {report.artifacts_checked}"
    )
    if report.signer_key_ids:
        print(f"trusted signers: {', '.join(report.signer_key_ids)}")
    for reason in report.reasons:
        print(f"  - {reason}")
    return 0 if report.status == "REPRODUCED" else 1


def cmd_demo_testnet(corpus: Path, stellar: Path, mappings: Path) -> int:
    runs = run_testnet_vertical(corpus, stellar, mappings)
    print("\n".join(runs[0].log[:3]))
    refused = next((p for r in runs if (p := provenance_problem(r.inputs))), None)
    if refused is not None:
        # A historical profile: today's adapter produces its on-chain evidence with a mapping
        # the profile does not admit, and nothing is relabelled.
        print(f"\nREFUSED: {refused}")
        print(
            "no new evaluation of this corpus can be made with today's adapter; its expected "
            "results document what was evaluated with the mapping it declares"
        )
        return 1
    failures = 0
    for run in runs:
        problems = run.mismatches()
        failures += bool(problems)
        print()
        print(f"== {run.scenario.scenario_id}: {'ok' if not problems else 'MISMATCH'}")
        for line in run.log[3:]:
            print(line)
        print("\n".join(explain(run.evaluation)))
        for problem in problems:
            print(f"    MISMATCH {problem}")
    print(f"\n{len(runs) - failures}/{len(runs)} testnet scenarios reproduce their expected result")
    return 1 if failures else 0


def _disclosure_warnings(access: Any) -> None:
    """Tell the operator (stderr, never the client) which outputs this access profile will
    have refused for lack of a disclosure policy. It grants nothing; it prints
    scope and policy names only, never the profile."""
    from invaria.query import disclosure

    for note in disclosure.startup_warnings(list(access.scopes), access.disclosure_policies):
        print(f"warning: {note}", file=sys.stderr)


def cmd_mcp_serve(access_profile: Path, audit_log: Path | None) -> int:
    """Serve the read-only consultative tools over stdio (needs the `mcp` and `db` extras)."""
    import psycopg

    from invaria.mcp_server import build_server
    from invaria.persistence.store import READER_ROLE, PgStore
    from invaria.query.models import AccessProfile
    from invaria.query.service import AuditLog, QueryService

    dsn = os.environ.get("INVARIA_DATABASE_URL")
    if not dsn:
        raise ValueError("set INVARIA_DATABASE_URL (kept outside the repository)")
    access = parse_contract(AccessProfile, access_profile.read_text("utf-8"))
    _disclosure_warnings(access)
    store = PgStore(psycopg.connect(dsn), role=READER_ROLE)
    service = QueryService(store, access, AuditLog(audit_log) if audit_log else None)
    build_server(service).run("stdio")
    return 0


def cmd_console_serve(
    access_profile: Path,
    port: int,
    audit_log: Path | None,
    events_max_examined: int = 5000,
    events_max_bytes: int = 2 * 1024 * 1024,
) -> int:
    """Serve the read-only evidence console on 127.0.0.1 (needs the `db` extra)."""
    import psycopg

    from invaria.console.server import serve
    from invaria.persistence.store import READER_ROLE, PgStore
    from invaria.query.models import AccessProfile
    from invaria.query.service import AuditLog, EventFeedLimits, QueryService

    dsn = os.environ.get("INVARIA_DATABASE_URL")
    if not dsn:
        raise ValueError("set INVARIA_DATABASE_URL (kept outside the repository)")
    access = parse_contract(AccessProfile, access_profile.read_text("utf-8"))
    _disclosure_warnings(access)
    store = PgStore(psycopg.connect(dsn), role=READER_ROLE)
    if not 100 <= events_max_examined <= 100_000:
        raise ValueError("--events-max-examined must be 100..100000")
    limits = EventFeedLimits(max_examined=events_max_examined)
    if not limits.min_max_bytes <= events_max_bytes <= limits.hard_max_bytes:
        raise ValueError(
            f"--events-max-bytes must be {limits.min_max_bytes}..{limits.hard_max_bytes}"
        )
    limits = EventFeedLimits(max_examined=events_max_examined, default_max_bytes=events_max_bytes)
    audit = AuditLog(audit_log) if audit_log else None
    serve(QueryService(store, access, audit, event_limits=limits), port)
    return 0


def _reader_service(
    access_profile: Path, *, writer: bool = False, exporter: bool = False
) -> QueryService:
    """Read-only role by default; ``writer`` (registering a report) uses the
    application role, which can only SELECT and INSERT; ``exporter`` the export
    role, which reads and only inserts into the export record."""
    import psycopg

    from invaria.persistence.store import APP_ROLE, EXPORTER_ROLE, READER_ROLE, PgStore
    from invaria.query.models import AccessProfile
    from invaria.query.service import QueryService

    dsn = os.environ.get("INVARIA_DATABASE_URL")
    if not dsn:
        raise ValueError("set INVARIA_DATABASE_URL (kept outside the repository)")
    access = parse_contract(AccessProfile, access_profile.read_text("utf-8"))
    role = APP_ROLE if writer else EXPORTER_ROLE if exporter else READER_ROLE
    return QueryService(PgStore(psycopg.connect(dsn), role=role), access)


def _metric_text(m: Any) -> str:
    if m.kind == "count":
        return f"{m.metric_id}: {m.numerator} ({m.numerator_definition})"
    value = (
        f"{m.value_percent} %"
        if m.value_percent is not None
        else f"undefined ({m.undefined_reason})"
    )
    text = (
        f"{m.metric_id}: {m.numerator}/{m.denominator} = {value} "
        f"[{m.numerator_definition} / {m.denominator_definition}]"
    )
    if m.unknown_excluded is not None:
        text += f"; UNKNOWN left out of this denominator: {m.unknown_excluded}"
    return text


def _age_text(seconds: int | None, label: str, reason: str | None = None) -> str:
    if seconds is None:
        return f"{label} undetermined" + (f" ({reason})" if reason else "")
    return (
        f"{label} {seconds // 86400} d {seconds % 86400 // 3600} h before the evidence horizon "
        "(knowledge time, not time open)"
    )


def integrity_report_lines(report: IntegrityReport) -> list[str]:
    d, b, x = report.distribution, report.blockers, report.discrepancies
    lines = [
        f"integrity report {report.report_sha256[:12]} ({report.definition}, "
        f"{report.selection_policy})",
        f"scope: tenant {report.scope.tenant_id}, population {report.scope.population}",
        f"evidence horizon {report.as_of.isoformat()}: admits snapshots with known_at up to it "
        "(knowledge time; --as-of is an alias)",
        "publication state: the present one, read at "
        + (
            report.store_read.read_at.isoformat()
            if report.store_read.read_at
            else "an unrecorded time"
        )
        + " (store clock); not reconstructed at the horizon; no economic-time filter",
        f"filters: profiles {report.profile_filter or 'all'}; exclusions: "
        + (", ".join(f"{e.reason} {e.operations}" for e in report.exclusions) or "none"),
        "",
        f"A. distribution: {d.included} included, {d.admitted} admitted "
        f"(MATCH {d.match_count}, BREAK {d.break_count}, UNKNOWN {d.unknown_count}); "
        f"technical error {d.technical_error}",
        "   statuses: " + ", ".join(f"{k} {v}" for k, v in d.statuses.items() if v),
        f"   stored evaluations {d.stored_evaluations}, not selected {d.evaluations_not_selected}",
    ]
    lines += [f"   {_metric_text(m)}" for m in d.metrics if not m.metric_id.startswith("A.status:")]
    lines.append("   segments:")
    lines += [
        f"     {s.profile_ref} / {s.engine_ref} ({s.engine_status}): {s.admitted} admitted, "
        f"MATCH {s.match_count}, BREAK {s.break_count}, UNKNOWN {s.unknown_count}"
        for s in report.segments
    ] or ["     none"]
    lines += [
        "",
        f"B. discrepancies: {x.operations_with_discrepancy} operations, "
        f"{x.control_discrepancies} control incidents",
    ]
    lines += [f"   {_metric_text(m)}" for m in x.metrics]
    for g in x.groups:
        codes = ", ".join(f"{r.reason_code} {r.incidents}" for r in g.reason_codes)
        lines.append(
            f"   {g.profile_ref} {g.control_id}: {g.operations} operations, {g.incidents} "
            f"incidents ({codes}); {_age_text(g.oldest_age_seconds, 'oldest FAIL run')}  "
            f"[cases {g.cases_key}]"
        )
    state = "complete" if b.complete else f"INCOMPLETE ({len(b.not_processed)} not processed)"
    lines += [
        "",
        f"C. blocks ({state}; {b.replayed} replays, max {b.max_replays}): "
        f"{b.operations_with_block} operations, {b.blocked_requirements} blocked requirements",
    ]
    lines += [f"   {_metric_text(m)}" for m in b.metrics]
    lines += [
        f"   {g.category}: {g.operations} operations, {g.requirements} requirements  "
        f"[cases {g.cases_key}]"
        for g in b.groups
    ]
    lines += ["", "limitations:"] + [f"  - {n}" for n in report.limitations]
    return lines


def cases_lines(view: CasesView) -> list[str]:
    lines = [
        f"cases {view.cases_key}: {view.total_operations} operations; showing "
        f"{view.offset + 1 if view.cases else 0}-{view.offset + len(view.cases)}"
    ]
    for c in view.cases:
        sel = c.selected
        where = (
            f"evaluation {sel.evaluation_id} (snapshot {sel.snapshot_id}, epoch "
            f"{sel.published_epoch}, known_at {sel.known_at.isoformat()}, {sel.engine_ref})"
            if sel
            else "no selected evaluation"
        )
        lines.append(f"{c.operation_ref} [{c.status}] {c.result or '-'}: {where}")
        lines.append(f"  criterion: {c.criterion}")
        for r in c.reasons:
            parts: list[str] = [r.kind]
            parts += [
                v
                for v in (r.control_id, r.requirement_id, r.status, r.reason_code, r.category)
                if v
            ]
            if r.age is not None:
                a = r.age
                parts.append(
                    f"FAIL run of {a.publications_in_run} publication(s) from snapshot known_at "
                    f"{a.origin_known_at.isoformat()}: "
                    + _age_text(a.age_seconds, "age", a.age_undetermined_reason)
                    + f", stopped by {a.run_stopped_by}"
                    + (
                        f" (unpublished epochs {a.unpublished_epochs_before_run})"
                        if a.unpublished_epochs_before_run
                        else ""
                    )
                )
            if r.required_from_source:
                parts.append(f"requires evidence from {r.required_from_source}")
            refs = "; ".join(
                f"{name} {', '.join(ids)}"
                for name, ids in (("evidence", r.evidence_refs), ("coverage", r.coverage_ids))
                if ids
            )
            lines.append("  - " + " ".join(parts) + (f" [{refs}]" if refs else ""))
        lines.append(
            "  follow up: "
            + "; ".join(
                f"{f.query}({', '.join(f'{k}={v}' for k, v in f.args.items())})"
                for f in c.follow_up
            )
        )
    return lines


def cmd_analytics(args: argparse.Namespace) -> int:
    """Operational integrity analytics over the store, read-only."""
    from invaria.query.service import QueryError

    register = args.analytics_command == "report" and args.register
    export = args.analytics_command == "report" and args.out is not None
    if export:
        if register:
            print(
                "error: --out cannot be combined with --register in this version: exporting "
                "and registering are separate use cases, each with its own database role and "
                "its own computation of the report; run them separately",
                file=sys.stderr,
            )
            return 2
        if args.out.endswith("/"):
            print("error: --out must name a file, not a directory", file=sys.stderr)
            return 2
        return _export_report(args)
    service = _reader_service(args.access_profile, writer=register)
    try:
        if args.analytics_command == "report":
            registration = None
            if register:
                report, registration = service.register_integrity_report(
                    args.evidence_horizon, args.profile, args.max_replays, args.max_report_bytes
                )
            else:
                report = service.integrity_report(
                    args.evidence_horizon, args.profile, args.max_replays
                )
            if args.json:
                print(json.dumps(report.model_dump(mode="json"), indent=2))
            else:
                print("\n".join(integrity_report_lines(report)))
            if registration is not None:
                state = "registered" if registration.created else "already registered"
                print(
                    f"{state}: {registration.report_id} at {registration.registered_at.isoformat()}"
                    f" (store clock; {registration.document_bytes} bytes). A stable reference "
                    "under trust in this service and store, not a signature",
                    file=sys.stderr if args.json else sys.stdout,
                )
            return 0
        report = parse_contract(IntegrityReport, args.report.read_text("utf-8"))
        if args.analytics_command == "cases":
            view = service.integrity_cases(report, args.key, args.offset, args.limit)
            print(
                json.dumps(view.model_dump(mode="json"), indent=2)
                if args.json
                else "\n".join(cases_lines(view))
            )
            return 0
        result: ReproductionView = service.reproduce_integrity_report(report)
        if args.json:
            print(json.dumps(result.model_dump(mode="json"), indent=2))
        else:
            print(
                f"{result.status}: report {result.report_sha256[:12]}, "
                f"{result.checked_evaluations} evaluations and {result.checked_publications} "
                "publications checked"
            )
            for d in result.differences:
                print(f"  - {d}")
            for n in result.limitations:
                print(f"  note: {n}")
        return 0 if result.status == "REPRODUCED" else 1
    except QueryError as error:
        print(f"error: {error.code}: {error.message}", file=sys.stderr)
        return 2


def _export_report(args: argparse.Namespace) -> int:
    """``analytics report --out``: export scope and policy, then the query's own,
    then a safe write without replacement and the export record (export role)."""
    from invaria.export_writer import ExportTarget
    from invaria.query.service import ExportError, QueryError

    service = _reader_service(args.access_profile, exporter=True)
    target = ExportTarget(Path(args.out))
    try:
        report, view = service.export_integrity_report_file(
            args.evidence_horizon, args.profile, args.max_replays, target
        )
    except ExportError as error:
        print(f"error: {error.code}: {error.message}", file=sys.stderr)
        if error.code == "EXPORT_UNCONFIRMED":
            return 1
        return 130 if error.failure == "interrupted" else 2
    except QueryError as error:
        print(f"error: {error.code}: {error.message}", file=sys.stderr)
        print("nothing was written and no export was recorded", file=sys.stderr)
        return 2
    # The report itself, as before (a query output); the export goes to stderr with
    # --json so that stdout stays the report contract.
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2))
    else:
        print("\n".join(integrity_report_lines(report)))
    out = sys.stderr if args.json else sys.stdout
    print(
        f"\nexport {view.export_id} completed: {Path(args.out).name} written in the directory "
        f"opened for {args.out} (the path is not re-resolved afterwards; {view.content_bytes} "
        "bytes, "
        f"content sha256 {view.content_sha256}, mode 0600) under "
        f"{', '.join(view.policy_refs)}; recorded at {view.completed_at.isoformat()} "
        "(store clock)",
        file=out,
    )
    print(
        f"report_sha256 {view.report_sha256} checks internal consistency only: it does not "
        "authenticate the report",
        file=out,
    )
    for note in view.limitations:
        print(f"  note: {note}", file=out)
    return 0


def cmd_disclosure(args: argparse.Namespace) -> int:
    """Issue or revoke a disclosed view of a registered report. Application role:
    it writes one issuance or revocation row, never a domain event."""
    from invaria.query.service import QueryError

    service = _reader_service(args.access_profile, writer=True)
    try:
        if args.disclosure_command == "issue":
            issued = service.issue_disclosure(args.report, args.recipient, args.policy)
            if args.json:
                print(json.dumps(issued.model_dump(mode="json"), indent=2))
                return 0
            print(
                f"issued {issued.view_id} to {issued.recipient_principal} under "
                f"{issued.policy_ref} for {issued.report_id} at {issued.issued_at.isoformat()} "
                "(store clock)"
            )
            print("the recipient will read exactly:")
            print(json.dumps(issued.preview.model_dump(mode="json"), indent=2))
            for note in issued.limitations:
                print(f"  note: {note}")
            return 0
        revoked = service.revoke_disclosure(args.view)
        if args.json:
            print(json.dumps(revoked.model_dump(mode="json"), indent=2))
            return 0
        state = "revoked" if revoked.revoked_now else "already revoked"
        print(f"{state}: {revoked.view_id} at {revoked.revoked_at.isoformat()} (store clock)")
        for note in revoked.limitations:
            print(f"  note: {note}")
        return 0
    except QueryError as error:
        print(f"error: {error.code}: {error.message}", file=sys.stderr)
        return 2


def cmd_events(args: argparse.Namespace) -> int:
    """Derive domain events from the outbox: local, one-shot, resumable."""
    import psycopg

    from invaria.domain_events import Limits
    from invaria.persistence.events import derive
    from invaria.persistence.store import APP_ROLE, PgStore

    dsn = os.environ.get("INVARIA_DATABASE_URL")
    if not dsn:
        raise ValueError("set INVARIA_DATABASE_URL (kept outside the repository)")
    limits = Limits(
        max_replays_per_run=args.max_replays,
        max_unknown_controls=args.max_unknown_controls,
        statement_timeout_ms=args.statement_timeout_ms,
        lock_timeout_ms=args.lock_timeout_ms,
    )
    with psycopg.connect(dsn) as conn:
        result = derive(PgStore(conn, role=APP_ROLE), limit=args.limit, limits=limits)
    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        print(
            f"derived {result.derived}, not public {result.not_public}, already derived "
            f"{result.already_derived}, errors {len(result.errors)}, pending {result.pending} "
            f"(replays {result.replays}, per-event limits reached {result.limited})"
        )
        for error in result.errors:
            print(f"  error: {error}")
        if result.paused:
            print("paused: the replay budget of this run is spent")
        if result.pending:
            print("run again to continue; derived sources are never derived twice")
    return 1 if result.errors else 0


def _admin_dsn() -> str:
    dsn = os.environ.get("INVARIA_ADMIN_DATABASE_URL")
    if not dsn:
        raise ValueError("set INVARIA_ADMIN_DATABASE_URL (admin URL, kept outside the repository)")
    return dsn


def _container_image(container: str) -> str:
    import subprocess

    out = subprocess.run(
        ["docker", "inspect", "--format", "{{.Config.Image}}", container],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def cmd_db(args: argparse.Namespace) -> int:
    """Logical backup / restore / verification through pg tools in the pinned container."""
    from invaria.persistence.backup import BackupError, PgTools

    tools = PgTools(("docker", "exec", "-i", args.container))
    admin = _admin_dsn()
    try:
        return _db(args, tools, admin)
    except BackupError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _db(args: argparse.Namespace, tools: PgTools, admin: str) -> int:
    import time
    from datetime import UTC, datetime

    from invaria.persistence.backup import (
        create_backup,
        read_manifest,
        restore_backup,
        verify_restore,
        write_manifest,
    )

    if args.db_command == "backup":
        started = time.monotonic()
        manifest, dump = create_backup(
            admin,
            args.database,
            tools,
            args.out,
            backup_id=args.backup_id,
            container_image=_container_image(args.container),
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        digest = write_manifest(manifest, args.manifest)
        elapsed = time.monotonic() - started
        rows = sum(int(t["rows"]) for t in manifest.tables.values())
        print(f"dump {dump} ({manifest.dump_bytes} bytes, sha256 {manifest.dump_sha256})")
        print(f"manifest {args.manifest} sha256 {digest}: keep it apart from the dump")
        print(f"cut: exported snapshot; {len(manifest.tables)} tables, {rows} rows; {elapsed:.2f}s")
        return 0
    manifest = read_manifest(args.manifest)
    if args.db_command == "restore":
        started = time.monotonic()
        created = restore_backup(admin, args.dump, manifest, args.target, tools)
        print(f"restored into new database {args.target}; roles rebuilt: {created or 'none'}")
        print(f"restore took {time.monotonic() - started:.2f}s")
    started = time.monotonic()
    report = verify_restore(admin, args.target, manifest, args.dump)
    print(
        f"{report.status}: {report.reproduced}/{report.evaluations} evaluations reproduced "
        f"with their exact engine; verification took {time.monotonic() - started:.2f}s"
    )
    for reason in report.reasons:
        print(f"  - {reason}")
    return 0 if report.status == "RESTORE_VERIFIED" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invaria", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="evaluate every scenario and compare with expected")
    check.add_argument("corpus", type=Path)
    run = sub.add_parser("evaluate", help="evaluate one scenario and explain it")
    run.add_argument("corpus", type=Path)
    run.add_argument("scenario")
    run.add_argument(
        "--out",
        type=Path,
        help="write canonical JSON export (unsigned) to a new file; never overwritten",
    )
    diag = sub.add_parser(
        "diagnose", help="what the engine checked per control of a scenario, and where it stopped"
    )
    diag.add_argument("corpus", type=Path)
    diag.add_argument("scenario")
    diag.add_argument("--control", help="one control id (default: every control)")
    diag.add_argument(
        "--engine", help="reproduce the evaluation of this engine label (current or retired)"
    )
    diag.add_argument("--json", action="store_true", help="print the diagnosis contract as JSON")
    obligations = sub.add_parser(
        "obligations",
        help="the obligations a scenario's profile sets, their controls and blocks",
    )
    obligations.add_argument("corpus", type=Path)
    obligations.add_argument("scenario")
    obligations.add_argument(
        "--engine", help="reproduce the evaluation of this engine label (current or retired)"
    )
    obligations.add_argument("--json", action="store_true", help="print the view as JSON")
    change = sub.add_parser("explain-change", help="compare two scenario evaluations")
    change.add_argument("corpus", type=Path)
    change.add_argument("before")
    change.add_argument("after")
    demo = sub.add_parser("demo", help="import raw CSVs, build K1/K2/K3 snapshots, evaluate")
    demo.add_argument("corpus", type=Path)
    bundle = sub.add_parser("bundle", help="write a directory evidence bundle for a scenario")
    bundle.add_argument("corpus", type=Path)
    bundle.add_argument("scenario")
    bundle.add_argument(
        "out",
        type=Path,
        help="new directory (must not exist, not even empty; never overwritten)",
    )
    bundle.add_argument("--r2", action="store_true", help="include raw CSV bytes and mappings")
    sign = sub.add_parser("sign", help="add a detached Ed25519 signature to a bundle")
    sign.add_argument("bundle", type=Path)
    sign.add_argument("--key-file", type=Path, required=True, help="PEM PKCS#8, mode 600")
    sign.add_argument("--key-id", required=True)
    sign.add_argument("--signed-at", required=True, help="explicit UTC time, e.g. ...Z")
    verify = sub.add_parser("verify", help="verify a bundle offline (R1/R2 replay)")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--level", choices=["R1", "R2"], default="R1")
    verify.add_argument("--expect-manifest-sha256", dest="expected")
    verify.add_argument("--trust-store", type=Path, help="TrustStore JSON chosen by the verifier")
    verify.add_argument("--trust-at", help="judge keys at this UTC time instead of signed_at")
    verify.add_argument("--min-signatures", type=int, default=1)
    verify.add_argument(
        "--engine-policy",
        choices=["any", "current"],
        default="any",
        help="current: refuse to reproduce conclusions of retired engines",
    )
    mcp = sub.add_parser("mcp", help="read-only consultative MCP server")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    serve = mcp_sub.add_parser("serve", help="serve over stdio; DSN from INVARIA_DATABASE_URL")
    serve.add_argument("--access-profile", type=Path, required=True)
    serve.add_argument("--audit-log", type=Path, help="append-only JSONL access log")
    console = sub.add_parser("console", help="read-only local evidence console (REST + HTML)")
    console_sub = console.add_subparsers(dest="console_command", required=True)
    cserve = console_sub.add_parser(
        "serve", help="serve on 127.0.0.1; DSN from INVARIA_DATABASE_URL"
    )
    cserve.add_argument("--access-profile", type=Path, required=True)
    cserve.add_argument("--port", type=int, default=8765, help="0 picks a free port")
    cserve.add_argument("--audit-log", type=Path, help="append-only JSONL access log")
    cserve.add_argument(
        "--events-max-examined",
        type=int,
        default=5000,
        help="feed rows examined per /v1/events request (100..100000)",
    )
    cserve.add_argument(
        "--events-max-bytes",
        type=int,
        default=2 * 1024 * 1024,
        help="default response budget of /v1/events items (4 KiB..4 MiB)",
    )
    an = sub.add_parser(
        "analytics",
        help="operational integrity analytics (read-only, except report --register; "
        "INVARIA_DATABASE_URL)",
    )
    an_sub = an.add_subparsers(dest="analytics_command", required=True)
    rep = an_sub.add_parser("report", help="distribution, discrepancies and blocks with references")
    rep.add_argument(
        "--evidence-horizon",
        "--as-of",
        dest="evidence_horizon",
        required=True,
        help="evidence horizon applied to snapshot known_at, explicit UTC time (...Z); "
        "--as-of is an alias. Publication state is always the present one",
    )
    rep.add_argument("--profile", action="append", help="profile_ref filter (repeatable)")
    rep.add_argument("--max-replays", type=int, default=100, help="work limit for the blocks")
    rep.add_argument(
        "--out",
        help="export the report JSON to a new file (scope exports:create and policy "
        "export-integrity-report-file@1 on top of the query's; export role). Never "
        "overwrites, never follows a symbolic link; recorded without the path. Not "
        "combinable with --register in this version",
    )
    rep.add_argument(
        "--json",
        action="store_true",
        help="print the report contract as JSON (a query output: redirecting it saves a "
        "copy outside the export record)",
    )
    rep.add_argument(
        "--register",
        action="store_true",
        help="store the computed report in the registry (scope reports:register; application "
        "role). Never takes a file or a hash. Not combinable with --out in this version",
    )
    rep.add_argument(
        "--max-report-bytes",
        type=int,
        default=4 * 1024 * 1024,
        help="registration limit for the document size (default 4 MiB, at most 64 MiB)",
    )
    cas = an_sub.add_parser("cases", help="the operations behind a figure of a saved report")
    cas.add_argument("--report", type=Path, required=True)
    cas.add_argument("--key", required=True, help="cases_key of a figure, e.g. result:UNKNOWN")
    cas.add_argument("--offset", type=int, default=0)
    cas.add_argument("--limit", type=int)
    cas.add_argument("--json", action="store_true")
    rp = an_sub.add_parser("reproduce", help="recompute a saved report from its references")
    rp.add_argument("--report", type=Path, required=True)
    rp.add_argument("--json", action="store_true")
    for cmd in (rep, cas, rp):
        cmd.add_argument("--access-profile", type=Path, required=True)
    ds = sub.add_parser(
        "disclosure",
        help="issue or revoke a disclosed view of a registered report ("
        "INVARIA_DATABASE_URL, application role)",
    )
    ds_sub = ds.add_subparsers(dest="disclosure_command", required=True)
    di = ds_sub.add_parser(
        "issue", help="bind a registered report to a recipient principal under a policy"
    )
    di.add_argument("--report", required=True, help="ir-<sha256> of a registered report")
    di.add_argument("--recipient", required=True, help="principal_id of the recipient")
    di.add_argument(
        "--policy", required=True, help="issuable policy, e.g. aggregate-outcome-counts@1"
    )
    dr = ds_sub.add_parser("revoke", help="revoke an issued view (later reads give 404)")
    dr.add_argument("--view", required=True, help="dv-<32 hex>")
    for cmd in (di, dr):
        cmd.add_argument("--access-profile", type=Path, required=True)
        cmd.add_argument("--json", action="store_true")
    ev = sub.add_parser("events", help="domain events; INVARIA_DATABASE_URL")
    ev_sub = ev.add_subparsers(dest="events_command", required=True)
    dv = ev_sub.add_parser(
        "derive", help="derive pending outbox rows into domain events (resumable, local only)"
    )
    dv.add_argument("--limit", type=int, default=100, help="outbox rows per run (1..10000)")
    dv.add_argument("--max-replays", type=int, default=200, help="diagnostic replays per run")
    dv.add_argument(
        "--max-unknown-controls",
        type=int,
        default=50,
        help="above this many UNKNOWN controls, blocks are not compared",
    )
    dv.add_argument("--statement-timeout-ms", type=int, default=5000, help="per diagnostic load")
    dv.add_argument(
        "--lock-timeout-ms",
        type=int,
        default=1000,
        help="lock wait per diagnostic load; exceeding it is transient (retried), 0 disables",
    )
    dv.add_argument("--json", action="store_true")
    db = sub.add_parser("db", help="logical backup, restore and restore verification")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    backup = db_sub.add_parser("backup", help="pg_dump from one exported snapshot")
    backup.add_argument("--database", required=True)
    backup.add_argument("--out", type=Path, required=True, help="new directory for the dump")
    backup.add_argument("--manifest", type=Path, required=True, help="kept apart from the dump")
    backup.add_argument("--backup-id", required=True)
    for name in ("restore", "verify-restore"):
        cmd = db_sub.add_parser(name, help=f"{name} into/against a dedicated database")
        cmd.add_argument("--dump", type=Path, required=True)
        cmd.add_argument("--manifest", type=Path, required=True)
        cmd.add_argument("--target", required=True, help="new database (never overwritten)")
    for cmd in (backup, *(db_sub.choices[n] for n in ("restore", "verify-restore"))):
        cmd.add_argument("--container", default="invaria-pg-dev")
    stellar_cli.add_parser(sub)
    testnet = sub.add_parser(
        "demo-testnet", help="evaluate SUB-0001 with real testnet evidence (replayed offline)"
    )
    testnet.add_argument(
        "corpus", type=Path, help="tests/fixtures/corpus/subscription-testnet-1.6.0"
    )
    testnet.add_argument("--stellar", type=Path, default=Path("tests/fixtures/stellar"))
    testnet.add_argument(
        "--mappings",
        type=Path,
        default=Path("tests/fixtures/corpus/subscription-synthetic/mappings"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "stellar":
            return stellar_cli.run(args)
        if args.command == "demo-testnet":
            return cmd_demo_testnet(args.corpus, args.stellar, args.mappings)
        if args.command == "verify":
            return cmd_verify(
                args.bundle,
                args.level,
                args.expected,
                args.trust_store,
                args.trust_at,
                args.min_signatures,
                args.engine_policy,
            )
        if args.command == "mcp":
            return cmd_mcp_serve(args.access_profile, args.audit_log)
        if args.command == "db":
            return cmd_db(args)
        if args.command == "analytics":
            return cmd_analytics(args)
        if args.command == "events":
            return cmd_events(args)
        if args.command == "disclosure":
            return cmd_disclosure(args)
        if args.command == "console":
            return cmd_console_serve(
                args.access_profile,
                args.port,
                args.audit_log,
                args.events_max_examined,
                args.events_max_bytes,
            )
        if args.command == "sign":
            return cmd_sign(args.bundle, args.key_file, args.key_id, args.signed_at)
        corpus = load_corpus(args.corpus)
        if args.command == "check":
            return cmd_check(corpus)
        if args.command == "evaluate":
            return cmd_evaluate(corpus, args.scenario, args.out)
        if args.command == "diagnose":
            return cmd_diagnose(corpus, args.scenario, args.control, args.engine, args.json)
        if args.command == "obligations":
            return cmd_obligations(corpus, args.scenario, args.engine, args.json)
        if args.command == "explain-change":
            return cmd_explain_change(corpus, args.before, args.after)
        if args.command == "bundle":
            return cmd_bundle(corpus, args.scenario, args.out, args.r2)
        return cmd_demo(corpus)
    except (ChainUnavailable, DataUnavailable, NetworkMismatch) as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 3
    except (KeyError, FileNotFoundError, FileExistsError, PermissionError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
