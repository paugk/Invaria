"""Build a closed snapshot from what was recorded up to ``known_at``.

The snapshot freezes membership explicitly; later replays use this membership instead of
re-querying stores, so information recorded later can never leak into a past conclusion.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.evaluation import SnapshotRef
from invaria.contracts.identity import IdentityLink
from invaria.contracts.observation import Observation
from invaria.contracts.profile import Profile, admitted_mapping_refs


def build_snapshot(
    *,
    snapshot_id: str,
    tenant_id: str,
    operation_ref: str,
    profile: Profile,
    valid_at: datetime,
    known_at: datetime,
    evaluation_clock: datetime,
    observations: Iterable[Observation],
    coverage: Iterable[CoverageCertificate],
    identity_links: Iterable[IdentityLink],
) -> SnapshotRef:
    def known(items: Iterable[Observation | CoverageCertificate | IdentityLink]) -> list[str]:
        ids = []
        for item in items:
            if item.recorded_at > known_at:
                continue
            if isinstance(item, Observation):
                ids.append(item.observation_id)
            elif isinstance(item, CoverageCertificate):
                ids.append(item.coverage_id)
            else:
                ids.append(item.link_id)
        return sorted(set(ids))

    return SnapshotRef(
        schema_version="1.0",
        snapshot_id=snapshot_id,
        tenant_id=tenant_id,
        operation_ref=operation_ref,
        valid_at=valid_at,
        known_at=known_at,
        evaluation_clock=evaluation_clock,
        observation_ids=known(o for o in observations if o.tenant_id == tenant_id),
        coverage_ids=known(c for c in coverage if c.tenant_id == tenant_id),
        identity_link_ids=known(identity_links),
        profile_ref=profile.profile_ref,
        rules_ref=profile.rules_ref,
        mapping_refs=admitted_mapping_refs(profile.sources),
    )
