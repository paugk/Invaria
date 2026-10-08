"""Retired invaria-engine@0.1.0: recovered source, provenance and result coincidence.

``tests/fixtures/engines/invaria-engine-0.1.0`` holds, byte for byte, the import closure of
``invaria.engine.evaluate`` at public commit c736292 (``evaluate.py`` unchanged since
cb6a299), with its ``uv.lock``. These tests keep three things apart:

- identity and provenance of that artifact (hashes against PROVENANCE.json);
- coincidence of the results: the compatibility implementation the verifier runs
  (current code, ``inv013=False``) gives the same result document as the recovered source
  on every recorded input it can parse;
- admission policy, tested elsewhere (retired: replay only).

Coincidence on these inputs does not make the compatibility implementation the historical
code: inputs the historical contracts cannot parse (e.g. a ``chain_effect``) are outside
the comparison.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from invaria.corpus_loader import load_corpus
from invaria.engine.evaluate import EvaluationInputs, replay
from invaria.vertical_testnet import run_testnet_vertical

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
HISTORICAL = FIXTURES / "engines/invaria-engine-0.1.0"
PROVENANCE = json.loads((HISTORICAL / "PROVENANCE.json").read_text("utf-8"))

RUNNER = """
import json, sys
import invaria.engine.evaluate as engine
from invaria.contracts.base import parse_contract
from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.evaluation import SnapshotRef
from invaria.contracts.identity import IdentityLink
from invaria.contracts.observation import Observation
from invaria.contracts.profile import OperationProfile
assert engine.__file__.startswith(sys.argv[1]), engine.__file__
out = []
for line in sys.stdin:
    case = json.loads(line)
    def many(model, items):
        return {k: parse_contract(model, json.dumps(v)) for k, v in items.items()}
    inputs = engine.EvaluationInputs(
        snapshot=parse_contract(SnapshotRef, json.dumps(case["snapshot"])),
        profile=parse_contract(OperationProfile, json.dumps(case["profile"])),
        observations=many(Observation, case["observations"]),
        coverage=many(CoverageCertificate, case["coverage"]),
        identity_links=many(IdentityLink, case["identity_links"]),
    )
    out.append(engine.evaluate(inputs).result.model_dump(mode="json"))
print(json.dumps(out))
"""


# Certificate fields added after 0.1.0, absent when None: today's adapter writes
# them on the testnet certificates, the historical contracts reject unknown fields, and
# neither 0.1.0 nor its compatibility implementation reads them. They are removed only from
# the historical code's copy; the compatibility implementation gets them, so equal results
# also show that it ignores them.
LATER_CERTIFICATE_FIELDS = ("chain_scope", "supersedes")
# Movement fields added after 0.1.0 (Classic payment mapping 1.1.0), absent when
# None: today's adapter writes the transaction memo of the testnet payments (DEMOA's T1 and
# T3 carry the text memo SUB-0001). Same treatment: removed only from the historical code's
# copy, so equal results also show that the compatibility implementation ignores the memo.
LATER_MOVEMENT_FIELDS = ("memo", "from_muxed_id", "to_muxed_id")


def _case(inputs: EvaluationInputs) -> str:
    def dump(items: dict) -> dict:  # type: ignore[type-arg]
        return {k: v.model_dump(mode="json") for k, v in items.items()}

    coverage = {
        k: {f: v for f, v in c.items() if f not in LATER_CERTIFICATE_FIELDS}
        for k, c in dump(dict(inputs.coverage)).items()
    }
    observations = dump(dict(inputs.observations))
    for o in observations.values():
        if o["payload"] and o["payload"]["payload_type"] == "token_movement":
            assert o["payload"].get("from_muxed_id") is None  # never in a recorded input
            assert o["payload"].get("to_muxed_id") is None
            for name in LATER_MOVEMENT_FIELDS:
                o["payload"].pop(name, None)
    return json.dumps(
        {
            "snapshot": inputs.snapshot.model_dump(mode="json"),
            "profile": inputs.profile.model_dump(mode="json"),
            "observations": observations,
            "coverage": coverage,
            "identity_links": dump(dict(inputs.identity_links)),
        }
    )


def _historical(cases: list[EvaluationInputs]) -> list[dict]:  # type: ignore[type-arg]
    source = str(HISTORICAL / "src")
    run = subprocess.run(
        [sys.executable, "-c", RUNNER, source],
        input="\n".join(_case(c) for c in cases),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": source},
        check=False,
    )
    assert run.returncode == 0, run.stderr
    results: list[dict] = json.loads(run.stdout)  # type: ignore[type-arg]
    return results


def test_the_recovered_source_matches_its_provenance() -> None:
    assert PROVENANCE["commit"] == "c736292474bce39a79f7d41246498db42f8e9468"
    assert PROVENANCE["evaluate_py_last_changed_in"].startswith("cb6a299")
    for path, pins in PROVENANCE["files"].items():
        data = (HISTORICAL / path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == pins["sha256"], path
        header = f"blob {len(data)}\0".encode()
        assert hashlib.sha1(header + data).hexdigest() == pins["git_blob"], path
    assert 'ENGINE_REF = "invaria-engine@0.1.0"' in (
        HISTORICAL / "src/invaria/engine/evaluate.py"
    ).read_text("utf-8")


def _recorded_inputs() -> list[tuple[str, EvaluationInputs]]:
    synthetic = load_corpus(FIXTURES / "corpus/subscription-synthetic")
    cases = [(f"synthetic:{s}", synthetic.inputs_for(s)) for s in sorted(synthetic.scenarios)]
    testnet = run_testnet_vertical(
        FIXTURES / "corpus/subscription-testnet",
        FIXTURES / "stellar",
        FIXTURES / "corpus/subscription-synthetic/mappings",
    )
    cases += [(f"testnet-1.0.0:{r.scenario.scenario_id}", r.inputs) for r in testnet]
    return cases


@pytest.fixture(scope="module")
def compared() -> list[tuple[str, dict, dict]]:  # type: ignore[type-arg]
    cases = _recorded_inputs()
    historical = _historical([inputs for _, inputs in cases])
    return [
        (name, old, replay(inputs, "invaria-engine@0.1.0").result.model_dump(mode="json"))
        for (name, inputs), old in zip(cases, historical, strict=True)
    ]


def test_the_compatibility_implementation_coincides_on_every_recorded_input(
    compared: list[tuple[str, dict, dict]],  # type: ignore[type-arg]
) -> None:
    assert len(compared) == 13  # 9 synthetic scenarios + 4 testnet scenarios (profile 1.0.0)
    for name, historical, compatible in compared:
        assert compatible == historical, name


def test_the_historical_k3_golden_is_the_recovered_engines_output(
    compared: list[tuple[str, dict, dict]],  # type: ignore[type-arg]
) -> None:
    (k3,) = [old for name, old, _ in compared if name == "synthetic:K3"]
    golden = json.loads((FIXTURES / "bundles/K3/evaluation.json").read_text("utf-8"))
    assert k3 == golden
