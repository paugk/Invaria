"""Reconciliation of the trustline of one asset for approved accounts, in a time interval,
from the meta of a verified replay and the anchored history.

It answers which balance changes can be reconstructed, which can be explained and what
stays unresolved. It is a report: it never states that the deliveries are complete or that
an obligation was met, and it changes no profile, engine, certificate or coverage level.

What it rests on, kept apart:
- the anchored checkpoint (headers, transaction and result sets), which gives the ledgers'
  ``close_time``, the operations and the transaction results;
- the replay meta, read only through ``verify_replay`` (artifacts checked against their
  record before decoding, then the meta against the anchored checkpoint) and only when the
  decoded stream is exactly the one the record declares. Its entry changes and events are
  derived from the replay, never committed by consensus. Without a trusted sha256 of the
  record the reading is an identified diagnostic, never an authenticated reconciliation.

A line is a network, a G account and a full asset (type, code and issuer): never a code
alone. Changes are matched to their evidence by location (ledger, transaction, operation),
account and asset identity, never by amount alone; a muxed participant gets no identity
from decoding its base account.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any, Literal

from invaria.contracts.chain import ChainTarget
from invaria.contracts.identity import IdentityLink
from invaria.contracts.observation import Observation
from invaria.stellar.ledger_proof import (
    CheckpointEvidence,
    ReplayArtifacts,
    ReplayEvidence,
    _base,
    _inner_tx,
    _outcome,
    _scval,
)

Bounds = Literal["half_open", "closed"]
TemporalStatus = Literal["COVERED", "INCOMPLETE"]
StateKind = Literal["PRESENT", "ABSENT", "NOT_ESTABLISHED"]
ChangeStatus = Literal[
    "EXPLAINED",
    "NO_BALANCE_CHANGE",
    "UNEXPLAINED",
    "AMBIGUOUS",
    "NOT_RECONSTRUCTED",
    "CONTRADICTED",
]
CHANGE_STATUSES: tuple[ChangeStatus, ...] = (
    "EXPLAINED",
    "NO_BALANCE_CHANGE",
    "UNEXPLAINED",
    "AMBIGUOUS",
    "NOT_RECONSTRUCTED",
    "CONTRADICTED",
)
ReconciliationStatus = Literal[
    "RECONCILED_IN_SCOPE",
    "RECONCILED_UNAUTHENTICATED",
    "UNRESOLVED_CHANGES",
    "CONTRADICTED",
    "INCOMPLETE",
    "NOT_ESTABLISHED",
]
CONTROL_SEMANTICS = (
    "subscription.token_units_vs_order (fund-subscription-testnet@1.6.0) needs coverage "
    "order_valid_time_to_valid_at: the engine accepts a half-open certificate [start, end) "
    "with start <= accepted_at and end >= valid_at, so the control covers [accepted_at, "
    "valid_at), valid_at excluded (engine/evaluate.py::_coverage_problems)"
)
EXCLUDED = [
    "claimable balances created for an approved account and not claimed: they do not change "
    "its trustline",
    "attribution of contract addresses (IdentityLink for contracts is not authorized)",
    "other representations of the instrument",
    "ledgers outside the verified replay",
    "effects Horizon derives that this evidence cannot reconstruct",
    "any integration with the financial engines",
]
NOT_SHOWN = [
    "the absence of a trustline change does not rule out every effect relevant to the "
    "investor (an unclaimed claimable balance, a contract balance, another representation)",
    "a balance that adds up does not show that every movement was identified",
    "completeness of the deliveries or compliance with any obligation",
    "a guarantee above provider_claimed: none is created and coverage_level is unchanged",
]
LIMITS = [
    "the meta gives one net change per entry and operation: movements inside one operation "
    "are seen only through its events, and are not separable when no event shows them",
    "entry changes and events are derived from the replay (pinned stellar-core build and "
    "configuration); no ledger header commits them",
    "the replay covers one checkpoint; its ledgers bound what can be reconstructed",
]

_ABSENT = {"state": "ABSENT", "balance": None}
_UNKNOWN = {"state": "NOT_ESTABLISHED", "balance": None}


def _iso(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def _seconds(moment: datetime) -> int:
    if moment.tzinfo is None or moment.utcoffset() != UTC.utcoffset(None):
        raise ValueError("interval bounds must be UTC")
    if moment.microsecond:
        raise ValueError("interval bounds must be whole seconds (close_time is in seconds)")
    return int(moment.timestamp())


def _ceil_seconds(moment: datetime) -> int:
    """For whole-second close times t: t >= moment and t < moment hold exactly as
    t >= ceil(moment) and t < ceil(moment)."""
    return _seconds(moment.replace(microsecond=0)) + (1 if moment.microsecond else 0)


# ------------------------------------------------------------------ time to ledgers


@dataclass
class TemporalCoverage:
    status: TemporalStatus
    start: str
    end: str
    bounds: Bounds
    ledgers: list[int] = field(default_factory=list)
    before_start: dict[str, Any] | None = None
    after_end: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)

    def section(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "requested": {
                "start": self.start,
                "end": self.end,
                "bounds": self.bounds,
                "rule": "start <= close_time < end"
                if self.bounds == "half_open"
                else "start <= close_time <= end",
            },
            "control_semantics": CONTROL_SEMANTICS,
            "time_source": "close_time of the ledger headers verified against the anchor; "
            "never the time Horizon reports",
            "examined_ledgers": [self.ledgers[0], self.ledgers[-1]] if self.ledgers else [],
            "border_before_start": self.before_start,
            "border_after_end": self.after_end,
            "problems": self.problems,
        }


def select_ledgers(
    evidence: CheckpointEvidence, start: datetime, end: datetime, bounds: Bounds = "half_open"
) -> TemporalCoverage:
    """The verified ledgers whose close_time falls in the interval, and the evidence of its
    borders: the ledger before the first selected and the one after the last must be in the
    verified history, or the coverage is INCOMPLETE (never widened or narrowed to fit)."""
    s, e = _seconds(start), _seconds(end)
    if e < s or (e == s and bounds == "half_open"):
        raise ValueError("the interval end must follow its start")
    out = TemporalCoverage("INCOMPLETE", _iso(s), _iso(e), bounds)
    if evidence.status != "VERIFIED" or not evidence.headers:
        out.problems.append(f"checkpoint evidence is {evidence.status}: no verified close_time")
        return out
    seqs = sorted(evidence.headers)
    times = {q: int(evidence.headers[q]["scp_value"]["close_time"]) for q in seqs}
    if seqs != list(range(seqs[0], seqs[-1] + 1)):
        out.problems.append("the verified headers have a hole")
    if any(times[a] >= times[b] for a, b in pairwise(seqs)):
        out.problems.append("close_time is not strictly increasing across the verified headers")

    def inside(t: int) -> bool:
        return s <= t < e if bounds == "half_open" else s <= t <= e

    def beyond(t: int) -> bool:
        return t >= e if bounds == "half_open" else t > e

    out.ledgers = [q for q in seqs if inside(times[q])]
    before = [q for q in seqs if times[q] < s]
    after = [q for q in seqs if beyond(times[q])]
    if before:
        q = before[-1]
        out.before_start = {"ledger": q, "close_time": _iso(times[q])}
    else:
        out.problems.append(
            "no verified ledger closes before the start: earlier ledgers, outside this "
            "history, may belong to the interval"
        )
    if after:
        q = after[0]
        out.after_end = {"ledger": q, "close_time": _iso(times[q])}
    else:
        out.problems.append(
            "no verified ledger closes at or after the end: later ledgers, outside this "
            "history, may belong to the interval"
        )
    if not out.problems:
        out.status = "COVERED"
    return out


# ------------------------------------------------------------------ approved accounts


ApprovalStatus = Literal["EXAMINED", "NOT_EXAMINABLE", "NOT_APPLICABLE"]


@dataclass
class Approval:
    """An approved address of the institutional account and the part of the requested
    interval in which its approval applies (windows in seconds: start, end, end included)."""

    address: str
    link_ids: list[str]
    status: ApprovalStatus
    reason: str
    windows: list[tuple[int, int, bool]] = field(default_factory=list)

    def applies(self, close_time: int) -> bool:
        return any(
            ws <= close_time <= we if inclusive else ws <= close_time < we
            for ws, we, inclusive in self.windows
        )

    def section(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "link_ids": self.link_ids,
            "status": self.status,
            "reason": self.reason,
            "applicable_subintervals": [
                {"start": _iso(ws), "end": _iso(we), "end_included": inclusive}
                for ws, we, inclusive in self.windows
            ],
        }


def approved_addresses(
    links: Iterable[IdentityLink],
    network: str,
    account_ref: str,
    start: datetime,
    end: datetime,
    bounds: Bounds = "half_open",
) -> list[Approval]:
    """Every address linked to ``account_ref``, with the subintervals of the request in which
    its link applies (a link's validity is [valid_from, valid_to)). An approval never
    applies outside its window. A G address is examined in its windows; a muxed sub-account
    cannot be, since its base account's trustline mixes every sub-account; a link outside
    the interval or on another network does not apply."""
    s, e = _seconds(start), _seconds(end)
    found: dict[str, Approval] = {}
    for link in links:
        if link.account_ref != account_ref:
            continue
        vf = max(s, _ceil_seconds(link.valid_from))
        vt = None if link.valid_to is None else _ceil_seconds(link.valid_to)
        if vt is None or vt > e or (vt == e and bounds == "half_open"):
            window = (vf, e, bounds == "closed")
        else:
            window = (vf, vt, False)
        empty = window[0] > window[1] or (window[0] == window[1] and not window[2])
        if link.network != network:
            status: ApprovalStatus = "NOT_APPLICABLE"
            reason = f"another network ({link.network})"
        elif empty:
            status, reason = "NOT_APPLICABLE", "the link is not valid in the interval"
        elif link.address.startswith("M"):
            status, reason = (
                "NOT_EXAMINABLE",
                "a muxed sub-account: its base account's trustline mixes every sub-account, "
                "and the link approves only this sub-account",
            )
        else:
            status, reason = "EXAMINED", "a G address: its trustline is examined in its windows"
        key = f"{status}:{link.address}"
        approval = found.setdefault(key, Approval(link.address, [], status, reason))
        approval.link_ids.append(link.link_id)
        if status != "NOT_APPLICABLE":
            approval.windows.append(window)
    return list(found.values())


# ------------------------------------------------------------------ meta reading


@dataclass(frozen=True)
class _Location:
    ledger: int
    order: tuple[int, int, int]
    where: str
    tx_index: int | None = None
    tx_hash: str | None = None
    operation_index: int | None = None


def _locations(body: dict[str, Any]) -> Iterator[tuple[_Location, list[Any], list[Any]]]:
    """Every list of LedgerEntryChanges of a LedgerCloseMeta body, with its location and,
    for an operation, its events. Raises ValueError on a meta it does not understand."""
    seq = body["ledger_header"]["header"]["ledger_seq"]
    for i, entry in enumerate(body["tx_processing"]):
        tx = entry["result"]["transaction_hash"]
        yield _Location(seq, (0, i, 0), "fee_processing", i, tx), entry["fee_processing"], []
        apply = entry["tx_apply_processing"]
        if "v4" not in apply:
            raise ValueError(f"ledger {seq}: transaction meta {sorted(apply)} is not supported")
        meta = apply["v4"]
        yield _Location(seq, (1, i, 0), "tx_changes_before", i, tx), meta["tx_changes_before"], []
        for j, op in enumerate(meta["operations"]):
            location = _Location(seq, (1, i, 1 + j), "operation", i, tx, j)
            yield location, op["changes"], op["events"]
        yield (
            _Location(seq, (1, i, 1 << 30), "tx_changes_after", i, tx),
            meta["tx_changes_after"],
            [],
        )
        post = entry.get("post_tx_apply_fee_processing") or []
        yield _Location(seq, (2, i, 0), "post_tx_apply_fee_processing", i, tx), post, []
    for k, upgrade in enumerate(body.get("upgrades_processing") or []):
        yield _Location(seq, (3, k, 0), "upgrade"), upgrade["changes"], []


LineKey = tuple[str, str, str, str]  # account, asset type, code, issuer


def _line_key(trustline: dict[str, Any]) -> LineKey | None:
    asset = trustline.get("asset")
    if not isinstance(asset, dict):
        return None
    for kind in ("credit_alphanum4", "credit_alphanum12"):
        if kind in asset:
            credit = asset[kind]
            return (trustline["account_id"], kind, credit["asset_code"], credit["issuer"])
    return None  # pool share trustlines


@dataclass(frozen=True)
class _Raw:
    key: LineKey
    kind: str
    before: dict[str, Any]
    after: dict[str, Any]


def _state(entry: dict[str, Any]) -> dict[str, Any]:
    return {"state": "PRESENT", "balance": int(entry["data"]["trustline"]["balance"])}


def _raw_changes(changes: list[Any]) -> list[_Raw]:
    """The trustline changes of one list, with their pre-images; ``state`` alone is a
    pre-image, not a change."""
    pre: dict[LineKey, dict[str, Any]] = {}
    out = []
    for change in changes:
        kind = next(iter(change))
        value = change[kind]
        line = (value if kind == "removed" else value.get("data", {})).get("trustline")
        if not isinstance(line, dict) or (key := _line_key(line)) is None:
            continue
        if kind == "state":
            pre[key] = _state(value)
            continue
        if kind == "created":
            out.append(_Raw(key, kind, pre.pop(key, dict(_ABSENT)), _state(value)))
        elif kind == "removed":
            out.append(_Raw(key, kind, pre.pop(key, dict(_UNKNOWN)), dict(_ABSENT)))
        else:  # updated, or a kind a trustline should never have (restored)
            out.append(_Raw(key, kind, pre.pop(key, dict(_UNKNOWN)), _state(value)))
    return out


def _count_refs(value: Any, keys: set[LineKey]) -> int:
    """Occurrences of the lines anywhere in a value (entries and ledger keys)."""
    if isinstance(value, list):
        return sum(_count_refs(v, keys) for v in value)
    if not isinstance(value, dict):
        return 0
    found = 0
    line = value.get("trustline")
    if isinstance(line, dict) and "account_id" in line and _line_key(line) in keys:
        found = 1
    return found + sum(_count_refs(v, keys) for v in value.values())


def _balance(state: dict[str, Any]) -> int | None:
    """The asset amount a known state holds (an absent trustline holds none)."""
    if state["state"] == "ABSENT":
        return 0
    if state["state"] == "PRESENT":
        return int(state["balance"])
    return None


def _account_events(
    events: Sequence[dict[str, Any]], sac: str, asset: str, accounts: set[str]
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """The asset's events naming each account (by the base of the address), and the number
    of events of other contracts for an asset with the same code."""
    found: dict[str, list[dict[str, Any]]] = {}
    other = 0
    code = asset.split(":", 1)[0]
    for index, event in enumerate(events):
        body = event["body"]["v0"]
        topics = [_scval(t) for t in body["topics"]]
        if event.get("contract_id") != sac or not topics or topics[-1] != asset:
            if topics and isinstance(topics[-1], str) and topics[-1].startswith(code + ":"):
                other += 1
            continue
        name = topics[0]
        parties = {
            "transfer": (topics[1], topics[2]) if len(topics) == 4 else (None, None),
            "mint": (None, topics[1]) if len(topics) == 3 else (None, None),
            "burn": (topics[1], None) if len(topics) == 3 else (None, None),
            "clawback": (topics[1], None) if len(topics) == 3 else (None, None),
        }.get(name, (None, None))
        data = body["data"]
        muxed_id = memo = None
        if isinstance(data, dict) and "map" in data:
            fields = {_scval(e["key"]): e["val"] for e in data["map"]}
            amount = _scval(fields.get("amount"))
            tag = fields.get("to_muxed_id")
            if isinstance(tag, dict) and "u64" in tag:
                muxed_id = str(tag["u64"])
            elif tag is not None:
                memo = _scval(tag)
        else:
            amount = _scval(data)
        sender, receiver = (None if p is None else _base(str(p)) for p in parties)
        for account in accounts:
            gross_in = int(amount) if receiver == account else 0
            gross_out = int(amount) if sender == account else 0
            if not (gross_in or gross_out):
                continue
            found.setdefault(account, []).append(
                {
                    "event_index": index,
                    "contract_id": event.get("contract_id"),
                    "name": name,
                    "amount": str(amount),
                    "gross_in": gross_in,
                    "gross_out": gross_out,
                    "muxed_id": muxed_id if gross_in else None,
                    "memo": memo,
                    "addresses": [str(p) for p in parties if p is not None],
                }
            )
    return found, other


def _mentions(value: Any, account: str) -> bool:
    if isinstance(value, str):
        if value == account:
            return True
        if value.startswith("M") and len(value) == 69:
            try:
                return _base(value) == account
            except ValueError:
                return False
        return False
    if isinstance(value, dict):
        return any(_mentions(v, account) for v in value.values())
    if isinstance(value, list):
        return any(_mentions(v, account) for v in value)
    return False


@dataclass(frozen=True)
class _Anchored:
    """What the anchored checkpoint says about one transaction and one of its operations."""

    succeeded: bool
    tx_code: str
    fee_charged: str
    fee_source: str
    operation_type: str | None
    operation: dict[str, Any] | None
    source: str | None


def _anchored(evidence: CheckpointEvidence, seq: int, tx_hash: str, index: int | None) -> _Anchored:
    ledger = evidence.ledgers[seq]
    envelope = ledger.envelopes[ledger.tx_hashes.index(tx_hash)]
    tx, fee_bump = _inner_tx(envelope)
    result = ledger.results[tx_hash]
    tx_code, _ = _outcome(result, fee_bump, index or 0)
    fee_source = envelope["tx_fee_bump"]["tx"]["fee_source"] if fee_bump else tx["source_account"]
    operation = op_type = source = None
    if index is not None and index < len(tx["operations"]):
        operation = tx["operations"][index]
        body = operation["body"]
        op_type = next(iter(body)) if isinstance(body, dict) else str(body)
        source = operation["source_account"] or tx["source_account"]
    return _Anchored(
        tx_code == "tx_success",
        ("fee bump, inner " if fee_bump else "") + tx_code,
        str(result["fee_charged"]),
        fee_source,
        op_type,
        operation,
        source,
    )


def _credit(asset: Any) -> tuple[str, str] | None:
    if not isinstance(asset, dict):
        return None
    credit = asset.get("credit_alphanum4") or asset.get("credit_alphanum12")
    return None if credit is None else (credit["asset_code"], credit["issuer"])


# ------------------------------------------------------------------ reconciliation


@dataclass
class _Op:
    location: _Location
    events: dict[str, list[dict[str, Any]]]
    keys: set[LineKey] = field(default_factory=set)


def _explain(
    change: dict[str, Any],
    raw: _Raw,
    anchored: _Anchored | None,
    events: list[dict[str, Any]],
    asset: tuple[str, str],
) -> tuple[ChangeStatus, list[str]]:
    account = raw.key[0]
    delta = change["delta"]
    if anchored is not None and not anchored.succeeded:
        return "CONTRADICTED", [
            f"the transaction failed ({anchored.tx_code}): it applies nothing, yet the line changed"
        ]
    if raw.kind == "created" and raw.before["state"] != "ABSENT":
        return "CONTRADICTED", ["a creation over an entry the meta shows as existing"]
    if change["location"] != "operation":
        return "UNEXPLAINED", [f"a change in {change['location']}, outside any operation"]
    if raw.kind not in ("created", "updated", "removed"):
        return "UNEXPLAINED", [f"a {raw.kind} change, which a trustline is not expected to have"]
    if delta is None:
        return "NOT_RECONSTRUCTED", ["the state before the change is not in the meta"]
    reasons = []
    events_net = sum(e["gross_in"] - e["gross_out"] for e in events)
    operation = anchored.operation if anchored else None
    if anchored is not None and anchored.operation_type == "payment" and operation is not None:
        payment = operation["body"]["payment"]
        if _credit(payment["asset"]) == asset:
            amount = int(payment["amount"])
            payment_net = (amount if _base(payment["destination"]) == account else 0) - (
                amount if anchored.source is not None and _base(anchored.source) == account else 0
            )
            if payment_net != delta:
                return "CONTRADICTED", [
                    f"the anchored payment moves {payment_net} for the account; the line "
                    f"changed by {delta}"
                ]
            reasons.append(f"the anchored payment moves {payment_net} for the account")
    if events and events_net != delta:
        return "CONTRADICTED", [
            f"the operation's asset events net {events_net}; the line changed by {delta}"
        ]
    status: ChangeStatus
    if raw.kind in ("created", "removed"):
        line = (operation or {}).get("body", {})
        change_trust = line.get("change_trust") if isinstance(line, dict) else None
        own = (
            anchored is not None
            and anchored.source is not None
            and (_base(anchored.source) == account)
        )
        if change_trust is not None and own and _credit(change_trust["line"]) == asset:
            if delta and not events:
                return "UNEXPLAINED", [f"{raw.kind} with a balance change and no asset event"]
            status = "EXPLAINED"
            reasons.append(f"{raw.kind} by a change_trust of this asset by the account")
        else:
            return "UNEXPLAINED", [
                f"{raw.kind} without a change_trust of this asset by the account "
                f"(operation {None if anchored is None else anchored.operation_type})"
            ]
    elif delta == 0:
        status = "EXPLAINED" if events else "NO_BALANCE_CHANGE"
        reasons.append(
            "asset events compensate within the operation (net 0)"
            if events
            else f"the entry changed without a balance change "
            f"(operation {None if anchored is None else anchored.operation_type})"
        )
    elif not events:
        return "UNEXPLAINED", ["no asset event of this operation names the account"]
    else:
        status = "EXPLAINED"
        reasons.append(f"asset events of the operation net {events_net} for the account")
    muxed = [e["muxed_id"] for e in events if e["muxed_id"] is not None]
    if anchored is not None and operation is not None:
        for address in [*_addresses(operation["body"]), anchored.source or ""]:
            if address.startswith("M") and len(address) == 69 and _base(address) == account:
                muxed.append(address)
    if muxed and status == "EXPLAINED":
        return "AMBIGUOUS", [
            *reasons,
            "MUXED_UNATTRIBUTED: a muxed sub-account of the account is named "
            f"({sorted(set(muxed))}); decoding it attributes no identity",
        ]
    return status, reasons


def _addresses(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value[:1] in ("G", "M") and len(value) in (56, 69) else []
    if isinstance(value, dict):
        return [a for v in value.values() for a in _addresses(v)]
    if isinstance(value, list):
        return [a for v in value for a in _addresses(v)]
    return []


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (a["state"], a["balance"]) == (b["state"], b["balance"])


def reconcile(
    evidence: CheckpointEvidence,
    artifacts: ReplayArtifacts,
    replay: ReplayEvidence,
    target: ChainTarget,
    approvals: Sequence[Approval],
    temporal: TemporalCoverage,
    observations: Sequence[Observation] = (),
) -> dict[str, Any]:
    """The trustline reconciliation section. Reads the meta only when the artifacts match,
    the replay is consistent with the anchored checkpoint and the decoded stream is the one
    the record declares."""
    accounts = [a.address for a in approvals if a.status == "EXAMINED"]
    asset = (target.asset_code, target.asset_issuer)
    asset_text = f"{target.asset_code}:{target.asset_issuer}"
    authenticated = artifacts.provenance == "imported_with_provenance" and not (
        artifacts.revision_sha256 is not None and artifacts.expected_revision_sha256 is None
    )
    section: dict[str, Any] = {
        "kind": "trustline_reconciliation",
        "status": "NOT_ESTABLISHED",
        "evidence_basis": "authenticated_replay" if authenticated else "unauthenticated_diagnostic",
        "question": "which balance changes of the trustline can be reconstructed, which can be "
        "explained and what stays unresolved",
        "scope": {
            "network": target.network,
            "asset": {"code": target.asset_code, "issuer": target.asset_issuer},
            "asset_contract": target.expected_sac_contract_id,
            "addresses": [a.section() for a in approvals],
            "examined_addresses": accounts,
            "replay_ledgers": [replay.ledgers[0], replay.ledgers[-1]] if replay.ledgers else [],
        },
        "temporal": temporal.section(),
        "lines": [],
        "transactions": [],
        "out_of_scope": {},
        "unlocated_references": 0,
        "summary": {},
        "problems": [],
        "trust": {
            "replay_provenance": artifacts.provenance,
            "execution_observed_by_this_process": artifacts.provenance == "observed_run",
            "record_sha256": artifacts.record_sha256,
            "expected_record_sha256": artifacts.expected_record_sha256,
            "what_a_trusted_hash_shows": "correspondence with the expected artifacts; not "
            "that the run happened",
            "changes_and_events": "derived from the replay; no ledger header commits them",
            "time": "close_time of headers verified against the anchor",
            "basis": "authenticated: the record's sha256 was supplied through a trusted channel"
            if authenticated
            else "diagnostic only: the artifacts were checked against their own record, "
            "which is coherence, not authenticity; nothing here is authenticated evidence",
        },
        "limits": LIMITS,
        "excluded": EXCLUDED,
        "not_shown": NOT_SHOWN,
        "coverage_effect": "none: no certificate, profile, coverage_level or financial result "
        "changes; provider_claimed stays for the current evaluations",
    }
    problems: list[str] = section["problems"]
    if artifacts.status != "ARTIFACTS_MATCH" or artifacts.meta_stream is None:
        problems.append(f"replay artifacts are {artifacts.status}: the meta is not read")
        return section
    if replay.status != "REPLAY_CONSISTENT":
        problems.append(f"replay is {replay.status}: the meta is not read")
        return section
    if replay.stream_sha256 != hashlib.sha256(artifacts.meta_stream).hexdigest():
        problems.append("the decoded meta is not the stream the checked artifacts hold")
        return section
    if evidence.status != "VERIFIED":
        problems.append(f"checkpoint evidence is {evidence.status}")
        return section
    borders = [b["ledger"] for b in (temporal.before_start, temporal.after_end) if b]
    unreplayed = sorted(set(temporal.ledgers + borders) - set(replay.metas))
    if unreplayed:
        temporal.status = "INCOMPLETE"
        temporal.problems.append(f"ledgers {unreplayed} are not in the verified replay")
        section["temporal"] = temporal.section()

    def keys_of(account: str) -> set[LineKey]:
        return {(account, k, *asset) for k in ("credit_alphanum4", "credit_alphanum12")}

    keys = {k for a in accounts for k in keys_of(a)}
    in_interval = set(temporal.ledgers)
    close = {q: int(evidence.headers[q]["scp_value"]["close_time"]) for q in evidence.headers}
    # Each address only in the ledgers where its approval applies.
    windows = {
        a.address: {q for q in temporal.ledgers if a.applies(close[q])}
        for a in approvals
        if a.status == "EXAMINED"
    }
    unlocated_by = dict.fromkeys(accounts, 0)
    by_line: dict[LineKey, list[dict[str, Any]]] = {}
    ops: dict[tuple[int, int, int], _Op] = {}
    other_issuer_changes = other_accounts_changes = other_issuer_events = unlocated = 0
    sac = target.expected_sac_contract_id or ""
    if not sac:
        problems.append("the target names no asset contract: no event can explain a change")
    try:
        for seq, body in sorted(replay.metas.items()):
            located = dict.fromkeys(accounts, 0)
            for location, changes, events in _locations(body):
                raws = _raw_changes(changes)
                for account in accounts:
                    located[account] += _count_refs(changes, keys_of(account))
                op = None
                if location.where == "operation":
                    found, other = _account_events(events, sac, asset_text, set(accounts))
                    if seq in in_interval:
                        other_issuer_events += other
                    op = _Op(location, found)
                    ops[(seq, location.tx_index or 0, location.operation_index or 0)] = op
                seen: dict[LineKey, _Raw] = {}
                for raw in raws:
                    if raw.key not in keys:
                        if raw.key[2] == target.asset_code and raw.key[0] in accounts:
                            other_issuer_changes += seq in in_interval
                        elif raw.key[2:] == asset:
                            other_accounts_changes += seq in in_interval
                        continue
                    if raw.key in seen:
                        previous = by_line[raw.key][-1]
                        if seen[raw.key] == raw:
                            previous["duplicate_representations"] += 1
                        else:
                            previous["contradictory_representation"] = {
                                "kind": raw.kind,
                                "before": raw.before,
                                "after": raw.after,
                            }
                        continue
                    seen[raw.key] = raw
                    if op is not None:
                        op.keys.add(raw.key)
                    by_line.setdefault(raw.key, []).append(
                        _change(evidence, seq, location, raw, op, asset, observations)
                    )
            for account in accounts:
                missing = _count_refs(body, keys_of(account)) - located[account]
                unlocated_by[account] += missing
                unlocated += missing
    except (ValueError, KeyError, TypeError, IndexError) as error:
        problems.append(f"meta not understood: {error}")
        section["status"] = "INCOMPLETE"
        return section

    lines = []
    replayed = set(replay.metas)
    for key in sorted(by_line, key=lambda k: (accounts.index(k[0]), k[1])):
        lines.append(
            _line(
                key, by_line[key], windows[key[0]], replayed, unlocated_by[key[0]], target.network
            )
        )
    kind = "credit_alphanum4" if len(target.asset_code) <= 4 else "credit_alphanum12"
    for account in accounts:
        if not any(k[0] == account for k in by_line):
            lines.append(
                _line(
                    (account, kind, *asset),
                    [],
                    windows[account],
                    replayed,
                    unlocated_by[account],
                    target.network,
                )
            )
    for line in lines:
        account = line["line"]["account"]
        for op in ops.values():
            loc = op.location
            events = op.events.get(account, [])
            if loc.ledger not in windows[account] or not events:
                continue
            if any(k[0] == account for k in op.keys):
                continue
            net = sum(e["gross_in"] - e["gross_out"] for e in events)
            line["events_without_change"].append(
                {
                    "ledger": loc.ledger,
                    "tx_hash": loc.tx_hash,
                    "operation_index": loc.operation_index,
                    "events": events,
                    "events_net": net,
                    "status": "COMPENSATED_IN_OPERATION" if net == 0 else "EVENT_WITHOUT_CHANGE",
                    "detail": "asset events compensate within the operation: gross movements, "
                    "net 0, no trustline change; not «no movement»"
                    if net == 0
                    else "asset events move the account's balance but the line did not change",
                }
            )
            if net == 0:
                shown = line["gross_shown_by_events"]
                shown["in"] += sum(e["gross_in"] for e in events)
                shown["out"] += sum(e["gross_out"] for e in events)
                if line["gross_in"] is not None:
                    line["gross_in"] = shown["in"]
                    line["gross_out"] = shown["out"]
            else:
                line["contradictions"].append(
                    f"ledger {loc.ledger} tx {loc.tx_hash} op {loc.operation_index}: asset events "
                    f"net {net} without a trustline change"
                )
        _payments_without_change(evidence, line, ops, windows[account], asset)
        line["status"] = _line_status(line)
    section["lines"] = lines
    section["transactions"] = _transactions(evidence, windows, by_line)
    section["out_of_scope"] = {
        "same_code_other_issuer_changes": other_issuer_changes,
        "same_code_other_issuer_events": other_issuer_events,
        "changes_of_other_accounts_lines": other_accounts_changes,
        "detail": "counted apart, never mixed with the lines in scope",
    }
    section["unlocated_references"] = unlocated
    statuses = [c["status"] for line in lines for c in line["changes"]]
    section["summary"] = {
        "reconstructed": sum(s != "NOT_RECONSTRUCTED" for s in statuses),
        **{s.lower(): statuses.count(s) for s in CHANGE_STATUSES},
        "events_without_change": sum(
            e["status"] == "EVENT_WITHOUT_CHANGE"
            for line in lines
            for e in line["events_without_change"]
        ),
        "compensated_in_operation": sum(
            e["status"] == "COMPENSATED_IN_OPERATION"
            for line in lines
            for e in line["events_without_change"]
        ),
    }
    contradicted = any(line["status"] == "CONTRADICTED" for line in lines) or any(
        t["status"] == "CONTRADICTED" for t in section["transactions"]
    )
    if unlocated:
        problems.append(f"{unlocated} reference(s) to the lines outside the locations read")
    unexaminable = [a for a in approvals if a.status == "NOT_EXAMINABLE"]
    for approval in unexaminable:
        problems.append(
            f"approved address {approval.address} is not reconciled ({approval.reason}): the "
            "reconciliation does not cover every approved address"
        )
    if not accounts:
        problems.append("no approved G address applies in the interval")
    status: ReconciliationStatus
    if contradicted:
        status = "CONTRADICTED"
    elif unlocated or any(line["status"] == "UNRESOLVED_CHANGES" for line in lines):
        status = "UNRESOLVED_CHANGES"
    elif (
        temporal.status != "COVERED"
        or not accounts
        or unexaminable
        or any(line["status"] == "INCOMPLETE" for line in lines)
    ):
        status = "INCOMPLETE"
    elif not authenticated:
        status = "RECONCILED_UNAUTHENTICATED"
    else:
        status = "RECONCILED_IN_SCOPE"
    section["status"] = status
    return section


def _change(
    evidence: CheckpointEvidence,
    seq: int,
    location: _Location,
    raw: _Raw,
    op: _Op | None,
    asset: tuple[str, str],
    observations: Sequence[Observation],
) -> dict[str, Any]:
    anchored = (
        _anchored(evidence, seq, location.tx_hash, location.operation_index)
        if location.tx_hash is not None
        else None
    )
    before, after = _balance(raw.before), _balance(raw.after)
    events = [] if op is None else op.events.get(raw.key[0], [])
    change: dict[str, Any] = {
        "ledger": seq,
        "close_time": _iso(int(evidence.headers[seq]["scp_value"]["close_time"])),
        "order": list(location.order),
        "tx_hash": location.tx_hash,
        "location": location.where,
        "operation_index": location.operation_index,
        "operation_type": None if anchored is None else anchored.operation_type,
        "transaction_result": None if anchored is None else anchored.tx_code,
        "kind": raw.kind,
        "before": raw.before,
        "after": raw.after,
        "delta": None if before is None or after is None else after - before,
        "events": events,
        "gross_in": sum(e["gross_in"] for e in events),
        "gross_out": sum(e["gross_out"] for e in events),
        "gross_basis": "asset_events",
        "observation_refs": sorted(
            o.observation_id
            for o in observations
            if (chain := getattr(o.payload, "chain", None)) is not None
            and chain.tx_hash == location.tx_hash
            and chain.operation_index == location.operation_index
        ),
        "duplicate_representations": 0,
        "contradictory_representation": None,
    }
    change["status"], change["reasons"] = _explain(change, raw, anchored, events, asset)
    if not events:
        if change["delta"] == 0:
            change["gross_basis"] = (
                "no asset event and no balance change: no movement is shown (one compensated "
                "inside the operation without events would not be visible)"
            )
        else:
            # Only the aggregate change is visible: its entries and exits are unknown.
            change["gross_in"] = change["gross_out"] = None
            change["gross_basis"] = (
                "net only: the meta shows the aggregate change of the operation; no event "
                "decomposes it into entries and exits"
            )
    return change


OBSERVED_INITIAL_STATE = (
    "not available: the replay keeps no ledger state (its buckets were not kept; the history "
    "state names only their hashes), so no opening balance is observed"
)


def _line(
    key: LineKey,
    changes: list[dict[str, Any]],
    window: set[int],
    replayed: set[int],
    unlocated: int,
    network: str,
) -> dict[str, Any]:
    account, kind, code, issuer = key
    changes = sorted(changes, key=lambda c: (c["ledger"], *c["order"]))
    contradictions = []
    for change in changes:
        if change["contradictory_representation"] is not None:
            change["status"] = "CONTRADICTED"
            change["reasons"] = [
                *change["reasons"],
                "the same location holds two different readings of this entry",
            ]
        if change["duplicate_representations"]:
            change["reasons"] = [
                *change["reasons"],
                f"repeated {change['duplicate_representations']} more time(s) identically: "
                "counted once",
            ]
    for previous, current in pairwise(changes):
        if "NOT_ESTABLISHED" in (previous["after"]["state"], current["before"]["state"]):
            continue
        if not _same(previous["after"], current["before"]):
            contradictions.append(
                f"balance chain broken between ledger {previous['ledger']} and ledger "
                f"{current['ledger']}: {previous['after']} then {current['before']}"
            )
    inside = [c for c in changes if c["ledger"] in window]
    first = min(window) if window else None
    earlier = [c for c in changes if first is not None and c["ledger"] < first]
    opening, evidence = _opening(inside, earlier, window, replayed, unlocated)
    closing = inside[-1]["after"] if inside else opening
    deltas = [c["delta"] for c in inside]
    total = None if None in deltas else sum(deltas)
    o, c_ = _balance(opening), _balance(closing)
    net = None if o is None or c_ is None else c_ - o
    if net is not None and total is not None and net != total:
        contradictions.append(f"net variation {net} differs from the sum of changes {total}")
    shown_in = sum(c["gross_in"] or 0 for c in inside)
    shown_out = sum(c["gross_out"] or 0 for c in inside)
    complete = all(c["gross_in"] is not None for c in inside)
    return {
        "line": {
            "network": network,
            "account": account,
            "asset": {"type": kind, "code": code, "issuer": issuer},
        },
        "status": "INCOMPLETE",
        "examined_ledgers": [min(window), max(window)] if window else [],
        "opening": {
            "state": opening["state"],
            "balance": opening["balance"],
            "evidence": evidence,
        },
        "closing": closing,
        "net_variation": net,
        "sum_of_changes": total,
        "gross_in": shown_in if complete else None,
        "gross_out": shown_out if complete else None,
        "gross_shown_by_events": {"in": shown_in, "out": shown_out},
        "granularity": "one net change per entry and operation in the meta; entries and exits "
        "only as the operation's asset events show them. Gross totals are given only when "
        "every change is decomposed by events; other decompositions with movements no event "
        "shows are not excluded",
        "net_vs_gross": "net variation is closing minus opening; gross in and out come from "
        "the asset events, never from the net",
        "changes": inside,
        "outside_interval": [
            {
                "ledger": c["ledger"],
                "tx_hash": c["tx_hash"],
                "kind": c["kind"],
                "delta": c["delta"],
                "status": c["status"],
            }
            for c in changes
            if c["ledger"] not in window
        ],
        "events_without_change": [],
        "contradictions": contradictions,
    }


def _opening(
    inside: list[dict[str, Any]],
    earlier: list[dict[str, Any]],
    window: set[int],
    replayed: set[int],
    unlocated: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The state of the line when the examined window starts, and its evidence. It is never
    observed (no initial state is kept); it is reconstructed only from a complete sequence
    of changes: the change that fixes it, and no other change of the entry in the derived
    meta between that change and the window's start. A later creation alone never shows
    an absence at the start."""
    evidence: dict[str, Any] = {"observed_initial_state": OBSERVED_INITIAL_STATE}
    if inside:
        anchor, state, span = inside[0], inside[0]["before"], (min(window), inside[0]["ledger"])
        how = (
            f"the {anchor['kind']} in ledger {anchor['ledger']} (tx {anchor['tx_hash']}, "
            f"operation {anchor['operation_index']}) has this state before it"
        )
    elif earlier:
        anchor, state = earlier[-1], earlier[-1]["after"]
        span = (anchor["ledger"], max(window))
        how = f"the {anchor['kind']} in ledger {anchor['ledger']} left this state"
    else:
        evidence.update(
            kind="not_established",
            detail="no change of the line in the replay: its state is not established (an "
            "unknown state is never read as zero or as absent)",
        )
        return dict(_UNKNOWN), evidence
    missing = sorted(set(range(span[0], span[1] + 1)) - replayed)
    gaps = []
    if state["state"] == "NOT_ESTABLISHED":
        gaps.append("the meta holds no state before that change")
    if missing:
        gaps.append(f"ledgers {missing} are not in the replay")
    if unlocated:
        gaps.append(
            f"{unlocated} reference(s) to the line lie outside the locations read, so the "
            "sequence of its changes is not shown complete"
        )
    if gaps:
        evidence.update(
            kind="inferred_from_a_change_only"
            if state["state"] != "NOT_ESTABLISHED"
            else "not_established",
            rejected_state=None if state["state"] == "NOT_ESTABLISHED" else dict(state),
            detail="not established: " + "; ".join(gaps),
        )
        return dict(_UNKNOWN), evidence
    evidence.update(
        kind="reconstructed_from_changes",
        detail=f"{how}, and the derived meta shows no other change of the entry in ledgers "
        f"{span[0]}-{span[1]}"
        + (
            ": the line did not exist when the window starts (its creation comes later, "
            "and nothing precedes it); reconstructed, not observed"
            if state["state"] == "ABSENT"
            else ""
        ),
        depends_on="the derived replay meta holding every change of the entry in those "
        "ledgers: the stream holds exactly the checkpoint's ledgers and anchored "
        "transactions, but the entry changes themselves are not authenticated",
    )
    return dict(state), evidence


def _payments_without_change(
    evidence: CheckpointEvidence,
    line: dict[str, Any],
    ops: dict[tuple[int, int, int], _Op],
    in_interval: set[int],
    asset: tuple[str, str],
) -> None:
    """An anchored successful payment of the asset that moves the account's balance must
    show in the line: its absence from the meta contradicts the anchored history."""
    account = line["line"]["account"]
    for op in ops.values():
        loc = op.location
        if (
            loc.ledger not in in_interval
            or loc.tx_hash is None
            or any(k[0] == account for k in op.keys)
        ):
            continue
        anchored = _anchored(evidence, loc.ledger, loc.tx_hash, loc.operation_index)
        if not anchored.succeeded or anchored.operation_type != "payment":
            continue
        assert anchored.operation is not None
        payment = anchored.operation["body"]["payment"]
        if _credit(payment["asset"]) != asset:
            continue
        amount = int(payment["amount"])
        net = (amount if _base(payment["destination"]) == account else 0) - (
            amount if anchored.source is not None and _base(anchored.source) == account else 0
        )
        if net:
            line["contradictions"].append(
                f"ledger {loc.ledger} tx {loc.tx_hash} op {loc.operation_index}: the anchored "
                f"payment moves {net} for the account but the line did not change"
            )


def _line_status(line: dict[str, Any]) -> str:
    statuses = {c["status"] for c in line["changes"]}
    if line["contradictions"] or "CONTRADICTED" in statuses:
        return "CONTRADICTED"
    if statuses & {"UNEXPLAINED", "AMBIGUOUS"}:
        return "UNRESOLVED_CHANGES"
    if line["opening"]["state"] == "NOT_ESTABLISHED" or "NOT_RECONSTRUCTED" in statuses:
        return "INCOMPLETE"
    return "RECONCILED"


def _transactions(
    evidence: CheckpointEvidence,
    windows: dict[str, set[int]],
    by_line: dict[LineKey, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Every anchored transaction that names an approved address in a ledger where its
    approval applies, with its result and its fee in XLM, kept apart from the asset's
    changes."""
    changed: dict[str, int] = {}
    for changes in by_line.values():
        for c in changes:
            if c["tx_hash"] is not None:
                changed[c["tx_hash"]] = changed.get(c["tx_hash"], 0) + 1
    out = []
    for seq in sorted(set().union(*windows.values())) if windows else []:
        ledger = evidence.ledgers.get(seq)
        if ledger is None:
            continue
        for tx_hash, envelope in zip(ledger.tx_hashes, ledger.envelopes, strict=True):
            named = [a for a, w in windows.items() if seq in w and _mentions(envelope, a)]
            if not named:
                continue
            anchored = _anchored(evidence, seq, tx_hash, None)
            count = changed.get(tx_hash, 0)
            if not anchored.succeeded:
                status = "CONTRADICTED" if count else "NO_EXECUTED_MOVEMENT"
            else:
                status = "LINE_CHANGED" if count else "NO_ASSET_CHANGE"
            out.append(
                {
                    "ledger": seq,
                    "tx_hash": tx_hash,
                    "accounts": named,
                    "result": anchored.tx_code,
                    "asset_line_changes": count,
                    "fee_xlm_stroops": anchored.fee_charged,
                    "fee_source": anchored.fee_source,
                    "fee": "charged in XLM, reported apart; never part of the asset's changes",
                    "status": status,
                }
            )
    return out
