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
from collections.abc import Callable, Iterable, Sequence
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
from invaria.contracts.profile import Profile, admitted_mapping_refs, parse_profile
from invaria.engine.dependencies import dependencies
from invaria.engine.evaluate import EvaluationInputs
from invaria.engine.versions import CURRENT_ENGINES
from invaria.query.registry import Pins, ReportRecord

APP_ROLE = "invaria_app"
READER_ROLE = "invaria_reader"
# Reads what a report computation reads and only inserts into the export record.
EXPORTER_ROLE = "invaria_exporter"
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
class IntegrityRows:
    """Raw rows of one consistent read for integrity analytics (``integrity_rows``)."""

    read_at: datetime
    epochs: list[tuple[Any, ...]]
    snapshots: list[tuple[Any, ...]]
    evaluations: list[tuple[Any, ...]]
    publications: list[tuple[Any, ...]]
    attempts: list[tuple[Any, ...]]
    documents: list[EvaluationResult]
    closed_cut: datetime | None


@dataclass(frozen=True)
class OutboxEvent:
    """An internal outbox row. ``operation_ref`` and ``epoch`` are null only for
    ``integrity_report.registered``."""

    event_seq: int
    tenant_id: str
    event_type: str
    operation_ref: str | None
    epoch: int | None
    payload: dict[str, Any]


# Outbox payloads written by the current code pin, in the fact's own transaction, the
# publication they refer to; rows without this marker predate it (historical).
OUTBOX_PAYLOAD_VERSION = 2


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

    def put_profile(self, profile: Profile) -> bool:
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
        # The latest publication when this epoch opens (all of them are of earlier epochs);
        # pinned here, under the tenant lock, so the event never has to guess it later.
        prior = self._prior_publication(tenant_id, operation_ref, epoch)
        self.conn.execute(
            "INSERT INTO invaria.outbox (tenant_id, event_type, operation_ref, epoch, dedup_key, "
            "payload) VALUES (%s, 'scope.invalidated', %s, %s, %s, %s)",
            (
                tenant_id,
                operation_ref,
                epoch,
                f"{tenant_id}|{operation_ref}|invalidated|{epoch}",
                Jsonb(
                    {
                        "profile_ref": profile_ref,
                        **document,
                        "payload_version": OUTBOX_PAYLOAD_VERSION,
                        "prior_publication": prior,
                    }
                ),
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
        profiles: dict[str, Profile] = {}
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
            if evaluation.versions.engine_ref not in CURRENT_ENGINES:
                refusal = (
                    f"evaluated with {evaluation.versions.engine_ref}, which is not a current "
                    "engine; only current engines publish"
                )
            elif epoch != head.epoch:
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
                previous = self._prior_publication(tenant_id, operation_ref, epoch)
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
                        Jsonb(
                            {
                                "evaluation_id": evaluation_id,
                                "result": evaluation.result,
                                "payload_version": OUTBOX_PAYLOAD_VERSION,
                                "previous_publication": previous,
                            }
                        ),
                    ),
                )
            return Publication(outcome, evaluation_id, epoch, detail)

    def _prior_publication(
        self, tenant_id: str, operation_ref: str, epoch: int
    ) -> dict[str, Any] | None:
        """The publication of highest epoch below ``epoch``. Stable once ``epoch`` exists:
        publication needs the current epoch, and the current epoch only grows."""
        row = self.conn.execute(
            "SELECT epoch, evaluation_id FROM invaria.publications WHERE tenant_id = %s "
            "AND operation_ref = %s AND epoch < %s ORDER BY epoch DESC LIMIT 1",
            (tenant_id, operation_ref, epoch),
        ).fetchone()
        return None if row is None else {"epoch": int(row[0]), "evaluation_id": str(row[1])}

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
            OutboxEvent(
                int(r[0]),
                str(r[1]),
                str(r[2]),
                None if r[3] is None else str(r[3]),
                None if r[4] is None else int(r[4]),
                dict(r[5]),
            )
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
                mapping_refs=admitted_mapping_refs(profile.sources),
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

    def _profile(self, profile_ref: str) -> Profile:
        row = self.conn.execute(
            "SELECT document FROM invaria.profiles WHERE profile_ref = %s", (profile_ref,)
        ).fetchone()
        if row is None:
            raise KeyError(f"profile {profile_ref} not stored")
        return parse_profile(json.dumps(row[0]))

    def load_profile(self, profile_ref: str) -> Profile:
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
        """Store a new conclusion: only current engines produce them. Conclusions
        of retired or blocked engines exist only as history (e.g. restored)."""
        if evaluation.versions.engine_ref not in CURRENT_ENGINES:
            raise ValueError(
                f"engine {evaluation.versions.engine_ref} is not current: its conclusions are "
                "history, not new evaluations"
            )
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

    # ------------------------------------------------------- consultative reads

    def evaluations_for(self, tenant_id: str, operation_ref: str) -> list[EvaluationResult]:
        """Every stored evaluation of an operation, oldest knowledge first."""
        rows = self.conn.execute(
            "SELECT e.document FROM invaria.evaluations e JOIN invaria.snapshots s "
            "ON s.tenant_id = e.tenant_id AND s.snapshot_id = e.snapshot_id "
            "WHERE e.tenant_id = %s AND s.operation_ref = %s "
            "ORDER BY s.known_at, s.evaluation_clock, e.evaluation_id",
            (tenant_id, operation_ref),
        ).fetchall()
        self.conn.commit()
        return [_load(EvaluationResult, r[0]) for r in rows]

    def operation_refs(self, tenant_id: str) -> list[str]:
        """Operations with a registered scope or a stored snapshot, sorted."""
        rows = self.conn.execute(
            "SELECT operation_ref FROM invaria.scope_epochs WHERE tenant_id = %s "
            "UNION SELECT operation_ref FROM invaria.snapshots WHERE tenant_id = %s "
            "ORDER BY 1",
            (tenant_id, tenant_id),
        ).fetchall()
        self.conn.commit()
        return [str(r[0]) for r in rows]

    def integrity_rows(self, tenant_id: str) -> IntegrityRows:
        """What operational integrity analytics counts, read in one REPEATABLE
        READ, READ ONLY transaction so that counts and members come from the same state.
        Every query is filtered by tenant. Documents: every published evaluation."""
        self.conn.commit()  # start a fresh transaction for the isolation level
        try:
            self.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")

            def rows(sql: str, params: tuple[Any, ...] = (tenant_id,)) -> list[tuple[Any, ...]]:
                return [tuple(r) for r in self.conn.execute(sql, params).fetchall()]

            read_at = rows("SELECT now()", ())[0][0]
            result = IntegrityRows(
                read_at=read_at,
                epochs=rows(
                    "SELECT operation_ref, epoch, profile_ref, cause FROM invaria.scope_epochs "
                    "WHERE tenant_id = %s ORDER BY operation_ref, epoch"
                ),
                snapshots=rows(
                    "SELECT operation_ref, snapshot_id, profile_ref, known_at, evaluation_clock "
                    "FROM invaria.snapshots WHERE tenant_id = %s ORDER BY operation_ref, "
                    "snapshot_id"
                ),
                evaluations=rows(
                    "SELECT e.evaluation_id, s.operation_ref, e.snapshot_id, s.profile_ref, "
                    "s.valid_at, s.known_at, s.evaluation_clock, e.result, e.engine_ref "
                    "FROM invaria.evaluations e JOIN invaria.snapshots s "
                    "ON s.tenant_id = e.tenant_id AND s.snapshot_id = e.snapshot_id "
                    "WHERE e.tenant_id = %s ORDER BY s.operation_ref, s.known_at, "
                    "s.evaluation_clock, e.evaluation_id"
                ),
                publications=rows(
                    "SELECT operation_ref, epoch, evaluation_id FROM invaria.publications "
                    "WHERE tenant_id = %s ORDER BY operation_ref, epoch"
                ),
                attempts=rows(
                    "SELECT evaluation_id, epoch, outcome FROM invaria.publish_attempts "
                    "WHERE tenant_id = %s ORDER BY evaluation_id, epoch"
                ),
                documents=[
                    _load(EvaluationResult, r[0])
                    for r in rows(
                        "SELECT e.document FROM invaria.evaluations e WHERE e.tenant_id = %s "
                        "AND e.evaluation_id IN (SELECT p.evaluation_id FROM "
                        "invaria.publications p WHERE p.tenant_id = %s) ORDER BY e.evaluation_id",
                        (tenant_id, tenant_id),
                    )
                ],
                closed_cut=rows(
                    "SELECT max(known_at) FROM invaria.knowledge_cuts WHERE tenant_id = %s"
                )[0][0],
            )
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()
        return result

    # ------------------------------------------- registered integrity reports

    _REPORT_COLUMNS = (
        "tenant_id, report_id, report_sha256, report_format, definition, selection_policy, "
        "evidence_horizon, store_read_at, registered_by, document, document_sha256"
    )

    @staticmethod
    def _report_record(row: Sequence[Any]) -> ReportRecord:
        return ReportRecord(
            tenant_id=str(row[0]),
            report_id=str(row[1]),
            report_sha256=str(row[2]),
            report_format=str(row[3]),
            definition=str(row[4]),
            selection_policy=str(row[5]),
            evidence_horizon=row[6],
            store_read_at=row[7],
            registered_by=str(row[8]),
            document=bytes(row[9]),
            document_sha256=str(row[10]),
            registered_at=row[11],
        )

    def _missing_pins(self, tenant_id: str, p: Pins) -> dict[str, int]:
        """How many pinned references of a report this tenant's store lacks (no ids)."""
        ops = [o for o, _ in p.epochs]
        ev_ops = [o for o, _ in p.evaluations]
        pub_ops = [o for o, _, _ in p.publications]
        row = self.conn.execute(
            "SELECT "
            "(SELECT count(*) FROM unnest(%(ops)s::text[]) AS u(op) WHERE NOT EXISTS ("
            " SELECT 1 FROM invaria.scope_epochs x WHERE x.tenant_id = %(t)s AND "
            " x.operation_ref = u.op) AND NOT EXISTS (SELECT 1 FROM invaria.snapshots s "
            " WHERE s.tenant_id = %(t)s AND s.operation_ref = u.op)), "
            "(SELECT count(*) FROM unnest(%(eops)s::text[], %(epochs)s::bigint[]) AS u(op, ep) "
            " WHERE NOT EXISTS (SELECT 1 FROM invaria.scope_epochs x WHERE x.tenant_id = %(t)s "
            " AND x.operation_ref = u.op AND x.epoch = u.ep)), "
            "(SELECT count(*) FROM unnest(%(vops)s::text[], %(vids)s::text[]) AS u(op, id) "
            " WHERE NOT EXISTS (SELECT 1 FROM invaria.evaluations e JOIN invaria.snapshots s "
            " ON s.tenant_id = e.tenant_id AND s.snapshot_id = e.snapshot_id "
            " WHERE e.tenant_id = %(t)s AND e.evaluation_id = u.id AND s.operation_ref = u.op)), "
            "(SELECT count(*) FROM unnest(%(pops)s::text[], %(peps)s::bigint[], "
            " %(pids)s::text[]) AS u(op, ep, id) WHERE NOT EXISTS (SELECT 1 FROM "
            " invaria.publications x WHERE x.tenant_id = %(t)s AND x.operation_ref = u.op AND "
            " x.epoch = u.ep AND x.evaluation_id = u.id))",
            {
                "t": tenant_id,
                "ops": p.operations,
                "eops": ops,
                "epochs": [e for _, e in p.epochs],
                "vops": ev_ops,
                "vids": [i for _, i in p.evaluations],
                "pops": pub_ops,
                "peps": [e for _, e, _ in p.publications],
                "pids": [i for _, _, i in p.publications],
            },
        ).fetchone()
        assert row is not None
        names = ("operations", "epochs", "evaluations", "publications")
        return {n: int(v) for n, v in zip(names, row, strict=True) if v}

    def missing_pins(self, tenant_id: str, p: Pins) -> dict[str, int]:
        try:
            return self._missing_pins(tenant_id, p)
        finally:
            self.conn.commit()

    def register_integrity_report(
        self, record: ReportRecord, p: Pins
    ) -> tuple[ReportRecord, bool, dict[str, int]]:
        """Insert a report the service computed, once. The pinned references are checked in
        the same transaction (append-only, so they cannot disappear afterwards); if any is
        missing nothing is written. Same id with other bytes is an ``ImmutableConflict``."""
        with self.conn.transaction():
            # Same lock order as every outbox producer: the tenant lock first.
            self._lock_tenant(record.tenant_id)
            missing = self._missing_pins(record.tenant_id, p)
            if missing:
                return record, False, missing
            inserted = self.conn.execute(
                f"INSERT INTO invaria.integrity_reports ({self._REPORT_COLUMNS}) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, report_id) DO NOTHING RETURNING 1",
                (
                    record.tenant_id,
                    record.report_id,
                    record.report_sha256,
                    record.report_format,
                    record.definition,
                    record.selection_policy,
                    record.evidence_horizon,
                    record.store_read_at,
                    record.registered_by,
                    record.document,
                    record.document_sha256,
                ),
            ).fetchone()
            if inserted is not None:  # a new resource only: a repeated registration is silent
                self.conn.execute(
                    "INSERT INTO invaria.outbox (tenant_id, event_type, operation_ref, epoch, "
                    "dedup_key, payload) VALUES (%s, 'integrity_report.registered', NULL, NULL, "
                    "%s, %s)",
                    (
                        record.tenant_id,
                        f"{record.tenant_id}|report|registered|{record.report_id}",
                        Jsonb(
                            {
                                "report_id": record.report_id,
                                "report_sha256": record.report_sha256,
                                "payload_version": OUTBOX_PAYLOAD_VERSION,
                            }
                        ),
                    ),
                )
            stored = self._integrity_report(record.tenant_id, record.report_id)
        if stored is None or stored.document != record.document:
            raise ImmutableConflict(f"integrity report {record.report_id} exists with other bytes")
        return stored, inserted is not None, {}

    def _integrity_report(self, tenant_id: str, report_id: str) -> ReportRecord | None:
        row = self.conn.execute(
            f"SELECT {self._REPORT_COLUMNS}, registered_at FROM invaria.integrity_reports "
            "WHERE tenant_id = %s AND report_id = %s",
            (tenant_id, report_id),
        ).fetchone()
        return None if row is None else self._report_record(row)

    def load_integrity_report(self, tenant_id: str, report_id: str) -> ReportRecord | None:
        try:
            return self._integrity_report(tenant_id, report_id)
        finally:
            self.conn.commit()

    def integrity_report_index(
        self, tenant_id: str, after: tuple[datetime, str] | None, limit: int
    ) -> list[tuple[Any, ...]]:
        """Registry columns (never the documents), newest registration first, keyset after
        ``after``. Each call is its own statement: no snapshot spans pages."""
        where = "tenant_id = %s"
        params: list[Any] = [tenant_id]
        if after is not None:
            where += " AND (registered_at, report_id) < (%s, %s)"
            params += [after[0], after[1]]
        rows = self.conn.execute(
            "SELECT report_id, report_sha256, report_format, definition, selection_policy, "
            "evidence_horizon, store_read_at, registered_at FROM invaria.integrity_reports "
            f"WHERE {where} ORDER BY registered_at DESC, report_id DESC LIMIT %s",
            [*params, limit],
        ).fetchall()
        self.conn.commit()
        return [tuple(r) for r in rows]

    # ------------------------------------------- controlled disclosure

    # ------------------------------------------------------------ exports

    def record_export_attempt(
        self,
        tenant_id: str,
        principal_id: str,
        policy_refs: list[str],
        report_sha256: str,
        content_sha256: str,
        content_bytes: int,
    ) -> tuple[str, datetime]:
        """Record that an export of this exact content is about to be written; the store
        chooses export_id and attempted_at. No path, file name or recipient is recorded."""
        with self.conn.transaction():
            row = self.conn.execute(
                "INSERT INTO invaria.export_attempts (tenant_id, principal_id, kind, "
                "file_format, policy_refs, report_sha256, content_sha256, content_bytes) "
                "VALUES (%s, %s, 'integrity_report_file', 'integrity-report-file@1', %s, %s, "
                "%s, %s) RETURNING export_id, attempted_at",
                (
                    tenant_id,
                    principal_id,
                    policy_refs,
                    report_sha256,
                    content_sha256,
                    content_bytes,
                ),
            ).fetchone()
        assert row is not None
        return str(row[0]), row[1]

    def record_export_outcome(
        self, tenant_id: str, export_id: str, outcome: str, failure: str | None
    ) -> datetime:
        """Record the one outcome of an attempt (a new row; a second outcome is refused by
        the primary key). Returns the store clock of the record."""
        with self.conn.transaction():
            row = self.conn.execute(
                "INSERT INTO invaria.export_outcomes (export_id, tenant_id, outcome, failure) "
                "VALUES (%s, %s, %s, %s) RETURNING recorded_at",
                (export_id, tenant_id, outcome, failure),
            ).fetchone()
        assert row is not None
        return row[0]  # type: ignore[no-any-return]

    def export_records(self, tenant_id: str) -> list[dict[str, Any]]:
        """This tenant's export attempts with their outcome (None: no outcome recorded),
        in attempt order. For the reader role and for tests."""
        with self.conn.transaction():
            rows = self.conn.execute(
                "SELECT a.export_id, a.principal_id, a.kind, a.file_format, a.policy_refs, "
                "a.report_sha256, a.content_sha256, a.content_bytes, a.attempted_at, "
                "o.outcome, o.failure, o.recorded_at FROM invaria.export_attempts a "
                "LEFT JOIN invaria.export_outcomes o ON o.export_id = a.export_id "
                "WHERE a.tenant_id = %s ORDER BY a.attempted_at, a.export_id",
                (tenant_id,),
            ).fetchall()
        names = (
            "export_id principal_id kind file_format policy_refs report_sha256 content_sha256 "
            "content_bytes attempted_at outcome failure recorded_at"
        ).split()
        return [dict(zip(names, r, strict=True)) for r in rows]

    def issue_disclosure(
        self,
        tenant_id: str,
        recipient_principal: str,
        policy_ref: str,
        view_format: str,
        report_id: str,
        issued_by: str,
    ) -> tuple[str, datetime]:
        """Record one issuance; the store chooses view_id and issued_at. No domain event and
        no outbox row: the issuance row itself is the trace."""
        with self.conn.transaction():
            row = self.conn.execute(
                "INSERT INTO invaria.disclosure_issuances (tenant_id, recipient_principal, "
                "policy_ref, view_format, report_id, issued_by) VALUES (%s, %s, %s, %s, %s, %s) "
                "RETURNING view_id, issued_at",
                (tenant_id, recipient_principal, policy_ref, view_format, report_id, issued_by),
            ).fetchone()
        assert row is not None
        return str(row[0]), row[1]

    def revoke_disclosure(
        self, tenant_id: str, view_id: str, revoked_by: str
    ) -> tuple[bool, datetime] | None:
        """(revoked by this call, revocation time), or None when the tenant has no such
        issuance. A repeated revocation keeps the first row."""
        with self.conn.transaction():
            exists = self.conn.execute(
                "SELECT 1 FROM invaria.disclosure_issuances WHERE tenant_id = %s AND view_id = %s",
                (tenant_id, view_id),
            ).fetchone()
            if exists is None:
                return None
            row = self.conn.execute(
                "INSERT INTO invaria.disclosure_revocations (view_id, tenant_id, revoked_by) "
                "VALUES (%s, %s, %s) ON CONFLICT (view_id) DO NOTHING RETURNING revoked_at",
                (view_id, tenant_id, revoked_by),
            ).fetchone()
            if row is not None:
                return True, row[0]
            first = self.conn.execute(
                "SELECT revoked_at FROM invaria.disclosure_revocations WHERE view_id = %s",
                (view_id,),
            ).fetchone()
        assert first is not None
        return False, first[0]

    def disclosure_for(
        self, tenant_id: str, recipient_principal: str, view_id: str
    ) -> tuple[str, str, ReportRecord] | None:
        """(policy_ref, view_format, report) of an issuance of this tenant to this recipient
        that is not revoked, read in ONE statement: that statement's snapshot is the
        consistency point. A revocation committed after it does not withdraw a response
        already being built; the next read sees it. Absent, foreign and revoked are the
        same None."""
        columns = ", ".join(f"r.{c.strip()}" for c in self._REPORT_COLUMNS.split(","))
        try:
            row = self.conn.execute(
                f"SELECT i.policy_ref, i.view_format, {columns}, r.registered_at "
                "FROM invaria.disclosure_issuances i JOIN invaria.integrity_reports r "
                "ON r.tenant_id = i.tenant_id AND r.report_id = i.report_id "
                "WHERE i.tenant_id = %s AND i.recipient_principal = %s AND i.view_id = %s "
                "AND NOT EXISTS (SELECT 1 FROM invaria.disclosure_revocations v "
                "WHERE v.view_id = i.view_id)",
                (tenant_id, recipient_principal, view_id),
            ).fetchone()
        finally:
            self.conn.commit()
        if row is None:
            return None
        return str(row[0]), str(row[1]), self._report_record(row[2:])

    def disclosures_of(
        self, tenant_id: str, recipient_principal: str, limit: int
    ) -> list[tuple[str, str]]:
        """(view_id, policy_ref) of the recipient's issuances not revoked, newest first."""
        try:
            rows = self.conn.execute(
                "SELECT i.view_id, i.policy_ref FROM invaria.disclosure_issuances i "
                "WHERE i.tenant_id = %s AND i.recipient_principal = %s AND NOT EXISTS ("
                "SELECT 1 FROM invaria.disclosure_revocations v WHERE v.view_id = i.view_id) "
                "ORDER BY i.issued_at DESC, i.view_id LIMIT %s",
                (tenant_id, recipient_principal, limit),
            ).fetchall()
        finally:
            self.conn.commit()
        return [(str(r[0]), str(r[1])) for r in rows]

    # ------------------------------------------------- event feed by cursor

    def feed_page(
        self,
        tenant_id: str,
        decide: Callable[[str, bytes, int], tuple[int, int, list[str], int] | None],
    ) -> tuple[str, bytes, int, list[tuple[int, datetime, str, bytes, str]]]:
        """One consistent read (REPEATABLE READ, READ ONLY) of the feed: the current
        generation and its key, the head (highest committed tenant_seq of the tenant), and
        the readable events of the window ``decide`` returns as (after, upper, types,
        limit) given (generation, key, head); None reads no events. tenant_seq has no gaps
        and commits in order, so no number up to the head can appear later."""
        self.conn.commit()
        try:
            self.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            gen = self.conn.execute(
                "SELECT generation_id, cursor_key FROM invaria.feed_generations "
                "ORDER BY ordinal DESC LIMIT 1"
            ).fetchone()
            if gen is None:
                raise KeyError("no feed generation")
            head_row = self.conn.execute(
                "SELECT coalesce(max(tenant_seq), 0) FROM invaria.domain_events "
                "WHERE tenant_id = %s",
                (tenant_id,),
            ).fetchone()
            head = int(head_row[0]) if head_row else 0
            generation, key = str(gen[0]), bytes(gen[1])
            window = decide(generation, key, head)
            rows: list[tuple[int, datetime, str, bytes, str]] = []
            if window is not None:
                after, upper, types, limit = window
                rows = [
                    (int(r[0]), r[1], str(r[2]), bytes(r[3]), str(r[4]))
                    for r in self.conn.execute(
                        "SELECT tenant_seq, derived_at, payload_sha256, payload, event_type "
                        "FROM invaria.domain_events WHERE tenant_id = %s AND tenant_seq > %s "
                        "AND tenant_seq <= %s AND event_type = ANY(%s) ORDER BY tenant_seq "
                        "LIMIT %s",
                        (tenant_id, after, upper, types, limit),
                    ).fetchall()
                ]
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()
        return generation, key, head, rows

    def epoch_log(
        self, tenant_id: str, operation_ref: str
    ) -> list[tuple[int, str, datetime | None, dict[str, Any]]]:
        """Revision epochs with their knowledge floor (latest recorded_at of the evidence)."""
        rows = self.conn.execute(
            "SELECT epoch, cause, knowledge_floor, document FROM invaria.scope_epochs "
            "WHERE tenant_id = %s AND operation_ref = %s ORDER BY epoch",
            (tenant_id, operation_ref),
        ).fetchall()
        self.conn.commit()
        return [(int(r[0]), str(r[1]), r[2], dict(r[3])) for r in rows]

    def load_observation(self, tenant_id: str, observation_id: str) -> Observation:
        row = self.conn.execute(
            "SELECT document FROM invaria.observations "
            "WHERE tenant_id = %s AND observation_id = %s",
            (tenant_id, observation_id),
        ).fetchone()
        self.conn.commit()
        if row is None:
            raise KeyError(f"observation {observation_id} not stored")
        return _load(Observation, row[0])

    def load_coverage(self, tenant_id: str, coverage_id: str) -> CoverageCertificate:
        row = self.conn.execute(
            "SELECT document FROM invaria.coverage_certificates "
            "WHERE tenant_id = %s AND coverage_id = %s",
            (tenant_id, coverage_id),
        ).fetchone()
        self.conn.commit()
        if row is None:
            raise KeyError(f"coverage {coverage_id} not stored")
        return _load(CoverageCertificate, row[0])
