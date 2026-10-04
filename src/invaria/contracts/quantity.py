"""Exact quantities: integer atoms as a string, explicit scale and unit. Never float."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import Field, StringConstraints

from invaria.contracts.base import Contract

MAX_SCALE = 38
MAX_ATOM_DIGITS = 78

Atoms = Annotated[
    str,
    StringConstraints(pattern=r"^(0|-?[1-9][0-9]*)$", max_length=MAX_ATOM_DIGITS + 1),
]
Unit = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,31}$")]

_DECIMAL_TEXT = re.compile(r"^(-?)([0-9]+)(?:\.([0-9]+))?$")


class Quantity(Contract):
    """``{"atoms": "1000000000", "scale": 7, "unit": "FUND_SHARE"}`` is 100 shares."""

    atoms: Atoms
    scale: Annotated[int, Field(ge=0, le=MAX_SCALE)]
    unit: Unit

    @classmethod
    def from_decimal_text(cls, text: str, *, scale: int, unit: str) -> Quantity:
        """Parse plain decimal text exactly. Rejects anything that would need rounding.

        Only ``[-]digits[.digits]`` is accepted: no exponent, no grouping, no locale.
        Interpreting separators belongs to a versioned mapping, not to this contract.
        """
        match = _DECIMAL_TEXT.fullmatch(text)
        if match is None:
            raise ValueError(f"not a plain decimal: {text!r}")
        sign, integer, fraction = match.group(1), match.group(2), match.group(3) or ""
        if len(fraction) > scale:
            raise ValueError(f"{text!r} has more than {scale} decimals; rounding is rejected")
        magnitude = int(integer + fraction.ljust(scale, "0"))
        atoms = str(-magnitude if sign and magnitude else magnitude)
        return cls(atoms=atoms, scale=scale, unit=unit)

    def to_decimal_text(self) -> str:
        """Render without rounding, for explanations."""
        value = int(self.atoms)
        digits = str(abs(value)).rjust(self.scale + 1, "0")
        sign = "-" if value < 0 else ""
        if self.scale == 0:
            return f"{sign}{digits}"
        return f"{sign}{digits[: -self.scale]}.{digits[-self.scale :]}"
