import asyncio
import logging

from bleak import BleakClient
from bleak.exc import BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTServiceCollection
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache

from threading import Lock, Event
from time import monotonic
from typing import Optional, Dict, Any, Coroutine

from obdii.transports.transport_base import TransportBase
from obdii.basetypes import MISSING

_LOGGER: logging.Logger = logging.getLogger(__package__)

class TransportBLE(TransportBase):
    def __init__(
        self,
        ble_device: BLEDevice = MISSING,
        uuid_write: str = MISSING,
        uuid_read: str = MISSING,
        timeout: float = 10.0,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        **kwargs,
    ) -> None:
        if ble_device is MISSING or uuid_write is MISSING or uuid_read is MISSING:
            raise ValueError(
                "ble_device (%s), uuid_write (%s) and uuid_read (%s) must be specified for TransportBLE.",
                ble_device, uuid_write, uuid_read
            )

        self.config: Dict[str, Any] = {
            "uuid_write": uuid_write,
            "uuid_read": uuid_read,
            "timeout": timeout,
            **kwargs,
        }

        self._ble_device = ble_device
        self._ble_conn: Optional[BleakClient] = None
        self._buffer = bytearray()
        self._lock = Lock()
        self._data_ready = Event()
        self._loop = loop
        self._rx_char: Optional[BleakGATTCharacteristic] = None
        self._tx_char: Optional[BleakGATTCharacteristic] = None

    def __repr__(self) -> str:
        return f"<TransportBLE {self._ble_device}>"

    def _run_coro(self, coro: Coroutine) -> Any:
        if self._loop is None:
            raise RuntimeError("Event loop is not running.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=self.config["timeout"])
    
    def _notify_callback(self, _, data: bytearray) -> None:
        with self._lock:
            self._buffer.extend(data)
        self._data_ready.set()

    def _service_uuid_for_char(self, characteristic: BleakGATTCharacteristic) -> str:
        service_uuid = getattr(characteristic, "service_uuid", None)
        if isinstance(service_uuid, str):
            return service_uuid.lower()
        service = getattr(characteristic, "service", None)
        if service is not None and isinstance(getattr(service, "uuid", None), str):
            return service.uuid.lower()
        return ""

    def _find_characteristics(self, uuid: str) -> list[BleakGATTCharacteristic]:
        if self._ble_conn is None:
            raise RuntimeError("BLE connection is not established.")
        uuid_l = uuid.lower()
        matches: list[BleakGATTCharacteristic] = []
        for service in self._ble_conn.services:
            for characteristic in service.characteristics:
                if characteristic.uuid.lower() == uuid_l:
                    matches.append(characteristic)
        return matches

    def _filter_by_properties(
        self,
        candidates: list[BleakGATTCharacteristic],
        required: tuple[str, ...],
    ) -> list[BleakGATTCharacteristic]:
        filtered = [
            characteristic
            for characteristic in candidates
            if any(prop in characteristic.properties for prop in required)
        ]
        return filtered or candidates

    def _resolve_io_characteristics(self) -> tuple[BleakGATTCharacteristic, BleakGATTCharacteristic]:
        rx_candidates = self._filter_by_properties(
            self._find_characteristics(self.config["uuid_read"]),
            ("notify", "indicate", "read"),
        )
        tx_candidates = self._filter_by_properties(
            self._find_characteristics(self.config["uuid_write"]),
            ("write", "write-without-response"),
        )

        if not rx_candidates:
            raise BleakError(f"No characteristic found for RX UUID {self.config['uuid_read']}")
        if not tx_candidates:
            raise BleakError(f"No characteristic found for TX UUID {self.config['uuid_write']}")

        preferred_service_uuid = str(self.config.get("service_uuid", "")).lower()
        if preferred_service_uuid:
            preferred_rx = [
                characteristic
                for characteristic in rx_candidates
                if self._service_uuid_for_char(characteristic) == preferred_service_uuid
            ]
            preferred_tx = [
                characteristic
                for characteristic in tx_candidates
                if self._service_uuid_for_char(characteristic) == preferred_service_uuid
            ]
            if preferred_rx and preferred_tx:
                return preferred_rx[0], preferred_tx[0]

        rx_by_service: Dict[str, list[BleakGATTCharacteristic]] = {}
        for characteristic in rx_candidates:
            rx_by_service.setdefault(self._service_uuid_for_char(characteristic), []).append(characteristic)
        tx_by_service: Dict[str, list[BleakGATTCharacteristic]] = {}
        for characteristic in tx_candidates:
            tx_by_service.setdefault(self._service_uuid_for_char(characteristic), []).append(characteristic)

        common_services = [service_uuid for service_uuid in rx_by_service if service_uuid in tx_by_service]
        if common_services:
            # Prefer the characteristic pair that shares the same service UUID.
            service_uuid = sorted(common_services)[0]
            return rx_by_service[service_uuid][0], tx_by_service[service_uuid][0]

        return rx_candidates[0], tx_candidates[0]

    async def async_connect(self) -> None:
        _LOGGER.debug("Attempting to connect to BLE device %s (%s)", self._ble_device.name, self._ble_device.address)
        self._ble_conn = await establish_connection(
            BleakClientWithServiceCache,
            self._ble_device,
            self._ble_device.name or "Unknown Device",
            max_attempts=3
        )
        self._rx_char, self._tx_char = self._resolve_io_characteristics()
        _LOGGER.debug(
            "Resolved BLE characteristics for %s: RX=%s (handle=%s, service=%s), TX=%s (handle=%s, service=%s)",
            self._ble_device.address,
            self._rx_char.uuid,
            self._rx_char.handle,
            self._service_uuid_for_char(self._rx_char),
            self._tx_char.uuid,
            self._tx_char.handle,
            self._service_uuid_for_char(self._tx_char),
        )
        await self._ble_conn.start_notify(self._rx_char, self._notify_callback)

    async def async_close(self) -> None:
        if self._ble_conn and self._ble_conn.is_connected:
            if self._rx_char is not None:
                await self._ble_conn.stop_notify(self._rx_char)
            await self._ble_conn.disconnect()
        self._ble_conn = None
        self._rx_char = None
        self._tx_char = None

    async def _write(self, query: bytes) -> None:
        if self._ble_conn is None:
            raise RuntimeError("BLE connection is not established.")
        await self._ble_conn.write_gatt_char(self._tx_char or self.config["uuid_write"], query)

    def get_service_collection(self) -> BleakGATTServiceCollection:
        if self._ble_conn is None:
            raise RuntimeError("BLE connection is not established.")
        return self._ble_conn.services

    def connect(self, loop: Optional[asyncio.AbstractEventLoop] = None, **kwargs) -> None:
        self.config.update(kwargs)

        if loop is not None:
            self._loop = loop

        try:
            self._run_coro(self.async_connect())
        except Exception:
            self.close() # Cleanup on failure
            raise

    def close(self) -> None:
        if self.is_connected():
            try:
                self._run_coro(self.async_close())
            except Exception:
                pass # Already disconnecting or loop is dead

    def is_connected(self) -> bool:
        if self._ble_conn is None:
            return False
        return self._ble_conn.is_connected

    def write_bytes(self, query: bytes) -> None:
        if not self.is_connected():
            raise RuntimeError("BLE is not connected.")
        with self._lock:
            self._buffer.clear()
        self._data_ready.clear()
        self._run_coro(self._write(query))

    def read_bytes(self, expected_seq: bytes = b'>', size: int = MISSING) -> bytes:
        lenterm = len(expected_seq)
        deadline = monotonic() + self.config["timeout"]

        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("read timed out.")

            with self._lock:
                snapshot = bytes(self._buffer)

            if snapshot[-lenterm:] == expected_seq:
                break
            if size is not MISSING and len(snapshot) >= size:
                break

            self._data_ready.wait(timeout=remaining)
            self._data_ready.clear()

        return snapshot
    
    def __enter__(self) -> "TransportBLE":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    async def __aenter__(self) -> "TransportBLE":
        await self.async_connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.async_close()
