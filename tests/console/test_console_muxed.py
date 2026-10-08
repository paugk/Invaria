"""A muxed movement and its memo as the console shows them.

The u64 sub-account id is shown as the exact text stored (never a number that could round),
next to the M address it derives, the holder is said to be unknown, and the memo keeps its
type apart from its value. Synthetic observation, rendered without a database.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from invaria.console.pages import evidence_page
from invaria.contracts.observation import Observation
from invaria.contracts.stellar import U64_MAX, encode_muxed_account
from invaria.query.models import EvidenceView

BASE = "GA7QYNF7SOWQ3GLR2BGMZEHXAVIRZA4KVWLTJJFC7MGXUA74P7UJVSGZ"
SENDER = "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5"


def _observation(to_muxed_id: str) -> Observation:
    when = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    return Observation.model_validate_json(
        json.dumps(
            {
                "schema_version": "1.0",
                "observation_id": "obs-muxed-console",
                "tenant_id": "tenant-synthetic",
                "kind": "assertion",
                "fact_type": "token_movement",
                "instrument_id": "synthetic:instrument",
                "representation_id": "rep-synthetic",
                "operation_ref": None,
                "source": {"source_id": "stellar-synthetic", "record_key": "k", "revision": 1},
                "valid_time": when.isoformat(),
                "recorded_at": when.isoformat(),
                "provenance": {
                    "raw_sha256": "ab" * 32,
                    "raw_locator": "synthetic#0",
                    "parser_ref": "stellar-horizon-payments-parser@1.0.0",
                    "mapping_ref": "stellar-classic-payment@1.1.0",
                },
                "supersedes": None,
                "payload": {
                    "payload_type": "token_movement",
                    "from_address": SENDER,
                    "to_address": BASE,
                    "units": {"atoms": "10", "scale": 7, "unit": "SYNUSD"},
                    "chain": {
                        "network": "stellar:testnet",
                        "ledger": 9,
                        "tx_hash": "cd" * 32,
                        "operation_index": 0,
                        "tx_successful": True,
                    },
                    "to_muxed_id": to_muxed_id,
                    "memo": {"memo_type": "id", "value": to_muxed_id},
                },
                "synthetic": True,
            }
        )
    )


def _page(to_muxed_id: str) -> str:
    view = EvidenceView(
        kind="observation",
        content_trust="untrusted_source_data",
        observation=_observation(to_muxed_id),
        coverage=None,
        limitations=["synthetic"],
    )
    return evidence_page(view).decode("utf-8")


def test_the_largest_sub_account_id_is_shown_exactly_with_its_m_address() -> None:
    page = _page(str(U64_MAX))
    assert "18446744073709551615" in page and "1.8446744073709552e+19" not in page
    assert encode_muxed_account(BASE, U64_MAX) in page
    assert "not known from the id" in page


def test_zero_is_shown_as_a_sub_account_and_the_memo_apart() -> None:
    page = _page("0")
    assert encode_muxed_account(BASE, 0) in page
    # The memo id 0 is shown with its type; it is not the sub-account, nor a link.
    assert "<code>id</code>" in page and "never a link to an operation" in page
