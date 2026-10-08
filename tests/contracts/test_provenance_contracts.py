"""Mapping provenance contracts.

A profile names, per source, the mappings it admits (``mapping_ref`` and, from
fund-subscription-testnet@1.4.0, ``additional_mapping_refs``); an evaluation names the
mappings that produced its evidence (``evidence_mapping_refs``, from subscription 0.8.0 and
redemption 0.9.0). Both are omitted when absent, so earlier artifacts stay byte-identical.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from invaria.contracts.evaluation import EvaluationVersions
from invaria.contracts.profile import SourceAuthority, admitted_mapping_refs, parse_profile

CORPORA = Path(__file__).resolve().parents[1] / "fixtures/corpus"
SOURCE: dict[str, Any] = {
    "source_id": "stellar-testnet",
    "kind": "stellar_ledger",
    "authoritative_for": ["token_movement"],
    "mapping_ref": "stellar-classic-payment@1.1.0",
    "parser_ref": "stellar-horizon-payments-parser@1.0.0",
    "description": "chain source",
}
VERSIONS: dict[str, Any] = {
    "casm": "1.0",
    "profile_ref": "p@1.0.0",
    "rules_ref": "r@1.0.0",
    "mapping_refs": ["a@1.0.0", "b@1.0.0"],
    "engine_ref": "invaria-engine@0.8.0",
}


def test_a_source_admits_its_mapping_and_the_additional_ones() -> None:
    plain = SourceAuthority.model_validate(SOURCE)
    assert plain.admitted_mapping_refs == {"stellar-classic-payment@1.1.0"}
    assert "additional_mapping_refs" not in json.loads(plain.model_dump_json())
    extra = ["stellar-sac-dex-fill@1.0.0", "stellar-sac-movement@1.1.0"]
    wider = SourceAuthority.model_validate({**SOURCE, "additional_mapping_refs": extra})
    assert wider.admitted_mapping_refs == {"stellar-classic-payment@1.1.0", *extra}
    assert admitted_mapping_refs([wider]) == sorted(wider.admitted_mapping_refs)


@pytest.mark.parametrize(
    "extra",
    [
        [],  # an empty list says nothing: omit the field
        ["stellar-sac-movement@1.1.0", "stellar-sac-dex-fill@1.0.0"],  # unsorted
        ["stellar-sac-dex-fill@1.0.0", "stellar-sac-dex-fill@1.0.0"],  # repeated
        ["stellar-classic-payment@1.1.0"],  # repeats mapping_ref
        ["not a version"],
    ],
)
def test_malformed_additional_mappings_are_rejected(extra: list[str]) -> None:
    with pytest.raises(ValidationError):
        SourceAuthority.model_validate({**SOURCE, "additional_mapping_refs": extra})


def test_historical_profiles_keep_their_bytes_and_admit_only_their_mapping() -> None:
    for name in (
        "subscription-testnet",
        "subscription-testnet-1.1.0",
        "subscription-testnet-1.2.0",
        "subscription-testnet-1.3.0",
        "subscription-synthetic",
        "redemption-synthetic-1.5.0",
    ):
        raw = (CORPORA / name / "profile.json").read_text("utf-8")
        profile = parse_profile(raw)
        assert all(s.additional_mapping_refs is None for s in profile.sources), name
        assert admitted_mapping_refs(profile.sources) == sorted(
            {s.mapping_ref for s in profile.sources}
        )
        assert json.loads(profile.model_dump_json()) == json.loads(raw)  # nothing added


def test_the_evaluation_states_the_mappings_of_its_evidence() -> None:
    absent = EvaluationVersions.model_validate(VERSIONS)
    assert "evidence_mapping_refs" not in json.loads(absent.model_dump_json())
    stated = EvaluationVersions.model_validate({**VERSIONS, "evidence_mapping_refs": ["a@1.0.0"]})
    assert stated.evidence_mapping_refs == ["a@1.0.0"]
    # Naming a mapping the profile does not admit is allowed: that evaluation is the
    # EVALUATION_ERROR refusing it, and it says what produced the evidence.
    refused = EvaluationVersions.model_validate(
        {**VERSIONS, "evidence_mapping_refs": ["a@1.0.0", "c@2.0.0"]}
    )
    assert refused.evidence_mapping_refs == ["a@1.0.0", "c@2.0.0"]
    assert EvaluationVersions.model_validate({**VERSIONS, "evidence_mapping_refs": []})


@pytest.mark.parametrize("evidence", [["b@1.0.0", "a@1.0.0"], ["a@1.0.0", "a@1.0.0"]])
def test_evidence_mappings_are_sorted_and_unique(evidence: list[str]) -> None:
    with pytest.raises(ValidationError):
        EvaluationVersions.model_validate({**VERSIONS, "evidence_mapping_refs": evidence})
