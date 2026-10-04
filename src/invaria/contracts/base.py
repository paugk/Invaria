"""Shared building blocks for Invaria contracts.

Contracts are pure data definitions: no I/O besides JSON parsing, no clock, no network,
no dependency on API frameworks, databases, blockchain SDKs or language models.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, StringConstraints

SCHEMA_VERSION = "1.0"
SchemaVersion = Literal["1.0"]


class Contract(BaseModel):
    """Closed, immutable, strictly typed record.

    strict=True means no silent coercion: a JSON number never becomes a string amount
    and a string never becomes an integer scale.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be expressed in UTC (offset +00:00 or Z)")
    return value


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_require_utc)]

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
"""Opaque identifier, unique within a tenant."""

VersionRef = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}@[0-9]+\.[0-9]+\.[0-9]+$")
]
"""Reference to a versioned artifact, e.g. ``bank-csv-synthetic@1.0.0``."""

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]

NonEmptyText = Annotated[str, StringConstraints(min_length=1, max_length=2000)]


class StrictJsonError(ValueError):
    """Raised when a JSON document violates Invaria's parsing rules."""


def _reject_float(literal: str) -> Any:
    raise StrictJsonError(f"JSON number with fraction/exponent is not allowed: {literal}")


def _reject_constant(literal: str) -> Any:
    raise StrictJsonError(f"JSON constant is not allowed: {literal}")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def check_strict_json(text: str) -> None:
    """Reject duplicate keys, fractional/exponent numbers and NaN/Infinity anywhere."""
    json.loads(
        text,
        object_pairs_hook=_reject_duplicates,
        parse_float=_reject_float,
        parse_constant=_reject_constant,
    )


def parse_contract[M: BaseModel](model: type[M], text: str) -> M:
    """Parse a JSON document into a contract after the strict JSON checks."""
    check_strict_json(text)
    return model.model_validate_json(text)
