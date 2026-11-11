"""Sensor platform for the daed integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfDataRate, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import (
    DaedLatencyCoordinator,
    DaedRuntime,
    DaedRuntimeCoordinator,
    GroupInfo,
    NodeLatency,
    SubscriptionInfo,
)
from .entity import DaedLatencyEntity, DaedRuntimeEntity


@dataclass(frozen=True, kw_only=True)
class DaedSensorEntityDescription(SensorEntityDescription):
    """Describes a daed runtime sensor."""

    value_fn: Callable[[DaedRuntime], StateType | datetime]


RUNTIME_SENSORS: tuple[DaedSensorEntityDescription, ...] = (
    DaedSensorEntityDescription(
        key="download_rate",
        translation_key="download_rate",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.KILOBYTES_PER_SECOND,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.download_rate,
    ),
    DaedSensorEntityDescription(
        key="upload_rate",
        translation_key="upload_rate",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.KILOBYTES_PER_SECOND,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.upload_rate,
    ),
    DaedSensorEntityDescription(
        key="download_total",
        translation_key="download_total",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=2,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.download_total,
    ),
    DaedSensorEntityDescription(
        key="upload_total",
        translation_key="upload_total",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=2,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.upload_total,
    ),
    DaedSensorEntityDescription(
        key="active_connections",
        translation_key="active_connections",
        icon="mdi:lan-connect",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.active_connections,
    ),
    DaedSensorEntityDescription(
        key="udp_sessions",
        translation_key="udp_sessions",
        icon="mdi:udp",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.udp_sessions,
    ),
    DaedSensorEntityDescription(
        key="group_count",
        translation_key="group_count",
        icon="mdi:account-group",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: len(data.groups),
    ),
    DaedSensorEntityDescription(
        key="subscription_count",
        translation_key="subscription_count",
        icon="mdi:rss",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: len(data.subscriptions),
    ),
    DaedSensorEntityDescription(
        key="version",
        translation_key="version",
        icon="mdi:tag-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.version,
    ),
    DaedSensorEntityDescription(
        key="config",
        translation_key="config",
        icon="mdi:file-cog-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.configured_config,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the daed sensors."""
    runtime: DaedRuntime = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        DaedRuntimeSensor(runtime.runtime, description)
        for description in RUNTIME_SENSORS
    ]
    entities.extend(
        DaedGroupSensor(runtime.runtime, group) for group in runtime.runtime.data.groups
    )
    entities.extend(
        DaedSubscriptionSensor(runtime.runtime, sub)
        for sub in runtime.runtime.data.subscriptions
    )
    entities.extend(
        DaedLatencySensor(runtime.latency, latency)
        for latency in (runtime.latency.data or {}).values()
    )
    async_add_entities(entities)


class DaedRuntimeSensor(DaedRuntimeEntity, SensorEntity):
    """One sensor describing the dae instance as a whole."""

    entity_description: DaedSensorEntityDescription

    def __init__(
        self,
        coordinator: DaedRuntimeCoordinator,
        description: DaedSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType | datetime:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return dae's flags and the last poll time on the version sensor.

        The timestamp lives here instead of in its own entity: it changes on
        every poll, so an entity would write a state row every 30 s and flood
        the recorder and the activity log without carrying any information.
        """
        data = self.coordinator.data
        if data is None or self.entity_description.key != "version":
            return None
        attributes: dict[str, Any] = {
            "modified": data.modified,
            "running": data.running,
        }
        if data.updated_at is not None:
            attributes["last_updated"] = data.updated_at
        return attributes


class DaedGroupSensor(DaedRuntimeEntity, SensorEntity):
    """Node count of one daed group."""

    _attr_icon = "mdi:account-group"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "group_nodes"

    def __init__(self, coordinator: DaedRuntimeCoordinator, group: GroupInfo) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, f"group_{group.id}")
        self._group_id = group.id
        self._attr_translation_placeholders = {"name": group.name}

    @property
    def group_info(self) -> GroupInfo | None:
        """Return the group this sensor describes.

        Deliberately not named ``group``: ``SensorEntity`` reserves that name
        for its entity group and HA reports a frame-helper warning otherwise.
        """
        if not self.coordinator.data:
            return None
        return next(
            (g for g in self.coordinator.data.groups if g.id == self._group_id), None
        )

    @property
    def available(self) -> bool:
        """Return False once the group is gone."""
        return super().available and self.group_info is not None

    @property
    def native_value(self) -> int | None:
        """Return the number of nodes in the group."""
        group = self.group_info
        return group.nodes if group else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the group load balancing policy."""
        group = self.group_info
        return {"policy": group.policy} if group else None


class DaedSubscriptionSensor(DaedRuntimeEntity, SensorEntity):
    """Health of one subscription."""

    _attr_icon = "mdi:playlist-check"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ok", "error"]
    _attr_translation_key = "subscription_status"

    def __init__(
        self, coordinator: DaedRuntimeCoordinator, subscription: SubscriptionInfo
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, f"subscription_{subscription.id}")
        self._subscription_id = subscription.id
        self._attr_translation_placeholders = {"name": subscription.display_name}

    @property
    def subscription(self) -> SubscriptionInfo | None:
        """Return the subscription this sensor describes."""
        if not self.coordinator.data:
            return None
        return next(
            (
                s
                for s in self.coordinator.data.subscriptions
                if s.id == self._subscription_id
            ),
            None,
        )

    @property
    def available(self) -> bool:
        """Return False once the subscription is gone."""
        return super().available and self.subscription is not None

    @property
    def native_value(self) -> str | None:
        """Return ok/error for the subscription."""
        sub = self.subscription
        if sub is None:
            return None
        return "ok" if sub.healthy else "error"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the raw status, info text and cron schedule."""
        sub = self.subscription
        if sub is None:
            return None
        attributes: dict[str, Any] = {"status": sub.status or None}
        if sub.info:
            attributes["info"] = sub.info
        if sub.updated_at:
            updated = dt_util.parse_datetime(sub.updated_at)
            if updated is not None:
                attributes["last_updated"] = updated
        if sub.cron_exp:
            attributes["cron"] = sub.cron_exp
        attributes["cron_enabled"] = sub.cron_enable
        return attributes


class DaedLatencySensor(DaedLatencyEntity, SensorEntity):
    """Latency of one daed node, measured by daed itself."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "ms"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:signal"
    _attr_translation_key = "node_latency"

    def __init__(
        self, coordinator: DaedLatencyCoordinator, latency: NodeLatency
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, f"node_{latency.node_id}_latency")
        self._node_id = latency.node_id
        self._attr_translation_placeholders = {"name": latency.name}

    @property
    def latency(self) -> NodeLatency | None:
        """Return the measurement this sensor describes."""
        return (self.coordinator.data or {}).get(self._node_id)

    @property
    def available(self) -> bool:
        """Return False when daed no longer reports the node."""
        return super().available and self.latency is not None

    @property
    def native_value(self) -> int | None:
        """Return the latency in milliseconds."""
        latency = self.latency
        return latency.latency_ms if latency else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return whether the node is alive and when it was tested."""
        latency = self.latency
        if latency is None:
            return None
        attributes: dict[str, Any] = {"alive": latency.alive}
        if latency.message:
            attributes["message"] = latency.message
        if latency.tested_at:
            tested = dt_util.parse_datetime(latency.tested_at)
            if tested is not None:
                attributes["tested_at"] = tested
        return attributes
