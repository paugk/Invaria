"""Integrate parsed rows into an append-only delivery journal.

Never edits or removes existing observations. Decides, per incoming row:
NEW, REVISION (links ``supersedes``), DUPLICATE (idempotent, not appended),
SOURCE_CONFLICT (same key and revision, different content: appended, nothing chosen),
ORPHAN_RETRACTION or OUT_OF_ORDER (rejected, not appended).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.mapping import CsvMapping
from invaria.contracts.observation import Observation
from invaria.ingest.csv_import import (
    Candidate,
    ImportContext,
    ParseResult,
    coverage_for,
    parse_csv,
)

Decision = Literal[
    "NEW",
    "REVISION",
    "DUPLICATE",
    "SOURCE_CONFLICT",
    "ORPHAN_RETRACTION",
    "OUT_OF_ORDER",
    "CONTRACT_VIOLATION",
]
REJECTED: frozenset[str] = frozenset({"ORPHAN_RETRACTION", "OUT_OF_ORDER", "CONTRACT_VIOLATION"})


@dataclass(frozen=True)
class RecordOutcome:
    row: int
    decision: Decision
    observation_id: str | None
    related_ids: tuple[str, ...]


@dataclass(frozen=True)
class Integration:
    appended: tuple[Observation, ...]
    outcomes: tuple[RecordOutcome, ...]

    @property
    def rejected_rows(self) -> int:
        return sum(1 for o in self.outcomes if o.decision in REJECTED)


def _content(observation: Observation) -> tuple[Any, ...]:
    return (
        observation.kind,
        observation.fact_type,
        observation.instrument_id,
        observation.representation_id,
        observation.operation_ref,
        observation.valid_time,
        observation.payload,
    )


def integrate(journal: Sequence[Observation], candidates: Sequence[Candidate]) -> Integration:
    working = list(journal)
    appended: list[Observation] = []
    outcomes: list[RecordOutcome] = []
    for candidate in candidates:
        source = candidate.source
        same_revision = [o for o in working if o.source == source]
        if same_revision:
            twins = [o.observation_id for o in same_revision if _content(o) == candidate.content()]
            if twins:
                outcomes.append(RecordOutcome(candidate.row, "DUPLICATE", None, tuple(twins)))
                continue
            decision: Decision = "SOURCE_CONFLICT"
            related = tuple(o.observation_id for o in same_revision)
            supersedes = None
        else:
            record = (source.source_id, source.record_key)
            earlier = [
                o
                for o in working
                if (o.source.source_id, o.source.record_key) == record
                and o.source.revision < source.revision
            ]
            if earlier:
                target = max(
                    earlier, key=lambda o: (o.source.revision, o.recorded_at, o.observation_id)
                )
                if target.recorded_at >= candidate.recorded_at:
                    outcomes.append(
                        RecordOutcome(candidate.row, "OUT_OF_ORDER", None, (target.observation_id,))
                    )
                    continue
                decision, related, supersedes = (
                    "REVISION",
                    (target.observation_id,),
                    (target.observation_id),
                )
            elif candidate.kind == "retraction":
                outcomes.append(RecordOutcome(candidate.row, "ORPHAN_RETRACTION", None, ()))
                continue
            else:
                decision, related, supersedes = "NEW", (), None
        try:
            observation = candidate.to_observation(supersedes)
        except ValidationError:
            outcomes.append(RecordOutcome(candidate.row, "CONTRACT_VIOLATION", None, related))
            continue
        working.append(observation)
        appended.append(observation)
        outcomes.append(RecordOutcome(candidate.row, decision, observation.observation_id, related))
    return Integration(tuple(appended), tuple(outcomes))


@dataclass(frozen=True)
class ImportOutcome:
    parsed: ParseResult
    integration: Integration
    coverage: CoverageCertificate | None


def import_csv(
    raw: bytes,
    mapping: CsvMapping,
    context: ImportContext,
    journal: Sequence[Observation],
) -> ImportOutcome:
    """Parse, integrate and certify coverage for one CSV export."""
    parsed = parse_csv(raw, mapping, context)
    integration = integrate(journal, parsed.candidates)
    coverage = coverage_for(parsed, mapping, context, integration.rejected_rows)
    return ImportOutcome(parsed, integration, coverage)
