"""Stellar identifiers needed by the contracts. Standard library only; not an SDK.

Account IDs use the StrKey format: base32(version byte + 32-byte key + CRC16-XModem
little-endian), 56 characters starting with ``G``.
"""

from __future__ import annotations

import base64
import re
from typing import Annotated, Literal

from pydantic import AfterValidator

NETWORK_PASSPHRASES: dict[str, str] = {
    "stellar:testnet": "Test SDF Network ; September 2015",
    "stellar:pubnet": "Public Global Stellar Network ; September 2015",
}
StellarNetwork = Literal["stellar:testnet", "stellar:pubnet"]

_ACCOUNT_ID_VERSION = 6 << 3


def _crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def encode_account_id(public_key: bytes) -> str:
    if len(public_key) != 32:
        raise ValueError("an ed25519 public key has 32 bytes")
    payload = bytes([_ACCOUNT_ID_VERSION]) + public_key
    checksum = _crc16_xmodem(payload).to_bytes(2, "little")
    return base64.b32encode(payload + checksum).decode("ascii").rstrip("=")


def decode_account_id(account_id: str) -> bytes:
    if len(account_id) != 56 or not account_id.startswith("G"):
        raise ValueError("a Stellar account ID has 56 characters and starts with 'G'")
    try:
        raw = base64.b32decode(account_id, casefold=False)
    except ValueError as error:
        raise ValueError("account ID is not valid base32") from error
    payload, checksum = raw[:-2], raw[-2:]
    if payload[0] != _ACCOUNT_ID_VERSION:
        raise ValueError("unexpected StrKey version byte for an account ID")
    if _crc16_xmodem(payload).to_bytes(2, "little") != checksum:
        raise ValueError("account ID checksum mismatch")
    if encode_account_id(payload[1:]) != account_id:
        raise ValueError("account ID is not in canonical form")
    return payload[1:]


def _validate_account_id(value: str) -> str:
    decode_account_id(value)
    return value


StellarAccountId = Annotated[str, AfterValidator(_validate_account_id)]


# ------------------------------------------------- other ledger entities
# SEP-23 strkeys: the version byte gives the entity kind, so an identifier never reads as an
# account. Contracts (C) and liquidity pools (L) carry a 32-byte id; claimable balances (B)
# one type byte (0, V0) and a 32-byte hash.
_STRKEY_VERSION = {"contract": 2 << 3, "claimable_balance": 1 << 3, "liquidity_pool": 11 << 3}
_STRKEY_LENGTH = {"contract": 32, "claimable_balance": 33, "liquidity_pool": 32}
StrkeyKind = Literal["contract", "claimable_balance", "liquidity_pool"]


def encode_strkey(kind: StrkeyKind, payload: bytes) -> str:
    if len(payload) != _STRKEY_LENGTH[kind]:
        raise ValueError(f"a {kind} id has {_STRKEY_LENGTH[kind]} bytes")
    if kind == "claimable_balance" and payload[0] != 0:
        raise ValueError("only claimable balance ids of type V0 are known")
    data = bytes([_STRKEY_VERSION[kind]]) + payload
    checksum = _crc16_xmodem(data).to_bytes(2, "little")
    return base64.b32encode(data + checksum).decode("ascii").rstrip("=")


def decode_strkey(kind: StrkeyKind, value: str) -> bytes:
    prefix = {"contract": "C", "claimable_balance": "B", "liquidity_pool": "L"}[kind]
    if not value.startswith(prefix):
        raise ValueError(f"a {kind} id starts with {prefix!r}")
    try:
        raw = base64.b32decode(value + "=" * (-len(value) % 8), casefold=False)
    except ValueError as error:
        raise ValueError(f"{kind} id is not valid base32") from error
    data, checksum = raw[:-2], raw[-2:]
    if len(data) != _STRKEY_LENGTH[kind] + 1 or data[0] != _STRKEY_VERSION[kind]:
        raise ValueError(f"unexpected StrKey version or length for a {kind} id")
    if _crc16_xmodem(data).to_bytes(2, "little") != checksum:
        raise ValueError(f"{kind} id checksum mismatch")
    if encode_strkey(kind, data[1:]) != value:
        raise ValueError(f"{kind} id is not in canonical form")
    return data[1:]


# ----------------------------------------------------- muxed accounts
# SEP-23 M strkey: version byte 12 << 3, the base account's 32-byte ed25519 key and the
# 64-bit sub-account id, big-endian. Decoding one only says which base account and id the
# bytes name: who holds that sub-account is never inferred from it.
_MUXED_VERSION = 12 << 3
U64_MAX = 2**64 - 1
_U64_TEXT = re.compile(r"^(0|[1-9][0-9]{0,19})$")


def parse_u64(text: str) -> int:
    """A u64 written in canonical decimal (no sign, no leading zeros); 0 is a valid id."""
    if not isinstance(text, str) or not _U64_TEXT.match(text) or int(text) > U64_MAX:
        raise ValueError(f"{text!r} is not a canonical decimal u64")
    return int(text)


def _validate_u64_text(value: str) -> str:
    parse_u64(value)
    return value


# Kept as text end to end (contracts, JSON, PostgreSQL jsonb, queries, HTML): a u64 does not
# fit an IEEE double, and 0 is an id, distinct from an absent one (None).
U64Text = Annotated[str, AfterValidator(_validate_u64_text)]


def encode_muxed_account(account_id: str, muxed_id: int) -> str:
    if not 0 <= muxed_id <= U64_MAX:
        raise ValueError("a muxed account id is a u64")
    data = bytes([_MUXED_VERSION]) + decode_account_id(account_id) + muxed_id.to_bytes(8, "big")
    checksum = _crc16_xmodem(data).to_bytes(2, "little")
    return base64.b32encode(data + checksum).decode("ascii").rstrip("=")


def decode_muxed_account(value: str) -> tuple[str, int]:
    """(base G account, u64 id) of an M strkey; raises on anything else."""
    if len(value) != 69 or not value.startswith("M"):
        raise ValueError("a muxed account has 69 characters and starts with 'M'")
    try:
        raw = base64.b32decode(value + "=" * (-len(value) % 8), casefold=False)
    except ValueError as error:
        raise ValueError("muxed account is not valid base32") from error
    data, checksum = raw[:-2], raw[-2:]
    if len(data) != 41 or data[0] != _MUXED_VERSION:
        raise ValueError("unexpected StrKey version or length for a muxed account")
    if _crc16_xmodem(data).to_bytes(2, "little") != checksum:
        raise ValueError("muxed account checksum mismatch")
    base, muxed_id = encode_account_id(data[1:33]), int.from_bytes(data[33:], "big")
    if encode_muxed_account(base, muxed_id) != value:
        raise ValueError("muxed account is not in canonical form")
    return base, muxed_id


def _validate_muxed_account(value: str) -> str:
    decode_muxed_account(value)
    return value


StellarMuxedAccountId = Annotated[str, AfterValidator(_validate_muxed_account)]


def _validate_account_or_muxed(value: str) -> str:
    if value.startswith("M"):
        decode_muxed_account(value)
    else:
        decode_account_id(value)
    return value


# An address an IdentityLink can approve from schema 1.1: a base account (G) or a muxed
# sub-account (M). A link to a base account never approves its sub-accounts, nor the reverse.
StellarAddress = Annotated[str, AfterValidator(_validate_account_or_muxed)]


def base_account(address: str) -> str:
    """The base G account of a G or M address (its technical decoding, not its holder)."""
    return decode_muxed_account(address)[0] if address.startswith("M") else address


def claimable_balance_hex(strkey: str) -> str:
    """Horizon's native form of a claimable balance id: the XDR ``ClaimableBalanceID``
    (4-byte type, 0 for V0, and the 32-byte hash) in hex."""
    payload = decode_strkey("claimable_balance", strkey)
    return (b"\x00\x00\x00\x00" + payload[1:]).hex()


def claimable_balance_strkey(native: str) -> str:
    raw = bytes.fromhex(native)
    if len(raw) != 36 or raw[:4] != b"\x00\x00\x00\x00":
        raise ValueError("a claimable balance id is a V0 type and a 32-byte hash")
    return encode_strkey("claimable_balance", b"\x00" + raw[4:])


def liquidity_pool_hex(strkey: str) -> str:
    """Horizon's native form of a liquidity pool id: the 32-byte pool id in hex."""
    return decode_strkey("liquidity_pool", strkey).hex()


def liquidity_pool_strkey(native: str) -> str:
    return encode_strkey("liquidity_pool", bytes.fromhex(native))
