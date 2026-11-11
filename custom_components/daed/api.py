"""Async client for the daed GraphQL API.

daed (the web dashboard for the eBPF transparent proxy ``dae``) speaks
GraphQL over HTTP at ``POST <base_url>/graphql``. Everything except
``healthCheck``, ``numberUsers`` and ``token`` is behind a bearer token:

* ``token(username, password)`` exchanges the dashboard credentials for a JWT.
* Every subsequent request carries ``Authorization: Bearer <jwt>``.

The client caches that token and re-authenticates once when daed rejects it,
which is what happens when the token expires or the daed process restarts.

The runtime traffic counters are *live*: daed recomputes ``runtimeOverview``
on every query, and ``uploadTotal``/``downloadTotal`` are cumulative byte
counters that only grow while the daed process runs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp

_LOGGER = logging.getLogger(__name__)

GRAPHQL_PATH = "/graphql"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# `token` is unauthenticated and cheap, but a separate timeout keeps a broken
# daed from blocking setup for the full request timeout.
LOGIN_TIMEOUT = aiohttp.ClientTimeout(total=15)
# Node latency sweeps and config application touch every node.
SLOW_TIMEOUT = aiohttp.ClientTimeout(total=60)

# How much of an unexpected response body to quote in an error message.
_BODY_SNIPPET_LIMIT = 200

# GraphQL reports auth failures as plain error messages, there is no error code
# to switch on.
_AUTH_ERROR_MESSAGES = (
    "access denied",
    "unauthorized",
    "incorrect username or password",
    "token is expired",
    "invalid token",
    "missing token",
)


class DaedError(Exception):
    """Base error for the daed integration."""


class DaedAuthError(DaedError):
    """daed rejected the credentials or the token."""


class DaedConnectionError(DaedError):
    """daed could not be reached."""


class DaedApiError(DaedError):
    """daed answered with a GraphQL or HTTP error."""


def normalize_base_url(base_url: str) -> str:
    """Normalize a user supplied base URL to ``scheme://host[:port][/path]``."""
    candidate = (base_url or "").strip()
    if not candidate:
        raise DaedConnectionError("A daed base URL is required")
    if "://" not in candidate:
        candidate = f"http://{candidate}"

    parsed = urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        raise DaedConnectionError(f"Invalid daed base URL: {base_url}")

    path = parsed.path.rstrip("/")
    if path.endswith(GRAPHQL_PATH):
        path = path[: -len(GRAPHQL_PATH)]

    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def is_auth_error(message: str) -> bool:
    """Return True when a daed error message means "not authenticated"."""
    lowered = (message or "").lower()
    return any(needle in lowered for needle in _AUTH_ERROR_MESSAGES)


# --- Queries -----------------------------------------------------------------

TOKEN_QUERY = """
query DaedToken($username: String!, $password: String!) {
  token(username: $username, password: $password)
}
"""

GENERAL_QUERY = """
query DaedGeneral($windowSec: Int!, $maxPoints: Int!) {
  general {
    dae { running modified version }
    runtimeOverview(windowSec: $windowSec, maxPoints: $maxPoints) {
      updatedAt
      uploadRate
      downloadRate
      uploadTotal
      downloadTotal
      activeConnections
      udpSessions
      samples { timestamp uploadRate downloadRate }
    }
  }
}
"""

NODES_QUERY = """
query DaedNodes {
  nodes {
    totalCount
    edges { id name protocol tag address subscriptionID }
  }
}
"""

SUBSCRIPTIONS_QUERY = """
query DaedSubscriptions {
  subscriptions { id tag status info updatedAt cronExp cronEnable }
}
"""

GROUPS_QUERY = """
query DaedGroups {
  groups {
    id
    name
    policy
    nodes { id tag }
    subscriptions { subscription { id tag } matchedCount }
  }
}
"""

LATENCIES_QUERY = """
query DaedNodeLatencies {
  nodeLatencies { id latencyMs alive testedAt message }
}
"""

CONFIGS_QUERY = """
query DaedConfigs {
  configs { id name selected }
}
"""

# --- Mutations ---------------------------------------------------------------

# `run` returns the exit code of the dae process.
RUN_MUTATION = """
mutation DaedRun($dry: Boolean!) {
  run(dry: $dry)
}
"""

UPDATE_SUBSCRIPTION_MUTATION = """
mutation DaedUpdateSubscription($id: ID!) {
  updateSubscription(id: $id) { id status }
}
"""

# `testNodeLatencies` returns the page type `NodeLatency`, so the selection set
# is mandatory; daed dials every node before answering.
TEST_LATENCIES_MUTATION = """
mutation DaedTestNodeLatencies {
  testNodeLatencies { id latencyMs alive testedAt message }
}
"""

# `selectConfig` returns `1` on success.
SELECT_CONFIG_MUTATION = """
mutation DaedSelectConfig($id: ID!) {
  selectConfig(id: $id)
}
"""


class DaedClient:
    """Minimal daed client for reading status and driving basic actions."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        username: str,
        password: str,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._base_url = normalize_base_url(base_url)
        self._username = username
        self._password = password
        self._token: str | None = None

    @property
    def base_url(self) -> str:
        """Return the normalized base URL."""
        return self._base_url

    @property
    def graphql_url(self) -> str:
        """Return the GraphQL endpoint URL."""
        return f"{self._base_url}{GRAPHQL_PATH}"

    @property
    def has_token(self) -> bool:
        """Return whether a token is currently cached."""
        return self._token is not None

    async def async_login(self) -> str:
        """Exchange the credentials for a bearer token."""
        payload = await self._async_execute(
            TOKEN_QUERY,
            {"username": self._username, "password": self._password},
            authenticated=False,
            timeout=LOGIN_TIMEOUT,
        )
        token = (payload.get("data") or {}).get("token")
        if not isinstance(token, str) or not token:
            raise DaedAuthError("daed did not return a token")
        self._token = token
        return token

    # --- Reading ------------------------------------------------------------

    async def async_query(
        self, query: str, variables: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run an authenticated query and return its ``data`` object."""
        payload = await self._async_authenticated(query, variables)
        return payload.get("data") or {}

    async def async_get_general(
        self, window_sec: int = 60, max_points: int = 2
    ) -> dict[str, Any]:
        """Return ``general`` (dae state plus the live runtime counters)."""
        data = await self.async_query(
            GENERAL_QUERY, {"windowSec": window_sec, "maxPoints": max_points}
        )
        general = data.get("general")
        if not isinstance(general, dict):
            raise DaedApiError("daed returned no 'general' object")
        return general

    async def async_get_nodes(self) -> list[dict[str, Any]]:
        """Return every node daed knows about."""
        data = await self.async_query(NODES_QUERY)
        nodes = data.get("nodes")
        if not isinstance(nodes, dict):
            return []
        return [_node(edge) for edge in nodes.get("edges") or [] if edge]

    async def async_get_subscriptions(self) -> list[dict[str, Any]]:
        """Return every subscription."""
        data = await self.async_query(SUBSCRIPTIONS_QUERY)
        return [sub for sub in data.get("subscriptions") or [] if sub]

    async def async_get_groups(self) -> list[dict[str, Any]]:
        """Return every group."""
        data = await self.async_query(GROUPS_QUERY)
        return [group for group in data.get("groups") or [] if group]

    async def async_get_configs(self) -> list[dict[str, Any]]:
        """Return every saved config."""
        data = await self.async_query(CONFIGS_QUERY)
        return [config for config in data.get("configs") or [] if config]

    async def async_get_node_latencies(self) -> list[dict[str, Any]]:
        """Return daed's cached latency results."""
        data = await self.async_query(LATENCIES_QUERY)
        return [item for item in data.get("nodeLatencies") or [] if item]

    # --- Acting -------------------------------------------------------------

    async def async_mutate(
        self,
        mutation: str,
        variables: Mapping[str, Any] | None = None,
        *,
        slow: bool = False,
    ) -> dict[str, Any]:
        """Run an authenticated mutation and return its ``data`` object."""
        timeout = SLOW_TIMEOUT if slow else REQUEST_TIMEOUT
        payload = await self._async_authenticated(mutation, variables, timeout=timeout)
        return payload.get("data") or {}

    async def async_run(self, dry: bool = False) -> int:
        """Apply the selected config (``dry=True`` only reports).

        ``run`` resolves to dae's exit code. Unlike the plain ``dae -c`` CLI,
        daed merges dae's configuration and error output into the stdout it
        parses, so a healthy run reports ``1`` here; a dae compile error
        surfaces as a GraphQL error instead. The raw code is returned so
        callers can log it without pretending ``0`` is the only success case.
        """
        data = await self.async_mutate(RUN_MUTATION, {"dry": dry}, slow=True)
        code = data.get("run")
        if code not in (0, 1):
            raise DaedApiError(
                f"dae reported exit code {code!r} while applying the config"
            )
        return int(code)

    async def async_update_subscriptions(self, ids: Iterable[str]) -> int:
        """Refresh the given subscriptions and return how many succeeded."""
        updated = 0
        for sub_id in ids:
            try:
                await self.async_mutate(
                    UPDATE_SUBSCRIPTION_MUTATION, {"id": sub_id}, slow=True
                )
            except DaedError as err:
                _LOGGER.warning("Updating daed subscription %s failed: %s", sub_id, err)
                continue
            updated += 1
        return updated

    async def async_test_node_latencies(self) -> list[dict[str, Any]]:
        """Ask daed to dial every node and return the fresh results."""
        data = await self.async_mutate(TEST_LATENCIES_MUTATION, slow=True)
        return [item for item in data.get("testNodeLatencies") or [] if item]

    async def async_select_config(self, config_id: str) -> bool:
        """Select a saved config."""
        data = await self.async_mutate(SELECT_CONFIG_MUTATION, {"id": config_id})
        return data.get("selectConfig") == 1

    # --- Transport ----------------------------------------------------------

    async def _async_authenticated(
        self,
        document: str,
        variables: Mapping[str, Any] | None,
        *,
        timeout: aiohttp.ClientTimeout = REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        """Execute a document, re-authenticating once on a rejected token."""
        if self._token is None:
            await self.async_login()
        try:
            return await self._async_execute(
                document, variables, authenticated=True, timeout=timeout
            )
        except DaedAuthError:
            _LOGGER.debug("daed rejected the cached token, signing in again")
            self._token = None
            await self.async_login()
            return await self._async_execute(
                document, variables, authenticated=True, timeout=timeout
            )

    async def _async_execute(
        self,
        document: str,
        variables: Mapping[str, Any] | None,
        *,
        authenticated: bool,
        timeout: aiohttp.ClientTimeout,
    ) -> dict[str, Any]:
        """POST one GraphQL document and validate the response envelope."""
        headers = {"Content-Type": "application/json"}
        if authenticated:
            if self._token is None:
                raise DaedAuthError("daed token is not available")
            headers["Authorization"] = f"Bearer {self._token}"

        body: dict[str, Any] = {"query": document}
        if variables is not None:
            body["variables"] = dict(variables)

        try:
            async with self._session.post(
                self.graphql_url, json=body, headers=headers, timeout=timeout
            ) as response:
                text = await response.text()
                status = response.status
        except TimeoutError as err:
            raise DaedConnectionError(
                f"Timed out talking to daed at {self._base_url}"
            ) from err
        except aiohttp.ClientError as err:
            raise DaedConnectionError(
                f"Cannot reach daed at {self._base_url}: {err}"
            ) from err

        try:
            payload = json.loads(text) if text else None
        except ValueError as err:
            raise DaedApiError(
                f"daed returned a non-JSON response (HTTP {status}): {_compact(text)}"
            ) from err

        if not isinstance(payload, dict):
            snippet = _compact(text)
            raise DaedApiError(
                f"daed returned an unexpected response (HTTP {status}): {snippet}"
            )

        errors = payload.get("errors")
        if errors:
            message = _format_errors(errors)
            if not authenticated or is_auth_error(message):
                raise DaedAuthError(message)
            raise DaedApiError(message)

        if status >= 400:
            raise DaedApiError(f"daed answered HTTP {status}: {_compact(text)}")

        return payload


def _node(edge: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one ``nodes.edges`` entry.

    daed returns the node fields flat on the edge (the page type is called
    ``Node`` upstream), but accept a nested ``node`` object so a future change
    to the usual cursor edge shape keeps working.
    """
    nested = edge.get("node")
    node = dict(nested) if isinstance(nested, Mapping) else dict(edge)
    if "id" not in node and "cursor" in edge:
        node["id"] = edge["cursor"]
    return node


def _format_errors(errors: Any) -> str:
    """Flatten GraphQL errors into one line."""
    if isinstance(errors, str):
        return errors
    if isinstance(errors, Mapping):
        return str(errors.get("message") or errors)
    if isinstance(errors, list):
        parts = []
        for error in errors:
            if isinstance(error, Mapping):
                parts.append(str(error.get("message") or error))
            else:
                parts.append(str(error))
        return "; ".join(part for part in parts if part)
    return str(errors)


def _compact(text: str, limit: int = _BODY_SNIPPET_LIMIT) -> str:
    """Collapse whitespace and truncate a response body for logging."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}... ({len(collapsed)} chars)"
