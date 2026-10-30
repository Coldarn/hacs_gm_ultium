"""Sensor platform for GM Ultium."""

import logging

# from typing import Any
from collections.abc import Iterable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry
from obdii import Command, Response
from obdii import commands as veh_commands

from . import Obd2BleConfigEntry
from .const import (
    CONF_COMMANDS,
    CONF_DEVICE_CLASS,
    CONF_ICON,
    CONF_MEASUREMENT_MODE,
    CONF_STATE_CLASS,
    CONF_UNIT,
    DOMAIN,
    ICON_KEYWORDS,
)
from .coordinator import Obd2BleDataUpdateCoordinator
from .entity import ObdBleEntity

_LOGGER = logging.getLogger(__name__)


def propose_icon_from_command(command: Command) -> str:
    """Propose an mdi icon string by checking token suffixes backwards."""
    tokens = command.name.lower().split("_")
    # Iterate backwards so the modifier (like 'speed' or 'temp') wins over 'engine'
    for token in tokens[::-1]:
        if token in ICON_KEYWORDS:
            return ICON_KEYWORDS[token]
    return "mdi:car-diagnostic"


def propose_sensor_state_class(command: Command) -> SensorStateClass | None:
    """Analyze OBD2 metrics using normalized unit collections."""

    if isinstance(command.units, Iterable) and not isinstance(command.units, (str, bytes)):
        raw_units = list(command.units)
    else:
        raw_units = [command.units]
    primary_unit = raw_units[0] if raw_units else None
    tokens = command.name.lower().split("_")
    last_token = tokens[-1] if tokens else ""

    if primary_unit is None or primary_unit in ("string", "bool"):
        return None
    elif last_token in ("count", "distance", "time", "odometer"):
        return SensorStateClass.TOTAL_INCREASING
    return SensorStateClass.MEASUREMENT


def get_list_of_units(command: Command) -> list[str]:
    if isinstance(command.units, Iterable) and not isinstance(command.units, (str, bytes)):
        return list(command.units)
    elif command.units is not None:
        return [str(command.units)]
    else:
        return []


def propose_sensor_device_class(command: Command) -> SensorDeviceClass | None:
    """Analyze OBD2 metrics using normalized unit collections."""

    if isinstance(command.units, Iterable) and not isinstance(command.units, (str, bytes)):
        raw_units = list(command.units)
    else:
        raw_units = [command.units]
    primary_unit = raw_units[0] if raw_units else None
    tokens = command.name.lower().split("_")

    if primary_unit == "°C":
        return SensorDeviceClass.TEMPERATURE
    elif primary_unit in ("kPa", "bar", "psi"):
        return SensorDeviceClass.PRESSURE
    elif primary_unit in ("V", "v"):
        return SensorDeviceClass.VOLTAGE
    elif primary_unit in ("km/h", "mph"):
        return SensorDeviceClass.SPEED
    elif primary_unit in ("s", "seconds", "min", "h"):
        return SensorDeviceClass.DURATION
    elif "temp" in tokens or "temperature" in tokens:
        return SensorDeviceClass.TEMPERATURE
    elif "speed" in tokens or "velocity" in tokens or "rpm" in tokens:
        return SensorDeviceClass.SPEED
    return None


class Obd2BleSensorEntityConfig:
    def __init__(self, command: Command, name: str|None=None, icon: str|None=None, unit: str|None=None, **kwargs) -> None:
        self.command = command
        self.description = SensorEntityDescription(
            key=command.name,
            name=name or " ".join(command.name.replace("_", " ").split()).capitalize(),
            icon=icon,
            native_unit_of_measurement=unit,
            **kwargs,
        )


async def async_setup_entry(
    hass: HomeAssistant, entry: Obd2BleConfigEntry, async_add_entities
):
    """Set up sensor platform."""

    _LOGGER.debug("Configured commands %s", entry.options.get(CONF_COMMANDS))

    active_command_names: set[str] = set()
    sensor_commands: list[Obd2BleSensorEntityConfig] = []
    for command_config in entry.options.get(CONF_COMMANDS, []):
        try:
            command = veh_commands[command_config.get("command")]
        except KeyError:
            _LOGGER.error(f"Command {command_config.get('command')} not found in obdii.commands, skipping")
        else:
            active_command_names.add(command.name)
            sensor_commands.append(Obd2BleSensorEntityConfig(
                command=command,
                icon=command_config.get(CONF_ICON) or None,
                unit=command_config.get(CONF_UNIT) or None,
                device_class=command_config.get(CONF_DEVICE_CLASS) or None,
                state_class=command_config.get(CONF_STATE_CLASS) or None,
            ))

    ent_reg = entity_registry.async_get(hass)
    existing_registry_entries = entity_registry.async_entries_for_config_entry(ent_reg, entry.entry_id)
    _LOGGER.debug("Existing registry entries for this config entry: %s", existing_registry_entries)
    for registered_entity in existing_registry_entries:
        # Unique ID signature from entity.py: f"{address}-sensor-{command.name}"
        unique_id_parts = registered_entity.unique_id.split("-sensor-")
        if len(unique_id_parts) < 2:
            continue
        registered_command_name = unique_id_parts[1]
        # If the tracking metric is not present in user options anymore, purge it entirely!
        if registered_command_name not in active_command_names:
            _LOGGER.info("Evicting unselected tracking sensor: %s", registered_entity.entity_id)
            ent_reg.async_remove(registered_entity.entity_id)

    coordinator = entry.runtime_data
    entities = [
        ObdBleSensor(coordinator, entry, sensor)
        for sensor in sensor_commands
    ]
    if entry.data.get(CONF_MEASUREMENT_MODE):
        entities.append(SleepLogStatusSensor(hass, coordinator, entry))
    async_add_entities(entities)


class SleepLogStatusSensor(SensorEntity):
    """Diagnostic sensor showing the sleep/power logger state."""

    _attr_should_poll = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chart-timeline-variant"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: Obd2BleDataUpdateCoordinator,
        entry: Obd2BleConfigEntry,
    ) -> None:
        self._hass = hass
        self._attr_name = "Sleep log"
        self._attr_unique_id = f"{entry.data[CONF_ADDRESS]}-sleep-log"
        self._attr_device_info = coordinator.device_info

    def update(self) -> None:
        logger = self._hass.data.get(DOMAIN, {}).get("sleep_log")
        if logger is None:
            self._attr_native_value = "idle"
            self._attr_extra_state_attributes = {}
            return

        status = logger.status
        self._attr_native_value = status.get("state", "idle")
        self._attr_extra_state_attributes = {
            key: value for key, value in status.items() if key != "state"
        }

class ObdBleSensor(ObdBleEntity, SensorEntity):
    """Config entry for obd2_ble sensors."""

    def __init__(
        self,
        coordinator: Obd2BleDataUpdateCoordinator,
        config_entry,
        config: Obd2BleSensorEntityConfig,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, config.command, "sensor")
        self._config = config
        self.entity_description = config.description
        # self._description = config.description
        # self._attr_name = f"{NAME} {config.description.name}"
        # self._attr_device_class = config.description.device_class
        # self._attr_native_unit_of_measurement = config.description.native_unit_of_measurement
        # self._attr_state_class = config.description.state_class

    # async def async_update(self) -> None:
    def _handle_coordinator_update(self) -> None:
        try:
            data: Response | None = self.coordinator.data.get(str(self._command))
            _LOGGER.debug("Updating sensor %s with data: %s", str(self._command), data)
        except Exception as ex:
            _LOGGER.error(f"Error updating sensor {str(self._command)}: {ex}")
            self._attr_available = False
        else:
            if data is None:
                _LOGGER.warning(f"No data available for sensor {str(self._command)}")
                self._attr_available = False
            elif isinstance(data, Response):
                self._attr_available = True
                self._attr_native_value = data.value

        super()._handle_coordinator_update()


# class ObdBleDiagSensor(ObdBleEntity, SensorEntity):
#     """Config entry for obd2_ble diagnostic sensors."""

#     def __init__(
#         self,
#         coordinator: Obd2BleDataUpdateCoordinator,
#         config_entry,
#         id: str,
#         description: SensorEntityDescription,
#     ) -> None:
#         """Initialize the sensor."""
#         super().__init__(coordinator, config_entry, id, description.icon, id, DOMAIN)
#         self._id = id
#         self._description = description
#         self._attr_name = f"{NAME} {description.name}"
#         self._attr_entity_category = EntityCategory.DIAGNOSTIC
