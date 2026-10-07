"""Thin readers for Horizon (REST) and Stellar RPC (JSON-RPC). Read-only.

Every call returns the exact response bytes and their sha256 so observations can cite
them. JSON numbers with a fraction are kept as strings: nothing becomes a float.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from invaria.contracts.stellar import NETWORK_PASSPHRASES
from invaria.stellar.http import ChainUnavailable, HttpClient

DEFAULT_HORIZON_URL = "https://horizon-testnet.stellar.org"
DEFAULT_RPC_URL = "https://soroban-testnet.stellar.org"
ENV_HORIZON_URL = "INVARIA_STELLAR_HORIZON_URL"
ENV_RPC_URL = "INVARIA_STELLAR_RPC_URL"


class DataUnavailable(Exception):
    """The requested range is outside what the provider holds. Never means 'no activity'."""


class NetworkMismatch(Exception):
    """An endpoint reports a different network than expected."""


@dataclass(frozen=True)
class Endpoints:
    horizon_url: str
    rpc_url: str

    @classmethod
    def resolve(cls, horizon: str | None = None, rpc: str | None = None) -> Endpoints:
        """Flags win over environment variables, which win over the public testnet URLs."""
        return cls(
            horizon_url=(horizon or os.environ.get(ENV_HORIZON_URL) or DEFAULT_HORIZON_URL).rstrip(
                "/"
            ),
            rpc_url=(rpc or os.environ.get(ENV_RPC_URL) or DEFAULT_RPC_URL).rstrip("/"),
        )


@dataclass(frozen=True)
class Page:
    locator: str
    raw: bytes
    sha256: str
    document: dict[str, Any]


def _parse(raw: bytes, locator: str) -> dict[str, Any]:
    try:
        document = json.loads(raw.decode("utf-8"), parse_float=str, parse_constant=str)
    except (UnicodeDecodeError, ValueError) as error:
        raise ChainUnavailable(f"{locator}: response is not JSON") from error
    if not isinstance(document, dict):
        raise ChainUnavailable(f"{locator}: unexpected JSON shape")
    return document


class Horizon:
    def __init__(self, base_url: str, client: HttpClient) -> None:
        self.base_url, self.client = base_url, client

    def get(self, path: str, query: dict[str, str] | None = None) -> Page:
        suffix = f"{path}?{urlencode(query)}" if query else path
        response = self.client.request("GET", f"{self.base_url}{suffix}", None)
        locator = f"horizon:{suffix}"
        if response.status == 404:
            raise DataUnavailable(f"{locator}: not found")
        if response.status != 200:
            raise ChainUnavailable(f"{locator}: HTTP {response.status}")
        return Page(
            locator,
            response.body,
            hashlib.sha256(response.body).hexdigest(),
            _parse(response.body, locator),
        )

    def root(self) -> Page:
        return self.get("/")

    def ledger(self, sequence: int) -> Page:
        return self.get(f"/ledgers/{sequence}")

    def asset(self, code: str, issuer: str) -> Page:
        return self.get("/assets", {"asset_code": code, "asset_issuer": issuer})

    def account_payments(self, account: str, cursor: str, limit: int) -> Page:
        return self.get(
            f"/accounts/{account}/payments",
            {
                "order": "asc",
                "limit": str(limit),
                "cursor": cursor,
                "join": "transactions",
                "include_failed": "true",
            },
        )


class Rpc:
    def __init__(self, url: str, client: HttpClient) -> None:
        self.url, self.client = url, client

    def call(self, method: str, params: dict[str, Any] | None = None) -> Page:
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            request["params"] = params
        body = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        response = self.client.request("POST", self.url, body)
        locator = f"rpc:{method}#req={hashlib.sha256(body).hexdigest()[:16]}"
        if response.status != 200:
            raise ChainUnavailable(f"{locator}: HTTP {response.status}")
        document = _parse(response.body, locator)
        if "error" in document:
            error = document["error"]
            message = str(error.get("message", "")) if isinstance(error, dict) else str(error)
            if "must be within the ledger range" in message:
                raise DataUnavailable(f"{locator}: {message}")
            raise ChainUnavailable(f"{locator}: {message}")
        if not isinstance(document.get("result"), dict):
            raise ChainUnavailable(f"{locator}: missing result")
        return Page(locator, response.body, hashlib.sha256(response.body).hexdigest(), document)

    def get_network(self) -> Page:
        return self.call("getNetwork")

    def get_events(
        self,
        contract_id: str,
        *,
        start_ledger: int | None,
        end_ledger: int,
        cursor: str | None,
        limit: int,
    ) -> Page:
        pagination: dict[str, Any] = {"limit": limit}
        params: dict[str, Any] = {
            "filters": [{"type": "contract", "contractIds": [contract_id]}],
            "pagination": pagination,
        }
        if cursor is not None:
            pagination["cursor"] = cursor  # RPC rejects ledger bounds together with a cursor
        else:
            params["startLedger"] = start_ledger
            # ``end_ledger`` is inclusive here; RPC's endLedger is exclusive (observed on
            # testnet: 5061232-5061233 omits ledger 5061233, 5061232-5061234 returns it).
            params["endLedger"] = end_ledger + 1
        return self.call("getEvents", params)


@dataclass(frozen=True)
class NetworkCheck:
    network: str
    passphrase: str
    horizon_latest_ledger: int
    horizon_elder_ledger: int
    protocol_version: int
    evidence: tuple[str, ...]


def verify_network(horizon: Horizon, rpc: Rpc, expected_network: str) -> NetworkCheck:
    expected = NETWORK_PASSPHRASES[expected_network]
    root = horizon.root()
    network = rpc.get_network()
    horizon_passphrase = str(root.document.get("network_passphrase"))
    rpc_passphrase = str(network.document["result"].get("passphrase"))
    if horizon_passphrase != expected:
        raise NetworkMismatch(f"Horizon reports {horizon_passphrase!r}, expected {expected!r}")
    if rpc_passphrase != expected:
        raise NetworkMismatch(f"RPC reports {rpc_passphrase!r}, expected {expected!r}")
    return NetworkCheck(
        network=expected_network,
        passphrase=expected,
        horizon_latest_ledger=int(root.document["history_latest_ledger"]),
        horizon_elder_ledger=int(root.document["history_elder_ledger"]),
        protocol_version=int(root.document["current_protocol_version"]),
        evidence=(
            f"{root.locator} sha256={root.sha256}",
            f"{network.locator} sha256={network.sha256}",
        ),
    )
