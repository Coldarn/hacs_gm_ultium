"""Validated GM Ultium (BT1/BEV3) Mode 22 commands.

All values were validated live against a 2024 BrightDrop (BEC at 0xCB,
DMCM at 0x17). Read-only: only service 0x22 is used.

Header form is the 3-byte ELM327 ``ATSH`` form (``DACBF1`` == ``18 DA CB F1``).
This ELM327 v1.5 clone rejects the 4-byte form; protocol ``ATSP7`` supplies
the 29-bit CAN priority.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from obdii import Command
from obdii.modes.group_commands import GroupCommands
from obdii.parsers.formula import Formula

BEC_HEADER = "DACBF1"    # 18 DA CB F1 — Battery Energy Control
DMCM_HEADER = "DA17F1"   # 18 DA 17 F1 — Drive Motor Control Module


def _payload(value: Any) -> list[int]:
    """Normalize a py-obdii resolver payload into a list of bytes."""
    if value is None:
        return []
    if isinstance(value, dict):
        out: list[int] = []
        for key in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if key not in value:
                break
            item = value[key]
            if not isinstance(item, int):
                break
            out.append(item & 0xFF)
        return out
    data = getattr(value, "data", value)
    if isinstance(data, (bytes, bytearray)):
        return [int(byte) for byte in data]
    if isinstance(data, Iterable) and not isinstance(data, (str, bytes)):
        return [int(byte) & 0xFF for byte in data if isinstance(byte, int)]
    return []


def _u16(data: list[int], index: int) -> int:
    return (data[index] << 8) | data[index + 1]


def _s16(data: list[int], index: int) -> int:
    value = _u16(data, index)
    return value - 0x10000 if value & 0x8000 else value


def _signed_word_div20(value: Any) -> float | None:
    data = _payload(value)
    return _s16(data, 0) / 20 if len(data) >= 2 else None


def _cell_avg(value: Any) -> float | None:
    data = _payload(value)
    return _u16(data, 0) / 10000 if len(data) >= 2 else None


def _cell_min(value: Any) -> float | None:
    data = _payload(value)
    return _u16(data, 2) / 10000 if len(data) >= 4 else None


def _cell_max(value: Any) -> float | None:
    data = _payload(value)
    return _u16(data, 4) / 10000 if len(data) >= 6 else None


def _module_temps(value: Any) -> list[float]:
    data = _payload(value)
    # Scaling is not fully settled in public sources; (x - 40) / 2 fits the
    # live BrightDrop sample (~25 C). Exposed as experimental.
    return [(byte - 40) / 2 for byte in data[:12]]


def _module_temp_min(value: Any) -> float | None:
    temps = _module_temps(value)
    return min(temps) if temps else None


def _module_temp_max(value: Any) -> float | None:
    temps = _module_temps(value)
    return max(temps) if temps else None


def _range_miles(value: Any) -> float | None:
    data = _payload(value)
    if len(data) < 3:
        return None
    return int.from_bytes(bytes(data[:3]), "big") / 103.0


def _u8(value: Any) -> int | None:
    data = _payload(value)
    return data[0] if data else None


class GMUltiumCommands(GroupCommands):
    """Validated Ultium signals exposed as individual py-obdii commands."""

    STATE_OF_CHARGE = Command(
        0x22, "27C6", 2, 0, 100, "%", Formula("(A*256+B)*100/65535")
    )
    """High-resolution pack state of charge (dashboard-consistent)."""

    ENERGY_REMAINING = Command(
        0x22, "27AF", 2, 0, 300, "kWh", Formula("(A*256+B)/100")
    )
    """BMS energy remaining in kWh."""

    CELL_VOLTAGE_AVG = Command(0x22, "2AF5", 10, 0, 6, "V", _cell_avg)
    """Average cell voltage."""

    CELL_VOLTAGE_MIN = Command(0x22, "2AF5", 10, 0, 6, "V", _cell_min)
    """Minimum cell voltage."""

    CELL_VOLTAGE_MAX = Command(0x22, "2AF5", 10, 0, 6, "V", _cell_max)
    """Maximum cell voltage."""

    CHARGE_STATE = Command(0x22, "5401", 1, 0, 255, "", _u8)
    """Raw charge-state byte (0x00 idle, 0x91-0x99 charging)."""

    MODULE_TEMP_MIN = Command(0x22, "2AF1", 12, -40, 100, "°C", _module_temp_min)
    """Minimum module temperature (experimental scaling)."""

    MODULE_TEMP_MAX = Command(0x22, "2AF1", 12, -40, 100, "°C", _module_temp_max)
    """Maximum module temperature (experimental scaling)."""

    RANGE_MILES = Command(0x22, "27C7", 3, 0, 1000, "mi", _range_miles)
    """Estimated range in miles (matches the dashboard)."""

    PACK_VOLTAGE = Command(
        0x22, "2885", 2, 0, 500, "V", Formula("(A*256+B)/100")
    )
    """High-voltage pack voltage."""

    PACK_CURRENT = Command(0x22, "2414", 2, -1000, 1000, "A", _signed_word_div20)
    """Pack current; positive discharges, negative charges.

    The scaling is 0.05 A per raw count, but the DMCM current sensor behaves
    like a 12-bit measurement over a wide range, so live values move in ~0.3 A
    steps (~0.09 kW at 320 V). This is the validated pack-current DID; the
    coarse low-current resolution is in the sensor, not the decoder.
    """

    LOW_VOLTAGE = Command(0x22, "33E5", 1, 0, 25, "V", Formula("A/10"))
    """12 V rail."""


ULTIUM_COMMANDS = list(GMUltiumCommands())
ULTIUM_COMMANDS_BY_NAME = {command.name: command for command in ULTIUM_COMMANDS}

ULTIUM_COMMAND_CAN_HEADERS = {
    "STATE_OF_CHARGE": BEC_HEADER,
    "ENERGY_REMAINING": BEC_HEADER,
    "CELL_VOLTAGE_AVG": BEC_HEADER,
    "CELL_VOLTAGE_MIN": BEC_HEADER,
    "CELL_VOLTAGE_MAX": BEC_HEADER,
    "CHARGE_STATE": BEC_HEADER,
    "MODULE_TEMP_MIN": BEC_HEADER,
    "MODULE_TEMP_MAX": BEC_HEADER,
    "RANGE_MILES": BEC_HEADER,
    "PACK_VOLTAGE": DMCM_HEADER,
    "PACK_CURRENT": DMCM_HEADER,
    "LOW_VOLTAGE": BEC_HEADER,
}
