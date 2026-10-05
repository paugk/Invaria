"""Dependency predicates: what new evidence can change an operation's evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.observation import Observation
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.dependencies import Dependencies, dependencies

CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


@pytest.fixture(scope="module")
def deps(corpus: Corpus) -> Dependencies:
    return dependencies(corpus.profile, "SUB-0001")


def _variant(o: Observation, **changes: Any) -> Observation:
    document = o.model_dump(mode="json")
    for path, value in changes.items():
        target = document
        *parents, leaf = path.split("__")
        for key in parents:
            target = target[key]
        target[leaf] = value
    return Observation.model_validate_json(json.dumps(document))


def _matches(deps: Dependencies, o: Observation, superseded_ref: str | None = None) -> bool:
    refs = [o.operation_ref] + ([superseded_ref] if o.supersedes else [])
    return deps.matches_observation(o, refs)


def test_every_fact_of_the_operation_is_a_dependency(corpus: Corpus, deps: Dependencies) -> None:
    for o in corpus.journals["main"].values():
        assert _matches(deps, o), o.observation_id
    retraction = corpus.journals["retraction"]["obs-B1-retraction"]
    assert _matches(deps, retraction, "SUB-0001")


def test_absence_is_a_predicate_not_a_member_list(corpus: Corpus, deps: Dependencies) -> None:
    """A cash observation that did not exist at K1 still matches: absence is invalidatable."""
    b1 = corpus.journals["main"]["obs-B1"]
    later = _variant(b1, observation_id="obs-B-new", recorded_at="2026-10-05T00:00:00Z")
    assert _matches(deps, later)


def test_unrelated_evidence_has_no_effect(corpus: Corpus, deps: Dependencies) -> None:
    b1 = corpus.journals["main"]["obs-B1"]
    other_operation = _variant(b1, operation_ref="SUB-0002", source__record_key="SUB-0002")
    other_instrument = _variant(b1, instrument_id="syn:fund:OTHER:class-a")
    not_authoritative = _variant(b1, source__source_id="oms-synthetic")
    for o in (other_operation, other_instrument, not_authoritative):
        assert not _matches(deps, o)


def test_revision_moving_a_record_away_still_invalidates(
    corpus: Corpus, deps: Dependencies
) -> None:
    b1 = corpus.journals["main"]["obs-B1"]
    moved = _variant(
        b1,
        observation_id="obs-B1-moved",
        operation_ref="SUB-0002",
        source__revision=2,
        supersedes="obs-B1",
    )
    assert _matches(deps, moved, superseded_ref="SUB-0001")
    assert not _matches(deps, moved, superseded_ref="SUB-0003")


def test_token_movements_unlinked_count_linked_elsewhere_do_not(
    corpus: Corpus, deps: Dependencies
) -> None:
    t1 = corpus.journals["main"]["obs-T1"]
    assert _matches(deps, _variant(t1, operation_ref=None))
    assert not _matches(deps, _variant(t1, operation_ref="SUB-0002"))
    assert not _matches(deps, _variant(t1, representation_id="rep-unknown"))


def test_coverage_predicates(corpus: Corpus, deps: Dependencies) -> None:
    for c in corpus.coverage.values():
        assert deps.matches_coverage(c), c.coverage_id
    document = corpus.coverage["cov-bank-k2"].model_dump(mode="json")
    document["instrument_id"] = "syn:fund:OTHER:class-a"
    other = CoverageCertificate.model_validate_json(json.dumps(document))
    assert not deps.matches_coverage(other)


def test_dependencies_are_deterministic(corpus: Corpus) -> None:
    assert dependencies(corpus.profile, "SUB-0001") == dependencies(corpus.profile, "SUB-0001")
    assert dependencies(corpus.profile, "SUB-0001") != dependencies(corpus.profile, "SUB-0002")
