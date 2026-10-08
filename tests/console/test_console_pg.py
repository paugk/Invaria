"""End to end: the real ``invaria console serve`` against PostgreSQL, walked over HTTP."""

from __future__ import annotations

import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from invaria.contracts.observation import Observation
from invaria.corpus_loader import Corpus, load_corpus
from invaria.persistence.dsn import with_database
from invaria.persistence.migrate import migrate
from invaria.persistence.provision import prepare_for_migrator
from invaria.persistence.store import READER_ROLE, PgStore
from invaria.persistence.worker import Clocks, reevaluate
from invaria.query.models import AccessProfile

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")
TENANT = "tenant-synthetic-demo"
OP = "SUB-0001"
CASH = "subscription.cash_vs_order"
CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


def _dsn_for(database: str) -> str:
    assert ADMIN_DSN is not None
    return with_database(ADMIN_DSN, database)


@pytest.fixture
def database() -> Iterator[str]:
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
    dsn: str
    writer: PgStore
    ids: list[str]


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
    yield World(database, writer, ids)
    writer.conn.close()


def profile_file(tmp_path: Path, **overrides: Any) -> Path:
    fields: dict[str, Any] = {
        "schema_version": "1.0",
        "principal_id": "analyst-1",
        "tenant_id": TENANT,
        "scopes": ["operations:read", "evidence:read"],
        "max_items": 50,
        **overrides,
    }
    path = tmp_path / "access.json"
    path.write_text(AccessProfile.model_validate(fields).model_dump_json(), "utf-8")
    return path


@dataclass
class Console:
    base: str
    login: str
    process: subprocess.Popen[str]


@pytest.fixture
def console(world: World, tmp_path: Path) -> Iterator[Console]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "invaria.cli",
            "console",
            "serve",
            "--access-profile",
            str(profile_file(tmp_path)),
            "--port",
            "0",
            "--audit-log",
            str(tmp_path / "audit.jsonl"),
        ],
        env={**os.environ, "INVARIA_DATABASE_URL": world.dsn},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    line = process.stdout.readline()
    assert "Open: " in line, line
    login = line.split("Open: ", 1)[1].strip()
    base = login.split("/login", 1)[0]
    try:
        yield Console(base, login, process)
    finally:
        process.terminate()
        process.wait(timeout=10)


def browser() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )


def fetch(
    opener: urllib.request.OpenerDirector, url: str | urllib.request.Request
) -> tuple[int, str]:
    try:
        with opener.open(url, timeout=30) as reply:
            return reply.status, reply.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


def test_walkthrough_over_http(console: Console, world: World, tmp_path: Path) -> None:
    k1, k2, k3 = world.ids
    anonymous = browser()
    assert fetch(anonymous, console.base + "/")[0] == 401

    user = browser()
    status, home = fetch(user, console.login)  # 303 -> cookie -> operations
    assert status == 200 and "Operations and their published conclusion" in home
    assert 'href="/operations/SUB-0001"' in home and ">BREAK<" in home

    status, detail = fetch(user, console.base + "/operations/SUB-0001")
    assert status == 200
    assert f"/compare?left={k1}&amp;right={k2}" in detail
    assert f"/compare?left={k2}&amp;right={k3}" in detail

    status, comparison = fetch(user, console.base + f"/compare?left={k2}&right={k3}")
    assert status == 200 and 'href="/evidence/obs-B2"' in comparison

    status, control = fetch(user, console.base + f"/evaluations/{k3}/controls/{CASH}")
    assert status == 200 and '<span class="qty">-500.00 USD</span>' in control

    status, body = fetch(user, console.base + "/v1/operations/SUB-0001/timeline")
    timeline = json.loads(body)
    assert [e["result"] for e in timeline["entries"] if e["kind"] == "evaluation"] == [
        "UNKNOWN",
        "MATCH",
        "BREAK",
    ]
    assert timeline["entries"][0]["cause"] == "registered"

    status, body = fetch(user, console.base + f"/v1/evaluation-comparisons?left={k2}&right={k3}")
    change = json.loads(body)["change"]
    assert (change["from_result"], change["to_result"]) == ("MATCH", "BREAK")
    assert change["added_evidence"] == ["obs-B2"]

    request = urllib.request.Request(console.base + "/v1/operations", method="POST", data=b"{}")
    assert fetch(user, request)[0] == 405

    console.process.terminate()
    _, stderr = console.process.communicate(timeout=10)
    token = console.login.split("token=", 1)[1]
    assert token not in stderr and "GET /operations/SUB-0001 200" in stderr
    audit = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert {"list_operations", "get_timeline", "compare_evaluations"} <= {a["tool"] for a in audit}


def test_store_reads_for_the_console(world: World, corpus: Corpus) -> None:
    """Through the read-only role: listing, epoch log, stale currency after new evidence."""
    reader = PgStore(psycopg.connect(world.dsn), role=READER_ROLE)
    try:
        assert reader.operation_refs(TENANT) == [OP]
        assert reader.operation_refs("tenant-other") == []
        log = reader.epoch_log(TENANT, OP)
        assert log[0][1] == "registered" and log[0][2] is None
        assert all(floor is not None for _, _, floor, _ in log[1:])
        b2 = corpus.journals["main"]["obs-B2"].model_dump(mode="json")
        b2.update(observation_id="obs-B3", recorded_at="2026-10-03T00:00:00Z", supersedes="obs-B2")
        b2["source"]["revision"] = 3
        world.writer.append_observations([Observation.model_validate_json(json.dumps(b2))])
        log = reader.epoch_log(TENANT, OP)
        assert log[-1][3]["observation_ids"] == ["obs-B3"]
    finally:
        reader.conn.close()


def test_stale_and_scope_over_http(world: World, corpus: Corpus, tmp_path: Path) -> None:
    b2 = corpus.journals["main"]["obs-B2"].model_dump(mode="json")
    b2.update(observation_id="obs-B3", recorded_at="2026-10-03T00:00:00Z", supersedes="obs-B2")
    b2["source"]["revision"] = 3
    world.writer.append_observations([Observation.model_validate_json(json.dumps(b2))])
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "invaria.cli",
            "console",
            "serve",
            "--access-profile",
            str(profile_file(tmp_path, scopes=["operations:read"])),
            "--port",
            "0",
        ],
        env={**os.environ, "INVARIA_DATABASE_URL": world.dsn},
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert process.stdout is not None
        login = process.stdout.readline().split("Open: ", 1)[1].strip()
        base = login.split("/login", 1)[0]
        user = browser()
        fetch(user, login)
        status, detail = fetch(user, base + "/operations/SUB-0001")
        assert status == 200 and '<span class="badge currency-stale">stale</span>' in detail
        assert fetch(user, base + "/evidence/obs-B1")[0] == 403
        assert fetch(user, base + "/operations/SUB-9999")[0] == 404
    finally:
        process.terminate()
        process.wait(timeout=10)
