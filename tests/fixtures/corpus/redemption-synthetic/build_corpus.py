"""Generate the synthetic redemption corpus. Standard library only.

Run from the repository root: ``python3 tests/fixtures/corpus/redemption-synthetic/build_corpus.py``.
A test checks that the committed files equal this script's output.

Three versions. Without arguments it writes this directory: profile
``fund-redemption-synthetic@1.3.0``, historical, evaluated by the retired 0.4.0 and kept to
reproduce it. With ``--profile=1.4.0`` it writes ``../redemption-synthetic-1.4.0``: the same
raw files, observations, coverage and hand-written expected results, under profile 1.4.0,
which only adds ``quarantine_scope`` to the chain requirement (and says so in its
disclaimer). With ``--profile=1.5.0`` it writes ``../redemption-synthetic-1.5.0``, which adds
the declared ``quarantine_policy``. With ``--profile=1.6.0`` it writes
``../redemption-synthetic-1.6.0``, which adds ``absence_needs_chain_scope``: the
chain certificates of this corpus declare no chain scope, so the four scenarios whose absence
of settlement rested on them change as written by hand in ``V16_EXPECTED``. With
``--profile=1.7.0`` it writes ``../redemption-synthetic-1.7.0``, which adds
``completeness_needs_coverage``: an equal or short burn comparison needs
coverage showing the burns observed are all of them, so ``_v17`` turns every equal burn of the
1.6.0 expectations into UNKNOWN (the corpus has no short or excess burn) and aggregates. The expectations do not change because this
corpus has no chain effect and no certificate listing quarantined records, the only inputs
the 0.5.0 to 0.7.0 rules read. Expected results are
written here by hand, per scenario, from the approved semantics; nothing is computed by the engine.
Everything is synthetic: accounts, hashes and ledgers are invented.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TENANT = "tenant-synthetic-demo"
OP = "RED-0001"
INSTRUMENT = "syn:fund:DEMO-A:class-a"
REP = "rep-stellar-testnet-demoa"
ISSUER = "GDMYHWUG6BLHEGQSTMCAGUFUZFRZ6XGXJSHDKMDUZ6IRCKJ4FGZCCZVZ"
INVESTOR = "GA2JRO6WKTHMJTH7TDAXHNDHWT5SF72VKABPO2G6RL5BCGKTNIPIBEKW"
ACCOUNT = "acct-pseudo-0001"
PRICE_REF = "price:DEMO-A:2026-10-04"
V17 = "--profile=1.7.0" in sys.argv
V16 = "--profile=1.6.0" in sys.argv or V17  # 1.7.0 keeps everything 1.6.0 declares
V15 = "--profile=1.5.0" in sys.argv or V16  # 1.6.0 keeps everything 1.5.0 declares
V14 = "--profile=1.4.0" in sys.argv or V15  # 1.5.0 keeps everything 1.4.0 declares
VERSION = "1.7.0" if V17 else "1.6.0" if V16 else "1.5.0" if V15 else "1.4.0" if V14 else "1.3.0"
PROFILE_REF = f"fund-redemption-synthetic@{VERSION}"
RULES_REF = f"redemption-synthetic-rules@{VERSION}"
OUT = ROOT.parent / f"redemption-synthetic-{VERSION}" if V14 else ROOT
CORPUS_ID = f"corpus-redemption-synthetic-{VERSION}" if V14 else "corpus-redemption-synthetic"
QUARANTINE_POLICY = {
    "evidence": "never_credits_fulfilment",
    "blocking": "only_controls_whose_conclusion_it_could_change",
    "certificates": "active_until_explicit_supersession_within_chain_scope",
    "classification": "relied_on_only_when_supported_by_the_snapshot",
    "unapproved_parties": "outside_declared_scope_unless_linked_associated_or_unresolved",
}
JOURNAL = "ta-journal-demoa"
MAPPINGS = {
    "ta-synthetic": ("ta-redemption-frozen@1.0.0", "frozen-json-synthetic@1.0.0"),
    "bank-synthetic": ("bank-redemption-frozen@1.0.0", "frozen-json-synthetic@1.0.0"),
    "pricing-synthetic": ("pricing-frozen-synthetic@1.0.0", "frozen-json-synthetic@1.0.0"),
    "stellar-testnet-frozen": ("stellar-frozen-synthetic@1.0.0", "stellar-frozen-parser@1.0.0"),
}

# Instants (UTC). Each stage is the knowledge cut, valid_at and clock of its snapshots.
ACCEPTED = "2026-10-05T10:00:00Z"  # TA journal sequence 1005
DUE = "2026-10-07T10:00:00Z"  # exactly 48 hours later
S0 = "2026-10-05T11:30:00Z"  # a cancellation is known, before its retraction
S1 = "2026-10-06T09:00:00Z"  # before the due time; no payment yet
S2 = "2026-10-06T16:00:00Z"  # payment on time is known
S3 = "2026-10-07T12:00:00Z"  # two hours after the due time
S4 = "2026-10-08T10:00:00Z"  # a late payment or a late-known cancellation is known
STAGES = {"s0": S0, "s1": S1, "s2": S2, "s3": S3, "s4": S4}


def units(atoms: str) -> dict[str, object]:
    return {"atoms": atoms, "scale": 7, "unit": "FUND_SHARE"}


def usd(atoms: str) -> dict[str, object]:
    return {"atoms": atoms, "scale": 2, "unit": "USD"}


def dump(document: object) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


FILES: dict[str, str] = {}


def raw(name: str, source_id: str, record: dict[str, object]) -> tuple[str, str]:
    text = dump({"synthetic": True, "source_id": source_id, "record": record})
    path = f"raw/{name}.json"
    FILES[path] = text
    return sha(text), path


# --------------------------------------------------------------------- observations


def observation(
    oid: str,
    fact_type: str,
    source_id: str,
    record_key: str,
    valid_time: str,
    recorded_at: str,
    payload: dict[str, object] | None,
    *,
    revision: int = 1,
    operation_ref: str | None = OP,
    supersedes: str | None = None,
    representation: str | None = None,
) -> dict[str, object]:
    mapping_ref, parser_ref = MAPPINGS[source_id]
    record = {"record_key": record_key, "revision": revision, "valid_time": valid_time}
    record.update({"payload": payload} if payload else {"retracted": True})
    digest, path = raw(oid, source_id, record)
    return {
        "schema_version": "1.0",
        "observation_id": oid,
        "tenant_id": TENANT,
        "kind": "assertion" if payload else "retraction",
        "fact_type": fact_type,
        "instrument_id": INSTRUMENT,
        "representation_id": representation,
        "operation_ref": operation_ref,
        "source": {"source_id": source_id, "record_key": record_key, "revision": revision},
        "valid_time": valid_time,
        "recorded_at": recorded_at,
        "provenance": {
            "raw_sha256": digest,
            "raw_locator": path,
            "parser_ref": parser_ref,
            "mapping_ref": mapping_ref,
        },
        "supersedes": supersedes,
        "payload": payload,
        "synthetic": True,
    }


def request(
    oid: str,
    *,
    qty: str = "1000000000",
    price: str = "10000",
    due: str = DUE,
    settlement_ref: str | None = None,
):
    return observation(
        oid,
        "redemption_accepted",
        "ta-synthetic",
        "RED-0001",
        ACCEPTED,
        "2026-10-05T10:05:00Z",
        {
            "payload_type": "redemption_accepted",
            "account_ref": ACCOUNT,
            "units": units(qty),
            "price_ref": PRICE_REF,
            "price_per_unit": usd(price),
            "accepted_at": ACCEPTED,
            "payment_due_at": due,
            "journal_id": JOURNAL,
            "sequence": 1005,
            "settlement_ref": settlement_ref,
        },
    )


def position(
    oid: str,
    qty: str = "10000000000",
    *,
    reserved: str = "0",
    as_of: str = ACCEPTED,
    sequence: int = 1004,
    recorded_at: str = "2026-10-05T10:05:00Z",
):
    return observation(
        oid,
        "position_held",
        "ta-synthetic",
        f"POS-{sequence}",
        as_of,
        recorded_at,
        {
            "payload_type": "position_held",
            "account_ref": ACCOUNT,
            "units": units(qty),
            "reserved": units(reserved),
            "as_of": as_of,
            "journal_id": JOURNAL,
            "sequence": sequence,
        },
    )


def change(
    oid: str,
    change_ref: str,
    *,
    hold: str,
    res: str,
    at: str,
    sequence: int,
    request_ref: str | None = None,
):
    return observation(
        oid,
        "position_changed",
        "ta-synthetic",
        change_ref,
        at,
        at.replace(":00Z", ":30Z"),
        {
            "payload_type": "position_changed",
            "account_ref": ACCOUNT,
            "change_ref": change_ref,
            "holding_delta": units(hold),
            "reserved_delta": units(res),
            "effective_at": at,
            "journal_id": JOURNAL,
            "sequence": sequence,
            "request_ref": request_ref,
        },
        operation_ref=request_ref,
    )


def payment(
    oid: str,
    valid_time: str,
    recorded_at: str,
    amount: str = "1000000",
    *,
    account: str = ACCOUNT,
    operation_ref: str | None = OP,
    record_key: str = "PAY-RED-0001",
    revision: int = 1,
    supersedes: str | None = None,
):
    return observation(
        oid,
        "cash_settled",
        "bank-synthetic",
        record_key,
        valid_time,
        recorded_at,
        {"payload_type": "cash_settled", "account_ref": account, "payment_ref": "PAY-RED-0001", "amount": usd(amount)},
        operation_ref=operation_ref,
        revision=revision,
        supersedes=supersedes,
    )


def cancellation(
    oid: str,
    authorized_by: str = "ta-ops-synthetic",
    at: str = "2026-10-05T11:00:00Z",
    recorded_at: str = "2026-10-05T11:05:00Z",
    *,
    revision: int = 1,
    supersedes: str | None = None,
):
    return observation(
        oid,
        "redemption_cancelled",
        "ta-synthetic",
        "CAN-RED-0001",
        at,
        recorded_at,
        {
            "payload_type": "redemption_cancelled",
            "account_ref": ACCOUNT,
            "cancelled_at": at,
            "authorized_by": authorized_by,
        },
        revision=revision,
        supersedes=supersedes,
    )


def burn(oid: str, label: str, at: str, recorded_at: str):
    tx = sha(f"invaria:synthetic:tx:{label}")
    return observation(
        oid,
        "token_movement",
        "stellar-testnet-frozen",
        f"{tx}:0",
        at,
        recorded_at,
        {
            "payload_type": "token_movement",
            "from_address": INVESTOR,
            "to_address": ISSUER,
            "units": units("1000000000"),
            "chain": {
                "network": "stellar:testnet",
                "ledger": 1100042,
                "tx_hash": tx,
                "operation_index": 0,
                "tx_successful": True,
            },
        },
        representation=REP,
    )


RQ1 = request("obs-RQ1")
PS1 = position("obs-PS1")
PR1 = observation(
    "obs-PR1",
    "price_approved",
    "pricing-synthetic",
    PRICE_REF,
    "2026-10-04T22:00:00Z",
    "2026-10-04T22:05:00Z",
    {"payload_type": "price_approved", "price_ref": PRICE_REF, "price_per_unit": usd("10000")},
    operation_ref=None,
)
UR1 = observation(
    "obs-UR1",
    "units_registered",
    "ta-synthetic",
    "REG-RED-0001",
    "2026-10-05T14:00:00Z",
    "2026-10-05T14:05:00Z",
    {"payload_type": "units_registered", "account_ref": ACCOUNT, "units": units("1000000000")},
)
BN1 = burn("obs-BN1", "redemption-burn-0001", "2026-10-05T14:30:00Z", "2026-10-05T14:31:00Z")
BN_EARLY = burn("obs-BN-EARLY", "redemption-burn-early", "2026-10-05T09:30:00Z", "2026-10-05T09:31:00Z")
PY1 = payment("obs-PY1", "2026-10-06T15:00:00Z", "2026-10-06T15:30:00Z")
PY1_DUP = {**PY1, "observation_id": "obs-PY1-resent", "recorded_at": "2026-10-06T15:45:00Z"}
FILES["raw/obs-PY1-resent.json"] = FILES["raw/obs-PY1.json"]
PY1_DUP["provenance"] = {**PY1["provenance"], "raw_locator": "raw/obs-PY1-resent.json"}
PY_AT_DUE = payment("obs-PY-DUE", DUE, "2026-10-07T10:30:00Z")
PY_LATE = payment("obs-PY-LATE", "2026-10-08T09:00:00Z", "2026-10-08T09:30:00Z")
PY_SHORT = payment("obs-PY-SHORT", "2026-10-06T15:00:00Z", "2026-10-06T15:30:00Z", "995000")
PY_WRONG = payment(
    "obs-PY-WRONG", "2026-10-06T15:00:00Z", "2026-10-06T15:30:00Z", account="acct-pseudo-0002"
)
PY_UNLINKED = payment("obs-PY-UNLINKED", "2026-10-06T15:00:00Z", "2026-10-06T15:30:00Z", operation_ref=None)
RQ_SETTLE = request("obs-RQ-SETTLE", settlement_ref="PAY-RED-0001")
PY_FIX = payment(
    "obs-PY-FIX",
    "2026-10-06T15:00:00Z",
    "2026-10-07T11:00:00Z",
    revision=2,
    supersedes="obs-PY-SHORT",
)
PY_OTHER_OP = payment(
    "obs-PY-OTHER-OP",
    "2026-10-06T16:00:00Z",
    "2026-10-06T16:30:00Z",
    operation_ref="RED-0002",
    record_key="PAY-RED-0002",
)
RQ_PRICE = request("obs-RQ-PRICE", price="10100")
RQ_INEXACT = request("obs-RQ-INEXACT", qty="1000000001")
RQ_BADDUE = request("obs-RQ-BADDUE", due="2026-10-07T17:00:00Z")
PS_LOW = position("obs-PS-LOW", "500000000")
# Position reconstruction: a snapshot the evening before, then journal changes up to the
# acceptance (sequence 1005). The request's own reservation (1006) is after it.
PS_OLD = position(
    "obs-PS-OLD", as_of="2026-10-04T18:00:00Z", sequence=900, recorded_at="2026-10-04T18:05:00Z"
)
PS_OLD_SMALL = position(
    "obs-PS-OLD-SMALL",
    "1200000000",
    as_of="2026-10-04T18:00:00Z",
    sequence=900,
    recorded_at="2026-10-04T18:05:00Z",
)
CH_SUB = change("obs-CH-0950", "J-0950", hold="500000000", res="0", at="2026-10-05T09:00:00Z", sequence=950)
CH_RES = change(
    "obs-CH-0960", "J-0960", hold="0", res="300000000", at="2026-10-05T09:30:00Z", sequence=960, request_ref="RED-0000"
)
CH_RES_BIG = change(
    "obs-CH-0961", "J-0961", hold="0", res="400000000", at="2026-10-05T09:30:00Z", sequence=961, request_ref="RED-0000"
)
CH_OWN = change(
    "obs-CH-1006", "J-1006", hold="0", res="1000000000", at=ACCEPTED, sequence=1006, request_ref=OP
)
PS_SAME_SEQ = position("obs-PS-SAME-SEQ", sequence=1005)
CN1 = cancellation("obs-CN1")
CN_UNAUTH = cancellation("obs-CN-UNAUTH", "someone-else")
CN_LATE = cancellation("obs-CN-LATE", at="2026-10-08T00:00:00Z", recorded_at="2026-10-08T00:05:00Z")
CN_KNOWN_LATE = cancellation("obs-CN-KNOWN-LATE", at="2026-10-06T08:00:00Z", recorded_at="2026-10-07T15:00:00Z")
CN1_RETRACTED = observation(
    "obs-CN1-R",
    "redemption_cancelled",
    "ta-synthetic",
    "CAN-RED-0001",
    "2026-10-05T11:00:00Z",
    "2026-10-05T12:00:00Z",
    None,
    revision=2,
    supersedes="obs-CN1",
)
CN1_REV2_OTHER = cancellation(
    "obs-CN1-X",
    "ta-ops-synthetic-2",
    recorded_at="2026-10-05T12:01:00Z",
    revision=2,
    supersedes="obs-CN1",
)
RA1 = observation(
    "obs-RA1",
    "redemption_reactivated",
    "ta-synthetic",
    "REA-RED-0001",
    "2026-10-05T13:00:00Z",
    "2026-10-05T13:05:00Z",
    {
        "payload_type": "redemption_reactivated",
        "account_ref": ACCOUNT,
        "reactivated_at": "2026-10-05T13:00:00Z",
        "authorized_by": "ta-ops-synthetic",
    },
)
RQ1_RETRACTED = observation(
    "obs-RQ1-R",
    "redemption_accepted",
    "ta-synthetic",
    "RED-0001",
    ACCEPTED,
    "2026-10-05T12:00:00Z",
    None,
    revision=2,
    supersedes="obs-RQ1",
)

BASE = [RQ1, PS1, PR1, UR1, BN1]
TIMELINES = {
    "main": [*BASE, PY1],
    "at-due": [*BASE, PY_AT_DUE],
    "unpaid-then-late": [*BASE, PY_LATE],
    "short-pay": [*BASE, PY_SHORT],
    "price-mismatch": [RQ_PRICE, PS1, PR1, UR1, BN1, PY1],
    "over-position": [RQ1, PS_LOW, PR1, UR1, BN1, PY1],
    "inexact": [RQ_INEXACT, PS1, PR1],
    "bad-due": [RQ_BADDUE, PS1, PR1, UR1, BN1],
    "position-reconstructed": [RQ1, PS_OLD, CH_SUB, CH_RES, CH_OWN, PR1, UR1, BN1, PY1],
    "position-short": [RQ1, PS_OLD_SMALL, CH_RES_BIG, PR1],
    "position-same-sequence": [RQ1, PS_SAME_SEQ, PR1, UR1, BN1, PY1],
    "early-burn": [RQ1, PS1, PR1, UR1, BN_EARLY, PY1],
    "cancelled": [RQ1, PS1, PR1, CN1],
    "cancelled-paid": [RQ1, PS1, PR1, CN1, PY1],
    "cancelled-burned": [RQ1, PS1, PR1, CN1, BN1],
    "cancelled-after-due": [RQ1, PS1, PR1, CN_LATE],
    "cancel-known-late": [RQ1, PS1, PR1, CN_KNOWN_LATE],
    "cancel-unauthorized": [RQ1, PS1, PR1, CN_UNAUTH],
    "cancel-retracted": [RQ1, PS1, PR1, CN1, CN1_RETRACTED, UR1, BN1, PY1],
    "cancel-retract-conflict": [RQ1, PS1, PR1, CN1, CN1_RETRACTED, CN1_REV2_OTHER],
    "reactivated": [RQ1, PS1, PR1, CN1, RA1],
    "retracted": [RQ1, RQ1_RETRACTED, PS1, PR1],
    "duplicate": [*BASE, PY1, PY1_DUP],
    "wrong-recipient": [*BASE, PY_WRONG],
    "unlinked-candidate": [RQ_SETTLE, PS1, PR1, UR1, BN1, PY_UNLINKED],
    "unlinked-not-candidate": [*BASE, PY_UNLINKED],
    "retracted-paid": [RQ1, RQ1_RETRACTED, PS1, PR1, PY1],
    "cancelled-early-burn": [RQ1, PS1, PR1, CN1, BN_EARLY],
    "short-pay-corrected": [*BASE, PY_SHORT, PY_FIX],
    "settlement-ref-collision": [RQ_SETTLE, PS1, PR1, UR1, BN1, PY_UNLINKED, PY_OTHER_OP],
}

# ------------------------------------------------------------------------ coverage

FACTS = {
    "ta-synthetic": [
        "redemption_accepted",
        "redemption_cancelled",
        "redemption_reactivated",
        "position_held",
        "position_changed",
        "units_registered",
    ],
    "bank-synthetic": ["cash_settled"],
    "pricing-synthetic": ["price_approved"],
    "stellar-testnet-frozen": ["token_movement"],
}
SHORT = {
    "ta-synthetic": "ta",
    "bank-synthetic": "bank",
    "pricing-synthetic": "price",
    "stellar-testnet-frozen": "chain",
}
SCOPES = {
    "ta-synthetic": {"accounts": [ACCOUNT], "currencies": None, "filters": []},
    "bank-synthetic": {"accounts": [ACCOUNT], "currencies": ["USD"], "filters": []},
}


def certificate(
    source_id: str,
    stage: str,
    *,
    suffix: str = "",
    start: str = "2026-10-04T00:00:00Z",
    end: str | None = None,
    gaps: list[dict[str, str]] | None = None,
    filters: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    cid = f"cov-{SHORT[source_id]}-{stage}{suffix}"
    end = end or STAGES[stage]
    digest, _ = raw(cid, source_id, {"export": cid, "interval": [start, end]})
    level = "provider_claimed" if source_id == "stellar-testnet-frozen" else "internally_checked"
    document: dict[str, object] = {
        "schema_version": "1.0",
        "coverage_id": cid,
        "tenant_id": TENANT,
        "source_id": source_id,
        "fact_types": FACTS[source_id],
        "instrument_id": INSTRUMENT,
        "interval": {"start": start, "end": end},
        "ledger_range": None,
        "level": level,
        "method": "synthetic fixture: full export",
        "records_received": 1,
        "records_quarantined": 0,
        "gaps": gaps or [],
        "raw_sha256": [digest],
        "recorded_at": STAGES[stage],
        "synthetic": True,
    }
    if source_id in SCOPES:
        document["scope"] = {**SCOPES[source_id], "filters": filters or []}
    return document


CERTS = [certificate(src, stage) for stage in STAGES for src in FACTS]
CERTS += [
    certificate("bank-synthetic", "s3", suffix="-partial", end="2026-10-06T00:00:00Z"),
    certificate("bank-synthetic", "s3", suffix="-to-due", end=DUE),
    certificate(
        "bank-synthetic",
        "s3",
        suffix="-gap",
        gaps=[{"start": "2026-10-06T12:00:00Z", "end": "2026-10-06T13:00:00Z"}],
    ),
    certificate("bank-synthetic", "s2", suffix="-partial", end="2026-10-06T12:00:00Z"),
    certificate(
        "bank-synthetic",
        "s3",
        suffix="-filtered-ok",
        filters=[
            {"field": "account_ref", "values": [ACCOUNT]},
            {"field": "currency", "values": ["USD"]},
        ],
    ),
    certificate(
        "bank-synthetic", "s3", suffix="-filtered-status", filters=[{"field": "status", "values": ["SETTLED"]}]
    ),
    certificate("ta-synthetic", "s2", suffix="-late-start", start="2026-10-05T00:00:00Z"),
]


def stage_certs(stage: str, **replace: str) -> list[str]:
    ids = {SHORT[src]: f"cov-{SHORT[src]}-{stage}" for src in FACTS}
    ids.update(replace)
    return sorted(ids.values())


# ----------------------------------------------------------------------- scenarios

CONTROLS = {
    "due": "redemption.declared_due_consistency",
    "price": "redemption.price_vs_approved",
    "position": "redemption.units_within_position",
    "cash": "redemption.cash_vs_expected",
    "deadline": "redemption.payment_deadline",
    "ta": "redemption.ta_units_vs_request",
    "burn": "redemption.burn_vs_request",
    "valid": "redemption.cancellation_valid",
    "settle": "redemption.no_settlement_after_cancellation",
}
PASS = ("PASS", "EXACT_MATCH")
NO_CANCEL = ("NOT_APPLICABLE", "NO_CANCELLATION")
GONE = ("NOT_APPLICABLE", "EXTINGUISHED_BY_CANCELLATION")
NOT_DUE = ("UNKNOWN", "PAYMENT_NOT_DUE")
MISSING = ("UNKNOWN", "MISSING_EVIDENCE")
SHORT_COVERAGE = ("UNKNOWN", "INSUFFICIENT_COVERAGE")
SETTLED = ["obs-BN1", "obs-PR1", "obs-PS1", "obs-RQ1", "obs-UR1"]
CANCELLED = {"cash": GONE, "deadline": GONE, "ta": GONE, "burn": GONE, "valid": PASS, "settle": PASS}
PENDING = {"cash": NOT_DUE, "deadline": NOT_DUE}
UNSETTLED_PENDING = {"cash": NOT_DUE, "deadline": NOT_DUE, "ta": MISSING, "burn": MISSING}
MISSED = {"cash": MISSING, "deadline": ("FAIL", "PAYMENT_MISSED")}
RECONSTRUCTED = ["obs-CH-0950", "obs-CH-0960", "obs-CH-1006"]


def expected(
    result: str, effective: list[str], state: str | None = None, **outcomes: tuple
) -> dict[str, object]:
    """All controls PASS and both cancellation controls NO_CANCELLATION unless overridden."""
    base = {name: PASS for name in CONTROLS} | {"valid": NO_CANCEL, "settle": NO_CANCEL}
    unknown = set(outcomes) - set(CONTROLS)
    assert not unknown, unknown
    base.update(outcomes)
    controls = []
    for name, control_id in CONTROLS.items():
        outcome = base[name]
        controls.append(
            {
                "control_id": control_id,
                "mandatory": True,
                "status": outcome[0],
                "reason_code": outcome[1],
                "delta": outcome[2] if len(outcome) == 3 else None,
            }
        )
    document: dict[str, object] = {
        "result": result,
        "controls": controls,
        "effective_observation_ids": sorted(effective),
    }
    if state is not None:
        document["operation_state"] = state
    return document


def scenario(
    sid: str,
    title: str,
    timeline: str,
    at: str,
    coverage: list[str],
    expect: dict[str, object],
    rationale: str,
    *,
    mode: str = "as_known",
    valid_at: str | None = None,
) -> dict[str, object]:
    members = sorted(o["observation_id"] for o in TIMELINES[timeline] if o["recorded_at"] <= at)
    return {
        "scenario_id": sid,
        "title": title,
        "timeline_id": timeline,
        "query_mode": mode,
        "snapshot": {
            "schema_version": "1.0",
            "snapshot_id": f"snap-{sid}",
            "tenant_id": TENANT,
            "operation_ref": OP,
            "valid_at": valid_at or at,
            "known_at": at,
            "evaluation_clock": at,
            "observation_ids": members,
            "coverage_ids": coverage,
            "identity_link_ids": ["link-0001"],
            "profile_ref": PROFILE_REF,
            "rules_ref": RULES_REF,
            "mapping_refs": sorted(m for m, _ in MAPPINGS.values()),
        },
        "expected": expect,
        "rationale": rationale,
        "acceptance_refs": ["INV-011a"],
    }


SCENARIOS = [
    # ---------------------------------------------------------------- deadline
    scenario(
        "RD-PENDING", "Burned and registered; no payment yet, before the due time", "main", S1,
        stage_certs("s1"), expected("UNKNOWN", SETTLED, **PENDING),
        "Due 2026-10-07T10:00Z, clock 2026-10-06T09:00Z: absence is not yet a breach.",
    ),
    scenario(
        "RD-PAID", "Full redemption settled on time", "main", S2, stage_certs("s2"),
        expected("MATCH", [*SETTLED, "obs-PY1"]),
        "Request, approved price, position before acceptance, USD 10,000.00 paid "
        "2026-10-06T15:00Z, TA debit and burn of 100: every control passes with coverage.",
    ),
    scenario(
        "RD-PAID-AT-DUE", "Payment effective exactly at the due time", "at-due", S3,
        stage_certs("s3"), expected("MATCH", [*SETTLED, "obs-PY-DUE"]),
        "A payment effective exactly at the due instant is on time.",
    ),
    scenario(
        "RD-MISSED", "No payment after the due time, with full bank coverage", "unpaid-then-late",
        S3, stage_certs("s3"), expected("BREAK", SETTLED, **MISSED),
        "The bank export [2026-10-04T00:00Z, 2026-10-07T12:00Z) for the account in USD, "
        "unfiltered, contains the due instant: absence of the payment is demonstrated.",
    ),
    scenario(
        "RD-MISSED-REPLAYED", "RD-MISSED replayed after the late payment arrived",
        "unpaid-then-late", S3, stage_certs("s3"), expected("BREAK", SETTLED, **MISSED),
        "Same closed snapshot as RD-MISSED consulted after RD-LATE: the late payment is not "
        "a member, so the history reproduces.",
        mode="as_known_now",
    ),
    scenario(
        "RD-MISSED-NO-COVERAGE", "Bank coverage stops a day early", "unpaid-then-late", S3,
        stage_certs("s3", bank="cov-bank-s3-partial"),
        expected("UNKNOWN", SETTLED, cash=MISSING, deadline=SHORT_COVERAGE),
        "The bank export ends 2026-10-06T00:00Z: absence cannot be demonstrated.",
    ),
    scenario(
        "RD-MISSED-COVERAGE-TO-DUE", "Bank coverage ends exactly at the due time",
        "unpaid-then-late", S3, stage_certs("s3", bank="cov-bank-s3-to-due"),
        expected("UNKNOWN", SETTLED, cash=MISSING, deadline=SHORT_COVERAGE),
        "Coverage intervals are half-open: [.., 2026-10-07T10:00Z) excludes the due instant, "
        "when an on-time payment could still be effective.",
    ),
    scenario(
        "RD-MISSED-BANK-GAP", "Bank coverage declares a gap", "unpaid-then-late", S3,
        stage_certs("s3", bank="cov-bank-s3-gap"),
        expected("UNKNOWN", SETTLED, cash=MISSING, deadline=SHORT_COVERAGE),
        "A declared gap (2026-10-06T12:00Z-13:00Z) means the export is incomplete.",
    ),
    scenario(
        "RD-LATE", "Payment arrives after the due time", "unpaid-then-late", S4,
        stage_certs("s4"),
        expected("BREAK", [*SETTLED, "obs-PY-LATE"], deadline=("FAIL", "PAYMENT_LATE")),
        "USD 10,000.00 effective 2026-10-08T09:00Z, 23 hours late: settlement completes and "
        "the breach of the deadline persists.",
    ),
    scenario(
        "RD-BAD-DUE", "TA declares another due time; not yet due either way", "bad-due", S1,
        stage_certs("s1"),
        expected("UNKNOWN", ["obs-BN1", "obs-PR1", "obs-PS1", "obs-RQ-BADDUE", "obs-UR1"],
                 due=("UNKNOWN", "DEADLINE_DATA_CONFLICT"), **PENDING),
        "payment_due_at 2026-10-07T17:00Z disagrees with accepted_at + 48h: a data conflict, "
        "not a financial breach.",
    ),
    scenario(
        "RD-BAD-DUE-MISSED", "TA declares another due time; no payment by the computed one",
        "bad-due", S3, stage_certs("s3"),
        expected("BREAK", ["obs-BN1", "obs-PR1", "obs-PS1", "obs-RQ-BADDUE", "obs-UR1"],
                 due=("UNKNOWN", "DEADLINE_DATA_CONFLICT"), **MISSED),
        "accepted_at is credited, so the payment is judged against the computed due time "
        "(10:00Z): missed, with coverage. The data conflict stays visible as UNKNOWN.",
    ),
    # ---------------------------------------------------------------- amounts
    scenario(
        "RD-SHORT-PAY", "Payment on time but USD 50.00 short", "short-pay", S2, stage_certs("s2"),
        expected("BREAK", [*SETTLED, "obs-PY-SHORT"], cash=("FAIL", "CASH_AMOUNT_MISMATCH", usd("-5000"))),
        "USD 9,950.00 paid against 100 x 100.00 = 10,000.00: delta USD -50.00.",
    ),
    scenario(
        "RD-PRICE-MISMATCH", "Request states another price", "price-mismatch", S2,
        stage_certs("s2"),
        expected("BREAK", ["obs-BN1", "obs-PR1", "obs-PS1", "obs-PY1", "obs-RQ-PRICE", "obs-UR1"],
                 price=("FAIL", "PRICE_MISMATCH", usd("100"))),
        "The request says USD 101.00; the approved price for its price_ref is 100.00.",
    ),
    scenario(
        "RD-INEXACT", "Expected payment not exact at the cash scale", "inexact", S1,
        stage_certs("s1"),
        expected("UNKNOWN", ["obs-PR1", "obs-PS1", "obs-RQ-INEXACT"],
                 cash=("UNKNOWN", "INEXACT_AMOUNT"), deadline=NOT_DUE, ta=MISSING, burn=MISSING),
        "100.0000001 units x USD 100.00 = 10,000.00001: not representable at scale 2.",
    ),
    # ---------------------------------------------------------------- position
    scenario(
        "RD-OVER-POSITION", "Requested units exceed the available position", "over-position", S2,
        stage_certs("s2"),
        expected("BREAK", ["obs-BN1", "obs-PR1", "obs-PS-LOW", "obs-PY1", "obs-RQ1", "obs-UR1"],
                 position=("FAIL", "UNITS_EXCEED_POSITION", units("500000000"))),
        "100 units requested; 50 available immediately before acceptance (sequence 1004).",
    ),
    scenario(
        "RD-POSITION-RECONSTRUCTED", "Older position rebuilt from the TA journal",
        "position-reconstructed", S2, stage_certs("s2"),
        expected("MATCH", ["obs-BN1", "obs-PR1", "obs-PS-OLD", "obs-PY1", "obs-RQ1", "obs-UR1",
                           *RECONSTRUCTED]),
        "Position 1,000 at sequence 900 + 50 (J-0950) - 30 reserved by RED-0000 (J-0960) = "
        "1,020 available before acceptance (1005). The request's own reservation (J-1006) "
        "is after it and is not subtracted twice.",
    ),
    scenario(
        "RD-POSITION-SHORT", "Rebuilt position is insufficient", "position-short", S1,
        stage_certs("s1"),
        expected("BREAK", ["obs-CH-0961", "obs-PR1", "obs-PS-OLD-SMALL", "obs-RQ1"],
                 position=("FAIL", "UNITS_EXCEED_POSITION", units("200000000")),
                 **UNSETTLED_PENDING),
        "120 held at sequence 900, 40 reserved by RED-0000 at 961: 80 available, 20 short.",
    ),
    scenario(
        "RD-POSITION-STALE", "Older position without coverage to rebuild it",
        "position-reconstructed", S2, stage_certs("s2", ta="cov-ta-s2-late-start"),
        expected("UNKNOWN", ["obs-BN1", "obs-PR1", "obs-PS-OLD", "obs-PY1", "obs-RQ1", "obs-UR1",
                             *RECONSTRUCTED],
                 position=SHORT_COVERAGE),
        "The TA export starts 2026-10-05T00:00Z, after the position (2026-10-04T18:00Z): the "
        "journal between them is not demonstrated complete. No age window replaces it.",
    ),
    scenario(
        "RD-POSITION-AMBIGUOUS", "Position and acceptance share a journal sequence",
        "position-same-sequence", S2, stage_certs("s2"),
        expected("UNKNOWN", ["obs-BN1", "obs-PR1", "obs-PS-SAME-SEQ", "obs-PY1", "obs-RQ1", "obs-UR1"],
                 position=("UNKNOWN", "AMBIGUOUS_SEQUENCE")),
        "Same timestamp and same sequence: nothing orders the position before the acceptance.",
    ),
    # ---------------------------------------------------------------- burn
    scenario(
        "RD-BURN-BEFORE-ACCEPTANCE", "Linked burn effective before acceptance", "early-burn", S2,
        stage_certs("s2"),
        expected("UNKNOWN", ["obs-PR1", "obs-PS1", "obs-PY1", "obs-RQ1", "obs-UR1"],
                 burn=("UNKNOWN", "BURN_BEFORE_ACCEPTANCE_UNSUPPORTED")),
        "The burn (09:30Z) precedes acceptance (10:00Z): the link is kept, but it does not "
        "fulfil the retirement in this profile.",
    ),
    # ---------------------------------------------------------------- cancellation
    scenario(
        "RD-CANCELLED", "Cancelled before any settlement, full coverage", "cancelled", S2,
        stage_certs("s2"),
        expected("MATCH", ["obs-CN1", "obs-PR1", "obs-PS1", "obs-RQ1"], "cancelled", **CANCELLED),
        "Authorized cancellation effective 2026-10-05T11:00Z; no payment or burn with bank "
        "and chain coverage to valid_at. MATCH rests on the two cancellation controls.",
    ),
    scenario(
        "RD-CANCELLED-NO-COVERAGE", "Cancelled, but bank coverage stops early", "cancelled", S2,
        stage_certs("s2", bank="cov-bank-s2-partial"),
        expected("UNKNOWN", ["obs-CN1", "obs-PR1", "obs-PS1", "obs-RQ1"], "cancelled",
                 **(CANCELLED | {"settle": SHORT_COVERAGE})),
        "Without bank coverage to valid_at the absence of settlement is not demonstrated.",
    ),
    scenario(
        "RD-CANCELLED-PAID", "Cancelled, yet the payment was made", "cancelled-paid", S2,
        stage_certs("s2"),
        expected("BREAK", ["obs-CN1", "obs-PR1", "obs-PS1", "obs-PY1", "obs-RQ1"], "cancelled",
                 **(CANCELLED | {"settle": ("FAIL", "SETTLED_DESPITE_CANCELLATION")})),
        "Payment linked to a cancelled request: review required, nothing reversed.",
    ),
    scenario(
        "RD-CANCELLED-BURNED", "Cancelled, yet the units were burned", "cancelled-burned", S2,
        stage_certs("s2"),
        expected("BREAK", ["obs-BN1", "obs-CN1", "obs-PR1", "obs-PS1", "obs-RQ1"], "cancelled",
                 **(CANCELLED | {"settle": ("FAIL", "SETTLED_DESPITE_CANCELLATION")})),
        "Burn linked to a cancelled request: review required, nothing reversed.",
    ),
    scenario(
        "RD-CANCELLED-AFTER-DUE", "Cancellation effective after a missed due time",
        "cancelled-after-due", S4, stage_certs("s4"),
        expected("BREAK", ["obs-CN-LATE", "obs-PR1", "obs-PS1", "obs-RQ1"], "cancelled",
                 **(CANCELLED | {"deadline": ("FAIL", "PAYMENT_MISSED")})),
        "Effective 2026-10-08T00:00Z, after the due time passed without payment: it "
        "extinguishes what was pending but not the demonstrated breach.",
    ),
    scenario(
        "RD-CANCEL-KNOWN-LATE-BEFORE", "Snapshot before a late-delivered cancellation",
        "cancel-known-late", S3, stage_certs("s3"),
        expected("BREAK", ["obs-PR1", "obs-PS1", "obs-RQ1"], ta=MISSING, burn=MISSING, **MISSED),
        "At 2026-10-07T12:00Z nothing showed a cancellation and the payment was missed with "
        "coverage: BREAK was justified by the evidence then, and replays so.",
    ),
    scenario(
        "RD-CANCEL-KNOWN-LATE-AFTER", "Cancellation effective before the due time, known after",
        "cancel-known-late", S4, stage_certs("s4"),
        expected("MATCH", ["obs-CN-KNOWN-LATE", "obs-PR1", "obs-PS1", "obs-RQ1"], "cancelled",
                 **CANCELLED),
        "Effective 2026-10-06T08:00Z (before the due time) though recorded 2026-10-07T15:00Z: "
        "the current conclusion for the period changes; the earlier snapshot keeps its BREAK.",
    ),
    scenario(
        "RD-CANCEL-UNAUTHORIZED", "Cancellation by an unauthorized party", "cancel-unauthorized",
        S1, stage_certs("s1"),
        expected("UNKNOWN", ["obs-CN-UNAUTH", "obs-PR1", "obs-PS1", "obs-RQ1"],
                 valid=("UNKNOWN", "INVALID_CANCELLATION"), **UNSETTLED_PENDING),
        "Not a cancellation authority: the obligations are not extinguished.",
    ),
    scenario(
        "RD-CANCEL-RETRACTED-BEFORE", "Snapshot while the cancellation stood", "cancel-retracted",
        S0, stage_certs("s0"),
        expected("MATCH", ["obs-CN1", "obs-PR1", "obs-PS1", "obs-RQ1"], "cancelled", **CANCELLED),
        "At 2026-10-05T11:30Z the cancellation was in force and nothing had settled.",
    ),
    scenario(
        "RD-CANCEL-RETRACTED-AFTER", "Cancellation retracted as an evidence correction",
        "cancel-retracted", S2, stage_certs("s2"), expected("MATCH", [*SETTLED, "obs-PY1"]),
        "The TA retracts revision 1 of CAN-RED-0001 with revision 2: no cancellation in force, "
        "with TA coverage. Obligations are judged again against the original due time.",
    ),
    scenario(
        "RD-CANCEL-RETRACT-CONFLICT", "Retraction and another revision 2 disagree",
        "cancel-retract-conflict", S1, stage_certs("s1"),
        expected("UNKNOWN", ["obs-PR1", "obs-PS1", "obs-RQ1"],
                 valid=("UNKNOWN", "SOURCE_CONFLICT"), **UNSETTLED_PENDING),
        "Two revision-2 deliveries of the cancellation record differ: no choice is made.",
    ),
    scenario(
        "RD-REACTIVATED", "A cancelled request is reactivated", "reactivated", S1,
        stage_certs("s1"),
        expected("UNKNOWN", ["obs-CN1", "obs-PR1", "obs-PS1", "obs-RA1", "obs-RQ1"],
                 valid=("UNKNOWN", "REACTIVATION_UNSUPPORTED"), **UNSETTLED_PENDING),
        "Business reactivation is outside this profile version.",
    ),
    scenario(
        "RD-RETRACTED", "The TA retracts the request record", "retracted", S1, stage_certs("s1"),
        expected(
            "UNKNOWN", ["obs-PS1"],
            **{name: ("UNKNOWN", "LOSS_OF_SUPPORT") for name in CONTROLS if name != "settle"},
        ),
        "Withdrawing the evidence of the request is not a cancellation: loss of support.",
    ),
    scenario(
        "RD-RETRACTED-PAID", "Request retracted after the payment was made", "retracted-paid",
        S2, stage_certs("s2"),
        expected(
            "UNKNOWN", ["obs-PS1", "obs-PY1"],
            **{name: ("UNKNOWN", "LOSS_OF_SUPPORT") for name in CONTROLS if name != "settle"},
        ),
        "Dependent controls lose support; the payment stays effective and is cited, not erased.",
    ),
    scenario(
        "RD-WRONG-RECIPIENT", "Linked payment made to another account", "wrong-recipient", S2,
        stage_certs("s2"),
        expected("BREAK", [*SETTLED, "obs-PY-WRONG"],
                 cash=("FAIL", "PAYMENT_TO_WRONG_ACCOUNT"), deadline=NOT_DUE),
        "PAY-RED-0001 is linked to RED-0001 but paid to acct-pseudo-0002; bank and TA share "
        "the account namespace in DEMO-A, so the wrong recipient is demonstrated.",
    ),
    scenario(
        "RD-UNLINKED-CANDIDATE", "Unlinked payment carrying the instructed settlement_ref",
        "unlinked-candidate", S3, stage_certs("s3"),
        expected("UNKNOWN", ["obs-BN1", "obs-PR1", "obs-PS1", "obs-RQ-SETTLE", "obs-UR1"],
                 cash=("UNKNOWN", "AMBIGUOUS_MATCH"), deadline=("UNKNOWN", "AMBIGUOUS_MATCH")),
        "The TA instructed settlement_ref PAY-RED-0001 and an unlinked payment carries it: a "
        "relevant candidate without an approved link. Neither paid nor missed.",
    ),
    scenario(
        "RD-UNLINKED-NOT-CANDIDATE", "Unlinked payment to the same account, no candidate",
        "unlinked-not-candidate", S3, stage_certs("s3"), expected("BREAK", SETTLED, **MISSED),
        "Sharing the account is not a candidate association: the unlinked payment neither "
        "settles RED-0001 nor makes it ambiguous, and the linked payment is missed.",
    ),
    scenario(
        "RD-PAID-AFTER-ECONOMIC-CUT", "Payment effective after the economic cut", "main", S2,
        stage_certs("s2"), expected("UNKNOWN", SETTLED, **PENDING),
        "valid_at 2026-10-06T12:00Z: the 15:00Z payment is known but not yet effective at the "
        "economic cut, and the due time has not passed.",
        valid_at="2026-10-06T12:00:00Z",
    ),
    scenario(
        "RD-MISSED-CUT-BEFORE-DUE", "Clock after the due time, economic cut before it",
        "unpaid-then-late", S3, stage_certs("s3"), expected("UNKNOWN", SETTLED, **PENDING),
        "Clock 2026-10-07T12:00Z but valid_at 09:00Z: the economic cut does not reach the due "
        "time (10:00Z, unchanged), so absence cannot be judged.",
        valid_at="2026-10-07T09:00:00Z",
    ),
    scenario(
        "RD-MISSED-FILTER-COMPATIBLE", "Bank export filtered to the account and currency",
        "unpaid-then-late", S3, stage_certs("s3", bank="cov-bank-s3-filtered-ok"),
        expected("BREAK", SETTLED, **MISSED),
        "Filtering by the requesting account and USD keeps every record the absence predicate "
        "needs: the missed payment is still demonstrated.",
    ),
    scenario(
        "RD-MISSED-FILTER-INCOMPATIBLE", "Bank export filtered by status",
        "unpaid-then-late", S3, stage_certs("s3", bank="cov-bank-s3-filtered-status"),
        expected("UNKNOWN", SETTLED, cash=MISSING, deadline=SHORT_COVERAGE),
        "A status filter cannot be shown to keep every relevant payment or revision: "
        "absence is not demonstrated.",
    ),
    scenario(
        "RD-CANCELLED-EARLY-BURN", "Cancelled, and a linked burn effective before acceptance",
        "cancelled-early-burn", S2, stage_certs("s2"),
        expected("BREAK", ["obs-CN1", "obs-PR1", "obs-PS1", "obs-RQ1"], "cancelled",
                 **(CANCELLED | {"settle": ("FAIL", "SETTLED_DESPITE_CANCELLATION")})),
        "The early burn does not fulfil the retirement, but it happened (successful, linked, "
        "effective): it contradicts the clean cancellation.",
    ),
    scenario(
        "RD-SHORT-PAY-BEFORE-CORRECTION", "Short payment, before the bank's correction is known",
        "short-pay-corrected", S2, stage_certs("s2"),
        expected("BREAK", [*SETTLED, "obs-PY-SHORT"],
                 cash=("FAIL", "CASH_AMOUNT_MISMATCH", usd("-5000"))),
        "At 2026-10-06T16:00Z only revision 1 (USD 9,950.00) is known.",
    ),
    scenario(
        "RD-CORRECTION-KNOWN-LATE", "Correction known after valid_at of an earlier payment",
        "short-pay-corrected", S3, stage_certs("s3"),
        expected("MATCH", [*SETTLED, "obs-PY-FIX"]),
        "Revision 2 (USD 10,000.00, effective 2026-10-06T15:00Z) is recorded 2026-10-07T11:00Z, "
        "after valid_at 16:00Z of the 6th but within the knowledge snapshot: it describes the "
        "earlier economic fact and counts.",
        valid_at=S2,
    ),
    scenario(
        "RD-SETTLEMENT-REF-COLLISION", "Two payments carry the instructed settlement_ref",
        "settlement-ref-collision", S3, stage_certs("s3"),
        expected("UNKNOWN", ["obs-BN1", "obs-PR1", "obs-PS1", "obs-RQ-SETTLE", "obs-UR1"],
                 cash=("UNKNOWN", "AMBIGUOUS_MATCH"), deadline=("UNKNOWN", "AMBIGUOUS_MATCH")),
        "An unlinked payment and one linked to RED-0002 both carry PAY-RED-0001: the collision "
        "stays visible and nothing is associated.",
    ),
    scenario(
        "RD-DUPLICATE", "The bank delivers the same payment twice", "duplicate", S2,
        stage_certs("s2"), expected("MATCH", [*SETTLED, "obs-PY1"]),
        "Identical redelivery of PAY-RED-0001 rev 1 counts once.",
    ),
]

# -------------------------------------------------------------------------- profile


def source(source_id: str, kind: str, facts: list[str], description: str) -> dict[str, object]:
    mapping_ref, parser_ref = MAPPINGS[source_id]
    return {
        "source_id": source_id,
        "kind": kind,
        "authoritative_for": facts,
        "mapping_ref": mapping_ref,
        "parser_ref": parser_ref,
        "description": description,
    }


COVERAGE = {
    "redemption.units_within_position": "reconstruction_interval",
    "redemption.payment_deadline": "absence_interval",
    "redemption.cancellation_valid": "absence_interval",
    "redemption.no_settlement_after_cancellation": "absence_interval",
}


def control(cid, requires, comparison, applies, left, right, failures):
    return {
        "control_id": cid,
        "mandatory": True,
        "requires": requires,
        "comparison": comparison,
        "applies": applies,
        "coverage": COVERAGE.get(cid, "positive_evidence_only"),
        "left": left,
        "right": right,
        "failure_codes": failures,
    }


SCOPE_REQUIRED = {
    "ta-synthetic": "account",
    "bank-synthetic": "account_and_currency",
    "pricing-synthetic": "none",
    "stellar-testnet-frozen": "none",
}
PROFILE = {
    "schema_version": "1.0",
    "profile_ref": PROFILE_REF,
    "operation_type": "redemption",
    "synthetic": True,
    "disclaimer": "Synthetic redemption profile for the DEMO-A fixture (ADR-008 rev. 4). Not a policy of any real fund; not for real client operations."
    + (
        " Version 1.5.0 (ADR-014, INV-013): the on-chain coverage declares its quarantine policy explicitly: a quarantined record never credits a retirement or a settlement; its existence makes UNKNOWN only the controls whose conclusion resolving it could change, the burn comparison included; a certificate's quarantine stays active until a certificate of the same chain scope explicitly supersedes it; a record's ledger operation is relied on only when the snapshot supports it. 1.4.0 and 1.3.0 are unchanged."
        if V15 and not V16
        else " Version 1.6.0 (INV-015): keeps the declared quarantine policy of 1.5.0 and requires a sufficient declared chain scope to show an absence on the chain source: a certificate without one, or one that does not declare what it leaves out of sight, never shows that no retirement or settlement occurred. 1.5.0 and earlier are unchanged."
        if V16 and not V17
        else " Version 1.7.0 (INV-015): keeps 1.6.0 and requires coverage to affirm that the observed burns are complete: an equal or short burn comparison needs a coherent chain scope of the representation of the burns covering acceptance to the cut; an excess and proven settlement stay a FAIL. 1.6.0 and earlier are unchanged."
        if V17
        else " Version 1.4.0 (ADR-010, INV-013): the on-chain coverage scopes its quarantine to records bearing on the request; 1.3.0 is unchanged."
        if V14
        else ""
    ),
    "instrument": {
        "instrument_id": INSTRUMENT,
        "unit": "FUND_SHARE",
        "scale": 7,
        "description": "DEMO-A class A participation (synthetic)",
        "synthetic": True,
    },
    "representations": [
        {
            "representation_id": REP,
            "instrument_id": INSTRUMENT,
            "standard": "stellar_classic",
            "network": "stellar:testnet",
            "network_passphrase": "Test SDF Network ; September 2015",
            "asset_code": "DEMOA",
            "issuer": ISSUER,
            "amount_scale": 7,
        }
    ],
    "representation_ratio": "1:1",
    "pricing": {
        "cash_unit": "USD",
        "cash_scale": 2,
        "price_fact": "price_approved",
        "expected_payment": "units_times_approved_price",
        "fees": "none",
        "rounding": "inexact_is_unknown",
        "tolerance": "none",
    },
    "correlation": {
        "operation_key": "request_ref",
        "institutional_link": "operation_ref_equals_request_ref",
        "price_link": "explicit_price_ref",
        "chain_link": "explicit_execution_link_only",
        "account_link": "approved_identity_link",
        "account_namespace": "shared_pseudonymous_account_ref",
        "unlinked_payment_candidate": "payment_ref_equals_request_settlement_ref",
        "settlement_ref_namespace": "bank_payment_ref",
    },
    "payment_deadline": {
        "anchor": "accepted_at",
        "hours": 48,
        "calendar": "none_exact_utc_hours",
        "on_time": "effective_at_or_before_due",
        "judged_with": "snapshot_evaluation_clock",
        "absence_after_due": "BREAK_only_with_sufficient_bank_coverage",
        "absence_coverage": "half_open_interval_must_contain_the_due_instant",
        "absence_requires": "clock_and_economic_cut_after_due",
        "time_precision": "second",
        "late_payment": "breach_persists",
        "declared_due_mismatch": "UNKNOWN_DEADLINE_DATA_CONFLICT_judge_computed_due",
    },
    "ta_journal": {
        "journal_id": JOURNAL,
        "ordering": "source_assigned_strict_total_order",
        "position_semantics": "state_after_its_sequence",
        "scope": "synthetic_demo_a_only",
    },
    "quantity": {
        "request": "fixed_quantity_settled_in_full",
        "position": "available_immediately_before_acceptance_by_ta_sequence",
        "position_reconstruction": "journal_changes_with_ta_coverage_no_age_window",
        "retirement": "burn_to_issuer",
        "burn_before_acceptance": "UNKNOWN_does_not_fulfil_but_contradicts_a_cancellation",
    },
    "cancellation": {
        "authorized_by": ["ta-ops-synthetic"],
        "valid_when": "effective_at_or_after_acceptance",
        "effect": "extinguishes_pending_settlement_obligations",
        "settlement_despite_cancellation": "BREAK_review_required",
        "automatic_reversal": False,
        "evidence_retraction": "not_a_cancellation",
        "effective_after_due": "keeps_a_demonstrated_breach",
        "retracted_cancellation": "no_cancellation_in_force_with_ta_coverage",
        "reactivation": "UNKNOWN_unsupported",
        "foreign_cancellation": "out_of_scope",
        "contradictory_association": "UNKNOWN_keep_conflict",
        "request_retraction": "LOSS_OF_SUPPORT_in_dependent_controls_only",
        "invalid_retraction": "UNKNOWN_INVALID_RETRACTION_keep_both",
    },
    "sources": [
        source("ta-synthetic", "transfer_agent", FACTS["ta-synthetic"], "Transfer agent: accepted redemption requests, cancellations, reactivations, positions and their journal, and register debits."),
        source("bank-synthetic", "bank", ["cash_settled"], "Bank: redemption payments to the investor."),
        source("pricing-synthetic", "pricing_agent", ["price_approved"], "Fund administrator: approved, versioned price per unit."),
        source("stellar-testnet-frozen", "stellar_ledger", ["token_movement"], "Frozen synthetic Stellar testnet burns of DEMOA (payment to the issuer). Authoritative only for on-chain retirement, with provider_claimed coverage."),
    ],
    "coverage_requirements": [
        {
            "source_id": src,
            "fact_types": FACTS[src],
            "min_level": "provider_claimed" if src == "stellar-testnet-frozen" else "internally_checked",
            "must_cover": "interval_named_by_each_control",
            "scope": SCOPE_REQUIRED[src],
            "filters": "only_those_keeping_every_record_of_the_predicate",
            "gaps_allowed": False,
            "quarantined_records_allowed": False,
            **({"quarantine_scope": "records_bearing_on_operation"} if V14 and src == "stellar-testnet-frozen" else {}),
            **({"quarantine_policy": QUARANTINE_POLICY} if V15 and src == "stellar-testnet-frozen" else {}),
            **({"absence_needs_chain_scope": True} if V16 and src == "stellar-testnet-frozen" else {}),
            **({"completeness_needs_coverage": True} if V17 and src == "stellar-testnet-frozen" else {}),
        }
        for src in FACTS
    ],
    "controls": [
        control("redemption.declared_due_consistency", ["redemption_accepted"], "data_consistency", "always", "payment_due_at declared by the TA", "accepted_at + 48 exact hours (profile)", []),
        control("redemption.price_vs_approved", ["redemption_accepted", "price_approved"], "exact_equal", "always", "price_per_unit stated in the request", "approved price for the request's price_ref", ["PRICE_MISMATCH"]),
        control("redemption.units_within_position", ["redemption_accepted", "position_held", "position_changed"], "at_most", "always", "units requested", "available immediately before acceptance (holding - reservations, by TA sequence)", ["UNITS_EXCEED_POSITION"]),
        control("redemption.cash_vs_expected", ["redemption_accepted", "price_approved", "cash_settled"], "exact_equal", "unless_cancelled", "cash paid by the bank to the requesting account", "units requested x approved price", ["CASH_AMOUNT_MISMATCH", "PAYMENT_TO_WRONG_ACCOUNT"]),
        control("redemption.payment_deadline", ["redemption_accepted", "cash_settled"], "deadline", "unless_cancelled", "effective time of the payment", "accepted_at + 48 exact hours (computed)", ["PAYMENT_LATE", "PAYMENT_MISSED"]),
        control("redemption.ta_units_vs_request", ["redemption_accepted", "units_registered"], "exact_equal", "unless_cancelled", "units debited in the TA register", "units requested", ["UNITS_MISMATCH"]),
        control("redemption.burn_vs_request", ["redemption_accepted", "token_movement"], "exact_equal", "unless_cancelled", "units burned (payments to the issuer) after acceptance, linked to the request", "units requested", ["UNITS_MISMATCH"]),
        control("redemption.cancellation_valid", ["redemption_accepted", "redemption_cancelled", "redemption_reactivated"], "validity", "cancellation", "cancellation in force", "authority, effective time and account of the profile", []),
        control("redemption.no_settlement_after_cancellation", ["redemption_accepted", "redemption_cancelled", "cash_settled", "token_movement"], "no_settlement", "cancellation", "payments and burns linked to the cancelled request", "none, with bank and chain coverage", ["SETTLED_DESPITE_CANCELLATION"]),
    ],
    "rules_ref": RULES_REF,
    "result_precedence": "BREAK>UNKNOWN>MATCH",
    "evidence_policy": {
        "absent": "UNKNOWN",
        "conflicting_authoritative": "UNKNOWN",
        "correction": "latest_revision_known_at_snapshot",
        "retraction_without_replacement": "UNKNOWN_LOSS_OF_SUPPORT",
        "duplicate_same_content": "single_effect",
        "duplicate_different_content": "SOURCE_CONFLICT",
        "failed_chain_transaction": "no_economic_effect",
        "technical_error": "UNKNOWN",
    },
    "unsupported": [
        "fees",
        "partial_execution",
        "multiple_cash_payments",
        "fx_conversion",
        "rounding",
        "business_day_calendar",
        "transfer_retirement",
        "automatic_reversal",
        "omnibus_account",
        "sac_events",
        "soroban_custom_token",
        "amount_time_matching",
        "business_reactivation",
        "position_age_window",
        "burn_before_acceptance",
    ],
}


def build() -> dict[str, str]:
    files = dict(FILES)
    corpus = CORPUS_ID
    files["profile.json"] = dump(PROFILE)
    files["coverage.json"] = dump({"schema_version": "1.0", "corpus_id": corpus, "synthetic": True, "certificates": CERTS})
    files["identity_links.json"] = dump(
        {
            "schema_version": "1.0",
            "corpus_id": corpus,
            "synthetic": True,
            "links": [
                {
                    "schema_version": "1.0",
                    "link_id": "link-0001",
                    "account_ref": ACCOUNT,
                    "network": "stellar:testnet",
                    "address": INVESTOR,
                    "valid_from": "2026-09-01T00:00:00Z",
                    "valid_to": None,
                    "approval_ref": "approval-synthetic-0001",
                    "recorded_at": "2026-09-01T00:00:00Z",
                }
            ],
        }
    )
    for timeline, observations in TIMELINES.items():
        files[f"observations/{timeline}.json"] = dump(
            {"schema_version": "1.0", "corpus_id": corpus, "timeline_id": timeline, "synthetic": True, "observations": observations}
        )
    if V17:
        files["README.md"] = README_V17
    elif V16:
        files["README.md"] = README_V16
    elif V15:
        files["README.md"] = README_V15
    elif V14:
        files["README.md"] = README_V14
    scenarios = [_v16(s) for s in SCENARIOS] if V16 else SCENARIOS
    if V17:
        scenarios = [_v17(s) for s in scenarios]
    files["scenarios.json"] = dump(
        {"schema_version": "1.0", "corpus_id": corpus, "synthetic": True, "profile_ref": PROFILE_REF, "scenarios": scenarios}
    )
    return files


# Written by hand from the scoped-absence rule: the chain certificates of
# this corpus declare no chain scope, so wherever the absence of settlement rested on them it
# is UNKNOWN; every other control, and a BREAK they do not support, is kept.
V16_EXPECTED = {
    "RD-CANCELLED": "UNKNOWN",
    "RD-CANCELLED-AFTER-DUE": "BREAK",  # the missed payment (bank coverage) keeps it
    "RD-CANCEL-KNOWN-LATE-AFTER": "UNKNOWN",
    "RD-CANCEL-RETRACTED-BEFORE": "UNKNOWN",
}


def _v16(s: dict[str, object]) -> dict[str, object]:
    sid = s["scenario_id"]
    if sid not in V16_EXPECTED:
        return s
    expect = dict(s["expected"])  # type: ignore[arg-type]
    controls = []
    for control in expect["controls"]:  # type: ignore[union-attr]
        if control["control_id"] == CONTROLS["settle"]:
            assert (control["status"], control["reason_code"]) == PASS, sid
            control = {**control, "status": "UNKNOWN", "reason_code": "INSUFFICIENT_COVERAGE"}
        controls.append(control)
    expect.update(result=V16_EXPECTED[sid], controls=controls)
    rationale = (
        f"{s['rationale']} Profile 1.6.0 (INV-015): the chain certificate declares no chain "
        "scope, so it cannot show that no burn occurred; the absence of settlement is UNKNOWN "
        "(INSUFFICIENT_COVERAGE)"
        + ("; the missed payment keeps the BREAK." if V16_EXPECTED[sid] == "BREAK" else ".")
    )
    return {**s, "expected": expect, "rationale": rationale}


def _aggregate(controls: list[dict[str, object]]) -> str:
    """BREAK>UNKNOWN>MATCH over the mandatory controls (the profile's precedence)."""
    statuses = [c["status"] for c in controls if c["mandatory"]]
    if "FAIL" in statuses:
        return "BREAK"
    if all(s in ("PASS", "NOT_APPLICABLE") for s in statuses) and "PASS" in statuses:
        return "MATCH"
    return "UNKNOWN"


def _v17(s: dict[str, object]) -> dict[str, object]:
    """Burn completeness, written from the rule: an equal burn
    (or a short one, absent here) needs coverage this corpus's scopeless chain certificates
    cannot give; an excess would stay a FAIL. Every other control is kept."""
    expect = dict(s["expected"])  # type: ignore[arg-type]
    controls, changed = [], False
    for control in expect["controls"]:  # type: ignore[union-attr]
        if control["control_id"] == CONTROLS["burn"]:
            delta = control["delta"]
            short = control["status"] == "FAIL" and delta is not None and int(delta["atoms"]) < 0
            if control["status"] == "PASS" or short:
                control = {**control, "status": "UNKNOWN", "reason_code": "INSUFFICIENT_COVERAGE", "delta": None}
                changed = True
        controls.append(control)
    if not changed:
        return s
    expect.update(result=_aggregate(controls), controls=controls)
    rationale = (
        f"{s['rationale']} Profile 1.7.0 (INV-015 M3): the chain certificate declares no chain "
        "scope, so it cannot show that the burns observed are all the burns; the burn comparison "
        "is UNKNOWN (INSUFFICIENT_COVERAGE)."
    )
    return {**s, "expected": expect, "rationale": rationale}


README_V17 = """# Corpus sintético de rescate, perfil 1.7.0 (actual)

Generado por `../redemption-synthetic/build_corpus.py --profile=1.7.0`; no editar a mano.
Es el corpus actual de rescate: perfil `fund-redemption-synthetic@1.7.0`, evaluado por
`invaria-redemption-engine@0.11.0` (regla de completitud). Comparte con los
corpus históricos los mismos bytes crudos, observaciones y coberturas. El perfil añade
`completeness_needs_coverage` al requisito de `stellar-testnet-frozen`: afirmar que los burns
observados son todos los burns exige cobertura con un alcance coherente de la representación de
los burns, de la aceptación al corte.

Los certificados on-chain de este corpus no declaran alcance, así que todo burn igual pasa a
UNKNOWN (`INSUFFICIENT_COVERAGE`). Lo aplica `_v17` a los esperados de 1.6.0, desde la regla y
no desde el motor. Seis MATCH pasan a UNKNOWN (RD-PAID, RD-PAID-AT-DUE,
RD-POSITION-RECONSTRUCTED, RD-CANCEL-RETRACTED-AFTER, RD-CORRECTION-KNOWN-LATE, RD-DUPLICATE) y
todos los BREAK se conservan. El corpus no tiene burns cortos
ni en exceso: esos casos, y el alcance suficiente, se prueban en
`tests/engine/test_scoped_absence.py`. Todo es sintético.
"""

README_V16 = """# Corpus sintético de rescate, perfil 1.6.0 (histórico)

Generado por `../redemption-synthetic/build_corpus.py --profile=1.6.0`; no editar a mano.
Corpus histórico desde la regla de completitud; el actual es `../redemption-synthetic-1.7.0`.
Perfil `fund-redemption-synthetic@1.6.0`, evaluado por `invaria-redemption-engine@0.10.0` y hoy
por 0.11.0 sin las reglas M3, B1 y B3, que no declara. Comparte con los corpus históricos
(`../redemption-synthetic-1.5.0`, `../redemption-synthetic-1.4.0` y
`../redemption-synthetic`) los mismos bytes crudos, observaciones y coberturas. El perfil
añade `absence_needs_chain_scope` al requisito de `stellar-testnet-frozen`: un certificado
on-chain sin alcance declarado suficiente no acredita la ausencia de liquidación.

Los certificados on-chain de este corpus no declaran alcance. Por eso cambian cuatro
esperados, escritos a mano en `V16_EXPECTED` desde la regla:
`no_settlement_after_cancellation` pasa a UNKNOWN (`INSUFFICIENT_COVERAGE`) en RD-CANCELLED,
RD-CANCEL-KNOWN-LATE-AFTER y RD-CANCEL-RETRACTED-BEFORE, que pasan a UNKNOWN, y en
RD-CANCELLED-AFTER-DUE, que conserva el BREAK del pago no hecho. Los demás esperados no
cambian. Los casos de alcance suficiente e insuficiente se prueban en
`tests/engine/test_scoped_absence.py`. Todo es sintético.
"""

README_V15 = """# Corpus sintético de rescate, perfil 1.5.0 (histórico)

Generado por `../redemption-synthetic/build_corpus.py --profile=1.5.0`; no editar a mano.
Corpus histórico de rescate desde la regla de ausencia con alcance: el actual es `../redemption-synthetic-1.6.0`.
Perfil `fund-redemption-synthetic@1.5.0`, evaluado por `invaria-redemption-engine@0.7.0`
y después por 0.8.0 a 0.10.0 con los mismos resultados: no declara
`absence_needs_chain_scope`, así que su certificado on-chain sin alcance sigue acreditando la
ausencia de liquidación (límite declarado). Comparte con los corpus históricos
(`../redemption-synthetic-1.4.0`, perfil 1.4.0; `../redemption-synthetic`, perfil 1.3.0) los
mismos bytes crudos, observaciones, coberturas y resultados esperados escritos a mano; solo
cambian el perfil (`quarantine_policy` declarada en el requisito de `stellar-testnet-frozen`),
sus referencias y el identificador del corpus. Los esperados no cambian porque el corpus no
contiene efectos on-chain ni certificados con registros en cuarentena, lo único que leen las
reglas de 0.5.0 a 0.7.0. Los casos de cuarentena por control en rescate se prueban en
`tests/engine/test_redemption_quarantine.py`. Todo es sintético.
"""

README_V14 = """# Corpus sintético de rescate, perfil 1.4.0 (histórico)

Generado por `../redemption-synthetic/build_corpus.py --profile=1.4.0`; no editar a mano.
Corpus histórico de rescate: perfil `fund-redemption-synthetic@1.4.0`, migrado con
`invaria-redemption-engine@0.5.0`, evaluado después por `0.6.0` y hoy
por `0.7.0` en modo compatible con su promesa (sin política declarada, el burn igual
no consulta la cuarentena), con los mismos resultados porque el corpus no contiene efectos
on-chain. El actual es `../redemption-synthetic-1.5.0`. Comparte con el corpus histórico
(`../redemption-synthetic`, perfil 1.3.0, motor retirado 0.4.0) los mismos bytes crudos,
observaciones, coberturas y resultados esperados escritos a mano; solo cambian el perfil
(`quarantine_scope` en el requisito de `stellar-testnet-frozen`), sus referencias y el
identificador del corpus. Todo es sintético.
"""


def main(argv: list[str]) -> int:
    files = build()
    stale_outputs = sorted(
        str(p.relative_to(OUT)) for p in OUT.rglob("*.json") if str(p.relative_to(OUT)) not in files
    )
    if "--check" in argv:
        stale = [p for p, text in files.items() if not (OUT / p).is_file() or (OUT / p).read_text("utf-8") != text]
        print("\n".join([*stale, *stale_outputs]) or "up to date")
        return 1 if stale or stale_outputs else 0
    for path in stale_outputs:
        (OUT / path).unlink()
    for path, text in files.items():
        (OUT / path).parent.mkdir(parents=True, exist_ok=True)
        (OUT / path).write_text(text, encoding="utf-8")
    print(f"wrote {len(files)} files, removed {len(stale_outputs)} stale")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
