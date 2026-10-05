"""Hardened verifier: hostile file trees, limits, signatures and trust policy, R2, runtime."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import stat
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from invaria.bundle import build_bundle, fs, verify_bundle
from invaria.bundle import verify as verify_module
from invaria.bundle.runtime import engine_source_sha256
from invaria.bundle.signing import (
    TrustPolicy,
    load_private_key,
    public_key_hex,
    sign_bundle,
)
from invaria.cli import main
from invaria.contracts.bundle import EvidenceBundleManifest, TrustStore
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.evaluate import evaluate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CORPUS_DIR = FIXTURES / "corpus/subscription-synthetic"
GOLDEN = FIXTURES / "bundles/corpus/bundles"
SIGNED_AT = datetime.fromisoformat("2026-10-03T10:00:00Z")


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


@pytest.fixture
def k3(tmp_path: Path) -> Path:
    target = tmp_path / "k3"
    shutil.copytree(GOLDEN / "k3-r1", target)
    return target


@pytest.fixture
def csv_r2(tmp_path: Path) -> Path:
    target = tmp_path / "csv"
    shutil.copytree(GOLDEN / "k2-csv-r2", target)
    return target


def dumps(document: Any) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def edit_json(path: Path, change: Callable[[Any], None]) -> None:
    document = json.loads(path.read_text("utf-8"))
    change(document)
    path.write_bytes(dumps(document))


def rehash(bundle: Path) -> None:
    """Update every artifact hash in the manifest (a consistent forger)."""

    def update(manifest: Any) -> None:
        for artifact in manifest["artifacts"]:
            data = (bundle / artifact["path"]).read_bytes()
            artifact["sha256"] = hashlib.sha256(data).hexdigest()

    edit_json(bundle / "manifest.json", update)


def store_for(*entries: tuple[str, Ed25519PrivateKey, dict[str, Any]]) -> TrustStore:
    return TrustStore.model_validate_json(
        json.dumps(
            {
                "schema_version": "1.0",
                "store_id": "test-store",
                "keys": [
                    {
                        "key_id": key_id,
                        "algorithm": "Ed25519",
                        "public_key": public_key_hex(key),
                        "purposes": ["bundle_signing"],
                        "valid_from": "2026-10-01T00:00:00Z",
                        "valid_to": None,
                        "revocation": None,
                        **overrides,
                    }
                    for key_id, key, overrides in entries
                ],
            }
        )
    )


# ----------------------------------------------------------------- hostile trees


def test_hard_link_is_rejected(k3: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_bytes((k3 / "profile.json").read_bytes())
    (k3 / "profile.json").unlink()
    os.link(outside, k3 / "profile.json")
    report = verify_bundle(k3)
    assert report.status == "REJECTED" and "hard-link" in report.reasons[-1]


def test_symlinked_parent_directory_is_not_followed(csv_r2: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    shutil.move(csv_r2 / "raw", outside)
    (csv_r2 / "raw").symlink_to(outside, target_is_directory=True)
    assert verify_bundle(csv_r2, level="R2").status == "REJECTED"
    with fs.open_root(csv_r2) as root_fd:  # also without the inventory pass
        with pytest.raises(fs.Stop) as stop:
            fs.read_at(root_fd, "raw/bank_statement_2026-10-01.csv", 1 << 20)
    assert stop.value.status == "REJECTED"


def test_fifo_is_rejected_without_blocking(k3: Path) -> None:
    os.mkfifo(k3 / "pipe")
    assert verify_bundle(k3).status == "REJECTED"
    (k3 / "pipe").unlink()
    (k3 / "profile.json").unlink()
    os.mkfifo(k3 / "profile.json")  # a declared artifact replaced by a FIFO
    assert verify_bundle(k3).status == "REJECTED"


def test_undeclared_directory_is_rejected(k3: Path) -> None:
    (k3 / "extra").mkdir()
    report = verify_bundle(k3)
    assert report.status == "REJECTED" and "directories" in report.reasons[-1]


def test_case_colliding_paths_are_rejected(k3: Path) -> None:
    (k3 / "Profile.json").write_bytes((k3 / "profile.json").read_bytes())
    assert verify_bundle(k3).status == "REJECTED"
    manifest = json.loads((k3 / "manifest.json").read_text("utf-8"))
    manifest["artifacts"].insert(0, {**manifest["artifacts"][-1], "path": "Snapshot.json"})
    with pytest.raises(ValueError, match="case"):
        EvidenceBundleManifest.model_validate_json(json.dumps(manifest))


def test_reserved_paths_cannot_be_artifacts(k3: Path) -> None:
    manifest = json.loads((k3 / "manifest.json").read_text("utf-8"))
    manifest["artifacts"].append({**manifest["artifacts"][-1], "path": "signature.json"})
    manifest["artifacts"].sort(key=lambda a: a["path"])
    with pytest.raises(ValueError, match="reserved"):
        EvidenceBundleManifest.model_validate_json(json.dumps(manifest))


@pytest.mark.parametrize(("limit", "value"), [("MAX_FILES", 3), ("MAX_TOTAL_BYTES", 1000)])
def test_file_count_and_total_size_limits(
    k3: Path, monkeypatch: pytest.MonkeyPatch, limit: str, value: int
) -> None:
    monkeypatch.setattr(verify_module, limit, value)
    report = verify_bundle(k3)
    assert report.status == "REJECTED" and "limits" in report.reasons[-1]


def test_deep_json_in_an_artifact_is_rejected(k3: Path) -> None:
    nested: Any = 1
    for _ in range(verify_module.MAX_JSON_DEPTH + 1):
        nested = {"x": nested}
    (k3 / "snapshot.json").write_bytes(dumps(nested))
    rehash(k3)
    report = verify_bundle(k3)
    assert report.status == "REJECTED" and "deeper" in report.reasons[-1]


def test_json_depth_ignores_brackets_inside_strings() -> None:
    assert fs.json_depth(b'{"a":"[[[[{{{\\"]]]","b":[1]}') == 2


# ------------------------------------------------------------------- runtime


def test_verification_never_writes(k3: Path) -> None:
    def tree() -> dict[str, tuple[bytes, int]]:
        return {
            p.relative_to(k3).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(k3.rglob("*"))
        }

    before = tree()
    os.chmod(k3, stat.S_IRUSR | stat.S_IXUSR)  # read-only directory
    try:
        report = verify_bundle(k3)
    finally:
        os.chmod(k3, stat.S_IRWXU)
    assert report.status == "REPRODUCED"
    assert tree() == before


def test_report_names_the_local_engine_source(k3: Path) -> None:
    report = verify_bundle(k3)
    assert report.engine_source_sha256 == engine_source_sha256()
    assert len(report.engine_source_sha256) == 64


def test_r2_and_signatures_run_with_network_disabled(
    csv_r2: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*_: Any, **__: Any) -> Any:
        raise AssertionError("network access attempted during verification")

    key = Ed25519PrivateKey.generate()
    sign_bundle(csv_r2, key, key_id="k1", signed_at=SIGNED_AT)
    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    report = verify_bundle(csv_r2, level="R2", trust_store=store_for(("k1", key, {})))
    assert (report.status, report.trust) == ("REPRODUCED", "trusted_signature")


# ------------------------------------------------------------------ signatures


def test_signing_leaves_the_manifest_untouched(k3: Path) -> None:
    before = (k3 / "manifest.json").read_bytes()
    key = Ed25519PrivateKey.generate()
    signature = sign_bundle(k3, key, key_id="k1", signed_at=SIGNED_AT)
    assert (k3 / "manifest.json").read_bytes() == before
    assert signature.manifest_sha256 == hashlib.sha256(before).hexdigest()
    with pytest.raises(ValueError, match="already signed"):
        sign_bundle(k3, key, key_id="k1", signed_at=SIGNED_AT)


def test_valid_signature_is_trusted_and_combines_with_anchor(k3: Path) -> None:
    key = Ed25519PrivateKey.generate()
    signature = sign_bundle(k3, key, key_id="k1", signed_at=SIGNED_AT)
    store = store_for(("k1", key, {}))
    report = verify_bundle(k3, trust_store=store)
    assert (report.status, report.trust, report.signer_key_ids) == (
        "REPRODUCED",
        "trusted_signature",
        ["k1"],
    )
    anchored = verify_bundle(
        k3, trust_store=store, expected_manifest_sha256=signature.manifest_sha256
    )
    assert anchored.trust == "anchored_and_trusted_signature"


@pytest.mark.parametrize(
    "tamper",
    [
        lambda s: s.update(
            signature=("0" if s["signature"][0] != "0" else "1") + s["signature"][1:]
        ),
        lambda s: s.update(signed_at="2026-10-02T10:00:00Z"),
        lambda s: s.update(manifest_sha256="0" * 64),
    ],
    ids=["signature-bytes", "signed-at", "manifest-hash"],
)
def test_tampered_signature_is_untrusted(k3: Path, tamper: Callable[[Any], None]) -> None:
    key = Ed25519PrivateKey.generate()
    sign_bundle(k3, key, key_id="k1", signed_at=SIGNED_AT)
    edit_json(k3 / "signature.json", lambda d: tamper(d["signatures"][0]))
    report = verify_bundle(k3, trust_store=store_for(("k1", key, {})))
    assert report.status == "UNTRUSTED" and report.signer_key_ids == []


@pytest.mark.parametrize(
    ("overrides", "policy_at", "trusted"),
    [
        ({"valid_from": "2026-10-04T00:00:00Z"}, None, False),  # not yet valid
        ({"valid_to": "2026-10-02T00:00:00Z"}, None, False),  # expired before signing
        ({"valid_to": "2026-10-04T00:00:00Z"}, None, True),  # valid at signing
        ({"valid_to": "2026-10-04T00:00:00Z"}, "2026-10-05T00:00:00Z", False),  # not now
        (
            {"revocation": {"revoked_at": "2026-10-02T00:00:00Z", "reason": "superseded"}},
            None,
            False,
        ),
        (
            {"revocation": {"revoked_at": "2026-10-04T00:00:00Z", "reason": "superseded"}},
            None,
            True,
        ),
        (
            {"revocation": {"revoked_at": "2026-10-04T00:00:00Z", "reason": "superseded"}},
            "2026-10-05T00:00:00Z",
            False,
        ),
        (
            {"revocation": {"revoked_at": "2026-10-09T00:00:00Z", "reason": "key_compromise"}},
            None,
            False,
        ),
    ],
)
def test_trust_policy_windows_and_revocation(
    k3: Path, overrides: dict[str, Any], policy_at: str | None, trusted: bool
) -> None:
    key = Ed25519PrivateKey.generate()
    sign_bundle(k3, key, key_id="k1", signed_at=SIGNED_AT)
    report = verify_bundle(
        k3,
        trust_store=store_for(("k1", key, overrides)),
        policy=TrustPolicy(at=datetime.fromisoformat(policy_at) if policy_at else None),
    )
    assert (report.status == "REPRODUCED") is trusted, report.reasons
    assert (report.status == "UNTRUSTED") is not trusted


def test_minimum_number_of_signatures(k3: Path) -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    store = store_for(("a", a, {}), ("b", b, {}))
    sign_bundle(k3, a, key_id="a", signed_at=SIGNED_AT)
    assert verify_bundle(k3, trust_store=store, policy=TrustPolicy(min_signatures=2)).status == (
        "UNTRUSTED"
    )
    sign_bundle(k3, b, key_id="b", signed_at=SIGNED_AT)
    report = verify_bundle(k3, trust_store=store, policy=TrustPolicy(min_signatures=2))
    assert (report.status, report.signer_key_ids) == ("REPRODUCED", ["a", "b"])


def test_signature_from_the_bundle_cannot_extend_trust(k3: Path) -> None:
    """A key the bundle signs with is irrelevant unless the verifier's store holds it."""
    trusted, attacker = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    sign_bundle(k3, attacker, key_id="trusted", signed_at=SIGNED_AT)  # reuses a trusted id
    report = verify_bundle(k3, trust_store=store_for(("trusted", trusted, {})))
    assert report.status == "UNTRUSTED" and "invalid signature" in " ".join(report.reasons)


def test_malformed_signature_file_is_rejected(k3: Path) -> None:
    (k3 / "signature.json").write_bytes(b'{"schema_version":"1.0","signatures":[]}')
    assert verify_bundle(k3).status == "REJECTED"


def test_private_key_file_must_be_private(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "key.pem"
    path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        load_private_key(path)
    path.chmod(0o600)
    assert public_key_hex(load_private_key(path)) == public_key_hex(key)


# ------------------------------------------------------------------------ R2


def test_r2_reproduces_csv_members(csv_r2: Path) -> None:
    report = verify_bundle(csv_r2, level="R2")
    assert report.status == "REPRODUCED"
    assert report.renormalized_observation_ids == ["obs-B1", "obs-O1", "obs-R1"]


def test_r2_correction_keeps_its_supersedes_link(corpus: Corpus, tmp_path: Path) -> None:
    k3 = corpus.inputs_for("K3")
    raw = {f"raw/{f.name}": f.read_bytes() for f in (CORPUS_DIR / "raw").iterdir()}
    out = tmp_path / "k3-r2"
    build_bundle(out, k3, evaluate(k3), mode="as_known", raw_sources=raw, mappings=corpus.mappings)
    report = verify_bundle(out, level="R2")
    assert report.status == "INCOMPLETE"  # obs-T1: no local normaliser
    assert "obs-B2" in report.renormalized_observation_ids

    def retarget(evidence: Any) -> None:
        for o in evidence["observations"]:
            if o["observation_id"] == "obs-B2":
                o["supersedes"] = "obs-O1"  # another source record

    edit_json(out / "evidence.json", retarget)
    rehash(out)
    report = verify_bundle(out, level="R2")
    assert report.status in {"MISMATCH", "REJECTED"}


def test_builder_refuses_altered_or_missing_raw(corpus: Corpus, tmp_path: Path) -> None:
    k2 = corpus.inputs_for("K2")
    raw = {f"raw/{f.name}": f.read_bytes() for f in (CORPUS_DIR / "raw").iterdir()}
    raw["raw/bank_statement_2026-10-01.csv"] += b"\n"
    with pytest.raises(ValueError, match="obs-B1"):
        build_bundle(
            tmp_path / "x",
            k2,
            evaluate(k2),
            mode="as_known",
            raw_sources=raw,
            mappings=corpus.mappings,
        )
    with pytest.raises(ValueError, match="mapping"):
        build_bundle(
            tmp_path / "y",
            k2,
            evaluate(k2),
            mode="as_known",
            raw_sources={k: v for k, v in raw.items() if "bank" not in k}
            | {
                "raw/bank_statement_2026-10-01.csv": (
                    CORPUS_DIR / "raw/bank_statement_2026-10-01.csv"
                ).read_bytes()
            },
            mappings={},
        )


def test_r2_catches_evidence_that_r1_cannot(csv_r2: Path) -> None:
    """A field the evaluation never reads: R1 still reproduces, only R2 sees the forgery."""

    def other_account(evidence: Any) -> None:
        for o in evidence["observations"]:
            if o["observation_id"] == "obs-O1":
                o["payload"]["account_ref"] = "acct-pseudo-9999"

    edit_json(csv_r2 / "evidence.json", other_account)
    rehash(csv_r2)
    assert verify_bundle(csv_r2, level="R1").status == "REPRODUCED"
    report = verify_bundle(csv_r2, level="R2")
    assert report.status == "MISMATCH" and "obs-O1" in report.reasons[-1]


def test_foreign_parser_is_incomplete(csv_r2: Path) -> None:
    def foreign(evidence: Any) -> None:
        evidence["observations"][0]["provenance"]["parser_ref"] = "other-parser@9.9.9"

    edit_json(csv_r2 / "evidence.json", foreign)
    rehash(csv_r2)
    report = verify_bundle(csv_r2, level="R2")
    assert report.status == "INCOMPLETE" and "other-parser@9.9.9" in report.reasons[-1]


# ------------------------------------------------------------------------ CLI


def test_cli_r2_sign_and_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "K2"
    assert main(["bundle", str(CORPUS_DIR), "K2", str(out), "--r2"]) == 0
    key = Ed25519PrivateKey.generate()
    key_file = tmp_path / "signer.pem"
    key_file.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    key_file.chmod(0o600)
    sign = ["sign", str(out), "--key-file", str(key_file), "--key-id", "k1"]
    assert main([*sign, "--signed-at", "2026-10-03T10:00:00"]) == 2  # naive time refused
    assert main([*sign, "--signed-at", "2026-10-03T10:00:00Z"]) == 0
    store_file = tmp_path / "store.json"
    store_file.write_text(store_for(("k1", key, {})).model_dump_json(), "utf-8")
    capsys.readouterr()
    assert main(["verify", str(out), "--trust-store", str(store_file)]) == 0
    assert "trust trusted_signature" in capsys.readouterr().out
    assert main(["verify", str(out), "--level", "R2"]) == 1  # obs-T1 cannot be renormalised
    assert "status INCOMPLETE" in capsys.readouterr().out
    assert (
        main(["verify", str(out), "--trust-store", str(store_file), "--min-signatures", "2"]) == 1
    )


# ------------------------------------------------- regressions from the security review


def test_sign_never_writes_through_a_symlink(k3: Path, tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch", "utf-8")
    (k3 / "signature.json").symlink_to(victim)
    with pytest.raises(ValueError, match="unsafe bundle"):
        sign_bundle(k3, Ed25519PrivateKey.generate(), key_id="k1", signed_at=SIGNED_AT)
    assert victim.read_text("utf-8") == "do not touch"
    (k3 / "signature.json").unlink()
    os.mkfifo(k3 / "signature.json")
    with pytest.raises(ValueError, match="unsafe bundle"):  # no hang on a FIFO
        sign_bundle(k3, Ed25519PrivateKey.generate(), key_id="k1", signed_at=SIGNED_AT)


def test_sign_temporary_file_is_exclusive(
    k3: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a predicted temporary name planted as a symlink is never written through."""
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch", "utf-8")
    monkeypatch.setattr(secrets, "token_hex", lambda _n: "predicted")
    (k3 / ".signature.json.predicted.tmp").symlink_to(victim)
    with pytest.raises(OSError):
        sign_bundle(k3, Ed25519PrivateKey.generate(), key_id="k1", signed_at=SIGNED_AT)
    assert victim.read_text("utf-8") == "do not touch"


def test_sign_leaves_no_temporary_file(k3: Path) -> None:
    sign_bundle(k3, Ed25519PrivateKey.generate(), key_id="k1", signed_at=SIGNED_AT)
    assert sorted(p.name for p in k3.iterdir()) == [
        "evaluation.json",
        "evidence.json",
        "manifest.json",
        "profile.json",
        "signature.json",
        "snapshot.json",
    ]
    assert verify_bundle(k3).status == "REPRODUCED"


def test_cli_refuses_to_sign_a_bundle_that_does_not_reproduce(
    k3: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_file = tmp_path / "signer.pem"
    key_file.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
    )
    key_file.chmod(0o600)
    (k3 / "extra.txt").write_text("x", "utf-8")
    argv = ["sign", str(k3), "--key-file", str(key_file), "--key-id", "k1"]
    assert main([*argv, "--signed-at", "2026-10-03T10:00:00Z"]) == 1
    assert not (k3 / "signature.json").exists()
    assert "refusing to sign" in capsys.readouterr().err


def test_deep_mapping_in_r2_is_a_report_not_a_crash(csv_r2: Path) -> None:
    (csv_r2 / "mappings/bank-csv-synthetic_1.0.0.json").write_bytes(b"[" * 100_000)
    rehash(csv_r2)
    report = verify_bundle(csv_r2, level="R2")
    assert report.status == "MISMATCH" and "too deeply" in report.reasons[-1]


def test_unicode_digit_locator_is_a_report_not_a_crash(csv_r2: Path) -> None:
    def superscript(evidence: Any) -> None:
        o = evidence["observations"][0]
        o["provenance"]["raw_locator"] = o["provenance"]["raw_locator"].split("#")[0] + "#row=²"

    edit_json(csv_r2 / "evidence.json", superscript)
    rehash(csv_r2)
    report = verify_bundle(csv_r2, level="R2")
    assert report.status == "MISMATCH" and "raw_locator" in report.reasons[-1]


def test_long_reasons_are_bounded(k3: Path) -> None:
    for i in range(40):
        (k3 / f"{'x' * 200}-{i}.json").write_text("{}", "utf-8")  # 10 listed > 2000 chars
    report = verify_bundle(k3)
    assert report.status == "REJECTED" and "[40, 30 not listed]" in report.reasons[-1]
    assert all(len(r) <= verify_module.MAX_REASON_CHARS for r in report.reasons)


def test_inventory_follows_the_opened_root_not_the_path(tmp_path: Path) -> None:
    clean = tmp_path / "bundle"
    shutil.copytree(GOLDEN / "k3-r1", clean)
    with fs.open_root(clean) as root_fd:
        clean.rename(tmp_path / "moved")
        clean.mkdir()
        (clean / "planted.json").write_text("{}", "utf-8")
        files, _ = fs.inventory(root_fd, max_entries=100, max_bytes=1 << 30, max_depth=8)
    assert "planted.json" not in files and "manifest.json" in files


def test_integrity_is_judged_before_availability(k3: Path) -> None:
    (k3 / "evaluation.json").unlink()
    edit_json(k3 / "snapshot.json", lambda s: s.update(operation_ref="SUB-9999"))
    report = verify_bundle(k3)
    assert report.status == "REJECTED" and "snapshot.json" in report.reasons[-1]


def test_cli_rejects_meaningless_policy_and_anchor(
    k3: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["verify", str(k3), "--min-signatures", "0"]) == 2
    assert main(["verify", str(k3), "--expect-manifest-sha256", ""]) == 2
    with pytest.raises(ValueError):
        TrustPolicy(min_signatures=0)


def test_signature_after_the_trust_time_is_not_trusted(k3: Path) -> None:
    key = Ed25519PrivateKey.generate()
    sign_bundle(k3, key, key_id="k1", signed_at=SIGNED_AT)
    report = verify_bundle(
        k3,
        trust_store=store_for(("k1", key, {})),
        policy=TrustPolicy(at=datetime.fromisoformat("2026-10-02T00:00:00Z")),
    )
    assert report.status == "UNTRUSTED" and "after the trust time" in " ".join(report.reasons)


def _bundle_with(corpus: Corpus, out: Path, change: Callable[[dict[str, Any]], None]) -> Path:
    """A legitimately built CSV-only K2 bundle whose evidence was altered before building."""
    k2 = corpus.inputs_for("K2")
    observations = {k: v for k, v in k2.observations.items() if k != "obs-T1"}
    extra: dict[str, Any] = {}
    change(extra)
    for observation in extra.get("observations", []):
        observations[observation.observation_id] = observation
    ids = sorted(
        {i for i in k2.snapshot.observation_ids if i != "obs-T1"}
        | {o.observation_id for o in extra.get("observations", [])}
    )
    snapshot = k2.snapshot.model_copy(
        update={
            "snapshot_id": "snap-altered",
            "observation_ids": ids,
            "coverage_ids": [i for i in k2.snapshot.coverage_ids if i != "cov-chain-k2"],
        }
    )
    inputs = type(k2)(snapshot, k2.profile, observations, k2.coverage, k2.identity_links)
    raw = {f"raw/{f.name}": f.read_bytes() for f in (CORPUS_DIR / "raw").iterdir()}
    build_bundle(
        out, inputs, evaluate(inputs), mode="as_known", raw_sources=raw, mappings=corpus.mappings
    )
    return out


def test_one_raw_row_cannot_back_two_observations(corpus: Corpus, tmp_path: Path) -> None:
    b1 = corpus.journals["main"]["obs-B1"]
    twin = b1.model_copy(update={"observation_id": "obs-B1-twin"})
    bundle = _bundle_with(corpus, tmp_path / "twin", lambda e: e.update(observations=[twin]))
    assert verify_bundle(bundle).status == "REPRODUCED"  # R1 cannot tell
    report = verify_bundle(bundle, level="R2")
    assert report.status == "MISMATCH" and "already renormalised" in report.reasons[-1]


def test_raw_row_cannot_be_attributed_to_another_instrument(corpus: Corpus, tmp_path: Path) -> None:
    """Tenant and instrument come from the bundle's own snapshot and profile, not from the
    observation under test: a TA row relabelled to another instrument is caught."""
    r1 = corpus.journals["main"]["obs-R1"]
    foreign = r1.model_copy(update={"instrument_id": "syn:fund:OTHER:class-a"})
    bundle = _bundle_with(corpus, tmp_path / "foreign", lambda e: e.update(observations=[foreign]))
    assert verify_bundle(bundle).status == "REPRODUCED"  # R1 reproduces the UNKNOWN
    report = verify_bundle(bundle, level="R2")
    assert report.status == "MISMATCH" and "obs-R1" in report.reasons[-1]
