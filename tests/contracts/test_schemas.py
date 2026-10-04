from __future__ import annotations

from pathlib import Path

import pytest

from invaria.contracts.schema_export import CONTRACTS, render

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_committed_schema_matches_models(name: str) -> None:
    committed = (SCHEMAS / f"{name}.schema.json").read_text("utf-8")
    assert committed == render(name, CONTRACTS[name]), (
        "schema drift: run `uv run python -m invaria.contracts.schema_export`"
    )


def test_no_orphan_schema_files() -> None:
    assert {p.name for p in SCHEMAS.glob("*.schema.json")} == {
        f"{name}.schema.json" for name in CONTRACTS
    }
