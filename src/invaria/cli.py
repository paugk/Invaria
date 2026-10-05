"""Offline CLI. No network; reads only the given corpus directory."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from invaria.bundle import build_bundle, verify_bundle
from invaria.bundle.signing import TrustPolicy, load_private_key, sign_bundle
from invaria.contracts.base import parse_contract
from invaria.contracts.bundle import TrustStore
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.evaluate import Evaluation, evaluate
from invaria.engine.explain import explain, explain_change, export_evaluation
from invaria.stellar import cli as stellar_cli
from invaria.stellar.http import ChainUnavailable
from invaria.stellar.sources import DataUnavailable, NetworkMismatch
from invaria.vertical import run_vertical
from invaria.vertical_testnet import run_testnet_vertical


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
    )
    print(
        f"status {report.status} | financial result {report.financial_result} | "
        f"level {report.level} | trust {report.trust}"
    )
    print(
        f"manifest sha256 {report.manifest_sha256} | engine {report.local_engine_ref} "
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invaria", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="evaluate every scenario and compare with expected")
    check.add_argument("corpus", type=Path)
    run = sub.add_parser("evaluate", help="evaluate one scenario and explain it")
    run.add_argument("corpus", type=Path)
    run.add_argument("scenario")
    run.add_argument("--out", type=Path, help="write canonical JSON export (unsigned)")
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
    mcp = sub.add_parser("mcp", help="read-only consultative MCP server")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    serve = mcp_sub.add_parser("serve", help="serve over stdio; DSN from INVARIA_DATABASE_URL")
    serve.add_argument("--access-profile", type=Path, required=True)
    serve.add_argument("--audit-log", type=Path, help="append-only JSONL access log")
    stellar_cli.add_parser(sub)
    testnet = sub.add_parser(
        "demo-testnet", help="evaluate SUB-0001 with real testnet evidence (replayed offline)"
    )
    testnet.add_argument("corpus", type=Path, help="tests/fixtures/corpus/subscription-testnet")
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
            )
        if args.command == "mcp":
            return cmd_mcp_serve(args.access_profile, args.audit_log)
        if args.command == "sign":
            return cmd_sign(args.bundle, args.key_file, args.key_id, args.signed_at)
        corpus = load_corpus(args.corpus)
        if args.command == "check":
            return cmd_check(corpus)
        if args.command == "evaluate":
            return cmd_evaluate(corpus, args.scenario, args.out)
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
