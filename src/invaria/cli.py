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
from typing import TYPE_CHECKING

from invaria.bundle import build_bundle, verify_bundle
from invaria.bundle.signing import TrustPolicy, load_private_key, sign_bundle
from invaria.contracts.base import parse_contract
from invaria.contracts.bundle import TrustStore
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.common import provenance_problem
from invaria.engine.evaluate import Evaluation, evaluate, replay
from invaria.engine.explain import explain, explain_change, export_evaluation
from invaria.query.diagnostics import diagnose
from invaria.query.models import ControlDiagnosisView, OperationObligationsView
from invaria.query.obligations import project_obligations
from invaria.stellar import cli as stellar_cli
from invaria.stellar.http import ChainUnavailable
from invaria.stellar.sources import DataUnavailable, NetworkMismatch
from invaria.vertical import run_vertical
from invaria.vertical_testnet import run_testnet_vertical

if TYPE_CHECKING:
    from invaria.persistence.backup import PgTools


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


def cmd_evaluate(corpus: Corpus, scenario_id: str, out: Path | None) -> int:
    evaluation = _evaluate(corpus, scenario_id)
    print("\n".join(explain(evaluation)))
    if out is not None:
        snapshot = corpus.scenarios[scenario_id].snapshot
        out.write_text(export_evaluation(snapshot, evaluation), encoding="utf-8")
        print(f"exported {out}")
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
    evaluation = _evaluate(corpus, scenario_id)
    raw_sources = (
        {f"raw/{f.name}": f.read_bytes() for f in sorted((corpus.root / "raw").iterdir())}
        if r2
        else None
    )
    manifest = build_bundle(
        out,
        corpus.inputs_for(scenario_id),
        evaluation,
        mode=corpus.scenarios[scenario_id].query_mode,
        raw_sources=raw_sources,
        mappings=corpus.mappings if r2 else None,
    )
    digest = hashlib.sha256((out / "manifest.json").read_bytes()).hexdigest()
    print(f"bundle {manifest.bundle_id} -> {out}")
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
    store = PgStore(psycopg.connect(dsn), role=READER_ROLE)
    service = QueryService(store, access, AuditLog(audit_log) if audit_log else None)
    build_server(service).run("stdio")
    return 0


def cmd_console_serve(access_profile: Path, port: int, audit_log: Path | None) -> int:
    """Serve the read-only evidence console on 127.0.0.1 (needs the `db` extra)."""
    import psycopg

    from invaria.console.server import serve
    from invaria.persistence.store import READER_ROLE, PgStore
    from invaria.query.models import AccessProfile
    from invaria.query.service import AuditLog, QueryService

    dsn = os.environ.get("INVARIA_DATABASE_URL")
    if not dsn:
        raise ValueError("set INVARIA_DATABASE_URL (kept outside the repository)")
    access = parse_contract(AccessProfile, access_profile.read_text("utf-8"))
    store = PgStore(psycopg.connect(dsn), role=READER_ROLE)
    serve(QueryService(store, access, AuditLog(audit_log) if audit_log else None), port)
    return 0


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
    run.add_argument("--out", type=Path, help="write canonical JSON export (unsigned)")
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
    bundle.add_argument("out", type=Path)
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
        if args.command == "console":
            return cmd_console_serve(args.access_profile, args.port, args.audit_log)
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
