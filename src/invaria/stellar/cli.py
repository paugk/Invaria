"""`invaria stellar ...`: read-only testnet access. Never signs, funds or submits."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from invaria.contracts.base import parse_contract
from invaria.contracts.chain import ChainTarget, ExecutionLinkSet
from invaria.contracts.corpus import IdentityLinkSet
from invaria.contracts.observation import TokenMovementPayload
from invaria.stellar.adapter import (
    RunResult,
    ingest_horizon_payments,
    ingest_sac_events,
    resolve_sac_contract,
)
from invaria.stellar.http import HttpClient, RecordingClient, ReplayClient, UrllibClient
from invaria.stellar.ledger_completeness import (
    compare,
    enumerate_payments,
    read_capture,
)
from invaria.stellar.ledger_completeness import (
    completeness_section as completeness_report,
)
from invaria.stellar.ledger_proof import (
    Anchor,
    StellarCli,
    anchor_from_run,
    bounded_get,
    check_event,
    check_observation,
    checkpoint_of,
    fetch_checkpoint,
    manual_anchor,
    observe_anchors,
    overall_result,
    replay_report,
    report,
    verify_checkpoint,
    verify_replay,
)
from invaria.stellar.sources import Endpoints, Horizon, Rpc, verify_network
from invaria.stellar.store import IngestStore
from invaria.stellar.trustline_reconciliation import (
    approved_addresses,
    reconcile,
    select_ledgers,
)

TESTNET_ARCHIVE = "https://history.stellar.org/prd/core-testnet/core_testnet_001"


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    stellar = sub.add_parser("stellar", help="read-only Stellar testnet adapter")
    actions = stellar.add_subparsers(dest="stellar_command", required=True)
    for name in ("check-network", "ingest"):
        p = actions.add_parser(name)
        p.add_argument(
            "--network", default="stellar:testnet", choices=["stellar:testnet", "stellar:pubnet"]
        )
        p.add_argument("--horizon", help="Horizon URL (else $INVARIA_STELLAR_HORIZON_URL)")
        p.add_argument("--rpc", help="RPC URL (else $INVARIA_STELLAR_RPC_URL)")
        p.add_argument("--record", type=Path, help="store every HTTP exchange in this directory")
        p.add_argument("--replay", type=Path, help="serve HTTP from recorded exchanges (offline)")
        if name == "ingest":
            p.add_argument("--target", type=Path, required=True)
            p.add_argument("--start-ledger", type=int, required=True)
            p.add_argument("--end-ledger", type=int, required=True)
            p.add_argument("--store", type=Path, required=True)
            p.add_argument("--links", type=Path, help="ExecutionLinkSet JSON (explicit links)")
            p.add_argument("--sac", action="store_true", help="also ingest SAC events via RPC")
            p.add_argument("--page-limit", type=int, default=200, help="Horizon page size")
            p.add_argument("--sac-page-limit", type=int, default=200, help="RPC page size")
            p.add_argument("--max-pages", type=int, default=50)
            p.add_argument("--recorded-at", help="ISO UTC time to stamp (default: now)")
    fetch = actions.add_parser(
        "ledger-fetch", help="download one history archive checkpoint (bounded; bytes kept)"
    )
    fetch.add_argument("--archive", default=TESTNET_ARCHIVE, help="history archive base URL")
    fetch.add_argument("--ledger", type=int, required=True, help="a ledger of the checkpoint")
    fetch.add_argument("--out", type=Path, required=True, help="new directory for the files")
    fetch.add_argument("--timeout", type=int, default=60, help="seconds per file")
    fetch.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024, help="bytes per file")
    observe = actions.add_parser(
        "ledger-anchor",
        help="run stellar-core verify-checkpoints (consensus observed, headers only) and record it",
    )
    observe.add_argument("--config", type=Path, required=True, help="stellar-core config")
    observe.add_argument("--from-ledger", type=int, required=True)
    observe.add_argument("--out", type=Path, required=True, help="new directory for the run")
    observe.add_argument("--timeout", type=int, default=900, help="seconds for the run")
    verify = actions.add_parser(
        "ledger-verify",
        help="anchored inclusion of Classic payments in a checkpoint (no completeness)",
    )
    verify.add_argument("--archive-dir", type=Path, required=True)
    verify.add_argument("--ledger", type=int, required=True, help="a ledger of the checkpoint")
    verify.add_argument(
        "--network", default="stellar:testnet", choices=["stellar:testnet", "stellar:pubnet"]
    )
    anchor = verify.add_mutually_exclusive_group(required=True)
    anchor.add_argument(
        "--anchor-run",
        type=Path,
        help="anchor-run.json of a recorded verify-checkpoints run (imported with provenance)",
    )
    anchor.add_argument(
        "--observe-anchor",
        type=Path,
        metavar="CONFIG",
        help="run stellar-core verify-checkpoints now with this config (observed run)",
    )
    anchor.add_argument("--anchor-hash", help="manually supplied hash of the checkpoint ledger")
    verify.add_argument("--anchor-note", help="free note for a manual anchor (reported as such)")
    verify.add_argument("--anchor-dir", type=Path, help="new directory for --observe-anchor")
    verify.add_argument("--target", type=Path, required=True, help="ChainTarget JSON")
    verify.add_argument("--store", type=Path, required=True, help="ingest store to contrast")
    verify.add_argument("--out", type=Path, help="write the JSON report here")
    verify.add_argument(
        "--replay-dir",
        type=Path,
        help="kept artifacts of a catchup replay of this checkpoint (replay-run.json and files)",
    )
    verify.add_argument(
        "--expect-replay-record-sha256",
        help="sha256 of replay-run.json obtained through a channel you trust; it shows "
        "correspondence with that record, not that the run happened; without it the "
        "artifacts are only checked against their own record (supplier_declared)",
    )
    verify.add_argument(
        "--completeness-ledgers",
        nargs=2,
        type=int,
        metavar=("START", "END"),
        help="report completeness of the target's Classic payments in these ledgers (both "
        "included), against --horizon-recording",
    )
    verify.add_argument(
        "--horizon-recording",
        type=Path,
        help="recorded Horizon exchanges of the account's payments (the provider's capture)",
    )
    verify.add_argument(
        "--expect-replay-revision-sha256",
        help="sha256 of replay-run.revision-1.json obtained through a channel you trust",
    )
    verify.add_argument(
        "--trustline-reconciliation",
        nargs=2,
        metavar=("START", "END"),
        help="reconcile the target asset's trustline of the approved accounts between these "
        "UTC times (ledgers by verified close_time), from --replay-dir; a report only",
    )
    verify.add_argument(
        "--interval-bounds",
        choices=["half_open", "closed"],
        default="half_open",
        help="half_open: START <= close_time < END (default, the control's semantics); "
        "closed: END included",
    )
    verify.add_argument(
        "--approved-links", type=Path, help="identity links file naming the approved addresses"
    )
    verify.add_argument("--account-ref", help="the institutional account whose links apply")


def _client(args: argparse.Namespace) -> HttpClient:
    if args.replay is not None:
        return ReplayClient(args.replay)
    live: HttpClient = UrllibClient()
    return RecordingClient(live, args.record) if args.record is not None else live


def _print_run(result: RunResult) -> None:
    print(
        f"[{result.path}] complete={result.complete} pages={result.pages} "
        f"appended={len(result.appended)} duplicates={result.duplicates} "
        f"conflicts={len(result.conflicts)} exclusions={len(result.exclusions)} "
        f"({result.stop_reason})"
    )
    for observation in result.appended:
        payload = observation.payload
        chain = getattr(payload, "chain", None)
        units = getattr(payload, "units", None)
        if chain is not None and units is not None:
            print(
                f"  + {observation.source.record_key} ledger {chain.ledger} "
                f"{units.to_decimal_text()} {units.unit} ok={chain.tx_successful} "
                f"op_ref={observation.operation_ref}"
            )
    for exclusion in result.exclusions:
        print(f"  - excluded {exclusion.reason}: {exclusion.detail}")
    if result.coverage is not None:
        c = result.coverage
        print(
            f"  coverage {c.coverage_id} ledgers {c.ledger_range} received "
            f"{c.records_received} quarantined {c.records_quarantined} level {c.level}"
        )


def run(args: argparse.Namespace) -> int:
    if args.stellar_command == "ledger-fetch":
        return _ledger_fetch(args)
    if args.stellar_command == "ledger-anchor":
        return _ledger_anchor(args)
    if args.stellar_command == "ledger-verify":
        return _ledger_verify(args)
    endpoints = Endpoints.resolve(args.horizon, args.rpc)
    client = _client(args)
    horizon, rpc = Horizon(endpoints.horizon_url, client), Rpc(endpoints.rpc_url, client)
    check = verify_network(horizon, rpc, args.network)
    print(
        f"network {check.network} verified: passphrase {check.passphrase!r}, protocol "
        f"{check.protocol_version}, Horizon history {check.horizon_elder_ledger}-"
        f"{check.horizon_latest_ledger}"
    )
    for line in check.evidence:
        print(f"  evidence {line}")
    if args.stellar_command == "check-network":
        return 0
    target = parse_contract(ChainTarget, args.target.read_text("utf-8"))
    if target.network != args.network:
        raise ValueError(f"target is for {target.network}, not {args.network}")
    links = (
        parse_contract(ExecutionLinkSet, args.links.read_text("utf-8")).links if args.links else []
    )
    recorded_at = (
        datetime.fromisoformat(args.recorded_at)
        if args.recorded_at
        else datetime.now(UTC).replace(microsecond=0)
    )
    store = IngestStore(args.store)
    result = ingest_horizon_payments(
        target,
        horizon,
        store,
        start_ledger=args.start_ledger,
        end_ledger=args.end_ledger,
        history=(check.horizon_elder_ledger, check.horizon_latest_ledger),
        recorded_at=recorded_at,
        links=links,
        page_limit=args.page_limit,
        max_pages=args.max_pages,
    )
    _print_run(result)
    complete = result.complete
    if args.sac and not complete:
        print("SAC events skipped: they need the complete Horizon run of the same range first")
    elif args.sac:
        print(
            f"SAC contract {resolve_sac_contract(target, horizon)} (derived locally; "
            f"Horizon /assets publishes the same id, or none while the SAC is not deployed)"
        )
        sac = ingest_sac_events(
            target,
            rpc,
            horizon,
            store,
            start_ledger=args.start_ledger,
            end_ledger=args.end_ledger,
            recorded_at=recorded_at,
            links=links,
            page_limit=args.sac_page_limit,
            max_pages=args.max_pages,
        )
        _print_run(sac)
        complete = complete and sac.complete
    print(
        f"store {args.store}: {len(store.observations())} observations, "
        f"{len(store.coverage())} coverage certificates"
    )
    return 0 if complete else 1


def _ledger_fetch(args: argparse.Namespace) -> int:
    checkpoint = checkpoint_of(args.ledger)
    fetched = fetch_checkpoint(
        args.archive, checkpoint, args.out, get=bounded_get(args.timeout, args.max_bytes)
    )
    for item in fetched:
        print(f"{item.kind} {item.size} bytes sha256 {item.sha256} {item.url}")
    print(f"checkpoint {checkpoint} kept in {args.out} (provenance.json); nothing verified yet")
    return 0


def _ledger_anchor(args: argparse.Namespace) -> int:
    record = observe_anchors(args.out, args.config, args.from_ledger, timeout_seconds=args.timeout)
    print(f"verify-checkpoints run recorded in {record}")
    return 0


def _ledger_verify(args: argparse.Namespace) -> int:
    checkpoint = checkpoint_of(args.ledger)
    anchor: Anchor | None
    anchor_problems: list[str] = []
    if args.anchor_hash is not None:
        anchor = manual_anchor(checkpoint, args.anchor_hash.lower(), args.anchor_note)
    elif args.observe_anchor is not None:
        if args.anchor_dir is None:
            raise ValueError("--observe-anchor needs --anchor-dir")
        record = observe_anchors(args.anchor_dir, args.observe_anchor, checkpoint)
        anchor, anchor_problems = anchor_from_run(record, checkpoint, observed_here=True)
    else:
        anchor, anchor_problems = anchor_from_run(args.anchor_run, checkpoint)
    for problem in anchor_problems:
        print(f"  anchor problem: {problem}")
    cli = StellarCli()
    evidence = verify_checkpoint(args.archive_dir, checkpoint, args.network, anchor, cli)
    target = parse_contract(ChainTarget, args.target.read_text("utf-8"))
    observations = IngestStore(args.store).observations()
    checks = [check_observation(evidence, o, target) for o in observations]
    replay_section = None
    events = []
    artifacts = replay = None
    if args.replay_dir is not None:
        expected = args.expect_replay_record_sha256
        revision = args.expect_replay_revision_sha256
        artifacts, replay = verify_replay(
            evidence,
            args.replay_dir,
            cli,
            expected_record_sha256=None if expected is None else expected.lower(),
            expected_revision_sha256=None if revision is None else revision.lower(),
        )
        events = [
            check_event(replay, o, target)
            for o in observations
            if isinstance(o.payload, TokenMovementPayload)
        ]
        replay_section = replay_report(evidence, artifacts, replay, events)
    elif args.expect_replay_record_sha256 or args.expect_replay_revision_sha256:
        raise ValueError("--expect-replay-*-sha256 needs --replay-dir")
    completeness_section = None
    completeness_status = None
    if args.completeness_ledgers is not None:
        if args.horizon_recording is None:
            raise ValueError("--completeness-ledgers needs --horizon-recording")
        start, end = args.completeness_ledgers
        found = compare(
            enumerate_payments(evidence, target, start, end),
            read_capture(args.horizon_recording, target.account, start, end),
            observations,
            target,
            start,
            end,
        )
        completeness_status = found.status
        completeness_section = completeness_report(found, evidence, target, start, end)
    elif args.horizon_recording is not None:
        raise ValueError("--horizon-recording needs --completeness-ledgers")
    trustline_section = None
    if args.trustline_reconciliation is not None:
        if artifacts is None or replay is None:
            raise ValueError("--trustline-reconciliation needs --replay-dir")
        if args.approved_links is None or args.account_ref is None:
            raise ValueError("--trustline-reconciliation needs --approved-links and --account-ref")
        start, end = (
            datetime.fromisoformat(t.replace("Z", "+00:00")) for t in args.trustline_reconciliation
        )
        links = parse_contract(IdentityLinkSet, args.approved_links.read_text("utf-8")).links
        approvals = approved_addresses(
            links, target.network, args.account_ref, start, end, args.interval_bounds
        )
        temporal = select_ledgers(evidence, start, end, args.interval_bounds)
        trustline_section = reconcile(
            evidence, artifacts, replay, target, approvals, temporal, observations
        )
    elif args.approved_links is not None or args.account_ref is not None:
        raise ValueError("--approved-links and --account-ref need --trustline-reconciliation")
    overall = overall_result(
        evidence,
        checks,
        artifacts,
        replay,
        events,
        completeness_status,
        None if trustline_section is None else trustline_section["status"],
    )
    out = report(
        evidence,
        checks,
        [cli.version],
        replay_section,
        overall,
        completeness_section,
        trustline_section,
    )
    text = json.dumps(out, indent=1, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.write_text(text, "utf-8")
    mode = "none" if anchor is None else anchor.mode
    print(f"evidence {evidence.status} (checkpoint {checkpoint}, anchor mode {mode})")
    for problem in evidence.problems:
        print(f"  problem: {problem}")
    for check in checks:
        print(
            f"  {check.status} {check.observation_id}: inclusion {check.inclusion}, "
            f"result {check.technical_result}, movement {check.movement}: {check.detail}"
        )
    print("coverage: inclusion only; completeness of the account's movements NOT established")
    if replay_section is not None:
        integrity = replay_section["meta_integrity"]
        execution = replay_section["core_execution"]
        correspondence = replay_section["correspondence"]
        print(
            f"replay: artifacts {integrity['status']} ({execution['provenance']}; execution "
            f"not observed by this process), correspondence {correspondence['status']}"
        )
        for problem in integrity["problems"] + correspondence["problems"]:
            print(f"  replay problem: {problem}")
        for event in events:
            print(f"  {event.status} {event.observation_id}: {event.detail}")
        print("events: derived from the replay, not committed by any ledger header")
    if completeness_section is not None:
        print(
            f"classic payment completeness {completeness_section['status']} in ledgers "
            f"{completeness_section['scope']['ledgers']} (payment operations only, not all "
            "movements)"
        )
        for difference in completeness_section["differences"]:
            print(f"  {difference['kind']} {difference['key']}: {difference['detail']}")
    if trustline_section is not None:
        temporal_section = trustline_section["temporal"]
        print(
            f"trustline reconciliation {trustline_section['status']} "
            f"({trustline_section['evidence_basis']}); interval "
            f"{temporal_section['requested']['start']} - {temporal_section['requested']['end']} "
            f"({temporal_section['requested']['bounds']}), ledgers "
            f"{temporal_section['examined_ledgers']}, time coverage {temporal_section['status']}"
        )
        for address in trustline_section["scope"]["addresses"]:
            print(
                f"  approved address {address['address']}: {address['status']} "
                f"{[(w['start'], w['end']) for w in address['applicable_subintervals']]}: "
                f"{address['reason']}"
            )
        for line in trustline_section["lines"]:
            print(
                f"  line {line['line']['account']} {line['line']['asset']['code']}:"
                f"{line['line']['asset']['issuer']}: {line['status']}, opening "
                f"{line['opening']['state']} {line['opening']['balance']} "
                f"({line['opening']['evidence']['kind']}), closing "
                f"{line['closing']['state']} {line['closing']['balance']}, net "
                f"{line['net_variation']}, gross in {line['gross_in']} out {line['gross_out']}"
            )
            for change in line["changes"]:
                print(
                    f"    {change['status']} ledger {change['ledger']} {change['kind']} "
                    f"delta {change['delta']} ({change['operation_type']}): "
                    + "; ".join(change["reasons"])
                )
            for item in line["events_without_change"]:
                print(f"    {item['status']} ledger {item['ledger']}: {item['detail']}")
            for problem in line["contradictions"]:
                print(f"    contradiction: {problem}")
        for tx in trustline_section["transactions"]:
            print(
                f"  tx {tx['tx_hash'][:12]} ledger {tx['ledger']}: {tx['status']} "
                f"({tx['result']}), fee {tx['fee_xlm_stroops']} stroops of XLM (apart)"
            )
        for problem in trustline_section["problems"] + temporal_section["problems"]:
            print(f"  reconciliation problem: {problem}")
        print("trustline changes and events: derived from the replay, not committed by consensus")
    print(f"overall {overall['status']}")
    for key in ("contradictions", "not_established", "unauthenticated"):
        for item in overall[key]:
            print(f"  {key.replace('_', ' ')}: {item}")
    return int(overall["exit_code"])
