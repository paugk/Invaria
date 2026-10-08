"""Containers for the synthetic corpus files.

A journal is the delivery log of one timeline. It may contain redeliveries and conflicting
deliveries on purpose; it may never contain edits: corrections and retractions are new
revisions that point backwards with ``supersedes``.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from invaria.contracts.base import Contract, Identifier, SchemaVersion
from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.identity import IdentityLink
from invaria.contracts.observation import Observation


class ObservationJournal(Contract):
    schema_version: SchemaVersion
    corpus_id: Identifier
    timeline_id: Identifier
    synthetic: Literal[True]
    observations: Annotated[list[Observation], Field(min_length=1)]

    @model_validator(mode="after")
    def _revisions(self) -> Self:
        by_id: dict[str, Observation] = {}
        for observation in self.observations:
            if observation.observation_id in by_id:
                raise ValueError(f"duplicate observation_id {observation.observation_id}")
            by_id[observation.observation_id] = observation
        for observation in self.observations:
            if observation.supersedes is None:
                continue
            target = by_id.get(observation.supersedes)
            if target is None:
                raise ValueError(f"{observation.observation_id} supersedes an unknown id")
            same_record = (target.source.source_id, target.source.record_key) == (
                observation.source.source_id,
                observation.source.record_key,
            )
            if not same_record:
                raise ValueError("supersedes must stay within the same source record")
            if target.source.revision >= observation.source.revision:
                raise ValueError("a superseding revision must be greater than its target")
            if target.recorded_at >= observation.recorded_at:
                raise ValueError("a superseding revision must be recorded later")
        return self


class CoverageSet(Contract):
    schema_version: SchemaVersion
    corpus_id: Identifier
    synthetic: Literal[True]
    certificates: Annotated[list[CoverageCertificate], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [c.coverage_id for c in self.certificates]
        if len(set(ids)) != len(ids):
            raise ValueError("coverage_id must be unique")
        return self


class IdentityLinkSet(Contract):
    schema_version: SchemaVersion
    corpus_id: Identifier
    synthetic: Literal[True]
    links: Annotated[list[IdentityLink], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [link.link_id for link in self.links]
        if len(set(ids)) != len(ids):
            raise ValueError("link_id must be unique")
        return self
