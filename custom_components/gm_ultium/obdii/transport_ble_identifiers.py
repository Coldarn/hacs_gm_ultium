from abc import ABC, abstractmethod
from typing import TypedDict
from glob import translate
import re

from bleak.backends.scanner import AdvertisementData


class MatcherPattern(TypedDict, total=False):
    """Optional patterns that can match Bleak advertisement data."""

    local_name: str  # name pattern that supports Unix shell-style wildcards
    manufacturer_data_start: list[int]  # start bytes of manufacturer data
    manufacturer_id: int  # required manufacturer ID
    oui: str  # required OUI used in the MAC address (first 3 bytes)
    service_data_uuid: str  # service data for the service UUID
    service_uuid: str  # 128-bit UUID that the device must advertise


class BaseOBD2(ABC):
    @staticmethod
    @abstractmethod
    def matcher_dict_list() -> list[MatcherPattern]:
        """Return a list of Bluetooth advertisement matchers."""

    @staticmethod
    @abstractmethod
    def uuid_rx() -> str:
        """Return 16-bit UUID of characteristic that provides notification/read property."""

    @staticmethod
    @abstractmethod
    def uuid_tx() -> str:
        """Return 16-bit UUID of characteristic that provides write property."""


class OBD2_BLE(BaseOBD2):
    @staticmethod
    def matcher_dict_list() -> list[MatcherPattern]:
        """Provide BluetoothMatcher definition."""
        return [{
            "local_name": "OBDII",
            "service_uuid": "0000fff0-0000-1000-8000-00805f9b34fb"
        }]

    @staticmethod
    def uuid_rx() -> str:
        return "0000fff1-0000-1000-8000-00805f9b34fb"

    @staticmethod
    def uuid_tx() -> str:
        return "0000fff2-0000-1000-8000-00805f9b34fb"


class VlinkOBD2_BLE(OBD2_BLE):
    @staticmethod
    def matcher_dict_list() -> list[MatcherPattern]:
        """Provide BluetoothMatcher definition."""
        return [{
            "local_name": "Vlink*",
            "service_uuid": "000018f0-0000-1000-8000-00805f9b34fb"
        }]

    @staticmethod
    def uuid_rx() -> str:
        return "000018f1-0000-1000-8000-00805f9b34fb"

    @staticmethod
    def uuid_tx() -> str:
        return "000018f2-0000-1000-8000-00805f9b34fb"

AVAILABLE_OBD2_CLASSES: list[type[BaseOBD2]] = [OBD2_BLE, VlinkOBD2_BLE]

def advertisement_matches(
    matcher: MatcherPattern,
    adv_data: AdvertisementData,
    mac_addr: str
) -> bool:
    """Determine whether the given advertisement data matches the specified pattern.
    Args:
        matcher (MatcherPattern): A dictionary containing the matching criteria.
        adv_data (AdvertisementData): An object containing the advertisement data to be checked.
        mac_addr (str): Bluetooth device address in the format: "00:11:22:aa:bb:cc"

    Returns:
        bool: True if the advertisement data matches the specified pattern, False otherwise.
    """
    if (
        service_uuid := matcher.get("service_uuid")
    ) and service_uuid not in adv_data.service_uuids:
        return False

    if (
        service_data_uuid := matcher.get("service_data_uuid")
    ) and service_data_uuid not in adv_data.service_data:
        return False

    if (oui := matcher.get("oui")) and not mac_addr.lower().startswith(oui.lower()[:8]):
        return False

    if (manufacturer_id := matcher.get("manufacturer_id")) is not None:
        if manufacturer_id not in adv_data.manufacturer_data:
            return False

        if manufacturer_data_start := matcher.get("manufacturer_data_start"):
            if not adv_data.manufacturer_data[manufacturer_id].startswith(
                bytes(manufacturer_data_start)
            ):
                return False

    return not (
        (local_name := matcher.get("local_name"))
        and not re.compile(translate(local_name)).match(adv_data.local_name or "")
    )
