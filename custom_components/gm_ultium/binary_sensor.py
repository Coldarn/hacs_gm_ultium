"""Binary sensor platform for GM Ultium."""

import logging
from collections.abc import Callable

# from typing import Any
# from collections.abc import Iterable
# from obdii import Command, Response, commands as veh_commands
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

# from homeassistant.helpers import entity_registry
from . import Obd2BleConfigEntry
from .const import (
    CONF_COMMAND,
    # CONF_ICON,
    # CONF_UNIT,
    # CONF_DEVICE_CLASS,
    # CONF_STATE_CLASS,
    CONF_COMMANDS,
)
from .coordinator import Obd2BleDataUpdateCoordinator
from .obdii.ultium_commands import ULTIUM_COMMANDS_BY_NAME

# from .entity import ObdBleEntity

_LOGGER = logging.getLogger(__name__)


# class Obd2BleBinarySensorEntityConfig:
#     def __init__(self, command: Command, name: str|None=None, icon: str|None=None, unit: str|None=None, **kwargs) -> None:
#         self.command = command
#         self.description = BinarySensorEntityDescription(
#             key=command.name,
#             name=name or " ".join(command.name.replace("_", " ").split()).capitalize(),
#             icon=icon,
#             **kwargs,
#         )


class Obd2BleStatusBinarySensorEntityConfig:
    def __init__(self, function: Callable[[], bool], icon, name: str|None=None, **kwargs) -> None:
        self.function = function
        self.description = BinarySensorEntityDescription(
            key=function.__name__,
            name=name or " ".join(function.__name__.replace("_", " ").split()).capitalize(),
            icon=icon,
            entity_category=EntityCategory.DIAGNOSTIC,
            **kwargs,
        )


async def async_setup_entry(
    hass: HomeAssistant, entry: Obd2BleConfigEntry, async_add_entities
):
    """Set up sensor platform."""

    _LOGGER.debug("Configured commands %s", entry.options.get(CONF_COMMANDS))

    # active_command_names: set[str] = set()
    # binary_sensor_commands: list[Obd2BleBinarySensorEntityConfig] = []
    # for command_config in entry.options.get(CONF_COMMANDS, []):
    #     try:
    #         command = veh_commands[command_config.get("command")]
    #     except KeyError:
    #         _LOGGER.error(f"Command {command_config.get('command')} not found in obdii.commands, skipping")
    #     else:
    #         active_command_names.add(command.name)
    #         binary_sensor_commands.append(Obd2BleBinarySensorEntityConfig(
    #             command=command,
    #             icon=command_config.get(CONF_ICON) or None,
    #             unit=command_config.get(CONF_UNIT) or None,
    #             device_class=command_config.get(CONF_DEVICE_CLASS) or None,
    #             state_class=command_config.get(CONF_STATE_CLASS) or None,
    #         ))

    # ent_reg = entity_registry.async_get(hass)
    # existing_registry_entries = entity_registry.async_entries_for_config_entry(ent_reg, entry.entry_id)
    # _LOGGER.debug("Existing registry entries for this config entry: %s", existing_registry_entries)
    # for registered_entity in existing_registry_entries:
    #     # Unique ID signature from entity.py: f"{address}-sensor-{command.name}"
    #     unique_id_parts = registered_entity.unique_id.split("-sensor-")
    #     if len(unique_id_parts) < 2:
    #         continue
    #     registered_command_name = unique_id_parts[1]
    #     # If the tracking metric is not present in user options anymore, purge it entirely!
    #     if registered_command_name not in active_command_names:
    #         _LOGGER.info("Evicting unselected tracking sensor: %s", registered_entity.entity_id)
    #         ent_reg.async_remove(registered_entity.entity_id)

    active_names = {
        command_config.get(CONF_COMMAND)
        for command_config in entry.options.get(CONF_COMMANDS, [])
    }
    coordinator = entry.runtime_data
    # entities: list[ObdBleBinarySensor | ObdBleStatusBinarySensor] = []
    entities: list[ObdBleStatusBinarySensor] = []

    # entities.extend([
    #     ObdBleBinarySensor(coordinator, entry, sensor)
    #     for sensor in binary_sensor_commands
    # ])

    entities.append(ObdBleStatusBinarySensor(
        coordinator,
        entry,
        Obd2BleStatusBinarySensorEntityConfig(
            function=coordinator.ble_connected,
            name="BLE Connected",
            icon="mdi:bluetooth",
            device_class=BinarySensorDeviceClass.CONNECTIVITY,
        )
    ))
    entities.append(ObdBleStatusBinarySensor(
        coordinator,
        entry,
        Obd2BleStatusBinarySensorEntityConfig(
            function=coordinator.car_connected,
            icon="mdi:car",
            device_class=BinarySensorDeviceClass.CONNECTIVITY,
        )
    ))

    if {"PACK_VOLTAGE", "PACK_CURRENT"} <= active_names:
        entities.append(ChargingBinarySensor(coordinator, entry))

    async_add_entities(entities)

# class ObdBleBinarySensor(ObdBleEntity, BinarySensorEntity):
#     """Config entry for obd2_ble binary sensors."""

#     def __init__(
#         self,
#         coordinator: Obd2BleDataUpdateCoordinator,
#         config_entry,
#         config: Obd2BleBinarySensorEntityConfig,
#     ) -> None:
#         """Initialize the sensor."""
#         super().__init__(coordinator, config_entry, config.command, "sensor")
#         self._config = config
#         self.entity_description = config.description
#         # self._description = config.description
#         # self._attr_name = f"{NAME} {config.description.name}"
#         # self._attr_device_class = config.description.device_class
#         # self._attr_native_unit_of_measurement = config.description.native_unit_of_measurement
#         # self._attr_state_class = config.description.state_class

#     # async def async_update(self) -> None:
#     def _handle_coordinator_update(self) -> None:
#         try:
#             data: Response | None = self.coordinator.data.get(str(self._command))
#             _LOGGER.debug("Updating sensor %s with data: %s", str(self._command), data)
#         except Exception as ex:
#             _LOGGER.error(f"Error updating sensor {str(self._command)}: {ex}")
#             self._attr_available = False
#         else:
#             if data is None:
#                 _LOGGER.warning(f"No data available for sensor {str(self._command)}")
#                 self._attr_available = False
#             elif isinstance(data, Response):
#                 self._attr_available = True
#                 self._attr_is_on = data.value

#         super()._handle_coordinator_update()

class ChargingBinarySensor(
    CoordinatorEntity[Obd2BleDataUpdateCoordinator], BinarySensorEntity
):
    """Charging state derived from pack voltage and current."""

    _attr_name = "Charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_icon = "mdi:ev-station"

    def __init__(
        self,
        coordinator: Obd2BleDataUpdateCoordinator,
        entry: Obd2BleConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_device_info = coordinator.device_info
        self._attr_unique_id = f"{entry.data[CONF_ADDRESS]}-charging"

    def _value(self, name: str):
        command = ULTIUM_COMMANDS_BY_NAME[name]
        response = (self.coordinator.data or {}).get(str(command))
        return response.value if response is not None else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Poll the definitive BEC charge-state byte even if the user did not
        # select it as a sensor in the options.
        self.coordinator.active_commands.add(
            ULTIUM_COMMANDS_BY_NAME["CHARGE_STATE"]
        )

    def _handle_coordinator_update(self) -> None:
        charge_state = self._value("CHARGE_STATE")
        voltage = self._value("PACK_VOLTAGE")
        current = self._value("PACK_CURRENT")
        if charge_state is not None:
            self._attr_is_on = charge_state == "charging"
            self._attr_available = True
        elif voltage is not None and current is not None:
            power = -(voltage * current) / 1000.0
            self._attr_is_on = power > 0.1 or current < -0.5
            self._attr_available = True
        else:
            self._attr_is_on = None
            self._attr_available = False
        super()._handle_coordinator_update()


class ObdBleStatusBinarySensor(CoordinatorEntity[Obd2BleDataUpdateCoordinator], BinarySensorEntity):
    """Config entry for obd2_ble binary sensors."""

    def __init__(
        self,
        coordinator: Obd2BleDataUpdateCoordinator,
        config_entry,
        config: Obd2BleStatusBinarySensorEntityConfig,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_device_info = coordinator.device_info
        self._attr_unique_id = f"{config_entry.data[CONF_ADDRESS]}-binary_sensor-{config.function.__name__}"
        self._config = config
        self.entity_description = config.description
        self.entitity_category = "diagnostic"

    # async def async_update(self) -> None:
    def _handle_coordinator_update(self) -> None:
        try:
            self._attr_is_on = self._config.function()
            _LOGGER.debug("Updating sensor %s with data: %s", str(self._config.function.__name__), self._attr_is_on)
        except Exception as ex:  # noqa: BLE001
            _LOGGER.error(f"Error updating sensor {self._config.function.__name__!s}: {ex!s}")
            self._attr_available = False
        else:
            self._attr_available = True
        super()._handle_coordinator_update()
