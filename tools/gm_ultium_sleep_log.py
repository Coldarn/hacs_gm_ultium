#!/usr/bin/env python3
"""Long-run sleep/power logger for GM Ultium over a BLE OBD dongle.

The goal is to answer, with data rather than guesses:

- When does the BEC/DMCM stop answering after ignition-off?
- Does polling keep the vehicle awake?
- Does a persistent BLE link behave differently from connect/poll/disconnect?
- What does the dongle's own 12 V supply (ATRV) do over a day?
- How often does the link fail or reconnect?

The logger is read-only. Use ``--mode dongle-only`` to avoid touching the
vehicle bus at all (only ATRV/ATIGN, which are adapter-local).

Examples
--------
Short smoke test (6 cycles, 10 s apart)::

    python tools/gm_ultium_sleep_log.py --mode cycle --interval 10 \
        --duration 0 --max-cycles 6

24 h parked log with one connection per sample::

    python tools/gm_ultium_sleep_log.py --mode cycle --interval 60 \
        --duration 24 --csv

24 h with a persistent BLE link::

    python tools/gm_ultium_sleep_log.py --mode persistent --interval 60 \
        --duration 24 --csv

Dongle-only baseline (does not wake the car)::

    python tools/gm_ultium_sleep_log.py --mode dongle-only --interval 60 \
        --duration 24 --csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import time
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

FIELDS = [
    "ts",
    "mono",
    "cycle",
    "mode",
    "link",
    "connected",
    "scan_ms",
    "connect_ms",
    "poll_ms",
    "cycle_ms",
    "atrv_v",
    "atign",
    "ignition",
    "vehicle_responding",
    "probe_ok",
    "soc_pct",
    "energy_kwh",
    "battery_12v_v",
    "charge_state",
    "pack_v",
    "pack_a",
    "power_kw",
    "transport_error",
    "errors",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_atrv(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().lower().replace("v", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def parse_ignition(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def build_api(device: Any, loop: asyncio.AbstractEventLoop, timeout: float) -> Connection:
    transport = TransportBLE(
        ble_device=device,
        uuid_write=WRITE_UUID,
        uuid_read=READ_UUID,
        loop=loop,
        timeout=timeout,
    )
    return Connection(
        transport=transport,
        auto_connect=False,
        protocol=Protocol.ISO_15765_4_CAN_B,  # ATSP7, 29-bit / 500 kbit
        log_handler=None,
    )


def connect_api(api: Connection, at_timeout: str) -> None:
    api.connect()
    response = api.query(at_commands.SET_TIMEOUT(hh=at_timeout))
    if response.value != "OK":
        raise RuntimeError(f"ATST {at_timeout} rejected: {response.value!r}")


def close_api(api: Connection | None) -> None:
    if api is None:
        return
    try:
        api.close()
    except Exception:
        pass


def set_header(api: Connection, header: str) -> bool:
    if len(header) != 6:
        raise ValueError(f"Expected 6 hex characters, got {header!r}")
    response = api.query(
        at_commands.SET_HEADER(xx=header[0:2], yy=header[2:4], zz=header[4:6])
    )
    return response.value == "OK"


def _query(
    api: Connection,
    command: Any,
    label: str,
    errors: list[str],
) -> Any:
    try:
        return api.query(command).value
    except TimeoutError:
        errors.append(f"{label}:timeout")
        raise
    except Exception as exc:
        errors.append(f"{label}:{type(exc).__name__}")
        return None


def poll(api: Connection, mode: str) -> dict[str, Any]:
    """One blocking poll. Returns a record fragment."""
    errors: list[str] = []
    record: dict[str, Any] = {
        "atrv_v": None,
        "atign": None,
        "ignition": None,
        "vehicle_responding": False,
        "probe_ok": False,
        "soc_pct": None,
        "energy_kwh": None,
        "battery_12v_v": None,
        "charge_state": None,
        "pack_v": None,
        "pack_a": None,
        "power_kw": None,
        "transport_error": False,
        "errors": errors,
    }

    try:
        record["atrv_v"] = parse_atrv(_query(api, at_commands.READ_VOLTAGE, "ATRV", errors))
        record["atign"] = parse_ignition(
            _query(api, at_commands.IGNITION_LEVEL, "ATIGN", errors)
        )
    except TimeoutError:
        record["transport_error"] = True
        return record

    if record["atign"] is not None:
        record["ignition"] = record["atign"].upper().startswith("ON")

    if mode == "dongle-only":
        return record

    try:
        if not set_header(api, uc.BEC_HEADER):
            errors.append("BEC header rejected")
            return record

        # Probe with SOC first. If the BEC is asleep, skip the rest of the
        # bus so a sleeping cycle stays cheap.
        soc = _query(
            api,
            uc.ULTIUM_COMMANDS_BY_NAME["STATE_OF_CHARGE"],
            "27C6",
            errors,
        )
        record["probe_ok"] = soc is not None
        record["vehicle_responding"] = soc is not None
        if soc is None:
            return record
        record["soc_pct"] = soc

        record["energy_kwh"] = _query(
            api, uc.ULTIUM_COMMANDS_BY_NAME["ENERGY_REMAINING"], "27AF", errors
        )
        record["battery_12v_v"] = _query(
            api, uc.ULTIUM_COMMANDS_BY_NAME["LOW_VOLTAGE"], "33E5", errors
        )
        record["charge_state"] = _query(
            api, uc.ULTIUM_COMMANDS_BY_NAME["CHARGE_STATE"], "5401", errors
        )

        if set_header(api, uc.DMCM_HEADER):
            record["pack_v"] = _query(
                api, uc.ULTIUM_COMMANDS_BY_NAME["PACK_VOLTAGE"], "2885", errors
            )
            record["pack_a"] = _query(
                api, uc.ULTIUM_COMMANDS_BY_NAME["PACK_CURRENT"], "2414", errors
            )
        else:
            errors.append("DMCM header rejected")
    except TimeoutError:
        record["transport_error"] = True

    if record["pack_v"] is not None and record["pack_a"] is not None:
        # Positive = charging, matching the integration's convention.
        record["power_kw"] = -(record["pack_v"] * record["pack_a"]) / 1000.0

    return record


def merge_record(base: dict[str, Any], fragment: dict[str, Any]) -> None:
    errors = base.setdefault("errors", [])
    frag_errors = fragment.pop("errors", [])
    base.update(fragment)
    errors.extend(frag_errors)


def summarize(
    records: list[dict[str, Any]],
    args: argparse.Namespace,
    started: str,
    ended: str,
    link_mode: str,
) -> dict[str, Any]:
    atrv = [r["atrv_v"] for r in records if r.get("atrv_v") is not None]
    scan_ms = [r["scan_ms"] for r in records if r.get("scan_ms") is not None]
    connect_ms = [r["connect_ms"] for r in records if r.get("connect_ms") is not None]
    poll_ms = [r["poll_ms"] for r in records if r.get("poll_ms") is not None]
    cycle_ms = [r["cycle_ms"] for r in records if r.get("cycle_ms") is not None]
    vehicle = [r for r in records if r.get("vehicle_responding")]

    longest_off = 0
    if args.mode != "dongle-only":
        current_off = 0
        for record in records:
            if record.get("vehicle_responding"):
                current_off = 0
            else:
                current_off += 1
                longest_off = max(longest_off, current_off)

    return {
        "started": started,
        "ended": ended,
        "mode": args.mode,
        "link": link_mode,
        "interval_s": args.interval,
        "cycles": len(records),
        "connected_cycles": sum(1 for r in records if r.get("connected")),
        "vehicle_response_cycles": len(vehicle),
        "first_vehicle_response": vehicle[0]["ts"] if vehicle else None,
        "last_vehicle_response": vehicle[-1]["ts"] if vehicle else None,
        "longest_no_response_run": longest_off,
        "longest_no_response_s": round(longest_off * args.interval),
        "atrv_min_v": min(atrv) if atrv else None,
        "atrv_max_v": max(atrv) if atrv else None,
        "avg_scan_ms": round(sum(scan_ms) / len(scan_ms)) if scan_ms else None,
        "avg_connect_ms": round(sum(connect_ms) / len(connect_ms)) if connect_ms else None,
        "avg_poll_ms": round(sum(poll_ms) / len(poll_ms)) if poll_ms else None,
        "avg_cycle_ms": round(sum(cycle_ms) / len(cycle_ms)) if cycle_ms else None,
        "transport_errors": sum(1 for r in records if r.get("transport_error")),
        "records_with_errors": sum(1 for r in records if r.get("errors")),
    }


async def run(args: argparse.Namespace) -> None:
    if args.duration <= 0 and args.max_cycles <= 0:
        raise SystemExit("Set --duration > 0 or --max-cycles > 0")

    if args.mode == "cycle":
        link_mode = "cycle"
    elif args.mode == "persistent":
        link_mode = "persistent"
    else:
        link_mode = "persistent" if args.link == "auto" else args.link

    started = utcnow()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = Path(args.output) if args.output else ROOT / "logs" / f"sleep-{stamp}.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    csv_path = output.with_suffix(".csv") if args.csv else None

    jsonl_file = output.open("w", encoding="utf-8")
    csv_file = csv_path.open("w", newline="", encoding="utf-8") if csv_path else None
    csv_writer = (
        csv.DictWriter(csv_file, fieldnames=FIELDS, extrasaction="ignore")
        if csv_file
        else None
    )
    if csv_writer:
        csv_writer.writeheader()

    loop = asyncio.get_running_loop()
    device: Any = None
    api: Connection | None = None
    records: list[dict[str, Any]] = []
    started_mono = time.monotonic()
    deadline = started_mono + args.duration * 3600 if args.duration > 0 else None
    cycle = 0

    print(f"logging to {output}" + (f" and {csv_path}" if csv_path else ""))
    print(
        f"mode={args.mode} link={link_mode} interval={args.interval}s "
        f"duration={args.duration}h max_cycles={args.max_cycles or 'none'} "
        f"at_timeout=0x{args.at_timeout}"
    )

    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                break
            if args.max_cycles and cycle >= args.max_cycles:
                break

            cycle += 1
            cycle_start = time.monotonic()
            record: dict[str, Any] = {field: None for field in FIELDS}
            record.update(
                {
                    "ts": utcnow(),
                    "mono": round(cycle_start - started_mono, 3),
                    "cycle": cycle,
                    "mode": args.mode,
                    "link": link_mode,
                    "connected": False,
                    "errors": [],
                }
            )

            record["scan_ms"] = 0
            record["connect_ms"] = 0
            record["poll_ms"] = 0
            connect_start: float | None = None
            try:
                if api is None or not api.is_connected():
                    close_api(api)
                    api = None
                    if device is None:
                        scan_start = time.monotonic()
                        device = await BleakScanner.find_device_by_address(
                            args.address, timeout=args.scan_timeout
                        )
                        record["scan_ms"] = round((time.monotonic() - scan_start) * 1000)
                        if device is None:
                            raise RuntimeError(
                                "dongle not found (connected to another app?)"
                            )
                    connect_start = time.monotonic()
                    api = build_api(device, loop, args.transport_timeout)
                    await asyncio.to_thread(connect_api, api, args.at_timeout)
                    record["connect_ms"] = round((time.monotonic() - connect_start) * 1000)

                record["connected"] = True
                poll_start = time.monotonic()
                fragment = await asyncio.to_thread(poll, api, args.mode)
                record["poll_ms"] = round((time.monotonic() - poll_start) * 1000)
                merge_record(record, fragment)
            except Exception as exc:
                record["connected"] = False
                if connect_start is not None and record["connect_ms"] == 0:
                    record["connect_ms"] = round(
                        (time.monotonic() - connect_start) * 1000
                    )
                record["errors"].append(f"{type(exc).__name__}: {exc}")
                close_api(api)
                api = None
                device = None
            finally:
                if link_mode == "cycle" or record.get("transport_error"):
                    close_api(api)
                    api = None
                    device = None

            record["cycle_ms"] = round((time.monotonic() - cycle_start) * 1000)
            records.append(record)

            jsonl_file.write(json.dumps(record, default=str) + "\n")
            jsonl_file.flush()
            if csv_writer:
                row = {
                    key: ("; ".join(record[key]) if key == "errors" and record[key] else record[key])
                    for key in FIELDS
                }
                csv_writer.writerow(row)
                csv_file.flush()

            if not args.quiet:
                soc = "n/a" if record["soc_pct"] is None else f"{record['soc_pct']:.2f}"
                energy = (
                    "n/a" if record["energy_kwh"] is None else f"{record['energy_kwh']:.2f}"
                )
                pack = (
                    "n/a"
                    if record["pack_v"] is None or record["pack_a"] is None
                    else f"{record['pack_v']:.1f}V/{record['pack_a']:.1f}A"
                )
                power = (
                    "n/a" if record["power_kw"] is None else f"{record['power_kw']:.2f}"
                )
                first_error = (
                    f" first={record['errors'][0]}" if record["errors"] else ""
                )
                print(
                    f"[{cycle:4d}] {record['ts']} "
                    f"conn={'Y' if record['connected'] else 'N'} "
                    f"scan={record['scan_ms']}ms connect={record['connect_ms']}ms "
                    f"poll={record['poll_ms']}ms cycle={record['cycle_ms']}ms "
                    f"atrv={record['atrv_v']}V ign={record['atign']} "
                    f"veh={'Y' if record['vehicle_responding'] else 'N'} "
                    f"soc={soc}% E={energy}kWh pack={pack} P={power}kW "
                    f"err={len(record['errors'])}{first_error}",
                    flush=True,
                )

            elapsed = time.monotonic() - cycle_start
            await asyncio.sleep(max(0.0, args.interval - elapsed))
    finally:
        close_api(api)
        jsonl_file.close()
        if csv_file:
            csv_file.close()

        ended = utcnow()
        summary = summarize(records, args, started, ended, link_mode)
        summary_path = output.with_name(output.stem + "-summary.json")
        summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print("\nSummary")
        print(json.dumps(summary, indent=2))
        print(f"\nWrote {output}")
        if csv_path:
            print(f"Wrote {csv_path}")
        print(f"Wrote {summary_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GM Ultium sleep/power logger")
    parser.add_argument("--address", default=DEFAULT_ADDRESS)
    parser.add_argument(
        "--mode",
        choices=["persistent", "cycle", "dongle-only"],
        default="cycle",
        help="persistent link, connect/disconnect per sample, or dongle-local only",
    )
    parser.add_argument(
        "--link",
        choices=["auto", "persistent", "cycle"],
        default="auto",
        help="BLE link behavior for --mode dongle-only (auto=persistent)",
    )
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between samples")
    parser.add_argument("--duration", type=float, default=24.0, help="hours; 0 disables the deadline")
    parser.add_argument("--max-cycles", type=int, default=0, help="stop after N cycles; 0 disables")
    parser.add_argument(
        "--at-timeout",
        default="32",
        help="ELM327 ATST value as two hex digits (0x32 = 200 ms); default 32",
    )
    parser.add_argument("--transport-timeout", type=float, default=6.0)
    parser.add_argument("--scan-timeout", type=float, default=10.0)
    parser.add_argument("--output", default="", help="JSONL output path")
    parser.add_argument("--csv", action="store_true", help="also write CSV")
    parser.add_argument("--quiet", action="store_true", help="do not print every cycle")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped by user.")


if __name__ == "__main__":
    main()
