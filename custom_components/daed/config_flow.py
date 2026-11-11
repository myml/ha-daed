"""Config flow for the daed integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    DaedAuthError,
    DaedClient,
    DaedConnectionError,
    DaedError,
    normalize_base_url,
)
from .const import (
    CONF_BASE_URL,
    CONF_LATENCY_INTERVAL_SECONDS,
    CONF_SCAN_INTERVAL_SECONDS,
    CONF_VERIFY_SSL,
    DEFAULT_LATENCY_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_LATENCY_INTERVAL_SECONDS,
    MAX_SCAN_INTERVAL_SECONDS,
    MIN_LATENCY_INTERVAL_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:2023"


def verify_ssl_for_entry(entry: ConfigEntry) -> bool:
    """Return the effective ``verify_ssl`` setting of an entry."""
    return bool(
        entry.options.get(CONF_VERIFY_SSL, entry.data.get(CONF_VERIFY_SSL, True))
    )


def scan_interval_for_entry(entry: ConfigEntry) -> timedelta:
    """Return the effective polling interval of an entry."""
    seconds = int(
        entry.options.get(
            CONF_SCAN_INTERVAL_SECONDS, int(DEFAULT_SCAN_INTERVAL.total_seconds())
        )
    )
    return timedelta(seconds=max(MIN_SCAN_INTERVAL_SECONDS, seconds))


def latency_interval_for_entry(entry: ConfigEntry) -> timedelta | None:
    """Return the latency poll interval, or ``None`` when polling is off.

    ``0`` disables the automatic latency poll; the latency entities are then
    only updated by the ``daed.test_node_latency`` action.
    """
    raw = int(
        entry.options.get(
            CONF_LATENCY_INTERVAL_SECONDS, int(DEFAULT_LATENCY_INTERVAL.total_seconds())
        )
    )
    if raw <= 0:
        return None
    return timedelta(seconds=max(MIN_LATENCY_INTERVAL_SECONDS, raw))


class DaedConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the daed config and reauth flows."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the daed URL and account credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            data = dict(user_input)
            try:
                data[CONF_BASE_URL] = normalize_base_url(data.get(CONF_BASE_URL, ""))
            except DaedConnectionError:
                errors["base"] = "invalid_url"
            else:
                errors = await self._async_validate(data)
                if not errors:
                    await self.async_set_unique_id(data[CONF_BASE_URL])
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=_entry_title(data[CONF_BASE_URL]),
                        data=data,
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=self._credentials_schema(user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start the reauth flow after daed rejected the credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update the stored credentials."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry is not None

        if user_input is not None:
            data = {**entry.data, **user_input}
            errors = await self._async_validate(data)
            if not errors:
                self.hass.config_entries.async_update_entry(entry, data=data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=entry.data.get(CONF_USERNAME)
                    ): TextSelector(),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={CONF_BASE_URL: entry.data[CONF_BASE_URL]},
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return DaedOptionsFlow(config_entry)

    async def _async_validate(self, data: Mapping[str, Any]) -> dict[str, str]:
        """Sign in and read the dae state to verify URL and credentials."""
        client = DaedClient(
            async_get_clientsession(self.hass),
            data[CONF_BASE_URL],
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
        )
        try:
            await client.async_login()
            await client.async_get_general()
        except DaedAuthError:
            return {"base": "invalid_auth"}
        except DaedConnectionError:
            return {"base": "cannot_connect"}
        except DaedError as err:
            _LOGGER.debug("Validating daed at %s failed: %s", data[CONF_BASE_URL], err)
            return {"base": "unknown"}
        return {}

    @staticmethod
    def _credentials_schema(user_input: dict[str, Any] | None) -> vol.Schema:
        """Return the credentials form schema."""
        defaults = user_input or {}
        return vol.Schema(
            {
                vol.Required(
                    CONF_BASE_URL, default=defaults.get(CONF_BASE_URL, DEFAULT_BASE_URL)
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                vol.Required(CONF_USERNAME): TextSelector(),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional(
                    CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)
                ): BooleanSelector(),
            }
        )


class DaedOptionsFlow(OptionsFlow):
    """Configure the polling intervals."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize the options flow."""
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self._entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL_SECONDS,
                        default=int(
                            options.get(
                                CONF_SCAN_INTERVAL_SECONDS,
                                int(DEFAULT_SCAN_INTERVAL.total_seconds()),
                            )
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL_SECONDS,
                            max=MAX_SCAN_INTERVAL_SECONDS,
                            step=1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_LATENCY_INTERVAL_SECONDS,
                        default=int(
                            options.get(
                                CONF_LATENCY_INTERVAL_SECONDS,
                                int(DEFAULT_LATENCY_INTERVAL.total_seconds()),
                            )
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=MAX_LATENCY_INTERVAL_SECONDS,
                            step=1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )


def _entry_title(base_url: str) -> str:
    """Return the config entry title for a daed instance."""
    return base_url.split("://", 1)[-1]
