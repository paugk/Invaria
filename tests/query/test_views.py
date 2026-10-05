"""Pure parts of the query layer: coverage judgement and argument validation (no database)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from invaria.contracts.coverage import CoverageCertificate
from invaria.corpus_loader import load_corpus
from invaria.engine.evaluate import EvaluationInputs
from invaria.query.service import QueryError, QueryService, _utc

CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"


def test_coverage_below_the_required_level_is_flagged() -> None:
    k2 = load_corpus(CORPUS_DIR).inputs_for("K2")
    bank = k2.coverage["cov-bank-k2"].model_dump(mode="json")
    bank["level"] = "provider_claimed"
    weaker = dict(k2.coverage)
    weaker["cov-bank-k2"] = CoverageCertificate.model_validate_json(json.dumps(bank))
    inputs = EvaluationInputs(k2.snapshot, k2.profile, k2.observations, weaker, k2.identity_links)
    views = {v.coverage_id: v for v in QueryService._coverage(inputs)}
    assert views["cov-bank-k2"].level == "provider_claimed"
    assert views["cov-bank-k2"].required_level == "internally_checked"
    assert views["cov-bank-k2"].meets_required_level is False
    assert views["cov-ta-k2"].meets_required_level is True


def test_only_utc_times_are_accepted() -> None:
    assert _utc("2026-10-01T17:00:00Z", "t") == datetime(2026, 10, 1, 17, tzinfo=UTC)
    utc = datetime(2026, 10, 1, 17, tzinfo=UTC)
    assert _utc(utc, "t") == utc
    for bad in (
        datetime(2026, 10, 1, 19, tzinfo=timezone(timedelta(hours=2))),
        datetime(2026, 10, 1, 17),
        "2026-10-01T17:00:00",
        "2026-10-01T19:00:00+02:00",
    ):
        with pytest.raises(QueryError) as invalid:
            _utc(bad, "t")
        assert invalid.value.code == "VALIDATION_ERROR"
