"""Offline verifier for directory evidence bundles (levels R1 and R2).

Reads only files inside the bundle directory, opening every path component relative to the
root's file descriptor with ``O_NOFOLLOW``; never follows symlinks, never writes, never
executes bundle content, never contacts sources. Replay uses the trusted local runtime
(``invaria.bundle.runtime``). Trust comes only from the caller: an expected manifest sha256
received out of band and/or a trust store for detached signatures.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from invaria.bundle.build import MANIFEST, R1_ARTIFACTS, canonical_json, mapping_path, raw_path
from invaria.bundle.fs import Stop as _Stop
from invaria.bundle.fs import inventory, json_depth, open_root, read_at
from invaria.bundle.runtime import (
    BLOCKED_ENGINES,
    RETIRED_ENGINES,
    TRUSTED_ENGINES,
    TRUSTED_NORMALIZERS,
    engine_source_sha256,
)
from invaria.bundle.signing import SIGNATURE_FILE, TrustPolicy, evaluate_trust
from invaria.contracts.base import StrictJsonError, parse_contract
from invaria.contracts.bundle import (
    RESERVED_PATHS,
    BundleEvidence,
    BundleSignatures,
    EvidenceBundleManifest,
    TrustState,
    TrustStore,
    VerificationReport,
    VerifierStatus,
)
from invaria.contracts.evaluation import EvaluationResult, FinancialResult, SnapshotRef
from invaria.contracts.mapping import CsvMapping
from invaria.contracts.observation import Observation
from invaria.contracts.profile import Profile, parse_profile
from invaria.engine.evaluate import ENGINE_REF, EvaluationInputs
from invaria.engine.versions import (
    ENGINE_IMPLEMENTATION,
    ENGINE_OPERATION,
    EngineImplementation,
    EngineStatus,
    engine_status,
)
from invaria.ingest.csv_import import ImportContext, ParseResult, parse_csv

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILES = 1_000
MAX_DEPTH = 8
MAX_JSON_DEPTH = 64
MAX_REASON_CHARS = 2000
MAX_LISTED = 10
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ROW = re.compile(r"^[1-9][0-9]{0,9}$")

Level = Literal["R1", "R2"]


def _cap(text: str) -> str:
    if len(text) <= MAX_REASON_CHARS:
        return text
    return text[: MAX_REASON_CHARS - 20] + " ... (truncated)"


def _listing(items: Iterable[str]) -> str:
    """At most MAX_LISTED items plus a count: reasons stay bounded for hostile inputs."""
    ordered = sorted(items)
    shown = ", ".join(ordered[:MAX_LISTED])
    more = len(ordered) - MAX_LISTED
    return f"[{len(ordered)}, {more} not listed] {shown}" if more > 0 else shown


@dataclass
class _Report:
    level: Level
    expected_manifest_sha256: str | None
    bundle_id: str | None = None
    manifest_sha256: str | None = None
    financial_result: FinancialResult | None = None
    artifacts_checked: int = 0
    anchored: bool = False
    signer_key_ids: list[str] = field(default_factory=list)
    renormalized: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    engine_ref: str = ENGINE_REF  # the local engine that replayed (or would replay) it
    engine_status: EngineStatus | None = None
    engine_policy: Literal["any", "current"] = "any"
    engine_implementation: EngineImplementation | None = None

    def trust(self) -> TrustState:
        if self.anchored and self.signer_key_ids:
            return "anchored_and_trusted_signature"
        if self.signer_key_ids:
            return "trusted_signature"
        if self.anchored:
            return "anchored_by_expected_manifest_sha256"
        return "unanchored"

    def finish(self, status: VerifierStatus, reason: str | None = None) -> VerificationReport:
        if reason:
            self.reasons.append(reason)
        self.reasons = [_cap(r) for r in self.reasons]
        return VerificationReport(
            schema_version="1.0",
            bundle_id=self.bundle_id,
            level=self.level,
            status=status,
            financial_result=self.financial_result,
            reasons=self.reasons,
            manifest_sha256=self.manifest_sha256,
            trust=self.trust(),
            signer_key_ids=sorted(self.signer_key_ids),
            local_engine_ref=self.engine_ref,
            engine_status=self.engine_status,
            engine_implementation=self.engine_implementation,
            engine_source_sha256=engine_source_sha256(),
            artifacts_checked=self.artifacts_checked,
            renormalized_observation_ids=sorted(self.renormalized),
        )


def _parse_profile(data: bytes) -> Profile:
    if json_depth(data) > MAX_JSON_DEPTH:
        raise _Stop("REJECTED", f"profile.json nests JSON deeper than {MAX_JSON_DEPTH}")
    try:
        return parse_profile(data.decode("utf-8"))
    except (UnicodeDecodeError, StrictJsonError, ValidationError, ValueError, RecursionError) as e:
        raise _Stop(
            "REJECTED", f"profile.json is not a valid operation profile: {type(e).__name__}"
        ) from e


def _parse[M: BaseModel](model: type[M], data: bytes, name: str) -> M:
    if json_depth(data) > MAX_JSON_DEPTH:
        raise _Stop("REJECTED", f"{name} nests JSON deeper than {MAX_JSON_DEPTH}")
    try:
        return parse_contract(model, data.decode("utf-8"))
    except (
        UnicodeDecodeError,
        StrictJsonError,
        ValidationError,
        ValueError,
        RecursionError,
    ) as error:
        raise _Stop(
            "REJECTED", f"{name} is not a valid {model.__name__}: {type(error).__name__}"
        ) from error


# ------------------------------------------------------------------ checks


def _check_consistency(
    manifest: EvidenceBundleManifest,
    snapshot: SnapshotRef,
    evidence: BundleEvidence,
    recorded: EvaluationResult,
    profile: Profile,
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


def _check_trust(
    data: bytes | None,
    manifest_sha256: str,
    store: TrustStore | None,
    policy: TrustPolicy,
    report: _Report,
) -> None:
    signatures = None if data is None else _parse(BundleSignatures, data, SIGNATURE_FILE)
    if store is None:
        if signatures is not None:
            report.reasons.append("signature.json present but no trust store given: not verified")
        return
    if signatures is None:
        raise _Stop("UNTRUSTED", "a trust store was given but the bundle is unsigned")
    decision = evaluate_trust(store, signatures, manifest_sha256, policy)
    report.reasons.extend(decision.reasons)
    if not decision.satisfied(policy):
        raise _Stop(
            "UNTRUSTED",
            f"{len(decision.trusted_key_ids)} trusted signature(s); "
            f"policy requires {policy.min_signatures}",
        )
    report.signer_key_ids = list(decision.trusted_key_ids)


@dataclass
class _R2:
    """R2 state: parse each (raw file, mapping, recorded_at) once; every row cited once."""

    snapshot: SnapshotRef
    profile: Profile
    contents: dict[str, bytes]
    members: dict[str, Observation]
    parsed: dict[tuple[str, str, str], ParseResult | str] = field(default_factory=dict)
    rows_seen: dict[tuple[str, str], str] = field(default_factory=dict)


def _renormalize(
    evidence: BundleEvidence,
    snapshot: SnapshotRef,
    profile: Profile,
    contents: dict[str, bytes],
    report: _Report,
) -> None:
    """R2: rebuild every observation from raw bytes with its mapping and compare.

    Coverage certificates and identity links are declarations (a source's completeness
    claim, an approved link), not rows of a raw file: they are verified at R1 only, and the
    report says so.
    """
    state = _R2(snapshot, profile, contents, {o.observation_id: o for o in evidence.observations})
    mismatches: list[str] = []
    unavailable: list[str] = []
    for o in evidence.observations:
        problem = _renormalize_one(o, state)
        if problem is None:
            report.renormalized.append(o.observation_id)
        elif problem[0] == "MISMATCH":
            mismatches.append(f"{o.observation_id}: {problem[1]}")
        else:
            unavailable.append(f"{o.observation_id}: {problem[1]}")
    if mismatches:
        raise _Stop("MISMATCH", "R2 renormalisation differs: " + _listing(mismatches))
    if unavailable:
        report.reasons.append(
            f"R2 renormalised {len(report.renormalized)} of {len(state.members)} observations"
        )
        raise _Stop("INCOMPLETE", "R2 not possible for: " + _listing(unavailable))
    report.reasons.append(
        "coverage certificates and identity links are declarations: verified at R1 only"
    )


def _parsed(o: Observation, path: str, state: _R2) -> ParseResult | str:
    key = (path, o.provenance.mapping_ref, o.recorded_at.isoformat())
    if key in state.parsed:
        return state.parsed[key]
    mapping_bytes = state.contents[mapping_path(o.provenance.mapping_ref)]
    result: ParseResult | str
    if json_depth(mapping_bytes) > MAX_JSON_DEPTH:
        result = "mapping nests JSON too deeply"
    else:
        try:
            mapping = parse_contract(CsvMapping, mapping_bytes.decode("utf-8"))
        except (UnicodeDecodeError, StrictJsonError, ValidationError, ValueError, RecursionError):
            result = "mapping is not a valid CsvMapping"
        else:
            if mapping.mapping_ref != o.provenance.mapping_ref:
                result = f"mapping file holds {mapping.mapping_ref}"
            else:
                context = ImportContext(
                    tenant_id=state.snapshot.tenant_id,
                    instrument_id=state.profile.instrument.instrument_id,
                    raw_path=path,
                    recorded_at=o.recorded_at,
                    coverage=None,
                )
                result = parse_csv(state.contents[path], mapping, context)
    state.parsed[key] = result
    return result


def _renormalize_one(
    o: Observation, state: _R2
) -> tuple[Literal["MISMATCH", "INCOMPLETE"], str] | None:
    parser = o.provenance.parser_ref
    if parser not in TRUSTED_NORMALIZERS:
        return "INCOMPLETE", f"no local normalizer for {parser}"
    path = raw_path(o.provenance.raw_locator)
    if path not in state.contents or mapping_path(o.provenance.mapping_ref) not in state.contents:
        return "INCOMPLETE", f"raw bytes or mapping {o.provenance.mapping_ref} not in bundle"
    if hashlib.sha256(state.contents[path]).hexdigest() != o.provenance.raw_sha256:
        return "MISMATCH", f"{path} is not the raw file the observation cites"
    _, _, locator = o.provenance.raw_locator.partition("#row=")
    if not _ROW.fullmatch(locator):
        return "MISMATCH", "unsupported raw_locator"
    cited = (o.provenance.raw_sha256, locator)
    if cited in state.rows_seen:
        return "MISMATCH", f"row {locator} already renormalised as {state.rows_seen[cited]}"
    state.rows_seen[cited] = o.observation_id
    parsed = _parsed(o, path, state)
    if isinstance(parsed, str):
        return "MISMATCH", parsed
    if not parsed.accepted:
        return "MISMATCH", f"raw file rejected by the parser ({parsed.file_reason})"
    row = next((c for c in parsed.candidates if c.row == int(locator)), None)
    if row is None:
        return "MISMATCH", f"row {locator} yields no observation"
    rebuilt = row.to_observation(o.supersedes)
    keep = {"observation_id"}
    if rebuilt.model_dump(exclude=keep) != o.model_dump(exclude=keep):
        return "MISMATCH", f"row {locator} renormalises to different content"
    if o.supersedes is not None:
        target = state.members.get(o.supersedes)
        if target is None or (target.source.source_id, target.source.record_key) != (
            o.source.source_id,
            o.source.record_key,
        ):
            return "MISMATCH", f"supersedes {o.supersedes} is not the same source record"
    return None


# ------------------------------------------------------------------ verify


def _verify(
    root: Path,
    report: _Report,
    store: TrustStore | None,
    policy: TrustPolicy,
) -> VerificationReport:
    budget = MAX_TOTAL_BYTES

    def read(root_fd: int, relative: str) -> bytes | None:
        nonlocal budget
        data = read_at(root_fd, relative, min(MAX_ARTIFACT_BYTES, budget))
        if data is not None:
            budget -= len(data)
        return data

    with open_root(root) as root_fd:
        files, directories = inventory(
            root_fd, max_entries=MAX_FILES, max_bytes=MAX_TOTAL_BYTES, max_depth=MAX_DEPTH
        )
        manifest_bytes = read(root_fd, MANIFEST)
        if manifest_bytes is None:
            raise _Stop("INCOMPLETE", f"missing file {MANIFEST}")
        report.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if report.expected_manifest_sha256 is not None:
            if report.expected_manifest_sha256 != report.manifest_sha256:
                raise _Stop("UNTRUSTED", "manifest sha256 differs from the expected value")
            report.anchored = True
        manifest = _parse(EvidenceBundleManifest, manifest_bytes, MANIFEST)
        report.bundle_id = manifest.bundle_id

        declared = {artifact.path for artifact in manifest.artifacts}
        undeclared = files - declared - RESERVED_PATHS
        if undeclared:
            raise _Stop("REJECTED", f"undeclared files in bundle: {_listing(undeclared)}")
        parents = {"/".join(p.split("/")[:i]) for p in declared for i in range(1, p.count("/") + 1)}
        stray = directories - parents
        if stray:
            raise _Stop("REJECTED", f"undeclared directories in bundle: {_listing(stray)}")

        signature_bytes = read(root_fd, SIGNATURE_FILE)
        _check_trust(signature_bytes, report.manifest_sha256, store, policy, report)

        # Integrity of everything present comes before any availability verdict.
        contents: dict[str, bytes] = {}
        missing: list[str] = []
        for artifact in manifest.artifacts:
            data = read(root_fd, artifact.path)
            if data is None:
                missing.append(artifact.path)
                continue
            if hashlib.sha256(data).hexdigest() != artifact.sha256:
                raise _Stop("REJECTED", f"sha256 mismatch for {artifact.path}")
            contents[artifact.path] = data
            report.artifacts_checked += 1

    replay = manifest.integrity.replay
    if replay == "not_implemented" or (report.level == "R2" and replay != "R2"):
        raise _Stop("INCOMPLETE", f"manifest does not declare {report.level} replay")
    r1 = {a.path for a in manifest.artifacts if "R1" in a.required_for}
    missing_declarations = sorted(set(R1_ARTIFACTS) - r1)
    if missing_declarations:
        raise _Stop("INCOMPLETE", f"R1 artifacts not declared: {missing_declarations}")
    needed = {a.path for a in manifest.artifacts if {"R1", report.level} & set(a.required_for)}
    if needed & set(missing):
        raise _Stop("INCOMPLETE", f"missing file(s): {_listing(needed & set(missing))}")
    for path in missing:
        report.reasons.append(f"{path} absent; only needed for R2")

    snapshot = _parse(SnapshotRef, contents["snapshot.json"], "snapshot.json")
    profile = _parse_profile(contents["profile.json"])
    evidence = _parse(BundleEvidence, contents["evidence.json"], "evidence.json")
    recorded = _parse(EvaluationResult, contents["evaluation.json"], "evaluation.json")
    report.financial_result = recorded.result
    _check_consistency(manifest, snapshot, evidence, recorded, profile)

    report.engine_status = engine_status(recorded.versions.engine_ref)
    report.engine_ref = recorded.versions.engine_ref  # what the report is about, replayed or not
    operation = ENGINE_OPERATION.get(recorded.versions.engine_ref)
    if operation is not None and operation != profile.operation_type:
        raise _Stop(
            "INCOMPLETE",
            f"engine {recorded.versions.engine_ref} evaluates {operation} profiles, not "
            f"{profile.operation_type}; it is not replayed and no other engine is substituted",
        )
    if report.engine_status == "retired" and report.engine_policy == "current":
        raise _Stop(
            "INCOMPLETE",
            f"engine {recorded.versions.engine_ref} is retired "
            f"({RETIRED_ENGINES[recorded.versions.engine_ref]}) and the policy requires a "
            "current engine; the historical conclusion is not reproduced",
        )
    if recorded.versions.engine_ref in BLOCKED_ENGINES:
        raise _Stop(
            "INCOMPLETE",
            f"engine {recorded.versions.engine_ref} is blocked by policy: "
            f"{BLOCKED_ENGINES[recorded.versions.engine_ref]}; it is not replayed and no "
            "other engine is substituted",
        )
    engine = TRUSTED_ENGINES.get(recorded.versions.engine_ref)
    if engine is None:
        raise _Stop(
            "INCOMPLETE",
            f"engine {recorded.versions.engine_ref} is not available "
            f"locally ({', '.join(sorted(TRUSTED_ENGINES))}); no other engine is substituted "
            "and nothing from the bundle is executed",
        )
    report.engine_implementation = ENGINE_IMPLEMENTATION[recorded.versions.engine_ref][0]
    replayed = engine(
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
            f"controls differing: {_listing(changed) or 'none (other fields differ)'}"
        )
        raise _Stop("MISMATCH", detail)
    report.reasons.append(
        f"R1 replay reproduced {recorded.result} with {recorded.versions.engine_ref}"
    )
    if report.engine_status == "retired":
        provenance = ENGINE_IMPLEMENTATION[recorded.versions.engine_ref][1]
        report.reasons.append(
            f"engine {recorded.versions.engine_ref} is retired: "
            f"{RETIRED_ENGINES[recorded.versions.engine_ref]}. The result coincided with a "
            f"compatibility implementation ({provenance}). "
            "Reproducing a historical conclusion does not validate it under the current "
            "semantics; a new evaluation is a different operation"
        )
    if report.level == "R2":
        _renormalize(evidence, snapshot, profile, contents, report)
        report.reasons.append(
            f"R2 renormalised all {len(report.renormalized)} observations from raw bytes"
        )
    if report.trust() == "unanchored":
        report.reasons.append("no expected manifest sha256 or trust store: integrity is unanchored")
    return report.finish("REPRODUCED")


def verify_bundle(
    root: Path,
    *,
    level: Level = "R1",
    expected_manifest_sha256: str | None = None,
    trust_store: TrustStore | None = None,
    policy: TrustPolicy | None = None,
    engine_policy: Literal["any", "current"] = "any",
) -> VerificationReport:
    """``engine_policy="current"`` refuses to reproduce a conclusion of a retired engine
    (INCOMPLETE); the default reproduces it with that label's (compatibility)
    implementation and says so."""
    if expected_manifest_sha256 is not None and not _SHA256.fullmatch(expected_manifest_sha256):
        raise ValueError("expected manifest sha256 must be 64 lowercase hex characters")
    report = _Report(level=level, expected_manifest_sha256=expected_manifest_sha256)
    report.engine_policy = engine_policy
    try:
        return _verify(root, report, trust_store, policy or TrustPolicy())
    except _Stop as stop:
        return report.finish(stop.status, stop.reason)
    except OSError as error:
        return report.finish("REJECTED", f"cannot read bundle: {type(error).__name__}")
    except RecursionError:
        return report.finish("REJECTED", "input nests too deeply to process")
