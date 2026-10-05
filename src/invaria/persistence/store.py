"""Append-only PostgreSQL store for evidence, closed snapshots and evaluations.

Writes run as ``invaria_app`` (SELECT + INSERT only). Documents are stored whole and
re-validated with the strict contracts on read. Snapshot membership is persisted, so a
later commit can never change what an existing snapshot contains.

Revision: every scope ``(tenant, operation_ref)`` has an append-only
``revision_epoch``. Appending evidence that matches a scope's dependency predicates opens a
new epoch and writes an outbox event in the same transaction. Publication is a
compare-and-swap on the epoch, so a result computed on a superseded revision never becomes
current. Writers, snapshot builds and publication serialize per tenant (advisory lock).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from invaria.contracts.base import parse_contract
from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.evaluation import EvaluationResult, SnapshotRef
from invaria.contracts.identity import IdentityLink
from invaria.contracts.observation import Observation
from invaria.contracts.profile import OperationProfile
from invaria.engine.dependencies import dependencies
from invaria.engine.evaluate import EvaluationInputs

APP_ROLE = "invaria_app"
Conn = psycopg.Connection[tuple[Any, ...]]


class ImmutableConflict(Exception):
    """Same identifier, different content: history is never overwritten."""


class SnapshotInvalid(Exception):
    """A snapshot names members that do not exist or were recorded after known_at."""


class LateRecord(Exception):
    """A new record claims a recorded_at inside an already closed knowledge cut.

    Accepting it would change what a rebuild "as known at" that cut contains.
    """


PublishOutcome = Literal["PUBLISHED", "ALREADY_PUBLISHED", "NOT_CURRENT"]
Currency = Literal["current", "stale", "superseded"]
EpochCause = Literal["registered", "evidence_appended", "profile_changed", "explicit"]


@dataclass(frozen=True)
class ScopeHead:
    tenant_id: str
    operation_ref: str
    epoch: int
    profile_ref: str


@dataclass(frozen=True)
class Publication:
    outcome: PublishOutcome
    evaluation_id: str
    epoch: int
    detail: str


@dataclass(frozen=True)
class CurrentView:
    evaluation_id: str
    published_epoch: int
    scope_epoch: int
    currency: Currency


@dataclass(frozen=True)
class OutboxEvent:
    event_seq: int
    tenant_id: str
    event_type: str
    operation_ref: str
    epoch: int
    payload: dict[str, Any]


def _doc(model: BaseModel) -> dict[str, Any]:
    document: dict[str, Any] = model.model_dump(mode="json")
    return document


def _load[M: BaseModel](model: type[M], document: Any) -> M:
    return parse_contract(model, json.dumps(document))


class PgStore:
    def __init__(self, conn: Conn, *, role: str | None = APP_ROLE) -> None:
        self.conn = conn
        if role is not None:
            conn.execute(f"SET ROLE {role}")  # constant role name, not user input
            conn.commit()

    # ---------------------------------------------------------------- helpers

    def _insert_once(
        self, table: str, key: dict[str, Any], columns: dict[str, Any], document: dict[str, Any]
    ) -> bool:
        """Insert unless the key exists; identical existing content is fine, else conflict."""
        names = [*key, *columns, "document"]
        values = [*key.values(), *columns.values(), Jsonb(document)]
        placeholders = ", ".join(["%s"] * len(values))
        inserted = self.conn.execute(
            f"INSERT INTO invaria.{table} ({', '.join(names)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({', '.join(key)}) DO NOTHING RETURNING 1",
            values,
        ).fetchone()
        if inserted is not None:
            return True
        where = " AND ".join(f"{k} = %s" for k in key)
        row = self.conn.execute(
            f"SELECT document FROM invaria.{table} WHERE {where}", list(key.values())
        ).fetchone()
        if row is None or row[0] != document:
            raise ImmutableConflict(f"{table} {list(key.values())} exists with other content")
        return False

    # ---------------------------------------------------------------- writes

    def put_profile(self, profile: OperationProfile) -> bool:
        with self.conn.transaction():
            return self._insert_once(
                "profiles", {"profile_ref": profile.profile_ref}, {}, _doc(profile)
            )

    def append_observations(self, observations: Iterable[Observation]) -> int:
        items = list(observations)
        appended: list[Observation] = []
        with self.conn.transaction():
            for tenant in sorted({o.tenant_id for o in items}):
                self._lock_tenant(tenant)
            for o in items:
                if self._insert_once(
                    "observations",
                    {"tenant_id": o.tenant_id, "observation_id": o.observation_id},
                    {
                        "source_id": o.source.source_id,
                        "record_key": o.source.record_key,
                        "revision": o.source.revision,
                        "fact_type": o.fact_type,
                        "kind": o.kind,
                        "operation_ref": o.operation_ref,
                        "valid_time": o.valid_time,
                        "recorded_at": o.recorded_at,
                        "raw_sha256": o.provenance.raw_sha256,
                        "mapping_ref": o.provenance.mapping_ref,
                        "supersedes": o.supersedes,
                    },
                    _doc(o),
                ):
                    appended.append(o)
            for tenant in sorted({o.tenant_id for o in appended}):
                mine = [o for o in appended if o.tenant_id == tenant]
                self._reject_late(
                    tenant, "observation", [(o.observation_id, o.recorded_at) for o in mine]
                )
                self._invalidate_for_evidence(tenant, observations=mine)
        return len(appended)

    def append_coverage(self, certificates: Iterable[CoverageCertificate]) -> int:
        items = list(certificates)
        appended: list[CoverageCertificate] = []
        with self.conn.transaction():
            for tenant in sorted({c.tenant_id for c in items}):
                self._lock_tenant(tenant)
            for c in items:
                if self._insert_once(
                    "coverage_certificates",
                    {"tenant_id": c.tenant_id, "coverage_id": c.coverage_id},
                    {"source_id": c.source_id, "recorded_at": c.recorded_at},
                    _doc(c),
                ):
                    appended.append(c)
            for tenant in sorted({c.tenant_id for c in appended}):
                mine = [c for c in appended if c.tenant_id == tenant]
                self._reject_late(
                    tenant, "coverage", [(c.coverage_id, c.recorded_at) for c in mine]
                )
                self._invalidate_for_evidence(tenant, coverage=mine)
        return len(appended)

    def append_identity_links(self, tenant_id: str, links: Iterable[IdentityLink]) -> int:
        appended: list[IdentityLink] = []
        with self.conn.transaction():
            self._lock_tenant(tenant_id)
            for link in links:
                if self._insert_once(
                    "identity_links",
                    {"tenant_id": tenant_id, "link_id": link.link_id},
                    {"recorded_at": link.recorded_at},
                    _doc(link),
                ):
                    appended.append(link)
            if appended:
                self._reject_late(
                    tenant_id, "identity link", [(k.link_id, k.recorded_at) for k in appended]
                )
                self._invalidate_for_evidence(tenant_id, links=appended)
        return len(appended)

    # -------------------------------------------------------------- revision

    def _lock_tenant(self, tenant_id: str) -> None:
        """Serialize writers, snapshot builds and publication of one tenant."""
        self.conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"invaria:{tenant_id}",)
        )

    def _reject_late(self, tenant_id: str, kind: str, records: list[tuple[str, datetime]]) -> None:
        row = self.conn.execute(
            "SELECT max(known_at) FROM invaria.knowledge_cuts WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()
        cut = row[0] if row else None
        if cut is None:
            return
        late = sorted(i for i, recorded in records if recorded <= cut)
        if late:
            raise LateRecord(f"{kind} {late} recorded at or before the closed cut {cut}")

    def _heads(self, tenant_id: str, operation_ref: str | None = None) -> list[ScopeHead]:
        rows = self.conn.execute(
            "SELECT DISTINCT ON (operation_ref) operation_ref, epoch, profile_ref "
            "FROM invaria.scope_epochs WHERE tenant_id = %s "
            "AND (%s::text IS NULL OR operation_ref = %s) "
            "ORDER BY operation_ref, epoch DESC",
            (tenant_id, operation_ref, operation_ref),
        ).fetchall()
        return [ScopeHead(tenant_id, str(r[0]), int(r[1]), str(r[2])) for r in rows]

    def _open_epoch(
        self,
        tenant_id: str,
        operation_ref: str,
        epoch: int,
        profile_ref: str,
        cause: EpochCause,
        knowledge_floor: datetime | None,
        detail: dict[str, Any],
    ) -> ScopeHead:
        document = {"cause": cause, **detail}
        self.conn.execute(
            "INSERT INTO invaria.scope_epochs (tenant_id, operation_ref, epoch, profile_ref, "
            "cause, knowledge_floor, document) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (tenant_id, operation_ref, epoch, profile_ref, cause, knowledge_floor, Jsonb(document)),
        )
        self.conn.execute(
            "INSERT INTO invaria.outbox (tenant_id, event_type, operation_ref, epoch, dedup_key, "
            "payload) VALUES (%s, 'scope.invalidated', %s, %s, %s, %s)",
            (
                tenant_id,
                operation_ref,
                epoch,
                f"{tenant_id}|{operation_ref}|invalidated|{epoch}",
                Jsonb({"profile_ref": profile_ref, **document}),
            ),
        )
        return ScopeHead(tenant_id, operation_ref, epoch, profile_ref)

    def _invalidate_for_evidence(
        self,
        tenant_id: str,
        *,
        observations: Sequence[Observation] = (),
        coverage: Sequence[CoverageCertificate] = (),
        links: Sequence[IdentityLink] = (),
    ) -> list[ScopeHead]:
        """Open a new epoch for every scope whose dependency predicates match the evidence."""
        superseded_refs: dict[str, str | None] = {}
        superseded = sorted({o.supersedes for o in observations if o.supersedes is not None})
        if superseded:
            rows = self.conn.execute(
                "SELECT observation_id, operation_ref FROM invaria.observations "
                "WHERE tenant_id = %s AND observation_id = ANY(%s)",
                (tenant_id, superseded),
            ).fetchall()
            superseded_refs = {str(r[0]): r[1] for r in rows}
        profiles: dict[str, OperationProfile] = {}
        opened: list[ScopeHead] = []
        for head in self._heads(tenant_id):
            if head.profile_ref not in profiles:
                profiles[head.profile_ref] = self._profile(head.profile_ref)
            deps = dependencies(profiles[head.profile_ref], head.operation_ref)
            hit_obs = [
                o
                for o in observations
                if deps.matches_observation(
                    o,
                    [o.operation_ref]
                    + ([superseded_refs.get(o.supersedes)] if o.supersedes else []),
                )
            ]
            hit_cov = [c for c in coverage if deps.matches_coverage(c)]
            hit_links = list(links) if deps.any_identity_link else []
            if not (hit_obs or hit_cov or hit_links):
                continue
            recorded = [
                *(o.recorded_at for o in hit_obs),
                *(c.recorded_at for c in hit_cov),
                *(k.recorded_at for k in hit_links),
            ]
            opened.append(
                self._open_epoch(
                    tenant_id,
                    head.operation_ref,
                    head.epoch + 1,
                    head.profile_ref,
                    "evidence_appended",
                    max(recorded),
                    {
                        "observation_ids": sorted(o.observation_id for o in hit_obs),
                        "coverage_ids": sorted(c.coverage_id for c in hit_cov),
                        "identity_link_ids": sorted(k.link_id for k in hit_links),
                    },
                )
            )
        return opened

    def register_scope(self, tenant_id: str, operation_ref: str, profile_ref: str) -> ScopeHead:
        """Watch an operation. Binding it to another profile version opens a new epoch."""
        with self.conn.transaction():
            self._lock_tenant(tenant_id)
            self._profile(profile_ref)
            heads = self._heads(tenant_id, operation_ref)
            if not heads:
                return self._open_epoch(
                    tenant_id, operation_ref, 1, profile_ref, "registered", None, {}
                )
            (head,) = heads
            if head.profile_ref == profile_ref:
                return head
            return self._open_epoch(
                tenant_id,
                operation_ref,
                head.epoch + 1,
                profile_ref,
                "profile_changed",
                None,
                {"previous_profile_ref": head.profile_ref},
            )

    def invalidate(
        self, tenant_id: str, operation_refs: Iterable[str] | None, reason: str
    ) -> list[ScopeHead]:
        """Explicit invalidation (e.g. a new engine version); None means every scope."""
        wanted = None if operation_refs is None else set(operation_refs)
        with self.conn.transaction():
            self._lock_tenant(tenant_id)
            return [
                self._open_epoch(
                    tenant_id,
                    h.operation_ref,
                    h.epoch + 1,
                    h.profile_ref,
                    "explicit",
                    None,
                    {"reason": reason},
                )
                for h in self._heads(tenant_id)
                if wanted is None or h.operation_ref in wanted
            ]

    def scope_head(self, tenant_id: str, operation_ref: str) -> ScopeHead:
        heads = self._heads(tenant_id, operation_ref)
        self.conn.commit()
        if not heads:
            raise KeyError(f"scope {operation_ref} not registered")
        return heads[0]

    def publish(self, tenant_id: str, evaluation_id: str, epoch: int) -> Publication:
        """Compare-and-swap: make the evaluation current only if ``epoch`` is still current.

        ``epoch`` is the one the worker read *before* building its snapshot. A refused
        attempt is kept (``publish_attempts``); the evaluation itself stays stored.
        """
        with self.conn.transaction():
            self._lock_tenant(tenant_id)
            evaluation = self._evaluation(tenant_id, evaluation_id)
            snapshot = self._snapshot(tenant_id, evaluation.snapshot_id)
            operation_ref = evaluation.operation_ref
            heads = self._heads(tenant_id, operation_ref)
            if not heads:
                raise KeyError(f"scope {operation_ref} not registered")
            head = heads[0]
            row = self.conn.execute(
                "SELECT evaluation_id FROM invaria.publications "
                "WHERE tenant_id = %s AND operation_ref = %s AND epoch = %s",
                (tenant_id, operation_ref, epoch),
            ).fetchone()
            existing = None if row is None else str(row[0])
            if existing == evaluation_id:
                return Publication("ALREADY_PUBLISHED", evaluation_id, epoch, "idempotent")
            floor_row = self.conn.execute(
                "SELECT max(knowledge_floor) FROM invaria.scope_epochs "
                "WHERE tenant_id = %s AND operation_ref = %s AND epoch <= %s",
                (tenant_id, operation_ref, epoch),
            ).fetchone()
            floor = floor_row[0] if floor_row else None
            refusal: str | None = None
            if epoch != head.epoch:
                refusal = f"epoch {epoch} is not current (scope is at {head.epoch})"
            elif existing is not None:
                refusal = f"epoch {epoch} already published {existing}"
            elif evaluation.versions.profile_ref != head.profile_ref:
                refusal = (
                    f"evaluated with {evaluation.versions.profile_ref}; "
                    f"scope is bound to {head.profile_ref}"
                )
            elif floor is not None and snapshot.known_at < floor:
                refusal = f"snapshot known_at {snapshot.known_at} predates the epoch's evidence"
            outcome: PublishOutcome = "NOT_CURRENT" if refusal else "PUBLISHED"
            detail = refusal or "current"
            self.conn.execute(
                "INSERT INTO invaria.publish_attempts (tenant_id, evaluation_id, epoch, outcome, "
                "detail) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (tenant_id, evaluation_id, epoch, outcome, detail),
            )
            if refusal is None:
                self.conn.execute(
                    "INSERT INTO invaria.publications (tenant_id, operation_ref, epoch, "
                    "evaluation_id) VALUES (%s, %s, %s, %s)",
                    (tenant_id, operation_ref, epoch, evaluation_id),
                )
                self.conn.execute(
                    "INSERT INTO invaria.outbox (tenant_id, event_type, operation_ref, epoch, "
                    "dedup_key, payload) VALUES (%s, 'evaluation.published', %s, %s, %s, %s)",
                    (
                        tenant_id,
                        operation_ref,
                        epoch,
                        f"{tenant_id}|{operation_ref}|published|{epoch}",
                        Jsonb({"evaluation_id": evaluation_id, "result": evaluation.result}),
                    ),
                )
            return Publication(outcome, evaluation_id, epoch, detail)

    def current_evaluation(self, tenant_id: str, operation_ref: str) -> CurrentView | None:
        heads = self._heads(tenant_id, operation_ref)
        row = self.conn.execute(
            "SELECT evaluation_id, epoch FROM invaria.publications "
            "WHERE tenant_id = %s AND operation_ref = %s ORDER BY epoch DESC LIMIT 1",
            (tenant_id, operation_ref),
        ).fetchone()
        self.conn.commit()
        if not heads:
            raise KeyError(f"scope {operation_ref} not registered")
        if row is None:
            return None
        scope_epoch = heads[0].epoch
        published = int(row[1])
        currency: Currency = "current" if published == scope_epoch else "stale"
        return CurrentView(str(row[0]), published, scope_epoch, currency)

    def currency(self, tenant_id: str, evaluation_id: str) -> Currency:
        """current / stale for the latest publication; superseded for any other evaluation."""
        evaluation = self.load_evaluation(tenant_id, evaluation_id)
        view = self.current_evaluation(tenant_id, evaluation.operation_ref)
        if view is not None and view.evaluation_id == evaluation_id:
            return view.currency
        return "superseded"

    def publish_attempts(self, tenant_id: str, evaluation_id: str) -> list[tuple[int, str, str]]:
        rows = self.conn.execute(
            "SELECT epoch, outcome, detail FROM invaria.publish_attempts "
            "WHERE tenant_id = %s AND evaluation_id = %s ORDER BY epoch",
            (tenant_id, evaluation_id),
        ).fetchall()
        self.conn.commit()
        return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]

    def epochs(self, tenant_id: str, operation_ref: str) -> list[tuple[int, str, dict[str, Any]]]:
        rows = self.conn.execute(
            "SELECT epoch, cause, document FROM invaria.scope_epochs "
            "WHERE tenant_id = %s AND operation_ref = %s ORDER BY epoch",
            (tenant_id, operation_ref),
        ).fetchall()
        self.conn.commit()
        return [(int(r[0]), str(r[1]), dict(r[2])) for r in rows]

    # ---------------------------------------------------------------- outbox

    def outbox_pending(self, consumer_id: str, limit: int = 100) -> list[OutboxEvent]:
        """Events not yet acknowledged by this consumer (at-least-once delivery)."""
        rows = self.conn.execute(
            "SELECT o.event_seq, o.tenant_id, o.event_type, o.operation_ref, o.epoch, o.payload "
            "FROM invaria.outbox o WHERE NOT EXISTS (SELECT 1 FROM invaria.outbox_acks a "
            "WHERE a.consumer_id = %s AND a.event_seq = o.event_seq) "
            "ORDER BY o.event_seq LIMIT %s",
            (consumer_id, limit),
        ).fetchall()
        self.conn.commit()
        return [
            OutboxEvent(int(r[0]), str(r[1]), str(r[2]), str(r[3]), int(r[4]), dict(r[5]))
            for r in rows
        ]

    def outbox_ack(self, consumer_id: str, event_seq: int) -> bool:
        with self.conn.transaction():
            row = self.conn.execute(
                "INSERT INTO invaria.outbox_acks (consumer_id, event_seq) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING RETURNING 1",
                (consumer_id, event_seq),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------- snapshots

    def _check_members(self, snapshot: SnapshotRef) -> None:
        checks = [
            ("observations", "observation_id", snapshot.observation_ids),
            ("coverage_certificates", "coverage_id", snapshot.coverage_ids),
            ("identity_links", "link_id", snapshot.identity_link_ids),
        ]
        for table, column, ids in checks:
            rows = self.conn.execute(
                f"SELECT {column}, recorded_at FROM invaria.{table} "
                f"WHERE tenant_id = %s AND {column} = ANY(%s)",
                (snapshot.tenant_id, list(ids)),
            ).fetchall()
            found = {r[0]: r[1] for r in rows}
            missing = sorted(set(ids) - set(found))
            if missing:
                raise SnapshotInvalid(f"{table} members missing: {missing}")
            late = sorted(i for i, recorded in found.items() if recorded > snapshot.known_at)
            if late:
                raise SnapshotInvalid(f"{table} members recorded after known_at: {late}")

    def create_snapshot(self, snapshot: SnapshotRef) -> bool:
        """Persist a closed snapshot and its explicit membership (idempotent)."""
        with self.conn.transaction():
            self._check_members(snapshot)
            created = self._insert_once(
                "snapshots",
                {"tenant_id": snapshot.tenant_id, "snapshot_id": snapshot.snapshot_id},
                {
                    "operation_ref": snapshot.operation_ref,
                    "valid_at": snapshot.valid_at,
                    "known_at": snapshot.known_at,
                    "evaluation_clock": snapshot.evaluation_clock,
                    "profile_ref": snapshot.profile_ref,
                },
                _doc(snapshot),
            )
            if created:
                for table, column, ids in (
                    ("snapshot_observations", "observation_id", snapshot.observation_ids),
                    ("snapshot_coverage", "coverage_id", snapshot.coverage_ids),
                    ("snapshot_identity_links", "link_id", snapshot.identity_link_ids),
                ):
                    with self.conn.cursor() as cursor:
                        cursor.executemany(
                            f"INSERT INTO invaria.{table} (tenant_id, snapshot_id, {column}) "
                            "VALUES (%s, %s, %s)",
                            [(snapshot.tenant_id, snapshot.snapshot_id, i) for i in ids],
                        )
            return created

    def build_snapshot(
        self,
        *,
        snapshot_id: str,
        tenant_id: str,
        operation_ref: str,
        profile_ref: str,
        valid_at: datetime,
        known_at: datetime,
        evaluation_clock: datetime,
    ) -> SnapshotRef:
        """Close a snapshot from what is recorded up to known_at, then persist it.

        Under the tenant lock no writer is mid-commit, so the membership is one consistent
        view. The build also records a knowledge cut: afterwards no new record may claim a
        recorded_at at or before it, so rebuilding "as known at" the cut is stable.
        """
        profile = self.load_profile(profile_ref)
        self.conn.commit()
        with self.conn.transaction():
            self._lock_tenant(tenant_id)

            def ids(table: str, column: str) -> list[str]:
                rows = self.conn.execute(
                    f"SELECT {column} FROM invaria.{table} "
                    "WHERE tenant_id = %s AND recorded_at <= %s ORDER BY 1",
                    (tenant_id, known_at),
                ).fetchall()
                return [r[0] for r in rows]

            snapshot = SnapshotRef(
                schema_version="1.0",
                snapshot_id=snapshot_id,
                tenant_id=tenant_id,
                operation_ref=operation_ref,
                valid_at=valid_at,
                known_at=known_at,
                evaluation_clock=evaluation_clock,
                observation_ids=ids("observations", "observation_id"),
                coverage_ids=ids("coverage_certificates", "coverage_id"),
                identity_link_ids=ids("identity_links", "link_id"),
                profile_ref=profile.profile_ref,
                rules_ref=profile.rules_ref,
                mapping_refs=sorted({s.mapping_ref for s in profile.sources}),
            )
            self.create_snapshot(snapshot)
            self.conn.execute(
                "INSERT INTO invaria.knowledge_cuts (tenant_id, known_at) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (tenant_id, known_at),
            )
        return snapshot

    # ----------------------------------------------------------------- reads

    # Public reads end their implicit transaction, so no lock or snapshot is left open;
    # the underscored variants are for use inside a transaction block.

    def _profile(self, profile_ref: str) -> OperationProfile:
        row = self.conn.execute(
            "SELECT document FROM invaria.profiles WHERE profile_ref = %s", (profile_ref,)
        ).fetchone()
        if row is None:
            raise KeyError(f"profile {profile_ref} not stored")
        return _load(OperationProfile, row[0])

    def load_profile(self, profile_ref: str) -> OperationProfile:
        try:
            return self._profile(profile_ref)
        finally:
            self.conn.commit()

    def _snapshot(self, tenant_id: str, snapshot_id: str) -> SnapshotRef:
        row = self.conn.execute(
            "SELECT document FROM invaria.snapshots WHERE tenant_id = %s AND snapshot_id = %s",
            (tenant_id, snapshot_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"snapshot {snapshot_id} not stored")
        return _load(SnapshotRef, row[0])

    def load_snapshot(self, tenant_id: str, snapshot_id: str) -> SnapshotRef:
        try:
            return self._snapshot(tenant_id, snapshot_id)
        finally:
            self.conn.commit()

    def _members(
        self,
        model: type[Any],
        table: str,
        link_table: str,
        column: str,
        tenant_id: str,
        snapshot_id: str,
    ) -> list[Any]:
        rows = self.conn.execute(
            f"SELECT t.document FROM invaria.{link_table} m JOIN invaria.{table} t "
            f"ON t.tenant_id = m.tenant_id AND t.{column} = m.{column} "
            f"WHERE m.tenant_id = %s AND m.snapshot_id = %s ORDER BY t.{column}",
            (tenant_id, snapshot_id),
        ).fetchall()
        return [_load(model, r[0]) for r in rows]

    def load_inputs(self, tenant_id: str, snapshot_id: str) -> EvaluationInputs:
        """Exactly the persisted members of a closed snapshot; nothing recorded later."""
        try:
            snapshot = self._snapshot(tenant_id, snapshot_id)
            observations: Sequence[Observation] = self._members(
                Observation,
                "observations",
                "snapshot_observations",
                "observation_id",
                tenant_id,
                snapshot_id,
            )
            coverage: Sequence[CoverageCertificate] = self._members(
                CoverageCertificate,
                "coverage_certificates",
                "snapshot_coverage",
                "coverage_id",
                tenant_id,
                snapshot_id,
            )
            links: Sequence[IdentityLink] = self._members(
                IdentityLink,
                "identity_links",
                "snapshot_identity_links",
                "link_id",
                tenant_id,
                snapshot_id,
            )
            profile = self._profile(snapshot.profile_ref)
        finally:
            self.conn.commit()
        return EvaluationInputs(
            snapshot=snapshot,
            profile=profile,
            observations={o.observation_id: o for o in observations},
            coverage={c.coverage_id: c for c in coverage},
            identity_links={link.link_id: link for link in links},
        )

    def journal(self, tenant_id: str) -> list[Observation]:
        rows = self.conn.execute(
            "SELECT document FROM invaria.observations WHERE tenant_id = %s ORDER BY journal_seq",
            (tenant_id,),
        ).fetchall()
        self.conn.commit()
        return [_load(Observation, r[0]) for r in rows]

    def save_evaluation(self, tenant_id: str, evaluation: EvaluationResult) -> bool:
        with self.conn.transaction():
            return self._insert_once(
                "evaluations",
                {"tenant_id": tenant_id, "evaluation_id": evaluation.evaluation_id},
                {
                    "snapshot_id": evaluation.snapshot_id,
                    "result": evaluation.result,
                    "engine_ref": evaluation.versions.engine_ref,
                },
                _doc(evaluation),
            )

    def _evaluation(self, tenant_id: str, evaluation_id: str) -> EvaluationResult:
        row = self.conn.execute(
            "SELECT document FROM invaria.evaluations WHERE tenant_id = %s AND evaluation_id = %s",
            (tenant_id, evaluation_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"evaluation {evaluation_id} not stored")
        return _load(EvaluationResult, row[0])

    def load_evaluation(self, tenant_id: str, evaluation_id: str) -> EvaluationResult:
        try:
            return self._evaluation(tenant_id, evaluation_id)
        finally:
            self.conn.commit()
