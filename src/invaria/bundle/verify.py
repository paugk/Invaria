"""Offline verifier for directory evidence bundles (level R1).

Reads only files inside the bundle directory; never follows symlinks, never executes
bundle content, never contacts sources. Replay uses the locally installed engine.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from invaria.bundle.build import MANIFEST, R1_ARTIFACTS, canonical_json
from invaria.contracts.base import StrictJsonError, parse_contract
from invaria.contracts.bundle import (
    BundleEvidence,
    EvidenceBundleManifest,
    VerificationReport,
    VerifierStatus,
)
from invaria.contracts.evaluation import EvaluationResult, FinancialResult, SnapshotRef
from invaria.contracts.profile import OperationProfile
from invaria.engine.evaluate import ENGINE_REF, EvaluationInputs, evaluate

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILES = 1_000
MAX_DEPTH = 8


@dataclass
class _Report:
    level: Literal["R1", "R2"]
    expected_manifest_sha256: str | None
    bundle_id: str | None = None
    manifest_sha256: str | None = None
    financial_result: FinancialResult | None = None
    artifacts_checked: int = 0
    reasons: list[str] = field(default_factory=list)

    def finish(self, status: VerifierStatus, reason: str | None = None) -> VerificationReport:
        if reason:
            self.reasons.append(reason)
        return VerificationReport(
            schema_version="1.0",
            bundle_id=self.bundle_id,
            level=self.level,
            status=status,
            financial_result=self.financial_result,
            reasons=self.reasons,
            manifest_sha256=self.manifest_sha256,
            trust=(
                "anchored_by_expected_manifest_sha256"
                if self.expected_manifest_sha256
                else "unanchored"
            ),
            local_engine_ref=ENGINE_REF,
            artifacts_checked=self.artifacts_checked,
        )


class _Stop(Exception):
    def __init__(self, status: VerifierStatus, reason: str) -> None:
        super().__init__(reason)
        self.status: VerifierStatus = status
        self.reason = reason


def _read_regular(path: Path, limit: int) -> bytes:
    """Read a regular file without following symlinks."""
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise _Stop("INCOMPLETE", f"missing file {path.name}") from error
    if not stat.S_ISREG(info.st_mode):
        raise _Stop("REJECTED", f"{path.name} is not a regular file")
    if info.st_size > limit:
        raise _Stop("REJECTED", f"{path.name} exceeds {limit} bytes")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise _Stop("REJECTED", f"{path.name} exceeds {limit} bytes")
    return data


def _inventory(root: Path) -> set[str]:
    """Every file under root as a relative POSIX path; reject anything but plain dirs/files."""
    found: set[str] = set()
    total = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        base = Path(current)
        for name in dirs:
            if (base / name).is_symlink():
                raise _Stop("REJECTED", f"symlink in bundle: {name}")
        for name in files:
            entry = base / name
            info = entry.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise _Stop("REJECTED", f"not a regular file: {name}")
            relative = entry.relative_to(root).as_posix()
            if relative.count("/") >= MAX_DEPTH:
                raise _Stop("REJECTED", f"path too deep: {relative}")
            total += info.st_size
            found.add(relative)
        if len(found) > MAX_FILES or total > MAX_TOTAL_BYTES:
            raise _Stop("REJECTED", "bundle exceeds file count or size limits")
    return found


def _parse[M: BaseModel](model: type[M], data: bytes, name: str) -> M:
    try:
        return parse_contract(model, data.decode("utf-8"))
    except (UnicodeDecodeError, StrictJsonError, ValidationError, ValueError) as error:
        raise _Stop(
            "REJECTED", f"{name} is not a valid {model.__name__}: {type(error).__name__}"
        ) from error


def _check_consistency(
    manifest: EvidenceBundleManifest,
    snapshot: SnapshotRef,
    evidence: BundleEvidence,
    recorded: EvaluationResult,
    profile: OperationProfile,
) -> None:
    ids = {manifest.snapshot_id, snapshot.snapshot_id, evidence.snapshot_id, recorded.snapshot_id}
    if len(ids) != 1:
        raise _Stop("REJECTED", f"snapshot_id differs across artifacts: {sorted(ids)}")
    members = (
        sorted(o.observation_id for o in evidence.observations),
        sorted(c.coverage_id for c in evidence.coverage),
        sorted(link.link_id for link in evidence.identity_links),
    )
    declared = (snapshot.observation_ids, snapshot.coverage_ids, snapshot.identity_link_ids)
    if members != declared:
        raise _Stop("REJECTED", "evidence.json members differ from the snapshot membership")
    if manifest.versions != recorded.versions:
        raise _Stop("REJECTED", "manifest versions differ from the recorded evaluation")
    if manifest.expected_result != recorded.result:
        raise _Stop("REJECTED", "manifest expected_result differs from the recorded evaluation")
    if profile.profile_ref != recorded.versions.profile_ref:
        raise _Stop("REJECTED", "profile.json is not the profile named by the evaluation")
    if (manifest.operation_ref, manifest.tenant_id) != (snapshot.operation_ref, snapshot.tenant_id):
        raise _Stop("REJECTED", "manifest operation or tenant differs from the snapshot")


def _verify(root: Path, report: _Report) -> VerificationReport:
    if root.is_symlink() or not root.is_dir():
        raise _Stop("REJECTED", "bundle path is not a real directory")
    manifest_bytes = _read_regular(root / MANIFEST, MAX_ARTIFACT_BYTES)
    report.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if report.expected_manifest_sha256 and (
        report.expected_manifest_sha256 != report.manifest_sha256
    ):
        raise _Stop("UNTRUSTED", "manifest sha256 differs from the expected value")
    manifest = _parse(EvidenceBundleManifest, manifest_bytes, MANIFEST)
    report.bundle_id = manifest.bundle_id

    declared = {artifact.path for artifact in manifest.artifacts}
    undeclared = _inventory(root) - declared - {MANIFEST}
    if undeclared:
        raise _Stop("REJECTED", f"undeclared files in bundle: {sorted(undeclared)}")

    if report.level != "R1":
        raise _Stop("INCOMPLETE", f"level {report.level} is not supported by this verifier")
    if manifest.integrity.replay != "R1":
        raise _Stop("INCOMPLETE", "manifest does not declare R1 replay")
    r1 = {a.path for a in manifest.artifacts if "R1" in a.required_for}
    missing_declarations = sorted(set(R1_ARTIFACTS) - r1)
    if missing_declarations:
        raise _Stop("INCOMPLETE", f"R1 artifacts not declared: {missing_declarations}")

    contents: dict[str, bytes] = {}
    for artifact in manifest.artifacts:
        data = _read_regular(root / artifact.path, MAX_ARTIFACT_BYTES)
        if hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise _Stop("REJECTED", f"sha256 mismatch for {artifact.path}")
        contents[artifact.path] = data
        report.artifacts_checked += 1

    snapshot = _parse(SnapshotRef, contents["snapshot.json"], "snapshot.json")
    profile = _parse(OperationProfile, contents["profile.json"], "profile.json")
    evidence = _parse(BundleEvidence, contents["evidence.json"], "evidence.json")
    recorded = _parse(EvaluationResult, contents["evaluation.json"], "evaluation.json")
    report.financial_result = recorded.result
    _check_consistency(manifest, snapshot, evidence, recorded, profile)

    if recorded.versions.engine_ref != ENGINE_REF:
        raise _Stop(
            "INCOMPLETE",
            f"engine {recorded.versions.engine_ref} is not available "
            f"locally ({ENGINE_REF}); nothing from the bundle is executed",
        )

    replayed = evaluate(
        EvaluationInputs(
            snapshot=snapshot,
            profile=profile,
            observations={o.observation_id: o for o in evidence.observations},
            coverage={c.coverage_id: c for c in evidence.coverage},
            identity_links={link.link_id: link for link in evidence.identity_links},
        )
    )
    if canonical_json(replayed.result) != contents["evaluation.json"]:
        before = {c.control_id: (c.status, c.reason_code) for c in recorded.controls}
        after = {c.control_id: (c.status, c.reason_code) for c in replayed.result.controls}
        changed = sorted(
            cid for cid in before.keys() | after.keys() if before.get(cid) != after.get(cid)
        )
        detail = (
            f"replayed result {replayed.result.result} vs recorded {recorded.result}; "
            f"controls differing: {changed or 'none (other fields differ)'}"
        )
        raise _Stop("MISMATCH", detail)
    report.reasons.append(f"R1 replay reproduced {recorded.result} with {ENGINE_REF}")
    if report.expected_manifest_sha256 is None:
        report.reasons.append("no expected manifest sha256 given: integrity is unanchored")
    return report.finish("REPRODUCED")


def verify_bundle(
    root: Path, *, level: Literal["R1", "R2"] = "R1", expected_manifest_sha256: str | None = None
) -> VerificationReport:
    report = _Report(level=level, expected_manifest_sha256=expected_manifest_sha256)
    try:
        return _verify(root, report)
    except _Stop as stop:
        return report.finish(stop.status, stop.reason)
    except OSError as error:
        return report.finish("REJECTED", f"cannot read bundle: {type(error).__name__}")
