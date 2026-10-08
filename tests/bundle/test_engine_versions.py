"""Engine version policy at verification: current, retired, blocked, unknown.

The redemption 0.4.0 bundles in ``tests/fixtures/bundles/redemption-0.4.0`` were frozen on
2026-10-07 with the retired engine as emulated by the current code (``inv013=False``):
any later drift of that emulation breaks these tests. Their results equal the hand-written
expectations of the corpus, which 0.4.0 reproduced 46/46.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from invaria.bundle import build_bundle, verify_bundle
from invaria.cli import main
from invaria.contracts.base import parse_contract
from invaria.contracts.profile import parse_profile
from invaria.corpus_loader import load_corpus
from invaria.engine.common import quarantine_affecting
from invaria.engine.evaluate import evaluate, replay
from invaria.engine.versions import REDEMPTION_ENGINE_REF

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REDEMPTION = FIXTURES / "corpus/redemption-synthetic"
SUBSCRIPTION = FIXTURES / "corpus/subscription-synthetic"
FROZEN = FIXTURES / "bundles/redemption-0.4.0"
FROZEN_MANIFESTS = {
    "RD-CANCELLED": "a709355b37d24123de303c3e34542a1045a39965e8480b239efaae9ca0a0e007",
    "RD-SHORT-PAY": "11ad2218c9a883379102a733925b7ec0caf5f7eaaf081774d6d154960969341e",
}
HISTORICAL_K3 = FIXTURES / "bundles/K3"
# Frozen on 2026-10-07 with redemption 0.5.0 (profile 1.4.0) just before 0.6.0.
FROZEN_0_5_0 = FIXTURES / "bundles/redemption-0.5.0"
FROZEN_0_5_0_MANIFESTS = {
    "RD-CANCELLED": "361685f3f28d61e7705efa471d773a39b79ed56d9345de5dd060bd0b87495fad",
    "RD-SHORT-PAY": "c7b68a658d27d80a3fe529c06214e1807bd646cfb99dd617c34a62e0de18e97d",
}
# Frozen on 2026-10-07 with redemption 0.6.0 (profile 1.4.0) just before 0.7.0.
FROZEN_0_6_0 = FIXTURES / "bundles/redemption-0.6.0"
FROZEN_0_6_0_MANIFESTS = {
    "RD-CANCELLED": "7a336dff7913d49388af1345fa7bf073dbcd4b0a8b896e0eaaa7d77c91074d0d",
    "RD-PAID": "6446eaf505afa85012b5de229547a863275e40824a2f5d6b8719d39f8542139d",
    "RD-SHORT-PAY": "56ea0e961cdbb610f8d06b49ef461643c04b6c25da5962bd4511195255de89f3",
}
# Frozen on 2026-10-08 with redemption 0.8.0 (profile 1.5.0) just before 0.9.0.
FROZEN_0_8_0 = FIXTURES / "bundles/redemption-0.8.0"
FROZEN_0_8_0_MANIFESTS = {
    "RD-CANCELLED": "d16fb5c244ac20d0358c59f985b2b03e607283c8a53facfedfb6c98c4b09ceb5",
    "RD-PAID": "fb09c81fee0a3c489a7c58e8e5cde75fe66dd8822304e128b2aee3d110efb72b",
    "RD-SHORT-PAY": "e4f0527577f3536319d07189c147bfee1745fc4bb2ed87cad34af32ed2deac67",
}
# Frozen on 2026-10-07 with redemption 0.7.0 (profile 1.5.0) just before 0.8.0.
FROZEN_0_10_0 = FIXTURES / "bundles/redemption-0.10.0"
FROZEN_0_10_0_MANIFESTS = {
    "RD-CANCELLED": "8db23771fdfa251f36c5962b4a2635fc2c4a681019d05ffe5b04f705d4d4ac51",
    "RD-PAID": "abac8f9519df2479e297bd732333402b4b5287de67fac9478b5c8f35bf295c56",
    "RD-SHORT-PAY": "cb284a948930bda0b94c956eb13872e9ec96209df5bb74aa1a7dc93d253f7ceb",
}
FROZEN_0_9_0 = FIXTURES / "bundles/redemption-0.9.0"
FROZEN_0_9_0_MANIFESTS = {
    "RD-CANCELLED": "76572f678a9ae72835b9494a761240150ec853761b12d9f3e9c56955687c13cf",
    "RD-PAID": "4cceaab6145b78ab90d5281ea3651495150d66a30df02b8781819828e0110d5b",
    "RD-SHORT-PAY": "35b7c6e5ac31f1d6c03dbc85ac914509a50dc6361574f7726b33b85ba1f9ef1f",
}
FROZEN_0_7_0 = FIXTURES / "bundles/redemption-0.7.0"
FROZEN_0_7_0_MANIFESTS = {
    "RD-CANCELLED": "ec6c6be41e697f2c91faa16661897434deb574bca9b71cc74c5f696d534fb125",
    "RD-PAID": "3d283d61b431f7e90597102b5c40d1bad60ef20a0e761befdb9ea0ca1076a586",
    "RD-SHORT-PAY": "60c42a75aa96146974031aadbb111ac90d1a8b76cf54f12a0d071c07fe356dfe",
}


@pytest.mark.parametrize("scenario_id", sorted(FROZEN_MANIFESTS))
def test_frozen_redemption_0_4_0_bundles_reproduce_and_rebuild(
    scenario_id: str, tmp_path: Path
) -> None:
    bundle = FROZEN / scenario_id
    manifest = (bundle / "manifest.json").read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == FROZEN_MANIFESTS[scenario_id]
    report = verify_bundle(bundle, expected_manifest_sha256=FROZEN_MANIFESTS[scenario_id])
    assert (report.status, report.engine_status) == ("REPRODUCED", "retired")
    assert report.engine_implementation == "compatibility"
    assert any(
        "full equivalence with the historical engine is not demonstrated" in r
        for r in report.reasons
    )
    corpus = load_corpus(REDEMPTION)
    assert report.financial_result == corpus.scenarios[scenario_id].expected.result
    inputs = corpus.inputs_for(scenario_id)
    out = tmp_path / "rebuilt"
    mode = corpus.scenarios[scenario_id].query_mode
    build_bundle(out, inputs, replay(inputs, "invaria-redemption-engine@0.4.0"), mode=mode)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {
        p.name: p.read_bytes() for p in bundle.iterdir()
    }


def test_a_current_engine_policy_refuses_a_retired_conclusion() -> None:
    report = verify_bundle(HISTORICAL_K3, engine_policy="current")
    assert (report.status, report.engine_status) == ("INCOMPLETE", "retired")
    assert any("policy requires a current engine" in r for r in report.reasons)
    assert verify_bundle(HISTORICAL_K3).status == "REPRODUCED"  # default: reproduce, flagged


def test_cli_engine_policy_and_status_on_the_first_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["verify", str(HISTORICAL_K3)]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.endswith("engine retired")
    assert main(["verify", str(HISTORICAL_K3), "--engine-policy", "current"]) == 1


def test_an_engine_of_another_operation_type_is_never_replayed(tmp_path: Path) -> None:
    subscription = load_corpus(SUBSCRIPTION)
    inputs = subscription.inputs_for("K2")
    crossed = evaluate(inputs, engine_ref="invaria-redemption-engine@0.6.0")
    build_bundle(tmp_path / "crossed", inputs, crossed, mode="as_known")
    report = verify_bundle(tmp_path / "crossed")
    assert report.status == "INCOMPLETE"
    assert report.local_engine_ref == "invaria-redemption-engine@0.6.0"
    assert any("evaluates redemption profiles, not subscription" in r for r in report.reasons)


def test_a_blocked_engine_report_names_the_recorded_engine(tmp_path: Path) -> None:
    corpus = load_corpus(REDEMPTION)
    inputs = corpus.inputs_for("RD-PAID")
    blocked = evaluate(inputs, engine_ref="invaria-redemption-engine@0.3.0")
    build_bundle(tmp_path / "blocked", inputs, blocked, mode="as_known")
    report = verify_bundle(tmp_path / "blocked")
    assert (report.status, report.engine_status) == ("INCOMPLETE", "blocked")
    assert report.local_engine_ref == "invaria-redemption-engine@0.3.0"


def test_quarantine_scope_is_declared_only_for_chain_movements() -> None:
    profile = json.loads(
        (FIXTURES / "corpus/redemption-synthetic-1.4.0/profile.json").read_text("utf-8")
    )
    parse_profile(json.dumps(profile))
    for requirement in profile["coverage_requirements"]:
        if requirement["source_id"] == "bank-synthetic":
            requirement["quarantine_scope"] = "records_bearing_on_operation"
    with pytest.raises(ValueError, match="only to a token_movement requirement"):
        parse_profile(json.dumps(profile))


def test_a_muxed_address_is_never_shown_foreign() -> None:
    corpus = load_corpus(SUBSCRIPTION)
    cert = next(iter(corpus.coverage.values()))
    muxed = "M" + "A" * 68
    scoped = parse_contract(
        type(cert),
        json.dumps(
            {
                **cert.model_dump(mode="json"),
                "records_received": cert.records_received + 1,
                "records_quarantined": 1,
                "quarantined_records": [{"locator": "m#1", "reasons": ["X"], "addresses": [muxed]}],
            }
        ),
    )
    assert quarantine_affecting(scoped, {"G" + "A" * 55}) == 1


def test_the_frozen_bundles_are_not_touched_by_a_new_build(tmp_path: Path) -> None:
    """A current build of the same scenario is a different conclusion, by another engine."""
    corpus = load_corpus(REDEMPTION)
    inputs = corpus.inputs_for("RD-CANCELLED")
    build_bundle(tmp_path / "now", inputs, evaluate(inputs), mode="as_known")
    now = json.loads((tmp_path / "now/evaluation.json").read_text("utf-8"))
    then = json.loads((FROZEN / "RD-CANCELLED/evaluation.json").read_text("utf-8"))
    assert now["versions"]["engine_ref"] == REDEMPTION_ENGINE_REF
    assert then["versions"]["engine_ref"] == "invaria-redemption-engine@0.4.0"
    assert (now["result"], now["controls"]) == (then["result"], then["controls"])
    assert now["evaluation_id"] != then["evaluation_id"]
    shutil.rmtree(tmp_path / "now")


@pytest.mark.parametrize("scenario_id", sorted(FROZEN_0_5_0_MANIFESTS))
def test_frozen_redemption_0_5_0_bundles_reproduce_and_rebuild(
    scenario_id: str, tmp_path: Path
) -> None:
    """Retired by 0.6.0: reproduced by its compatibility implementation, rebuilt byte for
    byte, flagged as retired; the current 0.6.0 gives the same result on these inputs."""
    bundle = FROZEN_0_5_0 / scenario_id
    sha = FROZEN_0_5_0_MANIFESTS[scenario_id]
    assert hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest() == sha
    report = verify_bundle(bundle, expected_manifest_sha256=sha)
    assert (report.status, report.engine_status, report.engine_implementation) == (
        "REPRODUCED",
        "retired",
        "compatibility",
    )
    assert verify_bundle(bundle, engine_policy="current").status == "INCOMPLETE"
    corpus = load_corpus(FIXTURES / "corpus/redemption-synthetic-1.4.0")
    inputs = corpus.inputs_for(scenario_id)
    out = tmp_path / "rebuilt"
    mode = corpus.scenarios[scenario_id].query_mode
    build_bundle(out, inputs, replay(inputs, "invaria-redemption-engine@0.5.0"), mode=mode)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {
        p.name: p.read_bytes() for p in bundle.iterdir()
    }
    now = evaluate(inputs).result
    then = json.loads((bundle / "evaluation.json").read_text("utf-8"))
    assert now.result == then["result"] == corpus.scenarios[scenario_id].expected.result


@pytest.mark.parametrize("scenario_id", sorted(FROZEN_0_6_0_MANIFESTS))
def test_frozen_redemption_0_6_0_bundles_reproduce_and_rebuild(
    scenario_id: str, tmp_path: Path
) -> None:
    """Retired by 0.7.0: frozen with the 0.6.0 code before the change, reproduced and
    rebuilt byte for byte by its compatibility implementation, flagged as retired. On these
    inputs (profile 1.4.0, no quarantine) the current engine gives the same result and
    controls."""
    bundle = FROZEN_0_6_0 / scenario_id
    sha = FROZEN_0_6_0_MANIFESTS[scenario_id]
    assert hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest() == sha
    report = verify_bundle(bundle, expected_manifest_sha256=sha)
    assert (report.status, report.engine_status, report.engine_implementation) == (
        "REPRODUCED",
        "retired",
        "compatibility",
    )
    assert verify_bundle(bundle, engine_policy="current").status == "INCOMPLETE"
    corpus = load_corpus(FIXTURES / "corpus/redemption-synthetic-1.4.0")
    inputs = corpus.inputs_for(scenario_id)
    out = tmp_path / "rebuilt"
    mode = corpus.scenarios[scenario_id].query_mode
    build_bundle(out, inputs, replay(inputs, "invaria-redemption-engine@0.6.0"), mode=mode)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {
        p.name: p.read_bytes() for p in bundle.iterdir()
    }
    now = evaluate(inputs).result
    then = json.loads((bundle / "evaluation.json").read_text("utf-8"))
    assert now.versions.engine_ref == REDEMPTION_ENGINE_REF
    assert (now.result, now.model_dump(mode="json")["controls"]) == (
        then["result"],
        then["controls"],
    )


@pytest.mark.parametrize("scenario_id", sorted(FROZEN_0_7_0_MANIFESTS))
def test_frozen_redemption_0_7_0_bundles_reproduce_and_rebuild(
    scenario_id: str, tmp_path: Path
) -> None:
    """Retired by 0.8.0: frozen with the 0.7.0 code on 2026-10-07 before the change
    (profile 1.5.0, current corpus), reproduced and rebuilt byte for byte by its
    compatibility implementation, flagged as retired. These inputs name no muxed account:
    0.8.0 gives the same result and controls."""
    bundle = FROZEN_0_7_0 / scenario_id
    sha = FROZEN_0_7_0_MANIFESTS[scenario_id]
    assert hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest() == sha
    report = verify_bundle(bundle, expected_manifest_sha256=sha)
    assert (report.status, report.engine_status, report.engine_implementation) == (
        "REPRODUCED",
        "retired",
        "compatibility",
    )
    assert verify_bundle(bundle, engine_policy="current").status == "INCOMPLETE"
    corpus = load_corpus(FIXTURES / "corpus/redemption-synthetic-1.5.0")
    inputs = corpus.inputs_for(scenario_id)
    out = tmp_path / "rebuilt"
    mode = corpus.scenarios[scenario_id].query_mode
    build_bundle(out, inputs, replay(inputs, "invaria-redemption-engine@0.7.0"), mode=mode)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {
        p.name: p.read_bytes() for p in bundle.iterdir()
    }
    now = evaluate(inputs).result
    then = json.loads((bundle / "evaluation.json").read_text("utf-8"))
    assert now.versions.engine_ref == REDEMPTION_ENGINE_REF
    assert (now.result, now.model_dump(mode="json")["controls"]) == (
        then["result"],
        then["controls"],
    )


@pytest.mark.parametrize("scenario_id", sorted(FROZEN_0_8_0_MANIFESTS))
def test_frozen_redemption_0_8_0_bundles_reproduce_and_rebuild(
    scenario_id: str, tmp_path: Path
) -> None:
    """Retired by 0.9.0: frozen with the 0.8.0 code on 2026-10-08 before the
    change (profile 1.5.0, current corpus), reproduced and rebuilt byte for byte by its
    compatibility implementation, flagged as retired. 0.9.0 gives the same result and
    controls on these inputs (no muxed movement, no unapproved linked burn, and a synthetic
    chain certificate without a chain scope declares nothing out of sight); its versions add
    the mappings that produced the evidence, which may be fewer than those admitted."""
    bundle = FROZEN_0_8_0 / scenario_id
    sha = FROZEN_0_8_0_MANIFESTS[scenario_id]
    assert hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest() == sha
    report = verify_bundle(bundle, expected_manifest_sha256=sha)
    assert (report.status, report.engine_status, report.engine_implementation) == (
        "REPRODUCED",
        "retired",
        "compatibility",
    )
    assert verify_bundle(bundle, engine_policy="current").status == "INCOMPLETE"
    corpus = load_corpus(FIXTURES / "corpus/redemption-synthetic-1.5.0")
    inputs = corpus.inputs_for(scenario_id)
    out = tmp_path / "rebuilt"
    mode = corpus.scenarios[scenario_id].query_mode
    build_bundle(out, inputs, replay(inputs, "invaria-redemption-engine@0.8.0"), mode=mode)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {
        p.name: p.read_bytes() for p in bundle.iterdir()
    }
    then = json.loads((bundle / "evaluation.json").read_text("utf-8"))
    assert "evidence_mapping_refs" not in then["versions"]
    now = evaluate(inputs).result
    assert now.versions.engine_ref == REDEMPTION_ENGINE_REF
    assert (now.result, now.model_dump(mode="json")["controls"]) == (
        then["result"],
        then["controls"],
    )
    # The evaluation states the mappings that produced its evidence, not the profile's.
    produced = sorted(
        {inputs.observations[i].provenance.mapping_ref for i in inputs.snapshot.observation_ids}
    )
    assert now.versions.evidence_mapping_refs == produced
    assert set(produced) <= set(now.versions.mapping_refs)


@pytest.mark.parametrize("scenario_id", sorted(FROZEN_0_9_0_MANIFESTS))
def test_frozen_redemption_0_9_0_bundles_reproduce_and_rebuild(
    scenario_id: str, tmp_path: Path
) -> None:
    """Retired by 0.10.0: frozen with the 0.9.0 code on 2026-10-08 before the change
    (profile 1.5.0), reproduced and rebuilt byte for byte by its compatibility
    implementation, flagged as retired. Under profile 1.5.0, which does not declare
    absence_needs_chain_scope, 0.10.0 gives the same result and controls: the rule binds
    only the profiles that declare it, so RD-CANCELLED keeps its MATCH there even though its
    chain certificate declares no scope (the declared limit of the earlier profiles)."""
    bundle = FROZEN_0_9_0 / scenario_id
    sha = FROZEN_0_9_0_MANIFESTS[scenario_id]
    assert hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest() == sha
    report = verify_bundle(bundle, expected_manifest_sha256=sha)
    assert (report.status, report.engine_status, report.engine_implementation) == (
        "REPRODUCED",
        "retired",
        "compatibility",
    )
    assert verify_bundle(bundle, engine_policy="current").status == "INCOMPLETE"
    corpus = load_corpus(FIXTURES / "corpus/redemption-synthetic-1.5.0")
    inputs = corpus.inputs_for(scenario_id)
    out = tmp_path / "rebuilt"
    mode = corpus.scenarios[scenario_id].query_mode
    build_bundle(out, inputs, replay(inputs, "invaria-redemption-engine@0.9.0"), mode=mode)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {
        p.name: p.read_bytes() for p in bundle.iterdir()
    }
    then = json.loads((bundle / "evaluation.json").read_text("utf-8"))
    now = evaluate(inputs).result
    assert now.versions.engine_ref == REDEMPTION_ENGINE_REF
    assert (now.result, now.model_dump(mode="json")["controls"]) == (
        then["result"],
        then["controls"],
    )
    assert now.assumptions == then["assumptions"]


@pytest.mark.parametrize("scenario_id", sorted(FROZEN_0_10_0_MANIFESTS))
def test_frozen_redemption_0_10_0_bundles_reproduce_and_rebuild(
    scenario_id: str, tmp_path: Path
) -> None:
    """Retired by 0.11.0: frozen with the 0.10.0 code on 2026-10-08 before the
    change (profile 1.6.0, where RD-CANCELLED is UNKNOWN by the first part of the rule),
    reproduced and rebuilt byte for byte by its compatibility implementation. Under 1.6.0,
    which does not declare completeness_needs_coverage, 0.11.0 gives the same result,
    controls and assumptions."""
    bundle = FROZEN_0_10_0 / scenario_id
    sha = FROZEN_0_10_0_MANIFESTS[scenario_id]
    assert hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest() == sha
    report = verify_bundle(bundle, expected_manifest_sha256=sha)
    assert (report.status, report.engine_status, report.engine_implementation) == (
        "REPRODUCED",
        "retired",
        "compatibility",
    )
    corpus = load_corpus(FIXTURES / "corpus/redemption-synthetic-1.6.0")
    inputs = corpus.inputs_for(scenario_id)
    out = tmp_path / "rebuilt"
    mode = corpus.scenarios[scenario_id].query_mode
    build_bundle(out, inputs, replay(inputs, "invaria-redemption-engine@0.10.0"), mode=mode)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {
        p.name: p.read_bytes() for p in bundle.iterdir()
    }
    then = json.loads((bundle / "evaluation.json").read_text("utf-8"))
    now = evaluate(inputs).result
    assert now.versions.engine_ref == REDEMPTION_ENGINE_REF
    assert (now.result, now.model_dump(mode="json")["controls"], now.assumptions) == (
        then["result"],
        then["controls"],
        then["assumptions"],
    )
