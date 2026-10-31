"""Custom integration to integrate GM Ultium vehicles with Home Assistant.

For more details about this integration, please refer to
https://github.com/Coldarn/hacs_gm_ultium

Derived from dala318/obd2_ble (MIT); see README.md for attribution.
"""

import logging
from typing import Final

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.config_validation import config_entry_only_config_schema

from .const import (
    ACTION_ATTEMPT_CONNECT,
    ACTION_GET_SLEEP_LOG_SUMMARY,
    ACTION_START_SLEEP_LOG,
    ACTION_STOP_SLEEP_LOG,
    CONF_CSV,
    CONF_DURATION,
    CONF_INTERVAL,
    CONF_LINK,
    CONF_MAX_CYCLES,
    CONF_MEASUREMENT_MODE,
    CONF_OUTPUT,
    CONF_SLEEP_MODE,
    DOMAIN,
    PLATFORMS,
    STARTUP_MESSAGE,
)
from .coordinator import Obd2BleConfigEntry, Obd2BleDataUpdateCoordinator
from .sleep_log import SleepLogger

_LOGGER: logging.Logger = logging.getLogger(__package__)

CONFIG_SCHEMA = config_entry_only_config_schema(DOMAIN)

START_SLEEP_LOG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SLEEP_MODE, default="cycle"): vol.In(
            ["persistent", "cycle", "dongle-only"]
        ),
        vol.Optional(CONF_LINK, default="auto"): vol.In(
            ["auto", "persistent", "cycle"]
        ),
        vol.Optional(CONF_INTERVAL, default=60): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=3600)
        ),
        vol.Optional(CONF_DURATION, default=24): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=168)
        ),
        vol.Optional(CONF_CSV, default=True): cv.boolean,
        vol.Optional(CONF_OUTPUT, default=""): cv.string,
        vol.Optional(CONF_MAX_CYCLES, default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0)
        ),
    }
)


def _get_sleep_logger(hass: HomeAssistant) -> SleepLogger | None:
    return hass.data.get(DOMAIN, {}).get("sleep_log")


def _async_register_measurement_services(
    hass: HomeAssistant, entry: Obd2BleConfigEntry
) -> None:
    """Register the sleep/power measurement services for this entry."""
    if hass.services.has_service(DOMAIN, ACTION_START_SLEEP_LOG):
        return

    async def handle_start(call: ServiceCall) -> ServiceResponse:
        existing = _get_sleep_logger(hass)
        if existing is not None and existing.running:
            raise ServiceValidationError("A sleep log is already running")

        if call.data[CONF_DURATION] <= 0 and call.data.get(CONF_MAX_CYCLES, 0) <= 0:
            raise ServiceValidationError(
                "Set duration > 0 or max_cycles > 0"
            )

        logger = SleepLogger(
            hass=hass,
            address=entry.unique_id,
            mode=call.data[CONF_SLEEP_MODE],
            link=call.data[CONF_LINK],
            interval=call.data[CONF_INTERVAL],
            duration=call.data[CONF_DURATION],
            csv_enabled=call.data[CONF_CSV],
            output=call.data.get(CONF_OUTPUT, ""),
            max_cycles=call.data.get(CONF_MAX_CYCLES, 0),
        )
        await logger.start()
        hass.data[DOMAIN]["sleep_log"] = logger
        return {
            "output": logger.jsonl_url,
            "csv": logger.csv_url,
            "summary": logger.summary_url,
        }

    async def handle_stop(call: ServiceCall) -> ServiceResponse:
        logger = _get_sleep_logger(hass)
        if logger is None:
            return {"stopped": False}
        await logger.stop()
        return {"stopped": True, "summary": logger.summary_url}

    async def handle_summary(call: ServiceCall) -> ServiceResponse:
        logger = _get_sleep_logger(hass)
        if logger is None:
            return {"summary": None, "url": None}
        return {"summary": logger.summary, "url": logger.summary_url}

    hass.services.async_register(
        DOMAIN,
        ACTION_START_SLEEP_LOG,
        handle_start,
        schema=START_SLEEP_LOG_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        ACTION_STOP_SLEEP_LOG,
        handle_stop,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        ACTION_GET_SLEEP_LOG_SUMMARY,
        handle_summary,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_setup_entry(hass: HomeAssistant, entry: Obd2BleConfigEntry) -> bool:
    """Set up this integration using UI."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        _LOGGER.info(STARTUP_MESSAGE)

    if entry.unique_id is None:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="missing_unique_id",
        )

    measurement_mode = bool(entry.data.get(CONF_MEASUREMENT_MODE, False))
    coordinator = Obd2BleDataUpdateCoordinator(hass, entry=entry)

    # await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    if measurement_mode:
        _async_register_measurement_services(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if not measurement_mode:
        hass.async_create_task(coordinator.async_request_refresh())

    @callback
    def _async_specific_device_found(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle re-discovery of the device."""
        if measurement_mode:
            return
        _LOGGER.debug("New service_info: %s - %s", service_info, change)
        hass.async_create_task(coordinator.async_request_refresh())

    # Stuff to do when cleaning up
    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_specific_device_found,
            {"address": entry.unique_id},
            bluetooth.BluetoothScanningMode.ACTIVE,
        )  # does the register callback, and returns a cancel callback for cleanup
    )

    async def handle_attempt_to_connect(call: ServiceCall) -> ServiceResponse:
        """Attempt a connection to the vehicle and return a boolean status."""
        _LOGGER.info("Firing action 'attempt_to_connect' via user trigger")

        try:
            connected_successfully = await coordinator.async_force_update()
            return {"connected": bool(connected_successfully)}
        except Exception as err:  # noqa: BLE001
            raise ServiceValidationError(
                f"Communication failed while targeting the OBD2 device: {err}"
            )

    hass.services.async_register(
        domain=DOMAIN,
        service=ACTION_ATTEMPT_CONNECT,
        service_func=handle_attempt_to_connect,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: Obd2BleConfigEntry) -> bool:
    """Unload a config entry."""
    if entry.data.get(CONF_MEASUREMENT_MODE):
        logger = _get_sleep_logger(hass)
        if logger is not None:
            await logger.stop()
            hass.data[DOMAIN]["sleep_log"] = None
        for service in (
            ACTION_START_SLEEP_LOG,
            ACTION_STOP_SLEEP_LOG,
            ACTION_GET_SLEEP_LOG_SUMMARY,
        ):
            if hass.services.has_service(DOMAIN, service):
                hass.services.async_remove(DOMAIN, service)

    unloaded: Final = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    _LOGGER.debug("Unloaded config entry: %s, ok? %s!", entry.unique_id, unloaded)
    if unloaded and getattr(entry, "runtime_data", None) is not None:
        await entry.runtime_data.async_shutdown()
    return unloaded
