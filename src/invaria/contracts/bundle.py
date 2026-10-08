"""OperationEvidenceBundle contracts: manifest, frozen evidence and verification report.

Implemented: directory bundles (``directory_v1``) with SHA-256 per artifact, offline replay
at R1 and R2 (CSV renormalisation from raw bytes), detached Ed25519 signatures
(``signature.json``, outside the manifest) checked against an explicit trust store. NOT
implemented: compressed archives, R3 and signatures inside the manifest
(``integrity.signature`` stays ``not_implemented``; signing never changes the manifest).
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from invaria.contracts.base import (
    Contract,
    Identifier,
    SchemaVersion,
    Sha256Hex,
    UtcDatetime,
)
from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.evaluation import EvaluationVersions, FinancialResult
from invaria.contracts.identity import IdentityLink
from invaria.contracts.observation import Observation

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

RelativePath = Annotated[str, StringConstraints(min_length=1, max_length=512)]


MAX_PATH_DEPTH = 8


def _check_path(path: str) -> None:
    if path.startswith("/") or "\\" in path:
        raise ValueError(f"artifact path must be relative POSIX: {path!r}")
    if path.count("/") >= MAX_PATH_DEPTH:
        raise ValueError(f"artifact path too deep: {path!r}")
    for segment in path.split("/"):
        if segment in {"", ".", ".."} or not _SEGMENT.fullmatch(segment):
            raise ValueError(f"unsafe artifact path segment in {path!r}")


class BundleArtifact(Contract):
    path: RelativePath
    sha256: Sha256Hex
    media_type: Literal["application/json", "text/csv"]
    required_for: Annotated[list[Literal["R1", "R2"]], Field(min_length=1)]

    @model_validator(mode="after")
    def _safe(self) -> Self:
        _check_path(self.path)
        if len(set(self.required_for)) != len(self.required_for):
            raise ValueError("required_for must be unique")
        return self


RESERVED_PATHS = frozenset({"manifest.json", "signature.json"})


class BundleQuery(Contract):
    valid_at: UtcDatetime
    known_at: UtcDatetime
    mode: Literal["as_known", "as_known_now"]


class BundleIntegrity(Contract):
    signature: Literal["not_implemented"]
    replay: Literal["not_implemented", "R1", "R2"]
    export_format: Literal["not_implemented", "directory_v1"]


class EvidenceBundleManifest(Contract):
    schema_version: SchemaVersion
    bundle_id: Identifier
    tenant_id: Identifier
    operation_ref: Identifier
    snapshot_id: Identifier
    query: BundleQuery
    versions: EvaluationVersions | None
    expected_result: FinancialResult | None
    artifacts: Annotated[list[BundleArtifact], Field(min_length=1, max_length=10_000)]
    disclosure: Literal["synthetic_public", "private"]
    integrity: BundleIntegrity

    @model_validator(mode="after")
    def _artifacts(self) -> Self:
        paths = [artifact.path for artifact in self.artifacts]
        if paths != sorted(set(paths)):
            raise ValueError("artifacts must be sorted by path without duplicates")
        if len({p.casefold() for p in paths}) != len(paths):
            raise ValueError("artifact paths collide when case is ignored")
        if any(p in RESERVED_PATHS for p in paths):
            raise ValueError(f"artifacts cannot use reserved paths {sorted(RESERVED_PATHS)}")
        if self.integrity.replay in ("R1", "R2"):
            if self.integrity.export_format != "directory_v1":
                raise ValueError("replay requires export_format directory_v1")
            if self.versions is None or self.expected_result is None:
                raise ValueError("replay requires versions and expected_result")
        return self


class BundleEvidence(Contract):
    """Exactly the members of one snapshot, frozen for replay."""

    schema_version: SchemaVersion
    snapshot_id: Identifier
    observations: list[Observation]
    coverage: list[CoverageCertificate]
    identity_links: list[IdentityLink]


Ed25519PublicKeyHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Ed25519SignatureHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{128}$")]


class BundleSignature(Contract):
    """Detached signature over the exact manifest bytes (through ``manifest_sha256``).

    Signed preimage: ``SIGNATURE_DOMAIN`` + canonical JSON of this record without
    ``signature``. ``signed_at`` is claimed by the signer and is covered by the signature.
    """

    algorithm: Literal["Ed25519"]
    key_id: Identifier
    signed_at: UtcDatetime
    manifest_sha256: Sha256Hex
    signature: Ed25519SignatureHex


class BundleSignatures(Contract):
    schema_version: SchemaVersion
    signatures: Annotated[list[BundleSignature], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [s.key_id for s in self.signatures]
        if len(set(ids)) != len(ids):
            raise ValueError("one signature per key_id")
        return self


class KeyRevocation(Contract):
    revoked_at: UtcDatetime
    reason: Literal["superseded", "key_compromise"]


class TrustedKey(Contract):
    key_id: Identifier
    algorithm: Literal["Ed25519"]
    public_key: Ed25519PublicKeyHex
    purposes: Annotated[list[Literal["bundle_signing"]], Field(min_length=1)]
    valid_from: UtcDatetime
    valid_to: UtcDatetime | None
    revocation: KeyRevocation | None

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must follow valid_from")
        return self


class TrustStore(Contract):
    """Keys a verifier trusts, supplied by the verifier, never taken from the bundle."""

    schema_version: SchemaVersion
    store_id: Identifier
    keys: Annotated[list[TrustedKey], Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [k.key_id for k in self.keys]
        publics = [k.public_key for k in self.keys]
        if len(set(ids)) != len(ids) or len(set(publics)) != len(publics):
            raise ValueError("key_id and public_key must be unique")
        return self


VerifierStatus = Literal["REPRODUCED", "MISMATCH", "INCOMPLETE", "UNTRUSTED", "REJECTED"]
TrustState = Literal[
    "unanchored",
    "anchored_by_expected_manifest_sha256",
    "trusted_signature",
    "anchored_and_trusted_signature",
]


class VerificationReport(Contract):
    """Verifier outcome. ``status`` is about the bundle; ``financial_result`` is the
    recorded conclusion and travels separately: reproducing a BREAK is REPRODUCED."""

    schema_version: SchemaVersion
    bundle_id: Identifier | None
    level: Literal["R1", "R2"]
    status: VerifierStatus
    financial_result: FinancialResult | None
    reasons: list[Annotated[str, StringConstraints(min_length=1, max_length=2000)]]
    manifest_sha256: Sha256Hex | None
    trust: TrustState
    signer_key_ids: list[Identifier]
    local_engine_ref: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    # What the recorded engine is under the local policy: "retired" means the
    # conclusion was reproduced under a retired engine label, which does not validate
    # it under the current ones. Absent when verification stopped before the engine.
    engine_status: Literal["current", "retired", "blocked", "unknown"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    # Which code replayed it: "compatibility" is an implementation of a retired
    # engine label, not the historical code itself. Kept apart from ``engine_status``
    # (admission) and from ``status`` (whether the result coincided).
    engine_implementation: Literal["current", "compatibility"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    engine_source_sha256: Sha256Hex
    artifacts_checked: Annotated[int, Field(ge=0)]
    renormalized_observation_ids: list[Identifier]
