"""Minimal XDR for the Stellar values Invaria reads. Standard library only; not an SDK.

Supports exactly what the SAC adapter needs and rejects anything else explicitly:
ScVal Symbol, String, Address (account, contract, muxed account, claimable balance,
liquidity pool), I128; Asset encoding; SAC contract id derivation (CAP-46
HashIdPreimage ENVELOPE_TYPE_CONTRACT_ID / FROM_ASSET).
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass

from invaria.contracts.stellar import (
    _crc16_xmodem,
    decode_account_id,
    encode_account_id,
    encode_muxed_account,
    encode_strkey,
)

SCV_U64 = 5
SCV_I128 = 10
SCV_BYTES = 13
SCV_MAP = 17
MAX_MAP_ENTRIES = 8
SCV_STRING = 14
SCV_SYMBOL = 15
SCV_ADDRESS = 18
SC_ADDRESS_ACCOUNT = 0
SC_ADDRESS_CONTRACT = 1
SC_ADDRESS_MUXED_ACCOUNT = 2
SC_ADDRESS_CLAIMABLE_BALANCE = 3
SC_ADDRESS_LIQUIDITY_POOL = 4
ENVELOPE_TYPE_CONTRACT_ID = 8
CONTRACT_ID_PREIMAGE_FROM_ASSET = 1
_CONTRACT_VERSION = 2 << 3
MAX_STRING = 1024


class XdrError(ValueError):
    """Value outside the supported subset or malformed bytes."""


class UnsupportedAddress(XdrError):
    """An ScAddress of a type this decoder does not represent (an unknown type, or a
    claimable balance id of a type other than V0)."""

    def __init__(self, address_kind: int) -> None:
        super().__init__(f"unsupported ScAddress type {address_kind}")
        self.address_kind = address_kind


def encode_contract_id(raw: bytes) -> str:
    if len(raw) != 32:
        raise XdrError("a contract id has 32 bytes")
    payload = bytes([_CONTRACT_VERSION]) + raw
    checksum = _crc16_xmodem(payload).to_bytes(2, "little")
    return base64.b32encode(payload + checksum).decode("ascii").rstrip("=")


def decode_contract_id(contract_id: str) -> bytes:
    if len(contract_id) != 56 or not contract_id.startswith("C"):
        raise XdrError("a contract id has 56 characters and starts with 'C'")
    raw = base64.b32decode(contract_id)
    payload, checksum = raw[:-2], raw[-2:]
    if payload[0] != _CONTRACT_VERSION or _crc16_xmodem(payload).to_bytes(2, "little") != checksum:
        raise XdrError("contract id checksum or version mismatch")
    return payload[1:]


def _pad(data: bytes) -> bytes:
    return data + b"\x00" * (-len(data) % 4)


def encode_asset(code: str, issuer: str) -> bytes:
    raw_code = code.encode("ascii")
    if not 1 <= len(raw_code) <= 12 or not code.isalnum():
        raise XdrError(f"invalid asset code {code!r}")
    kind, width = (1, 4) if len(raw_code) <= 4 else (2, 12)
    issuer_key = decode_account_id(issuer)
    return (
        struct.pack(">i", kind)
        + raw_code.ljust(width, b"\x00")
        + struct.pack(">i", 0)  # PUBLIC_KEY_TYPE_ED25519
        + issuer_key
    )


def sac_contract_id(code: str, issuer: str, network_passphrase: str) -> str:
    network_id = hashlib.sha256(network_passphrase.encode("utf-8")).digest()
    preimage = (
        struct.pack(">i", ENVELOPE_TYPE_CONTRACT_ID)
        + network_id
        + struct.pack(">i", CONTRACT_ID_PREIMAGE_FROM_ASSET)
        + encode_asset(code, issuer)
    )
    return encode_contract_id(hashlib.sha256(preimage).digest())


@dataclass(frozen=True)
class ScAddress:
    """A decoded ScAddress as a typed technical party: its kind and its SEP-23 strkey (G, C,
    M, B or L), a lossless encoding of the XDR bytes. Never an economic holder by itself: a
    muxed account's M strkey names a base account and a sub-account id, not who holds it."""

    kind: str  # "account" | "contract" | "muxed_account" | "claimable_balance" | ...
    strkey: str


ScValue = str | int | bytes | ScAddress | dict[str, "ScValue"]


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise XdrError("truncated XDR")
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def int32(self) -> int:
        value: int = struct.unpack(">i", self.take(4))[0]
        return value

    def uint32(self) -> int:
        value: int = struct.unpack(">I", self.take(4))[0]
        return value

    def opaque(self) -> bytes:
        length = self.uint32()
        if length > MAX_STRING:
            raise XdrError("string too long")
        data = self.take(length)
        if any(self.take(-length % 4)):
            raise XdrError("non-zero XDR padding")
        return data


def string_bytes(value: str) -> bytes:
    """The exact bytes of a decoded ScString (see ``SCV_STRING``)."""
    return value.encode("utf-8", "surrogateescape")


def decode_scval(b64: str) -> ScValue:
    """Decode one base64 ScVal of a supported type; reject everything else."""
    try:
        data = base64.b64decode(b64, validate=True)
    except ValueError as error:
        raise XdrError("invalid base64") from error
    reader = _Reader(data)
    value = _read_scval(reader, depth=0)
    if reader.pos != len(data):
        raise XdrError("trailing bytes after ScVal")
    return value


def _read_scval(reader: _Reader, depth: int) -> ScValue:
    kind = reader.int32()
    value: ScValue
    if kind == SCV_MAP:
        # CAP-67 movement data: {amount: i128, to_muxed_id: u64 | string | bytes}
        if depth > 0 or reader.uint32() != 1:
            raise XdrError("unsupported map (nested or absent)")
        entries = reader.uint32()
        if entries > MAX_MAP_ENTRIES:
            raise XdrError("map too large")
        mapping: dict[str, ScValue] = {}
        for _ in range(entries):
            key = _read_scval(reader, depth + 1)
            if not isinstance(key, str) or key in mapping:
                raise XdrError("map keys must be unique symbols")
            mapping[key] = _read_scval(reader, depth + 1)
        return mapping
    if kind == SCV_U64:
        value = struct.unpack(">Q", reader.take(8))[0]
    elif kind == SCV_BYTES:
        value = reader.opaque()
    elif kind == SCV_SYMBOL:
        try:
            value = reader.opaque().decode("utf-8")
        except UnicodeDecodeError as error:
            raise XdrError("non UTF-8 symbol") from error
    elif kind == SCV_STRING:
        # An ScString holds bytes, not necessarily UTF-8 (a text memo carried as
        # ``to_muxed_id`` may be any 28 bytes): kept losslessly; ``string_bytes`` gives
        # them back exactly.
        value = reader.opaque().decode("utf-8", "surrogateescape")
    elif kind == SCV_ADDRESS:
        address_kind = reader.int32()
        if address_kind == SC_ADDRESS_ACCOUNT:
            if reader.int32() != 0:
                raise XdrError("unsupported public key type")
            value = ScAddress("account", encode_account_id(reader.take(32)))
        elif address_kind == SC_ADDRESS_CONTRACT:
            value = ScAddress("contract", encode_contract_id(reader.take(32)))
        elif address_kind == SC_ADDRESS_MUXED_ACCOUNT:
            # MuxedEd25519Account: the u64 id, then the base account's ed25519 key.
            muxed_id = struct.unpack(">Q", reader.take(8))[0]
            base = encode_account_id(reader.take(32))
            value = ScAddress("muxed_account", encode_muxed_account(base, muxed_id))
        elif address_kind == SC_ADDRESS_CLAIMABLE_BALANCE:
            # ClaimableBalanceID: union type (0, V0) and a 32-byte hash.
            if reader.int32() != 0:
                raise UnsupportedAddress(address_kind)
            value = ScAddress(
                "claimable_balance", encode_strkey("claimable_balance", b"\x00" + reader.take(32))
            )
        elif address_kind == SC_ADDRESS_LIQUIDITY_POOL:
            value = ScAddress("liquidity_pool", encode_strkey("liquidity_pool", reader.take(32)))
        else:
            raise UnsupportedAddress(address_kind)
    elif kind == SCV_I128:
        hi = struct.unpack(">q", reader.take(8))[0]
        lo = struct.unpack(">Q", reader.take(8))[0]
        value = (hi << 64) | lo
    else:
        raise XdrError(f"unsupported ScVal type {kind}")
    return value
