"""Consultative layer against a real PostgreSQL, through the read-only role and over MCP."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from psycopg import errors
from psycopg.types.json import Jsonb

from invaria.contracts.observation import Observation
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.evaluate import replay
from invaria.mcp_server import build_server
from invaria.persistence.backup import reproduce_all
from invaria.persistence.dsn import with_database
from invaria.persistence.migrate import migrate
from invaria.persistence.provision import prepare_for_migrator
from invaria.persistence.store import READER_ROLE, PgStore
from invaria.persistence.worker import Clocks, reevaluate
from invaria.query.models import AccessProfile
from invaria.query.service import AuditLog, QueryError, QueryService

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")
CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"
TENANT = "tenant-synthetic-demo"
OP = "SUB-0001"


def _dsn_for(database: str) -> str:
    assert ADMIN_DSN is not None
    return with_database(ADMIN_DSN, database)


def insert_historical_evaluation(conn: psycopg.Connection[Any], evaluation: Any) -> None:
    """A conclusion recorded when its engine was current, as a restore brings it back.
    ``save_evaluation`` refuses engines that are not current."""
    conn.execute(
        "INSERT INTO invaria.evaluations (tenant_id, evaluation_id, snapshot_id, result, "
        "engine_ref, document) VALUES (%s, %s, %s, %s, %s, %s)",
        (
            TENANT,
            evaluation.evaluation_id,
            evaluation.snapshot_id,
            evaluation.result,
            evaluation.versions.engine_ref,
            Jsonb(evaluation.model_dump(mode="json")),
        ),
    )
    conn.commit()


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


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


def clocks(corpus: Corpus, scenario_id: str) -> Clocks:
    s = corpus.scenarios[scenario_id].snapshot
    return Clocks(s.valid_at, s.known_at, s.evaluation_clock)


def feed(store: PgStore, corpus: Corpus, upto: datetime) -> None:
    coverage_ids = {
        c for s in ("K1", "K2", "K3") for c in corpus.scenarios[s].snapshot.coverage_ids
    }
    store.append_identity_links(
        TENANT, [k for k in corpus.identity_links.values() if k.recorded_at <= upto]
    )
    store.append_coverage(
        c
        for c in corpus.coverage.values()
        if c.coverage_id in coverage_ids and c.recorded_at <= upto
    )
    store.append_observations(o for o in corpus.journals["main"].values() if o.recorded_at <= upto)


@dataclass
class World:
    writer: PgStore
    reader: PgStore
    k1: str
    k2: str
    k3: str
    dsn: str


@pytest.fixture
def world(database: str, corpus: Corpus) -> Iterator[World]:
    writer = PgStore(psycopg.connect(database))
    writer.put_profile(corpus.profile)
    writer.register_scope(TENANT, OP, corpus.profile.profile_ref)
    ids = []
    for scenario in ("K1", "K2", "K3"):
        feed(writer, corpus, clocks(corpus, scenario).known_at)
        prepared, publication = reevaluate(writer, TENANT, OP, clocks(corpus, scenario))
        assert publication.outcome == "PUBLISHED"
        ids.append(prepared.evaluation.result.evaluation_id)
    reader = PgStore(psycopg.connect(database), role=READER_ROLE)
    yield World(writer, reader, ids[0], ids[1], ids[2], database)
    writer.conn.close()
    reader.conn.close()


def access(**overrides: Any) -> AccessProfile:
    return AccessProfile.model_validate_json(
        json.dumps(
            {
                "schema_version": "1.0",
                "principal_id": "analyst-1",
                "tenant_id": TENANT,
                "scopes": ["operations:read", "evidence:read"],
                "max_items": 50,
                **overrides,
            }
        )
    )


def service(world: World, **overrides: Any) -> QueryService:
    return QueryService(world.reader, access(**overrides))


CASH = "subscription.cash_vs_order"


# ------------------------------------------------------------------- answers


def test_trace_shows_current_break_history_legs_and_epochs(world: World) -> None:
    trace = service(world).trace_operation(OP)
    assert trace.watched and trace.current is not None
    assert (trace.current.evaluation_id, trace.current.result) == (world.k3, "BREAK")
    assert trace.current.currency.currency == "current"
    assert [h.result for h in trace.history] == ["UNKNOWN", "MATCH", "BREAK"]
    assert [h.currency for h in trace.history] == ["superseded", "superseded", "current"]
    cash = next(leg for leg in trace.legs if leg.fact_type == "cash_settled")
    assert cash.authoritative_source == "bank-synthetic"
    assert set(cash.observation_ids) == {"obs-B1", "obs-B2"}
    assert trace.epochs[0].cause == "registered" and len(trace.epochs) >= 3
    assert any("obs-B2" in e.evidence_ids for e in trace.epochs)
    assert trace.current.snapshot.known_at == datetime.fromisoformat("2026-10-02T09:30:00Z")


def test_conclusion_as_known_at(world: World) -> None:
    svc = service(world)
    k2 = svc.get_conclusion_as_known_at(OP, "2026-10-01T17:00:00Z", "2026-10-01T18:00:00Z")
    assert (k2.evaluation_id, k2.result, k2.currency.currency) == (world.k2, "MATCH", "superseded")
    later = svc.get_conclusion_as_known_at(OP, "2026-10-01T17:00:00Z", "2026-10-02T08:00:00Z")
    assert later.evaluation_id == world.k2  # K3 was not yet known at 08:00 on the 2nd
    with pytest.raises(QueryError) as missing:
        svc.get_conclusion_as_known_at(OP, "2026-10-01T17:00:00Z", "2026-10-01T12:00:00Z")
    assert missing.value.code == "NOT_FOUND"


def test_discrepancy_gives_operands_and_exact_delta(world: World) -> None:
    view = service(world).explain_discrepancy(world.k3, CASH)
    assert view.status == "FAIL" and view.replay_consistent
    assert view.left is not None and view.right is not None and view.delta is not None
    assert (view.left.atoms, view.right.atoms, view.left.unit) == ("9950000", "10000000", "USD")
    assert view.delta.atoms == "-50000" and view.delta.scale == 2
    assert "obs-B2" in view.evidence_refs


def test_conclusion_change_cites_the_correction(world: World) -> None:
    change = service(world).explain_conclusion_change(world.k2, world.k3)
    assert (change.from_result, change.to_result) == ("MATCH", "BREAK")
    assert [c.control_id for c in change.changed_controls] == [CASH]
    assert change.added_evidence == ["obs-B2"] and change.removed_evidence == ["obs-B1"]
    assert change.replay_consistent


def test_missing_evidence_names_the_authoritative_source(world: World) -> None:
    view = service(world).get_missing_evidence(world.k1, CASH)
    assert view.missing and view.reason_code == "MISSING_EVIDENCE"
    (cash,) = [r for r in view.requirements if r.fact_type == "cash_settled"]
    assert (cash.authoritative_source, cash.min_coverage_level) == (
        "bank-synthetic",
        "internally_checked",
    )
    decided = service(world).get_missing_evidence(world.k3, CASH)
    assert not decided.missing and "Nothing is missing" in decided.note


def test_coverage_report_and_evidence(world: World) -> None:
    report = service(world).get_coverage(OP)
    assert report.evaluation_id == world.k3 and report.unmet == []
    assert {c.source_id for c in report.certificates} == {
        "oms-synthetic",
        "bank-synthetic",
        "ta-synthetic",
        "stellar-testnet-frozen",
    }
    assert all(c.meets_required_level for c in report.certificates)
    evidence = service(world).get_evidence("obs-B2")
    assert evidence.kind == "observation" and evidence.content_trust == "untrusted_source_data"
    assert evidence.observation is not None and evidence.observation.supersedes == "obs-B1"
    assert service(world).get_evidence("cov-bank-k3").kind == "coverage"


def test_new_evidence_makes_the_answer_stale(world: World, corpus: Corpus) -> None:
    b2 = corpus.journals["main"]["obs-B2"].model_dump(mode="json")
    b2.update(observation_id="obs-B3", recorded_at="2026-10-03T00:00:00Z", supersedes="obs-B2")
    b2["source"]["revision"] = 3
    world.writer.append_observations([Observation.model_validate_json(json.dumps(b2))])
    trace = service(world).trace_operation(OP)
    assert trace.current is not None and trace.current.currency.currency == "stale"
    assert service(world).get_coverage(OP).currency.currency == "stale"


# --------------------------------------------------------------- authorisation


def test_scope_is_enforced_per_tool(world: World) -> None:
    operations_only = service(world, scopes=["operations:read"])
    assert operations_only.trace_operation(OP).current is not None
    with pytest.raises(QueryError) as forbidden:
        operations_only.get_evidence("obs-B1")
    assert forbidden.value.code == "FORBIDDEN"
    evidence_only = service(world, scopes=["evidence:read"])
    with pytest.raises(QueryError) as forbidden_op:
        evidence_only.trace_operation(OP)
    assert forbidden_op.value.code == "FORBIDDEN"


def test_other_tenant_sees_nothing(world: World) -> None:
    stranger = service(world, tenant_id="tenant-other")
    for call in (
        lambda: stranger.trace_operation(OP),
        lambda: stranger.explain_discrepancy(world.k3, CASH),
        lambda: stranger.get_evidence("obs-B1"),
        lambda: stranger.get_coverage(OP),
    ):
        with pytest.raises(QueryError) as hidden:
            call()
        assert hidden.value.code == "NOT_FOUND"


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.trace_operation("SUB-0001; DROP TABLE x"),
        lambda s: s.get_evidence("../../etc/passwd"),
        lambda s: s.get_conclusion_as_known_at("SUB-0001", "yesterday", "2026-10-02T00:00:00Z"),
        lambda s: s.get_conclusion_as_known_at(
            "SUB-0001", "2026-10-01T17:00:00+02:00", "2026-10-02T00:00:00Z"
        ),
    ],
)
def test_invalid_arguments_are_rejected(world: World, call: Any) -> None:
    with pytest.raises(QueryError) as invalid:
        call(service(world))
    assert invalid.value.code == "VALIDATION_ERROR"


def test_limits_truncate_lists(world: World) -> None:
    trace = service(world, max_items=1).trace_operation(OP)
    assert trace.truncated and len(trace.history) == 1 and len(trace.epochs) == 1
    assert trace.history[0].evaluation_id == world.k3


def test_reader_role_cannot_write(world: World, corpus: Corpus) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        world.reader.append_observations(
            [corpus.journals["main"]["obs-B1"].model_copy(update={"observation_id": "obs-x"})]
        )
    world.reader.conn.rollback()
    for statement in (
        "INSERT INTO invaria.knowledge_cuts (tenant_id, known_at) VALUES ('t', now())",
        "UPDATE invaria.evaluations SET result = 'MATCH'",
        "SELECT * FROM invaria.schema_migrations",
    ):
        with pytest.raises(errors.InsufficientPrivilege):
            world.reader.conn.execute(statement)
        world.reader.conn.rollback()


def test_audit_log_records_calls_without_arguments(world: World, tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    svc = QueryService(world.reader, access(scopes=["operations:read"]), AuditLog(log))
    svc.trace_operation(OP)
    with pytest.raises(QueryError):
        svc.get_evidence("obs-B1")
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(entry["tool"], entry["status"]) for entry in lines] == [
        ("trace_operation", "ok"),
        ("get_evidence", "FORBIDDEN"),
    ]
    assert all(entry["principal"] == "analyst-1" for entry in lines)
    assert "obs-B1" not in log.read_text() and "SUB-0001" not in log.read_text()


def test_unavailable_store_is_a_retryable_error(world: World) -> None:
    world.reader.conn.close()
    with pytest.raises(QueryError) as down:
        service(world).trace_operation(OP)
    assert down.value.code == "SOURCE_UNAVAILABLE" and down.value.retryable


# ---------------------------------------------------------------------- MCP


def test_real_mcp_session_answers_with_structured_content(world: World) -> None:
    async def session() -> tuple[Any, Any, Any]:
        server = build_server(service(world))
        async with create_connected_server_and_client_session(server) as client:
            trace = await client.call_tool("trace_operation", {"operation_ref": OP})
            change = await client.call_tool(
                "explain_conclusion_change", {"old_id": world.k2, "new_id": world.k3}
            )
            missing = await client.call_tool("get_evidence", {"evidence_id": "obs-nope"})
            return trace, change, missing

    trace, change, missing = asyncio.run(session())
    assert not trace.isError and trace.structuredContent["current"]["result"] == "BREAK"
    assert trace.structuredContent["current"]["currency"]["currency"] == "current"
    assert change.structuredContent["added_evidence"] == ["obs-B2"]
    assert missing.isError and "NOT_FOUND" in json.dumps([c.model_dump() for c in missing.content])


def test_cli_serves_over_stdio(world: World, tmp_path: Path) -> None:
    """The real command, as a subprocess, spoken to by the SDK's stdio client."""
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    profile = tmp_path / "access.json"
    profile.write_text(access(scopes=["operations:read"]).model_dump_json(), "utf-8")
    audit = tmp_path / "audit.jsonl"
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "invaria.cli",
            "mcp",
            "serve",
            "--access-profile",
            str(profile),
            "--audit-log",
            str(audit),
        ],
        env={**os.environ, "INVARIA_DATABASE_URL": world.dsn},
    )

    async def session() -> tuple[Any, Any]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                result = await client.call_tool("get_coverage", {"operation_ref": OP})
                return tools, result

    tools, result = asyncio.run(session())
    assert len(tools.tools) == 7
    assert not result.isError and result.structuredContent["evaluation_id"] == world.k3
    assert json.loads(audit.read_text().splitlines()[0])["tool"] == "get_coverage"


# ----------------------------------------------- engine versions


def test_a_new_engine_never_overwrites_a_historical_evaluation(world: World) -> None:
    """A conclusion recorded with the retired 0.1.0 stays stored next to the current one;
    queries tell them apart and the historical one replays with its own engine."""
    current = world.writer.load_evaluation(TENANT, world.k3)
    assert current.versions.engine_ref == "invaria-engine@0.10.0"
    inputs = world.writer.load_inputs(TENANT, current.snapshot_id)
    historical = replay(inputs, "invaria-engine@0.1.0").result
    assert historical.evaluation_id != current.evaluation_id
    with pytest.raises(ValueError, match="not current"):
        world.writer.save_evaluation(TENANT, historical)
    insert_historical_evaluation(world.writer.conn, historical)
    assert world.writer.load_evaluation(TENANT, world.k3) == current  # untouched
    # Only current engines publish: the retired conclusion stays stored, never current.
    epoch = world.writer.scope_head(TENANT, OP).epoch
    refused = world.writer.publish(TENANT, historical.evaluation_id, epoch)
    assert refused.outcome == "NOT_CURRENT" and "not a current engine" in refused.detail
    query = service(world)
    old_view = query.get_evaluation(historical.evaluation_id)
    new_view = query.get_evaluation(world.k3)
    assert (old_view.versions.engine_ref, old_view.versions.engine_status) == (
        "invaria-engine@0.1.0",
        "retired",
    )
    assert (new_view.versions.engine_ref, new_view.versions.engine_status) == (
        "invaria-engine@0.10.0",
        "current",
    )
    assert any(
        "does not validate it under the current semantics" in x for x in old_view.limitations
    )
    assert not any("does not validate it" in x for x in new_view.limitations)
    change = query.explain_conclusion_change(historical.evaluation_id, world.k3)
    assert any("engines differ" in limitation for limitation in change.limitations)
    assert (old_view.result, old_view.controls) == (new_view.result, new_view.controls)
    # Replayed with its exact (retired) engine, never with the current one.
    old_discrepancy = query.explain_discrepancy(historical.evaluation_id, CASH)
    assert old_discrepancy.replay_consistent
    engines = {
        e.evaluation_id: e.engine_ref
        for e in query.get_timeline(OP).entries
        if e.kind == "evaluation"
    }
    assert engines[historical.evaluation_id] == "invaria-engine@0.1.0"
    assert engines[world.k3] == "invaria-engine@0.10.0"
    # The published conclusion is still the current engine's.
    published = world.reader.current_evaluation(TENANT, OP)
    assert published is not None and published.evaluation_id == world.k3
    total, reproduced, missing, mismatches = reproduce_all(world.reader)
    assert (reproduced, missing, mismatches) == (total, [], [])


def test_retired_engines_are_not_selectable_for_new_evaluations(
    world: World, corpus: Corpus
) -> None:
    for ref in ("invaria-engine@0.1.0", "invaria-engine@0.2.0", "invaria-redemption-engine@0.4.0"):
        with pytest.raises(ValueError, match="not selectable for new evaluations"):
            reevaluate(world.writer, TENANT, OP, clocks(corpus, "K3"), engine_ref=ref)
