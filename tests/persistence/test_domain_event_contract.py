"""Domain events v1, pure: contract, identifiers, bytes and the change block.

Expectations were written before the code. Real
evaluations of the corpora; no database.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from invaria.contracts.base import StrictJsonError
from invaria.contracts.evaluation import EvaluationResult
from invaria.contracts.events import (
    DomainEventRecord,
    PublicationRef,
    SnapshotTimes,
    canonical_bytes,
    event_id,
    parse_event,
)
from invaria.corpus_loader import Corpus, load_corpus
from invaria.domain_events import Budget, Limits, compare, evaluation_published, scope_reopened
from invaria.engine.common import Evaluation, EvaluationInputs
from invaria.engine.evaluate import evaluate, replay

CORPUS = Path(__file__).resolve().parents[1] / "fixtures/corpus"
T = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def sub() -> Corpus:
    return load_corpus(CORPUS / "subscription-synthetic")


@pytest.fixture(scope="module")
def red() -> Corpus:
    return load_corpus(CORPUS / "redemption-synthetic-1.7.0")


class Docs:
    def __init__(self, *corpora: Corpus) -> None:
        self.inputs: dict[str, EvaluationInputs] = {}
        self.replays: list[str] = []
        self.corpora = corpora

    def doc(self, corpus: Corpus, scenario: str) -> EvaluationResult:
        inputs = corpus.inputs_for(scenario)
        result = evaluate(inputs).result
        self.inputs[result.evaluation_id] = inputs
        return result

    def load(self, doc: EvaluationResult) -> tuple[EvaluationInputs, Evaluation]:
        self.replays.append(doc.evaluation_id)
        inputs = self.inputs[doc.evaluation_id]
        return inputs, replay(inputs, doc.versions.engine_ref)


def budget(**limits: int) -> Budget:
    return Budget(Limits(**limits))


def statuses(change: Any) -> dict[str, str]:
    names = ("result", "controls", "cited_evidence", "technical_errors", "blocks", "versions")
    return {n: getattr(change, n).status for n in names}


def test_a_financial_change_is_reported_from_the_stored_documents(sub: Corpus) -> None:
    d = Docs()
    k2, k3 = d.doc(sub, "K2"), d.doc(sub, "K3")
    change = compare(k2, k3, d.load, budget())
    assert (change.result.previous, change.result.current) == ("MATCH", "BREAK")
    assert statuses(change) == {
        "result": "changed",
        "controls": "changed",
        "cited_evidence": "changed",
        "technical_errors": "unchanged",
        "blocks": "unchanged",  # neither side has an UNKNOWN control: nothing to replay
        "versions": "unchanged",
    }
    assert [c.control_id for c in change.controls.changed] == ["subscription.cash_vs_order"]
    assert change.controls.changed[0].current == "FAIL/CASH_AMOUNT_MISMATCH"
    assert d.replays == []  # stored documents only


def test_blocks_change_while_unknown_stays_unknown(sub: Corpus) -> None:
    d = Docs()
    k1, conflict = d.doc(sub, "K1"), d.doc(sub, "SOURCE-CONFLICT")
    assert k1.result == conflict.result == "UNKNOWN"
    change = compare(k1, conflict, d.load, budget())
    assert change.result.status == "unchanged"
    assert change.blocks.status == "changed"
    assert {b.control_id for b in change.blocks.removed} >= {"subscription.ta_units_vs_order"}
    assert sorted(d.replays) == sorted([k1.evaluation_id, conflict.evaluation_id])


def test_identical_conclusions_are_unchanged_in_every_dimension(sub: Corpus) -> None:
    d = Docs()
    a, b = d.doc(sub, "K1"), d.doc(sub, "K1")
    assert set(statuses(compare(a, b, d.load, budget())).values()) == {"unchanged"}


def test_missing_information_is_never_unchanged(sub: Corpus, red: Corpus) -> None:
    d = Docs()
    k1, k1b = d.doc(sub, "K1"), d.doc(sub, "K1")
    limited = compare(k1, k1b, d.load, budget(max_replays_per_run=0))
    assert limited.blocks.status == "not_comparable"
    nc = limited.blocks.not_comparable
    assert nc is not None and (nc.reason, nc.detail) == ("limit_reached", "max_replays_per_run=0")
    assert compare(k1, k1b, d.load, budget(max_unknown_controls=1)).blocks.not_comparable
    # Another profile: controls, technical errors and blocks are not comparable; the
    # stored result and the versions still are.
    other = d.doc(red, "RD-PAID")
    change = compare(k1, other, d.load, budget())
    assert statuses(change) == {
        "result": "changed" if other.result != k1.result else "unchanged",
        "controls": "not_comparable",
        "cited_evidence": "changed",
        "technical_errors": "not_comparable",
        "blocks": "not_comparable",
        "versions": "changed",
    }
    assert change.blocks.not_comparable and change.blocks.not_comparable.reason == "profile_changed"


def test_blocked_or_uninstrumented_engines_are_never_replayed(
    sub: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = Docs()
    k1, k1b = d.doc(sub, "K1"), d.doc(sub, "K1")
    monkeypatch.setattr("invaria.domain_events.engine_status", lambda ref: "blocked")
    change = compare(k1, k1b, d.load, budget())
    assert change.blocks.not_comparable and change.blocks.not_comparable.reason == "engine_blocked"
    monkeypatch.setattr("invaria.domain_events.engine_status", lambda ref: "retired")
    monkeypatch.setattr("invaria.domain_events.DIAGNOSTIC_ENGINES", frozenset())
    change = compare(k1, k1b, d.load, budget())
    nc = change.blocks.not_comparable
    assert nc is not None and nc.reason == "engine_without_diagnosis"
    assert d.replays == []


def test_a_replay_that_does_not_reproduce_is_a_reconstruction_mismatch(sub: Corpus) -> None:
    d = Docs()
    k1 = d.doc(sub, "K1")
    edited = k1.model_copy(
        update={
            "controls": [
                c.model_copy(update={"reason": "edited"}) if c.status == "UNKNOWN" else c
                for c in k1.controls
            ]
        }
    )
    d.inputs[edited.evaluation_id] = d.inputs[k1.evaluation_id]
    change = compare(edited, k1, d.load, budget())
    nc = change.blocks.not_comparable
    assert nc is not None and (nc.reason, nc.side) == ("reconstruction_mismatch", "previous")


def test_technical_errors_are_the_evaluation_error_controls(sub: Corpus) -> None:
    d = Docs()
    k1 = d.doc(sub, "K1")
    broken = k1.model_copy(
        update={
            "controls": [
                c.model_copy(update={"reason_code": "EVALUATION_ERROR"})
                if c.control_id == "subscription.cash_vs_order"
                else c
                for c in k1.controls
            ]
        }
    )
    change = compare(broken, k1, lambda doc: d.load(k1), budget(max_replays_per_run=0))
    assert change.technical_errors.status == "changed"
    assert change.technical_errors.previous_controls == ["subscription.cash_vs_order"]
    assert change.technical_errors.current_controls == []


def test_no_previous_and_unidentifiable_previous(sub: Corpus) -> None:
    d = Docs()
    k1 = d.doc(sub, "K1")
    first = compare(None, k1, d.load, budget())
    assert first.basis == "no_previous" and set(statuses(first).values()) == {"no_previous"}
    lost = compare(None, k1, d.load, budget(), unidentifiable="the pinned publication differs")
    assert set(statuses(lost).values()) == {"not_comparable"}
    assert lost.result.not_comparable and lost.result.not_comparable.reason == (
        "previous_unidentifiable"
    )


def published(sub: Corpus) -> Any:
    d = Docs()
    k2, k3 = d.doc(sub, "K2"), d.doc(sub, "K3")
    snap = sub.scenarios["K3"].snapshot
    return evaluation_published(
        dedup_key="tenant-a|SUB-0001|published|3",
        epoch=3,
        current=k3,
        snapshot=SnapshotTimes(
            snapshot_id=snap.snapshot_id,
            valid_at=snap.valid_at,
            known_at=snap.known_at,
            evaluation_clock=snap.evaluation_clock,
        ),
        previous=PublicationRef(epoch=2, evaluation_id=k2.evaluation_id),
        change=compare(k2, k3, d.load, budget()),
        pinned=True,
        published_at=T,
    )


def test_ids_bytes_and_record_round_trip(sub: Corpus) -> None:
    event = published(sub)
    assert event.event_id == event_id(
        "operation.evaluation_published", "tenant-a|SUB-0001|published|3"
    )
    assert event.event_id != event_id("operation.scope_reopened", "tenant-a|SUB-0001|published|3")
    raw = canonical_bytes(event)
    assert parse_event(raw) == event and canonical_bytes(parse_event(raw)) == raw
    assert event.categories == ["result_changed", "controls_changed", "cited_evidence_changed"]
    text = raw.decode()
    for absent in ("tenant-a", "delta", "obs-", "principal", '"currency"', "/home", "secret"):
        assert absent not in text, absent
    record = DomainEventRecord.model_validate(
        {"tenant_seq": 1, "derived_at": T, "payload_sha256": "0" * 64, "event": event}
    )
    assert record.event == event


def test_readers_reject_unknown_versions_extra_fields_and_fractions(sub: Corpus) -> None:
    data = json.loads(canonical_bytes(published(sub)))
    edits: list[tuple[str | None, str, Any]] = [
        (None, "type_version", 2),
        (None, "schema_version", "1.1"),
        (None, "type", "conclusion.changed"),
        ("data", "extra", 1),
    ]
    for section, key, value in edits:
        changed = json.loads(json.dumps(data))
        (changed if section is None else changed[section])[key] = value
        with pytest.raises(ValidationError):
            parse_event(json.dumps(changed).encode())
    changed = json.loads(json.dumps(data))
    changed["data"]["epoch"] = 3.0
    with pytest.raises(StrictJsonError):
        parse_event(json.dumps(changed).encode())


def test_a_reopening_names_the_prior_publication_and_whether_it_was_current() -> None:
    event = scope_reopened(
        dedup_key="t|SUB-0001|invalidated|4",
        operation_ref="SUB-0001",
        epoch=4,
        cause="evidence_appended",
        profile_ref="fund-subscription-synthetic@1.0.0",
        previous_profile_ref=None,
        evidence_counts=(1, 0, 0),
        knowledge_floor=T,
        prior=PublicationRef(epoch=3, evaluation_id="eval-3"),
        pinned=True,
        reopened_at=T,
    )
    assert event.categories == ["currency_lost"]
    assert event.data.prior_publication and event.data.prior_publication.current_until_reopening
    older = scope_reopened(
        dedup_key="t|SUB-0001|invalidated|5",
        operation_ref="SUB-0001",
        epoch=5,
        cause="explicit",
        profile_ref="fund-subscription-synthetic@1.0.0",
        previous_profile_ref=None,
        evidence_counts=None,
        knowledge_floor=None,
        prior=PublicationRef(epoch=3, evaluation_id="eval-3"),
        pinned=False,
        reopened_at=T,
    )
    assert older.categories == []  # epoch 4 was never published: it was already stale
    assert older.provenance.source_payload == "historical"


def test_cited_evidence_is_an_indicator_never_identifiers_or_content(sub: Corpus) -> None:
    """The approved contract excludes evidence from the external payload: a change of cited
    evidence is reported as counts and the controls concerned, nothing else."""
    d = Docs()
    k2, k3 = d.doc(sub, "K2"), d.doc(sub, "K3")
    cited = {ref for doc in (k2, k3) for c in doc.controls for ref in c.evidence_refs}
    assert cited  # the documents do cite evidence
    event = published(sub)
    block = json.loads(canonical_bytes(event))["data"]["change"]["cited_evidence"]
    assert block == {
        "status": "changed",
        "added": 2,
        "removed": 2,
        "controls": ["subscription.cash_vs_order"],
        "not_comparable": None,
    }
    text = canonical_bytes(event).decode()
    assert not [ref for ref in cited if ref in text]  # no evidence identifier anywhere
