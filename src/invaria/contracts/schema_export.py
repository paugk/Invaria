"""Export JSON Schema (2020-12) for each top-level contract.

Usage: ``uv run python -m invaria.contracts.schema_export [output_dir]`` (default ``schemas``).
The committed files are derived artifacts; a test fails if they drift from the models.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from invaria.contracts import (
    CoverageSet,
    EvaluationResult,
    EvidenceBundleManifest,
    IdentityLinkSet,
    Observation,
    ObservationJournal,
    OperationProfile,
    Quantity,
    ScenarioCatalog,
    SnapshotRef,
)
from invaria.contracts.bundle import (
    BundleEvidence,
    BundleSignatures,
    TrustStore,
    VerificationReport,
)
from invaria.contracts.chain import ChainTarget, ExecutionLinkSet, IngestionCheckpoint
from invaria.contracts.mapping import CsvMapping
from invaria.contracts.profile import RedemptionProfile
from invaria.query.models import (
    AccessProfile,
    ComparisonView,
    ConclusionChangeView,
    ConclusionView,
    ControlDiagnosisView,
    CoverageReport,
    DiscrepancyView,
    EvidenceView,
    MissingEvidenceView,
    OperationObligationsView,
    OperationsView,
    TimelineView,
    TraceView,
)

CONTRACTS: dict[str, type[BaseModel]] = {
    "quantity": Quantity,
    "observation": Observation,
    "observation-journal": ObservationJournal,
    "coverage-set": CoverageSet,
    "identity-link-set": IdentityLinkSet,
    "operation-profile": OperationProfile,
    "redemption-profile": RedemptionProfile,
    "snapshot": SnapshotRef,
    "evaluation-result": EvaluationResult,
    "scenario-catalog": ScenarioCatalog,
    "evidence-bundle-manifest": EvidenceBundleManifest,
    "csv-mapping": CsvMapping,
    "bundle-evidence": BundleEvidence,
    "verification-report": VerificationReport,
    "bundle-signatures": BundleSignatures,
    "trust-store": TrustStore,
    "access-profile": AccessProfile,
    "query-trace": TraceView,
    "query-conclusion": ConclusionView,
    "query-missing-evidence": MissingEvidenceView,
    "query-control-diagnosis": ControlDiagnosisView,
    "query-operation-obligations": OperationObligationsView,
    "query-discrepancy": DiscrepancyView,
    "query-conclusion-change": ConclusionChangeView,
    "query-evidence": EvidenceView,
    "query-coverage": CoverageReport,
    "query-operations": OperationsView,
    "query-timeline": TimelineView,
    "query-comparison": ComparisonView,
    "chain-target": ChainTarget,
    "execution-link-set": ExecutionLinkSet,
    "ingestion-checkpoint": IngestionCheckpoint,
}


def render(name: str, model: type[BaseModel]) -> str:
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:invaria:schema:{name}:1.0",
        **model.model_json_schema(mode="validation"),
    }
    return json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else Path("schemas")
    out.mkdir(parents=True, exist_ok=True)
    for name, model in CONTRACTS.items():
        (out / f"{name}.schema.json").write_text(render(name, model), encoding="utf-8")
    print(f"wrote {len(CONTRACTS)} schemas to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
