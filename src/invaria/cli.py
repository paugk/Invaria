"""Offline CLI. No network; reads only the given corpus directory."""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Sequence
from pathlib import Path

from invaria.bundle import build_bundle, verify_bundle
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


def cmd_bundle(corpus: Corpus, scenario_id: str, out: Path) -> int:
    evaluation = _evaluate(corpus, scenario_id)
    manifest = build_bundle(
        out,
        corpus.inputs_for(scenario_id),
        evaluation,
        mode=corpus.scenarios[scenario_id].query_mode,
    )
    digest = hashlib.sha256((out / "manifest.json").read_bytes()).hexdigest()
    print(f"bundle {manifest.bundle_id} -> {out}")
    print(f"result {manifest.expected_result}; manifest sha256 {digest}")
    print("unsigned: share the manifest sha256 through a separate channel to anchor trust")
    return 0


def cmd_verify(bundle: Path, level: str, expected: str | None) -> int:
    report = verify_bundle(
        bundle, level="R2" if level == "R2" else "R1", expected_manifest_sha256=expected
    )
    print(
        f"status {report.status} | financial result {report.financial_result} | "
        f"level {report.level} | trust {report.trust}"
    )
    print(
        f"manifest sha256 {report.manifest_sha256} | engine {report.local_engine_ref} | "
        f"artifacts checked {report.artifacts_checked}"
    )
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
    verify = sub.add_parser("verify", help="verify a bundle offline (R1 replay)")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--level", choices=["R1", "R2"], default="R1")
    verify.add_argument("--expect-manifest-sha256", dest="expected")
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
            return cmd_verify(args.bundle, args.level, args.expected)
        corpus = load_corpus(args.corpus)
        if args.command == "check":
            return cmd_check(corpus)
        if args.command == "evaluate":
            return cmd_evaluate(corpus, args.scenario, args.out)
        if args.command == "explain-change":
            return cmd_explain_change(corpus, args.before, args.after)
        if args.command == "bundle":
            return cmd_bundle(corpus, args.scenario, args.out)
        return cmd_demo(corpus)
    except (ChainUnavailable, DataUnavailable, NetworkMismatch) as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 3
    except (KeyError, FileNotFoundError, FileExistsError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
