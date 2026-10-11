"""Authorized event feed by cursor. Pure: no I/O.

The cursor is opaque, versioned and **authenticated**: base64url of a JSON body plus an
HMAC-SHA256 tag computed with the current feed generation's server-side key over the
tenant and the body. A client cannot forge or edit it (position, filter, or the readable
types it was issued with) without the tag failing, so the notices about permission
changes are statements about what this server issued, not about what a client wrote. The
cursor still grants nothing: permissions are recomputed from the access profile on every
request, and the readable types in it are only compared to emit notices.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, get_args

from pydantic import ValidationError

from invaria.contracts.base import StrictJsonError
from invaria.contracts.events import (
    SCHEMA_VERSION,
    AnyEvent,
    EventType,
    canonical_bytes,
    parse_event,
)
from invaria.query import disclosure

CURSOR_VERSION = 1
CURSOR_ORDER = "tenant_seq"
MAX_CURSOR_CHARS = 1024
CATALOG: tuple[str, ...] = tuple(sorted(get_args(EventType)))
# The resource permission each event type needs, besides events:read.
RESOURCE_SCOPE = {
    "operation.scope_reopened": "operations:read",
    "operation.evaluation_published": "operations:read",
    "integrity_report.registered": "reports:read",
}
SUPPORTED = frozenset((SCHEMA_VERSION, t, 1) for t in CATALOG)


class FeedError(Exception):
    """A request the feed refuses. ``code`` is the query error code."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


def normalize_types(raw: str | None) -> tuple[str, ...]:
    """The client filter: absent means the whole catalog; otherwise a comma-separated list
    of catalog types, normalized to a sorted set. Unknown or empty entries are refused."""
    if raw is None:
        return CATALOG
    items = raw.split(",")
    if len(raw) > 512 or not items or any(i not in CATALOG for i in items):
        raise FeedError("VALIDATION_ERROR", "types must be a comma-separated list of event types")
    return tuple(sorted(set(items)))


def readable_types(
    scopes: list[str], policies: list[str], requested: tuple[str, ...]
) -> tuple[str, ...]:
    """Types readable now: their scope (``RESOURCE_SCOPE``) and their disclosure policies.
    Computed per request from the access profile, never from a cursor."""
    return disclosure.readable_event_types(
        scopes, policies, tuple(t for t in requested if RESOURCE_SCOPE[t] in scopes)
    )


# ------------------------------------------------------------------------------ cursor


@dataclass(frozen=True)
class Cursor:
    generation: str
    position: int
    types: tuple[str, ...]
    readable: tuple[str, ...]


def _tag(key: bytes, tenant_id: str, body: bytes) -> str:
    message = b"invaria-event-cursor|" + tenant_id.encode("utf-8") + b"|" + body
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


def encode_cursor(c: Cursor, *, tenant_id: str, key: bytes) -> str:
    body = json.dumps(
        {
            "v": CURSOR_VERSION,
            "o": CURSOR_ORDER,
            "g": c.generation,
            "p": c.position,
            "f": list(c.types),
            "r": list(c.readable),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    raw = json.dumps({"b": body.decode("utf-8"), "m": _tag(key, tenant_id, body)}).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _invalid() -> FeedError:
    return FeedError("VALIDATION_ERROR", "the cursor does not belong to this request")


def decode_cursor(text: str) -> tuple[dict[str, Any], bytes, str]:
    """Structure only (size, alphabet, JSON, keys, types). The tag is checked by
    ``authenticate`` with the generation the body names."""
    if not 1 <= len(text) <= MAX_CURSOR_CHARS or not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise _invalid()
    try:
        outer = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        body_text, tag = outer["b"], outer["m"]
        body = json.loads(body_text)
    except (binascii.Error, ValueError, KeyError, TypeError) as error:
        raise _invalid() from error
    if (
        not isinstance(outer, dict)
        or set(outer) != {"b", "m"}
        or not isinstance(tag, str)
        or not isinstance(body, dict)
        or set(body) != {"v", "o", "g", "p", "f", "r"}
        or body["v"] != CURSOR_VERSION
        or body["o"] != CURSOR_ORDER
        or not isinstance(body["g"], str)
        or not isinstance(body["p"], int)
        or isinstance(body["p"], bool)
        or body["p"] < 0
        or not isinstance(body["f"], list)
        or not isinstance(body["r"], list)
    ):
        raise _invalid()
    return body, body_text.encode("utf-8"), tag


def authenticate(
    body: dict[str, Any], body_bytes: bytes, tag: str, *, tenant_id: str, key: bytes
) -> Cursor:
    if not hmac.compare_digest(tag, _tag(key, tenant_id, body_bytes)):
        raise _invalid()  # forged, edited, or issued for another tenant
    types, readable = tuple(body["f"]), tuple(body["r"])
    if any(t not in CATALOG for t in (*types, *readable)):
        raise _invalid()
    return Cursor(body["g"], body["p"], types, readable)


# ------------------------------------------------------------------------- integrity


def verify_event(payload: bytes, payload_sha256: str, event_type: str) -> AnyEvent:
    """The event to serve, or ``FeedError``: corruption (``INTEGRITY_ERROR``) is never an
    omission; a version this code cannot read is ``UNSUPPORTED_EVENT_VERSION``."""
    if hashlib.sha256(payload).hexdigest() != payload_sha256:
        raise FeedError("INTEGRITY_ERROR", "a stored event fails its integrity check")
    try:
        head = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise FeedError("INTEGRITY_ERROR", "a stored event fails its integrity check") from error
    if not isinstance(head, dict):
        raise FeedError("INTEGRITY_ERROR", "a stored event fails its integrity check")
    declared = (head.get("schema_version"), head.get("type"), head.get("type_version"))
    if declared not in SUPPORTED:
        raise FeedError(
            "UNSUPPORTED_EVENT_VERSION", "this version of Invaria cannot read a stored event"
        )
    try:
        event = parse_event(payload)
    except (StrictJsonError, ValidationError, ValueError) as error:
        raise FeedError("INTEGRITY_ERROR", "a stored event fails its integrity check") from error
    if event.type != event_type or canonical_bytes(event) != payload:
        raise FeedError("INTEGRITY_ERROR", "a stored event fails its integrity check")
    return event
