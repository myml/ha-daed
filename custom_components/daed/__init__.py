"""The daed integration.

daed is the web dashboard for `dae`, an eBPF transparent proxy for Linux. This
integration polls the daed GraphQL API and exposes dae's runtime state, traffic
counters, subscriptions and per-node latencies to Home Assistant, plus services
to apply a config and to re-test node latency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DaedClient, DaedError
from .config_flow import (
    latency_interval_for_entry,
    scan_interval_for_entry,
    verify_ssl_for_entry,
)
from .const import (
    ATTR_CONFIG_ID,
    ATTR_ENTRY_ID,
    ATTR_SUBSCRIPTION_IDS,
    CONF_BASE_URL,
    DOMAIN,
    SERVICE_RUN,
    SERVICE_SELECT_CONFIG,
    SERVICE_TEST_NODE_LATENCY,
    SERVICE_UPDATE_SUBSCRIPTIONS,
)
from .coordinator import DaedLatencyCoordinator, DaedRuntimeCoordinator
from .entity import runtime_device_info

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

# Entity keys earlier versions registered and later ones dropped.
_STALE_ENTITY_KEYS: tuple[str, ...] = ("last_updated",)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SELECT_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Required(ATTR_CONFIG_ID): cv.string,
    }
)


@dataclass(slots=True)
class DaedRuntime:
    """The coordinators belonging to one daed config entry."""

    runtime: DaedRuntimeCoordinator
    latency: DaedLatencyCoordinator


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up daed (config entry only)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up daed from a config entry."""
    session = async_get_clientsession(hass, verify_ssl=verify_ssl_for_entry(entry))
    client = DaedClient(
        session,
        entry.data[CONF_BASE_URL],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )

    runtime = DaedRuntimeCoordinator(
        hass, entry, client, scan_interval_for_entry(entry)
    )
    # Raises ConfigEntryAuthFailed (which starts the reauth flow) or
    # ConfigEntryNotReady on the first failure.
    await runtime.async_config_entry_first_refresh()

    latency = DaedLatencyCoordinator(
        hass,
        entry,
        client,
        latency_interval_for_entry(entry),
        node_names=lambda: runtime.node_names,
    )
    # The latency cache is optional: a failure must never fail the entry.
    await latency.async_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = DaedRuntime(
        runtime=runtime, latency=latency
    )

    # Create the device so it exists even before the platforms add their
    # entities; `device_info` on the entities uses the same identifiers.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, **runtime_device_info(runtime)
    )

    _async_remove_stale_sensors(hass, entry, client)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # The registry only takes non-identifier fields (sw_version, model, ...) from
    # `async_get_or_create`, not from the entities' device_info, so re-registering
    # once with the version from the first poll keeps the device record current.
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, **runtime_device_info(runtime)
    )

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _async_register_services(hass)

    return True


def _async_remove_stale_sensors(
    hass: HomeAssistant, entry: ConfigEntry, client: DaedClient
) -> None:
    """Drop entities a previous version registered but this one no longer has.

    ``sensor.daed_last_updated`` was removed because it changed on every poll
    and flooded the recorder; leaving it behind would keep a dead entity that
    can never update again.
    """
    registry = er.async_get(hass)
    stale_unique_ids = {f"{client.base_url}_{key}" for key in _STALE_ENTITY_KEYS}
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    for entity_entry in list(entries):
        if entity_entry.platform != DOMAIN:
            continue
        if entity_entry.domain != Platform.SENSOR:
            continue
        if entity_entry.unique_id not in stale_unique_ids:
            continue
        _LOGGER.info(
            "Removing obsolete daed entity %s (unique_id %s)",
            entity_entry.entity_id,
            entity_entry.unique_id,
        )
        registry.async_remove(entity_entry.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a daed config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    runtimes: dict[str, DaedRuntime] = hass.data.get(DOMAIN, {})
    runtimes.pop(entry.entry_id, None)

    if not runtimes:
        for service in (
            SERVICE_RUN,
            SERVICE_TEST_NODE_LATENCY,
            SERVICE_UPDATE_SUBSCRIPTIONS,
            SERVICE_SELECT_CONFIG,
        ):
            hass.services.async_remove(DOMAIN, service)
        hass.data.pop(DOMAIN, None)

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry so option changes take effect."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_runtime(hass: HomeAssistant, entry_id: str | None) -> DaedRuntime:
    """Return the single runtime, or the one the service call targets."""
    runtimes: dict[str, DaedRuntime] = hass.data.get(DOMAIN, {})
    if not runtimes:
        raise ServiceValidationError("daed is not configured")

    if entry_id:
        runtime = runtimes.get(entry_id)
        if runtime is None:
            raise ServiceValidationError(f"No daed entry with id {entry_id}")
        return runtime

    if len(runtimes) > 1:
        raise ServiceValidationError(
            "More than one daed instance is configured; pass entry_id"
        )
    return next(iter(runtimes.values()))


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register the integration services once."""
    if hass.services.has_service(DOMAIN, SERVICE_RUN):
        return

    async def _async_handle_run(call: ServiceCall) -> None:
        runtime = _async_runtime(hass, call.data.get(ATTR_ENTRY_ID))
        try:
            code = await runtime.runtime.async_trigger_run()
        except DaedError as err:
            raise HomeAssistantError(f"daed could not apply the config: {err}") from err
        _LOGGER.debug("dae applied the config with exit code %s", code)

    async def _async_handle_test_latency(call: ServiceCall) -> None:
        runtime = _async_runtime(hass, call.data.get(ATTR_ENTRY_ID))
        try:
            await runtime.latency.async_trigger_test()
        except DaedError as err:
            raise HomeAssistantError(f"daed latency test failed: {err}") from err

    async def _async_handle_update_subscriptions(call: ServiceCall) -> None:
        runtime = _async_runtime(hass, call.data.get(ATTR_ENTRY_ID))
        data = runtime.runtime.data
        ids = [sub.id for sub in data.subscriptions] if data else []
        wanted = call.data.get(ATTR_SUBSCRIPTION_IDS)
        if wanted:
            missing = set(wanted) - set(ids)
            if missing:
                raise ServiceValidationError(
                    f"daed has no subscription with id(s) {', '.join(sorted(missing))}"
                )
            ids = list(wanted)
        if not ids:
            raise ServiceValidationError("There are no subscriptions to update")
        try:
            await runtime.runtime.client.async_update_subscriptions(ids)
        except DaedError as err:
            raise HomeAssistantError(
                f"Updating daed subscriptions failed: {err}"
            ) from err
        await runtime.runtime.async_request_refresh()

    async def _async_handle_select_config(call: ServiceCall) -> None:
        runtime = _async_runtime(hass, call.data.get(ATTR_ENTRY_ID))
        config_id = call.data[ATTR_CONFIG_ID]
        try:
            configs = await runtime.runtime.client.async_get_configs()
        except DaedError as err:
            raise HomeAssistantError(f"Cannot list daed configs: {err}") from err
        if config_id not in {str(config.get("id")) for config in configs}:
            raise ServiceValidationError(f"daed has no config with id {config_id}")
        try:
            await runtime.runtime.async_trigger_select_config(config_id)
        except DaedError as err:
            raise HomeAssistantError(
                f"Selecting the daed config failed: {err}"
            ) from err

    hass.services.async_register(DOMAIN, SERVICE_RUN, _async_handle_run)
    hass.services.async_register(
        DOMAIN, SERVICE_TEST_NODE_LATENCY, _async_handle_test_latency
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE_SUBSCRIPTIONS, _async_handle_update_subscriptions
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SELECT_CONFIG, _async_handle_select_config, SELECT_CONFIG_SCHEMA
    )
