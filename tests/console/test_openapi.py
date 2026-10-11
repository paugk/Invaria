"""The OpenAPI description of ``/v1`` matches the committed file and the routes served."""

from __future__ import annotations

import inspect
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

from pydantic import BaseModel

from invaria.console import openapi
from invaria.console.app import STATUS, ConsoleApp

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t" * 40
HOST = "127.0.0.1:8765"


class _Empty(BaseModel):
    pass


class Recorder:
    """Stands in for QueryService: records which use case a route called."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        # The console reads the access profile's scopes once, for its navigation.
        self.access = SimpleNamespace(scopes=["operations:read"])

    def allows(self, use_case: str) -> bool:  # navigation and links; not a route
        return True

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any) -> BaseModel:
            self.calls.append(name)
            return _Empty()

        return call


def test_the_committed_openapi_has_no_drift() -> None:
    committed = (ROOT / openapi.DEFAULT_PATH).read_text("utf-8")
    assert committed == openapi.render(), "run: uv run python -m invaria.console.openapi"


def test_every_documented_operation_is_the_route_served_and_only_get_exists() -> None:
    document = openapi.document()
    assert all(set(item) == {"get"} for item in document["paths"].values())
    for route in openapi.ROUTES:
        recorder = Recorder()
        app = ConsoleApp(recorder, token=TOKEN, allowed_hosts={HOST})  # type: ignore[arg-type]
        path = re.sub(r"\{[^}]+\}", "x1", route.path)
        query = "&".join(f"{p.name}=v" for p in route.query if p.required)
        captured: list[str] = []
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "HTTP_HOST": HOST,
            "HTTP_AUTHORIZATION": f"Bearer {TOKEN}",
            "wsgi.errors": io.StringIO(),
        }

        def start(status: str, _headers: list[tuple[str, str]], sink: list[str] = captured) -> None:
            sink.append(status)

        app(environ, start)
        assert captured == ["200 OK"] and recorder.calls == [route.operation_id], route.path
    source = inspect.getsource(ConsoleApp._api)
    assert source.count("case [") == len(openapi.ROUTES)


def test_documented_error_statuses_are_the_application_mapping() -> None:
    body = json.dumps(openapi.document()["paths"])
    for code, status in STATUS.items():
        if code in get_args(openapi.ErrorCode):
            assert f'"{status}"' in body, code
    assert set(get_args(openapi.ErrorCode)) == set(STATUS)
