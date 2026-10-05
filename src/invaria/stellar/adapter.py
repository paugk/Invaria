"""Stellar adapter: Horizon Classic payments and RPC SAC events -> canonical observations.

Read-only. Both paths emit the same economic-effect key ``<tx_hash>:<op_index>:<ordinal>``
under the target's single source id, so a Classic payment seen by Horizon and its unified
SAC event seen by RPC integrate as one effect (DUPLICATE) or a visible SOURCE_CONFLICT.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import ValidationError

from invaria.contracts.chain import ChainTarget, ExecutionLink, IngestionCheckpoint, OrdinalState
from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.observation import Observation
from invaria.contracts.quantity import Quantity
from invaria.contracts.stellar import NETWORK_PASSPHRASES
from invaria.ingest.journal import observation_content
from invaria.stellar.sources import DataUnavailable, Horizon, Page, Rpc
from invaria.stellar.store import IngestStore
from invaria.stellar.xdr import ScAddress, XdrError, decode_scval, sac_contract_id

HORIZON_PARSER = "stellar-horizon-payments-parser@1.0.0"
HORIZON_MAPPING = "stellar-classic-payment@1.0.0"
RPC_PARSER = "stellar-rpc-events-parser@1.0.0"
RPC_MAPPING = "stellar-sac-movement@1.0.0"
MOVEMENT_TOPICS = frozenset({"transfer", "mint", "burn", "clawback"})
SCALE = 7

ExclusionReason = Literal[
    "UNSUPPORTED_OPERATION",
    "UNSUPPORTED_EVENT",
    "UNSUPPORTED_PARTY",
    "CONTRACT_PARTY",
    "MUXED_ACCOUNT",
    "UNSUPPORTED_VALUE",
    "MALFORMED",
]


@dataclass(frozen=True)
class Exclusion:
    """A record of the target asset involving the account that cannot be represented."""

    path: str
    locator: str
    reason: ExclusionReason
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "locator": self.locator,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RunResult:
    path: str
    complete: bool
    pages: int
    appended: tuple[Observation, ...]
    duplicates: int
    conflicts: tuple[str, ...]
    exclusions: tuple[Exclusion, ...]
    coverage: CoverageCertificate | None
    stop_reason: str


def _link_lookup(links: Sequence[ExecutionLink]) -> dict[tuple[str, str, int, int], str]:
    return {
        (link.network, link.tx_hash, link.operation_index, link.ordinal): link.operation_ref
        for link in links
    }


def toid_parts(toid: int) -> tuple[int, int, int]:
    """(ledger, transaction application order, 0-based operation index) from a TOID."""
    op_number = toid & 0xFFF
    if op_number == 0:
        raise ValueError("TOID does not identify an operation")
    return toid >> 32, (toid >> 12) & 0xFFFFF, op_number - 1


def _observation(
    target: ChainTarget,
    *,
    tx_hash: str,
    op_index: int,
    ordinal: int,
    ledger: int,
    successful: bool,
    from_address: str,
    to_address: str,
    atoms: int,
    valid_time: str,
    page: Page,
    locator: str,
    parser: str,
    mapping: str,
    recorded_at: datetime,
    links: Mapping[tuple[str, str, int, int], str],
) -> Observation:
    record_key = f"{tx_hash}:{op_index}:{ordinal}"
    identity = f"{target.source_id}|{record_key}|1|{page.sha256}|{locator}"
    return Observation.model_validate_json(
        Observation.model_validate(
            {
                "schema_version": "1.0",
                "observation_id": "obs-" + hashlib.sha256(identity.encode()).hexdigest()[:32],
                "tenant_id": target.tenant_id,
                "kind": "assertion",
                "fact_type": "token_movement",
                "instrument_id": target.instrument_id,
                "representation_id": target.representation_id,
                "operation_ref": links.get((target.network, tx_hash, op_index, ordinal)),
                "source": {"source_id": target.source_id, "record_key": record_key, "revision": 1},
                "valid_time": datetime.fromisoformat(valid_time.replace("Z", "+00:00")),
                "recorded_at": recorded_at,
                "provenance": {
                    "raw_sha256": page.sha256,
                    "raw_locator": locator[:512],
                    "parser_ref": parser,
                    "mapping_ref": mapping,
                },
                "supersedes": None,
                "payload": {
                    "payload_type": "token_movement",
                    "from_address": from_address,
                    "to_address": to_address,
                    "units": Quantity(atoms=str(atoms), scale=SCALE, unit=target.unit),
                    "chain": {
                        "network": target.network,
                        "ledger": ledger,
                        "tx_hash": tx_hash,
                        "operation_index": op_index,
                        "tx_successful": successful,
                    },
                },
                "synthetic": target.synthetic,
            },
            strict=False,
        ).model_dump_json()
    )


# ------------------------------------------------------------ Horizon (Classic)


def normalize_horizon_record(
    target: ChainTarget,
    record: Mapping[str, Any],
    page: Page,
    index: int,
    recorded_at: datetime,
    links: Mapping[tuple[str, str, int, int], str],
) -> Observation | Exclusion | None:
    """None = outside the target scope (other asset, native-only operation)."""
    locator = f"{page.locator}#/_embedded/records/{index}"
    kind = str(record.get("type"))

    def is_target(prefix: str = "") -> bool:
        return (
            str(record.get(f"{prefix}asset_type", "")).startswith("credit_alphanum")
            and record.get(f"{prefix}asset_code") == target.asset_code
            and record.get(f"{prefix}asset_issuer") == target.asset_issuer
        )

    if kind in ("path_payment_strict_send", "path_payment_strict_receive"):
        if is_target() or is_target("source_"):
            return Exclusion(
                "horizon_payments",
                locator,
                "UNSUPPORTED_OPERATION",
                f"{kind} of the target asset is not supported yet",
            )
        return None
    if kind == "invoke_host_function":
        changes = record.get("asset_balance_changes") or []
        if any(
            c.get("asset_code") == target.asset_code
            and c.get("asset_issuer") == target.asset_issuer
            for c in changes
        ):
            return Exclusion(
                "horizon_payments",
                locator,
                "UNSUPPORTED_OPERATION",
                "SAC effect inside invoke_host_function: use the RPC path",
            )
        return None
    if kind != "payment" or not is_target():
        return None
    if record.get("from_muxed") or record.get("to_muxed"):
        return Exclusion(
            "horizon_payments",
            locator,
            "MUXED_ACCOUNT",
            "muxed sub-account attribution is not supported",
        )
    try:
        ledger, _order, op_index = toid_parts(int(str(record["paging_token"])))
        transaction = record.get("transaction") or {}
        tx_hash = str(record["transaction_hash"])
        if transaction and (
            int(transaction["ledger"]) != ledger
            or (
                str(transaction["hash"]) != tx_hash
                and str(transaction.get("inner_transaction", {}).get("hash")) != tx_hash
            )
        ):
            raise ValueError("joined transaction does not match the operation id")
        successful = bool(record["transaction_successful"])
        if transaction and bool(transaction["successful"]) != successful:
            raise ValueError("transaction_successful disagrees with the joined transaction")
        units = Quantity.from_decimal_text(str(record["amount"]), scale=SCALE, unit=target.unit)
        if units.atoms.startswith("-"):
            raise ValueError("negative amount")
        return _observation(
            target,
            tx_hash=tx_hash,
            op_index=op_index,
            ordinal=0,
            ledger=ledger,
            successful=successful,
            from_address=str(record["from"]),
            to_address=str(record["to"]),
            atoms=int(units.atoms),
            valid_time=str(record["created_at"]),
            page=page,
            locator=locator,
            parser=HORIZON_PARSER,
            mapping=HORIZON_MAPPING,
            recorded_at=recorded_at,
            links=links,
        )
    except (KeyError, ValueError, TypeError, ValidationError) as error:
        return Exclusion(
            "horizon_payments", locator, "MALFORMED", f"{type(error).__name__}: {str(error)[:200]}"
        )


# -------------------------------------------------------------- RPC (SAC events)


def _decode_topic(b64: str) -> Any:
    try:
        return decode_scval(b64)
    except XdrError as error:
        return error


def normalize_sac_event(
    target: ChainTarget,
    contract_id: str,
    event: Mapping[str, Any],
    ordinal: int,
    page: Page,
    recorded_at: datetime,
    links: Mapping[tuple[str, str, int, int], str],
) -> Observation | Exclusion | None:
    locator = f"{page.locator}#{event.get('id')}"
    if event.get("contractId") != contract_id or event.get("type") != "contract":
        return None
    topics = [_decode_topic(t) for t in event.get("topic", [])]
    if not topics or topics[0] not in MOVEMENT_TOPICS:
        return None  # approve, set_authorized, set_admin...: not movements
    head = topics[0]
    expected_asset = f"{target.asset_code}:{target.asset_issuer}"
    shape = {"transfer": 4, "mint": 3, "burn": 3, "clawback": 3}[head]
    parties = topics[1:-1]
    involves = any(isinstance(p, ScAddress) and p.strkey == target.account for p in parties)
    if head in ("mint", "burn", "clawback") and target.account == target.asset_issuer:
        involves = True  # supply changes always involve the issuer
    if not involves:
        return None
    if len(topics) != shape or topics[-1] != expected_asset:
        return Exclusion(
            "rpc_sac_events", locator, "MALFORMED", f"unexpected topic shape or asset for {head}"
        )
    if head == "clawback":
        return Exclusion(
            "rpc_sac_events", locator, "UNSUPPORTED_EVENT", "clawback is not supported yet"
        )
    if any(isinstance(p, XdrError) for p in parties):
        return Exclusion(
            "rpc_sac_events",
            locator,
            "UNSUPPORTED_PARTY",
            "counterparty address type not supported (claimable balance or pool)",
        )
    if any(isinstance(p, ScAddress) and p.kind == "contract" for p in parties):
        return Exclusion(
            "rpc_sac_events",
            locator,
            "CONTRACT_PARTY",
            "contract counterparty cannot be represented as an account movement",
        )
    value = _decode_topic(str(event.get("value", "")))
    if isinstance(value, dict):
        muxed = value.get("to_muxed_id")
        if set(value) - {"amount", "to_muxed_id"}:
            return Exclusion(
                "rpc_sac_events",
                locator,
                "UNSUPPORTED_VALUE",
                f"unexpected movement data keys {sorted(value)}",
            )
        if isinstance(muxed, int):
            return Exclusion(
                "rpc_sac_events",
                locator,
                "MUXED_ACCOUNT",
                "to_muxed_id is a u64: muxed destination or memo id, "
                "indistinguishable from the event; sub-account attribution "
                "is not supported",
            )
        value = value.get("amount")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return Exclusion(
            "rpc_sac_events",
            locator,
            "UNSUPPORTED_VALUE",
            "event amount is not a non-negative i128",
        )
    addresses = [p.strkey for p in parties if isinstance(p, ScAddress)]
    if head == "transfer":
        from_address, to_address = addresses
    elif head == "mint":
        from_address, to_address = target.asset_issuer, addresses[0]
    else:
        from_address, to_address = addresses[0], target.asset_issuer
    try:
        return _observation(
            target,
            tx_hash=str(event["txHash"]),
            op_index=int(event["operationIndex"]),
            ordinal=ordinal,
            ledger=int(event["ledger"]),
            successful=bool(event.get("inSuccessfulContractCall", False)),
            from_address=from_address,
            to_address=to_address,
            atoms=value,
            valid_time=str(event["ledgerClosedAt"]),
            page=page,
            locator=locator,
            parser=RPC_PARSER,
            mapping=RPC_MAPPING,
            recorded_at=recorded_at,
            links=links,
        )
    except (KeyError, ValueError, TypeError, ValidationError) as error:
        return Exclusion(
            "rpc_sac_events", locator, "MALFORMED", f"{type(error).__name__}: {str(error)[:200]}"
        )


# ------------------------------------------------------------------- integration


def integrate_chain(
    journal: Sequence[Observation], incoming: Sequence[Observation]
) -> tuple[list[Observation], int, list[str]]:
    """Append-only: (appended, duplicates, conflicting record keys)."""
    working = list(journal)
    appended: list[Observation] = []
    duplicates = 0
    conflicts: list[str] = []
    for observation in incoming:
        same = [o for o in working if o.source == observation.source]
        if any(observation_content(o) == observation_content(observation) for o in same):
            duplicates += 1
            continue
        if same:
            conflicts.append(observation.source.record_key)
        working.append(observation)
        appended.append(observation)
    return appended, duplicates, conflicts


# ------------------------------------------------------------------------- runs


def _close_time(horizon: Horizon, store: IngestStore, sequence: int) -> tuple[str, str]:
    page = horizon.ledger(sequence)
    store.save_page(page.sha256, page.raw)
    return str(page.document["closed_at"]), page.sha256


def _coverage(
    target: ChainTarget,
    horizon: Horizon,
    store: IngestStore,
    checkpoint: IngestionCheckpoint,
    method: str,
    recorded_at: datetime,
) -> CoverageCertificate:
    start_time, start_sha = _close_time(horizon, store, checkpoint.start_ledger)
    end_time, end_sha = _close_time(horizon, store, checkpoint.end_ledger + 1)
    return CoverageCertificate.model_validate_json(
        CoverageCertificate.model_validate(
            {
                "schema_version": "1.0",
                "coverage_id": f"cov-{target.target_id}-{checkpoint.path}-"
                f"{checkpoint.start_ledger}-{checkpoint.end_ledger}",
                "tenant_id": target.tenant_id,
                "source_id": target.source_id,
                "fact_types": ["token_movement"],
                "instrument_id": target.instrument_id,
                "interval": {"start": start_time, "end": end_time},
                "ledger_range": {"first": checkpoint.start_ledger, "last": checkpoint.end_ledger},
                "level": "provider_claimed",
                "method": method,
                "records_received": checkpoint.records_received,
                "records_quarantined": checkpoint.records_quarantined,
                "gaps": [],
                "raw_sha256": sorted({*checkpoint.page_sha256, start_sha, end_sha}),
                "recorded_at": recorded_at,
                "synthetic": target.synthetic,
            },
            strict=False,
        ).model_dump_json()
    )


def _fresh_checkpoint(
    target: ChainTarget,
    path: Literal["horizon_payments", "rpc_sac_events"],
    start: int,
    end: int,
    cursor: str,
) -> IngestionCheckpoint:
    return IngestionCheckpoint(
        schema_version="1.0",
        target_id=target.target_id,
        path=path,
        start_ledger=start,
        end_ledger=end,
        cursor=cursor,
        pages=0,
        records_received=0,
        records_quarantined=0,
        page_sha256=[],
        ordinal_state=None,
        complete=False,
    )


def _resume(
    store: IngestStore,
    target: ChainTarget,
    path: Literal["horizon_payments", "rpc_sac_events"],
    start: int,
    end: int,
    cursor: str,
) -> IngestionCheckpoint:
    existing = store.get_checkpoint(target.target_id, path)
    if existing and (existing.start_ledger, existing.end_ledger) == (start, end):
        return existing
    return _fresh_checkpoint(target, path, start, end, cursor)


def _persist_page(
    store: IngestStore,
    page: Page,
    observations: Sequence[Observation],
    exclusions: Sequence[Exclusion],
    checkpoint: IngestionCheckpoint,
) -> tuple[list[Observation], int, list[str]]:
    """Order matters: raw page, observations, exclusions (all fsync'ed), then checkpoint."""
    store.save_page(page.sha256, page.raw)
    appended, duplicates, conflicts = integrate_chain(store.observations(), observations)
    store.append_observations(appended)
    store.append_exclusions(e.as_dict() for e in exclusions)
    store.put_checkpoint(checkpoint)
    return appended, duplicates, conflicts


def ingest_horizon_payments(
    target: ChainTarget,
    horizon: Horizon,
    store: IngestStore,
    *,
    start_ledger: int,
    end_ledger: int,
    history: tuple[int, int],
    recorded_at: datetime,
    links: Sequence[ExecutionLink] = (),
    page_limit: int = 200,
    max_pages: int = 50,
) -> RunResult:
    elder, latest = history
    if start_ledger < elder or end_ledger >= latest:
        raise DataUnavailable(
            f"ledgers {start_ledger}-{end_ledger} not fully inside Horizon history "
            f"{elder}-{latest} (the ledger after the range must be closed)"
        )
    lookup = _link_lookup(links)
    checkpoint = _resume(
        store, target, "horizon_payments", start_ledger, end_ledger, str(start_ledger << 32)
    )
    appended_all: list[Observation] = []
    exclusions_all: list[Exclusion] = []
    conflicts_all: list[str] = []
    duplicates_all = 0
    pages = 0
    stop = "range complete" if checkpoint.complete else ""
    while not checkpoint.complete:
        if pages >= max_pages:
            stop = f"page budget {max_pages} exhausted before ledger {end_ledger}"
            break
        page = horizon.account_payments(target.account, checkpoint.cursor, page_limit)
        pages += 1
        records = page.document.get("_embedded", {}).get("records", [])
        observations: list[Observation] = []
        exclusions: list[Exclusion] = []
        cursor, received, done = checkpoint.cursor, 0, False
        for index, record in enumerate(records):
            if int(str(record["paging_token"])) >> 32 > end_ledger:
                done = True
                break
            received += 1
            cursor = str(record["paging_token"])
            result = normalize_horizon_record(target, record, page, index, recorded_at, lookup)
            if isinstance(result, Observation):
                observations.append(result)
            elif isinstance(result, Exclusion):
                exclusions.append(result)
        if len(records) < page_limit:
            done = True
        checkpoint = checkpoint.model_copy(
            update={
                "cursor": cursor,
                "pages": checkpoint.pages + 1,
                "records_received": checkpoint.records_received + received,
                "records_quarantined": checkpoint.records_quarantined + len(exclusions),
                "page_sha256": [*checkpoint.page_sha256, page.sha256],
                "complete": done,
            }
        )
        appended, duplicates, conflicts = _persist_page(
            store, page, observations, exclusions, checkpoint
        )
        appended_all += appended
        exclusions_all += exclusions
        conflicts_all += conflicts
        duplicates_all += duplicates
        stop = "range complete" if done else stop
    coverage = None
    if checkpoint.complete:
        method = (
            f"Horizon account payments for {target.account}, asset "
            f"{target.asset_code}:{target.asset_issuer}, failed transactions included; "
            f"provider claim, not independently verified"
        )
        coverage = _coverage(target, horizon, store, checkpoint, method, recorded_at)
        store.append_coverage(coverage)
    return RunResult(
        "horizon_payments",
        checkpoint.complete,
        pages,
        tuple(appended_all),
        duplicates_all,
        tuple(conflicts_all),
        tuple(exclusions_all),
        coverage,
        stop,
    )


def resolve_sac_contract(target: ChainTarget, horizon: Horizon | None = None) -> str:
    derived = sac_contract_id(
        target.asset_code, target.asset_issuer, NETWORK_PASSPHRASES[target.network]
    )
    if target.expected_sac_contract_id and target.expected_sac_contract_id != derived:
        raise ValueError(f"expected SAC {target.expected_sac_contract_id} != derived {derived}")
    if horizon is not None:
        page = horizon.asset(target.asset_code, target.asset_issuer)
        records = page.document.get("_embedded", {}).get("records", [])
        published = {r.get("contract_id") for r in records}
        if published != {derived}:
            raise ValueError(
                f"Horizon publishes SAC {sorted(map(str, published))}, derived {derived}"
            )
    return derived


def ingest_sac_events(
    target: ChainTarget,
    rpc: Rpc,
    horizon: Horizon,
    store: IngestStore,
    *,
    start_ledger: int,
    end_ledger: int,
    recorded_at: datetime,
    links: Sequence[ExecutionLink] = (),
    page_limit: int = 200,
    max_pages: int = 50,
) -> RunResult:
    contract_id = resolve_sac_contract(target, horizon)
    lookup = _link_lookup(links)
    checkpoint = _resume(store, target, "rpc_sac_events", start_ledger, end_ledger, "0")
    appended_all: list[Observation] = []
    exclusions_all: list[Exclusion] = []
    conflicts_all: list[str] = []
    duplicates_all = 0
    pages = 0
    stop = "range complete" if checkpoint.complete else ""
    while not checkpoint.complete:
        if pages >= max_pages:
            stop = f"page budget {max_pages} exhausted before ledger {end_ledger}"
            break
        first = checkpoint.pages == 0
        page = rpc.get_events(
            contract_id,
            start_ledger=start_ledger if first else None,
            end_ledger=end_ledger,
            cursor=None if first else checkpoint.cursor,
            limit=page_limit,
        )
        pages += 1
        result_doc = page.document["result"]
        if int(result_doc.get("latestLedger", 0)) < end_ledger:
            raise DataUnavailable(
                f"RPC latest ledger {result_doc.get('latestLedger')} < end ledger {end_ledger}"
            )
        returned = list(result_doc.get("events", []))
        beyond = [e for e in returned if int(e.get("ledger", 0)) > end_ledger]
        events = returned[: returned.index(beyond[0])] if beyond else returned
        state = checkpoint.ordinal_state
        observations: list[Observation] = []
        exclusions: list[Exclusion] = []
        for event in events:
            head = _decode_topic(event.get("topic", [""])[0])
            ordinal = 0
            if head in MOVEMENT_TOPICS:
                tx_hash, op_index = str(event.get("txHash")), int(event.get("operationIndex", 0))
                if state and (state.tx_hash, state.operation_index) == (tx_hash, op_index):
                    ordinal = state.next_ordinal
                state = OrdinalState(
                    tx_hash=tx_hash, operation_index=op_index, next_ordinal=ordinal + 1
                )
            result = normalize_sac_event(
                target, contract_id, event, ordinal, page, recorded_at, lookup
            )
            if isinstance(result, Observation):
                observations.append(result)
            elif isinstance(result, Exclusion):
                exclusions.append(result)
        done = bool(beyond) or len(returned) < page_limit
        next_cursor = str(events[-1]["id"]) if beyond and events else result_doc.get("cursor")
        checkpoint = checkpoint.model_copy(
            update={
                "cursor": str(next_cursor or checkpoint.cursor),
                "pages": checkpoint.pages + 1,
                "records_received": checkpoint.records_received + len(events),
                "records_quarantined": checkpoint.records_quarantined + len(exclusions),
                "page_sha256": [*checkpoint.page_sha256, page.sha256],
                "ordinal_state": state,
                "complete": done,
            }
        )
        appended, duplicates, conflicts = _persist_page(
            store, page, observations, exclusions, checkpoint
        )
        appended_all += appended
        exclusions_all += exclusions
        conflicts_all += conflicts
        duplicates_all += duplicates
        stop = "range complete" if done else stop
    coverage = None
    if checkpoint.complete:
        method = (
            f"RPC getEvents for SAC {contract_id} ({target.asset_code}:"
            f"{target.asset_issuer}), movements involving {target.account}; provider "
            f"claim within RPC retention, not independently verified"
        )
        coverage = _coverage(target, horizon, store, checkpoint, method, recorded_at)
        store.append_coverage(coverage)
    return RunResult(
        "rpc_sac_events",
        checkpoint.complete,
        pages,
        tuple(appended_all),
        duplicates_all,
        tuple(conflicts_all),
        tuple(exclusions_all),
        coverage,
        stop,
    )
