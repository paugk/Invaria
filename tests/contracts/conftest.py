from __future__ import annotations

from pathlib import Path

import pytest

from invaria.contracts import (
    CoverageSet,
    IdentityLinkSet,
    ObservationJournal,
    OperationProfile,
    ScenarioCatalog,
    parse_contract,
)

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "tests/fixtures/corpus/subscription-synthetic"


@pytest.fixture(scope="session")
def profile() -> OperationProfile:
    return parse_contract(OperationProfile, (CORPUS / "profile.json").read_text("utf-8"))


@pytest.fixture(scope="session")
def catalog() -> ScenarioCatalog:
    return parse_contract(ScenarioCatalog, (CORPUS / "scenarios.json").read_text("utf-8"))


@pytest.fixture(scope="session")
def coverage() -> CoverageSet:
    return parse_contract(CoverageSet, (CORPUS / "coverage.json").read_text("utf-8"))


@pytest.fixture(scope="session")
def links() -> IdentityLinkSet:
    return parse_contract(IdentityLinkSet, (CORPUS / "identity_links.json").read_text("utf-8"))


@pytest.fixture(scope="session")
def journals() -> dict[str, ObservationJournal]:
    result: dict[str, ObservationJournal] = {}
    for path in sorted((CORPUS / "observations").glob("*.json")):
        journal = parse_contract(ObservationJournal, path.read_text("utf-8"))
        assert journal.timeline_id == path.stem
        result[journal.timeline_id] = journal
    return result
