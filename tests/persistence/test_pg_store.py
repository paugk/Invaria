"""Persistence against a real PostgreSQL: append-only, closed snapshots, bitemporal rebuild."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors

from invaria.contracts.observation import Observation, TokenMovementPayload, is_muxed
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.evaluate import evaluate
from invaria.ingest import ImportContext, import_csv
from invaria.persistence.dsn import with_database
from invaria.persistence.migrate import MigrationTampered, available_migrations, migrate
from invaria.persistence.provision import prepare_for_migrator
from invaria.persistence.store import ImmutableConflict, PgStore, SnapshotInvalid
from invaria.vertical_testnet import run_testnet_vertical

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")


def _dsn_for(database: str) -> str:
    assert ADMIN_DSN is not None
    return with_database(ADMIN_DSN, database)


@pytest.fixture
def database() -> Iterator[str]:
    """A fresh, migrated database per test; dropped afterwards."""
    if not ADMIN_DSN:
        pytest.skip("set INVARIA_TEST_DATABASE_URL to an admin PostgreSQL URL to run DB tests")
    name = f"invaria_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    try:
        with psycopg.connect(_dsn_for(name)) as conn:
            prepare_for_migrator(conn)
            migrate(conn)
        yield _dsn_for(name)
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def connect(database: str) -> Iterator[Any]:
    opened: list[psycopg.Connection[Any]] = []

    def factory() -> psycopg.Connection[Any]:
        conn = psycopg.connect(database)
        opened.append(conn)
        return conn

    yield factory
    for conn in opened:
        conn.close()


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CORPUS_DIR = FIXTURES / "corpus/subscription-synthetic"
TENANT = "tenant-synthetic-demo"
MAIN_SCENARIOS = ("K1", "K2", "K3", "K2-historical")


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


def load_timeline(
    store: PgStore, corpus: Corpus, timeline: str, coverage_ids: set[str] | None = None
) -> None:
    store.put_profile(corpus.profile)
    certificates = [
        c for c in corpus.coverage.values() if coverage_ids is None or c.coverage_id in coverage_ids
    ]
    store.append_coverage(certificates)
    store.append_identity_links(TENANT, corpus.identity_links.values())
    store.append_observations(corpus.journals[timeline].values())


def main_coverage(corpus: Corpus) -> set[str]:
    return {c for s in MAIN_SCENARIOS for c in corpus.scenarios[s].snapshot.coverage_ids}


# ----------------------------------------------------------------- migrations


def test_migrations_are_idempotent_and_tamper_evident(connect: Any) -> None:
    conn = connect()
    assert migrate(conn) == []
    (version, sql), *_ = available_migrations()
    with pytest.raises(MigrationTampered):
        migrate(conn, [(version, sql + b"\n-- edited after being applied\n")])


ROLES = ("invaria_owner", "invaria_app", "invaria_reader")
TABLE_ACL = ["invaria_app=ar", "invaria_owner=arwdDxtm", "invaria_reader=r"]
SEQUENCE_ACL = ["invaria_app=U", "invaria_owner=rwU"]


def test_final_privileges_do_not_depend_on_the_migrator(database: str) -> None:
    """The state a superuser migrator leaves (pinned 17.11 container, 2026-10-08), whether
    or not the migrator is one: provisioning only makes the migrator a member of the roles;
    it grants nothing to ``invaria_app``, ``invaria_reader`` or PUBLIC."""
    with psycopg.connect(database, autocommit=True) as conn:
        assert prepare_for_migrator(conn) == []  # idempotent (and a no-op for a superuser)
        schema = conn.execute(
            "SELECT nspowner::regrole::text, nspacl::text[] FROM pg_namespace "
            "WHERE nspname = 'invaria'"
        ).fetchone()
        assert schema is not None
        assert schema[0] == "invaria_owner"
        assert sorted(schema[1]) == [
            "invaria_app=U/invaria_owner",
            "invaria_owner=UC/invaria_owner",
            "invaria_reader=U/invaria_owner",
        ]
        relations = conn.execute(
            "SELECT relname, relkind::text, relowner::regrole::text, relacl::text[] "
            "FROM pg_class WHERE relnamespace = 'invaria'::regnamespace"
        ).fetchall()
        assert {owner for _, _, owner, _ in relations} == {"invaria_owner"}
        acl = {
            (kind, name): sorted(a.removesuffix("/invaria_owner") for a in (acl or []))
            for name, kind, _, acl in relations
        }
        tables = sorted(name for kind, name in acl if kind == "r")
        assert len(tables) == 16 and "schema_migrations" in tables
        for name in tables:
            expected = (
                ["invaria_app=r", "invaria_owner=arwdDxtm"]
                if name == "schema_migrations"
                else TABLE_ACL
            )
            assert acl[("r", name)] == expected, name
        sequences = [name for kind, name in acl if kind == "S"]
        assert len(sequences) == 4
        assert all(acl[("S", name)] == SEQUENCE_ACL for name in sequences)
        assert all(not a for (kind, _), a in acl.items() if kind == "i")  # indexes: no ACL
        # The roles keep their attributes and are members of nothing.
        attrs = conn.execute(
            "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, "
            "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = ANY(%s)",
            (list(ROLES),),
        ).fetchall()
        assert sorted(attrs) == sorted(
            (r, False, True, False, False, False, False, False) for r in ROLES
        )
        assert conn.execute(
            "SELECT count(*) FROM pg_auth_members WHERE member IN "
            "(SELECT oid FROM pg_roles WHERE rolname = ANY(%s))",
            (list(ROLES),),
        ).fetchone() == (0,)


# ------------------------------------------------- scenario parity via the database


@pytest.mark.parametrize(
    "scenario_id",
    [
        "K1",
        "K2",
        "K3",
        "K2-historical",
        "RETRACTION",
        "DUPLICATE-DELIVERY",
        "SOURCE-CONFLICT",
        "AMBIGUOUS-IDENTITY",
        "AMBIGUOUS-SCALE",
    ],
)
def test_scenario_round_trip_through_database(
    connect: Any, corpus: Corpus, scenario_id: str
) -> None:
    scenario = corpus.scenarios[scenario_id]
    store = PgStore(connect())
    load_timeline(store, corpus, scenario.timeline_id, set(scenario.snapshot.coverage_ids))
    assert store.create_snapshot(scenario.snapshot) is True
    assert store.create_snapshot(scenario.snapshot) is False  # idempotent
    from_db = evaluate(store.load_inputs(TENANT, scenario.snapshot.snapshot_id))
    from_files = evaluate(corpus.inputs_for(scenario_id))
    assert from_db == from_files
    assert from_db.result.result == scenario.expected.result
    assert store.save_evaluation(TENANT, from_db.result) is True
    assert store.save_evaluation(TENANT, from_db.result) is False
    assert store.load_evaluation(TENANT, from_db.result.evaluation_id) == from_db.result


# ------------------------------------------------------------ bitemporal rebuild


def test_build_snapshot_as_known_excludes_later_correction(connect: Any, corpus: Corpus) -> None:
    store = PgStore(connect())
    load_timeline(store, corpus, "main", main_coverage(corpus))
    k2, k3 = corpus.scenarios["K2"].snapshot, corpus.scenarios["K3"].snapshot
    built_k2 = store.build_snapshot(
        snapshot_id="db-K2",
        tenant_id=TENANT,
        operation_ref="SUB-0001",
        profile_ref=k2.profile_ref,
        valid_at=k2.valid_at,
        known_at=k2.known_at,
        evaluation_clock=k3.known_at,
    )
    built_k3 = store.build_snapshot(
        snapshot_id="db-K3",
        tenant_id=TENANT,
        operation_ref="SUB-0001",
        profile_ref=k3.profile_ref,
        valid_at=k3.valid_at,
        known_at=k3.known_at,
        evaluation_clock=k3.known_at,
    )
    assert built_k2.observation_ids == k2.observation_ids  # obs-B2 stored but not yet known
    assert "obs-B2" in built_k3.observation_ids and "obs-B1" in built_k3.observation_ids
    assert evaluate(store.load_inputs(TENANT, "db-K2")).result.result == "MATCH"
    k3_eval = evaluate(store.load_inputs(TENANT, "db-K3")).result
    assert k3_eval.result == "BREAK"


def test_late_commit_does_not_change_existing_snapshot(connect: Any, corpus: Corpus) -> None:
    writer, builder = PgStore(connect()), PgStore(connect())
    load_timeline(builder, corpus, "main", main_coverage(corpus))
    late = corpus.journals["main"]["obs-R1"].model_dump(mode="json")
    late["observation_id"] = "obs-late-commit"
    late_obs = Observation.model_validate_json(json.dumps(late))
    k2 = corpus.scenarios["K2"].snapshot
    with writer.conn.transaction():  # uncommitted while the snapshot is closed
        writer.conn.execute("SELECT 1")
        writer._insert_once(
            "observations",
            {"tenant_id": TENANT, "observation_id": "obs-late-commit"},
            {
                "source_id": "ta-synthetic",
                "record_key": "SUB-0001",
                "revision": 1,
                "fact_type": "units_registered",
                "kind": "assertion",
                "operation_ref": "SUB-0001",
                "valid_time": late_obs.valid_time,
                "recorded_at": late_obs.recorded_at,
                "raw_sha256": late_obs.provenance.raw_sha256,
                "mapping_ref": late_obs.provenance.mapping_ref,
                "supersedes": None,
            },
            late_obs.model_dump(mode="json"),
        )
        snapshot = builder.build_snapshot(
            snapshot_id="db-K2-closed",
            tenant_id=TENANT,
            operation_ref="SUB-0001",
            profile_ref=k2.profile_ref,
            valid_at=k2.valid_at,
            known_at=k2.known_at,
            evaluation_clock=k2.known_at,
        )
    assert "obs-late-commit" not in snapshot.observation_ids
    reloaded = builder.load_inputs(TENANT, "db-K2-closed")
    assert "obs-late-commit" not in reloaded.observations
    assert "obs-late-commit" in {o.observation_id for o in builder.journal(TENANT)}


# --------------------------------------------------------------- append-only


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE invaria.observations SET recorded_at = now()",
        "DELETE FROM invaria.observations",
        "TRUNCATE invaria.observations",
        "UPDATE invaria.evaluations SET result = 'MATCH'",
        "DELETE FROM invaria.snapshot_observations",
        "INSERT INTO invaria.schema_migrations (version, sha256) VALUES ('x', repeat('0', 64))",
    ],
)
def test_application_role_cannot_rewrite_history(
    connect: Any, corpus: Corpus, statement: str
) -> None:
    store = PgStore(connect())
    load_timeline(store, corpus, "main", main_coverage(corpus))
    with pytest.raises(errors.InsufficientPrivilege):
        store.conn.execute(statement)
    store.conn.rollback()
    assert len(store.journal(TENANT)) == len(corpus.journals["main"])


def test_same_id_with_other_content_is_a_conflict(connect: Any, corpus: Corpus) -> None:
    store = PgStore(connect())
    load_timeline(store, corpus, "main", main_coverage(corpus))
    b1 = corpus.journals["main"]["obs-B1"]
    assert store.append_observations([b1]) == 0  # identical redelivery: nothing appended
    altered = b1.model_dump(mode="json")
    altered["payload"]["amount"]["atoms"] = "1"
    with pytest.raises(ImmutableConflict):
        store.append_observations([Observation.model_validate_json(json.dumps(altered))])
    profile = corpus.profile.model_dump(mode="json")
    profile["disclaimer"] = "edited"
    with pytest.raises(ImmutableConflict):
        store.put_profile(type(corpus.profile).model_validate_json(json.dumps(profile)))
    stored = store.journal(TENANT)
    assert next(o for o in stored if o.observation_id == "obs-B1") == b1


def test_snapshot_members_must_exist_and_be_known(connect: Any, corpus: Corpus) -> None:
    store = PgStore(connect())
    load_timeline(store, corpus, "main", main_coverage(corpus))
    k2 = corpus.scenarios["K2"].snapshot
    unknown = k2.model_copy(
        update={
            "snapshot_id": "bad-1",
            "observation_ids": sorted([*k2.observation_ids, "obs-ghost"]),
        }
    )
    with pytest.raises(SnapshotInvalid, match="missing"):
        store.create_snapshot(unknown)
    too_late = k2.model_copy(
        update={"snapshot_id": "bad-2", "observation_ids": sorted([*k2.observation_ids, "obs-B2"])}
    )
    with pytest.raises(SnapshotInvalid, match="after known_at"):
        store.create_snapshot(too_late)


def test_corrupted_document_is_rejected_on_read(
    connect: Any, database: str, corpus: Corpus
) -> None:
    store = PgStore(connect())
    load_timeline(store, corpus, "main", main_coverage(corpus))
    store.create_snapshot(corpus.scenarios["K2"].snapshot)
    with psycopg.connect(database, autocommit=True) as owner:  # bypass the app role
        owner.execute(
            "UPDATE invaria.observations SET document = jsonb_set(document, "
            "'{payload,amount,atoms}', '1000.5'::jsonb) WHERE observation_id = 'obs-B1'"
        )
    with pytest.raises(ValueError):
        store.load_inputs(TENANT, "snap-K2")


# ------------------------------------------------------- ingestion and testnet


def test_csv_import_against_database_journal(connect: Any, corpus: Corpus) -> None:
    store = PgStore(connect())
    store.put_profile(corpus.profile)
    store.append_observations([corpus.journals["main"]["obs-B1"]])
    name = "bank_correction_2026-10-02.csv"
    outcome = import_csv(
        (CORPUS_DIR / "raw" / name).read_bytes(),
        corpus.mappings["bank-csv-synthetic@1.0.0"],
        ImportContext(
            tenant_id=TENANT,
            instrument_id=corpus.profile.instrument.instrument_id,
            raw_path=f"raw/{name}",
            recorded_at=datetime.fromisoformat("2026-10-02T09:00:00+00:00"),
            coverage=None,
        ),
        store.journal(TENANT),
    )
    (correction,) = outcome.integration.appended
    assert outcome.integration.outcomes[0].decision == "REVISION"
    assert correction.supersedes == "obs-B1"
    assert store.append_observations([correction]) == 1
    assert [o.observation_id for o in store.journal(TENANT)] == [
        "obs-B1",
        correction.observation_id,
    ]


def test_testnet_evaluation_from_database(connect: Any) -> None:
    runs = run_testnet_vertical(
        FIXTURES / "corpus/subscription-testnet-1.4.0",
        FIXTURES / "stellar",
        CORPUS_DIR / "mappings",
    )
    linked = next(r for r in runs if r.scenario.scenario_id == "TN-LINKED")
    inputs = linked.inputs
    store = PgStore(connect())
    tenant = inputs.snapshot.tenant_id
    store.put_profile(inputs.profile)
    store.append_observations(inputs.observations.values())
    store.append_coverage(inputs.coverage.values())
    store.append_identity_links(tenant, inputs.identity_links.values())
    store.create_snapshot(inputs.snapshot)
    from_db = evaluate(store.load_inputs(tenant, inputs.snapshot.snapshot_id))
    assert from_db == linked.evaluation and from_db.result.result == "MATCH"


def test_muxed_ids_and_memos_round_trip_exactly(connect: Any) -> None:
    """The u64 sub-account ids (0, 2^53 and 2^64 - 1) and the
    memo survive jsonb as the exact text written; nothing is turned into a number. A
    sub-account id 0 is a key holding "0"; no sub-account is the key's absence, read back as
    None, never as 0. 2^53 is the first integer a binary double cannot tell from its
    neighbour (2^53 + 1 rounds to it): it comes from a DERIVED copy of the to-zero payment,
    since the synthetic recordings do not carry it."""
    import tempfile

    from invaria.contracts.base import parse_contract
    from invaria.contracts.chain import ChainTarget
    from invaria.stellar.adapter import ingest_horizon_payments, ingest_sac_events
    from invaria.stellar.http import ReplayClient
    from invaria.stellar.sources import Horizon, Rpc, verify_network
    from invaria.stellar.store import IngestStore

    synthetic = FIXTURES / "stellar/synthetic/muxed"
    target = parse_contract(ChainTarget, (synthetic / "target.json").read_text("utf-8"))
    client = ReplayClient(synthetic / "recordings")
    horizon = Horizon("https://horizon.synthetic.invalid", client)
    rpc = Rpc("https://rpc.synthetic.invalid", client)
    check = verify_network(horizon, rpc, "stellar:testnet")
    recorded_at = datetime.fromisoformat("2026-10-07T16:00:00+00:00")
    with tempfile.TemporaryDirectory() as tmp:
        ingest = IngestStore(Path(tmp))
        ingest_horizon_payments(
            target,
            horizon,
            ingest,
            start_ledger=90000500,
            end_ledger=90000530,
            history=(check.horizon_elder_ledger, check.horizon_latest_ledger),
            recorded_at=recorded_at,
            page_limit=3,
        )
        ingest_sac_events(
            target,
            rpc,
            horizon,
            ingest,
            start_ledger=90000500,
            end_ledger=90000530,
            recorded_at=recorded_at,
        )
        observations = ingest.observations()
    zero = next(
        o
        for o in observations
        if isinstance(o.payload, TokenMovementPayload) and o.payload.to_muxed_id == "0"
    )
    assert isinstance(zero.payload, TokenMovementPayload)
    beyond_double = zero.model_copy(
        update={
            "observation_id": "obs-derived-u64-2-53",
            "source": zero.source.model_copy(update={"record_key": "derived-2-53:0:0"}),
            "payload": zero.payload.model_copy(update={"to_muxed_id": str(2**53)}),
        }
    )
    observations = [*observations, beyond_double]
    store = PgStore(connect())
    store.append_observations(observations)
    for o in observations:
        assert store.load_observation(o.tenant_id, o.observation_id) == o
    loaded = {
        o.observation_id: store.load_observation(o.tenant_id, o.observation_id).payload
        for o in observations
    }
    plain = [
        i
        for i, p in loaded.items()
        if isinstance(p, TokenMovementPayload) and p.to_muxed_id is None
    ]
    assert plain  # payments to a base account: no sub-account, None, not "0"
    absent = {
        row[0]
        for row in store.conn.execute(
            "SELECT observation_id FROM invaria.observations "
            "WHERE document->>'fact_type' = 'token_movement' "
            "AND NOT document->'payload' ? 'to_muxed_id'"
        ).fetchall()
    }
    assert set(plain) <= absent  # absence is the key's absence in jsonb, never 0 or null
    zero_payload = loaded[zero.observation_id]
    assert isinstance(zero_payload, TokenMovementPayload)
    assert zero_payload.to_muxed_id == "0" and is_muxed(zero_payload)
    raw = {
        row[0]
        for row in store.conn.execute(
            "SELECT document->'payload'->>'to_muxed_id' FROM invaria.observations "
            "WHERE document->'payload' ? 'to_muxed_id'"
        ).fetchall()
    }
    assert {"0", "9007199254740992", "18446744073709551615", "42"} <= raw
    assert float(2**53 + 1) == float(2**53)  # what a JSON number would have lost
    kinds = {
        row[0]
        for row in store.conn.execute(
            "SELECT jsonb_typeof(document->'payload'->'to_muxed_id') FROM invaria.observations "
            "WHERE document->'payload' ? 'to_muxed_id'"
        ).fetchall()
    }
    assert kinds == {"string"}
    store.conn.rollback()
