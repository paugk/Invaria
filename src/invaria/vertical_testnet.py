"""Subscription SUB-0001 evaluated against real Stellar testnet evidence (offline).

Institutional records are synthetic CSVs imported with the versioned mappings. On-chain
evidence is produced by the Stellar adapter replaying recorded real testnet responses
(Invaria's own DEMOA issuance); execution links are explicit per scenario. Nothing here
contacts the network: an unrecorded request fails loudly.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from invaria.contracts import IdentityLinkSet
from invaria.contracts.base import (
    Contract,
    Identifier,
    NonEmptyText,
    UtcDatetime,
    VersionRef,
    parse_contract,
)
from invaria.contracts.chain import ChainTarget, ExecutionLink
from invaria.contracts.coverage import CoverageCertificate, CoverageLevel, TimeInterval
from invaria.contracts.evaluation import ControlOutcome, FinancialResult, SnapshotRef, aggregate
from invaria.contracts.identity import IdentityLink
from invaria.contracts.mapping import CsvMapping
from invaria.contracts.observation import Observation
from invaria.contracts.profile import OperationProfile
from invaria.engine.evaluate import Evaluation, EvaluationInputs, evaluate
from invaria.engine.snapshot import build_snapshot
from invaria.ingest import CoverageClaim, ImportContext, import_csv
from invaria.stellar.adapter import ingest_horizon_payments, ingest_sac_events
from invaria.stellar.http import ReplayClient
from invaria.stellar.sources import Endpoints, Horizon, Rpc, verify_network
from invaria.stellar.store import IngestStore


class ImportPlan(Contract):
    coverage_id: Identifier
    raw: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]+\.csv$")]
    mapping_ref: VersionRef
    recorded_at: UtcDatetime
    interval: TimeInterval
    level: CoverageLevel
    method: NonEmptyText


class ChainPlan(Contract):
    target: Annotated[str, Field(pattern=r"^[A-Za-z0-9._/-]+\.json$")]
    recordings: Annotated[str, Field(pattern=r"^[A-Za-z0-9._/-]+$")]
    start_ledger: Annotated[int, Field(ge=1)]
    end_ledger: Annotated[int, Field(ge=1)]
    recorded_at: UtcDatetime
    horizon_page_limit: Annotated[int, Field(ge=1, le=200)]
    sac_page_limit: Annotated[int, Field(ge=1, le=10_000)]


class VerticalPlan(Contract):
    tenant_id: Identifier
    operation_ref: Identifier
    valid_at: UtcDatetime
    known_at: UtcDatetime
    imports: Annotated[list[ImportPlan], Field(min_length=1)]
    chain: ChainPlan


class OnchainExpectation(Contract):
    result: FinancialResult
    controls: Annotated[list[ControlOutcome], Field(min_length=1)]

    @model_validator(mode="after")
    def _precedence(self) -> Self:
        if aggregate(self.controls) != self.result:
            raise ValueError("expected result contradicts control precedence")
        return self


class OnchainScenario(Contract):
    scenario_id: Identifier
    rationale: NonEmptyText
    links: list[ExecutionLink]
    expected: OnchainExpectation


class OnchainCatalog(Contract):
    schema_version: Literal["1.0"]
    corpus_id: Identifier
    profile_ref: VersionRef
    notice: NonEmptyText
    plan: VerticalPlan
    scenarios: Annotated[list[OnchainScenario], Field(min_length=1)]


@dataclass(frozen=True)
class OnchainRun:
    scenario: OnchainScenario
    snapshot: SnapshotRef
    inputs: EvaluationInputs
    evaluation: Evaluation
    chain_observations: tuple[Observation, ...]
    log: tuple[str, ...]

    def mismatches(self) -> list[str]:
        expected = self.scenario.expected
        problems = []
        if self.evaluation.result.result != expected.result:
            problems.append(f"result {self.evaluation.result.result} != {expected.result}")
        got = {c.control_id: c for c in self.evaluation.result.controls}
        for control in expected.controls:
            actual = got.get(control.control_id)
            if actual is None:
                problems.append(f"{control.control_id} missing")
                continue
            for name in ("status", "reason_code", "delta"):
                if getattr(actual, name) != getattr(control, name):
                    problems.append(
                        f"{control.control_id}.{name}: {getattr(actual, name)!r} "
                        f"!= {getattr(control, name)!r}"
                    )
        return problems


def _institutional(
    corpus: Path, mappings: dict[str, CsvMapping], plan: VerticalPlan, profile: OperationProfile
) -> tuple[list[Observation], list[CoverageCertificate], list[str]]:
    journal: list[Observation] = []
    coverage: list[CoverageCertificate] = []
    log: list[str] = []
    for item in plan.imports:
        context = ImportContext(
            tenant_id=plan.tenant_id,
            instrument_id=profile.instrument.instrument_id,
            raw_path=f"raw/{item.raw}",
            recorded_at=item.recorded_at,
            coverage=CoverageClaim(
                coverage_id=item.coverage_id,
                interval=item.interval,
                level=item.level,
                method=item.method,
            ),
        )
        outcome = import_csv(
            (corpus / "raw" / item.raw).read_bytes(), mappings[item.mapping_ref], context, journal
        )
        journal.extend(outcome.integration.appended)
        if outcome.coverage is not None:
            coverage.append(outcome.coverage)
        decisions = ", ".join(o.decision for o in outcome.integration.outcomes)
        log.append(f"import {item.raw} (synthetic): {decisions}")
    return journal, coverage, log


def _chain(
    stellar_fixtures: Path, plan: ChainPlan, links: list[ExecutionLink], store_dir: Path
) -> tuple[list[Observation], list[CoverageCertificate], list[str]]:
    target = parse_contract(ChainTarget, (stellar_fixtures / plan.target).read_text("utf-8"))
    client = ReplayClient(stellar_fixtures / plan.recordings)
    endpoints = Endpoints.resolve(None, None)
    horizon, rpc = Horizon(endpoints.horizon_url, client), Rpc(endpoints.rpc_url, client)
    check = verify_network(horizon, rpc, target.network)
    store = IngestStore(store_dir)
    classic = ingest_horizon_payments(
        target,
        horizon,
        store,
        start_ledger=plan.start_ledger,
        end_ledger=plan.end_ledger,
        history=(check.horizon_elder_ledger, check.horizon_latest_ledger),
        recorded_at=plan.recorded_at,
        links=links,
        page_limit=plan.horizon_page_limit,
    )
    sac = ingest_sac_events(
        target,
        rpc,
        horizon,
        store,
        start_ledger=plan.start_ledger,
        end_ledger=plan.end_ledger,
        recorded_at=plan.recorded_at,
        links=links,
        page_limit=plan.sac_page_limit,
    )
    log = [
        f"chain {target.asset_code}:{target.asset_issuer[:8]}… replayed from real testnet "
        f"recordings: classic {len(classic.appended)} effects, SAC duplicates "
        f"{sac.duplicates}, new {len(sac.appended)}"
    ]
    for observation in classic.appended:
        chain = getattr(observation.payload, "chain", None)
        if chain is not None:
            log.append(
                f"  tx {chain.tx_hash[:12]}… ledger {chain.ledger} ok="
                f"{chain.tx_successful} operation_ref={observation.operation_ref}"
            )
    if classic.coverage is None:
        raise ValueError("chain coverage incomplete for the planned ledger range")
    return store.observations(), [classic.coverage], log


def run_testnet_vertical(
    corpus: Path,
    stellar_fixtures: Path,
    mappings_dir: Path,
) -> list[OnchainRun]:
    catalog = parse_contract(OnchainCatalog, (corpus / "scenarios.json").read_text("utf-8"))
    profile = parse_contract(OperationProfile, (corpus / "profile.json").read_text("utf-8"))
    if profile.profile_ref != catalog.profile_ref:
        raise ValueError("scenarios.json and profile.json disagree on profile_ref")
    links_set = parse_contract(IdentityLinkSet, (corpus / "identity_links.json").read_text("utf-8"))
    identity: dict[str, IdentityLink] = {link.link_id: link for link in links_set.links}
    mappings = {
        m.mapping_ref: m
        for m in (
            parse_contract(CsvMapping, p.read_text("utf-8"))
            for p in sorted(mappings_dir.glob("*.json"))
        )
    }
    plan = catalog.plan
    institutional, institutional_cov, base_log = _institutional(corpus, mappings, plan, profile)
    runs: list[OnchainRun] = []
    for scenario in catalog.scenarios:
        with tempfile.TemporaryDirectory(prefix="invaria-testnet-") as tmp:
            chain_obs, chain_cov, chain_log = _chain(
                stellar_fixtures, plan.chain, list(scenario.links), Path(tmp)
            )
        observations = [*institutional, *chain_obs]
        coverage = [*institutional_cov, *chain_cov]
        snapshot = build_snapshot(
            snapshot_id=f"snap-{scenario.scenario_id}",
            tenant_id=plan.tenant_id,
            operation_ref=plan.operation_ref,
            profile=profile,
            valid_at=plan.valid_at,
            known_at=plan.known_at,
            evaluation_clock=plan.known_at,
            observations=observations,
            coverage=coverage,
            identity_links=identity.values(),
        )
        inputs = EvaluationInputs(
            snapshot=snapshot,
            profile=profile,
            observations={o.observation_id: o for o in observations},
            coverage={c.coverage_id: c for c in coverage},
            identity_links=identity,
        )
        runs.append(
            OnchainRun(
                scenario,
                snapshot,
                inputs,
                evaluate(inputs),
                tuple(chain_obs),
                (*base_log, *chain_log),
            )
        )
    return runs
