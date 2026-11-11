"""A scripted stand-in for the daed GraphQL API.

The integration is exercised through the real ``aiohttp`` client, so the only
thing that is faked is the transport: ``install_daed`` patches
``aiohttp.ClientSession.post`` and answers each GraphQL document from the
scripted responses below. That keeps the tests offline (Home Assistant blocks
sockets) while still covering query shape, auth headers, token caching and
re-authentication.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import aiohttp

TOKEN = "jwt-token"

# Responses that match each document the integration can send. A test can
# override or extend the script per document kind.
DEFAULT_RESPONSES: dict[str, Any] = {
    "token": {"data": {"token": TOKEN}},
    "general": {
        "data": {
            "general": {
                "dae": {"running": True, "modified": False, "version": "2026.09.24"},
                "runtimeOverview": {
                    "updatedAt": "2026-11-11T21:47:12.324085212+08:00",
                    # daed leaves the top-level rates at 0 and only fills
                    # `samples`, so the integration derives the rates below.
                    "uploadRate": 0,
                    "downloadRate": 0,
                    "samples": [
                        {
                            "timestamp": "2026-11-11T21:46:24.5+08:00",
                            "uploadRate": 1024.5,
                            "downloadRate": 2048.25,
                        },
                        {
                            "timestamp": "2026-11-11T21:46:36.75+08:00",
                            "uploadRate": 1024.5,
                            "downloadRate": 2048.25,
                        },
                        {
                            "timestamp": "2026-11-11T21:46:49+08:00",
                            "uploadRate": 1024.5,
                            "downloadRate": 2048.25,
                        },
                    ],
                    "uploadTotal": "2905213923",
                    "downloadTotal": "615568737",
                    "activeConnections": 19,
                    "udpSessions": 11,
                },
            }
        }
    },
    "nodes": {
        "data": {
            "nodes": {
                "totalCount": 2,
                "edges": [
                    {
                        "id": "Y3Vyc29yMQ",
                        "name": "",
                        "protocol": "hysteria2",
                        "tag": "s1",
                        "address": "s1.example.com:8443",
                        "subscriptionID": None,
                    },
                    {
                        "id": "Y3Vyc29yMg",
                        "name": "",
                        "protocol": "hysteria2",
                        "tag": "s2",
                        "address": "s2.example.com:8443",
                        "subscriptionID": None,
                    },
                ],
            }
        }
    },
    "groups": {
        "data": {
            "groups": [
                {
                    "id": "g1",
                    "name": "proxy",
                    "policy": "random",
                    "nodes": [{"id": "Y3Vyc29yMQ", "tag": "s1"}],
                    "subscriptions": [],
                },
                {
                    "id": "g2",
                    "name": "us",
                    "policy": "min_moving_avg",
                    "nodes": [{"id": "Y3Vyc29yMg", "tag": "s2"}],
                    "subscriptions": [],
                },
            ]
        }
    },
    "subscriptions": {"data": {"subscriptions": []}},
    "nodeLatencies": {
        "data": {
            "nodeLatencies": [
                {
                    "id": "Y3Vyc29yMQ",
                    "latencyMs": 193,
                    "alive": True,
                    "testedAt": "2026-11-11T21:47:36.331495047+08:00",
                    "message": None,
                },
                {
                    "id": "Y3Vyc29yMg",
                    "latencyMs": 240,
                    "alive": True,
                    "testedAt": "2026-11-11T21:47:36.331607089+08:00",
                    "message": None,
                },
            ]
        }
    },
    "configs": {
        "data": {"configs": [{"id": "Y3Vyc29yMQ", "name": "global", "selected": True}]}
    },
    "run": {"data": {"run": 0}},
    "selectConfig": {"data": {"selectConfig": 1}},
    "updateSubscription": {
        "data": {"updateSubscription": {"id": "s1", "status": "ok"}}
    },
    "testNodeLatencies": {"data": {"testNodeLatencies": []}},
}

# Document marker -> response kind. Order matters: `token` is checked first.
_MARKERS: tuple[tuple[str, str], ...] = (
    ("token(", "token"),
    ("runtimeOverview", "general"),
    ("totalCount", "nodes"),
    ("nodeLatencies", "nodeLatencies"),
    ("testNodeLatencies", "testNodeLatencies"),
    ("updateSubscription", "updateSubscription"),
    ("selectConfig", "selectConfig"),
    ("groups", "groups"),
    ("subscriptions", "subscriptions"),
    ("configs", "configs"),
    ("run(", "run"),
)


def kind_of(query: str) -> str:
    """Return the response kind a GraphQL document asks for."""
    for marker, kind in _MARKERS:
        if marker in query:
            return kind
    raise AssertionError(f"Unexpected daed document: {query}")


@dataclass
class RecordedRequest:
    """One request the integration sent to daed."""

    kind: str
    query: str
    variables: Mapping[str, Any] | None
    headers: Mapping[str, str]


@dataclass
class DaedMock:
    """Scripted daed API plus a record of every request."""

    base_url: str = "http://daed.test:2023"
    responses: dict[str, Any] = field(
        default_factory=lambda: {k: v for k, v in DEFAULT_RESPONSES.items()}
    )
    requests: list[RecordedRequest] = field(default_factory=list)
    tokens_issued: int = 0
    """How many successful sign-ins happened."""
    auth_failures: int = 0
    """How many authenticated requests to reject with "access denied"."""
    login_error: str | None = None
    """When set, the token query fails with this message."""

    def requests_of(self, kind: str) -> list[RecordedRequest]:
        """Return the recorded requests of one kind."""
        return [request for request in self.requests if request.kind == kind]

    def _response_for(self, query: str) -> Any:
        kind = kind_of(query)
        if kind == "token":
            if self.login_error is not None:
                return {"errors": [{"message": self.login_error}], "data": None}
            self.tokens_issued += 1
            return self.responses["token"]
        return self.responses[kind]

    connection_error: bool = False
    """When set, every request fails the way an unreachable host does."""

    def post(self, url, *, json=None, headers=None, timeout=None, **kwargs):
        """Stand-in for the request context manager of aiohttp.

        The integration uses ``async with session.post(...)``, so this returns a
        response object that is itself an async context manager.
        """
        if self.connection_error:
            return _FailingResponse()
        query = json["query"]
        kind = kind_of(query)
        variables = json.get("variables")
        self.requests.append(
            RecordedRequest(kind, query, variables, dict(headers or {}))
        )

        if kind != "token":
            expected = f"Bearer {TOKEN}"
            if self.auth_failures > 0 or headers.get("Authorization") != expected:
                if self.auth_failures > 0:
                    self.auth_failures -= 1
                return FakeResponse(
                    {"errors": [{"message": "access denied"}], "data": None}
                )

        return FakeResponse(self._response_for(query))


class _FailingResponse:
    """A response whose ``__aenter__`` raises, like an unreachable host."""

    async def __aenter__(self):
        raise aiohttp.ClientConnectionError("Connection refused")

    async def __aexit__(self, *args: Any) -> None:
        """Exit the response context."""


class FakeResponse:
    """The subset of ``aiohttp.ClientResponse`` the client reads."""

    def __init__(self, payload: Any, status: int = 200) -> None:
        """Initialize the response."""
        self._payload = payload
        self.status = status

    async def text(self) -> str:
        """Return the body as text."""
        import json

        return json.dumps(self._payload)

    async def __aenter__(self) -> FakeResponse:
        """Enter the response context."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Exit the response context."""


@contextmanager
def install_daed(mock: DaedMock | None = None) -> Iterator[DaedMock]:
    """Patch ``aiohttp.ClientSession.post`` to answer from ``mock``."""
    mock = mock or DaedMock()

    def _post(session_self, url, **kwargs):
        return mock.post(url, **kwargs)

    with patch("aiohttp.ClientSession.post", new=_post):
        yield mock


def entry_data(mock: DaedMock, **overrides: Any) -> dict[str, Any]:
    """Return config entry data pointing at ``mock``."""
    data = {
        "base_url": mock.base_url,
        "username": "admin",
        "password": "secret",
        "verify_ssl": True,
    }
    data.update(overrides)
    return data


def config_entry(mock: DaedMock, **kwargs: Any) -> Any:
    """Return a mock config entry that mirrors a real one (URL based title)."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    kwargs.setdefault("title", mock.base_url.split("://", 1)[-1])
    # The flow derives the unique id from the normalized base URL.
    kwargs.setdefault("unique_id", mock.base_url)
    return MockConfigEntry(domain="daed", data=entry_data(mock), **kwargs)


__all__ = [
    "DaedMock",
    "config_entry",
    "DEFAULT_RESPONSES",
    "TOKEN",
    "entry_data",
    "install_daed",
    "kind_of",
]
