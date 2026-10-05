"""`invaria stellar ...`: read-only testnet access. Never signs, funds or submits."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from invaria.contracts.base import parse_contract
from invaria.contracts.chain import ChainTarget, ExecutionLinkSet
from invaria.stellar.adapter import (
    RunResult,
    ingest_horizon_payments,
    ingest_sac_events,
    resolve_sac_contract,
)
from invaria.stellar.http import HttpClient, RecordingClient, ReplayClient, UrllibClient
from invaria.stellar.sources import Endpoints, Horizon, Rpc, verify_network
from invaria.stellar.store import IngestStore


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
    if args.sac:
        print(
            f"SAC contract {resolve_sac_contract(target, horizon)} (derived locally, "
            f"matches Horizon /assets contract_id)"
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
