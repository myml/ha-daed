"""Data update coordinators for the daed integration.

Two coordinators share one config entry:

* ``DaedRuntimeCoordinator`` polls the live dae state, traffic counters and
  topology on a short interval (30 s by default). A failure makes the entities
  unavailable.
* ``DaedLatencyCoordinator`` reads daed's cached node latency results on a much
  slower interval (10 min by default, can be disabled). A failure there never
  takes the runtime entities down with it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    ConfigEntryAuthFailed,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .api import DaedAuthError, DaedClient, DaedError
from .const import DOMAIN, HEALTHY_STATUSES
from .traffic import resolve_rate

_LOGGER = logging.getLogger(__name__)

# How much history of the live counter daed should sample for us: a minute of
# points, which is also the window `traffic.rate_from_samples` averages over.
RUNTIME_WINDOW_SECONDS = 60
RUNTIME_MAX_POINTS = 5


@dataclass(frozen=True, slots=True)
class GroupInfo:
    """One daed group (a load balanced set of nodes)."""

    id: str
    name: str
    policy: str
    nodes: int


@dataclass(frozen=True, slots=True)
class SubscriptionInfo:
    """One daed subscription."""

    id: str
    tag: str
    status: str
    info: str | None
    updated_at: str | None
    cron_exp: str | None
    cron_enable: bool

    @property
    def display_name(self) -> str:
        """Return a non-empty name for entity naming."""
        return self.tag or self.info or self.id

    @property
    def healthy(self) -> bool:
        """Return whether daed reports the subscription as healthy."""
        return (self.status or "").strip().lower() in HEALTHY_STATUSES


@dataclass(frozen=True, slots=True)
class NodeLatency:
    """daed's cached latency measurement for one node."""

    node_id: str
    name: str
    latency_ms: int | None
    alive: bool
    tested_at: str | None
    message: str | None


@dataclass(frozen=True, slots=True)
class DaedRuntime:
    """Everything one poll of daed can observe."""

    running: bool
    modified: bool
    version: str | None
    configured_config: str | None
    upload_rate: float
    download_rate: float
    upload_total: int | None
    download_total: int | None
    active_connections: int
    udp_sessions: int
    groups: tuple[GroupInfo, ...]
    subscriptions: tuple[SubscriptionInfo, ...]
    updated_at: datetime | None

    @property
    def subscription_errors(self) -> int:
        """Subscriptions daed reports as unhealthy."""
        return sum(1 for sub in self.subscriptions if not sub.healthy)


class DaedRuntimeCoordinator(DataUpdateCoordinator[DaedRuntime]):
    """Poll daed for the live dae state, traffic and topology."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: DaedClient,
        scan_interval: timedelta,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_runtime",
            config_entry=entry,
            update_interval=scan_interval,
            always_update=True,
        )
        self.entry = entry
        self.client = client
        self._node_count = 0
        self._node_names: dict[str, str] = {}
        self._last_general: dict[str, Any] = {}

    @property
    def version(self) -> str | None:
        """Return dae's version, available even right after the first refresh."""
        dae = self._last_general.get("dae") or {}
        version = dae.get("version")
        return version if isinstance(version, str) else None

    @property
    def node_count(self) -> int:
        """Return the number of nodes daed knows about."""
        return self._node_count

    @property
    def node_names(self) -> dict[str, str]:
        """Return node id -> display name."""
        return dict(self._node_names)

    async def _async_update_data(self) -> DaedRuntime:
        """Fetch dae state, runtime counters and topology."""
        try:
            general = await self.client.async_get_general(
                RUNTIME_WINDOW_SECONDS, RUNTIME_MAX_POINTS
            )
            self._last_general = general
            groups = await self.client.async_get_groups()
            subscriptions = await self.client.async_get_subscriptions()
            configs = await self.client.async_get_configs()
            node_list = await self.client.async_get_nodes()
        except DaedAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except DaedError as err:
            raise UpdateFailed(str(err)) from err

        self._node_count = len(node_list)
        self._node_names = {
            str(node["id"]): str(node.get("tag") or node.get("name") or node["id"])
            for node in node_list
            if node.get("id")
        }

        dae = general.get("dae") or {}
        overview = general.get("runtimeOverview") or {}
        samples = overview.get("samples") or []

        return DaedRuntime(
            running=bool(dae.get("running")),
            modified=bool(dae.get("modified")),
            version=dae.get("version"),
            configured_config=_configured_config(configs),
            upload_rate=resolve_rate(
                overview.get("uploadRate"), samples, direction="upload"
            ),
            download_rate=resolve_rate(
                overview.get("downloadRate"), samples, direction="download"
            ),
            upload_total=_total(overview.get("uploadTotal")),
            download_total=_total(overview.get("downloadTotal")),
            active_connections=_count(overview.get("activeConnections")),
            udp_sessions=_count(overview.get("udpSessions")),
            groups=tuple(_group(group) for group in groups if group.get("name")),
            subscriptions=tuple(
                _subscription(sub) for sub in subscriptions if sub.get("id")
            ),
            updated_at=dt_util.parse_datetime(overview.get("updatedAt") or "") or None,
        )

    async def async_trigger_run(self) -> int:
        """Apply the selected config in dae, then refresh."""
        code = await self.client.async_run()
        await self.async_request_refresh()
        return code

    async def async_trigger_select_config(self, config_id: str) -> None:
        """Select a saved config, then refresh."""
        await self.client.async_select_config(config_id)
        await self.async_request_refresh()


class DaedLatencyCoordinator(DataUpdateCoordinator[dict[str, NodeLatency]]):
    """Read daed's cached node latency results."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: DaedClient,
        scan_interval: timedelta | None,
        node_names: Any,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_latency",
            config_entry=entry,
            update_interval=scan_interval,
            always_update=True,
        )
        self.entry = entry
        self.client = client
        self._node_names = node_names

    async def _async_update_data(self) -> dict[str, NodeLatency]:
        """Read daed's cached latency results."""
        try:
            results = await self.client.async_get_node_latencies()
        except DaedAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except DaedError as err:
            raise UpdateFailed(str(err)) from err

        names = self._node_names() if callable(self._node_names) else {}
        latencies: dict[str, NodeLatency] = {}
        for item in results:
            node_id = item.get("id")
            if not node_id:
                continue
            latencies[str(node_id)] = NodeLatency(
                node_id=str(node_id),
                name=names.get(str(node_id), str(node_id)),
                latency_ms=_optional_int(item.get("latencyMs")),
                alive=bool(item.get("alive")),
                tested_at=item.get("testedAt"),
                message=item.get("message"),
            )
        return latencies

    async def async_trigger_test(self) -> list[dict[str, Any]]:
        """Ask daed to dial every node, then refresh the cached results."""
        results = await self.client.async_test_node_latencies()
        await self.async_request_refresh()
        return results


def _group(payload: Mapping[str, Any]) -> GroupInfo:
    """Normalize one group."""
    extra_nodes = sum(
        _count(sub.get("matchedCount")) for sub in payload.get("subscriptions") or []
    )
    return GroupInfo(
        id=str(payload.get("id") or payload.get("name")),
        name=str(payload.get("name") or ""),
        policy=str(payload.get("policy") or ""),
        nodes=len(payload.get("nodes") or []) + extra_nodes,
    )


def _subscription(payload: Mapping[str, Any]) -> SubscriptionInfo:
    """Normalize one subscription."""
    return SubscriptionInfo(
        id=str(payload.get("id")),
        tag=str(payload.get("tag") or ""),
        status=str(payload.get("status") or ""),
        info=payload.get("info"),
        updated_at=payload.get("updatedAt"),
        cron_exp=payload.get("cronExp"),
        cron_enable=bool(payload.get("cronEnable")),
    )


def _configured_config(configs: list[dict[str, Any]]) -> str | None:
    """Return the name of the selected config."""
    for config in configs:
        if config.get("selected"):
            return str(config.get("name") or config.get("id"))
    return None


def _count(value: Any) -> int:
    """Parse an integer counter, tolerating None and junk."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    """Parse an optional integer."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _total(value: Any) -> int | None:
    """Parse daed's string encoded byte counters."""
    if value is None:
        return None
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return None
