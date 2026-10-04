"""Initial OperationEvidenceBundle manifest contract.

Draft only: describes artifacts and their hashes. Signing, safe archive export and
offline replay are NOT implemented; the manifest states so explicitly.
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
from invaria.contracts.evaluation import EvaluationVersions, FinancialResult

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

RelativePath = Annotated[str, StringConstraints(min_length=1, max_length=512)]


def _check_path(path: str) -> None:
    if path.startswith("/") or "\\" in path:
        raise ValueError(f"artifact path must be relative POSIX: {path!r}")
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


class BundleQuery(Contract):
    valid_at: UtcDatetime
    known_at: UtcDatetime
    mode: Literal["as_known", "as_known_now"]


class BundleIntegrity(Contract):
    signature: Literal["not_implemented"]
    replay: Literal["not_implemented"]
    export_format: Literal["not_implemented"]


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
        return self
