"""Muxed sub-accounts, u64 ids and memos in the contracts.

A u64 is text from end to end (no float can hold it), 0 is an id distinct from an absent
one, an M address decodes to its base account and id without naming a holder, and a memo
keeps its type and exact value.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from invaria.contracts.base import parse_contract
from invaria.contracts.identity import IdentityLink
from invaria.contracts.observation import (
    Observation,
    TokenMovementPayload,
    TransactionMemo,
    is_muxed,
    movement_addresses,
    movement_receiver,
)
from invaria.contracts.stellar import (
    U64_MAX,
    base_account,
    decode_muxed_account,
    encode_muxed_account,
    parse_u64,
)

BASE = "GA7QYNF7SOWQ3GLR2BGMZEHXAVIRZA4KVWLTJJFC7MGXUA74P7UJVSGZ"
OTHER = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"
# SEP-23 test vectors: the same base account with ids 0 and 2^63.
M_ZERO = "MA7QYNF7SOWQ3GLR2BGMZEHXAVIRZA4KVWLTJJFC7MGXUA74P7UJUAAAAAAAAAAAACJUQ"
M_HIGH = "MA7QYNF7SOWQ3GLR2BGMZEHXAVIRZA4KVWLTJJFC7MGXUA74P7UJVAAAAAAAAAAAAAJLK"


def test_sep23_vectors_decode_to_base_and_id() -> None:
    assert decode_muxed_account(M_ZERO) == (BASE, 0)
    assert decode_muxed_account(M_HIGH) == (BASE, 2**63)
    assert encode_muxed_account(BASE, 0) == M_ZERO
    assert encode_muxed_account(BASE, 2**63) == M_HIGH
    assert base_account(M_HIGH) == BASE and base_account(BASE) == BASE


@pytest.mark.parametrize(
    "bad",
    [
        M_ZERO[:-1] + ("A" if M_ZERO[-1] != "A" else "B"),  # checksum
        M_ZERO.lower(),
        "G" + M_ZERO[1:],
        M_ZERO + "A",
        BASE,
    ],
)
def test_malformed_muxed_addresses_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        decode_muxed_account(bad)


@pytest.mark.parametrize("value", ["0", "1", "42", "18446744073709551615", "9007199254740993"])
def test_canonical_u64_text_round_trips_exactly(value: str) -> None:
    assert str(parse_u64(value)) == value
    m = encode_muxed_account(BASE, parse_u64(value))
    assert decode_muxed_account(m) == (BASE, int(value))


@pytest.mark.parametrize(
    "bad", ["", "-1", "01", "1.0", "1e3", " 1", "18446744073709551616", "+1", "0x10"]
)
def test_non_canonical_or_out_of_range_u64_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_u64(bad)


@pytest.mark.parametrize("out_of_range", [-1, U64_MAX + 1])
def test_a_muxed_address_cannot_encode_an_id_outside_u64(out_of_range: int) -> None:
    with pytest.raises(ValueError):
        encode_muxed_account(BASE, out_of_range)


def _movement(**fields: object) -> dict[str, object]:
    return {
        "payload_type": "token_movement",
        "from_address": OTHER,
        "to_address": BASE,
        "units": {"atoms": "10", "scale": 7, "unit": "USDC"},
        "chain": {
            "network": "stellar:testnet",
            "ledger": 5,
            "tx_hash": "ab" * 32,
            "operation_index": 0,
            "tx_successful": True,
        },
        **fields,
    }


def test_zero_is_a_sub_account_and_absent_is_not() -> None:
    zero = TokenMovementPayload.model_validate(_movement(to_muxed_id="0"))
    plain = TokenMovementPayload.model_validate(_movement())
    assert is_muxed(zero) and not is_muxed(plain)
    assert movement_receiver(zero) == M_ZERO and movement_receiver(plain) == BASE
    assert '"to_muxed_id":"0"' in zero.model_dump_json()
    assert "muxed" not in plain.model_dump_json() and "memo" not in plain.model_dump_json()
    assert movement_addresses(zero) == {OTHER, BASE, M_ZERO}


def test_the_largest_u64_survives_json_and_a_float_cannot() -> None:
    top = TokenMovementPayload.model_validate(_movement(to_muxed_id=str(U64_MAX)))
    again = TokenMovementPayload.model_validate_json(top.model_dump_json())
    assert again.to_muxed_id == "18446744073709551615"
    assert float(U64_MAX) != U64_MAX  # why it is never a JSON number
    with pytest.raises(ValidationError):
        TokenMovementPayload.model_validate(_movement(to_muxed_id=U64_MAX))  # a number
    with pytest.raises(ValidationError):
        TokenMovementPayload.model_validate(_movement(to_muxed_id=str(U64_MAX + 1)))


def test_an_observation_with_a_u64_number_is_rejected_by_strict_parsing() -> None:
    document = {
        "schema_version": "1.0",
        "observation_id": "obs-x",
        "tenant_id": "t",
        "kind": "assertion",
        "fact_type": "token_movement",
        "instrument_id": "i",
        "representation_id": "r",
        "operation_ref": None,
        "source": {"source_id": "s", "record_key": "k", "revision": 1},
        "valid_time": "2026-10-07T00:00:00Z",
        "recorded_at": "2026-10-07T00:00:00Z",
        "provenance": {
            "raw_sha256": "cd" * 32,
            "raw_locator": "x",
            "parser_ref": "p@1.0.0",
            "mapping_ref": "m@1.0.0",
        },
        "supersedes": None,
        "payload": _movement(to_muxed_id="7"),
        "synthetic": True,
    }
    parsed = parse_contract(Observation, json.dumps(document))
    assert isinstance(parsed.payload, TokenMovementPayload) and parsed.payload.to_muxed_id == "7"
    document["payload"] = _movement(to_muxed_id=7)
    with pytest.raises(ValueError):
        parse_contract(Observation, json.dumps(document))


@pytest.mark.parametrize(
    ("memo_type", "value"),
    [
        ("none", None),
        ("text", ""),  # an empty text memo is a memo, not "none"
        ("text", "U1VCLTAwMDE="),
        ("text", "//79"),  # not UTF-8: kept as bytes
        ("id", "0"),
        ("id", "18446744073709551615"),
        ("hash", "00" * 32),
        ("return", "ff" * 32),
    ],
)
def test_memos_keep_their_type_and_exact_value(memo_type: str, value: str | None) -> None:
    memo = TransactionMemo.model_validate({"memo_type": memo_type, "value": value})
    assert TransactionMemo.model_validate_json(memo.model_dump_json()) == memo


@pytest.mark.parametrize(
    ("memo_type", "value"),
    [
        ("none", ""),
        ("text", None),
        ("text", "not base64!"),
        ("text", "A" * 40),  # 30 bytes > 28
        ("text", "QR=="),  # non-zero padding bits: not canonical
        ("id", "-1"),
        ("id", "18446744073709551616"),
        ("hash", "AB" * 32),  # uppercase hex
        ("hash", "ab" * 31),
        ("return", None),
        ("other", "x"),
    ],
)
def test_inexact_memos_are_rejected(memo_type: str, value: str | None) -> None:
    with pytest.raises(ValidationError):
        TransactionMemo.model_validate({"memo_type": memo_type, "value": value})


def _link(schema: str, address: str) -> dict[str, object]:
    return {
        "schema_version": schema,
        "link_id": "link-1",
        "account_ref": "INV-1",
        "network": "stellar:testnet",
        "address": address,
        "valid_from": "2026-10-01T00:00:00Z",
        "valid_to": None,
        "approval_ref": "approval-1",
        "recorded_at": "2026-10-01T00:00:00Z",
    }


def test_identity_link_1_1_names_a_muxed_sub_account_and_1_0_cannot() -> None:
    link = parse_contract(IdentityLink, json.dumps(_link("1.1", M_HIGH)))
    assert link.address == M_HIGH
    assert parse_contract(IdentityLink, json.dumps(_link("1.0", BASE))).address == BASE
    with pytest.raises(ValueError, match=r"schema 1\.1"):
        parse_contract(IdentityLink, json.dumps(_link("1.0", M_HIGH)))
    with pytest.raises(ValueError):
        parse_contract(IdentityLink, json.dumps(_link("1.1", M_HIGH[:-1] + "A")))
