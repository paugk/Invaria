"""Append-only local ingest store: observations, exclusions, coverage, raw pages, checkpoints.

Minimal file persistence so a checkpoint never advances before the data it covers is on
disk (written, flushed and fsync'ed). It is NOT the product persistence layer.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

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


class IngestStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        for sub in ("pages", "checkpoints"):
            (root / sub).mkdir(parents=True, exist_ok=True)

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

    def exclusions(self) -> list[dict[str, str]]:
        return [json.loads(line) for line in self._read_lines("exclusions.jsonl")]

    def append_exclusions(self, exclusions: Iterable[dict[str, str]]) -> None:
        known = {(e["path"], e["locator"], e["reason"]) for e in self.exclusions()}
        fresh = [e for e in exclusions if (e["path"], e["locator"], e["reason"]) not in known]
        self._append_lines("exclusions.jsonl", (_canonical(e) for e in fresh))

    def coverage(self) -> list[CoverageCertificate]:
        return [
            parse_contract(CoverageCertificate, line) for line in self._read_lines("coverage.jsonl")
        ]

    def append_coverage(self, certificate: CoverageCertificate) -> None:
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
