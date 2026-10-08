"""Stellar adapter: Horizon Classic payments and RPC SAC events -> canonical observations.

Read-only. Both paths emit the same economic-effect key ``<tx_hash>:<op_index>:<ordinal>``
under the target's single source id, so a Classic payment seen by Horizon and its unified
SAC event seen by RPC integrate as one effect (DUPLICATE) or a visible SOURCE_CONFLICT.

Path payments are kept as ``chain_effect`` observations, never as movements: the
Classic debit/credit of the target asset (executed amounts only) and, separately, the SAC
legs of the same operation. Their correspondence is checked per account net, never by
index or ordinal.

DEX fills are kept apart too. A SAC movement whose operation Horizon did not list among
the account's payments is classified from the operation Horizon reports: an offer
operation, or a third party's path payment, gives ``dex_fill`` effects (the SAC event and
the account's Classic trade effects); a payment or contract call stays a movement; any
other origin, or none found, gives ``unresolved_movement``. That absence from the payments
listing is a scope of the endpoint, never evidence that nothing moved.

Every chain effect is quarantined with the addresses it involves, so the coverage lets a
profile tell which operations it can affect (``CoverageCertificate.quarantined_records``).

Muxed accounts and memos. A Classic payment keeps the
sub-account ids of a muxed sender or receiver and the transaction's memo, exactly. A SAC
event never names a muxed address in its topics; its ``to_muxed_id`` datum is the
receiver's sub-account id or, when the receiver is not muxed, the memo (CAP-67), so the
datum alone cannot tell a muxed id from a memo id. It is checked against the Classic record
of the same effect, which corroborates it (the event then adds no record) or contradicts it
(quarantined, nothing chosen). Without a Classic record a u64 datum stays unresolved.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import re
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import ValidationError

from invaria.contracts.chain import ChainTarget, ExecutionLink, IngestionCheckpoint, OrdinalState
from invaria.contracts.coverage import (
    ChainCoverageScope,
    CoverageCertificate,
    QuarantineNature,
    certificate_sha256,
)
from invaria.contracts.observation import (
    ChainEffectPayload,
    Observation,
    TokenMovementPayload,
    TransactionMemo,
    effect_parties,
    movement_addresses,
)
from invaria.contracts.quantity import Quantity
from invaria.contracts.stellar import (
    NETWORK_PASSPHRASES,
    claimable_balance_hex,
    decode_muxed_account,
    encode_muxed_account,
    parse_u64,
)
from invaria.ingest.journal import observation_content
from invaria.stellar.sources import DataUnavailable, Horizon, Page, Rpc
from invaria.stellar.store import IngestStore
from invaria.stellar.xdr import (
    ScAddress,
    XdrError,
    decode_scval,
    sac_contract_id,
    string_bytes,
)

HORIZON_PARSER = "stellar-horizon-payments-parser@1.0.0"
# 1.1.0: a payment keeps its muxed sender/receiver ids and the
# transaction's memo instead of being excluded (muxed) or losing the memo.
HORIZON_MAPPING = "stellar-classic-payment@1.1.0"
# 1.1.0 (increment 4): the transaction outcome keeps the memo; a muxed end is excluded with
# its M address and id instead of losing them.
HORIZON_PATH_MAPPING = "stellar-classic-path-effect@1.1.0"
RPC_PARSER = "stellar-rpc-events-parser@1.0.0"
# 1.1.0 (increment 4): a muxed party in the topics is decoded (MUXED_ACCOUNT, no longer
# UNSUPPORTED_PARTY), a u64 datum names its candidate sub-account, and an event with a
# Classic payment record is checked against it instead of producing its own copy.
RPC_MAPPING = "stellar-sac-movement@1.1.0"
# 1.1.0: a leg's liquidity pool or claimable balance counterparty is
# decoded as a typed party instead of leaving the leg unsupported.
# 1.2.0 (increment 4): a u64 ``to_muxed_id`` on the leg to the destination is accepted when
# the Classic operation shows it is the memo id (receiver not muxed); otherwise unsupported.
RPC_PATH_MAPPING = "stellar-sac-path-leg@1.2.0"
HORIZON_EFFECTS_PARSER = "stellar-horizon-effects-parser@1.0.0"
HORIZON_FILL_MAPPING = "stellar-classic-dex-fill@1.0.0"
RPC_FILL_MAPPING = "stellar-sac-dex-fill@1.0.0"
RPC_UNRESOLVED_MAPPING = "stellar-sac-unresolved-movement@1.0.0"
# 1.1.0 (increment 3): a holder that is not an account (claimable balance, contract) is a
# typed ``holder`` instead of an unsupported event.
RPC_CLAWBACK_MAPPING = "stellar-sac-clawback@1.1.0"
RPC_CONTRACT_MAPPING = "stellar-sac-contract-transfer@1.0.0"
RPC_POOL_MAPPING = "stellar-sac-pool-transfer@1.0.0"
RPC_BALANCE_MAPPING = "stellar-sac-claimable-balance@1.0.0"
HORIZON_BALANCE_PARSER = "stellar-horizon-claimable-balance-parser@1.0.0"
HORIZON_OPERATIONS_PARSER = "stellar-horizon-operations-parser@1.0.0"
HORIZON_CLAWBACK_MAPPING = "stellar-classic-clawback@1.0.0"
# Every mapping this adapter writes observations with: a profile admits them
# explicitly for its chain source (fund-subscription-testnet@1.4.0), or a new evaluation
# refuses the evidence they produced.
MAPPING_REFS: frozenset[str] = frozenset(
    {
        HORIZON_MAPPING,
        HORIZON_PATH_MAPPING,
        RPC_MAPPING,
        RPC_PATH_MAPPING,
        HORIZON_FILL_MAPPING,
        RPC_FILL_MAPPING,
        RPC_UNRESOLVED_MAPPING,
        RPC_CLAWBACK_MAPPING,
        RPC_CONTRACT_MAPPING,
        RPC_POOL_MAPPING,
        RPC_BALANCE_MAPPING,
        HORIZON_CLAWBACK_MAPPING,
    }
)
PATH_PAYMENTS = frozenset({"path_payment_strict_send", "path_payment_strict_receive"})
DEX_OPERATIONS = frozenset(
    {"manage_sell_offer", "manage_buy_offer", "create_passive_sell_offer", *PATH_PAYMENTS}
)
# Operation types whose SAC events stay movements, as in the first adapter release.
# Unlisted, they are kept as movements but quarantined: Horizon should have listed them for
# the account.
MOVEMENT_OPERATIONS = frozenset({"payment", "invoke_host_function"})
EFFECTS_LIMIT = 200
# Correspondence statuses that leave nothing unresolved; any other one is quarantined.
CORRESPONDENCE_RESOLVED = frozenset({"corroborated", "failed_no_effect"})
RPC_SCANNED_CURSOR = re.compile(r"^(\d{19})-\d{10}$")
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
    "EFFECT_NOT_A_MOVEMENT",
    "PATH_INTERMEDIATE_ONLY",
    "CORRESPONDENCE_CONFLICT",
    "UNRESOLVED_ORIGIN",
    "NOT_LISTED_BY_HORIZON",
    "CLAWBACK_CLAIMABLE_BALANCE",  # increment 2 only; kept to read recorded exclusions
    "CLAWBACK_CLASSIC_UNAVAILABLE",
    "CLAWBACK_CLASSIC_CONTRADICTS",
    "CLAWBACK_SAC_NATIVE",
    "UNSUCCESSFUL_EVENT",
]
# Why a SAC clawback has no Classic counterpart: the correspondence status it gives.
CLAWBACK_ORIGIN_STATUS = {
    "CLAWBACK_SAC_NATIVE": "sac_native",
    "CLAWBACK_CLASSIC_CONTRADICTS": "classic_contradicts",
    "CLAWBACK_CLASSIC_UNAVAILABLE": "classic_unavailable",
}
# Reasons that do not by themselves leave an operation's evidence incomplete. The notes on
# a clawback's missing Classic side describe the correspondence status itself
# (``CLAWBACK_ORIGIN_STATUS``); an ExecutionLink that quarantines them must not turn that
# status into ``sac_incomplete``.
NOT_INCOMPLETE = frozenset(
    {"EFFECT_NOT_A_MOVEMENT", "CORRESPONDENCE_CONFLICT", *CLAWBACK_ORIGIN_STATUS}
)


@dataclass(frozen=True)
class Exclusion:
    """A record of the target asset involving the account that is not a movement.

    ``quarantine`` (the default) counts it against the coverage of ``token_movement``, so
    the profiles cannot treat the view as complete for the operations it can affect. A
    note (``quarantine=False``) records a declared limitation that removes nothing.

    ``addresses`` are the ledger parties of the record; None means unknown, and then the
    record affects every operation. ``chain_op`` ("<tx_hash>:<op_index>") locates it on the
    ledger when known. ``target``/``page`` are stamped when it is persisted.
    """

    path: str
    locator: str
    reason: ExclusionReason
    detail: str
    quarantine: bool = True
    operation: str | None = None  # operation_key() of the operation, when known
    addresses: tuple[str, ...] | None = None
    chain_op: str | None = None
    target: str | None = None
    page: str | None = None
    # What the evidence establishes about the record (``QuarantineNature``); None
    # is "unresolved". Set where the producer knows more: an identified clawback, a
    # contradiction on it, or an identified movement no profile admits.
    nature: QuarantineNature | None = None

    def as_dict(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "path": self.path,
            "locator": self.locator,
            "reason": self.reason,
            "detail": self.detail,
            "quarantine": self.quarantine,
            "addresses": None if self.addresses is None else sorted(set(self.addresses)),
            "chain_op": self.chain_op,
            "target_id": self.target,
            "page": self.page,
        }
        if self.operation is not None:
            entry["operation"] = self.operation
        if self.nature is not None:
            entry["nature"] = self.nature
        return entry


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
    extra: Mapping[str, Any] | None = None,
) -> Observation:
    """A ``token_movement``. ``extra`` holds what only the Classic payment states: the muxed
    sub-account ids and the transaction memo (mapping 1.1.0)."""
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
                    **(extra or {}),
                },
                "synthetic": target.synthetic,
            },
            strict=False,
        ).model_dump_json()
    )


def _effect_observation(
    target: ChainTarget,
    *,
    record_key: str,
    payload: dict[str, Any],
    valid_time: str,
    page: Page,
    locator: str,
    parser: str,
    mapping: str,
    recorded_at: datetime,
) -> Observation:
    """A ``chain_effect``: never read as a movement nor admitted as evidence of fulfilment
    by the current profiles. The adapter assigns no ``operation_ref``; the coordinates and
    provenance it keeps are what a future explicit association would need."""
    identity = f"{target.source_id}|{record_key}|1|{page.sha256}|{locator}"
    return Observation.model_validate_json(
        Observation.model_validate(
            {
                "schema_version": "1.0",
                "observation_id": "obs-" + hashlib.sha256(identity.encode()).hexdigest()[:32],
                "tenant_id": target.tenant_id,
                "kind": "assertion",
                "fact_type": "chain_effect",
                "instrument_id": target.instrument_id,
                "representation_id": target.representation_id,
                "operation_ref": None,
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
                "payload": {"payload_type": "chain_effect", **payload},
                "synthetic": target.synthetic,
            },
            strict=False,
        ).model_dump_json()
    )


# ------------------------------------------------------------ Horizon (Classic)


def _horizon_asset(record: Mapping[str, Any], prefix: str = "") -> str:
    kind = str(record[f"{prefix}asset_type"])
    if kind == "native":
        return "native"
    if not kind.startswith("credit_alphanum"):
        raise ValueError(f"unsupported asset type {kind}")
    return f"{record[f'{prefix}asset_code']}:{record[f'{prefix}asset_issuer']}"


def _operation_identity(record: Mapping[str, Any]) -> tuple[int, int, str, bool]:
    """(ledger, op index, tx hash, successful), cross-checked with the joined transaction."""
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
    return ledger, op_index, tx_hash, successful


def transaction_memo(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The memo of the record's joined transaction, exactly: text as its raw bytes
    (Horizon's ``memo_bytes``, base64; a text memo may be empty or not UTF-8), id as the
    decimal u64, hash and return as the 32 bytes in hex. None when it is not read exactly
    (no joined transaction, an unknown type, a value it cannot decode, a text memo with
    ``memo`` but no ``memo_bytes``): unknown, never "no memo", and never a reason to drop the
    payment or its path payment index entry (Stellar review H3). Horizon omits empty
    ``memo``/``memo_bytes``, so a text memo with neither is the empty text memo."""
    try:
        return _transaction_memo(record)
    except (ValueError, TypeError):
        return None


def _transaction_memo(record: Mapping[str, Any]) -> dict[str, Any] | None:
    transaction = record.get("transaction")
    if not transaction:
        return None
    if "memo_type" not in transaction:
        return None  # the joined transaction does not say: unknown, never "no memo"
    kind = str(transaction["memo_type"])
    value: str | None
    if kind == "none":
        if transaction.get("memo") not in (None, ""):
            raise ValueError("memo_type none with a memo value")
        value = None
    elif kind == "text":
        raw = transaction.get("memo_bytes")
        if raw is None and transaction.get("memo") in (None, ""):
            raw = ""  # Horizon omits both for an empty text memo
        if not isinstance(raw, str):
            raise ValueError("text memo without memo_bytes: its exact bytes are unknown")
        value = base64.b64encode(base64.b64decode(raw, validate=True)).decode("ascii")
    elif kind == "id":
        value = str(parse_u64(str(transaction.get("memo"))))
    elif kind in ("hash", "return"):
        data = base64.b64decode(str(transaction.get("memo")), validate=True)
        if len(data) != 32:
            raise ValueError(f"a {kind} memo has 32 bytes")
        value = data.hex()
    else:
        raise ValueError(f"unknown memo_type {kind!r}")
    memo = TransactionMemo.model_validate({"memo_type": kind, "value": value})
    return memo.model_dump(mode="json")


def muxed_ids(record: Mapping[str, Any], prefix: str = "") -> dict[str, str | None]:
    """The sub-account ids of a Classic record's muxed sender and receiver, checked against
    the M addresses Horizon gives (``from_muxed``/``to_muxed``, or ``source_account_muxed``):
    the M address must decode to the record's base account and to the same id. Raises on
    any disagreement; a side that is not muxed gives None (an id of 0 is muxed)."""
    found: dict[str, str | None] = {}
    for side, base_key in (("from", "from"), ("to", "to")):
        address = record.get(f"{side}_muxed")
        stated = record.get(f"{side}_muxed_id")
        if not address:
            if stated not in (None, ""):
                raise ValueError(f"{side}_muxed_id without {side}_muxed")
            found[side] = None
            continue
        base, muxed_id = decode_muxed_account(str(address))
        if base != str(record[base_key]):
            raise ValueError(f"{side}_muxed names another base account than {side}")
        if stated is not None and parse_u64(str(stated)) != muxed_id:
            raise ValueError(f"{side}_muxed_id disagrees with {side}_muxed")
        found[side] = str(muxed_id)
    return found


def _muxed_addresses(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Base and M addresses of a Classic record's ends (the M ones only when they decode)."""
    found = [str(record["from"]), str(record["to"])]
    for side in ("from", "to"):
        address = record.get(f"{side}_muxed")
        if address:
            try:
                decode_muxed_account(str(address))
            except ValueError:
                continue
            found.append(str(address))
    return tuple(found)


def _stroops(text: str, unit: str, *, positive: bool) -> Quantity:
    units = Quantity.from_decimal_text(text, scale=SCALE, unit=unit)
    if int(units.atoms) < (1 if positive else 0):
        raise ValueError(f"amount {text!r} is not {'positive' if positive else 'non-negative'}")
    return units


def _fee_stroops(value: Any) -> str:
    text = str(value)
    if not text.isdigit():
        raise ValueError(f"fee_charged {text!r} is not a non-negative integer of stroops")
    return str(int(text))


def path_operation_entry(target: ChainTarget, record: Mapping[str, Any]) -> dict[str, Any]:
    """Index entry of a Classic path payment, used to classify its SAC events.

    Executed amounts are kept only for a successful operation; sendMax/destMin are never
    read. Raises on malformed records (the caller excludes them as MALFORMED).
    """
    ledger, op_index, tx_hash, successful = _operation_identity(record)
    watched = f"{target.asset_code}:{target.asset_issuer}"
    source_asset = _horizon_asset(record, "source_")
    destination_asset = _horizon_asset(record)
    path = [_horizon_asset(hop) for hop in record["path"]]
    source_amount = destination_amount = None
    if successful:
        source_amount = _stroops(str(record["source_amount"]), "ASSET", positive=True).atoms
        destination_amount = _stroops(str(record["amount"]), "ASSET", positive=True).atoms
    return {
        "target_id": target.target_id,
        "watched_asset": watched,
        "tx_hash": tx_hash,
        "op_index": op_index,
        "ledger": ledger,
        "operation_type": str(record["type"]),
        "successful": successful,
        "source_account": str(record["from"]),
        "destination_account": str(record["to"]),
        "source_asset": source_asset,
        "destination_asset": destination_asset,
        "source_amount": source_amount,
        "destination_amount": destination_amount,
        "path": path,
        # Increment 4: what the SAC legs' ``to_muxed_id`` datum is checked against.
        "destination_muxed_id": muxed_ids(record)["to"],
        "source_muxed_id": muxed_ids(record)["from"],
        "memo": transaction_memo(record),
        "source_is_target": source_asset == watched,
        "destination_is_target": destination_asset == watched,
        "intermediate_only": watched in path and watched not in (source_asset, destination_asset),
    }


def _classic_path_effects(
    target: ChainTarget,
    record: Mapping[str, Any],
    page: Page,
    locator: str,
    recorded_at: datetime,
) -> list[Observation | Exclusion]:
    entry = path_operation_entry(target, record)
    kind = entry["operation_type"]
    if entry["intermediate_only"]:
        return [
            Exclusion(
                "horizon_payments",
                locator,
                "PATH_INTERMEDIATE_ONLY",
                f"{kind}: the target asset is only an intermediate hop; it is not attributed "
                f"to {entry['source_account']} or {entry['destination_account']}",
                quarantine=False,
                addresses=(entry["source_account"], entry["destination_account"]),
            )
        ]
    if not (entry["source_is_target"] or entry["destination_is_target"]):
        return []
    if entry["source_muxed_id"] is not None or entry["destination_muxed_id"] is not None:
        # A path payment is a chain effect, never evidence of fulfilment; with a muxed end it
        # stays excluded, now with its M addresses and ids.
        return [
            Exclusion(
                "horizon_payments",
                locator,
                "MUXED_ACCOUNT",
                f"{kind} with a muxed end (sender sub-account {entry['source_muxed_id']}, "
                f"receiver sub-account {entry['destination_muxed_id']}): kept excluded; the "
                "sub-account holder is not known from the id",
                addresses=_muxed_addresses(record),
            )
        ]
    transaction = record.get("transaction")
    if not transaction:
        # The fee and technical result must be kept (failed operations included).
        raise ValueError("path payment without its joined transaction")
    outcome = {
        "fee_account": str(transaction["fee_account"]),
        # Horizon reports fee_charged as an integer number of stroops, not XLM text.
        "fee_charged": Quantity(
            atoms=_fee_stroops(transaction["fee_charged"]), scale=SCALE, unit="XLM"
        ),
        "result_xdr": transaction.get("result_xdr"),
        "memo": entry["memo"],
    }
    context = {
        key: entry[key]
        for key in (
            "source_account",
            "destination_account",
            "source_asset",
            "destination_asset",
            "source_amount",
            "destination_amount",
            "path",
        )
    }
    context["operation_type"] = kind
    chain = {
        "network": target.network,
        "ledger": entry["ledger"],
        "tx_hash": entry["tx_hash"],
        "operation_index": entry["op_index"],
        "tx_successful": entry["successful"],
    }
    prefix = f"{entry['tx_hash']}:{entry['op_index']}:classic"
    common = {
        "representation": "classic",
        "chain": chain,
        "path_payment": context,
        "transaction": outcome,
    }

    def effect(key: str, payload: dict[str, Any]) -> Observation:
        return _effect_observation(
            target,
            record_key=f"{prefix}:{key}",
            payload={**common, **payload},
            valid_time=str(record["created_at"]),
            page=page,
            locator=locator,
            parser=HORIZON_PARSER,
            mapping=HORIZON_PATH_MAPPING,
            recorded_at=recorded_at,
        )

    source = {"kind": "account", "id": entry["source_account"]}
    destination = {"kind": "account", "id": entry["destination_account"]}
    if not entry["successful"]:
        # A failed operation moves nothing: its technical result and fee are kept, no amount.
        return [
            effect(
                "failed",
                {
                    "effect_kind": "path_payment_failed",
                    "account": entry["source_account"],
                    "direction": None,
                    "counterparty": destination,
                    "units": None,
                },
            )
        ]
    results: list[Observation | Exclusion] = []
    if entry["source_is_target"]:
        results.append(
            effect(
                "debit",
                {
                    "effect_kind": "path_payment_debit",
                    "account": entry["source_account"],
                    "direction": "debit",
                    "counterparty": destination,
                    "units": Quantity(atoms=entry["source_amount"], scale=SCALE, unit=target.unit),
                },
            )
        )
    if entry["destination_is_target"]:
        results.append(
            effect(
                "credit",
                {
                    "effect_kind": "path_payment_credit",
                    "account": entry["destination_account"],
                    "direction": "credit",
                    "counterparty": source,
                    "units": Quantity(
                        atoms=entry["destination_amount"], scale=SCALE, unit=target.unit
                    ),
                },
            )
        )
    results.append(
        Exclusion(
            "horizon_payments",
            locator,
            "EFFECT_NOT_A_MOVEMENT",
            f"{kind} moves the target asset; kept as chain_effect, not interpreted as a "
            f"payment, delivery or redemption by any current profile",
            operation=operation_key(entry),
            addresses=(entry["source_account"], entry["destination_account"]),
        )
    )
    return results


def normalize_horizon_record(
    target: ChainTarget,
    record: Mapping[str, Any],
    page: Page,
    index: int,
    recorded_at: datetime,
    links: Mapping[tuple[str, str, int, int], str],
) -> list[Observation | Exclusion]:
    """Empty = outside the target scope (other asset, native-only operation)."""
    locator = f"{page.locator}#/_embedded/records/{index}"
    try:
        results = _normalize_horizon(target, record, page, locator, recorded_at, links)
        chain_op = f"{record['transaction_hash']}:{toid_parts(int(str(record['paging_token'])))[2]}"
        return [
            r if isinstance(r, Observation) else dataclasses.replace(r, chain_op=chain_op)
            for r in results
        ]
    except (KeyError, ValueError, TypeError, ValidationError) as error:
        return [
            Exclusion(
                "horizon_payments",
                locator,
                "MALFORMED",
                f"{type(error).__name__}: {str(error)[:200]}",
            )
        ]


def _normalize_horizon(
    target: ChainTarget,
    record: Mapping[str, Any],
    page: Page,
    locator: str,
    recorded_at: datetime,
    links: Mapping[tuple[str, str, int, int], str],
) -> list[Observation | Exclusion]:
    kind = str(record.get("type"))

    def is_target(prefix: str = "") -> bool:
        return (
            str(record.get(f"{prefix}asset_type", "")).startswith("credit_alphanum")
            and record.get(f"{prefix}asset_code") == target.asset_code
            and record.get(f"{prefix}asset_issuer") == target.asset_issuer
        )

    if kind in PATH_PAYMENTS:
        return _classic_path_effects(target, record, page, locator, recorded_at)
    if kind == "invoke_host_function":
        changes = record.get("asset_balance_changes") or []
        matching = [
            c
            for c in changes
            if c.get("asset_code") == target.asset_code
            and c.get("asset_issuer") == target.asset_issuer
        ]
        if matching:
            # mint has no ``from`` and burn/clawback no ``to``: that side is the issuer.
            issuer_side = {"mint": "from", "burn": "to", "clawback": "to"}
            parties = [
                str(c.get(side) or target.asset_issuer)
                for c in matching
                for side in ("from", "to")
                if c.get(side) or issuer_side.get(str(c.get("type"))) == side
            ]
            known = all(
                (c.get("from") or issuer_side.get(str(c.get("type"))) == "from")
                and (c.get("to") or issuer_side.get(str(c.get("type"))) == "to")
                for c in matching
            )
            # Horizon's own reading of the call: when every change of the target asset is a
            # clawback with both sides known, the record is that clawback (its SAC event
            # says the same on the RPC path); anything else stays unresolved.
            clawback_only = known and all(c.get("type") == "clawback" for c in matching)
            return [
                Exclusion(
                    "horizon_payments",
                    locator,
                    "UNSUPPORTED_OPERATION",
                    "SAC effect inside invoke_host_function: use the RPC path",
                    addresses=tuple(parties) if known else None,
                    nature="clawback_identified" if clawback_only else None,
                )
            ]
        return []
    if kind != "payment" or not is_target():
        return []
    ledger, op_index, tx_hash, successful = _operation_identity(record)
    units = _stroops(str(record["amount"]), target.unit, positive=False)
    muxed = muxed_ids(record)
    extra: dict[str, Any] = {"memo": transaction_memo(record)}
    if muxed["from"] is not None:
        extra["from_muxed_id"] = muxed["from"]
    if muxed["to"] is not None:
        extra["to_muxed_id"] = muxed["to"]
    return [
        _observation(
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
            extra=extra,
        )
    ]


# -------------------------------------------------------------- RPC (SAC events)


def _decode_topic(b64: str) -> Any:
    try:
        return decode_scval(b64)
    except XdrError as error:
        return error


def _event_addresses(target: ChainTarget, event: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Ledger parties of a movement event as typed strkeys (G, C, B, L; the issuer for mint,
    burn and clawback); None when one of them cannot be decoded."""
    topics = [_decode_topic(t) for t in event.get("topic", [])]
    if not topics or topics[0] not in MOVEMENT_TOPICS:
        return None
    parties = topics[1:-1]
    if not parties or any(not isinstance(p, ScAddress) for p in parties):
        return None
    found = tuple(p.strkey for p in parties if isinstance(p, ScAddress))
    return found if topics[0] == "transfer" else (*found, target.asset_issuer)


def sac_datum(event: Mapping[str, Any]) -> tuple[str, str] | None:
    """The ``to_muxed_id`` of a movement event as (type, value): ``u64`` (decimal),
    ``string`` (its UTF-8 bytes in base64) or ``bytes`` (hex); None for a plain amount."""
    value = _decode_topic(str(event.get("value", "")))
    if not isinstance(value, dict) or "to_muxed_id" not in value:
        return None
    datum = value["to_muxed_id"]
    if isinstance(datum, bool):
        raise ValueError("to_muxed_id is not a u64, string or bytes")
    if isinstance(datum, int):
        return "u64", str(datum)
    if isinstance(datum, str):
        return "string", base64.b64encode(string_bytes(datum)).decode("ascii")
    if isinstance(datum, bytes):
        return "bytes", datum.hex()
    raise ValueError("to_muxed_id is not a u64, string or bytes")


def expected_datum(head: str, payload: TokenMovementPayload) -> tuple[str, str] | str | None:
    """What CAP-67 puts in ``to_muxed_id`` for the effect a Classic payment describes: the
    receiver's sub-account id when it is muxed, otherwise the memo (id as u64, text as
    string, hash and return as bytes, none as no datum); a burn never carries one. "unknown"
    when the memo was not read and the receiver is not muxed."""
    if head == "burn":
        return None
    if payload.to_muxed_id is not None:
        return "u64", payload.to_muxed_id
    memo = payload.memo
    if memo is None:
        return "unknown"
    if memo.memo_type == "none" or memo.value is None:
        return None
    if memo.memo_type == "id":
        return "u64", memo.value
    if memo.memo_type == "text":
        return "string", memo.value
    return "bytes", memo.value


def corroborate_movement(
    target: ChainTarget,
    contract_id: str,
    event: Mapping[str, Any],
    ordinal: int,
    page: Page,
    recorded_at: datetime,
    links: Mapping[tuple[str, str, int, int], str],
    classic: Observation,
) -> list[Observation | Exclusion] | None:
    """A SAC movement event whose effect has a Classic payment record.

    The event states less than the Classic record (no muxed sender, no memo when the
    receiver is muxed): it is compared on what it can state, never completed with what it
    does not. Its amount, parties and execution must equal the Classic record's, and its
    ``to_muxed_id`` must be what CAP-67 derives from that record. Then it corroborates the
    record, which is offered again and counted as a duplicate: no observation is built from
    the event. Its own reading otherwise: a different amount or party becomes a visible
    source conflict as before; an equal one with another datum is quarantined as a
    contradiction (neither side chosen). None when the event is not a plain account movement
    (handled as before).
    """
    base = _normalize_sac_event(
        target, contract_id, event, ordinal, page, recorded_at, links, ignore_datum=True
    )
    assert isinstance(classic.payload, TokenMovementPayload)
    if not isinstance(base, Observation) or base.fact_type != "token_movement":
        return None
    stated = classic.payload.model_copy(
        update={"from_muxed_id": None, "to_muxed_id": None, "memo": None}
    )
    same = observation_content(base) == observation_content(
        classic.model_copy(update={"payload": stated})
    )
    head = _decode_topic(event.get("topic", [""])[0])
    locator = f"{page.locator}#{event.get('id')}"
    try:
        datum = sac_datum(event)
    except ValueError as error:
        return [Exclusion("rpc_sac_events", locator, "UNSUPPORTED_VALUE", str(error))]
    payload = classic.payload
    chain = payload.chain
    operation = f"{chain.tx_hash}:{chain.operation_index}@{target.asset_code}:{target.asset_issuer}"
    candidates = _candidate_sub_accounts(target, event)

    def conflict(detail: str) -> list[Observation | Exclusion]:
        # Both readings are named: the Classic record's parties (any M included) and the M
        # address the event's u64 would name (Stellar review H2); neither is chosen.
        return [
            Exclusion(
                "rpc_sac_events",
                locator,
                "CORRESPONDENCE_CONFLICT",
                detail + "; neither side is chosen",
                operation=operation,
                addresses=tuple(sorted({*movement_addresses(payload), *candidates})),
                nature="contradictory",
            )
        ]

    if not same:
        if datum is not None and datum[0] == "u64":
            # Its own reading would say "receiver not muxed", which the event does not state
            # (Stellar review H4): quarantined instead of a copy without the datum.
            return conflict(
                f"amount, parties or execution differ from the Classic payment, and the event "
                f"carries the u64 to_muxed_id {datum[1]}"
            )
        return [base]
    expected = expected_datum(str(head), classic.payload)
    if expected == "unknown":
        # The memo was not read. The Classic record says whether the receiver is muxed
        # (Horizon gives ``to_muxed`` whatever the join), so the datum can only be the memo:
        # identity is resolved and only the memo value goes unchecked (review H6).
        return [classic]
    if datum != expected:
        return conflict(
            f"to_muxed_id {datum} where the Classic payment gives {expected} (CAP-67: the "
            "receiver's sub-account id, else the memo)"
        )
    return [classic]


@dataclass(frozen=True)
class PendingBalanceEffect:
    """A SAC event on a claimable balance whose effect needs the balance's history (its
    creator, claimants and the Classic operation) before it can be recorded."""

    payload: Mapping[str, Any]
    record_key: str
    balance_id: str  # B strkey
    expected_operation: str
    valid_time: str
    locator: str
    mapping: str


def normalize_sac_event(
    target: ChainTarget,
    contract_id: str,
    event: Mapping[str, Any],
    ordinal: int,
    page: Page,
    recorded_at: datetime,
    links: Mapping[tuple[str, str, int, int], str],
) -> Observation | Exclusion | PendingBalanceEffect | None:
    result = _normalize_sac_event(target, contract_id, event, ordinal, page, recorded_at, links)
    if isinstance(result, Exclusion) and result.reason != "MALFORMED":
        addresses = _event_addresses(target, event)
        if addresses is not None and result.reason == "MUXED_ACCOUNT":
            addresses = (*addresses, *_candidate_sub_accounts(target, event))
        return dataclasses.replace(result, addresses=addresses)
    return result


def _receiver(target: ChainTarget, event: Mapping[str, Any]) -> str | None:
    """The base account a movement event credits (transfer: the second party; mint: the
    only one), or None."""
    topics = [_decode_topic(t) for t in event.get("topic", [])]
    head = topics[0] if topics else None
    side = 2 if head == "transfer" else 1 if head == "mint" else None
    if side is None or len(topics) <= side:
        return None
    party = topics[side]
    return party.strkey if isinstance(party, ScAddress) and party.kind == "account" else None


def _candidate_sub_accounts(target: ChainTarget, event: Mapping[str, Any]) -> tuple[str, ...]:
    """The sub-account a u64 ``to_muxed_id`` would name if it is not a memo id: the M
    address of the receiving base account and that id. A candidate the record must name so
    that an account approved by that M address is never ruled out (Stellar review H1); it is
    never an attribution."""
    try:
        datum = sac_datum(event)
    except ValueError:
        return ()
    receiver = _receiver(target, event)
    if datum is None or datum[0] != "u64" or receiver is None:
        return ()
    return (encode_muxed_account(receiver, int(datum[1])),)


def _normalize_sac_event(
    target: ChainTarget,
    contract_id: str,
    event: Mapping[str, Any],
    ordinal: int,
    page: Page,
    recorded_at: datetime,
    links: Mapping[tuple[str, str, int, int], str],
    *,
    ignore_datum: bool = False,
) -> Observation | Exclusion | PendingBalanceEffect | None:
    """``ignore_datum``: the ``to_muxed_id`` datum is checked by the caller against the
    Classic record of the same effect (``corroborate_movement``)."""
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
    if any(isinstance(p, ScAddress) and p.kind == "muxed_account" for p in parties):
        # CAP-67 never puts a muxed address in the topics (it splits it into the base
        # address and ``to_muxed_id``): an event that does is not one this mapping reads.
        return Exclusion(
            "rpc_sac_events",
            locator,
            "MUXED_ACCOUNT",
            "muxed account party (ScAddress type 2) in the topics, which CAP-67 never emits; "
            "decoded, kept unresolved, never attributed",
        )
    if any(isinstance(p, XdrError) for p in parties):
        return Exclusion(
            "rpc_sac_events", locator, "UNSUPPORTED_PARTY", "party address cannot be decoded"
        )
    value = _decode_topic(str(event.get("value", "")))
    if head == "clawback" and isinstance(value, dict):
        return Exclusion(
            "rpc_sac_events",
            locator,
            "UNSUPPORTED_VALUE",
            "a CAP-67 clawback carries a plain i128 amount, not a map",
        )
    if isinstance(value, dict):
        muxed = value.get("to_muxed_id")
        if set(value) - {"amount", "to_muxed_id"}:
            return Exclusion(
                "rpc_sac_events",
                locator,
                "UNSUPPORTED_VALUE",
                f"unexpected movement data keys {sorted(value)}",
            )
        if isinstance(muxed, int) and not ignore_datum:
            return Exclusion(
                "rpc_sac_events",
                locator,
                "MUXED_ACCOUNT",
                f"to_muxed_id is the u64 {muxed}: the receiver's sub-account id or the memo id, "
                "which the event alone cannot tell apart, and no Classic record of the effect "
                "says which; never attributed",
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
    if head == "clawback":
        if value <= 0:
            return Exclusion(
                "rpc_sac_events", locator, "UNSUPPORTED_VALUE", "clawback amount is not positive"
            )
        if not bool(event.get("inSuccessfulContractCall", False)):
            return Exclusion(
                "rpc_sac_events",
                locator,
                "UNSUCCESSFUL_EVENT",
                "clawback event of an unsuccessful call: no withdrawal was executed",
            )
        holder = parties[0]
        assert isinstance(holder, ScAddress)
        if holder.kind == "liquidity_pool":
            return Exclusion(
                "rpc_sac_events",
                locator,
                "UNSUPPORTED_PARTY",
                "a clawback whose holder is a liquidity pool: not a holder the protocol "
                "claws back from; kept unsupported, never attributed to its providers",
            )
        try:
            effect = _sac_clawback(target, event, ordinal, holder, value, page, recorded_at)
        except (KeyError, ValueError, TypeError, ValidationError) as error:
            return Exclusion(
                "rpc_sac_events",
                locator,
                "MALFORMED",
                f"{type(error).__name__}: {str(error)[:200]}",
            )
        return effect
    if any(isinstance(p, ScAddress) and p.kind != "account" for p in parties):
        try:
            return _typed_movement(target, event, ordinal, head, parties, value, page, recorded_at)
        except (KeyError, ValueError, TypeError, ValidationError) as error:
            return Exclusion(
                "rpc_sac_events",
                locator,
                "MALFORMED",
                f"{type(error).__name__}: {str(error)[:200]}",
            )
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


def _sac_clawback(
    target: ChainTarget,
    event: Mapping[str, Any],
    ordinal: int,
    holder: ScAddress,
    atoms: int,
    page: Page,
    recorded_at: datetime,
) -> Observation | PendingBalanceEffect:
    """The CAP-67 ``clawback`` event: ``atoms`` left ``holder`` under the asset's clawback
    authority. Canonical (the same for every target of the asset), so two targets never
    count it twice. A holder that is not an account is kept as a typed ``holder``; one that
    is a claimable balance waits for the balance's history."""
    tx_hash, op_index = str(event["txHash"]), int(event["operationIndex"])
    side: dict[str, Any] = (
        {"account": holder.strkey}
        if holder.kind == "account"
        else {"account": None, "holder": {"kind": holder.kind, "id": holder.strkey}}
    )
    record_key = f"{tx_hash}:{op_index}:sac:{ordinal}:clawback"
    if holder.kind == "claimable_balance":
        return PendingBalanceEffect(
            payload={**_clawback_payload(target, event, atoms), **side},
            record_key=record_key,
            balance_id=holder.strkey,
            expected_operation="clawback_claimable_balance",
            valid_time=str(event["ledgerClosedAt"]),
            locator=f"{page.locator}#{event.get('id')}",
            mapping=RPC_CLAWBACK_MAPPING,
        )
    return _effect_observation(
        target,
        record_key=record_key,
        payload={**_clawback_payload(target, event, atoms), **side},
        valid_time=str(event["ledgerClosedAt"]),
        page=page,
        locator=f"{page.locator}#{event.get('id')}",
        parser=RPC_PARSER,
        mapping=RPC_CLAWBACK_MAPPING,
        recorded_at=recorded_at,
    )


def _clawback_payload(target: ChainTarget, event: Mapping[str, Any], atoms: int) -> dict[str, Any]:
    tx_hash, op_index = str(event["txHash"]), int(event["operationIndex"])
    return {
        "effect_kind": "clawback",
        "representation": "sac",
        "direction": "debit",
        "counterparty": {"kind": "account", "id": target.asset_issuer},
        "units": Quantity(atoms=str(atoms), scale=SCALE, unit=target.unit),
        "chain": {
            "network": target.network,
            "ledger": int(event["ledger"]),
            "tx_hash": tx_hash,
            "operation_index": op_index,
            "tx_successful": bool(event.get("inSuccessfulContractCall", False)),
        },
        "path_payment": None,
        "transaction": None,
        "clawback": {
            "asset": f"{target.asset_code}:{target.asset_issuer}",
            "issuer": target.asset_issuer,
            "operation_type": None,
            "operation_source": None,
        },
    }


TYPED_KIND = {"contract": "contract_transfer", "liquidity_pool": "pool_transfer"}


def _typed_movement(
    target: ChainTarget,
    event: Mapping[str, Any],
    ordinal: int,
    head: str,
    parties: Sequence[Any],
    atoms: int,
    page: Page,
    recorded_at: datetime,
) -> Observation | Exclusion | PendingBalanceEffect:
    """A SAC movement between an account and a typed party (contract, liquidity pool or
    claimable balance), from the account's side: canonical, so every target of the asset
    records the same effect. The typed party is never read as an account, nor its owner
    inferred; the effect satisfies nothing."""
    locator = f"{page.locator}#{event.get('id')}"
    issuer = ScAddress("account", target.asset_issuer)
    sender, receiver = {
        "transfer": (parties[0], parties[-1]),
        "mint": (issuer, parties[0]),
        "burn": (parties[0], issuer),
    }[head]
    if sender.kind == "account" and receiver.kind != "account":
        account, typed, direction = sender, receiver, "debit"
    elif receiver.kind == "account" and sender.kind != "account":
        account, typed, direction = receiver, sender, "credit"
    else:
        return Exclusion(
            "rpc_sac_events", locator, "CONTRACT_PARTY", "movement between two non-account parties"
        )
    if not bool(event.get("inSuccessfulContractCall", False)):
        return Exclusion(
            "rpc_sac_events",
            locator,
            "UNSUCCESSFUL_EVENT",
            f"{head} event of an unsuccessful call: nothing moved",
        )
    if atoms <= 0:
        return Exclusion(
            "rpc_sac_events", locator, "UNSUPPORTED_VALUE", "movement amount is not positive"
        )
    tx_hash, op_index = str(event["txHash"]), int(event["operationIndex"])
    if typed.kind == "claimable_balance":
        kind = "claimable_balance_created" if direction == "debit" else "claimable_balance_claimed"
    else:
        kind = TYPED_KIND[typed.kind]
    payload = {
        "effect_kind": kind,
        "representation": "sac",
        "account": account.strkey,
        "direction": direction,
        "counterparty": {"kind": typed.kind, "id": typed.strkey},
        "units": Quantity(atoms=str(atoms), scale=SCALE, unit=target.unit),
        "chain": {
            "network": target.network,
            "ledger": int(event["ledger"]),
            "tx_hash": tx_hash,
            "operation_index": op_index,
            "tx_successful": True,
        },
        "path_payment": None,
        "transaction": None,
    }
    record_key = f"{tx_hash}:{op_index}:sac:{ordinal}:{kind}"
    if typed.kind == "claimable_balance":
        return PendingBalanceEffect(
            payload=payload,
            record_key=record_key,
            balance_id=typed.strkey,
            expected_operation="create_claimable_balance"
            if direction == "debit"
            else "claim_claimable_balance",
            valid_time=str(event["ledgerClosedAt"]),
            locator=locator,
            mapping=RPC_BALANCE_MAPPING,
        )
    return _effect_observation(
        target,
        record_key=record_key,
        payload=payload,
        valid_time=str(event["ledgerClosedAt"]),
        page=page,
        locator=locator,
        parser=RPC_PARSER,
        mapping=RPC_CONTRACT_MAPPING if typed.kind == "contract" else RPC_POOL_MAPPING,
        recorded_at=recorded_at,
    )


def classic_clawback(
    target: ChainTarget,
    record: Mapping[str, Any],
    page: Page,
    locator: str,
    recorded_at: datetime,
    tx_hash: str | None = None,
) -> Observation | None:
    """A Horizon ``clawback`` operation of the target asset, as an executed effect. A
    failed operation withdrew nothing: no effect (its absence is noted by the caller).

    ``tx_hash`` is the hash the operation was looked up by (the SAC event's). Observed on
    testnet (``usdc-feebump-payment``): RPC events, Horizon payments and Horizon operations
    all name a fee bump by its outer hash, even when the operation is looked up by the
    inner one. A record naming another hash is never paired with the event: the identity
    of the transaction is then not resolved (ValueError)."""
    if tx_hash is not None and str(record["transaction_hash"]) != tx_hash:
        raise ValueError("the operation names another transaction hash than the one looked up")
    tx_hash = str(record["transaction_hash"])
    if str(record.get("type")) != "clawback" or not bool(record["transaction_successful"]):
        return None
    asset = _horizon_asset(record)
    if asset != f"{target.asset_code}:{target.asset_issuer}":
        return None
    ledger, _order, op_index = toid_parts(int(str(record["paging_token"])))
    return _effect_observation(
        target,
        record_key=f"{tx_hash}:{op_index}:classic:clawback",
        payload={
            "effect_kind": "clawback",
            "representation": "classic",
            "account": str(record["from"]),
            "direction": "debit",
            "counterparty": {"kind": "account", "id": target.asset_issuer},
            "units": _stroops(str(record["amount"]), target.unit, positive=True),
            "chain": {
                "network": target.network,
                "ledger": ledger,
                "tx_hash": tx_hash,
                "operation_index": op_index,
                "tx_successful": True,
            },
            "path_payment": None,
            "transaction": None,
            "clawback": {
                "asset": asset,
                "issuer": target.asset_issuer,
                "operation_type": "clawback",
                "operation_source": str(record["source_account"]),
            },
        },
        valid_time=str(record["created_at"]),
        page=page,
        locator=locator,
        parser=HORIZON_OPERATIONS_PARSER,
        mapping=HORIZON_CLAWBACK_MAPPING,
        recorded_at=recorded_at,
    )


def _clawback_origin(
    target: ChainTarget,
    horizon: Horizon,
    tx_hash: str,
    op_index: int,
    ledger: int,
    recorded_at: datetime,
) -> tuple[tuple[Page, ...], tuple[Observation, ...], tuple[ExclusionReason, str] | None]:
    """(pages read, Classic clawback effects, why there is none). Horizon does not list a
    Classic clawback among the payments of the affected account, so the operation is read.
    Why there is none: a clawback native to the SAC (a contract call), a Classic operation
    that contradicts the event (failed, another asset or type), or nothing to read."""
    unavailable: ExclusionReason = "CLAWBACK_CLASSIC_UNAVAILABLE"
    contradicts: ExclusionReason = "CLAWBACK_CLASSIC_CONTRADICTS"
    try:
        page = horizon.transaction_operations(tx_hash)
    except DataUnavailable:
        return (), (), (unavailable, "Horizon does not find the transaction")
    _refuse_synthetic(target, page)
    try:
        records = page.document.get("_embedded", {}).get("records", [])
        found = [
            (index, r)
            for index, r in enumerate(records)
            if toid_parts(int(str(r["paging_token"])))[::2] == (ledger, op_index)
        ]
        if len(found) != 1:
            return (page,), (), (unavailable, "Horizon does not report the operation")
        index, record = found[0]
        if str(record["transaction_hash"]) != tx_hash:
            return (
                (page,),
                (),
                (
                    unavailable,
                    "Horizon names the operation's transaction by another hash; the "
                    "transaction's identity is not resolved and no correspondence is made",
                ),
            )
        kind = str(record["type"])
        if kind == "invoke_host_function":
            return (
                (page,),
                (),
                (
                    "CLAWBACK_SAC_NATIVE",
                    "a clawback by contract call (SAC admin): there is no Classic clawback",
                ),
            )
        if kind != "clawback":
            return (page,), (), (contradicts, f"the operation is {kind}, not a Classic clawback")
        if not bool(record["transaction_successful"]):
            return (page,), (), (contradicts, "Horizon reports the clawback operation as failed")
        effect = classic_clawback(
            target,
            record,
            page,
            f"{page.locator}#/_embedded/records/{index}",
            recorded_at,
            tx_hash=tx_hash,
        )
    except (KeyError, ValueError, TypeError, ValidationError) as error:
        return (page,), (), (unavailable, f"unreadable operation ({type(error).__name__})")
    if effect is None:
        return (page,), (), (contradicts, "the Classic clawback is of another asset")
    return (page,), (effect,), None


def clawback_correspondence(
    target: ChainTarget,
    operation: tuple[str, int, int],
    classic: Sequence[Observation],
    sac: Sequence[Observation],
    incomplete: bool,
    without_classic: str = "classic_unavailable",
) -> dict[str, Any]:
    """Classic clawback operation vs SAC ``clawback`` events of one operation, per affected
    account (debits only). Without a Classic side the status says why
    (``CLAWBACK_ORIGIN_STATUS``): ``sac_native``, ``classic_contradicts`` or
    ``classic_unavailable``; none of them is resolved. Agreement is an aggregate
    reconciliation within that scope; the two effects are kept, never merged, and nothing
    is deduplicated because sums agree."""
    tx_hash, op_index, ledger = operation
    totals: dict[str, dict[str, int]] = {"classic": {}, "sac": {}}
    for side, effects in (("classic", classic), ("sac", sac)):
        for effect in effects:
            payload = effect.payload
            assert isinstance(payload, ChainEffectPayload) and payload.units is not None
            holder = _affected(payload)
            totals[side][holder] = totals[side].get(holder, 0) - int(payload.units.atoms)
    if incomplete:
        status = "sac_incomplete"
    elif not classic:
        status = without_classic
    elif totals["classic"] != totals["sac"]:
        status = "conflict"
    else:
        status = "corroborated"
    evidence = sorted(o.observation_id for o in (*classic, *sac))
    return {
        "target_id": target.target_id,
        "kind": "clawback",
        "tx_hash": tx_hash,
        "op_index": op_index,
        "ledger": ledger,
        "asset": f"{target.asset_code}:{target.asset_issuer}",
        "basis": "debit_per_affected_account",
        "claim": "aggregate_reconciliation",
        "status": status,
        "classic_net": {a: str(v) for a, v in sorted(totals["classic"].items())},
        "sac_net": {a: str(v) for a, v in sorted(totals["sac"].items())},
        "classic_effects": sorted(o.source.record_key for o in classic),
        "sac_legs": sorted(o.source.record_key for o in sac),
        "evidence_sha256": hashlib.sha256(json.dumps([evidence, incomplete]).encode()).hexdigest(),
    }


def _affected(payload: ChainEffectPayload) -> str:
    """The party whose balance an effect changes: its account, or its typed holder."""
    if payload.account is not None:
        return payload.account
    assert payload.holder is not None and payload.holder.id is not None
    return payload.holder.id


def _leg_party(party: Any, issuer: str) -> dict[str, str] | None:
    # A muxed address in the topics (never emitted under CAP-67) is no typed party.
    if isinstance(party, ScAddress) and party.kind != "muxed_account":
        return {"kind": party.kind, "id": party.strkey}
    if party == "issuer":
        return {"kind": "account", "id": issuer}
    return None


def normalize_sac_leg(
    target: ChainTarget,
    event: Mapping[str, Any],
    ordinal: int,
    operation: Mapping[str, Any],
    page: Page,
    recorded_at: datetime,
) -> list[Observation | Exclusion]:
    """A SAC event of a Classic path payment (from the Horizon index): a leg, not a movement.

    Every movement event of the operation is kept, whoever its parties are, so the
    correspondence can sum it. ``account`` is the account side of the leg (the watched
    account when it is a party); mint comes from and burn goes to the issuer.
    """
    results = _sac_leg(target, event, ordinal, operation, page, recorded_at)
    key = operation_key(operation)
    addresses = _event_addresses(target, event)
    return [
        r
        if isinstance(r, Observation)
        else dataclasses.replace(
            r, operation=key, addresses=None if r.reason == "MALFORMED" else addresses
        )
        for r in results
    ]


def _sac_leg(
    target: ChainTarget,
    event: Mapping[str, Any],
    ordinal: int,
    operation: Mapping[str, Any],
    page: Page,
    recorded_at: datetime,
) -> list[Observation | Exclusion]:
    locator = f"{page.locator}#{event.get('id')}"
    topics = [_decode_topic(t) for t in event.get("topic", [])]
    head = topics[0]
    expected_asset = f"{target.asset_code}:{target.asset_issuer}"
    shape = {"transfer": 4, "mint": 3, "burn": 3, "clawback": 3}[head]
    if len(topics) != shape or topics[-1] != expected_asset:
        return [
            Exclusion(
                "rpc_sac_events",
                locator,
                "MALFORMED",
                f"unexpected topic shape or asset for {head}",
            )
        ]
    if head == "clawback":
        return [
            Exclusion(
                "rpc_sac_events", locator, "UNSUPPORTED_EVENT", "clawback is not supported yet"
            )
        ]
    parties = topics[1:-1]
    sides: tuple[Any, Any]
    if head == "transfer":
        sides = (parties[0], parties[1])
    elif head == "mint":
        sides = ("issuer", parties[0])
    else:
        sides = (parties[0], "issuer")
    sender = _leg_party(sides[0], target.asset_issuer)
    receiver = _leg_party(sides[1], target.asset_issuer)
    if sender is None or receiver is None:
        return [
            Exclusion(
                "rpc_sac_events",
                locator,
                "UNSUPPORTED_PARTY",
                "path payment leg with a party that cannot be decoded (a muxed account or an "
                "unknown address type); the operation's correspondence stays incomplete",
            )
        ]
    value = _decode_topic(str(event.get("value", "")))
    if isinstance(value, dict):
        datum = value.get("to_muxed_id")
        # A u64 is accepted only on the leg to the destination, when the Classic operation
        # shows the receiver is not muxed and its memo id is that value: it is then the
        # memo (CAP-67), never a sub-account.
        memo = operation.get("memo") or {}
        memo_id = (
            isinstance(datum, int)
            and not isinstance(datum, bool)
            and operation.get("destination_muxed_id") is None
            and memo.get("memo_type") == "id"
            and memo.get("value") == str(datum)
            and receiver.get("id") == operation["destination_account"]
        )
        if set(value) - {"amount", "to_muxed_id"} or (isinstance(datum, int) and not memo_id):
            return [
                Exclusion(
                    "rpc_sac_events",
                    locator,
                    "UNSUPPORTED_VALUE",
                    f"path payment leg data {sorted(value)} not supported (a u64 to_muxed_id "
                    "that the Classic operation does not show to be its memo id)",
                )
            ]
        value = value.get("amount")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return [
            Exclusion(
                "rpc_sac_events", locator, "UNSUPPORTED_VALUE", "leg amount is not a positive i128"
            )
        ]
    # Canonical, target-independent: the sender's side, or the receiver's when the sender
    # is not an account. Two targets of the same asset then record identical legs.
    if sender["kind"] == "account":
        account, direction, counterparty = sender, "debit", receiver
    else:
        account, direction, counterparty = receiver, "credit", sender
    if account["kind"] != "account":
        return [
            Exclusion(
                "rpc_sac_events",
                locator,
                "CONTRACT_PARTY",
                "path payment leg between two non-account parties",
            )
        ]
    tx_hash, op_index = str(event["txHash"]), int(event["operationIndex"])
    if (tx_hash, op_index) != (operation["tx_hash"], operation["op_index"]):
        raise ValueError("leg does not belong to the indexed operation")
    leg = _effect_observation(
        target,
        record_key=f"{tx_hash}:{op_index}:sac:{ordinal}",
        payload={
            "effect_kind": "path_payment_leg",
            "representation": "sac",
            "account": account["id"],
            "direction": direction,
            "counterparty": counterparty,
            "units": Quantity(atoms=str(value), scale=SCALE, unit=target.unit),
            "chain": {
                "network": target.network,
                "ledger": int(event["ledger"]),
                "tx_hash": tx_hash,
                "operation_index": op_index,
                "tx_successful": bool(event.get("inSuccessfulContractCall", False)),
            },
            "path_payment": None,
            "transaction": None,
        },
        valid_time=str(event["ledgerClosedAt"]),
        page=page,
        locator=locator,
        parser=RPC_PARSER,
        mapping=RPC_PATH_MAPPING,
        recorded_at=recorded_at,
    )
    results: list[Observation | Exclusion] = [leg]
    if ordinal == 0:
        intermediate = bool(operation["intermediate_only"])
        results.append(
            Exclusion(
                "rpc_sac_events",
                locator,
                "PATH_INTERMEDIATE_ONLY" if intermediate else "EFFECT_NOT_A_MOVEMENT",
                f"SAC legs of {operation['operation_type']} {tx_hash}:{op_index}; kept as "
                f"chain_effect, not as token movements",
                quarantine=not intermediate,
            )
        )
    return results


def operation_key(operation: Mapping[str, Any]) -> str:
    """Identity of a path operation for one watched asset (never the tx/op hash alone)."""
    return f"{operation['tx_hash']}:{operation['op_index']}@{operation['watched_asset']}"


def path_correspondence(
    operation: Mapping[str, Any], legs: Sequence[Observation], incomplete: bool
) -> dict[str, Any]:
    """Classic vs SAC effect on the operation's source and destination accounts.

    Compared per account net (credits minus debits), never by index or ordinal: SAC may
    split one Classic movement into several legs (e.g. a mint then a transfer). When the
    source is also the destination, the gross debit and credit are compared too (legs
    from the account to itself excluded), so a net cannot hide different amounts.
    Conversion counterparties (offer owners, pools) are not reconciled.

    "corroborated" is an aggregate reconciliation within the compared scope (one network,
    transaction, operation, asset and these accounts). It does not prove that each Classic
    effect is a given SAC leg, and nothing is deduplicated because sums agree.
    """
    source, destination = operation["source_account"], operation["destination_account"]
    accounts = sorted({source, destination})
    classic = dict.fromkeys(accounts, 0)
    classic_gross = [0, 0]  # [debit, credit] of the account when source == destination
    if operation["successful"]:
        if operation["source_is_target"]:
            classic[source] -= int(operation["source_amount"])
            classic_gross[0] = int(operation["source_amount"])
        if operation["destination_is_target"]:
            classic[destination] += int(operation["destination_amount"])
            classic_gross[1] = int(operation["destination_amount"])
    sac = dict.fromkeys(accounts, 0)
    sac_gross = [0, 0]
    for leg in legs:
        payload = leg.payload
        assert isinstance(payload, ChainEffectPayload) and payload.units is not None
        assert payload.counterparty is not None
        amount = int(payload.units.atoms)
        sign = 1 if payload.direction == "credit" else -1
        if payload.account in sac:
            sac[payload.account] += sign * amount
        if payload.counterparty.id in sac:
            sac[payload.counterparty.id] -= sign * amount
        if source == destination and payload.counterparty.id != payload.account:
            if source == payload.account:
                sac_gross[0 if sign < 0 else 1] += amount
            elif source == payload.counterparty.id:
                sac_gross[1 if sign < 0 else 0] += amount
    if incomplete:
        status = "sac_incomplete"
    elif not operation["successful"]:
        status = "sac_on_failed_operation" if legs else "failed_no_effect"
    elif not legs:
        # Nothing on the SAC side: never "corroborated", even when Classic nets to zero.
        status = "classic_only" if any(classic.values()) else "no_sac_evidence"
    elif classic != sac or (source == destination and classic_gross != sac_gross):
        status = "conflict"
    else:
        status = "corroborated"
    evidence = sorted(leg.observation_id for leg in legs)
    record: dict[str, Any] = {
        "target_id": operation["target_id"],
        "kind": "path_payment",
        "tx_hash": operation["tx_hash"],
        "op_index": operation["op_index"],
        "ledger": operation["ledger"],
        "asset": operation["watched_asset"],
        "basis": "net_and_gross_per_account" if source == destination else "net_per_account",
        "claim": "aggregate_reconciliation",
        "status": status,
        "classic_net": {a: str(v) for a, v in classic.items()},
        "sac_net": {a: str(v) for a, v in sac.items()},
        "sac_legs": sorted(leg.source.record_key for leg in legs),
        "evidence_sha256": hashlib.sha256(json.dumps([evidence, incomplete]).encode()).hexdigest(),
    }
    if source == destination:
        record["classic_gross"] = {"debit": str(classic_gross[0]), "credit": str(classic_gross[1])}
        record["sac_gross"] = {"debit": str(sac_gross[0]), "credit": str(sac_gross[1])}
    return record


def fill_correspondence(
    target: ChainTarget,
    operation: tuple[str, int, int],
    classic: Sequence[Observation],
    sac: Sequence[Observation],
    incomplete: bool,
) -> dict[str, Any]:
    """Classic trade effects vs SAC fill events of one DEX operation, on the watched account.

    Net and gross (debit, credit) of the target asset, never by index or ordinal. As for
    path payments, agreement is an aggregate reconciliation within that scope, not a
    pairing of individual effects; nothing is deduplicated because sums agree.
    """
    tx_hash, op_index, ledger = operation
    totals = {"classic": [0, 0], "sac": [0, 0]}  # [debit, credit] of target.account
    for side, effects in (("classic", classic), ("sac", sac)):
        for effect in effects:
            payload = effect.payload
            assert isinstance(payload, ChainEffectPayload) and payload.units is not None
            totals[side][0 if payload.direction == "debit" else 1] += int(payload.units.atoms)
    net = {side: credit - debit for side, (debit, credit) in totals.items()}
    if incomplete:
        status = "sac_incomplete"
    elif not classic:
        status = "sac_only"  # e.g. a pool trade or a trade Horizon did not report
    elif totals["classic"] != totals["sac"]:
        status = "conflict"
    else:
        status = "corroborated"
    evidence = sorted(o.observation_id for o in (*classic, *sac))
    return {
        "target_id": target.target_id,
        "kind": "dex_fill",
        "tx_hash": tx_hash,
        "op_index": op_index,
        "ledger": ledger,
        "asset": f"{target.asset_code}:{target.asset_issuer}",
        "basis": "net_and_gross_of_watched_account",
        "claim": "aggregate_reconciliation",
        "status": status,
        "classic_net": {target.account: str(net["classic"])},
        "sac_net": {target.account: str(net["sac"])},
        "classic_gross": {"debit": str(totals["classic"][0]), "credit": str(totals["classic"][1])},
        "sac_gross": {"debit": str(totals["sac"][0]), "credit": str(totals["sac"][1])},
        "classic_effects": sorted(o.source.record_key for o in classic),
        "sac_legs": sorted(o.source.record_key for o in sac),
        "evidence_sha256": hashlib.sha256(json.dumps([evidence, incomplete]).encode()).hexdigest(),
    }


# ----------------------------------------------------------- DEX fills (unlisted)


@dataclass(frozen=True)
class Origin:
    """What Horizon reports for the operation of an unlisted SAC movement."""

    kind: Literal["dex", "movement", "unresolved"]
    pages: tuple[Page, ...]
    operation: Mapping[str, Any] | None = None
    reason: str | None = None  # UnresolvedOrigin.reason
    classic: tuple[Observation, ...] = ()


def _operation_origin(
    target: ChainTarget,
    horizon: Horizon,
    tx_hash: str,
    op_index: int,
    ledger: int,
    recorded_at: datetime,
) -> Origin:
    try:
        return _resolve_origin(target, horizon, tx_hash, op_index, ledger, recorded_at)
    except (KeyError, ValueError, TypeError, ValidationError):
        # A response of unexpected shape: the origin stays unresolved for this event, it
        # does not block the range (and nothing it would have yielded is kept).
        return Origin("unresolved", (), reason="incomplete_trade_data")


def _resolve_origin(
    target: ChainTarget,
    horizon: Horizon,
    tx_hash: str,
    op_index: int,
    ledger: int,
    recorded_at: datetime,
) -> Origin:
    try:
        page = horizon.transaction_operations(tx_hash)
    except DataUnavailable:
        return Origin("unresolved", (), reason="operation_not_found")
    _refuse_synthetic(target, page)
    found = [
        r
        for r in page.document.get("_embedded", {}).get("records", [])
        if toid_parts(int(str(r["paging_token"])))[::2] == (ledger, op_index)
        # Another hash than the event's: the transaction's identity is not resolved.
        and str(r["transaction_hash"]) == tx_hash
    ]
    if len(found) != 1:
        return Origin("unresolved", (page,), reason="operation_not_found")
    operation = found[0]
    kind = str(operation["type"])
    if kind in MOVEMENT_OPERATIONS:
        return Origin("movement", (page,), operation)
    if kind not in DEX_OPERATIONS:
        return Origin("unresolved", (page,), operation, "unsupported_operation_type")
    if kind in PATH_PAYMENTS and target.account in (operation.get("from"), operation.get("to")):
        # Horizon should have listed it among the account's payments: sources disagree.
        return Origin("unresolved", (page,), operation, "inconsistent_listing")
    try:
        effects = horizon.operation_effects(str(operation["id"]), EFFECTS_LIMIT)
    except DataUnavailable:
        return Origin("unresolved", (page,), operation, "incomplete_trade_data")
    _refuse_synthetic(target, effects)
    records = effects.document.get("_embedded", {}).get("records", [])
    if len(records) >= EFFECTS_LIMIT:
        return Origin("unresolved", (page, effects), operation, "incomplete_trade_data")
    classic = tuple(
        _classic_fill(target, operation, record, effects, index, tx_hash, ledger, recorded_at)
        for index, record in enumerate(records)
        if record.get("type") == "trade"
        and record.get("account") == target.account
        and f"{target.asset_code}:{target.asset_issuer}"
        in (_horizon_asset(record, "sold_"), _horizon_asset(record, "bought_"))
    )
    if not classic:
        # A DEX operation type alone is not evidence of a fill of this account (e.g. a pool
        # trade, or a trade Horizon does not report): the semantics stay unresolved.
        return Origin("unresolved", (page, effects), operation, "incomplete_trade_data")
    return Origin("dex", (page, effects), operation, classic=classic)


def _classic_fill(
    target: ChainTarget,
    operation: Mapping[str, Any],
    record: Mapping[str, Any],
    page: Page,
    index: int,
    tx_hash: str,
    ledger: int,
    recorded_at: datetime,
) -> Observation:
    """One Horizon trade effect of the watched account in the target asset."""
    watched = f"{target.asset_code}:{target.asset_issuer}"
    sold, bought = _horizon_asset(record, "sold_"), _horizon_asset(record, "bought_")
    sold_atoms = _stroops(str(record["sold_amount"]), "ASSET", positive=True).atoms
    bought_atoms = _stroops(str(record["bought_amount"]), "ASSET", positive=True).atoms
    op_index = toid_parts(int(str(operation["paging_token"])))[2]
    effect_index = int(str(record["id"]).rsplit("-", 1)[1])
    return _effect_observation(
        target,
        record_key=f"{tx_hash}:{op_index}:classic:fill:{effect_index}",
        payload={
            "effect_kind": "dex_fill",
            "representation": "classic",
            "account": target.account,
            "direction": "debit" if sold == watched else "credit",
            "counterparty": {"kind": "account", "id": str(record["seller"])},
            "units": Quantity(
                atoms=sold_atoms if sold == watched else bought_atoms, scale=SCALE, unit=target.unit
            ),
            "chain": {
                "network": target.network,
                "ledger": ledger,
                "tx_hash": tx_hash,
                "operation_index": op_index,
                "tx_successful": True,
            },
            "path_payment": None,
            "transaction": None,
            "exchange": {
                "operation_type": str(operation["type"]),
                "operation_source": str(operation["source_account"]),
                "offer_id": str(record["offer_id"]),
                "sold_asset": sold,
                "sold_amount": sold_atoms,
                "bought_asset": bought,
                "bought_amount": bought_atoms,
            },
        },
        valid_time=str(record["created_at"]),
        page=page,
        locator=f"{page.locator}#/_embedded/records/{index}",
        parser=HORIZON_EFFECTS_PARSER,
        mapping=HORIZON_FILL_MAPPING,
        recorded_at=recorded_at,
    )


def _unlisted_effect(
    target: ChainTarget,
    movement: Observation,
    origin: Origin,
    ordinal: int,
    page: Page,
) -> list[Observation | Exclusion]:
    """The SAC side of a movement whose operation Horizon did not list for the account."""
    payload = movement.payload
    assert isinstance(payload, TokenMovementPayload)
    chain = payload.chain
    key = f"{chain.tx_hash}:{chain.operation_index}"
    operation_ref = f"{key}@{target.asset_code}:{target.asset_issuer}"
    debit = payload.from_address == target.account
    other = payload.to_address if debit else payload.from_address
    common: dict[str, Any] = {
        "representation": "sac",
        "account": target.account,
        "direction": "debit" if debit else "credit",
        "counterparty": {"kind": "account", "id": other},
        "units": payload.units.model_copy(update={"unit": target.unit}),
        "chain": chain.model_dump(mode="json"),
        "path_payment": None,
        "transaction": None,
    }
    operation = origin.operation or {}
    # The operation's own endpoints are in scope too (e.g. the destination of a third
    # party's path payment that crossed the offer): scope, never attribution.
    addresses = (
        payload.from_address,
        payload.to_address,
        *(str(operation[side]) for side in ("from", "to") if operation.get(side)),
    )
    if origin.kind == "dex":
        common |= {
            "effect_kind": "dex_fill",
            "exchange": {
                "operation_type": str(operation["type"]),
                "operation_source": str(operation["source_account"]),
                "offer_id": None,
                "sold_asset": None,
                "sold_amount": None,
                "bought_asset": None,
                "bought_amount": None,
            },
        }
        reason: ExclusionReason = "EFFECT_NOT_A_MOVEMENT"
        mapping = RPC_FILL_MAPPING
        detail = (
            f"DEX fill ({operation['type']} submitted by {operation['source_account']}); kept "
            f"as chain_effect on {target.account}, not a payment, delivery or redemption"
        )
    else:
        common |= {
            "effect_kind": "unresolved_movement",
            "unresolved": {
                "reason": origin.reason,
                "operation_type": operation.get("type"),
            },
        }
        reason = "UNRESOLVED_ORIGIN"
        mapping = RPC_UNRESOLVED_MAPPING
        detail = (
            f"origin of the movement not established ({origin.reason}); kept with unresolved "
            f"semantics, it satisfies no obligation"
        )
    effect = _effect_observation(
        target,
        # Seen from the watched account: another target of the asset sees the same event
        # from its side, so the key carries the perspective and never collides with it.
        record_key=f"{key}:sac:{ordinal}:{common['effect_kind']}:"
        f"{hashlib.sha256(target.account.encode()).hexdigest()[:12]}",
        payload=common,
        valid_time=movement.valid_time.isoformat(),
        page=page,
        locator=movement.provenance.raw_locator,
        parser=RPC_PARSER,
        mapping=mapping,
        recorded_at=movement.recorded_at,
    )
    exclusion = Exclusion(
        "rpc_sac_events",
        movement.provenance.raw_locator,
        reason,
        detail,
        operation=operation_ref,
        addresses=addresses,
    )
    return [effect, exclusion]


def _clawback_results(
    target: ChainTarget,
    horizon: Horizon,
    effect: Observation,
    key: tuple[str, int],
    ledger: int,
    recorded_at: datetime,
    origins: dict[tuple[str, int], str | None],
    lookups: list[Page],
    observations: list[Observation],
) -> list[Observation | Exclusion]:
    """The SAC clawback, its Classic operation (read once per operation) and the record
    that it is a forced withdrawal, not a movement any profile admits."""
    payload = effect.payload
    assert isinstance(payload, ChainEffectPayload)
    operation_ref = f"{key[0]}:{key[1]}@{target.asset_code}:{target.asset_issuer}"
    holder = _affected(payload)
    addresses = (holder, target.asset_issuer)
    results: list[Observation | Exclusion] = [
        effect,
        Exclusion(
            "rpc_sac_events",
            effect.provenance.raw_locator,
            "EFFECT_NOT_A_MOVEMENT",
            f"clawback from {holder} under the asset's clawback authority: a forced "
            "withdrawal kept as "
            "chain_effect, not a delivery, payment or redemption burn, and by itself no "
            "breach of any operation; the profiles judge its relevance, and only an "
            "unresolved correspondence is quarantined",
            quarantine=False,
            operation=operation_ref,
            addresses=addresses,
            nature="clawback_identified",
        ),
    ]
    if key not in origins:
        pages, classic, why = _clawback_origin(target, horizon, key[0], key[1], ledger, recorded_at)
        origins[key] = None if why is None else why[1]
        lookups.extend(pages)
        observations.extend(classic)
        if why is not None:
            reason, detail = why
            results.append(
                Exclusion(
                    "rpc_sac_events",
                    f"{effect.provenance.raw_locator}#classic",
                    reason,
                    f"no Classic clawback to compare: {detail}; the correspondence is "
                    f"{CLAWBACK_ORIGIN_STATUS[reason]} and quarantined",
                    quarantine=False,
                    operation=operation_ref,
                    addresses=addresses,
                    # The SAC event identifies the clawback; only a Classic operation that
                    # is not one contradicts it.
                    nature="contradictory"
                    if reason == "CLAWBACK_CLASSIC_CONTRADICTS"
                    else "clawback_identified",
                )
            )
    return results


# ------------------------------------------------ typed parties

BALANCE_HISTORY_LIMIT = 200


@dataclass(frozen=True)
class BalanceHistory:
    """What Horizon reports of a claimable balance (``/claimable_balances/{id}/operations``);
    ``records`` None when it could not be read in full."""

    pages: tuple[Page, ...]
    records: tuple[Mapping[str, Any], ...] | None
    why: str | None = None


def _balance_history(target: ChainTarget, horizon: Horizon, balance_id: str) -> BalanceHistory:
    try:
        page = horizon.claimable_balance_operations(
            claimable_balance_hex(balance_id), BALANCE_HISTORY_LIMIT
        )
    except DataUnavailable:
        return BalanceHistory((), None, "Horizon does not find the claimable balance")
    _refuse_synthetic(target, page)
    records = page.document.get("_embedded", {}).get("records", [])
    if not isinstance(records, list) or len(records) >= BALANCE_HISTORY_LIMIT:
        return BalanceHistory((page,), None, "the balance's history is longer than one page")
    return BalanceHistory((page,), tuple(records))


def _balance_check(
    target: ChainTarget,
    pending: PendingBalanceEffect,
    history: BalanceHistory,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """(context, correspondence record, detail) of an effect on a claimable balance.

    The Classic side is the operation of the event in the balance's history; amounts are
    compared per affected party (a balance is created, claimed or clawed back whole).
    Participants are resolved only from a single successful creation of the target asset;
    a claimant is a possible recipient, never a recipient until a claim is observed."""
    payload = pending.payload
    chain = payload["chain"]
    tx_hash, op_index, ledger = chain["tx_hash"], chain["operation_index"], chain["ledger"]
    watched = f"{target.asset_code}:{target.asset_issuer}"
    sac_atoms = int(payload["units"].atoms)
    side = payload["account"] or payload["holder"]["id"]
    sign = 1 if payload["direction"] == "credit" else -1
    creator: dict[str, Any] = {"kind": "unresolved", "reason": "balance history not read"}
    claimants: list[str] | None = None
    operation_type = operation_source = classic_type = None
    classic_net: dict[str, int] = {}
    status, detail = "classic_unavailable", history.why or ""
    if history.records is not None:
        records = history.records
        created = [
            r
            for r in records
            if r.get("type") == "create_claimable_balance" and bool(r.get("transaction_successful"))
        ]
        this = [
            r
            for r in records
            if toid_parts(int(str(r["paging_token"])))[::2] == (ledger, op_index)
            and str(r.get("transaction_hash")) == tx_hash
        ]
        # Horizon reports a claimable balance's asset as "native" or "CODE:ISSUER".
        if len(created) == 1 and str(created[0].get("asset")) == watched:
            creation = created[0]
            creator = {"kind": "account", "id": str(creation["source_account"])}
            claimants = [str(c["destination"]) for c in creation.get("claimants", [])] or None
        elif len(created) == 1:
            creator = {"kind": "unresolved", "reason": "the balance holds another asset"}
        else:
            creator = {"kind": "unresolved", "reason": "no single successful creation"}
        if len(this) != 1:
            status, detail = "classic_unavailable", "the operation is not in the balance's history"
        else:
            operation = this[0]
            classic_type = str(operation.get("type"))
            if classic_type in (
                "create_claimable_balance",
                "claim_claimable_balance",
                "clawback_claimable_balance",
            ):
                operation_type, operation_source = classic_type, str(operation["source_account"])
            named = operation.get("balance_id")
            if classic_type != pending.expected_operation:
                status, detail = "classic_contradicts", f"the operation is {classic_type}"
            elif named is not None and named != claimable_balance_hex(pending.balance_id):
                status, detail = "classic_contradicts", "the operation names another balance"
            elif not bool(operation.get("transaction_successful")):
                status, detail = "classic_contradicts", "Horizon reports the operation as failed"
            elif creator["kind"] == "unresolved":
                status, detail = "classic_unavailable", str(creator["reason"])
            else:
                amount = int(_stroops(str(created[0]["amount"]), target.unit, positive=True).atoms)
                party = {
                    "create_claimable_balance": str(operation["source_account"]),
                    "claim_claimable_balance": str(operation.get("claimant")),
                    "clawback_claimable_balance": pending.balance_id,
                }[classic_type]
                classic_net = {party: sign * amount}
                corroborated = classic_net == {side: sign * sac_atoms}
                status = "corroborated" if corroborated else "conflict"
                detail = "" if corroborated else "party or amount differ"
    resolved = claimants is not None and creator["kind"] == "account"
    if not resolved and status == "corroborated":
        # Agreement on party and amount does not resolve who may claim it.
        status, detail = "classic_unavailable", "participants of the balance not resolved"
    context = {
        "balance_id": pending.balance_id,
        "operation_type": operation_type,
        "operation_source": operation_source,
        "creator": creator,
        "claimants": claimants,
    }
    history_sha = history.pages[0].sha256 if history.pages else None
    record = {
        "target_id": target.target_id,
        "kind": "claimable_balance",
        "tx_hash": tx_hash,
        "op_index": op_index,
        "ledger": ledger,
        "asset": watched,
        "basis": "operation_in_balance_history",
        "claim": "aggregate_reconciliation",
        "status": status,
        "participants": "resolved" if resolved else "unresolved",
        "balance_id": pending.balance_id,
        "expected_operation": pending.expected_operation,
        "classic_operation": classic_type,
        "classic_net": {a: str(v) for a, v in sorted(classic_net.items())},
        "sac_net": {side: str(sign * sac_atoms)},
        "sac_legs": [pending.record_key],
        "history_sha256": history_sha,
        "evidence_sha256": hashlib.sha256(
            json.dumps([pending.record_key, sac_atoms, history_sha, status]).encode()
        ).hexdigest(),
    }
    return context, record, detail


def _balance_results(
    target: ChainTarget,
    horizon: Horizon,
    pending: PendingBalanceEffect,
    page: Page,
    recorded_at: datetime,
    histories: dict[str, BalanceHistory],
    lookups: list[Page],
    checks: list[dict[str, Any]],
) -> list[Observation | Exclusion]:
    """An effect on a claimable balance (creation, claim or clawback), with its participants
    from the balance's history and a correspondence check against the Classic operation.
    The effect itself is a note: only an unresolved check is quarantined, and with unknown
    parties when the participants could not be read."""
    if pending.balance_id not in histories:
        histories[pending.balance_id] = _balance_history(target, horizon, pending.balance_id)
        lookups.extend(histories[pending.balance_id].pages)
    history = histories[pending.balance_id]
    try:
        context, record, detail = _balance_check(target, pending, history)
    except (KeyError, ValueError, TypeError) as error:
        # A history of unexpected shape: the participants stay unresolved for this event.
        unreadable = BalanceHistory(
            history.pages, None, f"unreadable history ({type(error).__name__})"
        )
        context, record, detail = _balance_check(target, pending, unreadable)

    def build(balance: dict[str, Any]) -> Observation:
        return _effect_observation(
            target,
            record_key=pending.record_key,
            payload={**pending.payload, "claimable_balance": balance},
            valid_time=pending.valid_time,
            page=page,
            locator=pending.locator,
            parser=RPC_PARSER,
            mapping=pending.mapping,
            recorded_at=recorded_at,
        )

    try:
        effect = build(context)
    except ValidationError as error:
        # Participants of an unexpected shape (e.g. a claimant that is not an account):
        # they stay unresolved, never a reason to stop the range.
        unreadable = BalanceHistory(
            history.pages, None, f"unreadable history ({type(error).__name__})"
        )
        context, record, detail = _balance_check(target, pending, unreadable)
        effect = build(context)
    checks.append(record)
    payload = effect.payload
    assert isinstance(payload, ChainEffectPayload)
    chain = payload.chain
    parties = [p.id for p in effect_parties(payload) if p.id is not None]
    status = record["status"]
    return [
        effect,
        Exclusion(
            "rpc_sac_events",
            pending.locator,
            "EFFECT_NOT_A_MOVEMENT",
            f"{payload.effect_kind} of {pending.balance_id}: kept as chain_effect, never a "
            f"payment, delivery or redemption; "
            + (
                "the claimant's credit does not make it the owner of the account debited at "
                "creation"
                if payload.effect_kind == "claimable_balance_claimed"
                else "no claimant is presumed to have received it"
            )
            + f"; check against the balance's history: {status}"
            + (f" ({detail})" if detail else ""),
            quarantine=False,
            operation=f"{chain.tx_hash}:{chain.operation_index}@{target.asset_code}:"
            f"{target.asset_issuer}",
            addresses=None if record["participants"] == "unresolved" else tuple(parties),
            nature=_check_nature(record),
        ),
    ]


def _typed_results(
    target: ChainTarget, effect: Observation, listed: bool
) -> list[Observation | Exclusion]:
    """A movement with a contract or a liquidity pool (outside a path payment): the effect,
    and a record of what it is. A contract call Horizon did not list for the account is a
    disagreement of the sources (quarantined); a pool movement is not reconciled with the
    Classic pool operation in this increment (quarantined); a listed contract movement is a
    note (Horizon's own record of the call stays as the Horizon run left it)."""
    payload = effect.payload
    assert isinstance(payload, ChainEffectPayload) and payload.counterparty is not None
    chain = payload.chain
    operation_ref = (
        f"{chain.tx_hash}:{chain.operation_index}@{target.asset_code}:{target.asset_issuer}"
    )
    addresses = (str(payload.account), str(payload.counterparty.id))
    if payload.effect_kind == "pool_transfer":
        reason: ExclusionReason = "EFFECT_NOT_A_MOVEMENT"
        quarantine = True
        detail = (
            f"movement between {payload.account} and liquidity pool {payload.counterparty.id}: "
            "kept as chain_effect; the pool's funds are not attributed to its liquidity "
            "providers, and it is not reconciled with the Classic pool operation"
        )
    elif listed:
        reason, quarantine = "EFFECT_NOT_A_MOVEMENT", False
        detail = (
            f"movement between {payload.account} and contract {payload.counterparty.id}: kept "
            "as chain_effect, never a payment, delivery or redemption; the contract's owner "
            "is not inferred"
        )
    else:
        reason, quarantine = "NOT_LISTED_BY_HORIZON", True
        detail = (
            f"movement between {payload.account} and contract {payload.counterparty.id} is "
            "missing from Horizon's payments of the account; kept as chain_effect and "
            "quarantined"
        )
    return [
        effect,
        Exclusion(
            "rpc_sac_events",
            effect.provenance.raw_locator,
            reason,
            detail,
            quarantine=quarantine,
            operation=operation_ref,
            addresses=addresses,
            # Both parties and the movement are identified; none is admitted as delivery.
            nature="movement_identified",
        ),
    ]


def _not_listed(movement: Observation, origin: Origin) -> Exclusion:
    """A payment or contract call Horizon did not list for the account: still a movement,
    but the two sources disagree on its scope, so it is quarantined."""
    payload = movement.payload
    assert isinstance(payload, TokenMovementPayload)
    kind = (origin.operation or {}).get("type")
    return Exclusion(
        "rpc_sac_events",
        movement.provenance.raw_locator,
        "NOT_LISTED_BY_HORIZON",
        f"{kind} moving the target asset is missing from Horizon's payments of the account; "
        f"kept as a movement and quarantined",
        addresses=(payload.from_address, payload.to_address),
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


def _close_time(
    target: ChainTarget, horizon: Horizon, store: IngestStore, sequence: int
) -> tuple[str, str]:
    page = horizon.ledger(sequence)
    _refuse_synthetic(target, page)
    store.save_page(page.sha256, page.raw)
    return str(page.document["closed_at"]), page.sha256


def _coverage(
    target: ChainTarget,
    horizon: Horizon,
    store: IngestStore,
    checkpoint: IngestionCheckpoint,
    method: str,
    recorded_at: datetime,
    quarantined: list[dict[str, Any]] | None = None,
    links: Sequence[ExecutionLink] = (),
) -> CoverageCertificate:
    start_time, start_sha = _close_time(target, horizon, store, checkpoint.start_ledger)
    end_time, end_sha = _close_time(target, horizon, store, checkpoint.end_ledger + 1)
    ledgers = f"{checkpoint.start_ledger}-{checkpoint.end_ledger}"
    coverage_id = f"cov-{target.target_id}-{checkpoint.path}-{ledgers}"
    chain_scope = _chain_scope(target, checkpoint.path, links)
    return CoverageCertificate.model_validate_json(
        CoverageCertificate.model_validate(
            {
                "schema_version": "1.0",
                "coverage_id": coverage_id,
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
                # Only when something is quarantined, so other certificates stay identical.
                "quarantined_records": quarantined if checkpoint.records_quarantined else None,
                "chain_scope": chain_scope,
                "supersedes": _supersedes(store, coverage_id, chain_scope, checkpoint, recorded_at),
            },
            strict=False,
        ).model_dump_json()
    )


def links_sha256(target: ChainTarget, links: Sequence[ExecutionLink]) -> str:
    """sha256 of the ExecutionLinks a run applied on the target's network, canonical and
    sorted: they decide which notes are quarantined and which records list a link, so a
    certificate replaces only another run with the same links (Stellar review M2)."""
    applied = sorted(
        json.dumps(link.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        for link in links
        if link.network == target.network
    )
    return hashlib.sha256(json.dumps(applied).encode("utf-8")).hexdigest()


def _chain_scope(
    target: ChainTarget, route: str, links: Sequence[ExecutionLink]
) -> ChainCoverageScope:
    """What the certificate read (``not_covered`` by role and route):

    - a holder target sees neither the clawback of a claimable balance it created (the
      clawback names the balance and debits it, not the holder) nor a third party's claim
      of it (a transfer from the balance to the claimant);
    - the issuer target sees the clawback of a claimable balance only through the SAC
      route (Horizon's payments of an account do not list it), and never a third party's
      claim (the issuer is not a party of that transfer).
    """
    holder = target.account != target.asset_issuer
    not_covered = (
        ["claimable_balance_clawback", "claimable_balance_claim"]
        if holder or route == "horizon_payments"
        else ["claimable_balance_claim"]
    )
    return ChainCoverageScope.model_validate(
        {
            "network": target.network,
            "target_id": target.target_id,
            "account": target.account,
            "asset_code": target.asset_code,
            "asset_issuer": target.asset_issuer,
            "route": route,
            "links_sha256": links_sha256(target, links),
            "not_covered": not_covered,
        }
    )


def _supersedes(
    store: IngestStore,
    coverage_id: str,
    scope: ChainCoverageScope,
    checkpoint: IngestionCheckpoint,
    recorded_at: datetime,
) -> list[dict[str, str]] | None:
    """The earlier certificates of this store that this one replaces, declared explicitly
    with their content's sha256: same network, target, account, asset and links; a
    route this one includes; a ledger range inside this one's; recorded before it, or at
    the same instant with a strictly broader route or range. Never one of a broader route or
    range. The engines check each claim again (``supersession_problem``)."""
    replaced = [
        {"coverage_id": c.coverage_id, "sha256": certificate_sha256(c)}
        for c in store.coverage()
        if c.coverage_id != coverage_id
        and c.chain_scope is not None
        and c.ledger_range is not None
        and scope.includes(c.chain_scope)
        and checkpoint.start_ledger <= c.ledger_range.first
        and c.ledger_range.last <= checkpoint.end_ledger
        and (
            c.recorded_at < recorded_at
            or (
                c.recorded_at == recorded_at
                and (
                    c.chain_scope.route != scope.route
                    or (c.ledger_range.first, c.ledger_range.last)
                    != (checkpoint.start_ledger, checkpoint.end_ledger)
                )
            )
        )
    ]
    return sorted(replaced, key=lambda entry: entry["coverage_id"]) or None


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


def _quarantined(exclusions: Sequence[Exclusion]) -> int:
    """Records quarantined: one per input record, however many exclusions it produced."""
    return len({e.locator for e in exclusions if e.quarantine})


def _stamp(exclusions: Sequence[Exclusion], target: ChainTarget, page: Page) -> list[Exclusion]:
    return [dataclasses.replace(e, target=target.target_id, page=page.sha256) for e in exclusions]


def _effect_parties(target: ChainTarget, store: IngestStore) -> dict[tuple[str, int], set[str]]:
    """Ledger parties of this target's chain effects, per (tx, op)."""
    parties: dict[tuple[str, int], set[str]] = {}
    for observation in store.observations():
        payload = observation.payload
        if (
            isinstance(payload, ChainEffectPayload)
            and observation.source.source_id == target.source_id
            and observation.instrument_id == target.instrument_id
        ):
            found = parties.setdefault(
                (payload.chain.tx_hash, payload.chain.operation_index), set()
            )
            found |= {p.id for p in effect_parties(payload) if p.id is not None}
    return parties


def _quarantine_records(
    target: ChainTarget,
    store: IngestStore,
    path: str,
    pages: Sequence[str],
    checks: Sequence[Mapping[str, Any]],
    expected: int,
    links: Sequence[ExecutionLink] = (),
) -> list[dict[str, Any]] | None:
    """Each quarantined record of a coverage with the addresses it involves.

    An exclusion of an operation also involves every party of that operation's effects
    (e.g. the offer owners of a path payment). Each record says what the evidence
    establishes about it (``nature``) and its ledger operation when known. A record on an
    operation that an explicit ExecutionLink names keeps its addresses and lists the linked
    operations (``execution_links``): the link makes it relevant to them, and nothing about
    its parties is lost (until then its parties were written as unknown). If the
    records do not add up to the certificate's count, None: every quarantined record then
    affects every operation.
    """
    linked: dict[str, set[str]] = {}
    for link in links:
        if link.network == target.network:
            linked.setdefault(f"{link.tx_hash}:{link.operation_index}", set()).add(
                link.operation_ref
            )
    parties = _effect_parties(target, store)
    wanted = set(pages)
    records: dict[str, dict[str, Any]] = {}

    def add(
        locator: str,
        reason: str,
        addresses: set[str] | None,
        nature: str,
        chain_op: str | None,
    ) -> None:
        entry = records.setdefault(
            locator,
            {
                "locator": locator,
                "reasons": set(),
                "addresses": set(),
                "natures": set(),
                "chain_ops": set(),
            },
        )
        entry["reasons"].add(reason)
        entry["natures"].add(nature)
        entry["chain_ops"].add(chain_op)
        if addresses is None or entry["addresses"] is None:
            entry["addresses"] = None
        else:
            entry["addresses"] |= addresses

    def of_operation(operation: str | None) -> set[str]:
        if not operation:
            return set()
        tx_hash, op_index = operation.split("@")[0].split(":")
        return parties.get((tx_hash, int(op_index)), set())

    for e in store.exclusions():
        if (
            e["path"] != path
            or not e["quarantine"]
            or e.get("target_id") != target.target_id
            or e.get("page") not in wanted
        ):
            continue
        found = e.get("addresses")
        add(
            e["locator"],
            e["reason"],
            None if found is None else {*found, *of_operation(e.get("operation"))},
            e.get("nature") or "unresolved",
            e.get("chain_op"),
        )
    for check in checks:
        if check["status"] in CORRESPONDENCE_RESOLVED:
            continue
        add(
            _correspondence_locator(target, check),
            "CORRESPONDENCE_CONFLICT",
            None
            # A claimable balance whose history could not be read: its participants are
            # unknown, so its relevance cannot be ruled out for any operation.
            if check.get("participants") == "unresolved"
            else {
                *check["classic_net"],
                *check["sac_net"],
                *parties.get((check["tx_hash"], check["op_index"]), set()),
            },
            _check_nature(check),
            f"{check['tx_hash']}:{check['op_index']}",
        )
    if len(records) != expected:
        return None
    result = []
    for entry in sorted(records.values(), key=lambda entry: entry["locator"]):
        # One record, one ledger operation: copies naming different ones (or none) leave
        # it unknown. The least resolved nature wins.
        chain_ops = entry["chain_ops"]
        chain_op = next(iter(chain_ops)) if len(chain_ops) == 1 else None
        nature = least_resolved(entry["natures"])
        record: dict[str, Any] = {
            "locator": entry["locator"],
            "reasons": sorted(entry["reasons"]),
            "addresses": None if entry["addresses"] is None else sorted(entry["addresses"]),
            "nature": nature,
        }
        if chain_op is not None:
            record["chain_operation"] = chain_op
            if chain_op in linked:
                record["execution_links"] = sorted(linked[chain_op])
        result.append(record)
    return result


# From least to most resolved: a record whose copies disagree takes the least resolved.
NATURE_PRECEDENCE: tuple[QuarantineNature, ...] = (
    "unresolved",
    "contradictory",
    "movement_identified",
    "clawback_identified",
)


def least_resolved(natures: Set[str]) -> QuarantineNature:
    return next(n for n in NATURE_PRECEDENCE if n in natures)


def _check_nature(check: Mapping[str, Any]) -> QuarantineNature:
    """What an unresolved correspondence check establishes. A clawback (a SAC
    ``clawback`` event, or the clawback of a claimable balance) is identified unless the
    Classic side says the operation is something else; an effect on a claimable balance
    with unread participants is unresolved; any other check is a known kind of effect whose
    sides disagree (path payment, DEX fill, balance creation or claim): unresolved, except
    a balance effect whose participants were read."""
    clawback = check.get("kind") == "clawback" or (
        check.get("kind") == "claimable_balance"
        and check.get("expected_operation") == "clawback_claimable_balance"
    )
    if check.get("status") == "classic_contradicts":
        return "contradictory"
    if clawback:
        return "clawback_identified"
    if check.get("kind") == "claimable_balance" and check.get("participants") == "resolved":
        return "movement_identified"
    return "unresolved"


def _correspondence_locator(target: ChainTarget, check: Mapping[str, Any]) -> str:
    return (
        f"correspondence#{target.target_id}:{check['tx_hash']}:{check['op_index']}"
        f"@{check['evidence_sha256'][:16]}"
    )


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
        _refuse_synthetic(target, page)
        records = page.document.get("_embedded", {}).get("records", [])
        observations: list[Observation] = []
        exclusions: list[Exclusion] = []
        path_entries: list[dict[str, Any]] = []
        listed: list[dict[str, Any]] = []
        cursor, received, done = checkpoint.cursor, 0, False
        for index, record in enumerate(records):
            if int(str(record["paging_token"])) >> 32 > end_ledger:
                done = True
                break
            received += 1
            cursor = str(record["paging_token"])
            try:
                ledger, _order, op_index = toid_parts(int(cursor))
                listed.append(
                    {
                        "target_id": target.target_id,
                        "tx_hash": str(record["transaction_hash"]),
                        "op_index": op_index,
                        "ledger": ledger,
                        "type": str(record.get("type")),
                    }
                )
            except (KeyError, ValueError):
                pass  # excluded as MALFORMED; its SAC events then need their origin resolved
            for result in normalize_horizon_record(
                target, record, page, index, recorded_at, lookup
            ):
                if isinstance(result, Observation):
                    observations.append(result)
                else:
                    exclusions.append(result)
            if str(record.get("type")) in PATH_PAYMENTS:
                try:
                    entry = path_operation_entry(target, record)
                except (KeyError, ValueError, TypeError):
                    entry = None  # already excluded as MALFORMED
                if entry and (
                    entry["source_is_target"]
                    or entry["destination_is_target"]
                    or entry["intermediate_only"]
                ):
                    path_entries.append(entry)
        if len(records) < page_limit:
            done = True
        exclusions = _stamp(exclusions, target, page)
        checkpoint = checkpoint.model_copy(
            update={
                "cursor": cursor,
                "pages": checkpoint.pages + 1,
                "records_received": checkpoint.records_received + received,
                "records_quarantined": checkpoint.records_quarantined + _quarantined(exclusions),
                "page_sha256": [*checkpoint.page_sha256, page.sha256],
                "complete": done,
            }
        )
        store.append_path_operations(path_entries)  # before the checkpoint that covers them
        store.append_listed_operations(listed)
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
            f"Classic clawbacks, DEX fills and other effects Horizon does not list among the "
            f"payments are outside this method (the SAC run covers them); provider claim, "
            f"not independently verified"
        )
        quarantined = _quarantine_records(
            target,
            store,
            "horizon_payments",
            checkpoint.page_sha256,
            [],
            checkpoint.records_quarantined,
            links,
        )
        coverage = _coverage(
            target, horizon, store, checkpoint, method, recorded_at, quarantined, links
        )
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


def _check_correspondence(
    target: ChainTarget,
    store: IngestStore,
    path_index: Mapping[tuple[str, int], Mapping[str, Any]],
    start_ledger: int,
    end_ledger: int,
) -> list[Exclusion]:
    """Once the SAC run is complete: Classic vs SAC per path operation and per DEX fill
    operation of this target.

    Only effects of this target's network, asset and source count. A check is recomputed,
    and appended as a new revision, when its evidence changed; otherwise a resumed run
    neither repeats nor re-counts it.
    """
    legs: dict[tuple[str, int], list[Observation]] = {}
    fills: dict[tuple[str, int], dict[str, list[Observation]]] = {}
    clawbacks: dict[tuple[str, int], dict[str, list[Observation]]] = {}
    ledgers: dict[tuple[str, int], int] = {}
    for observation in store.observations():
        payload = observation.payload
        if not (
            isinstance(payload, ChainEffectPayload)
            and observation.source.source_id == target.source_id
            and observation.instrument_id == target.instrument_id
            and payload.chain.network == target.network
        ):
            continue
        key = (payload.chain.tx_hash, payload.chain.operation_index)
        if payload.effect_kind == "path_payment_leg":
            legs.setdefault(key, []).append(observation)
        elif (
            payload.effect_kind == "clawback"
            and payload.claimable_balance is None  # checked against the balance's history
            and target.account in (payload.account, target.asset_issuer)
        ):
            side = clawbacks.setdefault(key, {"classic": [], "sac": []})
            side[payload.representation].append(observation)
            ledgers[key] = payload.chain.ledger
        elif (
            payload.effect_kind == "dex_fill"
            and key not in path_index
            and payload.account == target.account
        ):
            side = fills.setdefault(key, {"classic": [], "sac": []})
            side[payload.representation].append(observation)
            ledgers[key] = payload.chain.ledger
    # An effect that could not be kept, or two different contents for one effect, leaves
    # the operation's evidence incomplete; neither copy is chosen nor summed twice.
    unresolved = {
        entry["operation"]
        for entry in store.exclusions()
        if entry["path"] == "rpc_sac_events"
        and entry.get("target_id") == target.target_id
        and entry.get("operation")
        and entry["quarantine"]
        and entry["reason"] not in NOT_INCOMPLETE
    }
    watched = f"{target.asset_code}:{target.asset_issuer}"

    def duplicated(effects: Sequence[Observation]) -> bool:
        keys = [o.source.record_key for o in effects]
        return len(keys) != len(set(keys))

    candidates: list[tuple[dict[str, Any], str]] = []
    for key, operation in sorted(path_index.items()):
        if not start_ledger <= operation["ledger"] <= end_ledger:
            continue
        found = legs.get(key, [])
        incomplete = duplicated(found) or operation_key(operation) in unresolved
        candidates.append(
            (path_correspondence(operation, found, incomplete), operation_key(operation))
        )
    for key, sides in sorted(fills.items()):
        if not start_ledger <= ledgers[key] <= end_ledger:
            continue
        operation_ref = f"{key[0]}:{key[1]}@{watched}"
        incomplete = (
            duplicated(sides["classic"]) or duplicated(sides["sac"]) or operation_ref in unresolved
        )
        record = fill_correspondence(
            target, (key[0], key[1], ledgers[key]), sides["classic"], sides["sac"], incomplete
        )
        candidates.append((record, operation_ref))
    origin_status = {
        entry["operation"]: CLAWBACK_ORIGIN_STATUS[entry["reason"]]
        for entry in store.exclusions()
        if entry.get("target_id") == target.target_id
        and entry["reason"] in CLAWBACK_ORIGIN_STATUS
        and entry.get("operation")
    }
    for key, sides in sorted(clawbacks.items()):
        if not start_ledger <= ledgers[key] <= end_ledger:
            continue
        operation_ref = f"{key[0]}:{key[1]}@{watched}"
        incomplete = (
            duplicated(sides["classic"]) or duplicated(sides["sac"]) or operation_ref in unresolved
        )
        record = clawback_correspondence(
            target,
            (key[0], key[1], ledgers[key]),
            sides["classic"],
            sides["sac"],
            incomplete,
            origin_status.get(operation_ref, "classic_unavailable"),
        )
        candidates.append((record, operation_ref))
    current = store.current_correspondence()
    records: list[dict[str, Any]] = []
    exclusions: list[Exclusion] = []
    for record, operation_ref in candidates:
        previous = current.get((target.target_id, record["tx_hash"], record["op_index"]))
        if previous and previous["evidence_sha256"] == record["evidence_sha256"]:
            continue
        records.append(record)
        if record["status"] not in CORRESPONDENCE_RESOLVED:
            exclusions.append(
                Exclusion(
                    "rpc_sac_events",
                    _correspondence_locator(target, record),
                    "CORRESPONDENCE_CONFLICT",
                    f"{record['status']}: Classic net {record['classic_net']} vs SAC net "
                    f"{record['sac_net']}; neither side is chosen",
                    operation=operation_ref,
                    chain_op=f"{record['tx_hash']}:{record['op_index']}",
                    target=target.target_id,
                )
            )
    store.append_exclusions(e.as_dict() for e in exclusions)
    store.append_correspondence(records)
    return exclusions


def _scanned_through(cursor: Any, end_ledger: int) -> bool:
    """RPC's page cursor is a TOID-based position; its ledger tells how far RPC scanned."""
    match = RPC_SCANNED_CURSOR.match(str(cursor or ""))
    return match is not None and int(match.group(1)) >> 32 >= end_ledger


def _refuse_synthetic(target: ChainTarget, page: Page) -> None:
    """A synthetic response can never yield observations marked as real."""
    if "_synthetic" in page.document and not target.synthetic:
        raise ValueError(f"{page.locator}: synthetic response for a non-synthetic target")


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
        # Horizon shows contract_id only once the SAC is deployed. Undeployed Classic assets
        # still emit CAP-67 unified events under the derived id (observed on testnet), so an
        # absent id is accepted; any different id is a contradiction.
        if not published <= {derived, None}:
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
    classic = store.get_checkpoint(target.target_id, "horizon_payments")
    if (
        classic is None
        or not classic.complete
        or (
            classic.start_ledger,
            classic.end_ledger,
        )
        != (start_ledger, end_ledger)
    ):
        raise ValueError(
            "SAC events need the complete Horizon run of the same ledger range first: "
            "without its path payment index a path leg would be read as a movement"
        )
    path_index = {
        (e["tx_hash"], e["op_index"]): e
        for e in store.path_operations()
        if e["target_id"] == target.target_id
    }
    listed = {
        (e["tx_hash"], e["op_index"])
        for e in store.listed_operations()
        if e["target_id"] == target.target_id
    }
    origins: dict[tuple[str, int], Origin] = {}
    clawback_origins: dict[tuple[str, int], str | None] = {}
    histories: dict[str, BalanceHistory] = {}
    linked_ops = {
        f"{link.tx_hash}:{link.operation_index}" for link in links if link.network == target.network
    }
    contract_id = resolve_sac_contract(target, horizon)
    lookup = _link_lookup(links)
    # The Classic payment record of each effect (mapping 1.1.0 on), which a SAC event's
    # muxed or memo datum is checked against.
    copies: dict[str, list[Observation]] = {}
    for o in store.observations():
        if (
            o.fact_type == "token_movement"
            and o.source.source_id == target.source_id
            and o.instrument_id == target.instrument_id
            and o.provenance.parser_ref == HORIZON_PARSER
            and o.provenance.mapping_ref == HORIZON_MAPPING
        ):
            copies.setdefault(o.source.record_key, []).append(o)
    # Only a record with one Classic content is corroborated; two conflicting Classic copies
    # stay a visible conflict and the event is read on its own (Stellar review H9).
    classic_movements = {
        key: found[0]
        for key, found in copies.items()
        if len({observation_content(o) for o in found}) == 1
    }
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
        _refuse_synthetic(target, page)
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
        lookups: list[Page] = []
        balance_checks: list[dict[str, Any]] = []
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
            operation = path_index.get(
                (str(event.get("txHash")), int(event.get("operationIndex", -1)))
            )
            if operation is not None and head in MOVEMENT_TOPICS:
                if event.get("contractId") != contract_id or event.get("type") != "contract":
                    continue
                try:
                    results = normalize_sac_leg(
                        target, event, ordinal, operation, page, recorded_at
                    )
                except (KeyError, ValueError, TypeError, ValidationError) as error:
                    results = [
                        Exclusion(
                            "rpc_sac_events",
                            f"{page.locator}#{event.get('id')}",
                            "MALFORMED",
                            f"{type(error).__name__}: {str(error)[:200]}",
                            operation=operation_key(operation),
                        )
                    ]
            elif (
                head in ("transfer", "mint", "burn")
                and (
                    classic_copy := classic_movements.get(
                        f"{event.get('txHash')}:{int(event.get('operationIndex', -1))}:{ordinal}"
                    )
                )
                is not None
                and (
                    corroborated := corroborate_movement(
                        target, contract_id, event, ordinal, page, recorded_at, lookup, classic_copy
                    )
                )
                is not None
            ):
                results = list(corroborated)
            else:
                single = normalize_sac_event(
                    target, contract_id, event, ordinal, page, recorded_at, lookup
                )
                results = (
                    [] if single is None or isinstance(single, PendingBalanceEffect) else [single]
                )
                key = (str(event.get("txHash")), int(event.get("operationIndex", -1)))
                clawback = (
                    isinstance(single, Observation)
                    and isinstance(single.payload, ChainEffectPayload)
                    and single.payload.effect_kind == "clawback"
                )
                if isinstance(single, PendingBalanceEffect):
                    results = _balance_results(
                        target,
                        horizon,
                        single,
                        page,
                        recorded_at,
                        histories,
                        lookups,
                        balance_checks,
                    )
                elif clawback:
                    assert isinstance(single, Observation)
                    results = _clawback_results(
                        target,
                        horizon,
                        single,
                        key,
                        int(event["ledger"]),
                        recorded_at,
                        clawback_origins,
                        lookups,
                        observations,
                    )
                elif isinstance(single, Observation) and single.fact_type == "chain_effect":
                    results = _typed_results(target, single, key in listed)
                elif single is not None and key not in listed:
                    if isinstance(single, Exclusion):
                        results = [
                            dataclasses.replace(
                                single,
                                operation=f"{key[0]}:{key[1]}@{target.asset_code}:"
                                f"{target.asset_issuer}",
                            )
                        ]
                    else:
                        if key not in origins:
                            origin = _operation_origin(
                                target, horizon, key[0], key[1], int(event["ledger"]), recorded_at
                            )
                            origins[key] = origin
                            lookups.extend(origin.pages)
                            observations.extend(origin.classic)
                        if origins[key].kind != "movement":
                            results = _unlisted_effect(target, single, origins[key], ordinal, page)
                        else:
                            results = [single, _not_listed(single, origins[key])]
            if event.get("txHash") is not None and "operationIndex" in event:
                chain_op = f"{event['txHash']}:{int(event['operationIndex'])}"
                # An operation an explicit ExecutionLink names must reach the linked
                # evaluation: a note on it (a corroborated clawback or balance effect, a
                # listed contract movement) is quarantined, and its record lists the link
                # beside its known addresses.
                linked = chain_op in linked_ops
                results = [
                    r
                    if isinstance(r, Observation)
                    else dataclasses.replace(
                        r,
                        chain_op=chain_op,
                        quarantine=r.quarantine or linked,
                        detail=r.detail
                        + (
                            "; quarantined because an ExecutionLink names the operation"
                            if linked and not r.quarantine
                            else ""
                        ),
                    )
                    for r in results
                ]
            for result in results:
                if isinstance(result, Observation):
                    observations.append(result)
                else:
                    exclusions.append(result)
        # A short page ends the range only when RPC's cursor shows it scanned through the
        # end ledger: RPC may stop scanning early and return fewer events than the limit.
        done = bool(beyond) or (
            len(returned) < page_limit and _scanned_through(result_doc.get("cursor"), end_ledger)
        )
        next_cursor = str(events[-1]["id"]) if beyond and events else result_doc.get("cursor")
        exclusions = _stamp(exclusions, target, page)
        for extra in lookups:
            store.save_page(extra.sha256, extra.raw)
        checkpoint = checkpoint.model_copy(
            update={
                "cursor": str(next_cursor or checkpoint.cursor),
                "pages": checkpoint.pages + 1,
                "records_received": checkpoint.records_received + len(events),
                "records_quarantined": checkpoint.records_quarantined + _quarantined(exclusions),
                # The operation and effect pages read to classify unlisted movements are
                # inputs of this run too.
                "page_sha256": [
                    *checkpoint.page_sha256,
                    page.sha256,
                    *(extra.sha256 for extra in lookups),
                ],
                "ordinal_state": state,
                "complete": done,
            }
        )
        # Checks of claimable balance effects go before the checkpoint that covers them; a
        # rerun with the same evidence does not repeat them.
        current = store.current_correspondence()
        store.append_correspondence(
            [
                record
                for record in balance_checks
                if (
                    previous := current.get(
                        (target.target_id, record["tx_hash"], record["op_index"])
                    )
                )
                is None
                or previous["evidence_sha256"] != record["evidence_sha256"]
            ]
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
        exclusions_all += _check_correspondence(target, store, path_index, start_ledger, end_ledger)
        method = (
            f"RPC getEvents for SAC {contract_id} ({target.asset_code}:"
            f"{target.asset_issuer}), movements involving {target.account}; path payment "
            f"legs, DEX fills, clawbacks and claimable balance effects kept apart and checked "
            f"against Classic per affected party (records include one correspondence check per "
            f"such operation); movements with contracts and liquidity pools kept as typed "
            f"effects; together with "
            f"the records of the Horizon run of the same range it depends on; provider claim "
            f"within RPC retention, not independently verified"
        )
        # Each correspondence check is a derived input record of this coverage; a check
        # that is not corroborated is quarantined. The Horizon run's records are included:
        # this certificate supersedes it as the latest of the source, and must not hide a
        # record that only the Horizon run quarantined.
        checks = [
            entry
            for (target_id, _tx, _op), entry in sorted(store.current_correspondence().items())
            if target_id == target.target_id and start_ledger <= entry["ledger"] <= end_ledger
        ]
        sac_quarantined = checkpoint.records_quarantined + sum(
            1 for entry in checks if entry["status"] not in CORRESPONDENCE_RESOLVED
        )
        counted = checkpoint.model_copy(
            update={
                "records_received": checkpoint.records_received
                + len(checks)
                + classic.records_received,
                "records_quarantined": sac_quarantined + classic.records_quarantined,
                "page_sha256": [*classic.page_sha256, *checkpoint.page_sha256],
            }
        )
        sac_records = _quarantine_records(
            target, store, "rpc_sac_events", checkpoint.page_sha256, checks, sac_quarantined, links
        )
        classic_records = _quarantine_records(
            target,
            store,
            "horizon_payments",
            classic.page_sha256,
            [],
            classic.records_quarantined,
            links,
        )
        quarantined = (
            None
            if sac_records is None or classic_records is None
            else sorted([*classic_records, *sac_records], key=lambda r: r["locator"])
        )
        coverage = _coverage(
            target, horizon, store, counted, method, recorded_at, quarantined, links
        )
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
