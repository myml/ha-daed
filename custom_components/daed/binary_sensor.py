"""Binary sensor platform for the daed integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import DaedLatencyCoordinator, DaedRuntime, DaedRuntimeCoordinator
from .entity import DaedLatencyEntity, DaedRuntimeEntity


@dataclass(frozen=True, kw_only=True)
class DaedBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a daed binary sensor."""

    value_fn: Callable[[DaedRuntime], bool]


RUNTIME_BINARY_SENSORS: tuple[DaedBinarySensorEntityDescription, ...] = (
    DaedBinarySensorEntityDescription(
        key="running",
        translation_key="running",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda data: data.running,
    ),
    DaedBinarySensorEntityDescription(
        key="config_modified",
        translation_key="config_modified",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.modified,
    ),
    DaedBinarySensorEntityDescription(
        key="subscription_errors",
        translation_key="subscription_errors",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.subscription_errors > 0,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the daed binary sensors."""
    runtimes = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [
        DaedRuntimeBinarySensor(runtimes.runtime, description)
        for description in RUNTIME_BINARY_SENSORS
    ]
    entities.extend(
        DaedNodeAliveBinarySensor(runtimes.latency, node_id)
        for node_id in (runtimes.latency.data or {})
    )
    async_add_entities(entities)


class DaedRuntimeBinarySensor(DaedRuntimeEntity, BinarySensorEntity):
    """One binary sensor describing the dae instance."""

    entity_description: DaedBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: DaedRuntimeCoordinator,
        description: DaedBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the sensor state."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)


class DaedNodeAliveBinarySensor(DaedLatencyEntity, BinarySensorEntity):
    """Whether daed considers one node reachable."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "node_alive"

    def __init__(self, coordinator: DaedLatencyCoordinator, node_id: str) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, f"node_{node_id}_alive")
        self._node_id = node_id
        latency = (coordinator.data or {}).get(node_id)
        self._attr_translation_placeholders = {
            "name": latency.name if latency else node_id
        }

    @property
    def available(self) -> bool:
        """Return False when daed no longer reports the node."""
        return super().available and self._node_id in (self.coordinator.data or {})

    @property
    def is_on(self) -> bool | None:
        """Return whether the node is reachable."""
        latency = (self.coordinator.data or {}).get(self._node_id)
        return latency.alive if latency else None
