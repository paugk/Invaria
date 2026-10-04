from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from invaria.contracts import Quantity, StrictJsonError, parse_contract


def test_valid_quantity_from_json() -> None:
    q = parse_contract(Quantity, '{"atoms": "10000000000", "scale": 7, "unit": "FUND_SHARE"}')
    assert q.to_decimal_text() == "1000.0000000"


@pytest.mark.parametrize(
    "document",
    [
        '{"atoms": 1000.0, "scale": 2, "unit": "USD"}',
        '{"atoms": 1e3, "scale": 2, "unit": "USD"}',
        '{"atoms": "1000", "scale": 2.0, "unit": "USD"}',
    ],
)
def test_float_literals_rejected_before_validation(document: str) -> None:
    with pytest.raises(StrictJsonError):
        parse_contract(Quantity, document)


@pytest.mark.parametrize(
    "data",
    [
        {"atoms": 1000.0, "scale": 2, "unit": "USD"},
        {"atoms": 1000, "scale": 2, "unit": "USD"},
        {"atoms": "1000", "scale": "2", "unit": "USD"},
        {"atoms": "1000", "scale": True, "unit": "USD"},
        {"atoms": "1000", "scale": 2.0, "unit": "USD"},
        {"atoms": "NaN", "scale": 2, "unit": "USD"},
        {"atoms": "1000.00", "scale": 2, "unit": "USD"},
        {"atoms": "1e3", "scale": 2, "unit": "USD"},
        {"atoms": "-0", "scale": 2, "unit": "USD"},
        {"atoms": "007", "scale": 2, "unit": "USD"},
        {"atoms": "", "scale": 2, "unit": "USD"},
        {"atoms": "1000", "scale": -1, "unit": "USD"},
        {"atoms": "1000", "scale": 39, "unit": "USD"},
        {"atoms": "1000", "scale": 2, "unit": "usd"},
        {"atoms": "1000", "scale": 2, "unit": ""},
        {"atoms": "1000", "scale": 2},
        {"atoms": "1000", "unit": "USD"},
        {"scale": 2, "unit": "USD"},
        {"atoms": "1000", "scale": 2, "unit": "USD", "currency": "USD"},
        {"atoms": "9" * 80, "scale": 2, "unit": "USD"},
    ],
)
def test_invalid_quantities_rejected(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Quantity.model_validate(data)


def test_quantity_is_immutable() -> None:
    q = Quantity(atoms="1", scale=0, unit="USD")
    with pytest.raises(ValidationError):
        q.atoms = "2"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("text", "scale", "atoms"),
    [
        ("100000.00", 2, "10000000"),
        ("100000", 2, "10000000"),
        ("99500.5", 2, "9950050"),
        ("1000.0000000", 7, "10000000000"),
        ("-500.00", 2, "-50000"),
        ("-0.00", 2, "0"),
        ("0", 0, "0"),
    ],
)
def test_from_decimal_text_is_exact(text: str, scale: int, atoms: str) -> None:
    assert Quantity.from_decimal_text(text, scale=scale, unit="USD").atoms == atoms


@pytest.mark.parametrize("text", ["100.001", "1e3", "1,000.00", "1.000,00", " 1", "+1", ".5", ""])
def test_from_decimal_text_rejects_rounding_and_locale(text: str) -> None:
    with pytest.raises(ValueError):
        Quantity.from_decimal_text(text, scale=2, unit="USD")


@given(st.integers(min_value=-(10**30), max_value=10**30), st.integers(min_value=0, max_value=38))
def test_decimal_text_round_trip(value: int, scale: int) -> None:
    q = Quantity(atoms=str(value), scale=scale, unit="FUND_SHARE")
    assert Quantity.from_decimal_text(q.to_decimal_text(), scale=scale, unit="FUND_SHARE") == q
