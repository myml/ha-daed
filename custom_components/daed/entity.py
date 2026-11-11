"""Shared entity plumbing for the daed integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import DaedClient
from .const import DOMAIN
from .coordinator import DaedLatencyCoordinator, DaedRuntimeCoordinator


def daed_device_info(client: DaedClient, version: str | None = None) -> DeviceInfo:
    """Return the device info of the dae router running daed.

    The identifiers only depend on the base URL, so the device survives a
    re-authentication as long as the URL stays the same.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, client.base_url)},
        name="daed",
        manufacturer="daeuniverse",
        model="dae / daed",
        sw_version=version,
        configuration_url=client.base_url,
        entry_type=DeviceEntryType.SERVICE,
    )


def runtime_device_info(coordinator: DaedRuntimeCoordinator) -> DeviceInfo:
    """Return the daed device info including dae's version.

    ``coordinator.data`` is only assigned after a fully successful update, so
    the version is read from the coordinator, which keeps the `general` payload
    around even when a later call in the same update failed.
    """
    return daed_device_info(coordinator.client, coordinator.version)


class DaedRuntimeEntity(CoordinatorEntity[DaedRuntimeCoordinator]):
    """Base class for entities backed by the runtime coordinator.

    ``entity_key`` must be set by the subclass before ``__init__`` returns; the
    unique id derives from it and from the daed URL, so it survives restarts and
    re-authentication.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: DaedRuntimeCoordinator, entity_key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.client.base_url}_{entity_key}"
        self._attr_device_info = runtime_device_info(coordinator)


class DaedLatencyEntity(CoordinatorEntity[DaedLatencyCoordinator]):
    """Base class for entities backed by the latency coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DaedLatencyCoordinator, entity_key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.client.base_url}_{entity_key}"
        self._attr_device_info = daed_device_info(coordinator.client)
