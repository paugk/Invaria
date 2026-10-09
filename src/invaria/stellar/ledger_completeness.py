"""Completeness of Classic ``payment`` operations of one asset and one account, in a declared
ledger interval, from a checkpoint verified against its anchor.

The scope is exact and narrow: Classic ``payment`` operations (type 1) of the target asset,
named by its code and full issuer, with the watched account as the operation's source
(explicit or the transaction's) or destination, muxed (M) addresses decoded to their base,
inner transactions of fee bumps included, successful and failed alike. Each operation is
counted once, by (transaction hash, operation index), never matched by amount.

Three things are kept apart:
- the enumeration from the anchored transaction and result sets (no provider filter);
- the provider's capture (recorded Horizon ``/accounts/{id}/payments`` pages), whether it is
  complete for the interval and whether its filters include each operation;
- the adapter's records in the ingest store.

A difference is a provider omission only when the capture is shown complete for the
interval and its filters include the operation (failed ones too); otherwise it is an
incomplete capture or an incompatible scope. Decoding an M address only enumerates: it
attributes no identity and changes no ``IdentityLink``. The absence shown concerns these
payment operations only: never all the account's movements or balance changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from invaria.contracts.chain import ChainTarget
from invaria.contracts.observation import Observation, TokenMovementPayload
from invaria.stellar.adapter import HORIZON_MAPPING
from invaria.stellar.ledger_proof import (
    CHECKPOINT_FREQUENCY,
    CheckpointEvidence,
    _base,
    _inner_tx,
    _muxed_id,
    _outcome,
)

EnumerationStatus = Literal["ENUMERATED", "INCOMPLETE", "UNVERIFIED"]
CaptureStatus = Literal["COMPLETE", "INCOMPLETE"]
DifferenceClass = Literal[
    "PROVIDER_OMISSION",
    "CAPTURE_INCOMPLETE",
    "SCOPE_INCOMPATIBLE",
    "EXTRACTION_GAP",
    "PROVIDER_CONTRADICTION",
    "ADAPTER_CONTRADICTION",
    "OUT_OF_SCOPE",
]
CompletenessStatus = Literal["COMPLETE_IN_SCOPE", "GAPS_FOUND", "CONTRADICTED", "INCOMPLETE"]
PAYMENTS_ENDPOINT = "/accounts/{account}/payments"
EXCLUDED = [
    "path_payment_strict_send and path_payment_strict_receive",
    "DEX fills, including those of the account's own offers (in other accounts' transactions)",
    "claimable balances: creation, claim and clawback",
    "clawback",
    "liquidity pool deposits and withdrawals",
    "invoke_host_function and SAC events",
    "change_trust and trustline flag changes",
    "fees (XLM)",
    "create_account and account_merge",
]
NOT_PROVEN = [
    "that the account had no other movements of the asset (path payments, fills, SAC, "
    "claimable balances, clawback)",
    "that its balance did not change in other ways",
    "anything outside the declared ledger interval",
]

Key = tuple[str, int]  # (transaction hash as the network names it, operation index)


@dataclass(frozen=True)
class ScopedPayment:
    ledger: int
    tx_hash: str
    op_index: int
    tx_successful: bool
    technical_result: str
    source: str
    destination: str
    source_muxed_id: str | None
    destination_muxed_id: str | None
    amount: str
    fee_bump: bool
    account_roles: tuple[str, ...]


@dataclass
class Enumeration:
    status: EnumerationStatus
    payments: list[ScopedPayment] = field(default_factory=list)
    ledgers_checked: list[int] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _credit(asset: Any) -> tuple[str, str] | None:
    if not isinstance(asset, dict):
        return None
    credit = asset.get("credit_alphanum4") or asset.get("credit_alphanum12")
    if not isinstance(credit, dict):
        return None
    return str(credit["asset_code"]), str(credit["issuer"])


def scoped_payments(
    ledger: int,
    tx_hash: str,
    envelope: dict[str, Any],
    result: dict[str, Any],
    target: ChainTarget,
) -> list[ScopedPayment]:
    """The in-scope payments of one transaction (ValueError if it cannot be read)."""
    tx, fee_bump = _inner_tx(envelope)
    found = []
    for index, operation in enumerate(tx["operations"]):
        body = operation["body"]
        if not isinstance(body, dict) or "payment" not in body:
            continue
        payment = body["payment"]
        if _credit(payment["asset"]) != (target.asset_code, target.asset_issuer):
            continue
        source = operation["source_account"] or tx["source_account"]
        destination = payment["destination"]
        roles = tuple(
            role
            for role, address in (("source", source), ("destination", destination))
            if _base(address) == target.account
        )
        if not roles:
            continue
        tx_code, op_code = _outcome(result, fee_bump, index)
        prefix = "fee bump, inner " if fee_bump else ""
        found.append(
            ScopedPayment(
                ledger,
                tx_hash,
                index,
                tx_code == "tx_success",
                f"{prefix}{tx_code}; operation {index}: {op_code}",
                _base(source),
                _base(destination),
                _muxed_id(source),
                _muxed_id(destination),
                str(payment["amount"]),
                fee_bump,
                roles,
            )
        )
    return found


def enumerate_payments(
    evidence: CheckpointEvidence, target: ChainTarget, start: int, end: int
) -> Enumeration:
    """Every in-scope payment of ledgers ``start``..``end`` (both included), from the
    transaction and result sets verified against the anchor. A ledger without a verified
    set, or an entry that cannot be read, leaves the enumeration INCOMPLETE."""
    out = Enumeration("INCOMPLETE")
    if evidence.status != "VERIFIED":
        out.status = "UNVERIFIED"
        out.problems.append(f"checkpoint evidence is {evidence.status}")
        return out
    first = evidence.checkpoint - CHECKPOINT_FREQUENCY + 1
    if not first <= start <= end <= evidence.checkpoint:
        out.problems.append(
            f"interval {start}-{end} is not inside the verified checkpoint {first}-"
            f"{evidence.checkpoint}"
        )
        return out
    if target.network != evidence.network:
        out.problems.append("the target names another network than the evidence")
        return out
    seen: set[Key] = set()
    for seq in range(start, end + 1):
        content = evidence.ledgers.get(seq)
        if content is None:
            out.problems.append(f"ledger {seq} has no verified transaction set")
            continue
        if len(content.envelopes) != len(content.tx_hashes):
            out.problems.append(f"ledger {seq}: envelopes and hashes do not correspond")
            continue
        for envelope, tx_hash in zip(content.envelopes, content.tx_hashes, strict=True):
            try:
                payments = scoped_payments(seq, tx_hash, envelope, content.results[tx_hash], target)
            except (ValueError, KeyError, TypeError, IndexError) as error:
                out.problems.append(f"ledger {seq}: unreadable transaction {tx_hash}: {error}")
                continue
            for payment in payments:
                key = (payment.tx_hash, payment.op_index)
                if key not in seen:
                    seen.add(key)
                    out.payments.append(payment)
        out.ledgers_checked.append(seq)
    if not out.problems:
        out.status = "ENUMERATED"
    return out


# ------------------------------------------------------------------ provider capture


@dataclass(frozen=True)
class CapturedRecord:
    ledger: int
    tx_hash: str
    op_index: int
    type: str
    tx_successful: bool
    asset: tuple[str, str] | None
    source: str | None
    destination: str | None
    source_muxed_id: str | None
    destination_muxed_id: str | None
    amount: str | None


@dataclass
class Capture:
    status: CaptureStatus
    filters: dict[str, Any] = field(default_factory=dict)
    pages: list[dict[str, Any]] = field(default_factory=list)
    end_evidence: str | None = None
    records: dict[Key, CapturedRecord] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _stroops(amount: str) -> str:
    try:
        value = Decimal(amount) * 10_000_000
    except InvalidOperation as error:
        raise ValueError(f"amount {amount!r}") from error
    if value != value.to_integral_value():
        raise ValueError(f"amount {amount!r} has more than 7 decimals")
    return str(int(value))


def _captured(record: dict[str, Any]) -> CapturedRecord:
    token = int(str(record["paging_token"]))
    op_number = token & 0xFFF
    if op_number == 0:
        raise ValueError("paging token does not identify an operation")
    kind = str(record["type"])
    asset = None
    if str(record.get("asset_type", "")).startswith("credit_alphanum"):
        asset = (str(record["asset_code"]), str(record["asset_issuer"]))
    payment = kind == "payment"
    return CapturedRecord(
        token >> 32,
        str(record["transaction_hash"]),
        op_number - 1,
        kind,
        bool(record["transaction_successful"]),
        asset,
        str(record["from"]) if payment else None,
        str(record["to"]) if payment else None,
        str(record["from_muxed_id"]) if payment and "from_muxed_id" in record else None,
        str(record["to_muxed_id"]) if payment and "to_muxed_id" in record else None,
        _stroops(str(record["amount"])) if payment else None,
    )


def read_capture(directory: Path, account: str, start: int, end: int) -> Capture:
    """The provider's capture of ``account``'s payments, from recorded Horizon exchanges.

    Complete for the interval only if the recorded pages chain from a cursor at or before
    the start of ``start``, each next page asked from the last token of the previous one,
    with failed transactions included, until a record beyond ``end`` or the end of the
    stream, the latter only when Horizon had already ingested past ``end`` (its root,
    recorded before that page)."""
    path = PAYMENTS_ENDPOINT.format(account=account)
    out = Capture("INCOMPLETE")
    out.filters = {
        "endpoint": path,
        "account": account,
        "include_failed": None,
        "order": "asc",
        "lists": "Horizon's payment-type operations in which the account (the base of an M "
        "address) is a participant: for payment, its source or destination (provider "
        "documentation, not verified here)",
    }
    pages: dict[int, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []
    for meta_path in sorted(directory.glob("*.json")):
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
            if meta.get("method") != "GET":
                continue
            url = urlsplit(str(meta["url"]))
            body = meta_path.with_suffix(".body").read_bytes()
        except (OSError, ValueError, KeyError) as error:
            out.problems.append(f"unreadable recording {meta_path.name}: {error}")
            return out
        if url.path not in ("/", path):
            continue
        if hashlib.sha256(body).hexdigest() != meta["response_sha256"] or meta["status"] != 200:
            out.problems.append(f"recording {meta_path.name} does not match its sha256 or failed")
            return out
        document = json.loads(body)
        entry = {
            "captured_at": str(meta["captured_at"]),
            "document": document,
            "sha256": meta["response_sha256"],
        }
        if url.path == "/":
            roots.append(entry)
            continue
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        entry.update(cursor=query.get("cursor", ""), query=query)
        try:
            pages[int(entry["cursor"])] = entry
        except ValueError:
            out.problems.append(f"page {meta_path.name} has no numeric cursor")
            return out
    begin = start << 32
    starts = [c for c in pages if c <= begin]
    if not starts:
        out.problems.append(f"no recorded page starts at or before ledger {start}")
        return out
    cursor = max(starts)
    chained: list[dict[str, Any]] = []
    while True:
        page = pages.get(cursor)
        if page is None:
            out.problems.append(f"the page after cursor {cursor} is not recorded")
            return out
        if page["query"].get("order") != "asc":
            out.problems.append(f"page {cursor} is not in ascending order")
            return out
        chained.append(page)
        records = page["document"].get("_embedded", {}).get("records", [])
        limit = int(page["query"].get("limit", "10"))
        out.pages.append(
            {
                "cursor": str(cursor),
                "limit": limit,
                "records": len(records),
                "sha256": page["sha256"],
                "captured_at": page["captured_at"],
            }
        )
        try:
            captured = [_captured(r) for r in records]
        except (KeyError, ValueError, TypeError) as error:
            out.problems.append(f"unreadable record in page {cursor}: {error}")
            return out
        for record in captured:
            if record.ledger > end:
                out.end_evidence = f"a record of ledger {record.ledger}, after {end}"
                break
            if record.ledger < start:
                continue
            key = (record.tx_hash, record.op_index)
            if key in out.records and out.records[key] != record:
                out.problems.append(f"operation {key} listed twice with different content")
                return out
            out.records[key] = record
        if out.end_evidence is not None:
            break
        if len(records) < limit:
            ingested = [
                int(r["document"].get("history_latest_ledger", 0))
                for r in roots
                if r["captured_at"] <= page["captured_at"]
            ]
            if not ingested or max(ingested) <= end:
                out.problems.append(
                    f"the stream ended but no root recorded before it shows ledger {end} "
                    "already ingested"
                )
                return out
            out.end_evidence = (
                f"end of stream while Horizon had ingested up to ledger {max(ingested)}"
            )
            break
        cursor = int(str(records[-1]["paging_token"]))
    # A filter, not a gap: without failed transactions the capture can still be complete for
    # the successful ones (filter_includes decides per operation).
    out.filters["include_failed"] = all(p["query"].get("include_failed") == "true" for p in chained)
    out.status = "COMPLETE"
    return out


def filter_includes(capture: Capture, payment: ScopedPayment) -> bool:
    """Whether the capture's declared filters include this in-scope payment."""
    account = capture.filters.get("account")
    if account not in (payment.source, payment.destination):
        return False
    return payment.tx_successful or capture.filters.get("include_failed") is True


# ------------------------------------------------------------------ comparison


@dataclass(frozen=True)
class Difference:
    kind: DifferenceClass
    key: Key
    ledger: int
    detail: str


@dataclass
class CompletenessReport:
    status: CompletenessStatus
    enumeration: Enumeration
    capture: Capture
    adapter: dict[str, Any]
    differences: list[Difference]


def _record_differs(payment: ScopedPayment, record: CapturedRecord) -> list[str]:
    fields = []
    if record.type != "payment":
        fields.append(f"type {record.type}")
    if record.tx_successful != payment.tx_successful:
        fields.append("success")
    if record.amount != payment.amount:
        fields.append("amount")
    if (record.source, record.destination) != (payment.source, payment.destination):
        fields.append("parties")
    if (record.source_muxed_id, record.destination_muxed_id) != (
        payment.source_muxed_id,
        payment.destination_muxed_id,
    ):
        fields.append("muxed ids")
    return fields


def _in_scope(record: CapturedRecord, target: ChainTarget) -> bool:
    return (
        record.type == "payment"
        and record.asset == (target.asset_code, target.asset_issuer)
        and target.account in (record.source, record.destination)
    )


def _chain(observation: Observation) -> Any:
    return getattr(observation.payload, "chain", None)


def compare(
    enumeration: Enumeration,
    capture: Capture,
    observations: list[Observation],
    target: ChainTarget,
    start: int,
    end: int,
) -> CompletenessReport:
    differences: list[Difference] = []
    stored: dict[Key, list[Observation]] = {}
    for observation in observations:
        chain = _chain(observation)
        if chain is None or not start <= chain.ledger <= end:
            continue
        if observation.provenance.mapping_ref == HORIZON_MAPPING:
            # The adapter's records of the provider's Classic payments; copies read from other
            # routes (SAC events) are not the extraction compared here.
            stored.setdefault((chain.tx_hash, chain.operation_index), []).append(observation)
    adapter: dict[str, Any] = {"matched": [], "failed_attempts": []}
    if enumeration.status != "ENUMERATED":
        return CompletenessReport("INCOMPLETE", enumeration, capture, adapter, differences)
    enumerated = {(p.tx_hash, p.op_index): p for p in enumeration.payments}
    for key, payment in enumerated.items():
        record = capture.records.get(key)
        if record is None:
            if capture.status != "COMPLETE":
                kind: DifferenceClass = "CAPTURE_INCOMPLETE"
                detail = "not in the capture, which is not shown complete for the interval"
            elif not filter_includes(capture, payment):
                kind = "SCOPE_INCOMPATIBLE"
                detail = "the capture's filters do not include this operation"
            else:
                kind = "PROVIDER_OMISSION"
                detail = (
                    "in the anchored history, in the capture's scope and filters, and absent "
                    "from a capture shown complete for the interval"
                )
            differences.append(Difference(kind, key, payment.ledger, detail))
        elif fields := _record_differs(payment, record):
            differences.append(
                Difference(
                    "PROVIDER_CONTRADICTION",
                    key,
                    payment.ledger,
                    "the capture differs from the anchored operation in " + ", ".join(fields),
                )
            )
        held = stored.get(key, [])
        if not held:
            if record is not None:
                differences.append(
                    Difference(
                        "EXTRACTION_GAP",
                        key,
                        payment.ledger,
                        "listed by the provider, not in the store",
                    )
                )
            continue
        problems = []
        for observation in held:
            chain = _chain(observation)
            if chain.tx_successful != payment.tx_successful:
                problems.append(f"{observation.observation_id}: success")
            moved = observation.payload
            if isinstance(moved, TokenMovementPayload):
                if str(moved.units.atoms) != payment.amount or moved.units.scale != 7:
                    problems.append(f"{observation.observation_id}: amount")
                if (moved.from_address, moved.to_address) != (payment.source, payment.destination):
                    problems.append(f"{observation.observation_id}: parties")
        if problems:
            differences.append(
                Difference(
                    "ADAPTER_CONTRADICTION",
                    key,
                    payment.ledger,
                    "the store differs from the anchored operation: " + "; ".join(problems),
                )
            )
            continue
        representation = sorted({o.fact_type for o in held})
        entry = {"key": list(key), "observations": [o.observation_id for o in held]}
        if payment.tx_successful:
            adapter["matched"].append({**entry, "fact_types": representation})
        else:
            # A failed attempt: any representation that keeps the transaction as failed.
            adapter["failed_attempts"].append({**entry, "fact_types": representation})
    for key, record in sorted(capture.records.items()):
        if key in enumerated:
            continue
        if _in_scope(record, target):
            differences.append(
                Difference(
                    "PROVIDER_CONTRADICTION",
                    key,
                    record.ledger,
                    "the provider lists an in-scope payment the anchored history does not hold",
                )
            )
        else:
            differences.append(
                Difference(
                    "OUT_OF_SCOPE", key, record.ledger, f"provider record of type {record.type}"
                )
            )
    for key, held in sorted(stored.items()):
        if key in enumerated:
            continue
        # The adapter recorded a Classic payment of the account that the anchored history
        # does not hold in scope.
        differences.append(
            Difference(
                "ADAPTER_CONTRADICTION",
                key,
                _chain(held[0]).ledger,
                "the store holds a Classic payment record the anchored history does not have "
                "in scope",
            )
        )
    kinds = {d.kind for d in differences}
    status: CompletenessStatus
    if kinds & {"PROVIDER_CONTRADICTION", "ADAPTER_CONTRADICTION"}:
        status = "CONTRADICTED"
    elif kinds & {"PROVIDER_OMISSION", "EXTRACTION_GAP"}:
        status = "GAPS_FOUND"
    elif kinds & {"CAPTURE_INCOMPLETE", "SCOPE_INCOMPATIBLE"} or capture.status != "COMPLETE":
        status = "INCOMPLETE"
    else:
        status = "COMPLETE_IN_SCOPE"
    return CompletenessReport(status, enumeration, capture, adapter, differences)


def completeness_section(
    report: CompletenessReport,
    evidence: CheckpointEvidence,
    target: ChainTarget,
    start: int,
    end: int,
) -> dict[str, Any]:
    anchor = evidence.anchor
    proven = None
    if report.status == "COMPLETE_IN_SCOPE":
        proven = (
            f"no Classic payment of {target.asset_code}:{target.asset_issuer} with "
            f"{target.account} as source or destination, successful or failed, in ledgers "
            f"{start}-{end} besides the {len(report.enumeration.payments)} enumerated; this "
            "concerns these payment operations only, not all the account's movements or "
            "balance changes"
        )
    return {
        "kind": "classic_payment_completeness",
        "status": report.status,
        "scope": {
            "network": target.network,
            "asset": {"code": target.asset_code, "issuer": target.asset_issuer},
            "account": target.account,
            "ledgers": [start, end],
            "bounds": "both included",
            "operation_types": ["payment"],
            "account_roles": ["source (explicit or the transaction's)", "destination"],
            "includes": [
                "muxed (M) addresses, decoded to their base only to enumerate: no identity is "
                "attributed and no IdentityLink changes",
                "inner transactions of fee bumps (keyed by the outer hash, as the network "
                "names them)",
                "successful and failed transactions",
            ],
            "counted": "once per (transaction hash, operation index); never matched by amount",
            "excluded": EXCLUDED,
        },
        "enumeration": {
            "source": "transaction and result sets verified against the anchor",
            "status": report.enumeration.status,
            "ledgers_checked": [
                report.enumeration.ledgers_checked[0],
                report.enumeration.ledgers_checked[-1],
            ]
            if report.enumeration.ledgers_checked
            else [],
            "payments": [asdict(p) for p in report.enumeration.payments],
            "problems": report.enumeration.problems,
        },
        "provider_capture": {
            "status": report.capture.status,
            "filters": report.capture.filters,
            "pages": report.capture.pages,
            "end_evidence": report.capture.end_evidence,
            "records_in_interval": len(report.capture.records),
            "problems": report.capture.problems,
        },
        "adapter_records": report.adapter,
        "differences": [asdict(d) for d in report.differences],
        "proven_absence": proven,
        "not_proven": NOT_PROVEN,
        "trust": {
            "anchor_mode": None if anchor is None else anchor.mode,
            "operator": "on testnet SDF operates the validators, the history archives and "
            "Horizon: the anchored history is another system than the provider's database, "
            "not another operator",
        },
        "coverage_effect": "none: no certificate, profile, coverage_level or financial result "
        "changes; provider_claimed stays for the current evaluations",
    }
