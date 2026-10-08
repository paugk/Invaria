"""Append-only local ingest store: observations, exclusions, coverage, raw pages, checkpoints.

Minimal file persistence so a checkpoint never advances before the data it covers is on
disk (written, flushed and fsync'ed). It is NOT the product persistence layer.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from invaria.contracts.base import parse_contract
from invaria.contracts.chain import IngestionCheckpoint, IngestPath
from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.observation import Observation


def _canonical(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


# 2: path payment index and correspondence. 3: index of the operations Horizon
# lists for the account, and exclusions with their page and addresses (scoped quarantine).
# 4: clawbacks become chain effects with their correspondence; a
# complete range of an older store would keep them as unsupported exclusions.
# 5: typed parties: claimable balances, contracts and liquidity
# pools become typed effects with claimable balance checks; an older store keeps them as
# unsupported or contract-party exclusions.
# 8: muxed accounts and memos: payments keep their muxed ids and
# memo (mapping 1.1.0), the path payment index keeps them for the SAC legs, and a SAC event
# with a Classic record is checked against it; an older store holds payments without memo
# that would conflict with the new records.
STORE_FORMAT = "invaria-ingest-store@8"


class IngestStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        marker = root / "FORMAT"
        if marker.is_file():
            found = marker.read_text("utf-8").strip()
            if found != STORE_FORMAT:
                raise ValueError(f"ingest store format {found!r}, expected {STORE_FORMAT!r}")
        elif root.is_dir() and any(root.glob("*.jsonl")):
            # A store written before the path payment index has complete checkpoints
            # without it: resuming it would keep path legs as movements. Fail closed.
            raise ValueError(
                f"{root} predates {STORE_FORMAT} (no path payment or listed operation index); "
                f"ingest into a new store"
            )
        for sub in ("pages", "checkpoints"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        if not marker.is_file():
            _atomic_write(marker, f"{STORE_FORMAT}\n".encode())

    # ----------------------------------------------------------- append-only logs

    def _append_lines(self, name: str, lines: Iterable[str]) -> None:
        payload = "".join(f"{line}\n" for line in lines)
        if not payload:
            return
        with open(self.root / name, "a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    def _read_lines(self, name: str) -> list[str]:
        path = self.root / name
        if not path.exists():
            return []
        return [line for line in path.read_text("utf-8").splitlines() if line]

    def observations(self) -> list[Observation]:
        return [
            parse_contract(Observation, line) for line in self._read_lines("observations.jsonl")
        ]

    def append_observations(self, observations: Iterable[Observation]) -> None:
        self._append_lines(
            "observations.jsonl", (_canonical(o.model_dump(mode="json")) for o in observations)
        )

    def exclusions(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self._read_lines("exclusions.jsonl")]

    def append_exclusions(self, exclusions: Iterable[dict[str, Any]]) -> None:
        """Per target and page: one record can be excluded for several targets of an asset."""

        def key(e: dict[str, Any]) -> tuple[Any, ...]:
            # The page too: a live re-read of a page has another sha, and the coverage keeps
            # only the exclusions of the pages its checkpoint names.
            return (e.get("target_id"), e.get("page"), e["path"], e["locator"], e["reason"])

        known = {key(e) for e in self.exclusions()}
        fresh = []
        for entry in exclusions:
            if key(entry) not in known:
                known.add(key(entry))
                fresh.append(entry)
        self._append_lines("exclusions.jsonl", (_canonical(e) for e in fresh))

    def path_operations(self) -> list[dict[str, Any]]:
        """Classic path payments seen by Horizon, used to classify their SAC events."""
        return [json.loads(line) for line in self._read_lines("path_operations.jsonl")]

    def append_path_operations(self, entries: Iterable[dict[str, Any]]) -> None:
        """One entry per (target, operation): the flags depend on the target's asset."""
        known = {(e["target_id"], e["tx_hash"], e["op_index"]) for e in self.path_operations()}
        fresh = []
        for entry in entries:
            key = (entry["target_id"], entry["tx_hash"], entry["op_index"])
            if key not in known:
                known.add(key)
                fresh.append(entry)
        self._append_lines("path_operations.jsonl", (_canonical(e) for e in fresh))

    def listed_operations(self) -> list[dict[str, Any]]:
        """Operations Horizon listed among the account's payments, per target."""
        return [json.loads(line) for line in self._read_lines("listed_operations.jsonl")]

    def append_listed_operations(self, entries: Iterable[dict[str, Any]]) -> None:
        known = {(e["target_id"], e["tx_hash"], e["op_index"]) for e in self.listed_operations()}
        fresh = []
        for entry in entries:
            key = (entry["target_id"], entry["tx_hash"], entry["op_index"])
            if key not in known:
                known.add(key)
                fresh.append(entry)
        self._append_lines("listed_operations.jsonl", (_canonical(e) for e in fresh))

    def correspondence(self) -> list[dict[str, Any]]:
        """Append-only history of correspondence checks per (target, operation)."""
        return [json.loads(line) for line in self._read_lines("correspondence.jsonl")]

    def current_correspondence(self) -> dict[tuple[str, str, int], dict[str, Any]]:
        """The latest check per (target, operation); earlier ones stay as history."""
        latest: dict[tuple[str, str, int], dict[str, Any]] = {}
        for entry in self.correspondence():
            latest[(entry["target_id"], entry["tx_hash"], entry["op_index"])] = entry
        return latest

    def append_correspondence(self, entries: Iterable[dict[str, Any]]) -> None:
        """A check is new when its evidence (the set of legs) differs from the latest one."""
        latest = self.current_correspondence()
        fresh = []
        for entry in entries:
            key = (entry["target_id"], entry["tx_hash"], entry["op_index"])
            if key not in latest or latest[key]["evidence_sha256"] != entry["evidence_sha256"]:
                latest[key] = entry
                fresh.append(entry)
        self._append_lines("correspondence.jsonl", (_canonical(e) for e in fresh))

    def coverage(self) -> list[CoverageCertificate]:
        return [
            parse_contract(CoverageCertificate, line) for line in self._read_lines("coverage.jsonl")
        ]

    def append_coverage(self, certificate: CoverageCertificate) -> None:
        # The first certificate recorded under an id is kept; a later run with revised
        # evidence over the same range keeps its records in the store, not its certificate
        # (declared limit). A ``supersedes`` claim names the content it replaces
        # by sha256, so the engines never apply it to another content.
        if certificate.coverage_id in {c.coverage_id for c in self.coverage()}:
            return
        self._append_lines("coverage.jsonl", [_canonical(certificate.model_dump(mode="json"))])

    # ------------------------------------------------------------- raw pages

    def save_page(self, sha256: str, raw: bytes) -> None:
        path = self.root / "pages" / f"{sha256}.json"
        if not path.exists():
            _atomic_write(path, raw)

    # ------------------------------------------------------------- checkpoints

    def _checkpoint_path(self, target_id: str, path: IngestPath) -> Path:
        return self.root / "checkpoints" / f"{target_id}__{path}.json"

    def get_checkpoint(self, target_id: str, path: IngestPath) -> IngestionCheckpoint | None:
        file = self._checkpoint_path(target_id, path)
        if not file.exists():
            return None
        return parse_contract(IngestionCheckpoint, file.read_text("utf-8"))

    def put_checkpoint(self, checkpoint: IngestionCheckpoint) -> None:
        data = _canonical(checkpoint.model_dump(mode="json")).encode("utf-8")
        _atomic_write(self._checkpoint_path(checkpoint.target_id, checkpoint.path), data)
