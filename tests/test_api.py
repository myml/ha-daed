"""Tests for the daed GraphQL client.

The transport is scripted in ``tests.mock_daed``; the real ``aiohttp`` client
code is exercised, so the request shape, the bearer header, token caching,
transparent re-authentication and the error mapping are all covered.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from aiohttp import ClientSession

from custom_components.daed.api import (
    DaedApiError,
    DaedAuthError,
    DaedClient,
    DaedConnectionError,
    is_auth_error,
    normalize_base_url,
)

from .mock_daed import TOKEN, DaedMock, install_daed

BASE = "http://daed.test:2023"


@pytest.fixture
def mock() -> Iterator[DaedMock]:
    """Install the scripted daed transport."""
    with install_daed() as scripted:
        yield scripted


@pytest.fixture
async def client() -> AsyncIterator[DaedClient]:
    """Return a client bound to the scripted daed."""
    async with ClientSession() as session:
        yield DaedClient(session, BASE, "admin", "secret")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://10.0.0.1:2023", "http://10.0.0.1:2023"),
        ("http://10.0.0.1:2023/", "http://10.0.0.1:2023"),
        ("10.0.0.1:2023", "http://10.0.0.1:2023"),
        ("http://10.0.0.1:2023/graphql", "http://10.0.0.1:2023"),
        ("https://dae.example.com/daed/", "https://dae.example.com/daed"),
    ],
)
def test_normalize_base_url(raw: str, expected: str) -> None:
    """Base URLs are normalized to scheme, host, port and path."""
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("access denied", True),
        ("incorrect username or password", True),
        ("failed to parse cursor", False),
        ('Field "run" argument "dry" is required', False),
    ],
)
def test_is_auth_error(message: str, expected: bool) -> None:
    """Auth failures are recognized by their message."""
    assert is_auth_error(message) is expected


async def test_login_sends_credentials_and_caches_token(
    client: DaedClient, mock: DaedMock
) -> None:
    """The token is fetched once and reused with a bearer header."""
    await client.async_login()
    await client.async_get_general()
    await client.async_get_general()

    token_requests = mock.requests_of("token")
    assert len(token_requests) == 1
    assert token_requests[0].variables == {"username": "admin", "password": "secret"}
    general = mock.requests_of("general")
    assert len(general) == 2
    assert all(
        request.headers["Authorization"] == f"Bearer {TOKEN}" for request in general
    )
    # `async_get_general` defaults; the coordinator passes its own values.
    assert general[0].variables == {"windowSec": 60, "maxPoints": 2}


async def test_query_logs_in_implicitly(client: DaedClient, mock: DaedMock) -> None:
    """The first query performs the sign-in itself."""
    general = await client.async_get_general()

    assert general["dae"]["running"] is True
    assert len(mock.requests_of("token")) == 1


async def test_reauth_on_rejected_token(client: DaedClient, mock: DaedMock) -> None:
    """A rejected token is replaced and the query retried once."""
    await client.async_login()
    mock.auth_failures = 1
    general = await client.async_get_general()

    assert general["dae"]["version"] == "2026.09.24"
    assert mock.tokens_issued == 2


async def test_invalid_credentials_raise_auth_error(
    client: DaedClient, mock: DaedMock
) -> None:
    """A wrong password surfaces as an auth error, not a generic API error."""
    mock.login_error = "incorrect username or password"
    with pytest.raises(DaedAuthError, match="incorrect username or password"):
        await client.async_login()


async def test_persistent_auth_failure_raises(
    client: DaedClient, mock: DaedMock
) -> None:
    """When the retried request is still rejected, the error propagates."""
    await client.async_login()
    mock.auth_failures = 5
    with pytest.raises(DaedAuthError, match="access denied"):
        await client.async_get_general()


async def test_graphql_error_is_api_error(client: DaedClient, mock: DaedMock) -> None:
    """A non-auth GraphQL error is reported with its message."""
    mock.responses["groups"] = {
        "errors": [{"message": "failed to parse cursor"}],
        "data": None,
    }
    with pytest.raises(DaedApiError, match="failed to parse cursor"):
        await client.async_get_groups()


async def test_unreachable_host(client: DaedClient, mock: DaedMock) -> None:
    """A connection error is reported as such."""
    mock.connection_error = True
    with pytest.raises(DaedConnectionError, match="Cannot reach daed"):
        await client.async_login()


async def test_general_query_requests_samples(
    client: DaedClient, mock: DaedMock
) -> None:
    """daed only fills `samples` when it is asked for; rates depend on it."""
    general = await client.async_get_general()
    request = mock.requests_of("general")[0]
    assert "samples {" in request.query
    assert "uploadRate" in request.query and "downloadRate" in request.query
    assert general["runtimeOverview"]["samples"]


async def test_nodes_are_flattened_from_edges(
    client: DaedClient, mock: DaedMock
) -> None:
    """daed returns node fields flat on the edge."""
    nodes = await client.async_get_nodes()

    assert [node["tag"] for node in nodes] == ["s1", "s2"]
    assert nodes[0]["protocol"] == "hysteria2"


async def test_nested_edges_are_supported(client: DaedClient, mock: DaedMock) -> None:
    """A future cursor-style edge keeps working."""
    mock.responses["nodes"] = {
        "data": {
            "nodes": {
                "totalCount": 1,
                "edges": [{"cursor": "abc", "node": {"id": "n1", "tag": "s9"}}],
            }
        }
    }
    nodes = await client.async_get_nodes()

    assert nodes == [{"id": "n1", "tag": "s9"}]


async def test_run_returns_dae_exit_code(client: DaedClient, mock: DaedMock) -> None:
    """`run` reports the raw dae exit code (daed merges output, so 1 is healthy)."""
    assert await client.async_run() == 0
    mock.responses["run"] = {"data": {"run": 1}}
    assert await client.async_run() == 1


async def test_run_unexpected_exit_code(client: DaedClient, mock: DaedMock) -> None:
    """An exit code dae cannot produce is surfaced as an error."""
    mock.responses["run"] = {"data": {"run": 7}}
    with pytest.raises(DaedApiError, match="exit code 7"):
        await client.async_run()


async def test_select_config(client: DaedClient, mock: DaedMock) -> None:
    """`selectConfig` returns 1 on success."""
    assert await client.async_select_config("g1") is True
    assert mock.requests_of("selectConfig")[0].variables == {"id": "g1"}


async def test_update_subscriptions_counts_successes(
    client: DaedClient, mock: DaedMock
) -> None:
    """Every subscription is refreshed."""
    updated = await client.async_update_subscriptions(["a", "b"])

    assert updated == 2
    assert len(mock.requests_of("updateSubscription")) == 2


async def test_test_node_latencies(client: DaedClient, mock: DaedMock) -> None:
    """The latency sweep reads the returned results."""
    mock.responses["testNodeLatencies"] = {
        "data": {"testNodeLatencies": [{"id": "n1", "latencyMs": 12, "alive": True}]}
    }
    results = await client.async_test_node_latencies()

    assert results[0]["latencyMs"] == 12
