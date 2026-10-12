"""Operational integrity analytics against a real PostgreSQL, read-only role.

Two tenants share the database; the worker evaluates and publishes as in production. The
expectations were written before the code.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from invaria.cli import main
from invaria.contracts.base import parse_contract
from invaria.corpus_loader import Corpus, load_corpus
from invaria.persistence.dsn import with_database
from invaria.persistence.migrate import migrate
from invaria.persistence.provision import prepare_for_migrator
from invaria.persistence.store import READER_ROLE, PgStore
from invaria.persistence.worker import Clocks, prepare, reevaluate
from invaria.query import analytics
from invaria.query.models import (
    AccessProfile,
    IntegrityMember,
    IntegrityReport,
    SelectedEvaluation,
)
from invaria.query.service import QueryError, QueryService

pytestmark = pytest.mark.postgres
ADMIN_DSN = os.environ.get("INVARIA_TEST_DATABASE_URL")
CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"
TENANT = "tenant-synthetic-demo"
OTHER = "tenant-other"
OP = "SUB-0001"


@pytest.fixture
def database() -> Iterator[str]:
    if not ADMIN_DSN:
        pytest.skip("set INVARIA_TEST_DATABASE_URL to an admin PostgreSQL URL to run DB tests")
    name = f"invaria_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    try:
        with psycopg.connect(with_database(ADMIN_DSN, name)) as conn:
            prepare_for_migrator(conn)
            migrate(conn)
        yield with_database(ADMIN_DSN, name)
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


def clocks(corpus: Corpus, scenario: str) -> Clocks:
    s = corpus.scenarios[scenario].snapshot
    return Clocks(s.valid_at, s.known_at, s.evaluation_clock)


def feed(store: PgStore, corpus: Corpus, upto: datetime) -> None:
    coverage = {c for s in ("K1", "K2", "K3") for c in corpus.scenarios[s].snapshot.coverage_ids}
    store.append_identity_links(
        TENANT, [k for k in corpus.identity_links.values() if k.recorded_at <= upto]
    )
    store.append_coverage(
        c for c in corpus.coverage.values() if c.coverage_id in coverage and c.recorded_at <= upto
    )
    store.append_observations(o for o in corpus.journals["main"].values() if o.recorded_at <= upto)


def chosen(member: IntegrityMember) -> SelectedEvaluation:
    assert member.selected is not None
    return member.selected


@dataclass
class World:
    writer: PgStore
    reader: PgStore
    dsn: str
    corpus: Corpus
    other_evaluation: str

    def publish(self, scenario: str) -> str:
        prepared, publication = reevaluate(self.writer, TENANT, OP, clocks(self.corpus, scenario))
        assert publication.outcome == "PUBLISHED"
        return prepared.evaluation.result.evaluation_id


@pytest.fixture
def world(database: str, corpus: Corpus) -> Iterator[World]:
    """Tenant A: SUB-0001 published K1 then K2 (MATCH); SUB-0002 evaluated, never published;
    SUB-0003 registered, never evaluated. Tenant B: one published operation of its own."""
    writer = PgStore(psycopg.connect(database))
    profile = corpus.profile.profile_ref
    writer.put_profile(corpus.profile)
    writer.register_scope(TENANT, OP, profile)
    for scenario in ("K1", "K2"):
        feed(writer, corpus, clocks(corpus, scenario).known_at)
        _, publication = reevaluate(writer, TENANT, OP, clocks(corpus, scenario))
        assert publication.outcome == "PUBLISHED"
    writer.register_scope(TENANT, "SUB-0002", profile)
    prepare(writer, TENANT, "SUB-0002", clocks(corpus, "K2"))  # stored, not published
    writer.register_scope(TENANT, "SUB-0003", profile)
    for op in ("B-0001", OP):  # the other tenant reuses an operation_ref on purpose
        writer.register_scope(OTHER, op, profile)
    other, publication = reevaluate(writer, OTHER, "B-0001", clocks(corpus, "K2"))
    assert publication.outcome == "PUBLISHED"
    reader = PgStore(psycopg.connect(database), role=READER_ROLE)
    yield World(writer, reader, database, corpus, other.evaluation.result.evaluation_id)
    writer.conn.close()
    reader.conn.close()


EXPORT_POLICY = "export-integrity-report-file@1"


def access(tenant: str = TENANT, **overrides: Any) -> AccessProfile:
    return AccessProfile.model_validate_json(
        json.dumps(
            {
                "schema_version": "1.0",
                "principal_id": "analyst-1",
                "tenant_id": tenant,
                "scopes": ["operations:read"],
                "max_items": 50,
                # Explicit full disclosure policies: no implicit policy exists.
                "disclosure_policies": [
                    "full-operations@1",
                    "full-evidence@1",
                    "full-report-index@1",
                    "full-report-summary@1",
                ],
                **overrides,
            }
        )
    )


def svc(world: World, tenant: str = TENANT, **overrides: Any) -> QueryService:
    return QueryService(world.reader, access(tenant, **overrides))


def as_of(world: World, scenario: str) -> datetime:
    return clocks(world.corpus, scenario).known_at + timedelta(hours=1)


def test_population_selection_and_tenant_isolation(world: World) -> None:
    report = svc(world).integrity_report(as_of(world, "K2"))
    found = {m.operation_ref: (m.status, m.status_detail) for m in report.members}
    assert found == {
        OP: ("admitted", []),
        "SUB-0002": ("stored_not_published", ["no_publication_attempt_recorded"]),
        "SUB-0003": ("registered_not_evaluated", []),
    }
    d = report.distribution
    assert (d.included, d.admitted, d.match_count) == (3, 1, 1)
    assert (d.stored_evaluations, d.evaluations_not_selected) == (3, 2)  # K1 and SUB-0002
    assert report.members[0].published_history[0].epoch < chosen(report.members[0]).published_epoch
    other = svc(world, OTHER).integrity_report(as_of(world, "K2"))
    assert {m.operation_ref for m in other.members} == {"B-0001", OP}
    assert other.distribution.included == 2
    assert world.reader.conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_new_evidence_makes_the_publication_stale_and_a_kept_report_reproduces(
    world: World,
) -> None:
    service = svc(world)
    before = service.integrity_report(as_of(world, "K2"))
    feed(world.writer, world.corpus, clocks(world.corpus, "K3").known_at)  # new evidence
    stale = service.integrity_report(as_of(world, "K3"))
    assert stale.members[0].status == "stale_publication"
    assert stale.members[0].status_detail == ["evidence_appended"]
    assert chosen(stale.members[0]).result == "MATCH"  # history, not counted
    assert stale.distribution.admitted == 0
    world.publish("K3")
    after = service.integrity_report(as_of(world, "K3"))
    assert (after.distribution.admitted, after.distribution.break_count) == (1, 1)
    early = service.integrity_report(as_of(world, "K2"))  # K3 knows evidence after this cut
    assert early.members[0].status == "published_after_cut"
    kept = service.reproduce_integrity_report(before)
    assert (kept.status, kept.differences) == ("REPRODUCED", [])
    assert kept.checked_evaluations == 2  # K1 and K2 publications


def test_cases_lead_to_stored_evidence(world: World) -> None:
    feed(world.writer, world.corpus, clocks(world.corpus, "K3").known_at)
    world.publish("K3")
    service = svc(world)
    report = service.integrity_report(as_of(world, "K3"))
    view = service.integrity_cases(report, "discrepancy:any")
    (case,) = view.cases
    assert case.operation_ref == OP and case.result == "BREAK"
    (reason,) = case.reasons
    assert reason.control_id == "subscription.cash_vs_order" and reason.evidence_refs
    assert view.total_operations == report.discrepancies.operations_with_discrepancy
    page = service.integrity_cases(report, "included", offset=1, limit=1)
    assert [c.operation_ref for c in page.cases] == ["SUB-0002"]


def test_reports_of_another_scope_or_altered_never_widen_access(world: World) -> None:
    mine = svc(world).integrity_report(as_of(world, "K2"))
    theirs = svc(world, OTHER).integrity_report(as_of(world, "K2"))
    with pytest.raises(QueryError) as error:
        svc(world).integrity_cases(theirs, "included")
    assert error.value.code == "FORBIDDEN"
    with pytest.raises(QueryError) as error:
        svc(world).reproduce_integrity_report(theirs)
    assert error.value.code == "FORBIDDEN"
    altered = mine.model_copy(update={"max_replays": 7})
    with pytest.raises(QueryError) as error:
        svc(world).integrity_cases(altered, "included")
    assert error.value.code == "VALIDATION_ERROR"
    # A forged report: tenant A's label, tenant B's evaluation, re-hashed so it passes the
    # integrity check. The store of tenant A does not have it.
    data = mine.model_dump(mode="json")
    member = data["members"][0]
    member["selected"]["evaluation_id"] = world.other_evaluation
    member["published_history"][-1]["evaluation_id"] = world.other_evaluation
    data["report_sha256"] = analytics.report_sha256(data)
    forged = parse_contract(IntegrityReport, json.dumps(data))
    for call in (
        lambda: svc(world).integrity_cases(forged, "status:admitted"),
        lambda: svc(world).reproduce_integrity_report(forged),
    ):
        with pytest.raises(QueryError) as error:
            call()
        assert error.value.code == "NOT_FOUND"
    # A coherent swap: the unevaluated member renamed to the other tenant's operation, so
    # every count still agrees; the store of this scope refuses it.
    data = mine.model_dump(mode="json")
    for m in data["members"]:
        if m["operation_ref"] == "SUB-0003":
            m["operation_ref"] = "B-0001"
    data["report_sha256"] = analytics.report_sha256(data)
    smuggled = parse_contract(IntegrityReport, json.dumps(data))
    assert analytics.consistency_problems(smuggled) == []
    with pytest.raises(QueryError) as error:
        svc(world).integrity_cases(smuggled, "status:registered_not_evaluated")
    assert error.value.code == "NOT_FOUND"


def test_scope_is_checked_before_any_count(world: World) -> None:
    with pytest.raises(QueryError) as error:
        svc(world, scopes=["evidence:read"]).integrity_report(as_of(world, "K2"))
    assert error.value.code == "FORBIDDEN"
    with pytest.raises(QueryError) as error:
        svc(world).integrity_report("2026-10-01T00:00:00")
    assert error.value.code == "VALIDATION_ERROR"


def test_a_changed_pinned_evaluation_is_a_mismatch(world: World) -> None:
    report = svc(world).integrity_report(as_of(world, "K2"))
    data = report.model_dump(mode="json")
    data["members"][0]["published_history"][0]["evaluation_sha256"] = "0" * 64
    data["report_sha256"] = analytics.report_sha256(data)
    result = svc(world).reproduce_integrity_report(
        parse_contract(IntegrityReport, json.dumps(data))
    )
    assert result.status == "MISMATCH"
    assert any("pinned sha256" in d for d in result.differences)


def test_cli_report_cases_and_reproduce(
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile = tmp_path / "access.json"
    # Writing the file is an export, with its own scope and policy.
    exporter = access(scopes=["operations:read", "exports:create"])
    exporter = exporter.model_copy(
        update={"disclosure_policies": [*exporter.disclosure_policies, EXPORT_POLICY]}
    )
    profile.write_text(exporter.model_dump_json(), "utf-8")
    monkeypatch.setenv("INVARIA_DATABASE_URL", world.dsn)
    out = tmp_path / "report.json"
    moment = as_of(world, "K2").isoformat().replace("+00:00", "Z")
    common = ["--access-profile", str(profile)]
    assert main(["analytics", "report", *common, "--as-of", moment, "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "A.match_share_of_conclusive: 1/1 = 100.00 %" in text
    assert "evidence horizon" in text and "not reconstructed at the horizon" in text
    assert "does not authenticate the report" in text
    assert "UNKNOWN left out of this denominator: 0" in text
    assert main(["analytics", "report", *common, "--as-of", moment, "--out", str(out)]) == 2
    capsys.readouterr()
    assert (
        main(["analytics", "cases", *common, "--report", str(out), "--key", "status:admitted"]) == 0
    )
    assert "SUB-0001 [admitted] MATCH" in capsys.readouterr().out
    assert main(["analytics", "reproduce", *common, "--report", str(out)]) == 0
    assert capsys.readouterr().out.startswith("REPRODUCED")
    assert main(["analytics", "cases", *common, "--report", str(out), "--key", "nope"]) == 2


def rehashed(report: IntegrityReport, edit: Any) -> IntegrityReport:
    data = report.model_dump(mode="json")
    edit(data)
    data["report_sha256"] = analytics.report_sha256(data)
    return parse_contract(IntegrityReport, json.dumps(data))


def test_an_engine_retired_after_publication_needs_reevaluation(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invaria.engine.versions import SUBSCRIPTION_ENGINE_REF, engine_status

    service = svc(world)
    before = service.integrity_report(as_of(world, "K2"))  # published with a current engine
    assert chosen(before.members[0]).engine_status == "current"
    monkeypatch.setattr(
        "invaria.query.analytics.engine_status",
        lambda ref: "retired" if ref == SUBSCRIPTION_ENGINE_REF else engine_status(ref),
    )
    after = service.integrity_report(as_of(world, "K2"))
    member = after.members[0]
    assert (member.status, member.status_detail) == (
        "engine_retired_requires_reevaluation",
        [SUBSCRIPTION_ENGINE_REF],
    )
    assert (after.distribution.included, after.distribution.admitted) == (3, 0)
    current = world.reader.current_evaluation(TENANT, OP)  # the store is untouched
    assert current is not None and current.currency == "current"
    kept = service.reproduce_integrity_report(before)  # pinned policy and engine status
    assert (kept.status, kept.differences) == ("REPRODUCED", [])


def test_tenant_and_scopes_in_the_file_grant_nothing(world: World) -> None:
    mine = svc(world).integrity_report(as_of(world, "K2"))

    def to_other(data: dict[str, Any]) -> None:
        data["scope"]["tenant_id"] = OTHER

    moved = rehashed(mine, to_other)
    for call in (
        lambda: svc(world, OTHER).integrity_cases(moved, "status:admitted"),
        lambda: svc(world, OTHER).reproduce_integrity_report(moved),
    ):
        with pytest.raises(QueryError) as error:  # its references are not in OTHER's store
            call()
        assert error.value.code == "NOT_FOUND"

    def widen(data: dict[str, Any]) -> None:
        data["scope"]["access_scopes"] = ["evidence:read", "operations:read"]

    widened = rehashed(mine, widen)
    with pytest.raises(QueryError) as error:  # scopes come from the access profile
        svc(world, scopes=["evidence:read"]).integrity_cases(widened, "included")
    assert error.value.code == "FORBIDDEN"


def test_an_edited_figure_or_a_dropped_case_is_refused_even_rehashed(world: World) -> None:
    mine = svc(world).integrity_report(as_of(world, "K2"))

    def numerator(data: dict[str, Any]) -> None:
        data["distribution"]["metrics"][1]["numerator"] += 1

    def drop(data: dict[str, Any]) -> None:
        data["members"] = data["members"][1:]

    for edit in (numerator, drop):
        forged = rehashed(mine, edit)
        with pytest.raises(QueryError) as error:
            svc(world).integrity_cases(forged, "included")
        assert error.value.code == "VALIDATION_ERROR"
        result = svc(world).reproduce_integrity_report(forged)
        assert result.status == "MISMATCH" and result.differences
