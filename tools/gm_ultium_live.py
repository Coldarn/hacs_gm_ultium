#!/usr/bin/env python3
"""Live GM Ultium energy/power monitor for a VEEPEAK/ELM327 BLE dongle.

Keeps one BLE link open and polls only the handful of DIDs needed for a live
dashboard:

    BEC  (DACBF1)  22 27AF   energy remaining, kWh
    BEC  (DACBF1)  22 27C6   state of charge, %
    DMCM (DA17F1)  22 2885   pack voltage, V
    DMCM (DA17F1)  22 2414   pack current, A

Power is derived as ``-(voltage * current) / 1000`` kW, positive = charging.
Energy/SOC are refreshed less often than voltage/current so the fast loop only
carries the two DMCM queries (one CAN header, no ATSH switching).

Note on current resolution: ``2414`` is the validated pack-current DID, but the
DMCM current sensor is effectively 12-bit over a wide range, so the live value
moves in ~0.3 A steps (about 0.09 kW at a 320 V pack). The ``--smooth`` option
averages power over a few seconds to keep that quantisation from flickering on
screen; it does not create resolution that the sensor does not have.

Usage:
    python tools/gm_ultium_live.py
    python tools/gm_ultium_live.py --interval 0.2
    python tools/gm_ultium_live.py --interval 0 --energy-interval 10
    python tools/gm_ultium_live.py --smooth 10
    python tools/gm_ultium_live.py AA:BB:CC:DD:EE:FF --once
    python tools/gm_ultium_live.py --csv live.csv

Stop with Ctrl-C.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "gm_ultium" / "obdii"))

import ultium_commands as uc
from bleak import BleakScanner
from obdii import Connection, at_commands
from obdii.protocol import Protocol
from transport_ble import TransportBLE

logging.getLogger("obdii").setLevel(logging.WARNING)
logging.getLogger("obdii").propagate = False

DEFAULT_ADDRESS = "66:1E:87:05:B3:41"
WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
READ_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"

# Two DMCM queries per fast cycle: no header change, ~2 round trips.
POWER_COMMANDS = ("PACK_VOLTAGE", "PACK_CURRENT")
# BEC queries, refreshed at --energy-interval.
ENERGY_COMMANDS = ("ENERGY_REMAINING", "STATE_OF_CHARGE")


def _set_header(api: Connection, header: str) -> None:
    """Set the 3-byte ELM327 header form, e.g. DACBF1 for 18 DA CB F1."""
    response = api.query(
        at_commands.SET_HEADER(xx=header[0:2], yy=header[2:4], zz=header[4:6])
    )
    if response.value != "OK":
        raise RuntimeError(f"Dongle rejected header {header}: {response.value!r}")


class Link:
    """One persistent BLE/ELM327 link, serialized onto a single thread."""

    def __init__(self, address: str, scan_timeout: float) -> None:
        self.address = address
        self.scan_timeout = scan_timeout
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="obd")
        self.transport: TransportBLE | None = None
        self.api: Connection | None = None
        self.active_header: str | None = None
        self.loop = asyncio.get_running_loop()

    def _open_blocking(self, device: Any) -> None:
        self.transport = TransportBLE(
            ble_device=device,
            uuid_write=WRITE_UUID,
            uuid_read=READ_UUID,
            loop=self.loop,
            timeout=10.0,
        )
        self.api = Connection(
            transport=self.transport,
            auto_connect=False,
            protocol=Protocol.ISO_15765_4_CAN_B,  # ATSP7, 29-bit / 500 kbit
            log_handler=None,
        )
        self.api.connect()
        self.active_header = None

    async def open(self) -> None:
        device = await BleakScanner.find_device_by_address(
            self.address, timeout=self.scan_timeout
        )
        if device is None:
            raise RuntimeError(
                f"{self.address} not found. Is another BLE client (phone, Car "
                "Scanner, Home Assistant) holding the dongle?"
            )
        await self.loop.run_in_executor(self.executor, self._open_blocking, device)

    def _close_blocking(self) -> None:
        if self.api is not None:
            try:
                self.api.close()
            except Exception:
                pass
        self.api = None
        self.transport = None
        self.active_header = None

    async def close(self) -> None:
        await self.loop.run_in_executor(self.executor, self._close_blocking)

    def _poll_blocking(self, names: tuple[str, ...]) -> dict[str, Any]:
        if self.api is None:
            raise RuntimeError("BLE link is not open")
        values: dict[str, Any] = {}
        # Group by CAN header so each ATSH happens at most once per sample.
        for name in sorted(
            names,
            key=lambda n: (uc.ULTIUM_COMMAND_CAN_HEADERS.get(n, ""), n),
        ):
            header = uc.ULTIUM_COMMAND_CAN_HEADERS.get(name)
            if header and header != self.active_header:
                _set_header(self.api, header)
                self.active_header = header
            response = self.api.query(uc.ULTIUM_COMMANDS_BY_NAME[name])
            values[name] = response.value
        return values

    async def poll(self, names: tuple[str, ...]) -> dict[str, Any]:
        return await self.loop.run_in_executor(self.executor, self._poll_blocking, names)


def _fmt(value: Any, unit: str, precision: int) -> str:
    if value is None:
        return f"{'n/a':>12}"
    return f"{value:12.{precision}f} {unit}"


def render(
    values: dict[str, Any],
    *,
    smoothed_power: float | None,
    smooth_tau: float,
    rate_hz: float,
    latency_s: float,
    samples: int,
    uptime_s: float,
    interval_s: float,
) -> str:
    energy = values.get("ENERGY_REMAINING")
    soc = values.get("STATE_OF_CHARGE")
    voltage = values.get("PACK_VOLTAGE")
    current = values.get("PACK_CURRENT")
    power = _power(values)

    if power is None:
        direction = ""
    elif power > 0.05:
        direction = "charging"
    elif power < -0.05:
        direction = "discharging"
    else:
        direction = "idle"

    lines = [
        "GM Ultium live",
        "---------------",
        f"Energy remaining  {_fmt(energy, 'kWh', 3)}",
        f"State of charge   {_fmt(soc, '%', 2)}",
        f"Pack voltage      {_fmt(voltage, 'V', 2)}",
        f"Pack current      {_fmt(current, 'A', 2)}   (positive = discharge)",
    ]
    if smooth_tau > 0 and smoothed_power is not None:
        lines.append(f"Battery power     {_fmt(smoothed_power, 'kW', 2)}   {direction}  (avg {smooth_tau:g} s)")
        lines.append(f"  raw sample      {_fmt(power, 'kW', 2)}")
    else:
        lines.append(f"Battery power     {_fmt(power, 'kW', 2)}   {direction}")

    target = "max rate" if interval_s <= 0 else f"target {interval_s:g} s"
    lines.extend(
        [
            "",
            f"{rate_hz:5.2f} Hz  |  poll {latency_s * 1000:5.0f} ms  |  "
            f"samples {samples}  |  up {uptime_s:,.0f} s  |  {target}",
            "Current LSB is ~0.3 A (~0.09 kW at 320 V); --smooth averages it.",
            "Ctrl-C to stop",
        ]
    )
    return "\n".join(lines)


def _one_line(
    values: dict[str, Any],
    rate_hz: float,
    latency_s: float,
    smoothed_power: float | None,
    smooth_tau: float,
) -> str:
    power = _power(values)
    avg = ""
    if smooth_tau > 0 and smoothed_power is not None:
        avg = f" avg={_fmt(smoothed_power, 'kW', 2)} ({smooth_tau:g}s)"
    return (
        f"{datetime.now().astimezone().isoformat(timespec='seconds')} "
        f"energy={_fmt(values.get('ENERGY_REMAINING'), 'kWh', 3)} "
        f"soc={_fmt(values.get('STATE_OF_CHARGE'), '%', 2)} "
        f"power={_fmt(power, 'kW', 3)}{avg} "
        f"rate={rate_hz:.2f}Hz lat={latency_s * 1000:.0f}ms"
    )


async def run(args: argparse.Namespace) -> None:
    link = Link(args.address, args.timeout)
    csv_file = None
    writer = None
    if args.csv:
        csv_file = open(args.csv, "w", newline="", encoding="utf-8")
        writer = csv.writer(csv_file)
        writer.writerow(
            [
                "timestamp",
                "energy_kwh",
                "soc_percent",
                "pack_voltage_v",
                "pack_current_a",
                "power_kw",
                "power_kw_smoothed",
                "latency_ms",
                "rate_hz",
            ]
        )

    values: dict[str, Any] = {}
    sample_times: deque[float] = deque(maxlen=20)
    samples = 0
    errors = 0
    started = time.monotonic()
    next_energy = 0.0
    smoothed_power: float | None = None
    last_power_t: float | None = None
    interactive = sys.stdout.isatty() and not args.no_screen

    try:
        await link.open()
        while True:
            now = time.monotonic()
            include_energy = now >= next_energy
            names = POWER_COMMANDS + (ENERGY_COMMANDS if include_energy else ())

            t0 = time.monotonic()
            try:
                fresh = await link.poll(names)
            except Exception as err:  # noqa: BLE001 - displayed, then reconnect
                errors += 1
                if interactive:
                    sys.stdout.write(f"\rLink error: {err} (retrying)")
                    sys.stdout.flush()
                else:
                    print(f"Link error: {err} (retrying)", file=sys.stderr)
                if errors >= 5:
                    raise SystemExit("too many consecutive link errors") from err
                await link.close()
                await asyncio.sleep(min(2.0 * errors, 10.0))
                await link.open()
                next_energy = 0.0
                continue

            errors = 0
            latency = time.monotonic() - t0
            for name, value in fresh.items():
                if value is not None:
                    values[name] = value
            if include_energy:
                next_energy = time.monotonic() + args.energy_interval

            samples += 1
            sample_times.append(time.monotonic())
            rate_hz = 0.0
            if len(sample_times) > 1:
                rate_hz = (len(sample_times) - 1) / (sample_times[-1] - sample_times[0])

            sample_t = time.monotonic()
            raw_power = _power(values)
            if raw_power is not None and args.smooth > 0:
                if smoothed_power is None or last_power_t is None:
                    smoothed_power = raw_power
                else:
                    dt = max(sample_t - last_power_t, 1e-6)
                    alpha = 1.0 - math.exp(-dt / args.smooth)
                    smoothed_power += alpha * (raw_power - smoothed_power)
                last_power_t = sample_t

            if writer is not None:
                writer.writerow(
                    [
                        datetime.now(timezone.utc).isoformat(),
                        values.get("ENERGY_REMAINING"),
                        values.get("STATE_OF_CHARGE"),
                        values.get("PACK_VOLTAGE"),
                        values.get("PACK_CURRENT"),
                        raw_power,
                        smoothed_power if args.smooth > 0 else None,
                        f"{latency * 1000:.1f}",
                        f"{rate_hz:.2f}",
                    ]
                )
                csv_file.flush()

            if args.jsonl:
                print(
                    _json_line(
                        values,
                        power=raw_power,
                        smoothed_power=smoothed_power,
                        latency=latency,
                        rate_hz=rate_hz,
                    ),
                    flush=True,
                )
            elif interactive:
                frame = render(
                    values,
                    smoothed_power=smoothed_power,
                    smooth_tau=args.smooth,
                    rate_hz=rate_hz,
                    latency_s=latency,
                    samples=samples,
                    uptime_s=time.monotonic() - started,
                    interval_s=args.interval,
                )
                sys.stdout.write("\033[H\033[J" + frame + "\n")
                sys.stdout.flush()
            else:
                print(
                    _one_line(values, rate_hz, latency, smoothed_power, args.smooth),
                    flush=True,
                )

            if args.once:
                break
            if args.interval > 0:
                await asyncio.sleep(max(0.0, args.interval - latency))
    finally:
        await link.close()
        link.executor.shutdown(wait=False)
        if csv_file is not None:
            csv_file.close()


def _power(values: dict[str, Any]) -> float | None:
    voltage = values.get("PACK_VOLTAGE")
    current = values.get("PACK_CURRENT")
    if voltage is None or current is None:
        return None
    return -(voltage * current) / 1000.0


def _json_line(
    values: dict[str, Any],
    *,
    power: float | None,
    smoothed_power: float | None,
    latency: float,
    rate_hz: float,
) -> str:
    return json.dumps(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "energy_kwh": values.get("ENERGY_REMAINING"),
            "soc_percent": values.get("STATE_OF_CHARGE"),
            "pack_voltage_v": values.get("PACK_VOLTAGE"),
            "pack_current_a": values.get("PACK_CURRENT"),
            "power_kw": power,
            "power_kw_smoothed": smoothed_power,
            "latency_ms": round(latency * 1000, 1),
            "rate_hz": round(rate_hz, 2),
        },
        default=str,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live GM Ultium energy (kWh) and battery power (kW) over BLE"
    )
    parser.add_argument("address", nargs="?", default=DEFAULT_ADDRESS)
    parser.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="seconds between samples; 0 = as fast as the dongle allows (default)",
    )
    parser.add_argument(
        "--energy-interval",
        type=float,
        default=5.0,
        help="seconds between BEC energy/SOC refreshes (default 5)",
    )
    parser.add_argument(
        "--smooth",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="power averaging time constant; 0 shows the raw sample (default 5)",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="BLE scan timeout in seconds")
    parser.add_argument("--once", action="store_true", help="take one sample and exit")
    parser.add_argument("--jsonl", action="store_true", help="print one JSON object per sample")
    parser.add_argument("--csv", metavar="PATH", help="write samples to a CSV file")
    parser.add_argument("--no-screen", action="store_true", help="do not clear/redraw the terminal")
    args = parser.parse_args()

    if args.energy_interval < 0:
        parser.error("--energy-interval must be >= 0")
    if args.interval < 0:
        parser.error("--interval must be >= 0")
    if args.smooth < 0:
        parser.error("--smooth must be >= 0")

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as err:  # noqa: BLE001 - final user-facing error
        raise SystemExit(f"error: {err}") from err


if __name__ == "__main__":
    main()
