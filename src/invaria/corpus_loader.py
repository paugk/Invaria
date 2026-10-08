"""Load a corpus directory into contracts. File I/O lives here, never in the engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from invaria.contracts import (
    CoverageSet,
    IdentityLinkSet,
    ObservationJournal,
    Scenario,
    ScenarioCatalog,
    parse_contract,
)
from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.identity import IdentityLink
from invaria.contracts.mapping import CsvMapping
from invaria.contracts.observation import Observation
from invaria.contracts.profile import Profile, parse_profile
from invaria.engine.evaluate import EvaluationInputs


@dataclass(frozen=True)
class Corpus:
    root: Path
    profile: Profile
    coverage: dict[str, CoverageCertificate]
    identity_links: dict[str, IdentityLink]
    scenarios: dict[str, Scenario]
    journals: dict[str, dict[str, Observation]]
    mappings: dict[str, CsvMapping]

    def inputs_for(self, scenario_id: str) -> EvaluationInputs:
        scenario = self.scenarios[scenario_id]
        return EvaluationInputs(
            snapshot=scenario.snapshot,
            profile=self.profile,
            observations=self.journals[scenario.timeline_id],
            coverage=self.coverage,
            identity_links=self.identity_links,
        )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_corpus(root: Path) -> Corpus:
    profile = parse_profile(_read(root / "profile.json"))
    coverage = parse_contract(CoverageSet, _read(root / "coverage.json"))
    links = parse_contract(IdentityLinkSet, _read(root / "identity_links.json"))
    catalog = parse_contract(ScenarioCatalog, _read(root / "scenarios.json"))
    journals: dict[str, dict[str, Observation]] = {}
    for path in sorted((root / "observations").glob("*.json")):
        journal = parse_contract(ObservationJournal, _read(path))
        journals[journal.timeline_id] = {o.observation_id: o for o in journal.observations}
    mappings: dict[str, CsvMapping] = {}
    for path in sorted((root / "mappings").glob("*.json")):
        mapping = parse_contract(CsvMapping, _read(path))
        mappings[mapping.mapping_ref] = mapping
    return Corpus(
        root=root,
        profile=profile,
        coverage={c.coverage_id: c for c in coverage.certificates},
        identity_links={link.link_id: link for link in links.links},
        scenarios={s.scenario_id: s for s in catalog.scenarios},
        journals=journals,
        mappings=mappings,
    )
