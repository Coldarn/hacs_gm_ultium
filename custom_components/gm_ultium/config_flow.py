"""Adds config flow for GM Ultium."""

import logging
import re
from typing import Any

try:
    from bluetooth_data_tools import human_readable_name  # type: ignore
except ImportError:  # pragma: no cover - fallback for missing dependency
    def human_readable_name(_manufacturer: str | None, name: str | None, address: str):
        """Fallback if bluetooth_data_tools is unavailable."""
        return name or address
from dataclasses import dataclass

import voluptuous as vol
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakOutOfConnectionSlotsError
from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers import device_registry, selector
from obdii import Command
from obdii import commands as veh_commands
from obdii.protocol import Protocol

from . import Obd2BleConfigEntry
from .const import (
    CONF_AUTO_CONFIGURE,
    CONF_CACHED_VALUES,
    CONF_CHARACTERISTIC_UUID_READ,
    CONF_CHARACTERISTIC_UUID_WRITE,
    CONF_COMMAND,
    CONF_COMMANDS,
    CONF_DEVICE_CLASS,
    CONF_FAST_POLL,
    CONF_ICON,
    CONF_MEASUREMENT_MODE,
    CONF_PROTOCOL,
    CONF_SLOW_POLL,
    CONF_STATE_CLASS,
    CONF_UNIT,
    CONF_XS_POLL,
    DEFAULT_CACHED_VALUES,
    DEFAULT_CHARACTERISTIC_UUID_READ,
    DEFAULT_CHARACTERISTIC_UUID_WRITE,
    DEFAULT_FAST_POLL,
    DEFAULT_SLOW_POLL,
    DEFAULT_XS_POLL,
    DOMAIN,
)
from .obdii.transport_ble import TransportBLE
from .obdii.transport_ble_identifiers import (
    AVAILABLE_OBD2_CLASSES,
    BaseOBD2,
    advertisement_matches,
)
from .sensor import (
    get_list_of_units,
    propose_icon_from_command,
    propose_sensor_device_class,
    propose_sensor_state_class,
)

_LOGGER = logging.getLogger(__name__)

# OptionsFlowWithReload was added in a later Home Assistant release. Fall back
# to the standard OptionsFlow on older installs so the integration still loads.
_OPTIONS_FLOW_BASE = getattr(
    config_entries, "OptionsFlowWithReload", config_entries.OptionsFlow
)


@dataclass
class DiscoveredDevice:
    """A discovered Bluetooth device."""

    name: str
    discovery_info: BluetoothServiceInfoBleak
    type: str

    def model(self) -> str:
        """Return BMS type in capital letters, e.g. 'DUMMY OBDII'."""
        return self.type.rsplit(".", 1)[1].replace("_", " ").upper()


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow handler."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        """Initialize."""
        self._errors = {}
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        # self._discovered_devices: dict[str, DiscoveredDevice] = {}
        self._characteristic_uuid_read: str = DEFAULT_CHARACTERISTIC_UUID_READ
        self._characteristic_uuid_write: str = DEFAULT_CHARACTERISTIC_UUID_WRITE
        self._protocol: int = Protocol.AUTO.value
        self._transport: TransportBLE | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: Obd2BleConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow."""
        return Obd2BleOptionsFlowHandler()

    async def _async_device_supported(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> type[BaseOBD2] | None:
        """Check if device is supported by an available OBD2 BLE class."""
        for obd2_class in AVAILABLE_OBD2_CLASSES:
            if all(advertisement_matches(matcher, discovery_info.advertisement, discovery_info.address) for matcher in obd2_class.matcher_dict_list()):
                _LOGGER.debug(
                    "Device %s (%s) detected as '%s'",
                    discovery_info.name,
                    device_registry.format_mac(discovery_info.address),
                    # obd2_class.obd2_id(),
                    obd2_class.__name__,
                )
                return obd2_class
        return None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> config_entries.ConfigFlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        # if not (obd2_class := await self._async_device_supported(discovery_info)):
        if not (await self._async_device_supported(discovery_info)):
            return self.async_abort(reason="not_supported")

        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": human_readable_name(
                None, discovery_info.name, discovery_info.address
            )
        }
        return await self.async_step_user()

    def _user_schema(self) -> vol.Schema:
        """Build the discovered-device form schema."""
        return vol.Schema(
            {
                vol.Required(CONF_ADDRESS): vol.In(
                    {
                        service_info.address: f"{service_info.name} ({service_info.address})"
                        for service_info in self._discovered_devices.values()
                    }
                ),
                vol.Required(
                    CONF_AUTO_CONFIGURE, default=True
                ): bool,
                vol.Optional(CONF_MEASUREMENT_MODE, default=True): bool,
            }
        )

    async def async_step_user(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Handle the user step to pick discovered device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._discovery_info = self._discovered_devices[user_input[CONF_ADDRESS]]
            await self.async_set_unique_id(
                self._discovery_info.address, raise_on_progress=False
            )
            self._abort_if_unique_id_configured()

            if user_input.get(CONF_MEASUREMENT_MODE):
                assert self._discovery_info is not None
                return self.async_create_entry(
                    title=f"{self._discovery_info.name} (measurement)",
                    data={
                        CONF_ADDRESS: self._discovery_info.address,
                        CONF_MEASUREMENT_MODE: True,
                    },
                )

            if user_input.get(CONF_AUTO_CONFIGURE, True):
                if obdii_dev := await self._async_device_supported(self._discovery_info):
                    _LOGGER.debug("Auto-configuring device %s using class %s", self._discovery_info.name, obdii_dev.__name__)
                    self._characteristic_uuid_read = obdii_dev.uuid_rx()
                    self._characteristic_uuid_write = obdii_dev.uuid_tx()
                else:
                    _LOGGER.warning("Device %s does not match any known OBD2 classes, auto-configuration may fail", self._discovery_info.name)

            try:
                ble_device: BLEDevice | None = async_ble_device_from_address(
                    self.hass, self._discovery_info.address, True
                )
                if ble_device is None:
                    errors["base"] = "cannot_connect"
                    return self.async_show_form(
                        step_id="user",
                        data_schema=self._user_schema(),
                        errors=errors,
                    )

                self._transport = TransportBLE(
                    ble_device=ble_device,
                    uuid_write=self._characteristic_uuid_write,
                    uuid_read=self._characteristic_uuid_read,
                    loop=self.hass.loop,
                )
                await self._transport.async_connect()
            except BleakOutOfConnectionSlotsError:
                if self._transport and self._transport.is_connected():
                    await self._transport.async_close()
                self._transport = None
                _LOGGER.warning(
                    "No free Bluetooth connection slot for %s. Close other "
                    "BLE integrations/apps or add a Bluetooth proxy.",
                    self._discovery_info.address,
                )
                errors["base"] = "out_of_connection_slots"
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._user_schema(),
                    errors=errors,
                )
            except Exception:
                if self._transport and self._transport.is_connected():
                    await self._transport.async_close()
                self._transport = None
                _LOGGER.exception(
                    "Failed to connect to %s", self._discovery_info.address
                )
                errors["base"] = "cannot_connect"
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._user_schema(),
                    errors=errors,
                )

            return await self.async_step_connection()

        if discovery := self._discovery_info:
            self._discovered_devices[discovery.address] = discovery
        else:
            current_addresses = self._async_current_ids()
            for discovery in async_discovered_service_info(self.hass):
                if (
                    discovery.address in current_addresses
                    or discovery.address in self._discovered_devices
                    or not (await self._async_device_supported(discovery))
                ):
                    continue
                self._discovered_devices[discovery.address] = discovery

        if not self._discovered_devices:
            cached = list(async_discovered_service_info(self.hass))
            _LOGGER.warning(
                "No supported GM Ultium BLE device found. "
                "Home Assistant Bluetooth cache contains %d device(s).",
                len(cached),
            )
            _LOGGER.debug(
                "Bluetooth cache: %s",
                [
                    (device.name, device.address, device.advertisement.service_uuids)
                    for device in cached
                ],
            )
            return await self.async_step_manual()

        return self.async_show_form(
            step_id="user",
            data_schema=self._user_schema(),
            errors=errors,
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manual address fallback when Bluetooth discovery finds nothing."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper()
            if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", address):
                errors["base"] = "invalid_address"
            else:
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"GM Ultium ({address})",
                    data={
                        CONF_ADDRESS: address,
                        CONF_MEASUREMENT_MODE: True,
                    },
                )

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): str}),
            errors=errors,
        )

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage the options."""
        # if not self.options:
        #     self.options = dict(self.config_entry.options)

        if user_input is not None:
            self._characteristic_uuid_read = user_input[CONF_CHARACTERISTIC_UUID_READ]
            self._characteristic_uuid_write = user_input[CONF_CHARACTERISTIC_UUID_WRITE]
            self._protocol = int(user_input[CONF_PROTOCOL])
            if self._transport is not None and self._transport.is_connected():
                await self._transport.async_close()

            if self.source == config_entries.SOURCE_RECONFIGURE:
                reconfigure_entry = self._get_reconfigure_entry()
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data={
                        **reconfigure_entry.data,
                        CONF_CHARACTERISTIC_UUID_READ: self._characteristic_uuid_read,
                        CONF_CHARACTERISTIC_UUID_WRITE: self._characteristic_uuid_write,
                        CONF_PROTOCOL: self._protocol,
                    },
                )

            assert self._discovery_info is not None, "Discovery info should have been set by now"
            return self.async_create_entry(
                title= self._discovery_info.name,
                data={
                    CONF_ADDRESS: self._discovery_info.address,
                    CONF_CHARACTERISTIC_UUID_READ: self._characteristic_uuid_read,
                    CONF_CHARACTERISTIC_UUID_WRITE: self._characteristic_uuid_write,
                    CONF_PROTOCOL: self._protocol,
                },
            )

        assert self._transport is not None and self._transport.is_connected(), "Transport should have been initialized and connected by now"
        characteristics: list[BleakGATTCharacteristic] = []
        for service in self._transport.get_service_collection():
            characteristics.extend(service.characteristics)
        if not characteristics:
            raise ValueError("No characteristics found")

        return self.async_show_form(
            step_id="connection",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CHARACTERISTIC_UUID_READ, default=self._characteristic_uuid_read): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {
                                    "value": characteristic.uuid,
                                    "label": f"{characteristic.description} {characteristic.uuid.split('-')[0]}"
                                } for characteristic in characteristics],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            translation_key="ble_read_characteristics",
                        )
                    ),
                    vol.Required(CONF_CHARACTERISTIC_UUID_WRITE, default=self._characteristic_uuid_write): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {
                                    "value": characteristic.uuid,
                                    "label": f"{characteristic.description} {characteristic.uuid.split('-')[0]}"
                                } for characteristic in characteristics],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            translation_key="ble_write_characteristics",
                        )
                    ),
                    vol.Required(CONF_PROTOCOL, default=str(self._protocol)): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {
                                    "value": str(protocol.value),
                                    "label": f"{protocol.name} ({protocol.value})"
                                } for protocol in Protocol if protocol != Protocol.UNKNOWN],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            translation_key="obdii_protocol",
                        ),
                    ),
                }
            )
        )

    async def async_step_reconfigure(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        self._transport = self._get_reconfigure_entry().runtime_data.transport
        assert self._transport is not None, "Transport should have been initialized by now"
        if not self._transport.is_connected():
            await self._transport.async_connect()
        self._characteristic_uuid_read = self._get_reconfigure_entry().data.get(CONF_CHARACTERISTIC_UUID_READ, DEFAULT_CHARACTERISTIC_UUID_READ)
        self._characteristic_uuid_write = self._get_reconfigure_entry().data.get(CONF_CHARACTERISTIC_UUID_WRITE, DEFAULT_CHARACTERISTIC_UUID_WRITE)
        self._protocol = self._get_reconfigure_entry().data.get(CONF_PROTOCOL, Protocol.AUTO.value)
        return await self.async_step_connection(user_input)

    @callback
    def async_remove(self) -> None:
        """Handle flow removal/cancellation."""
        if self._transport and self._transport.is_connected():
            _LOGGER.debug("Config flow cancelled/removed. Forcing BLE disconnect.")
            self._transport.close()
        super().async_remove()

class Obd2BleOptionsFlowHandler(_OPTIONS_FLOW_BASE):
    """Config flow options handler for obd2_ble."""

    def __init__(self) -> None:
        """Initialize options flow."""
        self._options: dict = {}
        self._selected_commands: list[Command] = []
        self._configured_commands: list[dict[str, str | None]] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """First step: Display the built-in option menu selection."""
        if not self._options:
            self._options = dict(self.config_entry.options)

        return self.async_show_menu(
            step_id="init",
            menu_options=["polling", "commands_select"],
        )

    async def async_step_polling(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle polling interval setup options form."""

        if user_input is not None:
            self._options.update(user_input)
            return self.async_create_entry(
                title=self.config_entry.data.get(CONF_ADDRESS),
                data=self._options,
            )

        return self.async_show_form(
            step_id="polling",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CACHED_VALUES, default=self._options.get(CONF_CACHED_VALUES, DEFAULT_CACHED_VALUES)
                    ): bool,
                    vol.Required(
                        CONF_FAST_POLL, default=self._options.get(CONF_FAST_POLL, DEFAULT_FAST_POLL)
                    ): int,
                    vol.Required(
                        CONF_SLOW_POLL, default=self._options.get(CONF_SLOW_POLL, DEFAULT_SLOW_POLL)
                    ): int,
                    vol.Required(
                        CONF_XS_POLL, default=self._options.get(CONF_XS_POLL, DEFAULT_XS_POLL)
                    ): int,
                }
            ),
        )

    async def async_step_commands_select(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle choosing which commands should be loaded."""

        if user_input is not None:
            if len(user_input[CONF_COMMANDS]) == 0:
                self._options[CONF_COMMANDS] = []
                return self.async_create_entry(
                    title=self.config_entry.data.get(CONF_ADDRESS),
                    data=self._options,
                )

            # Save the list of target objects we want to configure sequentially
            self._selected_commands = [veh_commands[cmd] for cmd in user_input[CONF_COMMANDS]]
            self._configured_commands = [] # Reset our configuration queue storage
            return await self.async_step_commands_config()

        _, commands = await self.config_entry.runtime_data.async_get_all_pid_commands(force_refresh=True)
        if pre_configured := self._options.get(CONF_COMMANDS):
            commands = list(set(commands) | {veh_commands[cmd[CONF_COMMAND]] for cmd in pre_configured})
        commands = sorted(commands, key=lambda cmd: (cmd.name, cmd.mode, cmd.pid))

        return self.async_show_form(
            step_id="commands_select",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_COMMANDS, default=[cmd[CONF_COMMAND] for cmd in self._options.get(CONF_COMMANDS, [])]): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {
                                    "value": command.name,
                                    "label": f"{command.name} ({command.mode} {command.pid})"
                                } for command in commands],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            translation_key="commands",
                            multiple=True,
                        )
                    ),
                }
            ),
        )

    async def async_step_commands_config(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:

        if user_input is not None:
            self._configured_commands.append(
                {
                    CONF_COMMAND: self._command.name,
                    CONF_ICON: user_input.get(CONF_ICON),
                    CONF_UNIT: user_input.get(CONF_UNIT),
                    CONF_DEVICE_CLASS: user_input.get(CONF_DEVICE_CLASS),
                    CONF_STATE_CLASS: user_input.get(CONF_STATE_CLASS),
                }
            )

            if len(self._selected_commands) == 0:
                self._options[CONF_COMMANDS] = self._configured_commands
                return self.async_create_entry(
                    title=self.config_entry.data.get(CONF_ADDRESS),
                    data=self._options,
            )

        assert len(self._selected_commands) != 0, "Should not have gotten to here if no commands are selected"
        self._command = self._selected_commands.pop(0)
        previous_config = next((cmd_config for cmd_config in self._options.get(CONF_COMMANDS, []) if cmd_config[CONF_COMMAND] == self._command.name), None)

        return self.async_show_form(
            step_id="commands_config",
            description_placeholders={
                "command_name": " ".join(self._command.name.replace("_", " ").split()).capitalize()
            },
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ICON,
                        default=previous_config.get(CONF_ICON) if previous_config
                                else propose_icon_from_command(self._command),
                    ): selector.IconSelector(),
                    vol.Optional(
                        CONF_UNIT,
                        default=previous_config.get(CONF_UNIT) if previous_config 
                                else get_list_of_units(self._command)[0] if get_list_of_units(self._command)
                                else None,
                    ): vol.Any(None, selector.TextSelector()),
                    vol.Optional(
                        CONF_DEVICE_CLASS,
                        default=previous_config.get(CONF_DEVICE_CLASS) if previous_config
                                else propose_sensor_device_class(self._command)
                                or None,
                    ): vol.Any(None, selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {
                                    "value": dev_cls.value,
                                    "label": f"{dev_cls.name.replace('_', ' ').title()}"
                                } for dev_cls in SensorDeviceClass],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        ),
                    )),
                    vol.Optional(
                        CONF_STATE_CLASS,
                        default=previous_config.get(CONF_STATE_CLASS) if previous_config
                                else propose_sensor_state_class(self._command)
                                or None,
                    ): vol.Any(None, selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {
                                    "value": state_cls.value,
                                    "label": f"{state_cls.name.replace('_', ' ').title()}"
                                } for state_cls in SensorStateClass],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        ),
                    )),
                }
            ),
        )