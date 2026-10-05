"""Golden corpus of evidence bundles: every case keeps its expected verifier outcome.

Cases are a committed base bundle plus an optional overlay (files added or replaced) and
removals, materialised in a temporary directory before verification. Signing keys were
ephemeral: the corpus holds public keys and signatures only, never a private key.

Regenerate (explicit decision, see the README): INVARIA_REGENERATE_GOLDEN_CORPUS=1.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from invaria.bundle import build_bundle, verify_bundle
from invaria.bundle.signing import TrustPolicy, public_key_hex, sign_bundle
from invaria.contracts.base import parse_contract
from invaria.contracts.bundle import TrustStore
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.evaluate import EvaluationInputs, evaluate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CORPUS_DIR = FIXTURES / "corpus/subscription-synthetic"
GOLDEN = FIXTURES / "bundles/corpus"
REGENERATE = os.environ.get("INVARIA_REGENERATE_GOLDEN_CORPUS") == "1"


def dumps(document: Any) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


# ------------------------------------------------------------------ generation


def csv_only_inputs(corpus: Corpus) -> EvaluationInputs:
    """K2 without the chain member: every observation has a local normaliser (R2)."""
    k2 = corpus.inputs_for("K2")
    snapshot = k2.snapshot.model_copy(
        update={
            "snapshot_id": "snap-K2-csv-only",
            "observation_ids": [i for i in k2.snapshot.observation_ids if i != "obs-T1"],
            "coverage_ids": [i for i in k2.snapshot.coverage_ids if i != "cov-chain-k2"],
        }
    )
    return EvaluationInputs(
        snapshot=snapshot,
        profile=k2.profile,
        observations=k2.observations,
        coverage=k2.coverage,
        identity_links=k2.identity_links,
    )


def raw_files(corpus: Corpus) -> dict[str, bytes]:
    return {f"raw/{f.name}": f.read_bytes() for f in sorted((corpus.root / "raw").iterdir())}


def build_bases(corpus: Corpus, out: Path) -> None:
    k3 = corpus.inputs_for("K3")
    k3_mode = corpus.scenarios["K3"].query_mode
    build_bundle(out / "k3-r1", k3, evaluate(k3), mode=k3_mode)
    build_bundle(
        out / "k3-r2",
        k3,
        evaluate(k3),
        mode=k3_mode,
        raw_sources=raw_files(corpus),
        mappings=corpus.mappings,
    )
    csv_only = csv_only_inputs(corpus)
    build_bundle(
        out / "k2-csv-r2",
        csv_only,
        evaluate(csv_only),
        mode=corpus.scenarios["K2"].query_mode,
        raw_sources=raw_files(corpus),
        mappings=corpus.mappings,
    )


def _rehashed_manifest(bundle: Path, replaced: dict[str, bytes]) -> bytes:
    manifest = json.loads((bundle / "manifest.json").read_text("utf-8"))
    for artifact in manifest["artifacts"]:
        data = replaced.get(artifact["path"], (bundle / artifact["path"]).read_bytes())
        artifact["sha256"] = hashlib.sha256(data).hexdigest()
    return dumps(manifest)


def hostile_overlays(bases: Path) -> dict[str, dict[str, bytes]]:
    """Deterministic overlays (no keys involved)."""
    k3, csv = bases / "k3-r1", bases / "k2-csv-r2"
    evaluation = (k3 / "evaluation.json").read_bytes()
    manifest = json.loads((k3 / "manifest.json").read_text("utf-8"))
    traversal = json.loads(json.dumps(manifest))
    traversal["artifacts"][0]["path"] = "../evaluation.json"
    deep: Any = "x"
    for _ in range(100):
        deep = [deep]
    bank = "raw/bank_statement_2026-10-01.csv"
    tampered_bank = (csv / bank).read_bytes().replace(b"100000.00", b"100500.00")
    mapping_path = "mappings/bank-csv-synthetic_1.0.0.json"
    mapping = json.loads((csv / mapping_path).read_text("utf-8"))
    for spec in mapping["payload"].values():
        if "scale" in spec:
            spec["scale"] += 1  # same file, other semantics
    tampered_mapping = dumps(mapping)
    return {
        "tampered-byte": {"evaluation.json": evaluation.replace(b"BREAK", b"MATCH", 1)},
        "undeclared-file": {"run.py": b'raise SystemExit("bundle code must never run")\n'},
        "traversal-path": {"manifest.json": dumps(traversal)},
        "deep-json": {"manifest.json": dumps(deep)},
        "r2-raw-tampered": {
            bank: tampered_bank,
            "manifest.json": _rehashed_manifest(csv, {bank: tampered_bank}),
        },
        "r2-mapping-tampered": {
            mapping_path: tampered_mapping,
            "manifest.json": _rehashed_manifest(csv, {mapping_path: tampered_mapping}),
        },
    }


KEYS = {
    "invaria-golden-signer": None,
    "invaria-golden-rotated": ("2026-10-04T12:00:00Z", "superseded"),
    "invaria-golden-compromised": ("2026-10-04T12:00:00Z", "key_compromise"),
}


def sign_overlays(bases: Path, out: Path) -> dict[str, Any]:
    """Sign copies of k3-r1 with fresh keys; return the trust store. Keys are discarded."""
    keys = {name: Ed25519PrivateKey.generate() for name in [*KEYS, "invaria-unknown-signer"]}
    plan = {
        "signed": "invaria-golden-signer",
        "signed-rotated": "invaria-golden-rotated",
        "signed-compromised": "invaria-golden-compromised",
        "signed-unknown-key": "invaria-unknown-signer",
    }
    for case, key_id in plan.items():
        work = out / "_work" / case
        shutil.copytree(bases / "k3-r1", work)
        sign_bundle(work, keys[key_id], key_id=key_id, signed_at=at("2026-10-03T10:00:00Z"))
        (out / "overlays" / case).mkdir(parents=True)
        shutil.copy(work / "signature.json", out / "overlays" / case / "signature.json")
    altered = json.loads((bases / "k3-r1/manifest.json").read_text("utf-8"))
    altered["disclosure"] = "private"
    (out / "overlays/signed-manifest-altered").mkdir(parents=True)
    shutil.copy(
        out / "overlays/signed/signature.json",
        out / "overlays/signed-manifest-altered/signature.json",
    )
    (out / "overlays/signed-manifest-altered/manifest.json").write_bytes(dumps(altered))
    shutil.rmtree(out / "_work")
    return {
        "schema_version": "1.0",
        "store_id": "invaria-golden-corpus",
        "keys": [
            {
                "key_id": name,
                "algorithm": "Ed25519",
                "public_key": public_key_hex(keys[name]),
                "purposes": ["bundle_signing"],
                "valid_from": "2026-10-01T00:00:00Z",
                "valid_to": None,
                "revocation": None
                if revocation is None
                else {"revoked_at": revocation[0], "reason": revocation[1]},
            }
            for name, revocation in KEYS.items()
        ],
    }


def regenerate(corpus: Corpus) -> None:
    staging = GOLDEN.with_name("corpus.new")
    shutil.rmtree(staging, ignore_errors=True)
    build_bases(corpus, staging / "bundles")
    for case, files in hostile_overlays(staging / "bundles").items():
        for path, data in files.items():
            target = staging / "overlays" / case / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    store = sign_overlays(staging / "bundles", staging)
    (staging / "trust-store.json").write_bytes(dumps(store))
    for keep in ("catalog.json", "README.md"):
        shutil.copy(GOLDEN / keep, staging / keep)
    shutil.rmtree(GOLDEN)
    staging.rename(GOLDEN)


# ------------------------------------------------------------------ verification


CATALOG = json.loads((GOLDEN / "catalog.json").read_text("utf-8"))


def materialise(case: dict[str, Any], target: Path) -> Path:
    shutil.copytree(GOLDEN / "bundles" / case["bundle"], target)
    if case.get("overlay"):
        for source in sorted((GOLDEN / "overlays" / case["overlay"]).rglob("*")):
            if source.is_file():
                relative = source.relative_to(GOLDEN / "overlays" / case["overlay"])
                (target / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(source, target / relative)
    for removed in case.get("remove", []):
        (target / removed).unlink()
    return target


def store() -> TrustStore:
    return parse_contract(TrustStore, (GOLDEN / "trust-store.json").read_text("utf-8"))


@pytest.mark.parametrize("case", CATALOG["cases"], ids=[c["case_id"] for c in CATALOG["cases"]])
def test_golden_case(case: dict[str, Any], tmp_path: Path) -> None:
    bundle = materialise(case, tmp_path / case["case_id"])
    report = verify_bundle(
        bundle,
        level=case["level"],
        trust_store=store() if case["trust_store"] else None,
        policy=TrustPolicy(
            at=at(case["trust_at"]) if case.get("trust_at") else None,
            min_signatures=case.get("min_signatures", 1),
        ),
    )
    assert report.status == case["expected_status"], report.reasons
    assert report.trust == case["expected_trust"], report.reasons
    assert report.financial_result == case["expected_financial_result"]
    if "expected_renormalized" in case:
        assert len(report.renormalized_observation_ids) == case["expected_renormalized"]


def test_catalog_covers_every_status_and_overlay() -> None:
    statuses = {c["expected_status"] for c in CATALOG["cases"]}
    assert statuses == {"REPRODUCED", "MISMATCH", "INCOMPLETE", "UNTRUSTED", "REJECTED"}
    used = {c["overlay"] for c in CATALOG["cases"] if c.get("overlay")}
    assert used == {p.name for p in (GOLDEN / "overlays").iterdir()}


def test_corpus_holds_no_private_key() -> None:
    for path in GOLDEN.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert b"PRIVATE KEY" not in data, path


def test_unsigned_corpus_matches_a_fresh_build(corpus: Corpus, tmp_path: Path) -> None:
    if REGENERATE:
        regenerate(corpus)
    build_bases(corpus, tmp_path / "bundles")
    for base in (GOLDEN / "bundles").iterdir():
        for path in base.rglob("*"):
            if path.is_file():
                fresh = tmp_path / "bundles" / base.name / path.relative_to(base)
                assert fresh.read_bytes() == path.read_bytes(), path
    golden_k3 = FIXTURES / "bundles/K3"
    for path in golden_k3.iterdir():
        assert (GOLDEN / "bundles/k3-r1" / path.name).read_bytes() == path.read_bytes()
    for case, files in hostile_overlays(tmp_path / "bundles").items():
        for relative, data in files.items():
            assert (GOLDEN / "overlays" / case / relative).read_bytes() == data, (case, relative)
