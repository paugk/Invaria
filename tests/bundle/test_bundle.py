"""Directory bundles and offline R1 verification: replay, tampering, isolation."""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from invaria.bundle import build_bundle, verify_bundle
from invaria.bundle import verify as verify_module
from invaria.cli import main
from invaria.contracts import OperationProfile, SnapshotRef, parse_contract
from invaria.contracts.bundle import BundleEvidence
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.evaluate import ENGINE_REF, EvaluationInputs, evaluate, replay

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CORPUS_DIR = FIXTURES / "corpus/subscription-synthetic"
# Historical golden: K3 as recorded with the retired invaria-engine@0.1.0. It is
# kept as evidence and still reproduces with that exact engine.
GOLDEN_K3 = FIXTURES / "bundles/K3"
GOLDEN_K3_MANIFEST_SHA256 = "bec6969130fed2f8d64ca92c65fb6c4808c5d5beae65e83bd1283cd61c7b447f"
# Historical golden of the retired invaria-engine@0.2.0 (retired by 0.3.0).
GOLDEN_K3_0_2_0 = FIXTURES / "bundles/K3-engine-0.2.0"
GOLDEN_K3_0_2_0_MANIFEST_SHA256 = "bd74fa58ca71108ee395e634bd303f135b0033bfbe0ac1215b5c2a1d0b42367d"
# Historical golden of the retired invaria-engine@0.3.0 (retired by 0.4.0).
GOLDEN_K3_0_3_0 = FIXTURES / "bundles/K3-engine-0.3.0"
GOLDEN_K3_0_3_0_MANIFEST_SHA256 = "da810a88da49377cf6128ee05bc1d6e631f39a7444c55bdc6390ef2cd560d6ed"
# Historical golden of the retired invaria-engine@0.4.0 (retired by 0.5.0).
GOLDEN_K3_0_4_0 = FIXTURES / "bundles/K3-engine-0.4.0"
GOLDEN_K3_0_4_0_MANIFEST_SHA256 = "7d574c8d89042f9435562a47061849c541d0de4af2c1bc2aeef6da4b47fe73fd"
# Historical golden of the retired invaria-engine@0.5.0 (retired by 0.6.0).
GOLDEN_K3_0_5_0 = FIXTURES / "bundles/K3-engine-0.5.0"
GOLDEN_K3_0_5_0_MANIFEST_SHA256 = "69c8c2f924b26c8a2f8ba528e12385af725597cfa9729bab2dfd23fa07471c21"
GOLDEN_K3_0_6_0 = FIXTURES / "bundles/K3-engine-0.6.0"
GOLDEN_K3_0_6_0_MANIFEST_SHA256 = "3f634c702b6a69964897560173962f72b585cd1be542639bce8529280749caa5"
GOLDEN_K3_0_7_0 = FIXTURES / "bundles/K3-engine-0.7.0"
GOLDEN_K3_0_7_0_MANIFEST_SHA256 = "aa28dbccc4c71345481d639bb74fb570624b95b1e0fac2a04cb9ab4cc251f188"
GOLDEN_K3_0_8_0 = FIXTURES / "bundles/K3-engine-0.8.0"
GOLDEN_K3_0_8_0_MANIFEST_SHA256 = "96571b25184a514ae6402ef92ff91a4d96a42a0ac2675a2e5078e1b5662e4eb7"
GOLDEN_K3_0_9_0 = FIXTURES / "bundles/K3-engine-0.9.0"
GOLDEN_K3_0_9_0_MANIFEST_SHA256 = "3cbc4a18ccbec62c46ff28ca158c0600ceaf91eff257e68072dc92652115a033"
# Current golden: the same K3 evaluated with invaria-engine@0.10.0. Its
# profile (fund-subscription-synthetic@1.0.0) declares neither absence_needs_chain_scope nor
# completeness_needs_coverage, so the conclusion, controls and assumptions are those of 0.8.0.
GOLDEN_K3_CURRENT = FIXTURES / "bundles/K3-engine-0.10.0"
GOLDEN_K3_CURRENT_MANIFEST_SHA256 = (
    "d612eb4ed9b1a951b9d2a94558d2f3f82599113899dfeef79f7156abcdf4ba64"
)


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


@pytest.fixture
def k3(tmp_path: Path) -> Path:
    target = tmp_path / "K3"
    shutil.copytree(GOLDEN_K3_CURRENT, target)
    return target


def dumps(document: Any) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def edit_json(bundle: Path, name: str, change: Callable[[Any], None]) -> None:
    path = bundle / name
    document = json.loads(path.read_text("utf-8"))
    change(document)
    path.write_bytes(dumps(document))


def rehash(bundle: Path) -> None:
    """Update the manifest so every artifact hash matches again (a consistent forger)."""

    def update(manifest: Any) -> None:
        for artifact in manifest["artifacts"]:
            data = (bundle / artifact["path"]).read_bytes()
            artifact["sha256"] = hashlib.sha256(data).hexdigest()

    edit_json(bundle, "manifest.json", update)


# ------------------------------------------------------------------ replay


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
def test_every_scenario_bundle_reproduces(corpus: Corpus, tmp_path: Path, scenario_id: str) -> None:
    inputs = corpus.inputs_for(scenario_id)
    out = tmp_path / scenario_id
    build_bundle(out, inputs, evaluate(inputs), mode=corpus.scenarios[scenario_id].query_mode)
    report = verify_bundle(out)
    assert report.status == "REPRODUCED", report.reasons
    assert report.financial_result == corpus.scenarios[scenario_id].expected.result
    assert report.artifacts_checked == 4 and report.trust == "unanchored"


def test_reproducing_a_break_is_reproduced_not_failure() -> None:
    report = verify_bundle(
        GOLDEN_K3_CURRENT, expected_manifest_sha256=GOLDEN_K3_CURRENT_MANIFEST_SHA256
    )
    assert (report.status, report.financial_result) == ("REPRODUCED", "BREAK")
    assert report.trust == "anchored_by_expected_manifest_sha256"
    assert (report.local_engine_ref, report.engine_status) == (ENGINE_REF, "current")
    assert report.engine_implementation == "current"


def test_historical_k3_reproduces_with_its_retired_engine_only() -> None:
    """The 0.1.0 conclusion is reproduced with 0.1.0 itself, and the report says that this
    does not validate it under the current semantics."""
    report = verify_bundle(GOLDEN_K3, expected_manifest_sha256=GOLDEN_K3_MANIFEST_SHA256)
    assert (report.status, report.financial_result) == ("REPRODUCED", "BREAK")
    assert (report.local_engine_ref, report.engine_status) == ("invaria-engine@0.1.0", "retired")
    # The result coincided; the code that ran is a compatibility implementation, and the
    # report never presents it as the historical code.
    assert report.engine_implementation == "compatibility"
    assert any("does not validate it under the current semantics" in r for r in report.reasons)
    assert any("identity is not claimed" in r for r in report.reasons)


def test_golden_bundle_matches_a_fresh_build(corpus: Corpus, tmp_path: Path) -> None:
    inputs = corpus.inputs_for("K3")
    out = tmp_path / "fresh"
    build_bundle(out, inputs, evaluate(inputs), mode="as_known_now")
    golden = {p.name: p.read_bytes() for p in GOLDEN_K3_CURRENT.iterdir()}
    fresh = {p.name: p.read_bytes() for p in out.iterdir()}
    assert fresh == golden, "bundle format or engine output drifted; regenerate deliberately"
    assert hashlib.sha256(golden["manifest.json"]).hexdigest() == (
        GOLDEN_K3_CURRENT_MANIFEST_SHA256
    )


def test_historical_golden_is_rebuilt_byte_for_byte_by_its_exact_engine(
    corpus: Corpus, tmp_path: Path
) -> None:
    inputs = corpus.inputs_for("K3")
    out = tmp_path / "historical"
    build_bundle(out, inputs, replay(inputs, "invaria-engine@0.1.0"), mode="as_known_now")
    golden = {p.name: p.read_bytes() for p in GOLDEN_K3.iterdir()}
    assert {p.name: p.read_bytes() for p in out.iterdir()} == golden
    assert hashlib.sha256(golden["manifest.json"]).hexdigest() == GOLDEN_K3_MANIFEST_SHA256


@pytest.mark.parametrize(
    ("older", "kept"),
    [
        ("K3", 5),
        ("K3-engine-0.2.0", 7),
        ("K3-engine-0.3.0", 7),
        ("K3-engine-0.4.0", 9),
        ("K3-engine-0.5.0", 9),
        ("K3-engine-0.6.0", 12),
        ("K3-engine-0.7.0", 12),
        ("K3-engine-0.8.0", 15),
        ("K3-engine-0.9.0", 15),
    ],
    ids=["0.1.0", "0.2.0", "0.3.0", "0.4.0", "0.5.0", "0.6.0", "0.7.0", "0.8.0", "0.9.0"],
)
def test_later_engines_keep_the_k3_conclusion_and_controls(older: str, kept: int) -> None:
    """Only the version, its identifier and the stated assumptions change for K3. 0.4.0
    keeps the first assumptions, rewords the clawback one of 0.3.0 and adds the
    unresolved-participant one; 0.5.0 adds the bounded quarantine one;
    0.6.0 states no quarantine policy for K3's profile, which does not scope its quarantine,
    and adds the three integrity rules of the declared policy to the nine of 0.4.0; 0.7.0
    keeps those twelve and adds the muxed-account rule for a profile that does not declare it
    and the memo rule; 0.8.0 rewords the muxed-account rule and adds the provenance
    rule, and its versions also state the mappings that produced the evidence, which
    here are exactly those the profile admits; 0.9.0 changes nothing for K3, whose profile
    does not declare absence_needs_chain_scope."""
    base = FIXTURES / "bundles" / older
    old = json.loads((base / "evaluation.json").read_text("utf-8"))
    new = json.loads((GOLDEN_K3_CURRENT / "evaluation.json").read_text("utf-8"))
    assert (old["result"], old["controls"]) == (new["result"], new["controls"])
    changed = {k for k in old if old[k] != new[k]}
    assert {"evaluation_id", "versions"} <= changed <= {"assumptions", "evaluation_id", "versions"}
    assert new["assumptions"][:kept] == old["assumptions"][:kept]
    assert len(new["assumptions"]) == 15
    assert {k for k in old["versions"] if old["versions"][k] != new["versions"][k]} == {
        "engine_ref"
    }
    assert set(new["versions"]) - set(old["versions"]) <= {"evidence_mapping_refs"}
    evidence = json.loads((GOLDEN_K3_CURRENT / "evidence.json").read_text("utf-8"))
    assert new["versions"]["evidence_mapping_refs"] == sorted(
        {o["provenance"]["mapping_ref"] for o in evidence["observations"]}
    )
    for name in ("snapshot.json", "evidence.json", "profile.json"):
        assert (base / name).read_bytes() == (GOLDEN_K3_CURRENT / name).read_bytes()


@pytest.mark.parametrize(
    ("golden", "sha", "engine"),
    [
        (GOLDEN_K3_0_2_0, GOLDEN_K3_0_2_0_MANIFEST_SHA256, "invaria-engine@0.2.0"),
        (GOLDEN_K3_0_3_0, GOLDEN_K3_0_3_0_MANIFEST_SHA256, "invaria-engine@0.3.0"),
        (GOLDEN_K3_0_4_0, GOLDEN_K3_0_4_0_MANIFEST_SHA256, "invaria-engine@0.4.0"),
        (GOLDEN_K3_0_5_0, GOLDEN_K3_0_5_0_MANIFEST_SHA256, "invaria-engine@0.5.0"),
        (GOLDEN_K3_0_6_0, GOLDEN_K3_0_6_0_MANIFEST_SHA256, "invaria-engine@0.6.0"),
        (GOLDEN_K3_0_7_0, GOLDEN_K3_0_7_0_MANIFEST_SHA256, "invaria-engine@0.7.0"),
        (GOLDEN_K3_0_8_0, GOLDEN_K3_0_8_0_MANIFEST_SHA256, "invaria-engine@0.8.0"),
        (GOLDEN_K3_0_9_0, GOLDEN_K3_0_9_0_MANIFEST_SHA256, "invaria-engine@0.9.0"),
    ],
    ids=["0.2.0", "0.3.0", "0.4.0", "0.5.0", "0.6.0", "0.7.0", "0.8.0", "0.9.0"],
)
def test_retired_goldens_reproduce_and_rebuild_with_their_engines(
    corpus: Corpus, tmp_path: Path, golden: Path, sha: str, engine: str
) -> None:
    report = verify_bundle(golden, expected_manifest_sha256=sha)
    assert (report.status, report.engine_status, report.engine_implementation) == (
        "REPRODUCED",
        "retired",
        "compatibility",
    )
    inputs = corpus.inputs_for("K3")
    out = tmp_path / "k3-retired"
    build_bundle(out, inputs, replay(inputs, engine), mode="as_known_now")
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {
        p.name: p.read_bytes() for p in golden.iterdir()
    }


def test_bundle_contains_only_snapshot_members(corpus: Corpus) -> None:
    evidence = json.loads((GOLDEN_K3 / "evidence.json").read_text("utf-8"))
    snapshot = corpus.scenarios["K3"].snapshot
    assert sorted(o["observation_id"] for o in evidence["observations"]) == (
        snapshot.observation_ids
    )
    assert sorted(c["coverage_id"] for c in evidence["coverage"]) == snapshot.coverage_ids


def test_k2_historical_bundle_excludes_later_correction(corpus: Corpus, tmp_path: Path) -> None:
    inputs = corpus.inputs_for("K2-historical")
    out = tmp_path / "hist"
    build_bundle(out, inputs, evaluate(inputs), mode="as_known")
    assert "obs-B2" not in (out / "evidence.json").read_text("utf-8")
    assert verify_bundle(out).financial_result == "MATCH"


def test_verification_runs_with_network_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*_: Any, **__: Any) -> Any:
        raise AssertionError("network access attempted during verification")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    report = verify_bundle(GOLDEN_K3)
    assert report.status == "REPRODUCED"


def test_build_refuses_non_empty_destination(corpus: Corpus, k3: Path) -> None:
    inputs = corpus.inputs_for("K3")
    with pytest.raises(FileExistsError):
        build_bundle(k3, inputs, evaluate(inputs), mode="as_known_now")


# --------------------------------------------------------------- REJECTED


def test_altered_byte_is_rejected(k3: Path) -> None:
    data = bytearray((k3 / "evidence.json").read_bytes())
    data[data.index(b"9950000")] = ord("8")
    (k3 / "evidence.json").write_bytes(bytes(data))
    report = verify_bundle(k3)
    assert report.status == "REJECTED" and "sha256 mismatch for evidence.json" in report.reasons[0]


def test_undeclared_file_is_rejected(k3: Path) -> None:
    (k3 / "run.py").write_text("print('never executed')", "utf-8")
    report = verify_bundle(k3)
    assert report.status == "REJECTED" and "undeclared" in report.reasons[0]


def test_symlinked_artifact_is_rejected(k3: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_bytes((k3 / "profile.json").read_bytes())
    (k3 / "profile.json").unlink()
    (k3 / "profile.json").symlink_to(outside)
    assert verify_bundle(k3).status == "REJECTED"


def test_symlinked_directory_is_rejected(k3: Path, tmp_path: Path) -> None:
    (k3 / "extra").symlink_to(tmp_path, target_is_directory=True)
    assert verify_bundle(k3).status == "REJECTED"


def test_bundle_root_must_be_a_directory(tmp_path: Path) -> None:
    file = tmp_path / "bundle"
    file.write_text("not a bundle", "utf-8")
    assert verify_bundle(file).status == "REJECTED"


@pytest.mark.parametrize("path", ["../profile.json", "/etc/passwd", "a/../../b.json", ".hidden"])
def test_hostile_manifest_path_is_rejected(k3: Path, path: str) -> None:
    edit_json(k3, "manifest.json", lambda m: m["artifacts"][0].update(path=path))
    report = verify_bundle(k3)
    assert report.status == "REJECTED" and "manifest.json" in report.reasons[0]


def test_duplicate_key_and_float_in_manifest_are_rejected(k3: Path) -> None:
    text = (k3 / "manifest.json").read_text("utf-8")
    (k3 / "manifest.json").write_text(
        text.replace('{"artifacts"', '{"bundle_id":"x","artifacts"', 1), "utf-8"
    )
    assert verify_bundle(k3).status == "REJECTED"
    (k3 / "manifest.json").write_text(
        text.replace('"schema_version":"1.0"', '"schema_version":"1.0","x":0.5', 1), "utf-8"
    )
    assert verify_bundle(k3).status == "REJECTED"


def test_extra_evidence_member_is_rejected(k3: Path, corpus: Corpus) -> None:
    stranger = corpus.journals["source-conflict"]["obs-B1-conflicting-resend"]
    edit_json(
        k3, "evidence.json", lambda e: e["observations"].append(stranger.model_dump(mode="json"))
    )
    rehash(k3)
    report = verify_bundle(k3)
    assert report.status == "REJECTED" and "membership" in report.reasons[0]


def test_inconsistent_versions_are_rejected(k3: Path) -> None:
    edit_json(
        k3,
        "manifest.json",
        lambda m: m["versions"].update(rules_ref="subscription-synthetic-rules@9.0.0"),
    )
    report = verify_bundle(k3)
    assert report.status == "REJECTED" and "versions" in report.reasons[0]


def test_invalid_artifact_contract_is_rejected(k3: Path) -> None:
    (k3 / "profile.json").write_bytes(b'{"not": "a profile"}')
    rehash(k3)
    report = verify_bundle(k3)
    assert report.status == "REJECTED" and "profile.json" in report.reasons[0]


def test_size_limit_is_enforced(k3: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_module, "MAX_ARTIFACT_BYTES", 100)
    assert verify_bundle(k3).status == "REJECTED"


# ------------------------------------------------------------- INCOMPLETE


def test_missing_artifact_is_incomplete(k3: Path) -> None:
    (k3 / "profile.json").unlink()
    report = verify_bundle(k3)
    assert report.status == "INCOMPLETE" and "profile.json" in report.reasons[0]


def test_r2_on_an_r1_bundle_is_incomplete(k3: Path) -> None:
    report = verify_bundle(k3, level="R2")
    assert report.status == "INCOMPLETE" and "R2" in report.reasons[0]


def test_foreign_engine_is_incomplete_and_not_executed(k3: Path) -> None:
    edit_json(
        k3,
        "evaluation.json",
        lambda e: e["versions"].update(engine_ref="someone-else-engine@1.0.0"),
    )
    edit_json(
        k3, "manifest.json", lambda m: m["versions"].update(engine_ref="someone-else-engine@1.0.0")
    )
    rehash(k3)
    report = verify_bundle(k3)
    assert report.status == "INCOMPLETE" and "not available locally" in report.reasons[0]


# --------------------------------------------------------------- MISMATCH


def test_tampered_evidence_with_updated_hashes_is_mismatch(k3: Path) -> None:
    def pay_in_full(evidence: Any) -> None:
        for observation in evidence["observations"]:
            if observation["observation_id"] == "obs-B2":
                observation["payload"]["amount"]["atoms"] = "10000000"

    edit_json(k3, "evidence.json", pay_in_full)
    rehash(k3)
    report = verify_bundle(k3)
    assert report.status == "MISMATCH"
    assert "replayed result MATCH vs recorded BREAK" in report.reasons[0]
    assert report.financial_result == "BREAK"  # recorded value, reported separately


def test_tampered_recorded_result_is_mismatch(k3: Path) -> None:
    def claim_match(evaluation: Any) -> None:
        evaluation["result"] = "MATCH"
        for control in evaluation["controls"]:
            if control["control_id"] == "subscription.cash_vs_order":
                control.update(status="PASS", reason_code="EXACT_MATCH", delta=None)

    edit_json(k3, "evaluation.json", claim_match)
    edit_json(k3, "manifest.json", lambda m: m.update(expected_result="MATCH"))
    rehash(k3)
    report = verify_bundle(k3)
    assert report.status == "MISMATCH" and "subscription.cash_vs_order" in report.reasons[0]


# --------------------------------------------------------------- UNTRUSTED


def test_consistent_forgery_is_caught_only_by_anchor(k3: Path) -> None:
    def pay_in_full(evidence: Any) -> None:
        for observation in evidence["observations"]:
            if observation["observation_id"] == "obs-B2":
                observation["payload"]["amount"]["atoms"] = "10000000"

    edit_json(k3, "evidence.json", pay_in_full)
    replayed = evaluate(load_corpus(CORPUS_DIR).inputs_for("K3"))  # untouched reference
    assert replayed.result.result == "BREAK"
    # A forger who also rewrites the evaluation produces an internally consistent bundle:
    evidence = parse_contract(BundleEvidence, (k3 / "evidence.json").read_text("utf-8"))
    forged = evaluate(
        EvaluationInputs(
            snapshot=parse_contract(SnapshotRef, (k3 / "snapshot.json").read_text("utf-8")),
            profile=parse_contract(OperationProfile, (k3 / "profile.json").read_text("utf-8")),
            observations={o.observation_id: o for o in evidence.observations},
            coverage={c.coverage_id: c for c in evidence.coverage},
            identity_links={link.link_id: link for link in evidence.identity_links},
        )
    )
    (k3 / "evaluation.json").write_bytes(dumps(forged.result.model_dump(mode="json")))
    edit_json(k3, "manifest.json", lambda m: m.update(expected_result="MATCH"))
    rehash(k3)
    assert verify_bundle(k3).status == "REPRODUCED"  # hashes alone prove no authenticity
    anchored = verify_bundle(k3, expected_manifest_sha256=GOLDEN_K3_CURRENT_MANIFEST_SHA256)
    assert anchored.status == "UNTRUSTED"


def test_wrong_anchor_is_untrusted() -> None:
    report = verify_bundle(GOLDEN_K3, expected_manifest_sha256="0" * 64)
    assert report.status == "UNTRUSTED"


# --------------------------------------------------------------------- CLI


def test_cli_bundle_and_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "K2"
    assert main(["bundle", str(CORPUS_DIR), "K2", str(out)]) == 0
    assert "result MATCH" in capsys.readouterr().out
    assert main(["verify", str(out)]) == 0
    assert "status REPRODUCED | financial result MATCH" in capsys.readouterr().out
    assert main(["verify", str(GOLDEN_K3), "--expect-manifest-sha256", "f" * 64]) == 1
    assert main(["bundle", str(CORPUS_DIR), "K2", str(out)]) == 2  # refuses to overwrite


def test_tampered_detail_with_same_result_is_mismatch(k3: Path) -> None:
    def smaller_delta(evaluation: Any) -> None:
        for control in evaluation["controls"]:
            if control["control_id"] == "subscription.cash_vs_order":
                control["delta"]["atoms"] = "-40000"  # still FAIL, still BREAK overall

    edit_json(k3, "evaluation.json", smaller_delta)
    rehash(k3)
    report = verify_bundle(k3)
    assert report.status == "MISMATCH"
    assert "none (other fields differ)" in report.reasons[0]


def test_the_two_k3_goldens_differ_only_in_engine_derived_fields(tmp_path: Path) -> None:
    """The manifest differs in the bundle id, the engine and the evaluation's hash;
    disclosure, query, expected result, integrity and every other artifact are the same,
    so the trust conditions do not change."""
    old = json.loads((GOLDEN_K3 / "manifest.json").read_text("utf-8"))
    new = json.loads((GOLDEN_K3_CURRENT / "manifest.json").read_text("utf-8"))
    assert {k for k in old if old[k] != new[k]} == {"artifacts", "bundle_id", "versions"}
    assert {k for k in old["versions"] if old["versions"][k] != new["versions"][k]} == {
        "engine_ref"
    }
    changed = [a["path"] for a, b in zip(old["artifacts"], new["artifacts"], strict=True) if a != b]
    assert changed == ["evaluation.json"]
    reports = [
        verify_bundle(GOLDEN_K3, expected_manifest_sha256=GOLDEN_K3_MANIFEST_SHA256),
        verify_bundle(
            GOLDEN_K3_CURRENT, expected_manifest_sha256=GOLDEN_K3_CURRENT_MANIFEST_SHA256
        ),
    ]
    assert {(r.status, r.trust, r.level, r.artifacts_checked) for r in reports} == {
        ("REPRODUCED", "anchored_by_expected_manifest_sha256", "R1", 4)
    }
    # The two added assumptions are part of the reproduced document, not of any trust
    # decision: rewriting one is a MISMATCH even with consistent hashes.
    k3 = tmp_path / "K3"
    shutil.copytree(GOLDEN_K3_CURRENT, k3)
    edit_json(k3, "evaluation.json", lambda e: e["assumptions"].pop())
    rehash(k3)
    assert verify_bundle(k3).status == "MISMATCH"
