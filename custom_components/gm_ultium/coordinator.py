"""Coordinator for GM Ultium."""

import logging
from datetime import timedelta
from typing import Any

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakOutOfConnectionSlotsError
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.api import async_address_present
from homeassistant.components.bluetooth.const import DOMAIN as BLUETOOTH_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConditionError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from obdii import Command, Connection, Protocol, Response, at_commands
from obdii import __version__ as obdii_version
from obdii import commands as veh_commands

from .const import (
    CONF_CACHED_VALUES,
    CONF_CHARACTERISTIC_UUID_READ,
    CONF_CHARACTERISTIC_UUID_WRITE,
    CONF_FAST_POLL,
    CONF_HW_VERSION,
    CONF_MEASUREMENT_MODE,
    CONF_PROTOCOL,
    CONF_SLOW_POLL,
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
from .obdii.ultium_commands import ULTIUM_COMMANDS, get_command_can_header

try:
    from .debug import FAKE_COMMANDS
except ImportError:
    FAKE_COMMANDS: list[Command] = []

_LOGGER = logging.getLogger(__name__)

type Obd2BleConfigEntry = ConfigEntry[Obd2BleDataUpdateCoordinator]


class Obd2BleDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Response]]):
    """Class to manage fetching data from the API."""

    def __init__(
        self, hass: HomeAssistant, entry: Obd2BleConfigEntry
    ) -> None:
        """Initialize."""
        measurement_mode = bool(entry.data.get(CONF_MEASUREMENT_MODE, False))
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None
            if measurement_mode
            else timedelta(seconds=entry.options.get(CONF_FAST_POLL, DEFAULT_FAST_POLL)),
            always_update=True,
            config_entry=entry,
        )
        self.measurement_mode = measurement_mode

        self._cache_data: dict[str, Response] = {}
        self._supported_pids = []
        self._supported_cmds = []
        self._last_fetch_successful = False

        self.transport: TransportBLE | None = None 
        self.api: Connection | None = None
        self.active_commands: set[Command] = set()
        self._active_can_header: str | None = None
        self._force_next_update = False

        if not entry or not entry.unique_id:
            raise ConditionError("No unique_id found in config entry")

        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id), (BLUETOOTH_DOMAIN, entry.unique_id)},
            connections={(CONNECTION_BLUETOOTH, entry.unique_id)},
            hw_version=entry.data.get(CONF_HW_VERSION),
            model_id=Protocol(entry.data.get(CONF_PROTOCOL, Protocol.AUTO.value)).name,
            sw_version=obdii_version,
        )

        # self._connect_ble()

    async def _async_connect_ble(self) -> bool:
        """Connect to the BLE device."""
        if not self.config_entry or not self.config_entry.unique_id:
            _LOGGER.error("No config entry available for coordinator")
            return False
        address = self.config_entry.unique_id

        ble_device: BLEDevice | None = bluetooth.async_ble_device_from_address(
            self.hass, address, True
        )
        if ble_device is None:
            _LOGGER.warning("Failed to discover device %s via Bluetooth", address)
            return False

        entry_data = dict(self.config_entry.data)
        self.transport = TransportBLE(
            ble_device=ble_device,
            uuid_write=entry_data.get(
                CONF_CHARACTERISTIC_UUID_WRITE, DEFAULT_CHARACTERISTIC_UUID_WRITE
            ),
            uuid_read=entry_data.get(
                CONF_CHARACTERISTIC_UUID_READ, DEFAULT_CHARACTERISTIC_UUID_READ
            ),
            loop=self.hass.loop,
        )
        self.api = Connection(
            transport=self.transport,
            auto_connect=False,
            protocol=Protocol(
                int(
                    entry_data.get(
                        CONF_PROTOCOL, Protocol.ISO_15765_4_CAN_B.value
                    )
                )
            ),
            log_handler=None,
        )

        try:
            await self.hass.async_add_executor_job(self.api.connect)
            hw_version = await self.hass.async_add_executor_job(
                self.api.query, at_commands.VERSION_ID
            )
        except Exception as err:
            self.api = None
            self.transport = None
            cause = err.__cause__
            _LOGGER.error(
                "OBD2 connect failed: %s: %r (cause=%s: %r)",
                type(err).__name__,
                err,
                type(cause).__name__ if cause else None,
                cause,
            )
            if isinstance(cause, BleakOutOfConnectionSlotsError) or (
                "connection slot" in str(err).lower()
            ):
                raise UpdateFailed(
                    "No free Bluetooth connection slot for the dongle. Close "
                    "other BLE integrations/apps or add a Bluetooth proxy."
                ) from err
            detail = (
                f"{type(cause).__name__}: {cause!r}"
                if cause
                else f"{type(err).__name__}: {err!r}"
            )
            raise UpdateFailed(f"Error connecting to OBD2: {detail}") from err

        if hw_version is not None:
            entry_data[CONF_HW_VERSION] = hw_version.value if hw_version else None
            self.device_info["hw_version"] = hw_version.value if hw_version else None
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=entry_data
            )

        self._active_can_header = None
        return True

    async def _async_set_can_header(self, header: str) -> None:
        """Set the 3-byte ELM327 CAN header for a command."""
        if self.api is None:
            return
        normalized = header.strip().upper()
        if len(normalized) != 6 or any(
            char not in "0123456789ABCDEF" for char in normalized
        ):
            raise UpdateFailed(f"Invalid CAN header {header!r}")
        if self._active_can_header == normalized:
            return

        response = await self.hass.async_add_executor_job(
            self.api.query,
            at_commands.SET_HEADER(
                xx=normalized[0:2], yy=normalized[2:4], zz=normalized[4:6]
            ),
        )
        if response.value != "OK":
            raise UpdateFailed(
                f"Adapter rejected CAN header {normalized}: {response.value!r}"
            )
        self._active_can_header = normalized

    async def async_shutdown(self) -> None:
        """Shutdown coordinator and any connection."""
        _LOGGER.debug("Shutting down BMS (%s)", self.name)
        await super().async_shutdown()
        try:
            if self.api is not None and self.api.is_connected():
                await self.hass.async_add_executor_job(self.api.close)
        except Exception as err:
            _LOGGER.warning(f"Error occurred while closing API connection: {err}")
        else:
            _LOGGER.debug("API connection closed successfully")

    async def _async_update_data(self, force: bool = False) -> dict[str, Response]:
        """Update data via library.

        ``force`` is used by the manual attempt_to_connect service: it skips the
        out-of-range backoff and tries a connection even if HA has not seen a
        recent advertisement.
        """

        if self.measurement_mode:
            return {}

        force = force or self._force_next_update
        self._force_next_update = False

        if not self.config_entry or not self.config_entry.unique_id:
            _LOGGER.error("No config entry available for coordinator")
            return {}

        # While the dongle is connected it stops advertising, so the
        # advertisement-presence check must only gate a *disconnected* link.
        if self.api is None or not self.api.is_connected():
            _LOGGER.debug("Check if the device is still available")
            available = async_address_present(
                self.hass, self.config_entry.unique_id, connectable=True
            )
            if not available and not force:
                _LOGGER.debug("Car out of range? Switch to extra slow polling")
                self.update_interval = timedelta(
                    seconds=self.config_entry.options.get(
                        CONF_XS_POLL, DEFAULT_XS_POLL
                    )
                )
                _LOGGER.debug(
                    "Car out of range? Switch to ultra slow polling: interval = %s",
                    self.update_interval,
                )
                if self.config_entry.options.get(
                    CONF_CACHED_VALUES, DEFAULT_CACHED_VALUES
                ):
                    return self._cache_data
                return {}

        if self.api is None:
            if not await self._async_connect_ble():
                raise UpdateFailed("Dongle not found in the Bluetooth cache")

        assert self.api is not None, "API should be initialized at this point"
        _LOGGER.debug("Device is available, check if connected")
        if not self.api.is_connected():
            try:
                _LOGGER.info("Device is available but not connected, attempt to connect")
                await self.hass.async_add_executor_job(self.api.connect)
                if not self.api.is_connected():
                    raise UpdateFailed("No connection to OBD2 after connect attempt")
            except Exception as err:
                raise UpdateFailed(f"Error connecting with OBD2: {err}")

        _LOGGER.debug("Device is connected, proceed to query data")
        try:
            self._last_fetch_successful = False
            new_data: dict[str, Response] = {}
            for command in self.active_commands:
                if command is None:
                    _LOGGER.warning("Skipping invalid command: %s", command)
                    continue
                try:
                    header = get_command_can_header(command)
                    if header:
                        await self._async_set_can_header(header)
                    _LOGGER.debug("Querying OBD2 for command %s", command)
                    response: Response = await self.hass.async_add_executor_job(self.api.query, command)
                    _LOGGER.debug("Received response for command %s: %s", command, response)
                    if response is not None and response.value is not None:
                        new_data[str(command)] = response
                    else:
                        _LOGGER.warning("Received empty response for command %s", command)
                except Exception as err:
                    _LOGGER.error(f"Error occurred while querying command {command}: {err}")
            if len(new_data) == 0:
                self.update_interval = timedelta(seconds=self.config_entry.options.get(CONF_SLOW_POLL, DEFAULT_SLOW_POLL))
                _LOGGER.debug(
                    "Car is probably off, switch to slow polling: interval = %s",
                    self.update_interval,
                )
            else:
                self._last_fetch_successful = True
                self.update_interval = timedelta(seconds=self.config_entry.options.get(CONF_FAST_POLL, DEFAULT_FAST_POLL))
                _LOGGER.debug(
                    "Car is on, polling: interval = %s",
                    self.update_interval,
                )
        except Exception as err:
            raise UpdateFailed(f"Unable to fetch data: {err}") from err
        else:
            if self.config_entry.options.get(CONF_CACHED_VALUES, DEFAULT_CACHED_VALUES):
                self._cache_data.update(new_data)
                return self._cache_data
            return new_data

    async def async_get_all_pid_commands(self, force_refresh=False) -> tuple[list[Any], list[Any]]:
        if self._supported_pids and self._supported_cmds and not force_refresh:
            return self._supported_pids, self._supported_cmds

        if self.api is None or not self.api.is_connected():
            # The options flow can still offer the curated Ultium commands
            # without a live connection.
            return [], list(ULTIUM_COMMANDS)
        
        self._supported_pids = []
        self._supported_cmds = FAKE_COMMANDS.copy()
        for cmd in range(0x00, 0xE0, 0x20):
            try:
                response: Response = await self.hass.async_add_executor_job(self.api.query, veh_commands[1][cmd])
                if isinstance(response.value, list):
                    self._supported_pids.extend(response.value)
                    for pid in response.value:
                        try:
                            self._supported_cmds.append(veh_commands[1][pid])
                        except KeyError:
                            _LOGGER.warning(f"PID {pid} is supported but no command found in library")
            except Exception:
                _LOGGER.warning(f"Failed to query supported PIDs for command {veh_commands[1][cmd]}")

        self._supported_cmds = list({*self._supported_cmds, *ULTIUM_COMMANDS})
        _LOGGER.info(f"Supported PIDs: {self._supported_pids}")
        _LOGGER.info(f"Supported Commands: {self._supported_cmds}")

        return self._supported_pids, self._supported_cmds
    
    def ble_found(self) -> bool:
        if not self.config_entry or not self.config_entry.unique_id:
            return False
        address = self.config_entry.unique_id
        ble_device: BLEDevice | None = bluetooth.async_ble_device_from_address(
            self.hass, address, True
        )
        return ble_device is not None

    def ble_connected(self) -> bool:
        return self.api.is_connected() if self.api else False

    def car_connected(self) -> bool:
        return self._last_fetch_successful and self.ble_connected()

    async def async_force_update(self) -> bool:
        """Force a real refresh, update entities, and return the outcome."""
        self._force_next_update = True
        try:
            await self.async_refresh()
            if self.last_update_success and self.car_connected():
                _LOGGER.info("Successfully connected to the car during forced update")
                return True
            if self.last_exception:
                _LOGGER.error("Forced update failed: %r", self.last_exception)
        except Exception as err:
            _LOGGER.error("Forced update failed: %r", err)
        return False
