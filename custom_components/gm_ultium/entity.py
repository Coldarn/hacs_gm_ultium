"""ObdBleEntity class."""

import logging

import obdii
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import Obd2BleDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


class ObdBleEntity(CoordinatorEntity[Obd2BleDataUpdateCoordinator]):
    """Config entry for gm_ultium."""

    def __init__(self, coordinator: Obd2BleDataUpdateCoordinator, config_entry, command: obdii.Command, domain) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self._command = command
        self._attr_device_info = coordinator.device_info
        self._attr_unique_id = f"{config_entry.data[CONF_ADDRESS]}-{domain}-{command.name}"

    async def async_added_to_hass(self):
        """Run when entity is added to register its command."""
        self.coordinator.active_commands.add(self._command)
        _LOGGER.debug("Added command %s to active commands", self._command)
        await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        """Clean up when sensor is disabled/removed."""
        self.coordinator.active_commands.discard(self._command)
        _LOGGER.debug("Removed command %s from active commands", self._command)
        await super().async_will_remove_from_hass()

