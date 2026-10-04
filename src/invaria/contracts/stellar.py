"""Stellar identifiers needed by the contracts. Standard library only; not an SDK.

Account IDs use the StrKey format: base32(version byte + 32-byte key + CRC16-XModem
little-endian), 56 characters starting with ``G``.
"""

from __future__ import annotations

import base64
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
