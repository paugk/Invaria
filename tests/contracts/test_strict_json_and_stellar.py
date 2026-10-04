from __future__ import annotations

import hashlib

import pytest

from invaria.contracts import StrictJsonError, check_strict_json
from invaria.contracts.stellar import decode_account_id, encode_account_id

# Public example account from Stellar documentation; used only as a StrKey format vector.
DOC_VECTOR = "GAAZI4TCR3TY5OJHCTJC2A4QSY6CJWJH5IAJTGKIN2ER7LBNVKOCCWN7"


@pytest.mark.parametrize(
    "document",
    [
        '{"a": "1", "a": "2"}',
        '{"outer": {"x": 1, "x": 1}}',
        '{"value": 0.5}',
        '{"values": [1, 2e2]}',
        '{"value": NaN}',
        '{"value": Infinity}',
    ],
)
def test_strict_json_rejects(document: str) -> None:
    with pytest.raises(StrictJsonError):
        check_strict_json(document)


def test_strict_json_accepts_integers_and_strings() -> None:
    check_strict_json('{"ledger": 1000123, "amount": "1000.0000000"}')


def test_account_id_round_trip_on_documented_vector() -> None:
    assert encode_account_id(decode_account_id(DOC_VECTOR)) == DOC_VECTOR


def test_account_id_checksum_detected() -> None:
    tampered = DOC_VECTOR[:-1] + ("A" if DOC_VECTOR[-1] != "A" else "B")
    with pytest.raises(ValueError):
        decode_account_id(tampered)


@pytest.mark.parametrize(
    "value",
    [
        DOC_VECTOR.lower(),
        "S" + DOC_VECTOR[1:],
        DOC_VECTOR[:-1],
        "",
    ],
)
def test_account_id_malformed(value: str) -> None:
    with pytest.raises(ValueError):
        decode_account_id(value)


def test_encode_rejects_wrong_key_length() -> None:
    with pytest.raises(ValueError):
        encode_account_id(hashlib.sha256(b"x").digest()[:31])
