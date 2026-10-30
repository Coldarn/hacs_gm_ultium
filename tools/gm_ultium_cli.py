#!/usr/bin/env python3
"""Read validated GM Ultium battery data over a VEEPEAK/ELM327 BLE dongle.

This is the standalone validation client for the gm_ultium integration. It
uses the same TransportBLE + py-obdii stack as the Home Assistant component,
so a successful run here is the integration's protocol milestone.

Usage:
    python tools/gm_ultium_cli.py
    python tools/gm_ultium_cli.py --json
    python tools/gm_ultium_cli.py AA:BB:CC:DD:EE:FF
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "gm_ultium" / "obdii"))

from bleak import BleakScanner  # noqa: E402
from obdii import Connection, at_commands  # noqa: E402
from obdii.basetypes import MISSING  # noqa: E402
from obdii.protocol import Protocol  # noqa: E402

import ultium_commands as uc  # noqa: E402
from transport_ble import TransportBLE  # noqa: E402

DEFAULT_ADDRESS = "66:1E:87:05:B3:41"
WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
READ_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"

CHARGE_STATE_NAMES = {
    0x00: "idle",
    0x01: "ready",
}
for _state in range(0x91, 0x9A):
    CHARGE_STATE_NAMES[_state] = "charging"


def _set_header(api: Connection, header: str) -> None:
    """Set the 3-byte ELM327 header form, e.g. DACBF1 for 18 DA CB F1."""
    if len(header) != 6:
        raise ValueError(f"Expected 6 hex characters, got {header!r}")
    response = api.query(
        at_commands.SET_HEADER(xx=header[0:2], yy=header[2:4], zz=header[4:6])
    )
    if response.value != "OK":
        raise RuntimeError(f"Dongle rejected header {header}: {response.value!r}")


def read_all(api: Connection) -> dict[str, dict[str, Any]]:
    """Query every validated command, grouped by CAN header. Blocking."""
    out: dict[str, dict[str, Any]] = {}
    active_header: str | None = None
    for command in sorted(
        uc.ULTIUM_COMMANDS,
        key=lambda c: (uc.ULTIUM_COMMAND_CAN_HEADERS.get(c.name, ""), c.name),
    ):
        header = uc.ULTIUM_COMMAND_CAN_HEADERS.get(command.name)
        if header and header != active_header:
            _set_header(api, header)
            active_header = header
        response = api.query(command)
        out[command.name] = {
            "value": response.value,
            "unparsed": list(response.unparsed) if response.unparsed else None,
            "raw": response.raw.decode("ascii", "replace").strip(),
        }
    return out


def derive(data: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Add the derived power/capacity/charging values."""
    soc = data.get("STATE_OF_CHARGE", {}).get("value")
    energy = data.get("ENERGY_REMAINING", {}).get("value")
    voltage = data.get("PACK_VOLTAGE", {}).get("value")
    current = data.get("PACK_CURRENT", {}).get("value")
    charge_state = data.get("CHARGE_STATE", {}).get("value")

    power_kw = None
    if voltage is not None and current is not None:
        # Raw current is positive while discharging; invert so positive = charging.
        power_kw = -(voltage * current) / 1000.0

    capacity_kwh = None
    if energy is not None and soc:
        capacity_kwh = energy / (soc / 100.0)

    charging = False
    if power_kw is not None and power_kw > 0.5:
        charging = True
    if current is not None and current < -1.0:
        charging = True
    if charge_state in CHARGE_STATE_NAMES and CHARGE_STATE_NAMES[charge_state] == "charging":
        charging = True

    return {
        "soc_percent": soc,
        "energy_remaining_kwh": energy,
        "estimated_capacity_kwh": capacity_kwh,
        "pack_voltage_v": voltage,
        "pack_current_a": current,
        "battery_power_kw": power_kw,
        "charging": charging,
        "cell_voltage_avg_v": data.get("CELL_VOLTAGE_AVG", {}).get("value"),
        "cell_voltage_min_v": data.get("CELL_VOLTAGE_MIN", {}).get("value"),
        "cell_voltage_max_v": data.get("CELL_VOLTAGE_MAX", {}).get("value"),
        "module_temp_min_c": data.get("MODULE_TEMP_MIN", {}).get("value"),
        "module_temp_max_c": data.get("MODULE_TEMP_MAX", {}).get("value"),
        "range_miles": data.get("RANGE_MILES", {}).get("value"),
        "charge_state": charge_state,
        "charge_state_name": CHARGE_STATE_NAMES.get(charge_state, "unknown"),
        "low_voltage_v": data.get("LOW_VOLTAGE", {}).get("value"),
        "last_seen": datetime.now(timezone.utc).isoformat(),
    }


def _fmt(value: Any, unit: str = "", precision: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, str):
        return value
    return f"{value:.{precision}f} {unit}".strip()


def render(derived: dict[str, Any]) -> str:
    cell_triplet = " / ".join(
        _fmt(derived[key], "V", 4)
        for key in ("cell_voltage_avg_v", "cell_voltage_min_v", "cell_voltage_max_v")
    )
    temp_triplet = " / ".join(
        _fmt(derived[key], "C", 1)
        for key in ("module_temp_min_c", "module_temp_max_c")
    )
    range_miles = derived["range_miles"]
    range_km = range_miles * 1.609344 if range_miles is not None else None
    power = derived["battery_power_kw"]
    power_note = ""
    if power is not None:
        power_note = " (charging)" if power > 0.5 else " (discharging)" if power < -0.5 else " (idle)"
    return "\n".join(
        [
            "GM Ultium / BrightDrop",
            "----------------------",
            f"SOC                    {_fmt(derived['soc_percent'], '%', 2)}",
            f"Energy remaining       {_fmt(derived['energy_remaining_kwh'], 'kWh', 2)}",
            f"Estimated capacity     {_fmt(derived['estimated_capacity_kwh'], 'kWh', 1)}",
            f"Pack voltage           {_fmt(derived['pack_voltage_v'], 'V', 2)}",
            f"Pack current           {_fmt(derived['pack_current_a'], 'A', 2)} (positive = discharge)",
            f"Battery power          {_fmt(power, 'kW', 2)}{power_note}",
            f"Cell voltage (a/m/M)   {cell_triplet}",
            f"Module temp (min/max)  {temp_triplet}",
            f"Range                  {_fmt(range_miles, 'mi', 1)} / {_fmt(range_km, 'km', 1)}",
            f"Charge state           {derived['charge_state_name']} (0x{derived['charge_state']:02x})"
            if derived["charge_state"] is not None
            else "Charge state           n/a",
            f"12 V rail              {_fmt(derived['low_voltage_v'], 'V', 1)}",
            f"Last seen              {derived['last_seen']}",
        ]
    )


async def run(address: str, timeout: float) -> dict[str, Any]:
    device = await BleakScanner.find_device_by_address(address, timeout=timeout)
    if device is None:
        raise SystemExit(
            f"{address} not found. Is the dongle connected to another device "
            "(phone/Car Scanner or another BLE client)?"
        )

    loop = asyncio.get_running_loop()
    transport = TransportBLE(
        ble_device=device,
        uuid_write=WRITE_UUID,
        uuid_read=READ_UUID,
        loop=loop,
        timeout=20.0,
    )
    api = Connection(
        transport=transport,
        auto_connect=False,
        protocol=Protocol.ISO_15765_4_CAN_B,  # ATSP7, 29-bit / 500 kbit
        log_handler=MISSING,
        log_formatter=MISSING,
        log_level=MISSING,
    )

    await asyncio.to_thread(api.connect)
    try:
        raw = await asyncio.to_thread(read_all, api)
    finally:
        try:
            await asyncio.to_thread(api.close)
        except Exception:
            pass

    derived = derive(raw)
    derived["raw"] = raw
    return derived


def main() -> None:
    parser = argparse.ArgumentParser(description="Read GM Ultium battery data over BLE")
    parser.add_argument("address", nargs="?", default=DEFAULT_ADDRESS)
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    parser.add_argument("--timeout", type=float, default=15.0, help="BLE scan timeout")
    args = parser.parse_args()

    derived = asyncio.run(run(args.address, args.timeout))
    if args.json:
        print(json.dumps(derived, indent=2, default=str))
    else:
        print(render(derived))


if __name__ == "__main__":
    main()
