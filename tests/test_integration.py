"""Tests for the daed integration inside Home Assistant.

The GraphQL transport is scripted (``tests.mock_daed``), so these tests cover
the Home Assistant plumbing: setup, entities and devices, the services, the
config/reauth/options flows and the coordinator's failure handling.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    UnitOfDataRate,
    UnitOfInformation,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.daed.const import (
    CONF_BASE_URL,
    CONF_LATENCY_INTERVAL_SECONDS,
    CONF_SCAN_INTERVAL_SECONDS,
    DOMAIN,
)

from .mock_daed import DaedMock, config_entry, install_daed


@pytest.fixture
def mock() -> Iterator[DaedMock]:
    """Install the scripted daed transport."""
    with install_daed() as scripted:
        yield scripted


@pytest.fixture
async def entry(hass: HomeAssistant, mock: DaedMock) -> MockConfigEntry:
    """Set up a loaded daed config entry."""
    setup_entry = config_entry(mock)
    setup_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(setup_entry.entry_id)
    await hass.async_block_till_done()
    return setup_entry


def state(hass: HomeAssistant, entity_id: str):
    """Return a state, asserting it exists."""
    value = hass.states.get(entity_id)
    assert value is not None, f"{entity_id} is missing"
    return value


async def test_setup_creates_entities(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The entry loads and every entity carries the expected state."""
    assert entry.state is ConfigEntryState.LOADED

    assert state(hass, "binary_sensor.daed_dae_running").state == "on"
    assert state(hass, "binary_sensor.daed_config_modified").state == "off"
    assert state(hass, "binary_sensor.daed_subscription_errors").state == "off"

    assert state(hass, "sensor.daed_config").state == "global"
    assert state(hass, "sensor.daed_version").state == "2026.09.24"
    assert state(hass, "sensor.daed_active_connections").state == "19"
    assert state(hass, "sensor.daed_udp_sessions").state == "11"
    assert state(hass, "sensor.daed_groups").state == "2"
    assert state(hass, "sensor.daed_subscriptions").state == "0"

    assert state(hass, "sensor.daed_group_proxy_nodes").state == "1"
    assert state(hass, "sensor.daed_group_proxy_nodes").attributes["policy"] == "random"
    assert (
        state(hass, "sensor.daed_group_us_nodes").attributes["policy"]
        == "min_moving_avg"
    )

    assert state(hass, "sensor.daed_node_s1_latency").state == "193"
    assert state(hass, "binary_sensor.daed_node_s1_reachable").state == "on"


async def test_units_and_timestamps(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Rates and totals keep their native units, timestamps are parsed."""
    # Rates are native B/s but displayed as kB/s, so the state is converted
    # from daed's bytes-per-second value (2048.25 B/s).
    rate = state(hass, "sensor.daed_download_rate")
    assert rate.attributes["unit_of_measurement"] == UnitOfDataRate.KILOBYTES_PER_SECOND
    assert float(rate.state) == pytest.approx(2.04825)

    # Totals are native bytes shown as GB, without conversion of precision.
    total = state(hass, "sensor.daed_uploaded")
    assert total.attributes["unit_of_measurement"] == UnitOfInformation.GIGABYTES
    assert float(total.state) == pytest.approx(2.905213923)
    assert total.attributes["state_class"] == "total_increasing"

    # Upload and download must read their own field from daed's samples: the
    # mock reports 1024.5 B/s up and 2048.25 B/s down.
    upload = state(hass, "sensor.daed_upload_rate")
    assert float(upload.state) == pytest.approx(1.0245)

    # The poll time is an attribute, not an entity: an entity would write a
    # state row on every poll. daed sends nanosecond precision with a +08:00
    # offset, which must still parse.
    version = state(hass, "sensor.daed_version")
    updated = version.attributes["last_updated"]
    assert isinstance(updated, datetime)
    assert updated.utcoffset() is not None
    assert hass.states.get("sensor.daed_last_updated") is None
    assert version.attributes["running"] is True
    assert version.attributes["modified"] is False


async def test_device_is_created(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Every entity belongs to one daed device."""
    coord = hass.data[DOMAIN][entry.entry_id].runtime
    assert coord.last_update_success is True
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "http://daed.test:2023"), entry.entry_id
    )
    assert device is not None
    assert device.name == "daed", (device, [type(d).__name__ for d in []])
    assert device.manufacturer == "daeuniverse"
    assert device.sw_version == "2026.09.24"
    assert device.configuration_url == "http://daed.test:2023"


async def test_entities_have_stable_unique_ids(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Unique ids only depend on the URL and the entity key."""
    registry = er.async_get(hass)
    unique_ids = {
        registry.async_get(entity_id).unique_id
        for entity_id in hass.states.async_entity_ids()
    }
    assert "http://daed.test:2023_download_rate" in unique_ids
    assert "http://daed.test:2023_node_Y3Vyc29yMQ_latency" in unique_ids


async def test_subscriptions_and_their_health(
    hass: HomeAssistant, mock: DaedMock, entry: MockConfigEntry
) -> None:
    """Subscriptions are added on a reload and track their status."""
    mock.responses["subscriptions"] = {
        "data": {
            "subscriptions": [
                {
                    "id": "sub1",
                    "tag": "main",
                    "status": "ok",
                    "info": "12 nodes",
                    "updatedAt": "2026-11-11T21:40:00+08:00",
                    "cronExp": "0 3 * * *",
                    "cronEnable": True,
                },
                {
                    "id": "sub2",
                    "tag": "broken",
                    "status": "failed to parse",
                    "info": None,
                    "updatedAt": None,
                    "cronExp": None,
                    "cronEnable": False,
                },
            ]
        }
    }
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    healthy = state(hass, "sensor.daed_subscription_main")
    assert healthy.state == "ok"
    assert healthy.attributes["cron"] == "0 3 * * *"
    assert healthy.attributes["cron_enabled"] is True

    assert state(hass, "sensor.daed_subscription_broken").state == "error"
    assert state(hass, "sensor.daed_subscriptions").state == "2"
    assert state(hass, "binary_sensor.daed_subscription_errors").state == "on"


async def test_poll_updates_state(
    hass: HomeAssistant, mock: DaedMock, entry: MockConfigEntry, freezer
) -> None:
    """A scheduled refresh picks up new dae state."""
    mock.responses["general"] = {
        "data": {
            "general": {
                "dae": {"running": False, "modified": True, "version": None},
                "runtimeOverview": {
                    "updatedAt": "2026-11-11T21:50:00Z",
                    "uploadRate": 0,
                    "downloadRate": 0,
                    "uploadTotal": "1",
                    "downloadTotal": "2",
                    "activeConnections": 0,
                    "udpSessions": 0,
                },
            }
        }
    }

    freezer.tick(31)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert state(hass, "binary_sensor.daed_dae_running").state == "off"
    assert state(hass, "binary_sensor.daed_config_modified").state == "on"
    assert state(hass, "sensor.daed_version").state == "unknown"


async def test_failed_poll_marks_unavailable(
    hass: HomeAssistant, mock: DaedMock, entry: MockConfigEntry, freezer
) -> None:
    """A broken daed makes the runtime entities unavailable, not unloaded."""
    mock.connection_error = True

    freezer.tick(31)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert state(hass, "sensor.daed_active_connections").state == "unavailable"


async def test_latency_polling_can_be_disabled(
    hass: HomeAssistant, mock: DaedMock
) -> None:
    """With a 0 interval, only the latency action refreshes the cache."""
    disabled = config_entry(mock, options={CONF_LATENCY_INTERVAL_SECONDS: 0})
    disabled.add_to_hass(hass)
    assert await hass.config_entries.async_setup(disabled.entry_id)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][disabled.entry_id]
    assert runtime.latency.update_interval is None


# --- Services ----------------------------------------------------------------


async def test_service_run(
    hass: HomeAssistant, mock: DaedMock, entry: MockConfigEntry
) -> None:
    """daed.run applies the selected config."""
    await hass.services.async_call(DOMAIN, "run", {}, blocking=True)
    await hass.async_block_till_done()

    assert mock.requests_of("run")[0].variables == {"dry": False}


async def test_service_test_node_latency(
    hass: HomeAssistant, mock: DaedMock, entry: MockConfigEntry
) -> None:
    """daed.test_node_latency triggers a sweep and refreshes the sensors."""
    mock.responses["testNodeLatencies"] = {"data": {"testNodeLatencies": []}}
    mock.responses["nodeLatencies"] = {
        "data": {
            "nodeLatencies": [
                {
                    "id": "Y3Vyc29yMQ",
                    "latencyMs": 42,
                    "alive": True,
                    "testedAt": "2026-11-11T22:00:00Z",
                    "message": None,
                }
            ]
        }
    }

    await hass.services.async_call(DOMAIN, "test_node_latency", {}, blocking=True)
    await hass.async_block_till_done()

    assert len(mock.requests_of("testNodeLatencies")) == 1
    assert state(hass, "sensor.daed_node_s1_latency").state == "42"


async def test_service_select_config(
    hass: HomeAssistant, mock: DaedMock, entry: MockConfigEntry
) -> None:
    """daed.select_config selects an existing config and rejects unknown ids."""
    await hass.services.async_call(
        DOMAIN, "select_config", {"config_id": "Y3Vyc29yMQ"}, blocking=True
    )
    await hass.async_block_till_done()
    assert mock.requests_of("selectConfig")[0].variables == {"id": "Y3Vyc29yMQ"}

    from homeassistant.exceptions import ServiceValidationError

    with pytest.raises(ServiceValidationError, match="no config with id"):
        await hass.services.async_call(
            DOMAIN, "select_config", {"config_id": "nope"}, blocking=True
        )


async def test_service_update_subscriptions(
    hass: HomeAssistant, mock: DaedMock, entry: MockConfigEntry
) -> None:
    """Subscriptions are refreshed, and unknown ids are rejected."""
    from homeassistant.exceptions import ServiceValidationError

    with pytest.raises(ServiceValidationError, match="no subscriptions to update"):
        await hass.services.async_call(
            DOMAIN, "update_subscriptions", {}, blocking=True
        )

    mock.responses["subscriptions"] = {
        "data": {"subscriptions": [{"id": "sub1", "tag": "main", "status": "ok"}]}
    }
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(DOMAIN, "update_subscriptions", {}, blocking=True)
    await hass.async_block_till_done()
    assert [r.variables for r in mock.requests_of("updateSubscription")] == [
        {"id": "sub1"}
    ]

    with pytest.raises(ServiceValidationError, match="no subscription"):
        await hass.services.async_call(
            DOMAIN,
            "update_subscriptions",
            {"subscription_ids": ["ghost"]},
            blocking=True,
        )


# --- Config flow -------------------------------------------------------------


async def test_user_flow(hass: HomeAssistant, mock: DaedMock) -> None:
    """The user flow signs in and stores the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_BASE_URL: "http://daed.test:2023/",
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "secret",
            "verify_ssl": True,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert result["title"] == "daed.test:2023"
    assert result["data"][CONF_BASE_URL] == "http://daed.test:2023"
    assert result["result"].unique_id == "http://daed.test:2023"


async def test_user_flow_invalid_auth(hass: HomeAssistant, mock: DaedMock) -> None:
    """A wrong password shows the auth error."""
    mock.login_error = "incorrect username or password"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_BASE_URL: "http://daed.test:2023",
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "wrong",
            "verify_ssl": True,
        },
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass: HomeAssistant, mock: DaedMock) -> None:
    """An unreachable daed shows the connection error."""
    mock.connection_error = True
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_BASE_URL: "http://daed.test:2023",
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "secret",
            "verify_ssl": True,
        },
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_already_configured(hass: HomeAssistant, mock: DaedMock) -> None:
    """The same URL cannot be added twice."""
    first = config_entry(mock)
    first.add_to_hass(hass)
    assert await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_BASE_URL: "http://daed.test:2023",
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "secret",
            "verify_ssl": True,
        },
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, mock: DaedMock, entry: MockConfigEntry
) -> None:
    """Re-authentication replaces the stored password."""
    entry.async_start_reauth(hass)
    await hass.async_block_till_done()

    flow = next(iter(hass.config_entries.flow.async_progress()))
    assert flow["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        user_input={CONF_USERNAME: "admin", CONF_PASSWORD: "new-secret"},
    )
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-secret"
    assert entry.state is ConfigEntryState.LOADED


async def test_options_flow(
    hass: HomeAssistant, mock: DaedMock, entry: MockConfigEntry
) -> None:
    """The options flow changes the poll intervals."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_SCAN_INTERVAL_SECONDS: 60,
            CONF_LATENCY_INTERVAL_SECONDS: 0,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.runtime.update_interval.total_seconds() == 60
    assert runtime.latency.update_interval is None


async def test_auth_failure_on_setup_starts_reauth(
    hass: HomeAssistant, mock: DaedMock
) -> None:
    """Wrong credentials at setup put the entry into the reauth flow."""
    mock.login_error = "incorrect username or password"
    auth_entry = config_entry(mock)
    auth_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(auth_entry.entry_id)
    await hass.async_block_till_done()

    assert auth_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"].get("source") == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_unload(
    hass: HomeAssistant, mock: DaedMock, entry: MockConfigEntry
) -> None:
    """Unloading removes the services and the runtime data."""
    assert hass.services.has_service(DOMAIN, "run")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service(DOMAIN, "run")
    assert DOMAIN not in hass.data


# --- Diagnostics -------------------------------------------------------------


async def test_diagnostics_redacts_password(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Diagnostics include the runtime data but never the password."""
    from custom_components.daed.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry"]["data"][CONF_PASSWORD] == "**REDACTED**"
    assert diagnostics["runtime"]["version"] == "2026.09.24"
    assert diagnostics["runtime"]["config"] == "global"
    assert diagnostics["runtime"]["active_connections"] == 19
    # The poll time lives here as well as on the version sensor's attributes,
    # so a support dump shows how fresh the data is.
    assert diagnostics["runtime"]["updated_at"].startswith("2026-11-11T21:47:12")
    assert len(diagnostics["latencies"]) == 2

    coordinators = diagnostics["coordinators"]
    assert coordinators["runtime"]["last_update_success"] is True
    assert coordinators["runtime"]["update_interval"] == 30.0
    assert coordinators["runtime"]["last_exception"] is None
    assert coordinators["latency"]["last_update_success"] is True

    assert "secret" not in str(diagnostics)


async def test_group_sensor_avoids_reserved_attribute(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The group sensor must not shadow ``SensorEntity.group``.

    HA reserves ``group`` on sensor entities and reports a frame-helper warning
    (breaking in 2027.2) when an integration sets it to a non-Group value.
    """
    from homeassistant.components.sensor import SensorEntity

    from custom_components.daed.sensor import DaedGroupSensor

    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator = runtime.runtime
    group = coordinator.data.groups[0]
    sensor = DaedGroupSensor(coordinator, group)

    assert sensor.group_info is group
    assert sensor.native_value == group.nodes
    assert sensor.extra_state_attributes == {"policy": group.policy}

    assert DaedGroupSensor.group is SensorEntity.group


async def test_obsolete_last_updated_entity_is_removed(
    hass: HomeAssistant, mock: DaedMock
) -> None:
    """An entity dropped by this version is removed from the registry.

    Earlier versions registered ``sensor.daed_last_updated``, which wrote a
    state row on every poll. It must not linger as an orphan after upgrading.
    """
    registry = er.async_get(hass)
    setup_entry = config_entry(mock)
    setup_entry.add_to_hass(hass)
    stale = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "http://daed.test:2023_last_updated",
        suggested_object_id="daed_last_updated",
        config_entry=setup_entry,
    )
    assert await hass.config_entries.async_setup(setup_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(stale.entity_id) is None
    assert hass.states.get("sensor.daed_last_updated") is None
