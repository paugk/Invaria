"""Anchored inclusion of Classic operations, from Stellar history archive checkpoints.

A first, bounded verification of ledger evidence that does not rest on Horizon or RPC.
The checkpoint files are bytes from a history archive. They are checked against an anchor:
the hash of the checkpoint ledger, which the verification itself never invents. An anchor
is one of three things, and every report says which:
- ``observed_run``: this process ran ``stellar-core verify-checkpoints``, which observed
  the network's consensus with the validators of its config and verified the chain of
  checkpoint headers back to the one asked for;
- ``imported_with_provenance``: the anchors of such a run, read from its record, whose
  config, output and log still match it by sha256. The verifier observed nothing itself;
- ``manual``: a hash supplied by whoever runs the check.
In every case, the result holds relative to that anchor.

What is checked, inside one checkpoint:
- every header hashes to its recorded hash;
- headers chain through ``previous_ledger_hash`` to the anchored checkpoint hash;
- each ledger's transaction set and result set hash to the header's ``tx_set_hash`` and
  ``tx_set_result_hash``;
- each transaction hash is recomputed with the network passphrase.

XDR is decoded and re-encoded with the official ``stellar`` CLI, and every record must
re-encode to its exact bytes. This module only frames records, hashes and compares.

What it cannot show. Archives hold no ``TransactionMeta``, so contract events (SAC
transfers, mints, burns, clawbacks), ledger entry changes and the effects Horizon derives
are out of reach without a replay. Showing that one operation is included never shows that
there are no others: no absence or completeness rests on this report.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from invaria.contracts.chain import ChainTarget
from invaria.contracts.observation import Observation, TokenMovementPayload
from invaria.contracts.stellar import decode_muxed_account

CHECKPOINT_FREQUENCY = 64
NETWORK_PASSPHRASES = {
    "stellar:testnet": "Test SDF Network ; September 2015",
    "stellar:pubnet": "Public Global Stellar Network ; September 2015",
}
KINDS = ("history", "ledger", "transactions", "results")
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
STELLAR_CLI_VERSION = "stellar 28.1.0"

# VERIFIED: every check passed against the anchor. ANCHOR_MISMATCH: the files are
# internally consistent but the checkpoint hash is not the anchor's (files and anchor come
# from different histories, or one of them is wrong; this alone does not say which).
# INCONSISTENT: a committed hash does not match inside the files themselves. INCOMPLETE:
# something needed is missing or not understood. REJECTED: another network or checkpoint.
EvidenceStatus = Literal["VERIFIED", "ANCHOR_MISMATCH", "INCONSISTENT", "INCOMPLETE", "REJECTED"]
ObservationStatus = Literal[
    "SUPPORTED", "CONTRADICTED", "NOT_FOUND", "CANNOT_RECONSTRUCT", "UNVERIFIED"
]
# observed_run: this process ran stellar-core, which observed the network's consensus.
# imported_with_provenance: anchors read from the record of such a run, whose artifacts
# match; the verifier itself observed nothing. manual: a hash supplied by whoever runs it.
AnchorMode = Literal["observed_run", "imported_with_provenance", "manual"]
STELLAR_CORE_IMAGE = (
    "stellar/stellar-core@sha256:4fae6c47fea671e4883b0ed3c58fa1cdbd97f51c9196a7256b8fba80fd55f0e2"
)
ANCHOR_RUN_KIND = "stellar_core_verify_checkpoints_run"
# What the hashes commit and what they do not (declared in every report).
INTEGRITY_SCOPE = {
    "committed": [
        "LedgerHeader of every ledger (its sha256 is the ledger hash, chained to the anchor)",
        "GeneralizedTransactionSet (header scp_value.tx_set_hash)",
        "TransactionResultSet (header tx_set_result_hash)",
    ],
    "not_committed": [
        "the exact gzip bytes of the files: their sha256 is provenance, not verified evidence",
        "the history state JSON beyond its network passphrase and checkpoint number",
        "record framing and the ext fields of the history entries outside the hashed parts",
    ],
}


def checkpoint_of(ledger: int) -> int:
    """The checkpoint ledger whose files hold ``ledger`` (the last of each 64-ledger block)."""
    if ledger < 1:
        raise ValueError("ledgers start at 1")
    return (ledger // CHECKPOINT_FREQUENCY + 1) * CHECKPOINT_FREQUENCY - 1


def archive_path(kind: str, checkpoint: int) -> str:
    """Path of a checkpoint file inside a history archive."""
    if kind not in KINDS or (checkpoint + 1) % CHECKPOINT_FREQUENCY:
        raise ValueError(f"no {kind} file for ledger {checkpoint}")
    h = f"{checkpoint:08x}"
    ext = "json" if kind == "history" else "xdr.gz"
    return f"{kind}/{h[0:2]}/{h[2:4]}/{h[4:6]}/{kind}-{h}.{ext}"


# ------------------------------------------------------------------ acquisition


@dataclass(frozen=True)
class FetchedFile:
    kind: str
    url: str
    sha256: str
    size: int
    fetched_at: str


def fetch_checkpoint(
    archive_url: str,
    checkpoint: int,
    out_dir: Path,
    *,
    get: Callable[[str], bytes],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[FetchedFile]:
    """Download the four files of one checkpoint, keep their exact bytes and provenance.

    ``get`` must enforce its own timeout and size bound (``bounded_get``). Nothing is
    decoded or trusted here; ``provenance.json`` records URL, sha256, size and time.
    """
    if not archive_url.startswith("https://"):
        raise ValueError("history archives are read over HTTPS only")
    out_dir.mkdir(parents=True, exist_ok=False)
    fetched = []
    for kind in KINDS:
        url = f"{archive_url.rstrip('/')}/{archive_path(kind, checkpoint)}"
        body = get(url)
        (out_dir / Path(archive_path(kind, checkpoint)).name).write_bytes(body)
        fetched.append(
            FetchedFile(
                kind=kind,
                url=url,
                sha256=hashlib.sha256(body).hexdigest(),
                size=len(body),
                fetched_at=now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )
    provenance = {
        "checkpoint": checkpoint,
        "archive": archive_url,
        "files": [asdict(f) for f in fetched],
    }
    (out_dir / "provenance.json").write_text(json.dumps(provenance, indent=1) + "\n", "utf-8")
    return fetched


def bounded_get(
    timeout_seconds: int = 60, max_bytes: int = MAX_FILE_BYTES
) -> Callable[[str], bytes]:
    """An HTTPS GET with a timeout and a hard size bound; any failure raises, never empty."""
    import urllib.request

    def get(url: str) -> bytes:
        if not url.startswith("https://"):
            raise ValueError(f"refusing non-HTTPS URL {url}")
        request = urllib.request.Request(url, headers={"User-Agent": "invaria-ledger-proof/0.1"})
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body: bytes = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError(f"{url} exceeds {max_bytes} bytes")
        return body

    return get


# ------------------------------------------------------------------ XDR through the CLI


class XdrTool(Protocol):
    def decode(self, xdr_type: str, record: bytes) -> dict[str, Any]: ...
    def encode(self, xdr_type: str, value: Any) -> bytes: ...


class StellarCli:
    """The official ``stellar`` CLI (``stellar-xdr``), pinned to the version checked here."""

    def __init__(
        self, binary: str = "stellar", expected_version: str = STELLAR_CLI_VERSION
    ) -> None:
        self.binary = binary
        found = self._run(["--version"], b"").decode().splitlines()[0]
        if not found.startswith(expected_version + " "):
            raise RuntimeError(f"{binary} is {found!r}, expected {expected_version}")
        self.version = found

    def _run(self, args: list[str], data: bytes) -> bytes:
        done = subprocess.run(
            [self.binary, *args], input=data, capture_output=True, timeout=60, check=False
        )
        if done.returncode:
            raise ValueError(done.stderr.decode(errors="replace").strip()[:300])
        return done.stdout

    def decode(self, xdr_type: str, record: bytes) -> dict[str, Any]:
        out = self._run(
            ["xdr", "decode", "--type", xdr_type, "--input", "single", "--output", "json"], record
        )
        value: dict[str, Any] = json.loads(out)
        return value

    def encode(self, xdr_type: str, value: Any) -> bytes:
        return self._run(
            ["xdr", "encode", "--type", xdr_type, "--input", "json", "--output", "single"],
            json.dumps(value).encode(),
        )


def transaction_hash(xdr: XdrTool, envelope: dict[str, Any], passphrase: str) -> str:
    """The hash of a transaction as the protocol defines it: sha256 of the XDR of its
    ``TransactionSignaturePayload`` (network id and tagged transaction; a fee bump is
    hashed as the outer transaction)."""
    network_id = hashlib.sha256(passphrase.encode()).hexdigest()
    if "tx" in envelope:
        tagged = {"tx": envelope["tx"]["tx"]}
    elif "tx_fee_bump" in envelope:
        tagged = {"tx_fee_bump": envelope["tx_fee_bump"]["tx"]}
    else:
        raise ValueError("legacy v0 envelopes are not supported")
    payload = {"network_id": network_id, "tagged_transaction": tagged}
    return hashlib.sha256(xdr.encode("TransactionSignaturePayload", payload)).hexdigest()


def frames(stream: bytes) -> list[bytes]:
    """Split an RFC 5531 record-marked stream (the archive file format) into records."""
    records, offset = [], 0
    while offset < len(stream):
        if offset + 4 > len(stream):
            raise ValueError("truncated record mark")
        mark = int.from_bytes(stream[offset : offset + 4], "big")
        if not mark & 0x80000000:
            raise ValueError("fragmented records are not used by history archives")
        length = mark & 0x7FFFFFFF
        record = stream[offset + 4 : offset + 4 + length]
        if len(record) != length:
            raise ValueError("truncated record")
        records.append(record)
        offset += 4 + length
    return records


def _gunzip(data: bytes) -> bytes:
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as handle:
        out = handle.read(MAX_DECOMPRESSED_BYTES + 1)
    if len(out) > MAX_DECOMPRESSED_BYTES:
        raise ValueError("decompressed file exceeds the bound")
    return out


# ------------------------------------------------------------------ anchors


@dataclass(frozen=True)
class Anchor:
    """The trusted hash of a checkpoint ledger and the checked facts of where it came from."""

    mode: AnchorMode
    ledger: int
    hash: str
    provenance: dict[str, Any]
    note: str | None = None


def manual_anchor(checkpoint: int, hash_: str, note: str | None) -> Anchor:
    """A hash supplied by hand: every result is relative to it, nothing more."""
    if not re.fullmatch(r"[0-9a-f]{64}", hash_):
        raise ValueError("an anchor is a 64-character lowercase hex hash")
    return Anchor("manual", checkpoint, hash_, {"supplied": "manually"}, note)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validators(config: Path) -> list[dict[str, str]]:
    document = tomllib.loads(config.read_text("utf-8"))
    return [
        {
            "name": v.get("NAME", ""),
            "public_key": v["PUBLIC_KEY"],
            "home_domain": v.get("HOME_DOMAIN", ""),
            "history": v.get("HISTORY", ""),
        }
        for v in document.get("VALIDATORS", [])
    ]


_SCP_CHECK = re.compile(r"Verifying ledger \[seq=(\d+), hash=([0-9a-f]+)\] against SCP hash")
_QUORUM = re.compile(r"Quorum information for (\d+) : (\{.*\})")


def run_record(
    directory: Path,
    *,
    from_ledger: int,
    image: str,
    core_version: str,
    started_at: str,
    finished_at: str,
    exit_code: int,
) -> dict[str, Any]:
    """The record of one ``verify-checkpoints`` run, from the artifacts it left in
    ``directory`` (config, output and log); every fact in it is derived from those files."""
    output = json.loads((directory / "anchors.json").read_text("utf-8"))
    entries = [(ledger, h) for ledger, h in output if ledger > 0]
    top_ledger, top_hash = max(entries)
    log = (directory / "verify-checkpoints.log").read_text("utf-8", errors="replace")
    scp = [(int(m[1]), m[2]) for m in _SCP_CHECK.finditer(log)]
    externalized = []
    for match in _QUORUM.finditer(log):
        info = json.loads(match[2])
        if info.get("phase") == "EXTERNALIZE":
            externalized.append(
                {
                    "ledger": int(match[1]),
                    "agree": info.get("agree"),
                    "disagree": info.get("disagree"),
                }
            )
    return {
        "kind": ANCHOR_RUN_KIND,
        "stellar_core": {"version": core_version, "image": image},
        "command": ["verify-checkpoints", "--from-ledger", str(from_ledger)],
        "config_sha256": _sha256_file(directory / "testnet.cfg"),
        "validators": [
            {k: v[k] for k in ("name", "public_key", "home_domain")}
            for v in _validators(directory / "testnet.cfg")
        ],
        "archives": [v["history"] for v in _validators(directory / "testnet.cfg")],
        "from_ledger": from_ledger,
        "top_checkpoint": {"ledger": top_ledger, "hash": top_hash},
        "checked_against_scp": [{"ledger": ledger, "hash_prefix": h} for ledger, h in scp],
        "externalized_seen": externalized[-3:],
        "anchors_sha256": _sha256_file(directory / "anchors.json"),
        "log_sha256": _sha256_file(directory / "verify-checkpoints.log"),
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": exit_code,
    }


def observe_anchors(
    directory: Path,
    config: Path,
    from_ledger: int,
    *,
    image: str = STELLAR_CORE_IMAGE,
    timeout_seconds: int = 900,
    run: Callable[[list[str], int], tuple[int, bytes]] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Path:
    """Run ``stellar-core verify-checkpoints`` in the pinned image and keep its record.

    The run observes the network's consensus (P2P) with the validators of ``config``,
    downloads checkpoint headers only (no bucket state) and verifies them back to
    ``from_ledger``. ``anchor-run.json`` ties the anchors to the run's config, version,
    image and log by sha256.
    """
    runner = run or _docker
    directory.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(config, directory / "testnet.cfg")
    mount = ["-v", f"{directory.resolve()}:/work", "-w", "/work"]
    user = ["--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/work"]
    base = ["docker", "run", "--rm", *user, *mount, "--entrypoint", "stellar-core", image]
    code, out = runner([*base, "version"], 60)
    if code:
        raise RuntimeError("stellar-core version failed")
    core_version = next(
        (line for line in out.decode().splitlines() if line.startswith("stellar-core ")), ""
    )
    code, _ = runner([*base, "new-db", "--conf", "/work/testnet.cfg"], 120)
    if code:
        raise RuntimeError("stellar-core new-db failed")
    started = now().strftime("%Y-%m-%dT%H:%M:%SZ")
    command = [
        *base,
        "verify-checkpoints",
        "--console",
        "--conf",
        "/work/testnet.cfg",
        "--from-ledger",
        str(from_ledger),
        "--output-file",
        "/work/anchors.json",
    ]
    code, log = runner(command, timeout_seconds)
    finished = now().strftime("%Y-%m-%dT%H:%M:%SZ")
    (directory / "verify-checkpoints.log").write_bytes(kept_log(log))
    # Only the config, the anchors, the kept log and the record stay; the run's database,
    # bucket directory and default log file are scratch.
    for target in directory.iterdir():
        if target.name in ("testnet.cfg", "anchors.json", "verify-checkpoints.log"):
            continue
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    if code or not (directory / "anchors.json").is_file():
        raise RuntimeError(f"verify-checkpoints failed (exit {code}); see the log")
    record = run_record(
        directory,
        from_ledger=from_ledger,
        image=image,
        core_version=core_version,
        started_at=started,
        finished_at=finished,
        exit_code=code,
    )
    path = directory / "anchor-run.json"
    path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", "utf-8")
    return path


_IPV4 = re.compile(rb"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b")


def kept_log(log: bytes) -> bytes:
    """The part of a run's log worth keeping: no debug lines, no peer connection lines
    (Overlay) and no IP address, so that third-party node addresses never reach a record."""
    kept = [
        _IPV4.sub(b"<address>", line)
        for line in log.splitlines()
        if b" DEBUG " not in line and b"Joining" not in line and b" [Overlay " not in line
    ]
    return b"\n".join(kept) + b"\n"


def _docker(command: list[str], timeout: int) -> tuple[int, bytes]:
    done = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    return done.returncode, done.stdout + done.stderr


def anchor_from_run(
    record_path: Path, checkpoint: int, *, observed_here: bool = False
) -> tuple[Anchor | None, list[str]]:
    """The anchor of ``checkpoint`` from a run record whose artifacts still match it.

    Unless this process produced the record (``observed_here``), the anchor is imported:
    the verifier observed no consensus itself, it only checked the record's artifacts.
    """
    problems: list[str] = []
    try:
        record = json.loads(record_path.read_text("utf-8"))
        directory = record_path.parent
        if record.get("kind") != ANCHOR_RUN_KIND or record.get("exit_code") != 0:
            return None, ["the anchor record is not a successful verify-checkpoints run"]
        for name, key in (
            ("anchors.json", "anchors_sha256"),
            ("testnet.cfg", "config_sha256"),
            ("verify-checkpoints.log", "log_sha256"),
        ):
            if _sha256_file(directory / name) != record[key]:
                problems.append(f"{name} does not match the anchor record")
        pairs = json.loads((directory / "anchors.json").read_text("utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as error:
        return None, [f"unreadable anchor record: {error}"]
    if problems:
        return None, problems
    if checkpoint < record["from_ledger"]:
        return None, [f"the run verified from ledger {record['from_ledger']}, not {checkpoint}"]
    found = [h for ledger, h in pairs if ledger == checkpoint]
    if len(found) != 1:
        return None, [f"the run holds no anchor for checkpoint {checkpoint}"]
    provenance = {
        "record_sha256": _sha256_file(record_path),
        "stellar_core": record["stellar_core"],
        "config_sha256": record["config_sha256"],
        "validators": record["validators"],
        "archives": record["archives"],
        "top_checkpoint": record["top_checkpoint"],
        "checked_against_scp": record["checked_against_scp"],
        "observed_at": record["finished_at"],
        "depends_on": "the signing keys of the validators named in the run's config, the "
        "archives it read and the pinned stellar-core build; not other operators",
    }
    mode: AnchorMode = "observed_run" if observed_here else "imported_with_provenance"
    return Anchor(mode, checkpoint, found[0], provenance), []


# ------------------------------------------------------------------ verification


@dataclass
class LedgerContent:
    seq: int
    close_time: int
    envelopes: list[dict[str, Any]]
    tx_hashes: list[str]
    results: dict[str, dict[str, Any]]


@dataclass
class CheckpointEvidence:
    status: EvidenceStatus
    checkpoint: int
    network: str
    anchor: Anchor | None
    problems: list[str] = field(default_factory=list)
    ledgers: dict[int, LedgerContent] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    header_hashes: dict[int, str] = field(default_factory=dict)
    # The verified LedgerHeader bodies (hashed to header_hashes), for contrast with a replay.
    headers: dict[int, dict[str, Any]] = field(default_factory=dict)


class _Inconsistent(Exception):
    pass


def _records(xdr: XdrTool, xdr_type: str, raw: bytes) -> list[dict[str, Any]]:
    """Decode every record, requiring each to re-encode to its exact bytes."""
    out = []
    for record in frames(raw):
        value = xdr.decode(xdr_type, record)
        if xdr.encode(xdr_type, value) != record:
            raise _Inconsistent(f"a {xdr_type} record does not re-encode to its bytes")
        out.append(value)
    return out


def _phase_envelopes(generalized: dict[str, Any]) -> list[dict[str, Any]]:
    envelopes: list[dict[str, Any]] = []
    for phase in generalized["v1"]["phases"]:
        if "v0" in phase:
            for component in phase["v0"]:
                envelopes += component["txset_comp_txs_maybe_discounted_fee"]["txs"]
        elif "v1" in phase:
            # Parallel Soroban phase: execution stages of dependent clusters.
            for stage in phase["v1"]["execution_stages"]:
                for cluster in stage:
                    envelopes += cluster
        else:
            raise ValueError(f"unknown transaction phase {sorted(phase)}")
    return envelopes


def verify_checkpoint(
    directory: Path,
    checkpoint: int,
    network: str,
    anchor: Anchor | None,
    xdr: XdrTool,
) -> CheckpointEvidence:
    """Integrity of one checkpoint's files against its anchor. Never raises on bad input."""
    evidence = CheckpointEvidence("INCOMPLETE", checkpoint, network, anchor)
    passphrase = NETWORK_PASSPHRASES.get(network)
    if passphrase is None:
        evidence.status = "REJECTED"
        evidence.problems.append(f"unknown network {network}")
        return evidence
    try:
        raw = {}
        for kind in KINDS:
            path = directory / Path(archive_path(kind, checkpoint)).name
            if not path.is_file():
                evidence.problems.append(f"missing {kind} file")
                continue
            raw[kind] = path.read_bytes()
            evidence.files[kind] = hashlib.sha256(raw[kind]).hexdigest()
        if anchor is None or anchor.ledger != checkpoint:
            evidence.problems.append(f"no anchor for checkpoint {checkpoint}")
        if evidence.problems:
            return evidence
        assert anchor is not None
        state = json.loads(raw["history"])
        if state.get("networkPassphrase") != passphrase:
            evidence.status = "REJECTED"
            evidence.problems.append("the archive belongs to another network")
            return evidence
        if state.get("currentLedger") != checkpoint:
            evidence.status = "REJECTED"
            evidence.problems.append("the history state names another checkpoint")
            return evidence
        headers = _records(xdr, "LedgerHeaderHistoryEntry", _gunzip(raw["ledger"]))
        txs = _records(xdr, "TransactionHistoryEntry", _gunzip(raw["transactions"]))
        results = _records(xdr, "TransactionHistoryResultEntry", _gunzip(raw["results"]))
    except _Inconsistent as error:
        evidence.status = "INCONSISTENT"
        evidence.problems.append(str(error))
        return evidence
    except (OSError, ValueError, KeyError, EOFError, json.JSONDecodeError) as error:
        evidence.problems.append(f"unreadable checkpoint files: {error}")
        return evidence
    try:
        return _verify_contents(evidence, anchor, passphrase, headers, txs, results, xdr)
    except (ValueError, KeyError, TypeError) as error:
        evidence.status = "INCOMPLETE"
        evidence.ledgers.clear()
        evidence.problems.append(f"checkpoint content not understood: {error}")
        return evidence


def _inconsistent(evidence: CheckpointEvidence, problem: str) -> CheckpointEvidence:
    evidence.status = "INCONSISTENT"
    evidence.ledgers.clear()
    evidence.header_hashes.clear()
    evidence.problems.append(problem)
    return evidence


def _verify_contents(
    evidence: CheckpointEvidence,
    anchor: Anchor,
    passphrase: str,
    headers: list[dict[str, Any]],
    txs: list[dict[str, Any]],
    results: list[dict[str, Any]],
    xdr: XdrTool,
) -> CheckpointEvidence:
    checkpoint = evidence.checkpoint
    expected = list(range(max(1, checkpoint - CHECKPOINT_FREQUENCY + 1), checkpoint + 1))
    by_seq = {h["header"]["ledger_seq"]: h for h in headers}
    if sorted(by_seq) != expected or len(headers) != len(expected):
        evidence.problems.append("the ledger file does not hold exactly the checkpoint's ledgers")
        return evidence
    for seq in expected:
        entry = by_seq[seq]
        if hashlib.sha256(xdr.encode("LedgerHeader", entry["header"])).hexdigest() != entry["hash"]:
            return _inconsistent(evidence, f"header {seq} does not hash to its recorded hash")
    for seq in expected[1:]:
        if by_seq[seq]["header"]["previous_ledger_hash"] != by_seq[seq - 1]["hash"]:
            return _inconsistent(evidence, f"header {seq} does not chain to {seq - 1}")
    evidence.header_hashes = {seq: by_seq[seq]["hash"] for seq in expected}
    evidence.headers = {seq: by_seq[seq]["header"] for seq in expected}
    if by_seq[checkpoint]["hash"] != anchor.hash:
        evidence.header_hashes, evidence.headers = {}, {}
        evidence.status = "ANCHOR_MISMATCH"
        evidence.problems.append(
            "the checkpoint header chain is internally consistent but its hash is not the "
            "anchor's: files and anchor come from different histories, or one of them is wrong"
        )
        return evidence
    tx_by_seq = {t["ledger_seq"]: t for t in txs}
    res_by_seq = {r["ledger_seq"]: r for r in results}
    for seq in expected:
        header = by_seq[seq]["header"]
        content = LedgerContent(seq, int(header["scp_value"]["close_time"]), [], [], {})
        tx_entry, res_entry = tx_by_seq.get(seq), res_by_seq.get(seq)
        if tx_entry is None or res_entry is None:
            # An archive omits a ledger only when it has no transactions; the hashes of the
            # empty sets cannot be checked here, so the ledger is left unverified.
            evidence.problems.append(f"ledger {seq} has no transaction or result entry")
            continue
        generalized = tx_entry["ext"].get("v1") if isinstance(tx_entry["ext"], dict) else None
        if generalized is None:
            evidence.problems.append(f"ledger {seq} has a legacy transaction set (not supported)")
            continue
        tx_set_hash = hashlib.sha256(
            xdr.encode("GeneralizedTransactionSet", generalized)
        ).hexdigest()
        if tx_set_hash != header["scp_value"]["tx_set_hash"]:
            return _inconsistent(
                evidence, f"ledger {seq}: transaction set does not match its header"
            )
        result_set = xdr.encode("TransactionResultSet", res_entry["tx_result_set"])
        if hashlib.sha256(result_set).hexdigest() != header["tx_set_result_hash"]:
            return _inconsistent(evidence, f"ledger {seq}: result set does not match its header")
        for envelope in _phase_envelopes(generalized):
            content.envelopes.append(envelope)
            content.tx_hashes.append(transaction_hash(xdr, envelope, passphrase))
        content.results = {
            r["transaction_hash"]: r["result"] for r in res_entry["tx_result_set"]["results"]
        }
        if sorted(content.tx_hashes) != sorted(content.results):
            return _inconsistent(
                evidence, f"ledger {seq}: transactions and results do not correspond"
            )
        evidence.ledgers[seq] = content
    if not evidence.problems:
        evidence.status = "VERIFIED"
    return evidence


# ------------------------------------------------------------------ observations

# What a check establishes, kept apart: the transaction's inclusion in a verified ledger,
# its technical result (transaction code and the observed operation's code), and whether
# the movement was executed. A failed transaction is included and charged its fee, but
# nothing it contains is applied: its inclusion never shows a transfer, even when the
# result lists an operation code of "success" before the one that failed.
Inclusion = Literal["INCLUDED", "NOT_INCLUDED", "NOT_ESTABLISHED"]
Movement = Literal["EXECUTED_PER_RESULT", "NOT_EXECUTED", "NOT_ESTABLISHED"]


@dataclass(frozen=True)
class ObservationCheck:
    observation_id: str
    status: ObservationStatus
    detail: str
    inclusion: Inclusion = "NOT_ESTABLISHED"
    technical_result: str | None = None
    movement: Movement = "NOT_ESTABLISHED"


def _inner_tx(envelope: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if "tx" in envelope:
        return envelope["tx"]["tx"], False
    if "tx_fee_bump" in envelope:
        return envelope["tx_fee_bump"]["tx"]["inner_tx"]["tx"]["tx"], True
    raise ValueError("legacy v0 envelopes are not supported")


def _outcome(result: dict[str, Any], fee_bump: bool, index: int) -> tuple[str, str | None]:
    """(transaction code, code of operation ``index``) from a transaction result."""
    outcome = result["result"]
    if fee_bump:
        key = next(iter(outcome)) if isinstance(outcome, dict) else str(outcome)
        if key not in ("tx_fee_bump_inner_success", "tx_fee_bump_inner_failed"):
            return key, None
        outcome = outcome[key]["result"]["result"]
    if not isinstance(outcome, dict):
        return str(outcome), None
    code = next(iter(outcome))
    operations = outcome[code]
    if not isinstance(operations, list) or index >= len(operations):
        return code, None
    op = operations[index]
    inner = op.get("op_inner") if isinstance(op, dict) else None
    if isinstance(inner, dict):
        value = next(iter(inner.values()))
        op_code = value if isinstance(value, str) else next(iter(value))
    else:
        op_code = str(op)
    return code, op_code


def check_observation(
    evidence: CheckpointEvidence, observation: Observation, target: ChainTarget
) -> ObservationCheck:
    """What the verified checkpoint establishes about one observation."""
    oid = observation.observation_id
    payload = observation.payload
    if not isinstance(payload, TokenMovementPayload):
        return ObservationCheck(
            oid, "CANNOT_RECONSTRUCT", f"{observation.fact_type} needs transaction meta or events"
        )
    chain = payload.chain
    if evidence.status != "VERIFIED":
        return ObservationCheck(oid, "UNVERIFIED", f"evidence is {evidence.status}")
    if chain.network != evidence.network:
        return ObservationCheck(oid, "CONTRADICTED", "observation names another network")
    if checkpoint_of(chain.ledger) != evidence.checkpoint:
        return ObservationCheck(oid, "UNVERIFIED", "ledger outside this checkpoint")
    ledger = evidence.ledgers.get(chain.ledger)
    if ledger is None:
        return ObservationCheck(oid, "UNVERIFIED", f"ledger {chain.ledger} not verified")
    if chain.tx_hash not in ledger.tx_hashes:
        return ObservationCheck(
            oid, "NOT_FOUND", "transaction not in the verified ledger", "NOT_INCLUDED"
        )
    envelope = ledger.envelopes[ledger.tx_hashes.index(chain.tx_hash)]
    tx, fee_bump = _inner_tx(envelope)
    result = ledger.results[chain.tx_hash]
    tx_code, op_code = _outcome(result, fee_bump, chain.operation_index)
    succeeded = tx_code == "tx_success"  # after unwrapping a fee bump, its inner result
    prefix = "fee bump, inner " if fee_bump else ""
    technical = f"{prefix}{tx_code}; operation {chain.operation_index}: {op_code}"
    operations = tx["operations"]
    if chain.operation_index >= len(operations):
        return ObservationCheck(
            oid, "CONTRADICTED", "operation index out of range", "INCLUDED", technical
        )
    operation = operations[chain.operation_index]
    body = operation["body"]
    if not isinstance(body, dict) or "payment" not in body:
        return ObservationCheck(
            oid,
            "CANNOT_RECONSTRUCT",
            "not a plain payment: what moved is in the transaction meta",
            "INCLUDED",
            technical,
        )
    executed = succeeded and op_code == "success"
    movement: Movement = "EXECUTED_PER_RESULT" if executed else "NOT_EXECUTED"
    payment = body["payment"]
    asset = payment["asset"]
    credit = (
        asset.get("credit_alphanum4") or asset.get("credit_alphanum12")
        if isinstance(asset, dict)
        else None
    )
    source = operation["source_account"] or tx["source_account"]
    differences = []
    if succeeded != chain.tx_successful:
        differences.append("success")
    if credit is None or (credit["asset_code"], credit["issuer"]) != (
        target.asset_code,
        target.asset_issuer,
    ):
        differences.append("asset")
    if _base(source) != payload.from_address or _base(payment["destination"]) != payload.to_address:
        differences.append("parties")
    if (
        _muxed_id(source) != payload.from_muxed_id
        or _muxed_id(payment["destination"]) != payload.to_muxed_id
    ):
        differences.append("muxed ids")
    if payload.units.scale != 7 or str(payload.units.atoms) != str(payment["amount"]):
        differences.append("amount")
    if differences:
        return ObservationCheck(
            oid,
            "CONTRADICTED",
            "differs in " + ", ".join(differences),
            "INCLUDED",
            technical,
            movement,
        )
    if executed:
        detail = (
            f"included in ledger {chain.ledger}; the payment operation succeeded, so the "
            "protocol moved exactly this amount (balance changes themselves are in the meta)"
        )
    else:
        detail = (
            f"included in ledger {chain.ledger} as a failed transaction: it shows the attempt "
            "and its fee, never a transfer"
        )
    return ObservationCheck(oid, "SUPPORTED", detail, "INCLUDED", technical, movement)


def _base(account: str) -> str:
    """The G account of a MuxedAccount, as the official JSON writes it (G or M strkey)."""
    return decode_muxed_account(account)[0] if account.startswith("M") else account


def _muxed_id(account: str) -> str | None:
    return str(decode_muxed_account(account)[1]) if account.startswith("M") else None


# ------------------------------------------------------------------ replay meta

# The meta a replay writes (LedgerCloseMeta) is not committed by any ledger header: it is
# a by-product of executing the anchored ledgers with a given build and configuration.
# ``check_replay`` ties it to what is committed (each ledger's header, transaction hashes
# and results must be the anchored ones); the events and entry changes inside stay
# "derived from a verified replay", never "committed by consensus".
ReplayStatus = Literal["REPLAY_CONSISTENT", "INCONSISTENT", "INCOMPLETE", "UNVERIFIED"]
EventStatus = Literal[
    "EVENT_MATCH", "EVENT_MISMATCH", "NO_MOVEMENT_IN_TX", "EVENT_NOT_RECONSTRUCTED", "UNVERIFIED"
]
# Exactly what REPLAY_CONSISTENT compares with the anchored checkpoint, and what it does
# not: it never authenticates the events or the state changes the meta holds.
REPLAY_CHECKS = {
    "checked": [
        "the stream holds exactly the checkpoint's ledgers, each once",
        "ledger_header.hash of each ledger equals the anchored header hash",
        "ledger_header.header of each ledger equals the verified header body",
        "the transaction hashes of each ledger's tx_processing are the anchored ones",
        "each tx_processing result (TransactionResult) equals the anchored result",
    ],
    "not_checked": [
        "tx_apply_processing: operation meta, contract events and ledger entry changes",
        "fee_processing and the fee events",
        "diagnostic events, upgrades_processing, evicted entries, scp_info and other fields",
    ],
    "meaning": "the meta replays the anchored ledgers; the events and entry changes it "
    "carries are not authenticated by this check",
}

# Where replay artifacts come from. observed_run: this process ran the catchup itself (no
# Invaria command does yet, so no code path produces it). imported_with_provenance: the
# run's record matches a sha256 the caller obtained through a channel it trusts, and every
# file matches the record. supplier_declared: the files match a record that came with
# them; without a trusted hash of that record, coherence is not authenticity.
# A trusted record hash shows correspondence with the expected record, nothing more: not
# that the run happened, nor who documented it truthfully; a hash kept apart today guards
# against later changes, it does not authenticate the original capture retroactively.
ReplayProvenance = Literal["observed_run", "imported_with_provenance", "supplier_declared"]
# ARTIFACTS_MATCH: every declared file is present and matches; REJECTED: a file is altered,
# undeclared or not a regular file, or the record is not the trusted one or not this
# checkpoint's run; INCOMPLETE: a declared file or field is missing or unreadable.
ArtifactStatus = Literal["ARTIFACTS_MATCH", "REJECTED", "INCOMPLETE"]
REPLAY_RUN_KIND = "stellar_core_catchup_replay"
REPLAY_RECORD = "replay-run.json"
# A later revision of the record, kept apart: it names the record it revises by sha256 and
# declares files added after the run, whose hashes were not computed during the run.
REPLAY_REVISION = "replay-run.revision-1.json"
REPLAY_REVISION_KIND = "stellar_core_catchup_replay_record_revision"


@dataclass
class ReplayArtifacts:
    status: ArtifactStatus
    provenance: ReplayProvenance
    record_sha256: str | None
    expected_record_sha256: str | None
    problems: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    record: dict[str, Any] | None = None
    # What the record and its config declare about the run: declared, not observed here.
    execution: dict[str, Any] = field(default_factory=dict)
    # The revision, if any: its sha256, the expected one, and the files it adds after the run.
    revision_sha256: str | None = None
    expected_revision_sha256: str | None = None
    added_after_run: dict[str, str] = field(default_factory=dict)
    revision: dict[str, Any] | None = None
    # Only set when every check passed: nothing is decoded from unchecked bytes.
    meta_stream: bytes | None = None


def _declared_files(record: dict[str, Any]) -> dict[str, str]:
    """The files a replay record declares, by name, with their sha256 (KeyError if absent)."""
    declared = {
        record["meta"]["kept"]: record["meta"]["kept_sha256"],
        record["log"]["kept"]: record["log"]["kept_sha256"],
        record["initial_state"]["history_state"]: record["initial_state"]["history_state_sha256"],
        "replay.cfg": record["config_sha256"],
        "fetch.sh": record["fetch_script_sha256"],
    }
    return declared


def _history_buckets(state: dict[str, Any]) -> set[str]:
    zero = "0" * 64
    found: set[str] = set()
    for key in ("currentBuckets", "hotArchiveBuckets"):
        for level in state.get(key, []):
            found.update(h for h in (level["curr"], level["snap"]) if h != zero)
    return found


def load_replay_artifacts(
    directory: Path,
    checkpoint: int,
    anchor: Anchor | None,
    *,
    expected_record_sha256: str | None = None,
    expected_revision_sha256: str | None = None,
) -> ReplayArtifacts:
    """Check a replay's artifacts against its record before anything is decoded.

    Fails closed: a missing, altered or undeclared file, a symlink, or a record that is not
    the trusted one (when ``expected_record_sha256`` is given) or not this checkpoint's run
    under this anchor leaves ``meta_stream`` unset. The record's own hashes are no root of
    trust: only a sha256 of the record obtained through another channel makes the files
    ``imported_with_provenance``; without it they stay ``supplier_declared``. A revision
    (``REPLAY_REVISION``) must name this record by sha256; the files it adds are reported
    apart, as added after the run, and its own sha256 is checked like the record's.
    """
    provenance: ReplayProvenance = (
        "imported_with_provenance" if expected_record_sha256 is not None else "supplier_declared"
    )
    out = ReplayArtifacts("INCOMPLETE", provenance, None, expected_record_sha256)
    out.expected_revision_sha256 = expected_revision_sha256

    def reject(problem: str) -> ReplayArtifacts:
        out.status = "REJECTED"
        out.problems.append(problem)
        return out

    record_path = directory / REPLAY_RECORD
    if not record_path.is_file() or record_path.is_symlink():
        out.problems.append(f"{REPLAY_RECORD} is missing")
        return out
    raw_record = record_path.read_bytes()
    out.record_sha256 = hashlib.sha256(raw_record).hexdigest()
    if expected_record_sha256 is not None and out.record_sha256 != expected_record_sha256:
        return reject("the replay record is not the trusted one (sha256 differs)")
    try:
        record = json.loads(raw_record)
        declared = _declared_files(record)
        exit_code = record["consumption"]["exit_code"]
        trusted = record["anchor"]["trusted_checkpoint"]
        anchor_record = record["anchor"]["record_sha256"]
        replayed = record["replayed_ledgers"]
        buckets = set(record["initial_state"]["bucket_hashes"])
        raw_meta_sha, raw_meta_bytes = record["meta"]["raw_sha256"], record["meta"]["raw_bytes"]
    except (ValueError, KeyError, TypeError) as error:
        out.problems.append(f"unreadable replay record: {error}")
        return out
    out.record = record
    if record.get("kind") != REPLAY_RUN_KIND or exit_code != 0:
        return reject("the record is not a successful stellar-core catchup run")
    revision_path = directory / REPLAY_REVISION
    if revision_path.is_symlink() or (revision_path.exists() and not revision_path.is_file()):
        return reject(f"{REPLAY_REVISION} is not a regular file")
    if revision_path.exists():
        raw_revision = revision_path.read_bytes()
        out.revision_sha256 = hashlib.sha256(raw_revision).hexdigest()
        if expected_revision_sha256 not in (None, out.revision_sha256):
            return reject("the record revision is not the trusted one (sha256 differs)")
        try:
            revision = json.loads(raw_revision)
            revises = revision["revises"]["sha256"]
            added = dict(revision["added_after_run"])
        except (ValueError, KeyError, TypeError) as error:
            out.problems.append(f"unreadable record revision: {error}")
            return out
        out.revision = revision
        if revision.get("kind") != REPLAY_REVISION_KIND or revises != out.record_sha256:
            return reject("the record revision does not revise this record")
        if set(added) & set(declared):
            return reject("the record revision redeclares a file of the record")
        out.added_after_run = added
        declared = {**declared, **added}
    elif expected_revision_sha256 is not None:
        out.problems.append(f"{REPLAY_REVISION} is missing")
        return out
    if any(
        "/" in name or name in (REPLAY_RECORD, REPLAY_REVISION, "", ".", "..") for name in declared
    ):
        return reject("the record declares a file outside its directory")
    present = {p.name for p in directory.iterdir()} - {REPLAY_RECORD, REPLAY_REVISION}
    for name in sorted(present - set(declared)):
        reject(f"{name} is not declared by the record")
    for name, sha in sorted(declared.items()):
        path = directory / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            reject(f"{name} is not a regular file")
        elif not path.exists():
            out.problems.append(f"{name} is missing")
        elif (actual := _sha256_file(path)) != sha:
            reject(f"{name} does not match the record")
        else:
            out.files[name] = actual
    if out.status == "REJECTED" or out.problems:
        return out
    if anchor is None:
        out.problems.append("no anchor to bind the replay to")
        return out
    if trusted != [checkpoint, anchor.hash]:
        return reject("the replay ran under another trusted checkpoint hash than the anchor")
    if anchor_record != anchor.provenance.get("record_sha256", anchor_record):
        return reject("the replay ran with another anchor record than the one verified")
    if replayed != [max(1, checkpoint - CHECKPOINT_FREQUENCY + 1), checkpoint]:
        return reject("the record replayed other ledgers than this checkpoint's")
    try:
        state = json.loads((directory / record["initial_state"]["history_state"]).read_bytes())
        if state["currentLedger"] != checkpoint - CHECKPOINT_FREQUENCY:
            return reject("the initial history state is not the previous checkpoint's")
        if _history_buckets(state) != buckets:
            return reject("the bucket references differ from the initial history state")
        stream = _gunzip((directory / record["meta"]["kept"]).read_bytes())
        config = tomllib.loads((directory / "replay.cfg").read_text("utf-8"))
        out.execution = {
            "stellar_core": record["stellar_core"],
            "command": record["command"],
            "exit_code": exit_code,
            "started_at": record["started_at"],
            "finished_at": record["finished_at"],
            "invariant_checks": config.get("INVARIANT_CHECKS", []),
            "emit_classic_events": config.get("EMIT_CLASSIC_EVENTS", False),
            "backfill_stellar_asset_events": config.get("BACKFILL_STELLAR_ASSET_EVENTS", False),
        }
    except (OSError, ValueError, KeyError, TypeError, EOFError) as error:
        out.problems.append(f"unreadable replay artifact: {error}")
        return out
    if len(stream) != raw_meta_bytes or hashlib.sha256(stream).hexdigest() != raw_meta_sha:
        return reject("the decompressed meta does not match the record")
    out.status = "ARTIFACTS_MATCH"
    out.meta_stream = stream
    return out


@dataclass
class ReplayEvidence:
    status: ReplayStatus
    checkpoint: int
    problems: list[str] = field(default_factory=list)
    # tx hash -> (ledger, TransactionResultMetaV1-like processing entry)
    processing: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    ledgers: list[int] = field(default_factory=list)


def verify_replay(
    evidence: CheckpointEvidence,
    directory: Path,
    xdr: XdrTool,
    *,
    expected_record_sha256: str | None = None,
    expected_revision_sha256: str | None = None,
) -> tuple[ReplayArtifacts, ReplayEvidence]:
    """The normal replay flow: artifacts against their record first, then the meta against
    the anchored checkpoint. Nothing is decoded unless the artifacts match."""
    artifacts = load_replay_artifacts(
        directory,
        evidence.checkpoint,
        evidence.anchor,
        expected_record_sha256=expected_record_sha256,
        expected_revision_sha256=expected_revision_sha256,
    )
    if artifacts.meta_stream is None:
        replay = ReplayEvidence("UNVERIFIED", evidence.checkpoint)
        replay.problems.append(f"replay artifacts are {artifacts.status}: meta not decoded")
        return artifacts, replay
    return artifacts, check_replay(evidence, artifacts.meta_stream, xdr)


def check_replay(evidence: CheckpointEvidence, meta_stream: bytes, xdr: XdrTool) -> ReplayEvidence:
    """Whether a replay's ``LedgerCloseMeta`` stream is the anchored checkpoint, executed
    (only the fields in ``REPLAY_CHECKS``). It does not check where the bytes came from:
    use ``verify_replay`` for artifacts on disk."""
    replay = ReplayEvidence("INCOMPLETE", evidence.checkpoint)
    if evidence.status != "VERIFIED":
        replay.status = "UNVERIFIED"
        replay.problems.append(f"checkpoint evidence is {evidence.status}")
        return replay
    try:
        metas = _records(xdr, "LedgerCloseMeta", meta_stream)
    except _Inconsistent as error:
        replay.status = "INCONSISTENT"
        replay.problems.append(str(error))
        return replay
    except ValueError as error:
        replay.problems.append(f"unreadable meta stream: {error}")
        return replay
    seen: dict[int, dict[str, Any]] = {}
    for meta in metas:
        version = next(iter(meta))
        if version not in ("v1", "v2"):
            replay.problems.append(f"unsupported LedgerCloseMeta {version}")
            return replay
        body = meta[version]
        seen[body["ledger_header"]["header"]["ledger_seq"]] = body
    if len(metas) != len(seen) or sorted(seen) != sorted(evidence.header_hashes):
        replay.problems.append("the meta stream does not hold exactly the checkpoint's ledgers")
        return replay
    for seq, body in sorted(seen.items()):
        if body["ledger_header"]["hash"] != evidence.header_hashes[seq]:
            replay.status = "INCONSISTENT"
            replay.problems.append(f"ledger {seq}: replayed header is not the anchored one")
            return replay
        if body["ledger_header"]["header"] != evidence.headers[seq]:
            replay.status = "INCONSISTENT"
            replay.problems.append(f"ledger {seq}: replayed header body is not the verified one")
            return replay
        verified = evidence.ledgers[seq]
        hashes = []
        for entry in body["tx_processing"]:
            pair = entry["result"]
            hashes.append(pair["transaction_hash"])
            if verified.results.get(pair["transaction_hash"]) != pair["result"]:
                replay.status = "INCONSISTENT"
                replay.problems.append(f"ledger {seq}: a replayed result is not the anchored one")
                return replay
            replay.processing[pair["transaction_hash"]] = (seq, entry)
        if sorted(hashes) != sorted(verified.results):
            replay.status = "INCONSISTENT"
            replay.problems.append(f"ledger {seq}: replayed transactions are not the anchored ones")
            return replay
    replay.ledgers = sorted(seen)
    replay.status = "REPLAY_CONSISTENT"
    return replay


@dataclass(frozen=True)
class EventCheck:
    observation_id: str
    status: EventStatus
    detail: str
    asset_events: tuple[dict[str, Any], ...] = ()
    balance_deltas: dict[str, int] = field(default_factory=dict)
    fee: dict[str, Any] = field(default_factory=dict)
    diagnostic_events: int = 0
    other_operations: int = 0
    memo_in_event: str | None = None


def _scval(value: Any) -> Any:
    if isinstance(value, dict) and len(value) == 1:
        kind, inner = next(iter(value.items()))
        if kind in ("symbol", "string", "address", "i128"):
            return inner
    return value


def _asset_event(event: dict[str, Any]) -> dict[str, Any]:
    body = event["body"]["v0"]
    topics = [_scval(t) for t in body["topics"]]
    data = body["data"]
    memo = None
    if isinstance(data, dict) and "map" in data:
        fields = {_scval(e["key"]): _scval(e["val"]) for e in data["map"]}
        amount = fields.get("amount")
        memo = fields.get("to_muxed_id")
    else:
        amount = _scval(data)
    return {"name": topics[0], "topics": topics[1:], "amount": str(amount), "memo": memo}


def _trustline_deltas(changes: list[dict[str, Any]], code: str, issuer: str) -> dict[str, int]:
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    for change in changes:
        kind = next(iter(change))
        entry = change[kind]
        if kind == "removed" or not isinstance(entry, dict):
            continue
        line = entry.get("data", {}).get("trustline")
        if line is None:
            continue
        asset = line["asset"].get("credit_alphanum4") or line["asset"].get("credit_alphanum12")
        if asset is None or (asset["asset_code"], asset["issuer"]) != (code, issuer):
            continue
        target = before if kind == "state" else after
        target[line["account_id"]] = int(line["balance"])
    return {a: after[a] - before.get(a, 0) for a in after if after[a] != before.get(a, 0)}


def check_event(
    replay: ReplayEvidence, observation: Observation, target: ChainTarget
) -> EventCheck:
    """Contrast one Classic movement with the asset events and trustline changes of the
    replayed operation. Fees and other operations are reported apart, never counted."""
    oid = observation.observation_id
    payload = observation.payload
    if replay.status != "REPLAY_CONSISTENT":
        return EventCheck(oid, "UNVERIFIED", f"replay is {replay.status}")
    if not isinstance(payload, TokenMovementPayload):
        return EventCheck(oid, "EVENT_NOT_RECONSTRUCTED", "not a token movement")
    chain = payload.chain
    found = replay.processing.get(chain.tx_hash)
    if found is None or found[0] != chain.ledger:
        return EventCheck(oid, "EVENT_NOT_RECONSTRUCTED", "transaction not in the replayed ledgers")
    _, entry = found
    apply = entry["tx_apply_processing"]
    if "v4" not in apply:
        return EventCheck(oid, "EVENT_NOT_RECONSTRUCTED", "meta is not TransactionMetaV4")
    meta = apply["v4"]
    fee = {
        "charged": entry["result"]["result"]["fee_charged"],
        "events": [_asset_event(t["event"]) for t in meta["events"]],
    }
    diagnostics = len(meta["diagnostic_events"])
    operations = meta["operations"]
    sac = target.expected_sac_contract_id
    if not chain.tx_successful:
        # A failed transaction applies nothing: no operation meta, no asset event, no
        # trustline change. Diagnostic events, if any, are reported, never read as movements.
        moved = [e for op in operations for e in op["events"] if e.get("contract_id") == sac]
        deltas = {}
        for op in operations:
            deltas.update(_trustline_deltas(op["changes"], target.asset_code, target.asset_issuer))
        if moved or deltas:
            return EventCheck(
                oid, "EVENT_MISMATCH", "a failed transaction shows an asset movement", fee=fee
            )
        return EventCheck(
            oid,
            "NO_MOVEMENT_IN_TX",
            "failed transaction: no asset event and no trustline change in this transaction; "
            "this says nothing about the account's other transactions",
            fee=fee,
            diagnostic_events=diagnostics,
            other_operations=len(operations),
        )
    if chain.operation_index >= len(operations):
        return EventCheck(oid, "EVENT_NOT_RECONSTRUCTED", "operation meta missing", fee=fee)
    op = operations[chain.operation_index]
    events = tuple(_asset_event(e) for e in op["events"] if e.get("contract_id") == sac)
    deltas = _trustline_deltas(op["changes"], target.asset_code, target.asset_issuer)
    asset = f"{target.asset_code}:{target.asset_issuer}"
    sender, receiver = payload.from_address, payload.to_address
    atoms = int(payload.units.atoms)
    if sender == target.asset_issuer:
        expected = {"name": "mint", "topics": [receiver, asset]}
        expected_deltas = {receiver: atoms}
    elif receiver == target.asset_issuer:
        expected = {"name": "burn", "topics": [sender, asset]}
        expected_deltas = {sender: -atoms}
    else:
        expected = {"name": "transfer", "topics": [sender, receiver, asset]}
        expected_deltas = {sender: -atoms, receiver: atoms}
    base = EventCheck(
        oid,
        "EVENT_MISMATCH",
        "",
        events,
        deltas,
        fee,
        diagnostics,
        len(operations) - 1,
        events[0]["memo"] if len(events) == 1 else None,
    )
    problems = []
    if len(events) != 1:
        problems.append(f"{len(events)} asset events in the operation")
    else:
        event = events[0]
        if (event["name"], event["topics"]) != (expected["name"], expected["topics"]):
            problems.append(f"event {event['name']} {event['topics']} is not {expected}")
        if event["amount"] != str(atoms):
            problems.append("event amount differs")
    if deltas != expected_deltas:
        problems.append(f"trustline changes {deltas} are not {expected_deltas}")
    if problems:
        return replace(base, detail="; ".join(problems))
    return replace(
        base,
        status="EVENT_MATCH",
        detail=f"{expected['name']} of {payload.units.atoms} reconstructed by the replay and "
        "matched by the trustline change; derived from a verified replay, not committed by "
        "consensus",
    )


# ------------------------------------------------------------------ report


def replay_report(
    evidence: CheckpointEvidence,
    artifacts: ReplayArtifacts,
    replay: ReplayEvidence,
    events: Sequence[EventCheck],
) -> dict[str, Any]:
    """The four results of a replay check, kept apart: the history against the anchor, the
    stellar-core run (declared by its record, never observed by an offline check), the
    integrity of the kept meta, and the correspondence with Invaria's observations."""
    anchor = evidence.anchor
    trusted = artifacts.expected_record_sha256 is not None
    return {
        "history_against_anchor": {
            "status": evidence.status,
            "anchor_mode": None if anchor is None else anchor.mode,
            "verifier_observed_consensus": anchor is not None and anchor.mode == "observed_run",
        },
        "core_execution": {
            "provenance": artifacts.provenance,
            "observed_by_this_process": artifacts.provenance == "observed_run",
            "declared_by_record": artifacts.execution,
            "invariants": "the invariants named in the run's config checked the events against "
            "the entry changes while the original run produced them; they do not protect a "
            "copy modified afterwards, which only the integrity checks below can expose",
        },
        "meta_integrity": {
            "status": artifacts.status,
            "record_sha256": artifacts.record_sha256,
            "expected_record_sha256": artifacts.expected_record_sha256,
            "trusted_record_source": "a sha256 of the record supplied by the caller, obtained "
            "through a channel it trusts (the record is no root of trust by itself)"
            if trusted
            else "none: the files were only checked against the record that came with them; "
            "internal coherence, not authenticity",
            "files_sha256": artifacts.files,
            "problems": artifacts.problems,
            "revision": None
            if artifacts.revision_sha256 is None
            else {
                "sha256": artifacts.revision_sha256,
                "expected_sha256": artifacts.expected_revision_sha256,
                "revises_record_sha256": artifacts.record_sha256,
                "added_after_run": artifacts.added_after_run,
                "hashes_contemporaneous": False,
            },
            "what_a_trusted_hash_shows": "correspondence with the expected record (anchored "
            "integrity of the record); not that the run happened, not that a declared "
            "provenance was observed, and not trust in whoever documented the run. A hash kept "
            "apart now guards against later changes; it does not authenticate the original "
            "capture retroactively",
        },
        "correspondence": {
            "status": replay.status,
            "problems": replay.problems,
            "replay_checks": REPLAY_CHECKS,
            "events": [asdict(e) for e in events],
            "events_trust": "derived from the replay; no ledger header commits them",
        },
    }


# The global result of a ledger-verify run, and its exit code: 0 only for CONSISTENT.
# CONTRADICTED: some check contradicts the anchored transaction or the replayed meta, even
# when every other check passes. NOT_ESTABLISHED: evidence, artifacts or replay are not
# verified, or an observation could not be checked. COHERENT_UNAUTHENTICATED: everything
# agrees, but the replay artifacts were only checked against their own record (or revision).
OverallStatus = Literal["CONSISTENT", "CONTRADICTED", "NOT_ESTABLISHED", "COHERENT_UNAUTHENTICATED"]


def overall_result(
    evidence: CheckpointEvidence,
    checks: Sequence[ObservationCheck],
    artifacts: ReplayArtifacts | None = None,
    replay: ReplayEvidence | None = None,
    events: Sequence[EventCheck] = (),
    completeness: str | None = None,
) -> dict[str, Any]:
    contradicted = [f"{c.observation_id}: {c.detail}" for c in checks if c.status == "CONTRADICTED"]
    contradicted += [
        f"{e.observation_id}: {e.detail}" for e in events if e.status == "EVENT_MISMATCH"
    ]
    missing = [] if evidence.status == "VERIFIED" else [f"evidence is {evidence.status}"]
    missing += [
        f"{c.observation_id}: {c.status}"
        for c in checks
        if c.status not in ("SUPPORTED", "CONTRADICTED")
    ]
    if completeness in ("CONTRADICTED", "GAPS_FOUND"):
        contradicted.append(f"classic payment completeness: {completeness}")
    elif completeness not in (None, "COMPLETE_IN_SCOPE"):
        missing.append(f"classic payment completeness: {completeness}")
    unauthenticated = []
    if artifacts is not None and replay is not None:
        if artifacts.status != "ARTIFACTS_MATCH":
            missing.append(f"replay artifacts are {artifacts.status}")
        if replay.status != "REPLAY_CONSISTENT":
            missing.append(f"replay is {replay.status}")
        missing += [
            f"{e.observation_id}: {e.status}"
            for e in events
            if e.status not in ("EVENT_MATCH", "NO_MOVEMENT_IN_TX", "EVENT_MISMATCH")
        ]
        if artifacts.expected_record_sha256 is None:
            unauthenticated.append("no trusted sha256 of the replay record")
        if artifacts.revision_sha256 is not None and artifacts.expected_revision_sha256 is None:
            unauthenticated.append("no trusted sha256 of the record revision")
    status: OverallStatus
    if contradicted:
        status = "CONTRADICTED"
    elif missing:
        status = "NOT_ESTABLISHED"
    elif unauthenticated:
        status = "COHERENT_UNAUTHENTICATED"
    else:
        status = "CONSISTENT"
    return {
        "status": status,
        "exit_code": 0 if status == "CONSISTENT" else 1,
        "contradictions": contradicted,
        "not_established": missing,
        "unauthenticated": unauthenticated,
    }


def report(
    evidence: CheckpointEvidence,
    checks: Sequence[ObservationCheck],
    tool_versions: Iterable[str],
    replay: dict[str, Any] | None = None,
    overall: dict[str, Any] | None = None,
    completeness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A reproducible report that keeps evidence integrity, anchor trust and extraction
    coverage apart; it says nothing about any financial result."""
    anchor = evidence.anchor
    return {
        "kind": "ledger_inclusion_report",
        "network": evidence.network,
        "checkpoint": evidence.checkpoint,
        "evidence": {
            "status": evidence.status,
            "problems": evidence.problems,
            "files_sha256": evidence.files,
            "ledgers_verified": sorted(evidence.ledgers),
            "integrity_scope": INTEGRITY_SCOPE,
        },
        "anchor": None
        if anchor is None
        else {
            "mode": anchor.mode,
            "ledger": anchor.ledger,
            "hash": anchor.hash,
            "provenance": anchor.provenance,
            "note": anchor.note,
            "relative_to_anchor": True,
            "verifier_observed_consensus": anchor.mode == "observed_run",
        },
        "coverage": {
            "kind": "inclusion_only",
            "completeness": "not_established",
            "not_in_archives": [
                "transaction meta and contract events (SAC transfer, mint, burn, clawback)",
                "ledger entry changes and balances",
                "effects Horizon derives (path payment legs, offer fills, balance claims)",
            ],
        },
        "observations": [asdict(c) for c in checks],
        "replay": replay,
        "classic_payment_completeness": completeness,
        "overall": overall,
        "tools": sorted(tool_versions),
        "financial_evaluation": "unchanged: no profile, result or coverage level is altered",
    }
