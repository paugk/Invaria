"""Append-only PostgreSQL store for evidence, closed snapshots and evaluations.

Writes run as ``invaria_app`` (SELECT + INSERT only). Documents are stored whole and
re-validated with the strict contracts on read. Snapshot membership is persisted, so a
later commit can never change what an existing snapshot contains.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from invaria.contracts.base import parse_contract
from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.evaluation import EvaluationResult, SnapshotRef
from invaria.contracts.identity import IdentityLink
from invaria.contracts.observation import Observation
from invaria.contracts.profile import OperationProfile
from invaria.engine.evaluate import EvaluationInputs

APP_ROLE = "invaria_app"
Conn = psycopg.Connection[tuple[Any, ...]]


class ImmutableConflict(Exception):
    """Same identifier, different content: history is never overwritten."""


class SnapshotInvalid(Exception):
    """A snapshot names members that do not exist or were recorded after known_at."""


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
        inserted = 0
        with self.conn.transaction():
            for o in observations:
                inserted += self._insert_once(
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
                )
        return inserted

    def append_coverage(self, certificates: Iterable[CoverageCertificate]) -> int:
        inserted = 0
        with self.conn.transaction():
            for c in certificates:
                inserted += self._insert_once(
                    "coverage_certificates",
                    {"tenant_id": c.tenant_id, "coverage_id": c.coverage_id},
                    {"source_id": c.source_id, "recorded_at": c.recorded_at},
                    _doc(c),
                )
        return inserted

    def append_identity_links(self, tenant_id: str, links: Iterable[IdentityLink]) -> int:
        inserted = 0
        with self.conn.transaction():
            for link in links:
                inserted += self._insert_once(
                    "identity_links",
                    {"tenant_id": tenant_id, "link_id": link.link_id},
                    {"recorded_at": link.recorded_at},
                    _doc(link),
                )
        return inserted

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
        """Close a snapshot from what is visible and recorded up to known_at, then persist it.

        One REPEATABLE READ transaction: membership is computed from a single consistent
        view, so a concurrent, not yet committed insert cannot be half-included.
        """
        profile = self.load_profile(profile_ref)
        self.conn.commit()
        self.conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        try:
            with self.conn.transaction():

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
        finally:
            self.conn.isolation_level = None
        return snapshot

    # ----------------------------------------------------------------- reads

    def load_profile(self, profile_ref: str) -> OperationProfile:
        row = self.conn.execute(
            "SELECT document FROM invaria.profiles WHERE profile_ref = %s", (profile_ref,)
        ).fetchone()
        if row is None:
            raise KeyError(f"profile {profile_ref} not stored")
        return _load(OperationProfile, row[0])

    def load_snapshot(self, tenant_id: str, snapshot_id: str) -> SnapshotRef:
        row = self.conn.execute(
            "SELECT document FROM invaria.snapshots WHERE tenant_id = %s AND snapshot_id = %s",
            (tenant_id, snapshot_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"snapshot {snapshot_id} not stored")
        return _load(SnapshotRef, row[0])

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
        snapshot = self.load_snapshot(tenant_id, snapshot_id)
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
        self.conn.commit()
        return EvaluationInputs(
            snapshot=snapshot,
            profile=self.load_profile(snapshot.profile_ref),
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

    def load_evaluation(self, tenant_id: str, evaluation_id: str) -> EvaluationResult:
        row = self.conn.execute(
            "SELECT document FROM invaria.evaluations WHERE tenant_id = %s AND evaluation_id = %s",
            (tenant_id, evaluation_id),
        ).fetchone()
        self.conn.commit()
        if row is None:
            raise KeyError(f"evaluation {evaluation_id} not stored")
        return _load(EvaluationResult, row[0])
