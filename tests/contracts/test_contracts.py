"""Valid and invalid examples for each contract, derived from the synthetic corpus."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from invaria.contracts import (
    ControlOutcome,
    CoverageSet,
    EvaluationResult,
    EvidenceBundleManifest,
    ObservationJournal,
    OperationProfile,
    ScenarioCatalog,
    SnapshotRef,
    aggregate,
    parse_contract,
)
from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.observation import Observation

CORPUS = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"

Mutation = Callable[[dict[str, Any]], None]


def mutated[M: BaseModel](model: type[M], source: BaseModel, mutate: Mutation) -> M:
    data = copy.deepcopy(source.model_dump(mode="json"))
    mutate(data)
    return parse_contract(model, json.dumps(data))


def _set(path: str, value: Any) -> Mutation:
    def apply(data: dict[str, Any]) -> None:
        *parents, leaf = path.split(".")
        node: Any = data
        for key in parents:
            node = node[int(key)] if isinstance(node, list) else node[key]
        if isinstance(node, list):
            node[int(leaf)] = value
        else:
            node[leaf] = value

    return apply


def _drop(path: str) -> Mutation:
    def apply(data: dict[str, Any]) -> None:
        *parents, leaf = path.split(".")
        node: Any = data
        for key in parents:
            node = node[key]
        del node[leaf]

    return apply


# ---------------------------------------------------------------- observation


@pytest.fixture(scope="module")
def bank_obs(journals: dict[str, ObservationJournal]) -> Observation:
    return next(o for o in journals["main"].observations if o.observation_id == "obs-B1")


@pytest.fixture(scope="module")
def token_obs(journals: dict[str, ObservationJournal]) -> Observation:
    return next(o for o in journals["main"].observations if o.observation_id == "obs-T1")


def test_observation_carries_provenance_and_versions(bank_obs: Observation) -> None:
    assert len(bank_obs.provenance.raw_sha256) == 64
    assert bank_obs.provenance.mapping_ref == "bank-csv-synthetic@1.0.0"
    assert bank_obs.provenance.parser_ref == "csv-synthetic-parser@1.0.0"
    assert bank_obs.schema_version == "1.0"


@pytest.mark.parametrize(
    "mutation",
    [
        _drop("provenance"),
        _drop("provenance.mapping_ref"),
        _set("provenance.mapping_ref", "bank-csv-synthetic"),
        _set("provenance.parser_ref", "latest"),
        _set("provenance.raw_sha256", "ABC"),
        _set("payload.amount.atoms", 10000000),
        _drop("payload.amount.scale"),
        _set("payload.amount.unit", "usd"),
        _set("payload.payload_type", "units_registered"),
        _set("schema_version", "2.0"),
        _set("valid_time", "2026-10-01T12:00:00"),
        _set("valid_time", "2026-10-01T14:00:00+02:00"),
        _set("kind", "retraction"),
        _set("supersedes", "obs-B1"),
        _set("source.revision", 0),
        _set("unexpected", "field"),
        _drop("tenant_id"),
    ],
)
def test_invalid_observations_rejected(bank_obs: Observation, mutation: Mutation) -> None:
    with pytest.raises(ValidationError):
        mutated(Observation, bank_obs, mutation)


def test_token_movement_requires_representation_and_valid_address(
    token_obs: Observation,
) -> None:
    with pytest.raises(ValidationError):
        mutated(Observation, token_obs, _set("representation_id", None))
    with pytest.raises(ValidationError):
        mutated(Observation, token_obs, _set("payload.to_address", "GNOTAVALIDSTELLARACCOUNT"))
    with pytest.raises(ValidationError):
        mutated(Observation, token_obs, _set("payload.chain.network", "ethereum:1"))


def test_retraction_shape(journals: dict[str, ObservationJournal]) -> None:
    retraction = next(o for o in journals["retraction"].observations if o.kind == "retraction")
    assert retraction.payload is None and retraction.supersedes == "obs-B1"
    with pytest.raises(ValidationError):
        mutated(Observation, retraction, _set("supersedes", None))


def test_journal_rejects_bad_revision_chains(journals: dict[str, ObservationJournal]) -> None:
    main = journals["main"]
    b2 = 4  # index of obs-B2 in the main timeline
    assert main.observations[b2].observation_id == "obs-B2"
    cases: list[Mutation] = [
        _set(f"observations.{b2}.supersedes", "obs-missing"),
        _set(f"observations.{b2}.supersedes", "obs-R1"),
        _set(f"observations.{b2}.recorded_at", "2026-10-01T17:00:00Z"),
        _set("observations.1.observation_id", "obs-O1"),
    ]
    for mutation in cases:
        with pytest.raises(ValidationError):
            mutated(ObservationJournal, main, mutation)


# ------------------------------------------------------------------- coverage


def test_coverage_rules(coverage: CoverageSet) -> None:
    cert = coverage.certificates[0]
    assert cert.is_gap_free
    bad: list[Mutation] = [
        _set("interval.end", cert.interval.start.isoformat()),
        _set("gaps", [{"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T01:00:00Z"}]),
        _set("records_quarantined", 5),
        _set("level", "trusted"),
        _set("fact_types", []),
    ]
    for mutation in bad:
        with pytest.raises(ValidationError):
            mutated(CoverageCertificate, cert, mutation)
    with_gap = mutated(
        CoverageCertificate,
        cert,
        _set("gaps", [{"start": "2026-10-01T01:00:00Z", "end": "2026-10-01T02:00:00Z"}]),
    )
    assert not with_gap.is_gap_free


# -------------------------------------------------------------------- profile


def test_profile_declares_authority_per_fact(profile: OperationProfile) -> None:
    assert profile.authority_for("cash_settled") == "bank-synthetic"
    assert profile.authority_for("units_registered") == "ta-synthetic"
    assert profile.authority_for("token_movement") == "stellar-testnet-frozen"
    assert profile.pricing.tolerance == "none" and profile.pricing.fees == "none"
    assert all(c.mandatory for c in profile.controls)


@pytest.mark.parametrize(
    "mutation",
    [
        _set("representations.0.network", "stellar:pubnet"),
        _set("representations.0.network_passphrase", "Some Other Network"),
        _set("representations.0.amount_scale", 2),
        _set("representations.0.issuer", "GABC"),
        _set("pricing.tolerance", "0.01"),
        _set("pricing.price_per_unit.unit", "EUR"),
        _set("sources.1.authoritative_for", ["order_accepted"]),
        _set("coverage_requirements", []),
        _set("result_precedence", "MATCH>UNKNOWN>BREAK"),
        _set("evidence_policy.absent", "MATCH"),
        _set("unsupported", ["fees", "fees"]),
        _set("unsupported", ["teleportation"]),
    ],
)
def test_invalid_profiles_rejected(profile: OperationProfile, mutation: Mutation) -> None:
    with pytest.raises(ValidationError):
        mutated(OperationProfile, profile, mutation)


def test_profile_without_cash_authority_rejected(profile: OperationProfile) -> None:
    def drop_bank(data: dict[str, Any]) -> None:
        data["sources"] = [s for s in data["sources"] if s["source_id"] != "bank-synthetic"]
        data["coverage_requirements"] = [
            r for r in data["coverage_requirements"] if r["source_id"] != "bank-synthetic"
        ]

    with pytest.raises(ValidationError, match="needs a source for cash_settled"):
        mutated(OperationProfile, profile, drop_bank)


# ------------------------------------------------------- snapshot / evaluation


def test_snapshot_rules(catalog: ScenarioCatalog) -> None:
    snap = catalog.scenarios[0].snapshot
    for mutation in [
        _set("observation_ids", ["obs-T1", "obs-O1"]),
        _set("observation_ids", ["obs-O1", "obs-O1"]),
        _set("evaluation_clock", "2026-10-01T09:00:00Z"),
        _set("mapping_refs", []),
        _set("rules_ref", "rules"),
    ]:
        with pytest.raises(ValidationError):
            mutated(SnapshotRef, snap, mutation)


def _control(status: str, reason: str, *, mandatory: bool = True) -> ControlOutcome:
    return ControlOutcome.model_validate(
        {
            "control_id": f"c-{status}-{reason}",
            "mandatory": mandatory,
            "status": status,
            "reason_code": reason,
            "delta": None,
        }
    )


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([("PASS", "EXACT_MATCH"), ("PASS", "EXACT_MATCH")], "MATCH"),
        ([("PASS", "EXACT_MATCH"), ("UNKNOWN", "MISSING_EVIDENCE")], "UNKNOWN"),
        ([("FAIL", "UNITS_MISMATCH"), ("UNKNOWN", "MISSING_EVIDENCE")], "BREAK"),
        ([("UNKNOWN", "EVALUATION_ERROR")], "UNKNOWN"),
        ([("NOT_APPLICABLE", "NOT_APPLICABLE_BY_PROFILE")], "UNKNOWN"),
        ([], "UNKNOWN"),
    ],
)
def test_precedence(statuses: list[tuple[str, str]], expected: str) -> None:
    assert aggregate([_control(s, r) for s, r in statuses]) == expected


def test_optional_controls_do_not_drive_result() -> None:
    controls = [
        _control("PASS", "EXACT_MATCH"),
        _control("FAIL", "UNITS_MISMATCH", mandatory=False),
    ]
    assert aggregate(controls) == "MATCH"


def test_reason_must_match_status() -> None:
    with pytest.raises(ValidationError):
        _control("PASS", "EVALUATION_ERROR")
    with pytest.raises(ValidationError):
        _control("UNKNOWN", "EXACT_MATCH")


def _evaluation(result: str, controls: list[dict[str, Any]]) -> EvaluationResult:
    return EvaluationResult.model_validate_json(
        json.dumps(
            {
                "schema_version": "1.0",
                "evaluation_id": "eval-example",
                "snapshot_id": "snap-K3",
                "operation_ref": "SUB-0001",
                "result": result,
                "controls": controls,
                "versions": {
                    "casm": "1.0",
                    "profile_ref": "fund-subscription-synthetic@1.0.0",
                    "rules_ref": "subscription-synthetic-rules@1.0.0",
                    "mapping_refs": ["bank-csv-synthetic@1.0.0"],
                    "engine_ref": "invaria-engine-example@0.0.0",
                },
                "evaluation_clock": "2026-10-02T09:30:00Z",
                "assumptions": ["synthetic example"],
            }
        )
    )


CASH_FAIL = {
    "control_id": "subscription.cash_vs_order",
    "mandatory": True,
    "status": "FAIL",
    "reason_code": "CASH_AMOUNT_MISMATCH",
    "reason": "settled cash below order amount",
    "evidence_refs": ["obs-B2", "obs-O1"],
    "delta": {"atoms": "-50000", "scale": 2, "unit": "USD"},
}
TA_UNKNOWN = {
    "control_id": "subscription.ta_units_vs_order",
    "mandatory": True,
    "status": "UNKNOWN",
    "reason_code": "MISSING_EVIDENCE",
    "reason": "no TA observation",
    "evidence_refs": [],
    "delta": None,
}


def test_break_coexists_with_unknown_controls() -> None:
    evaluation = _evaluation("BREAK", [CASH_FAIL, TA_UNKNOWN])
    assert [c.status for c in evaluation.controls] == ["FAIL", "UNKNOWN"]


@pytest.mark.parametrize("claimed", ["MATCH", "UNKNOWN"])
def test_result_must_follow_precedence(claimed: str) -> None:
    with pytest.raises(ValidationError, match="contradicts control precedence"):
        _evaluation(claimed, [CASH_FAIL, TA_UNKNOWN])


def test_evaluation_requires_engine_and_evidence() -> None:
    with pytest.raises(ValidationError):
        _evaluation("BREAK", [{**CASH_FAIL, "evidence_refs": []}])
    with pytest.raises(ValidationError):
        _evaluation("UNKNOWN", [{**TA_UNKNOWN, "delta": CASH_FAIL["delta"]}])
    with pytest.raises(ValidationError):
        _evaluation("BREAK", [CASH_FAIL, {**CASH_FAIL}])


# --------------------------------------------------------------------- bundle


@pytest.fixture(scope="module")
def manifest() -> EvidenceBundleManifest:
    text = (CORPUS / "bundle_manifest_K2.draft.json").read_text("utf-8")
    return parse_contract(EvidenceBundleManifest, text)


def test_manifest_declares_unimplemented_capabilities(manifest: EvidenceBundleManifest) -> None:
    assert manifest.integrity.signature == "not_implemented"
    assert manifest.integrity.replay == "not_implemented"
    with pytest.raises(ValidationError):
        mutated(EvidenceBundleManifest, manifest, _set("integrity.signature", "ed25519"))


@pytest.mark.parametrize(
    "path",
    [
        "../profile.json",
        "/etc/passwd",
        "raw/../../x",
        "raw//x.csv",
        "./profile.json",
        "raw\\x.csv",
        ".hidden",
        "raw/",
    ],
)
def test_manifest_rejects_unsafe_paths(manifest: EvidenceBundleManifest, path: str) -> None:
    with pytest.raises(ValidationError):
        mutated(EvidenceBundleManifest, manifest, _set("artifacts.0.path", path))


def test_manifest_rejects_duplicates_and_unsorted(manifest: EvidenceBundleManifest) -> None:
    def duplicate(data: dict[str, Any]) -> None:
        data["artifacts"].append(data["artifacts"][0])

    def reverse(data: dict[str, Any]) -> None:
        data["artifacts"].reverse()

    for mutation in (duplicate, reverse):
        with pytest.raises(ValidationError):
            mutated(EvidenceBundleManifest, manifest, mutation)
