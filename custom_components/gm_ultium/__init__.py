"""Custom integration to integrate OBD2 BLE with Home Assistant.

For more details about this integration, please refer to
https://github.com/dala318/obd2_ble
"""

import logging
from typing_extensions import Final

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError
from homeassistant.helpers.config_validation import config_entry_only_config_schema

from .const import (
    DOMAIN,
    ACTION_ATTEMPT_CONNECT,
    PLATFORMS,
    STARTUP_MESSAGE
)
from .coordinator import Obd2BleDataUpdateCoordinator, Obd2BleConfigEntry

_LOGGER: logging.Logger = logging.getLogger(__package__)

CONFIG_SCHEMA = config_entry_only_config_schema(DOMAIN)


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

    coordinator = Obd2BleDataUpdateCoordinator(
        hass, entry=entry
    )

    # await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    @callback
    def _async_specific_device_found(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle re-discovery of the device."""
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
        except Exception as err:
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
    unloaded: Final = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    _LOGGER.debug("Unloaded config entry: %s, ok? %s!", entry.unique_id, unloaded)
    if unloaded and getattr(entry, "runtime_data", None) is not None:
        await entry.runtime_data.async_shutdown()
    return unloaded
