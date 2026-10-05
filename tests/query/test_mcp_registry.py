"""MCP surface without a database: exactly seven read-only tools, bounded inputs, errors."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from invaria.contracts.base import parse_contract
from invaria.mcp_server import INSTRUCTIONS, TOOL_NAMES, build_server
from invaria.query.models import AccessProfile
from invaria.query.service import QueryError

WRITE_WORDS = ("write", "send", "sign", "approve", "pay", "submit", "delete", "update", "set_",
               "create", "execute", "sql", "shell", "fetch", "reconcile")  # fmt: skip
ALLOWED_ARGS = {
    "operation_ref",
    "valid_at",
    "known_at",
    "evaluation_id",
    "control_id",
    "old_id",
    "new_id",
    "evidence_id",
}


class Refusing:
    """A service stub: every call is forbidden (registry tests never reach a database)."""

    def __getattr__(self, name: str) -> Any:
        def refuse(*_: Any, **__: Any) -> Any:
            raise QueryError("FORBIDDEN", f"{name} requires a scope this profile lacks")

        return refuse


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


async def _tools() -> Any:
    async with create_connected_server_and_client_session(build_server(Refusing())) as client:  # type: ignore[arg-type]
        return (await client.list_tools()).tools


def test_exactly_the_approved_read_only_tools() -> None:
    tools = run(_tools())
    assert sorted(t.name for t in tools) == sorted(TOOL_NAMES)
    assert len(tools) == 7
    for tool in tools:
        assert not any(word in tool.name for word in WRITE_WORDS), tool.name
        annotations = tool.annotations
        assert annotations is not None
        assert annotations.readOnlyHint is True
        assert annotations.destructiveHint is False
        assert annotations.openWorldHint is False
        assert tool.outputSchema is not None, f"{tool.name} has no structured output schema"


def test_inputs_are_bounded_identifiers_and_times_only() -> None:
    for tool in run(_tools()):
        properties = tool.inputSchema.get("properties", {})
        assert set(properties) <= ALLOWED_ARGS, (tool.name, set(properties))
        for name, spec in properties.items():
            assert spec.get("type") == "string", (tool.name, name)
        assert set(tool.inputSchema.get("required", [])) == set(properties)


def test_errors_reach_the_client_as_tool_errors() -> None:
    async def call() -> Any:
        async with create_connected_server_and_client_session(build_server(Refusing())) as client:  # type: ignore[arg-type]
            return await client.call_tool("trace_operation", {"operation_ref": "SUB-0001"})

    result = run(call())
    assert result.isError is True
    assert "FORBIDDEN" in json.dumps([c.model_dump() for c in result.content])


def test_instructions_mark_source_content_as_untrusted() -> None:
    assert "read-only" in INSTRUCTIONS and "untrusted" in INSTRUCTIONS
    assert "UNKNOWN is not a failure" in INSTRUCTIONS


def test_access_profile_is_strict() -> None:
    good = {
        "schema_version": "1.0",
        "principal_id": "analyst-1",
        "tenant_id": "tenant-synthetic-demo",
        "scopes": ["operations:read"],
        "max_items": 50,
    }
    assert parse_contract(AccessProfile, json.dumps(good)).scopes == ["operations:read"]
    for bad in (
        {**good, "scopes": ["operations:write"]},
        {**good, "scopes": []},
        {**good, "max_items": 0},
        {**good, "extra": True},
    ):
        with pytest.raises(ValueError):
            parse_contract(AccessProfile, json.dumps(bad))
