"""Diagnostics support for the daed integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import DOMAIN

TO_REDACT = {CONF_PASSWORD}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry
) -> dict[str, Any]:
    """Return diagnostics for a daed config entry."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    data = runtime.runtime.data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "runtime": {
            "running": data.running,
            "modified": data.modified,
            "version": data.version,
            "config": data.configured_config,
            "updated_at": data.updated_at.isoformat() if data.updated_at else None,
            "upload_rate": data.upload_rate,
            "download_rate": data.download_rate,
            "upload_total": data.upload_total,
            "download_total": data.download_total,
            "active_connections": data.active_connections,
            "udp_sessions": data.udp_sessions,
            "groups": [asdict(group) for group in data.groups],
            "subscriptions": [asdict(sub) for sub in data.subscriptions],
        },
        "latencies": [asdict(item) for item in (runtime.latency.data or {}).values()],
        "coordinators": {
            "runtime": _coordinator_state(runtime.runtime),
            "latency": _coordinator_state(runtime.latency),
        },
    }


def _coordinator_state(coordinator: Any) -> dict[str, Any]:
    """Describe one coordinator's health for support purposes."""
    # `last_update_success_time` only exists on newer Home Assistant versions.
    last_ok = getattr(coordinator, "last_update_success_time", None)
    return {
        "last_update_success": coordinator.last_update_success,
        "last_update_success_time": last_ok.isoformat() if last_ok else None,
        "update_interval": (
            coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None
        ),
        "last_exception": (
            repr(coordinator.last_exception)
            if coordinator.last_exception is not None
            else None
        ),
    }
