"""Write a directory evidence bundle (``directory_v1``) for one evaluation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from invaria.contracts.bundle import (
    BundleArtifact,
    BundleEvidence,
    BundleIntegrity,
    BundleQuery,
    EvidenceBundleManifest,
)
from invaria.contracts.mapping import CsvMapping
from invaria.engine.evaluate import Evaluation, EvaluationInputs
from invaria.ingest.csv_import import PARSER_REF as CSV_PARSER_REF

MANIFEST = "manifest.json"
R1_ARTIFACTS = ("evaluation.json", "evidence.json", "profile.json", "snapshot.json")


def mapping_path(mapping_ref: str) -> str:
    return f"mappings/{mapping_ref.replace('@', '_')}.json"


def raw_path(raw_locator: str) -> str:
    return raw_locator.split("#", 1)[0]


def canonical_json(model: BaseModel) -> bytes:
    """Deterministic serialisation: sorted keys, no insignificant whitespace, UTF-8.

    Contracts only contain strings, integers, booleans and null, so this form is stable;
    it is not a certified RFC 8785 implementation.
    """
    text = json.dumps(
        model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return text.encode("utf-8")


def snapshot_evidence(inputs: EvaluationInputs) -> BundleEvidence:
    snapshot = inputs.snapshot
    return BundleEvidence(
        schema_version="1.0",
        snapshot_id=snapshot.snapshot_id,
        observations=[inputs.observations[i] for i in snapshot.observation_ids],
        coverage=[inputs.coverage[i] for i in snapshot.coverage_ids],
        identity_links=[inputs.identity_links[i] for i in snapshot.identity_link_ids],
    )


def build_bundle(
    out_dir: Path,
    inputs: EvaluationInputs,
    evaluation: Evaluation,
    *,
    mode: Literal["as_known", "as_known_now"],
    raw_sources: Mapping[str, bytes] | None = None,
    mappings: Mapping[str, CsvMapping] | None = None,
) -> EvidenceBundleManifest:
    """R1 bundle; with ``raw_sources`` (keyed by raw path) and ``mappings`` also R2.

    For R2 only the raw files and mappings referenced by CSV-derived snapshot members are
    included. Members without a local normalizer get nothing extra: the verifier reports
    them and R2 is then INCOMPLETE.
    """
    snapshot = inputs.snapshot
    if evaluation.result.snapshot_id != snapshot.snapshot_id:
        raise ValueError("evaluation does not belong to this snapshot")
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise FileExistsError(f"{out_dir} exists and is not an empty directory")
    evidence = snapshot_evidence(inputs)
    payloads: dict[str, bytes] = {
        "evaluation.json": canonical_json(evaluation.result),
        "evidence.json": canonical_json(evidence),
        "profile.json": canonical_json(inputs.profile),
        "snapshot.json": canonical_json(snapshot),
    }
    r2: dict[str, tuple[bytes, Literal["application/json", "text/csv"]]] = {}
    if raw_sources is not None:
        available = mappings or {}
        for o in evidence.observations:
            if o.provenance.parser_ref != CSV_PARSER_REF:
                continue
            path = raw_path(o.provenance.raw_locator)
            data = raw_sources.get(path)
            if data is None or hashlib.sha256(data).hexdigest() != o.provenance.raw_sha256:
                raise ValueError(f"raw bytes for {o.observation_id} ({path}) missing or altered")
            mapping = available.get(o.provenance.mapping_ref)
            if mapping is None:
                raise ValueError(f"mapping {o.provenance.mapping_ref} not provided")
            r2[path] = (data, "text/csv")
            r2[mapping_path(mapping.mapping_ref)] = (canonical_json(mapping), "application/json")
    synthetic = all(o.synthetic for o in evidence.observations) and inputs.profile.synthetic
    manifest = EvidenceBundleManifest(
        schema_version="1.0",
        bundle_id=f"oeb-{evaluation.result.evaluation_id}",
        tenant_id=snapshot.tenant_id,
        operation_ref=snapshot.operation_ref,
        snapshot_id=snapshot.snapshot_id,
        query=BundleQuery(valid_at=snapshot.valid_at, known_at=snapshot.known_at, mode=mode),
        versions=evaluation.result.versions,
        expected_result=evaluation.result.result,
        artifacts=sorted(
            [
                *(
                    BundleArtifact(
                        path=path,
                        sha256=hashlib.sha256(data).hexdigest(),
                        media_type="application/json",
                        required_for=["R1"],
                    )
                    for path, data in payloads.items()
                ),
                *(
                    BundleArtifact(
                        path=path,
                        sha256=hashlib.sha256(data).hexdigest(),
                        media_type=media_type,
                        required_for=["R2"],
                    )
                    for path, (data, media_type) in r2.items()
                ),
            ],
            key=lambda artifact: artifact.path,
        ),
        disclosure="synthetic_public" if synthetic else "private",
        integrity=BundleIntegrity(
            signature="not_implemented",
            replay="R2" if raw_sources is not None else "R1",
            export_format="directory_v1",
        ),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    for path, data in [*payloads.items(), *((p, d) for p, (d, _) in r2.items())]:
        (out_dir / path).parent.mkdir(parents=True, exist_ok=True)
        (out_dir / path).write_bytes(data)
    (out_dir / MANIFEST).write_bytes(canonical_json(manifest))
    return manifest
