"""HTTP access for the read-only adapter: bounded timeouts/retries, recording and replay.

Only GET (Horizon) and JSON-RPC POST (RPC) are issued. No credentials are sent.
Recorded exchanges make every test reproducible offline.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
USER_AGENT = "invaria-stellar-adapter/0.1 (read-only)"


class ChainUnavailable(Exception):
    """Endpoint unreachable or failing after bounded retries; never means 'no data'."""


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes


class HttpClient(Protocol):
    def request(self, method: str, url: str, body: bytes | None) -> Response: ...


def request_key(method: str, url: str, body: bytes | None) -> str:
    material = method.encode() + b"\n" + url.encode() + b"\n" + (body or b"")
    return hashlib.sha256(material).hexdigest()


class UrllibClient:
    def __init__(
        self,
        *,
        timeout_seconds: int = 15,
        retries: int = 3,
        backoff_ms: int = 500,
        sleep_ms: Callable[[int], None] | None = None,
    ) -> None:
        self.timeout, self.retries, self.backoff_ms = timeout_seconds, retries, backoff_ms
        self.sleep_ms = sleep_ms or (lambda ms: time.sleep(ms / 1000))

    def request(self, method: str, url: str, body: bytes | None) -> Response:
        if not url.startswith("https://"):
            raise ChainUnavailable(f"refusing non-HTTPS endpoint {url}")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        last = "no attempt"
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = resp.read(MAX_RESPONSE_BYTES + 1)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise ChainUnavailable(f"response from {url} exceeds size limit")
                    return Response(resp.status, data)
            except urllib.error.HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504):
                    return Response(error.code, error.read(MAX_RESPONSE_BYTES))
                last = f"HTTP {error.code}"
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                last = type(error).__name__
            if attempt < self.retries:
                self.sleep_ms(self.backoff_ms * (2**attempt))
        raise ChainUnavailable(f"{method} {url} failed after {self.retries + 1} attempts: {last}")


class RecordingClient:
    """Wraps a live client and stores every exchange with capture time and hashes."""

    def __init__(self, inner: HttpClient, directory: Path) -> None:
        self.inner, self.directory = inner, directory
        directory.mkdir(parents=True, exist_ok=True)

    def request(self, method: str, url: str, body: bytes | None) -> Response:
        response = self.inner.request(method, url, body)
        key = request_key(method, url, body)
        (self.directory / f"{key}.body").write_bytes(response.body)
        meta = {
            "method": method,
            "url": url,
            "request_body": body.decode("utf-8") if body else None,
            "status": response.status,
            "response_sha256": hashlib.sha256(response.body).hexdigest(),
            "captured_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
        (self.directory / f"{key}.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n", "utf-8"
        )
        return response


class ReplayClient:
    """Serves recorded exchanges; unknown requests fail loudly (no silent network)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def request(self, method: str, url: str, body: bytes | None) -> Response:
        key = request_key(method, url, body)
        meta_path = self.directory / f"{key}.json"
        if not meta_path.is_file():
            raise ChainUnavailable(f"no recorded exchange for {method} {url}")
        meta: dict[str, Any] = json.loads(meta_path.read_text("utf-8"))
        data = (self.directory / f"{key}.body").read_bytes()
        if hashlib.sha256(data).hexdigest() != meta["response_sha256"]:
            raise ChainUnavailable(f"recorded body for {url} does not match its sha256")
        return Response(int(meta["status"]), data)
