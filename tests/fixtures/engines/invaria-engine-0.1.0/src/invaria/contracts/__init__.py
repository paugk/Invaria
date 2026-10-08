"""Invaria contracts. Pure data definitions; no evaluator lives here."""

from invaria.contracts.base import (
    SCHEMA_VERSION,
    StrictJsonError,
    check_strict_json,
    parse_contract,
)
from invaria.contracts.bundle import EvidenceBundleManifest
from invaria.contracts.corpus import CoverageSet, IdentityLinkSet, ObservationJournal
from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.evaluation import (
    ControlOutcome,
    ControlResult,
    EvaluationResult,
    ExpectedOutcome,
    Scenario,
    ScenarioCatalog,
    SnapshotRef,
    aggregate,
)
from invaria.contracts.identity import IdentityLink, InstrumentRef, StellarClassicRepresentation
from invaria.contracts.observation import Observation
from invaria.contracts.profile import OperationProfile
from invaria.contracts.quantity import Quantity

__all__ = [
    "SCHEMA_VERSION",
    "ControlOutcome",
    "ControlResult",
    "CoverageCertificate",
    "CoverageSet",
    "EvaluationResult",
    "EvidenceBundleManifest",
    "ExpectedOutcome",
    "IdentityLink",
    "IdentityLinkSet",
    "InstrumentRef",
    "Observation",
    "ObservationJournal",
    "OperationProfile",
    "Quantity",
    "Scenario",
    "ScenarioCatalog",
    "SnapshotRef",
    "StellarClassicRepresentation",
    "StrictJsonError",
    "aggregate",
    "check_strict_json",
    "parse_contract",
]
