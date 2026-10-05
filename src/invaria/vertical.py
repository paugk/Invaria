"""End-to-end offline vertical for the synthetic subscription.

Institutional evidence is imported from the raw CSV bytes with the versioned mappings.
On-chain evidence is NOT ingested: there is no Stellar adapter yet, so the frozen synthetic
chain observation and its coverage are taken as fixtures and labelled as such.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.evaluation import SnapshotRef
from invaria.contracts.observation import Observation
from invaria.corpus_loader import Corpus
from invaria.engine.evaluate import Evaluation, EvaluationInputs, evaluate
from invaria.engine.snapshot import build_snapshot
from invaria.ingest import CoverageClaim, ImportContext, import_csv

# Imports of the main timeline, in recorded order: (coverage claim id in the corpus,
# raw file, mapping). The claim's interval/level/method/recorded_at come from coverage.json.
MAIN_IMPORTS = (
    ("cov-oms-k1", "oms_orders_2026-10-01.csv", "oms-csv-synthetic@1.0.0"),
    ("cov-oms-k2", "oms_orders_2026-10-01.csv", "oms-csv-synthetic@1.0.0"),
    ("cov-bank-k2", "bank_statement_2026-10-01.csv", "bank-csv-synthetic@1.0.0"),
    ("cov-ta-k2", "ta_register_2026-10-01.csv", "ta-csv-synthetic@1.0.0"),
    ("cov-bank-k3", "bank_correction_2026-10-02.csv", "bank-csv-synthetic@1.0.0"),
)
CHAIN_FIXTURES = (("obs-T1",), ("cov-chain-k1", "cov-chain-k2"))
SNAPSHOTS = (
    ("K1", "2026-10-01T09:45:00+00:00", "2026-10-01T10:00:00+00:00"),
    ("K2", "2026-10-01T17:00:00+00:00", "2026-10-01T18:00:00+00:00"),
    ("K3", "2026-10-01T17:00:00+00:00", "2026-10-02T09:30:00+00:00"),
)


@dataclass(frozen=True)
class VerticalRun:
    label: str
    snapshot: SnapshotRef
    evaluation: Evaluation


@dataclass(frozen=True)
class RebuiltTimeline:
    observations: tuple[Observation, ...]
    coverage: tuple[CoverageCertificate, ...]
    log: tuple[str, ...]


def rebuild_main_timeline(corpus: Corpus) -> RebuiltTimeline:
    journal: list[Observation] = []
    coverage: list[CoverageCertificate] = []
    log: list[str] = []
    profile = corpus.profile
    tenant = corpus.coverage[MAIN_IMPORTS[0][0]].tenant_id
    for claim_id, raw_name, mapping_ref in MAIN_IMPORTS:
        declared = corpus.coverage[claim_id]
        context = ImportContext(
            tenant_id=tenant,
            instrument_id=profile.instrument.instrument_id,
            raw_path=f"raw/{raw_name}",
            recorded_at=declared.recorded_at,
            coverage=CoverageClaim(
                coverage_id=claim_id,
                interval=declared.interval,
                level=declared.level,
                method=declared.method,
            ),
        )
        raw = (corpus.root / "raw" / raw_name).read_bytes()
        outcome = import_csv(raw, corpus.mappings[mapping_ref], context, journal)
        journal.extend(outcome.integration.appended)
        if outcome.coverage is not None:
            coverage.append(outcome.coverage)
        decisions = ", ".join(o.decision for o in outcome.integration.outcomes) or "none"
        quarantined = len(outcome.parsed.quarantined)
        log.append(
            f"import {raw_name} @ {declared.recorded_at.isoformat()}: {decisions}; "
            f"quarantined rows {quarantined}"
        )
    main = corpus.journals["main"]
    for observation_id in CHAIN_FIXTURES[0]:
        journal.append(main[observation_id])
        log.append(f"chain fixture {observation_id} (frozen synthetic; no Stellar ingestion)")
    for coverage_id in CHAIN_FIXTURES[1]:
        coverage.append(corpus.coverage[coverage_id])
        log.append(f"chain coverage fixture {coverage_id}")
    return RebuiltTimeline(tuple(journal), tuple(coverage), tuple(log))


def run_vertical(corpus: Corpus) -> tuple[RebuiltTimeline, list[VerticalRun]]:
    timeline = rebuild_main_timeline(corpus)
    runs: list[VerticalRun] = []
    links = list(corpus.identity_links.values())
    for label, valid_at, known_at in SNAPSHOTS:
        known = datetime.fromisoformat(known_at)
        snapshot = build_snapshot(
            snapshot_id=f"snap-vertical-{label}",
            tenant_id=timeline.coverage[0].tenant_id,
            operation_ref="SUB-0001",
            profile=corpus.profile,
            valid_at=datetime.fromisoformat(valid_at),
            known_at=known,
            evaluation_clock=known,
            observations=timeline.observations,
            coverage=timeline.coverage,
            identity_links=links,
        )
        inputs = EvaluationInputs(
            snapshot=snapshot,
            profile=corpus.profile,
            observations={o.observation_id: o for o in timeline.observations},
            coverage={c.coverage_id: c for c in timeline.coverage},
            identity_links=corpus.identity_links,
        )
        runs.append(VerticalRun(label, snapshot, evaluate(inputs)))
    return timeline, runs
