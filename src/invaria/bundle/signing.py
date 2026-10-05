"""Detached Ed25519 bundle signatures and trust evaluation against an explicit trust store.

The signature covers the exact manifest bytes through their sha256 and the claimed
``signed_at``; the manifest never changes when a bundle is signed. Trust is decided only
by the verifier's trust store and policy, never by anything inside the bundle.
"""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_private_key,
)

from invaria.bundle.fs import Stop, open_root, read_at, replace_at
from invaria.contracts.base import parse_contract
from invaria.contracts.bundle import BundleSignature, BundleSignatures, TrustStore

SIGNATURE_DOMAIN = b"INVARIA-OEB-v1\x00"
SIGNATURE_FILE = "signature.json"
MANIFEST_FILE = "manifest.json"


def _canonical(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def signing_preimage(signature: BundleSignature) -> bytes:
    record = signature.model_dump(mode="json", exclude={"signature"})
    return SIGNATURE_DOMAIN + _canonical(record)


def public_key_hex(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return public.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def load_private_key(path: Path) -> Ed25519PrivateKey:
    """PEM PKCS#8 Ed25519 key; refused if group or others can read it."""
    info = path.stat()
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(f"{path} must not be accessible by group or others (chmod 600)")
    key = load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("only Ed25519 private keys are supported")
    return key


MAX_SIGNED_FILE_BYTES = 64 * 1024 * 1024


def sign_bundle(
    root: Path, key: Ed25519PrivateKey, *, key_id: str, signed_at: datetime
) -> BundleSignature:
    """Add a detached signature to ``signature.json``; the manifest is not modified.

    Reads and writes go through the bundle's root descriptor without following symlinks;
    the new file is written to an exclusive temporary name and renamed in place.
    """
    try:
        with open_root(root) as root_fd:
            manifest = read_at(root_fd, MANIFEST_FILE, MAX_SIGNED_FILE_BYTES)
            if manifest is None:
                raise ValueError("bundle has no manifest.json")
            unsigned = BundleSignature(
                algorithm="Ed25519",
                key_id=key_id,
                signed_at=signed_at,
                manifest_sha256=hashlib.sha256(manifest).hexdigest(),
                signature="0" * 128,
            )
            signature = unsigned.model_copy(
                update={"signature": key.sign(signing_preimage(unsigned)).hex()}
            )
            existing_bytes = read_at(root_fd, SIGNATURE_FILE, MAX_SIGNED_FILE_BYTES)
            existing: list[BundleSignature] = []
            if existing_bytes is not None:
                existing = list(
                    parse_contract(BundleSignatures, existing_bytes.decode("utf-8")).signatures
                )
                if any(s.key_id == key_id for s in existing):
                    raise ValueError(f"bundle already signed by {key_id}")
            document = BundleSignatures(schema_version="1.0", signatures=[*existing, signature])
            replace_at(root_fd, SIGNATURE_FILE, _canonical(document.model_dump(mode="json")))
    except Stop as stop:
        raise ValueError(f"unsafe bundle: {stop.reason}") from stop
    return signature


@dataclass(frozen=True)
class TrustPolicy:
    """``at=None``: trust as of each signature's ``signed_at``; else as of ``at``."""

    at: datetime | None = None
    min_signatures: int = 1

    def __post_init__(self) -> None:
        if self.min_signatures < 1:
            raise ValueError("a trust policy requires at least one signature")
        if self.at is not None and self.at.utcoffset() is None:
            raise ValueError("trust time must be timezone-aware")


@dataclass(frozen=True)
class TrustDecision:
    trusted_key_ids: tuple[str, ...]
    reasons: tuple[str, ...]

    def satisfied(self, policy: TrustPolicy) -> bool:
        return len(self.trusted_key_ids) >= policy.min_signatures


def evaluate_trust(
    store: TrustStore,
    signatures: BundleSignatures,
    manifest_sha256: str,
    policy: TrustPolicy,
) -> TrustDecision:
    keys = {k.key_id: k for k in store.keys}
    trusted: list[str] = []
    reasons: list[str] = []
    for s in signatures.signatures:
        key = keys.get(s.key_id)
        if key is None:
            reasons.append(f"{s.key_id}: unknown key, ignored")
            continue
        if key.algorithm != s.algorithm or "bundle_signing" not in key.purposes:
            reasons.append(f"{s.key_id}: key not trusted for bundle signing")
            continue
        if s.manifest_sha256 != manifest_sha256:
            reasons.append(f"{s.key_id}: signature is for another manifest")
            continue
        try:
            public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(key.public_key))
            public.verify(bytes.fromhex(s.signature), signing_preimage(s))
        except (InvalidSignature, ValueError):
            reasons.append(f"{s.key_id}: invalid signature")
            continue
        if policy.at is not None and s.signed_at > policy.at:
            reasons.append(f"{s.key_id}: signed_at is after the trust time {policy.at.isoformat()}")
            continue
        moment = policy.at or s.signed_at
        basis = "at" if policy.at else "signed_at"
        if moment < key.valid_from or (key.valid_to is not None and moment >= key.valid_to):
            reasons.append(f"{s.key_id}: key not valid {basis} {moment.isoformat()}")
            continue
        if key.revocation is not None:
            if key.revocation.reason == "key_compromise":
                reasons.append(f"{s.key_id}: key revoked for compromise; no signature is trusted")
                continue
            if moment >= key.revocation.revoked_at:
                reasons.append(f"{s.key_id}: key revoked before {basis} {moment.isoformat()}")
                continue
        trusted.append(s.key_id)
        reasons.append(f"{s.key_id}: valid signature, key trusted {basis} {moment.isoformat()}")
    return TrustDecision(tuple(sorted(trusted)), tuple(reasons))
