"""Console behaviour without PostgreSQL: an in-memory store over K1 -> K2 -> K3, a WSGI
client, a structural accessibility checker and an opt-in headless Chrome rendering.

Browser: ``INVARIA_BROWSER_E2E=1 uv run --locked pytest -m browser tests/console`` (a
throwaway profile directory; never the user's browser sessions).
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from invaria.console.app import ConsoleApp
from invaria.console.html import el
from invaria.console.pages import quantity
from invaria.console.server import build
from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.evaluation import EvaluationResult, SnapshotRef
from invaria.contracts.observation import Observation
from invaria.contracts.quantity import Quantity
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.evaluate import EvaluationInputs, evaluate
from invaria.persistence.store import CurrentView
from invaria.query.models import (
    AccessProfile,
    ComparisonView,
    OperationsView,
    TimelineView,
)
from invaria.query.service import QueryService

CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"
TENANT = "tenant-synthetic-demo"
OP = "SUB-0001"
TOKEN = "t" * 40


def session_for(token: str) -> str:
    """The cookie value the console issues for a sign-in token."""
    return hmac.new(token.encode(), b"invaria-console-session", hashlib.sha256).hexdigest()


SESSION = session_for(TOKEN)
HOST = "127.0.0.1:8765"


@pytest.fixture(scope="session")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


@dataclass
class MemoryStore:
    """The PgStore reads used by QueryService, over K1 -> K2 -> K3 evaluated in memory."""

    corpus: Corpus
    stale: bool = False
    observations: dict[str, Observation] = field(default_factory=dict)
    snapshots: dict[str, SnapshotRef] = field(default_factory=dict)
    evaluations: list[EvaluationResult] = field(default_factory=list)
    log: list[tuple[int, str, datetime | None, dict[str, Any]]] = field(default_factory=list)
    registered_only: list[str] = field(default_factory=list)  # scopes with no evaluation yet

    def __post_init__(self) -> None:
        self.observations.update(self.corpus.journals["main"])
        self.log.append((1, "registered", None, {}))
        seen_obs: set[str] = set()
        seen_cov: set[str] = set()
        for epoch, scenario in enumerate(("K1", "K2", "K3"), start=2):
            snapshot = self.corpus.scenarios[scenario].snapshot
            self.snapshots[snapshot.snapshot_id] = snapshot
            self.evaluations.append(evaluate(self.corpus.inputs_for(scenario)).result)
            new_obs = sorted(set(snapshot.observation_ids) - seen_obs)
            new_cov = sorted(set(snapshot.coverage_ids) - seen_cov)
            seen_obs |= set(new_obs)
            seen_cov |= set(new_cov)
            floor = max(
                [self.observations[i].recorded_at for i in new_obs]
                + [self.corpus.coverage[i].recorded_at for i in new_cov]
            )
            document = {"observation_ids": new_obs, "coverage_ids": new_cov}
            self.log.append((epoch, "evidence_appended", floor, document))

    def _tenant(self, tenant_id: str) -> None:
        if tenant_id != TENANT:
            raise KeyError(tenant_id)

    def operation_refs(self, tenant_id: str) -> list[str]:
        return sorted([OP, *self.registered_only]) if tenant_id == TENANT else []

    def evaluations_for(self, tenant_id: str, operation_ref: str) -> list[EvaluationResult]:
        if tenant_id != TENANT or operation_ref != OP:
            return []
        return list(self.evaluations)

    def epochs(self, tenant_id: str, operation_ref: str) -> list[tuple[int, str, dict[str, Any]]]:
        return [(n, c, d) for n, c, _, d in self.epoch_log(tenant_id, operation_ref)]

    def epoch_log(
        self, tenant_id: str, operation_ref: str
    ) -> list[tuple[int, str, datetime | None, dict[str, Any]]]:
        if tenant_id != TENANT or operation_ref != OP:
            return []
        return list(self.log)

    def current_evaluation(self, tenant_id: str, operation_ref: str) -> CurrentView | None:
        if tenant_id != TENANT or operation_ref != OP:
            raise KeyError(operation_ref)
        published = len(self.log)
        scope = published + 1 if self.stale else published
        return CurrentView(
            self.evaluations[-1].evaluation_id,
            published,
            scope,
            "stale" if self.stale else "current",
        )

    def load_evaluation(self, tenant_id: str, evaluation_id: str) -> EvaluationResult:
        self._tenant(tenant_id)
        for e in self.evaluations:
            if e.evaluation_id == evaluation_id:
                return e
        raise KeyError(evaluation_id)

    def load_snapshot(self, tenant_id: str, snapshot_id: str) -> SnapshotRef:
        self._tenant(tenant_id)
        return self.snapshots[snapshot_id]

    def load_inputs(self, tenant_id: str, snapshot_id: str) -> EvaluationInputs:
        self._tenant(tenant_id)
        return EvaluationInputs(
            snapshot=self.snapshots[snapshot_id],
            profile=self.corpus.profile,
            observations=self.corpus.journals["main"],
            coverage=self.corpus.coverage,
            identity_links=self.corpus.identity_links,
        )

    def load_observation(self, tenant_id: str, observation_id: str) -> Observation:
        self._tenant(tenant_id)
        return self.observations[observation_id]

    def load_coverage(self, tenant_id: str, coverage_id: str) -> CoverageCertificate:
        self._tenant(tenant_id)
        return self.corpus.coverage[coverage_id]


def access(**overrides: Any) -> AccessProfile:
    fields: dict[str, Any] = {
        "schema_version": "1.0",
        "principal_id": "analyst-1",
        "tenant_id": TENANT,
        "scopes": ["operations:read", "evidence:read"],
        "max_items": 50,
        **overrides,
    }
    return AccessProfile.model_validate(fields)


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


@dataclass
class Client:
    app: ConsoleApp

    def get(
        self,
        url: str,
        *,
        method: str = "GET",
        host: str = HOST,
        auth: bool = True,
        headers: dict[str, str] | None = None,
    ) -> Reply:
        parts = urlsplit(url)
        environ: dict[str, Any] = {
            "REQUEST_METHOD": method,
            "PATH_INFO": parts.path,
            "QUERY_STRING": parts.query,
            "HTTP_HOST": host,
            "wsgi.errors": io.StringIO(),
        }
        if auth:
            environ["HTTP_COOKIE"] = f"invaria_console={SESSION}"
        for name, value in (headers or {}).items():
            environ["HTTP_" + name.upper().replace("-", "_")] = value
        captured: dict[str, Any] = {}

        def start_response(status: str, response_headers: list[tuple[str, str]]) -> None:
            captured["status"] = int(status.split()[0])
            captured["headers"] = dict(response_headers)

        body = b"".join(self.app(environ, start_response))
        return Reply(captured["status"], captured["headers"], body)


@pytest.fixture
def store(corpus: Corpus) -> MemoryStore:
    return MemoryStore(corpus)


@pytest.fixture
def make_client(store: MemoryStore) -> Callable[..., Client]:
    def make(**overrides: Any) -> Client:
        service = QueryService(store, access(**overrides))  # type: ignore[arg-type]
        return Client(ConsoleApp(service, token=TOKEN, allowed_hosts={HOST}))

    return make


@pytest.fixture
def client(make_client: Callable[..., Client]) -> Client:
    return make_client()


# --------------------------------------------------------------- accessibility


class _Audit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.headings: list[int] = []
        self.ids: list[str] = []
        self.label_for: set[str] = set()
        self.controls: list[str] = []
        self.link_text: list[str] | None = None
        self.captions = 0
        self.tables = 0
        self.problems: list[str] = []
        self.first_link: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        self.tags.append((tag, a))
        if a.get("id"):
            self.ids.append(str(a["id"]))
        if any(name.startswith("on") for name in a) or "style" in a:
            self.problems.append(f"inline script or style on <{tag}>")
        if tag == "script":
            self.problems.append("script element")
        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append(int(tag[1]))
        if tag == "label" and a.get("for"):
            self.label_for.add(str(a["for"]))
        if tag in ("input", "select", "textarea"):
            if not a.get("id"):
                self.problems.append(f"<{tag}> without id")
            else:
                self.controls.append(str(a["id"]))
        if tag == "form" and (a.get("method") or "get").lower() != "get":
            self.problems.append("form that is not GET")
        if tag == "table":
            self.tables += 1
        if tag == "caption":
            self.captions += 1
        if tag == "th" and a.get("scope") not in ("col", "row"):
            self.problems.append("<th> without scope")
        if tag == "time" and not a.get("datetime"):
            self.problems.append("<time> without datetime")
        if tag == "img" and a.get("alt") is None:
            self.problems.append("<img> without alt")
        if tag == "a":
            if self.first_link is None:
                self.first_link = a.get("href")
            self.link_text = []
        if tag not in ("meta", "link", "input", "br"):
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.link_text is not None:
            if not "".join(self.link_text).strip():
                self.problems.append("link without text")
            self.link_text = None
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self.link_text is not None:
            self.link_text.append(data)


def audit_html(text: str) -> list[str]:
    """Structural WCAG checks a parser can decide; returns the problems found."""
    parser = _Audit()
    parser.feed(text)
    p = list(parser.problems)
    html_tags = [a for t, a in parser.tags if t == "html"]
    if not html_tags or not html_tags[0].get("lang"):
        p.append("missing <html lang>")
    titles = [t for t, _ in parser.tags if t == "title"]
    if len(titles) != 1:
        p.append("needs exactly one <title>")
    for landmark in ("header", "nav", "main", "footer"):
        if not any(t == landmark for t, _ in parser.tags):
            p.append(f"missing <{landmark}>")
    if parser.headings.count(1) != 1:
        p.append("needs exactly one <h1>")
    if parser.headings and parser.headings[0] != 1:
        p.append("first heading is not <h1>")
    for before, after in zip(parser.headings, parser.headings[1:], strict=False):
        if after > before + 1:
            p.append(f"heading level skips from h{before} to h{after}")
    if len(parser.ids) != len(set(parser.ids)):
        p.append("duplicate id")
    if parser.first_link != "#main" or "main" not in parser.ids:
        p.append("skip link to #main is not the first link")
    for control in parser.controls:
        if control not in parser.label_for:
            p.append(f"form control {control} has no <label for>")
    if parser.captions != parser.tables:
        p.append("table without caption")
    for tag, a in parser.tags:
        for ref in str(a.get("aria-labelledby") or a.get("aria-describedby") or "").split():
            if ref not in parser.ids:
                p.append(f"<{tag}> references missing id {ref}")
    return p


@pytest.fixture
def audit() -> Callable[[str], list[str]]:
    return audit_html


CASH = "subscription.cash_vs_order"
# Evaluation ids hash (snapshot, profile, engine): with invaria-engine@0.10.0 they differ
# from the 0.1.0 ones (eval-f85cece3…, eval-c825196b…, eval-be1923e6…), the 0.2.0 ones
# (eval-a2575fc0…, eval-5da7c663…, eval-ce2ef2ae…), the 0.3.0 ones (eval-400ae743…,
# eval-00a59c51…, eval-cd2ada80…), the 0.4.0 ones (eval-0140cb2e…, eval-e259b717…,
# eval-b98ab0f4…), the 0.5.0 ones (eval-a1ec12fe…, eval-953049e4…, eval-23819823…) and the
# 0.6.0 ones (eval-083fa80f…, eval-d9c50cd2…, eval-00812ffa…), the 0.8.0 ones
# (eval-7cb19cf8…, eval-ba2c74e0…, eval-241ee881…) and the 0.9.0 ones (eval-ff7007c7…,
# eval-818c74cb…, eval-35d85a6f…); each engine version changed them.
K1 = "eval-5b0018ff288eca10903ee9bf94542355"
K2 = "eval-1dbe4cb6f07b054253ce2c6831d0f01b"
K3 = "eval-36c57ded242e6f9cd24f31abcdbe79f1"
EVERY_PAGE = [
    "/",
    "/?result=BREAK",
    "/operations/SUB-0001",
    "/operations/SUB-0001/coverage",
    "/operations/SUB-0001/as-known?valid_at=2026-10-01T17:00:00Z&known_at=2026-10-01T18:00:00Z",
    f"/evaluations/{K3}",
    f"/evaluations/{K1}/controls/{CASH}",
    f"/evaluations/{K3}/controls/{CASH}",
    f"/compare?left={K2}&right={K3}",
    "/evidence/obs-B2",
    "/evidence/cov-bank-k3",
    "/evidence/nope",
]


# ---------------------------------------------------------------- timeline


def test_timeline_orders_evidence_and_evaluations_by_knowledge(client: Any) -> None:
    reply = client.get("/v1/operations/SUB-0001/timeline")
    assert reply.status == 200
    view = TimelineView.model_validate_json(reply.body)
    evaluations = [e for e in view.entries if e.kind == "evaluation"]
    assert [(e.evaluation_id, e.result) for e in evaluations] == [
        (K1, "UNKNOWN"),
        (K2, "MATCH"),
        (K3, "BREAK"),
    ]
    assert [e.currency for e in evaluations] == ["superseded", "superseded", "current"]
    assert [e.previous_evaluation_id for e in evaluations] == [None, K1, K2]
    assert {e.engine_ref for e in evaluations} == {"invaria-engine@0.10.0"}
    page = client.get("/operations/SUB-0001").text  # the timeline is on the operation page
    assert "<code>invaria-engine@0.10.0</code>" in page
    kinds = [e.kind for e in view.entries]
    assert kinds == [
        "evidence",
        "evidence",
        "evaluation",
        "evidence",
        "evaluation",
        "evidence",
        "evaluation",
    ]
    assert view.entries[0].kind == "evidence" and view.entries[0].cause == "registered"
    b2 = view.entries[5]
    assert b2.kind == "evidence" and "obs-B2" in b2.evidence_ids
    times = [e.at for e in view.entries[1:]]
    assert times == sorted(times)  # type: ignore[type-var]


def test_evidence_precedes_an_evaluation_known_at_the_same_instant(client: Any, store: Any) -> None:
    number, cause, _, document = store.log[1]
    k1_known = store.snapshots["snap-K1"].known_at
    store.log[1] = (number, cause, k1_known, document)
    view = TimelineView.model_validate_json(client.get("/v1/operations/SUB-0001/timeline").body)
    kinds = [(e.kind, e.at) for e in view.entries[1:3]]
    assert kinds == [("evidence", k1_known), ("evaluation", k1_known)]


def test_timeline_page_links_each_step_to_its_comparison(client: Any) -> None:
    page = client.get("/operations/SUB-0001").text
    assert f'href="/compare?left={K1}&amp;right={K2}"' in page
    assert f'href="/compare?left={K2}&amp;right={K3}"' in page
    assert page.index("UNKNOWN") < page.index(">MATCH<") < page.rindex(">BREAK<")
    assert '<ol class="timeline">' in page


def test_timeline_is_truncated_to_the_latest_entries(make_client: Any) -> None:
    view = TimelineView.model_validate_json(
        make_client(max_items=2).get("/v1/operations/SUB-0001/timeline").body
    )
    assert view.truncated and len(view.entries) == 2
    assert view.entries[-1].kind == "evaluation" and view.entries[-1].evaluation_id == K3


# -------------------------------------------------------------- comparison


def test_comparison_k2_to_k3(client: Any) -> None:
    reply = client.get(f"/v1/evaluation-comparisons?left={K2}&right={K3}")
    assert reply.status == 200
    view = ComparisonView.model_validate_json(reply.body)
    assert (view.left.result, view.right.result) == ("MATCH", "BREAK")
    assert view.change.added_evidence == ["obs-B2"]
    assert view.change.removed_evidence == ["obs-B1"]
    assert view.change.replay_consistent
    (cash,) = [c for c in view.controls if c.control_id == CASH]
    assert (cash.left_status, cash.right_status, cash.changed) == ("PASS", "FAIL", True)
    assert [c.control_id for c in view.controls if c.changed] == [CASH]


def test_comparison_page_shows_both_sides_and_what_changed(client: Any) -> None:
    page = client.get(f"/compare?left={K2}&right={K3}").text
    assert "<caption>Conclusions side by side</caption>" in page
    assert "<caption>Controls side by side</caption>" in page
    assert re.search(rf"<th scope=\"row\">{re.escape(CASH)}</th>.*?PASS.*?FAIL.*?changed", page)
    assert 'href="/evidence/obs-B2"' in page and 'href="/evidence/obs-B1"' in page
    assert "does not claim a unique root cause" in page


def test_comparison_needs_both_sides_and_one_operation(
    client: Any, store: Any, corpus: Any
) -> None:
    assert client.get(f"/v1/evaluation-comparisons?left={K2}").status == 422
    other = store.evaluations[0].model_copy(update={"operation_ref": "SUB-9999"})
    other = other.model_copy(update={"evaluation_id": "eval-other"})
    store.evaluations.append(other)
    reply = client.get(f"/v1/evaluation-comparisons?left={K2}&right=eval-other")
    assert reply.status == 422
    assert json.loads(reply.body)["error"]["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------- conclusion pages


def test_conditions_come_before_any_narrative(client: Any) -> None:
    page = client.get(f"/evaluations/{K3}").text
    positions = [
        page.index(label)
        for label in (
            "<dt>Result</dt>",
            "<dt>Currency</dt>",
            "<dt>Economic cut (valid_at)</dt>",
            "<dt>Known at (known_at)</dt>",
            "<dt>Evaluation clock</dt>",
            "<dt>Coverage</dt>",
        )
    ]
    first_reason = min(page.index(c.reason) for c in client.app.service.get_evaluation(K3).controls)
    assert positions == sorted(positions) and positions[-1] < first_reason
    assert '<h2 id="controls-h">Controls</h2>' in page
    assert page.index("Conclusion and conditions") < page.index("controls-h")


def test_result_and_currency_are_written_as_text(client: Any) -> None:
    page = client.get(f"/evaluations/{K2}").text
    assert '<span class="badge result-match">MATCH</span>' in page
    assert '<span class="badge currency-superseded">superseded</span>' in page
    assert "superseded: a later evaluation is published" in page


def test_exact_delta_of_the_k3_break(client: Any) -> None:
    page = client.get(f"/evaluations/{K3}/controls/{CASH}").text
    assert '<span class="qty">-500.00 USD</span>' in page
    assert '<span class="qty">99500.00 USD</span>' in page
    assert '<span class="qty">100000.00 USD</span>' in page
    assert "Replay consistent" in page and "yes: replaying" in page


def test_missing_evidence_in_k1_names_the_bank(client: Any) -> None:
    page = client.get(f"/evaluations/{K1}/controls/{CASH}").text
    assert "Missing evidence: <strong>yes</strong>" in page
    assert re.search(r"cash_settled</th><td>bank-synthetic</td>.*?internally_checked", page)


def test_as_known_at_k2_gives_match(client: Any) -> None:
    page = client.get(
        "/operations/SUB-0001/as-known?valid_at=2026-10-01T17:00:00Z&known_at=2026-10-01T18:00:00Z"
    ).text
    assert '<span class="badge result-match">MATCH</span>' in page
    api = client.get(
        "/v1/operations/SUB-0001/conclusion?valid_at=2026-10-01T17:00:00Z"
        "&known_at=2026-10-01T18:00:00Z"
    )
    assert json.loads(api.body)["evaluation_id"] == K2


def test_new_evidence_shows_as_stale(client: Any, store: Any) -> None:
    store.stale = True
    page = client.get("/operations/SUB-0001").text
    assert '<span class="badge currency-stale">stale</span>' in page
    assert "stale: newer evidence arrived after publication" in page
    listing = OperationsView.model_validate_json(client.get("/v1/operations").body)
    assert listing.items[0].currency == "stale"


def test_operations_list_and_filter(client: Any) -> None:
    all_ops = OperationsView.model_validate_json(client.get("/v1/operations").body)
    assert [(i.operation_ref, i.result, i.evaluations) for i in all_ops.items] == [
        ("SUB-0001", "BREAK", 3)
    ]
    match = OperationsView.model_validate_json(client.get("/v1/operations?result=MATCH").body)
    assert match.items == [] and match.result_filter == "MATCH"
    assert "No operation matches." in client.get("/?result=MATCH").text
    assert client.get("/v1/operations?result=GREEN").status == 422


def test_operations_list_is_truncated_at_max_items(make_client: Any, store: Any) -> None:
    store.registered_only = ["SUB-0002", "SUB-0003"]
    full = OperationsView.model_validate_json(make_client().get("/v1/operations").body)
    assert [(i.operation_ref, i.result) for i in full.items] == [
        ("SUB-0001", "BREAK"),
        ("SUB-0002", None),
        ("SUB-0003", None),
    ]
    assert not full.truncated
    store.registered_only += ["SUB-0004", "SUB-0005"]
    reads: list[str] = []
    original: Callable[[str, str], list[EvaluationResult]] = store.evaluations_for

    def counted(tenant_id: str, operation_ref: str) -> list[EvaluationResult]:
        reads.append(operation_ref)
        return original(tenant_id, operation_ref)

    store.evaluations_for = counted
    small = make_client(max_items=2)
    view = OperationsView.model_validate_json(small.get("/v1/operations").body)
    assert view.truncated and len(view.items) == 2
    assert len(reads) == 3  # stops one past the limit instead of reading every operation
    assert "The list is truncated." in small.get("/").text


def test_coverage_page_lists_certificates_and_levels(client: Any) -> None:
    page = client.get("/operations/SUB-0001/coverage").text
    assert 'href="/evidence/cov-bank-k3"' in page
    assert "<caption>Coverage certificates applied</caption>" in page
    assert "Unmet requirements" in page and "<p>None.</p>" in page


# ------------------------------------------------------------- evidence


def test_evidence_shows_provenance_and_is_marked_untrusted(client: Any) -> None:
    page = client.get("/evidence/obs-B2").text
    assert "Untrusted source data." in page
    for label in ("Raw sha256", "Raw locator", "Parser", "Mapping", "Record key", "Revision"):
        assert f"<dt>{label}</dt>" in page
    assert 'href="/evidence/obs-B1"' in page  # supersedes
    assert "99500.00 USD" in page and "atoms 9950000, scale 2" in page


def test_source_content_is_escaped(client: Any, store: Any) -> None:
    evil = store.observations["obs-B2"].model_copy(
        update={
            "observation_id": "obs-evil",
            "provenance": store.observations["obs-B2"].provenance.model_copy(
                update={"raw_locator": '"><script>alert(1)</script><a href="x'}
            ),
        }
    )
    store.observations["obs-evil"] = evil
    reply = client.get("/evidence/obs-evil")
    assert reply.status == 200
    assert "<script>" not in reply.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in reply.text
    assert "&quot;&gt;" in reply.text


def test_builder_escapes_text_and_attributes() -> None:
    markup = el("a", "<b>&", href='x" onclick="y', title="<t>")
    assert markup == '<a href="x&quot; onclick=&quot;y" title="&lt;t&gt;">&lt;b&gt;&amp;</a>'


@pytest.mark.parametrize(
    ("atoms", "scale", "unit", "text"),
    [
        ("-50000", 2, "USD", "-500.00 USD"),
        ("5", 2, "USD", "0.05 USD"),
        ("-5", 2, "USD", "-0.05 USD"),
        ("1000", 0, "FUND_SHARE", "1000 FUND_SHARE"),
        ("10000000000", 7, "DEMOA", "1000.0000000 DEMOA"),
        ("0", 2, "USD", "0.00 USD"),
    ],
)
def test_quantities_keep_exact_scale_and_unit(atoms: str, scale: int, unit: str, text: str) -> None:
    assert (
        quantity(Quantity(atoms=atoms, scale=scale, unit=unit))
        == f'<span class="qty">{text}</span>'
    )


# ------------------------------------------------------------- security


def test_every_page_requires_the_token(client: Any) -> None:
    for url in ["/", "/operations/SUB-0001", "/v1/operations", f"/v1/evaluations/{K3}"]:
        reply = client.get(url, auth=False)
        assert reply.status == 401, url
    api = client.get("/v1/operations", auth=False)
    error = json.loads(api.body)["error"]
    assert error["code"] == "UNAUTHENTICATED" and error["request_id"]
    assert (
        client.get("/", auth=False, headers={"Cookie": "invaria_console=" + "x" * 40}).status == 401
    )


def test_bearer_token_works_for_the_api(client: Any) -> None:
    reply = client.get(
        "/v1/operations", auth=False, headers={"Authorization": "Bearer " + "t" * 40}
    )
    assert reply.status == 200


def test_login_exchanges_the_token_for_a_strict_cookie(client: Any) -> None:
    reply = client.get("/login?token=" + "t" * 40, auth=False)
    assert reply.status == 303 and reply.headers["Location"] == "/"
    cookie = reply.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Max-Age=28800" in cookie
    assert f"invaria_console={SESSION};" in cookie and TOKEN not in cookie
    raw_token_cookie = {"Cookie": f"invaria_console={TOKEN}"}
    assert client.get("/", auth=False, headers=raw_token_cookie).status == 401
    assert client.get("/login?token=wrong", auth=False).status == 401


def test_only_get_and_head(client: Any) -> None:
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        reply = client.get("/v1/operations", method=method)
        assert reply.status == 405 and reply.headers["Allow"] == "GET, HEAD"
    head = client.get("/v1/operations", method="HEAD")
    assert head.status == 200 and head.body == b"" and int(head.headers["Content-Length"]) > 0


def test_foreign_host_is_refused_even_with_the_token(client: Any) -> None:
    for host in ("evil.example:8765", "127.0.0.1:9999", ""):
        assert client.get("/v1/operations", host=host).status == 421


def test_strict_headers_on_every_response(client: Any) -> None:
    for url in ["/", "/v1/operations", "/nope", "/static/console.css"]:
        headers = client.get(url).headers
        assert "script-src" not in headers["Content-Security-Policy"]
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["Cache-Control"] == "no-store"


def test_scope_tenant_and_validation_errors(make_client: Any) -> None:
    no_evidence = make_client(scopes=["operations:read"])
    assert no_evidence.get("/v1/evidence/obs-B1").status == 403
    assert no_evidence.get("/evidence/obs-B1").status == 403
    stranger = make_client(tenant_id="tenant-other")
    for url in ["/v1/operations/SUB-0001", f"/v1/evaluations/{K3}", "/v1/evidence/obs-B1"]:
        assert stranger.get(url).status == 404, url
    client = make_client()
    for url in [
        "/v1/operations/SUB-0001;DROP",
        "/v1/operations/SUB-0001/conclusion?valid_at=yesterday&known_at=2026-10-02T00:00:00Z",
        "/v1/operations/SUB-0001/conclusion?valid_at=2026-10-01T17:00:00Z",
        "/v1/operations?result=MATCH&result=BREAK",
        "/v1/operations?unexpected=1",
        "/v1/operations/SUB-0001?debug=1",
    ]:
        reply = client.get(url)
        assert reply.status == 422, url
        assert json.loads(reply.body)["error"]["code"] == "VALIDATION_ERROR"
    for url in ["/v1/nope", "/operations/SUB-0001/x/y", "/v1/operations//x", "/admin"]:
        assert client.get(url).status == 404, url


def test_internal_errors_keep_headers_and_hide_details(store: Any, make_client: Any) -> None:
    client = make_client()

    def boom(*_: Any) -> Any:
        raise RuntimeError("secret detail")

    store.load_snapshot = boom
    reply = client.get("/v1/operations/SUB-0001")
    assert reply.status == 500 and b"secret detail" not in reply.body
    assert "Content-Security-Policy" in reply.headers


def test_no_write_surface_in_any_page(client: Any) -> None:
    for url in EVERY_PAGE:
        page = client.get(url).text
        assert 'method="post"' not in page.lower(), url
        for word in ("resolve", "waive", "approve", "reconcile", "delete"):
            assert f">{word}" not in page.lower(), (url, word)


def test_credentials_of_other_lengths_are_rejected(client: Any) -> None:
    for value in ("", "t", "t" * 39, "t" * 41, SESSION[:-1], SESSION + "0"):
        assert (
            client.get("/", auth=False, headers={"Cookie": f"invaria_console={value}"}).status
            == 401
        )
        bearer = {"Authorization": f"Bearer {value}"}
        assert client.get("/v1/operations", auth=False, headers=bearer).status == 401


def test_too_many_query_parameters(client: Any) -> None:
    query = "&".join(f"p{i}=1" for i in range(9))
    reply = client.get(f"/v1/operations?{query}")
    assert reply.status == 422 and json.loads(reply.body)["error"]["code"] == "VALIDATION_ERROR"


def test_other_tenant_sees_nothing_through_the_new_use_cases(make_client: Any) -> None:
    stranger = make_client(tenant_id="tenant-other")
    listing = OperationsView.model_validate_json(stranger.get("/v1/operations").body)
    assert listing.items == []
    for url in [
        "/v1/operations/SUB-0001/timeline",
        f"/v1/evaluation-comparisons?left={K2}&right={K3}",
        f"/compare?left={K2}&right={K3}",
        f"/evaluations/{K3}/controls/{CASH}",
        "/operations/SUB-0001",
    ]:
        reply = stranger.get(url)
        assert reply.status == 404, url
        assert b"BREAK" not in reply.body and b"obs-B2" not in reply.body


@pytest.mark.parametrize("token", ["short", "t" * 31, "t" * 40 + ";x", "t" * 40 + "\r\nX: y"])
def test_unsafe_or_short_tokens_are_refused(store: Any, token: str) -> None:
    with pytest.raises(ValueError):
        ConsoleApp(store, token=token, allowed_hosts={"127.0.0.1:1"})


def test_stored_identifiers_are_quoted_in_links(client: Any, store: Any) -> None:
    b2 = store.observations["obs-B2"]
    store.observations["obs-x:1"] = b2.model_copy(
        update={"observation_id": "obs-x:1", "supersedes": "obs-a.b_c:d"}
    )
    page = client.get("/evidence/obs-x:1").text
    assert 'href="/evidence/obs-a.b_c%3Ad"' in page


def test_request_handler_has_a_timeout() -> None:
    from invaria.console.server import _QuietHandler

    assert _QuietHandler.timeout == 10


def test_unexpected_errors_roll_back_the_connection(store: Any) -> None:
    class Conn:
        closed = False
        rolled_back = 0

        def rollback(self) -> None:
            self.rolled_back += 1

    conn = Conn()
    store.conn = conn

    def boom(*_: Any) -> Any:
        raise RuntimeError("aborted transaction")

    store.epoch_log = boom
    service = QueryService(store, access())
    with pytest.raises(RuntimeError):
        service.get_timeline(OP)
    assert conn.rolled_back == 1


# ----------------------------------------------------------- accessibility


@pytest.mark.parametrize("url", EVERY_PAGE)
def test_pages_pass_structural_accessibility_checks(
    client: Any, audit: Callable[[str], list[str]], url: str
) -> None:
    reply = client.get(url)
    assert reply.headers["Content-Type"].startswith("text/html")
    assert audit(reply.text) == [], url


def test_error_pages_are_accessible_too(client: Any, audit: Callable[[str], list[str]]) -> None:
    for reply in (client.get("/nope"), client.get("/", auth=False)):
        assert reply.status in (401, 404)
        assert audit(reply.text) == []


def test_audit_detects_problems(audit: Callable[[str], list[str]]) -> None:
    bad = (
        "<!doctype html><html><head><title>x</title></head><body><main>"
        "<h1>a</h1><h3>b</h3><table><tr><th>x</th></tr></table>"
        '<input id="q"><a href="/"></a><form method="post"></form><script></script>'
        "</main></body></html>"
    )
    problems = audit(bad)
    for expected in (
        "missing <html lang>",
        "heading level skips from h1 to h3",
        "table without caption",
        "<th> without scope",
        "form control q has no <label for>",
        "link without text",
        "form that is not GET",
        "script element",
        "missing <nav>",
        "skip link to #main is not the first link",
    ):
        assert expected in problems


# ----------------------------------------------------------------- browser

CHROME = next(
    (p for p in ("google-chrome", "chromium", "chromium-browser") if shutil.which(p)), None
)


@pytest.fixture
def base(store: Any) -> Iterator[str]:
    if os.environ.get("INVARIA_BROWSER_E2E") != "1" or CHROME is None:
        pytest.skip("set INVARIA_BROWSER_E2E=1 with Chrome or Chromium installed")
    server, login = build(QueryService(store, access(principal_id="browser-e2e")), 0, TOKEN)
    app = server.get_app()
    assert app is not None
    cookie = f"invaria_console_{server.server_port}={session_for(TOKEN)}"

    def signed_in(environ: dict[str, Any], start_response: Any) -> Any:
        environ["HTTP_COOKIE"] = cookie  # the browser test skips the sign-in redirect
        return app(environ, start_response)

    server.set_app(signed_in)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield login.split("/login", 1)[0]
    finally:
        server.shutdown()
        server.server_close()


def chrome(tmp_path: Path, *args: str) -> str:
    assert CHROME is not None
    result = subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            f"--user-data-dir={tmp_path / 'profile'}",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout


@pytest.mark.browser
def test_console_renders_in_a_real_browser(base: str, tmp_path: Path) -> None:
    dom = chrome(tmp_path, "--dump-dom", f"{base}/operations/SUB-0001")
    assert "<h1>Operation SUB-0001</h1>" in dom
    assert '<ol class="timeline">' in dom and "result-break" in dom
    comparison = chrome(tmp_path, "--dump-dom", f"{base}/compare?left={K2}&right={K3}")
    assert "Controls side by side" in comparison
    out = Path(os.environ.get("INVARIA_BROWSER_SHOTS", tmp_path))
    out.mkdir(parents=True, exist_ok=True)
    for name, width, url in (
        ("operation-mobile", 390, "/operations/SUB-0001"),
        ("comparison-desktop", 1280, f"/compare?left={K2}&right={K3}"),
    ):
        shot = out / f"{name}.png"
        chrome(
            tmp_path,
            f"--screenshot={shot}",
            f"--window-size={width},1600",
            "--hide-scrollbars",
            f"{base}{url}",
        )
        assert shot.stat().st_size > 10_000
