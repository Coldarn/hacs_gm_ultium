"""Sleep/power measurement logger that runs inside Home Assistant.

The logger is deliberately read-only. It uses the same TransportBLE +
py-obdii path as the coordinator, but owns the dongle while it runs. The
normal coordinator is disabled in measurement mode so there is only one BLE
owner.

Files are written to ``/config/www/gm_ultium_logs`` so they are downloadable
from Home Assistant at ``/local/gm_ultium_logs/...`` without Samba or SSH.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from obdii import Connection, at_commands
from obdii.protocol import Protocol

from .const import SLEEP_LOG_WWW_DIR
from .obdii import ultium_commands as uc
from .obdii.transport_ble import TransportBLE

_LOGGER = logging.getLogger(__name__)

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


def connect_api(api: Connection, at_timeout: str = "32") -> None:
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


def _query(api: Connection, command: Any, label: str, errors: list[str]) -> Any:
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
        record["atrv_v"] = parse_atrv(
            _query(api, at_commands.READ_VOLTAGE, "ATRV", errors)
        )
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
    mode: str,
    link: str,
    interval: float,
    started: str | None,
    ended: str,
) -> dict[str, Any]:
    atrv = [r["atrv_v"] for r in records if r.get("atrv_v") is not None]
    scan_ms = [r["scan_ms"] for r in records if r.get("scan_ms") is not None]
    connect_ms = [r["connect_ms"] for r in records if r.get("connect_ms") is not None]
    poll_ms = [r["poll_ms"] for r in records if r.get("poll_ms") is not None]
    cycle_ms = [r["cycle_ms"] for r in records if r.get("cycle_ms") is not None]
    vehicle = [r for r in records if r.get("vehicle_responding")]

    longest_off = 0
    if mode != "dongle-only":
        current_off = 0
        for record in records:
            if record.get("vehicle_responding"):
                current_off = 0
            else:
                current_off += 1
                longest_off = max(longest_off, current_off)

    def avg(values: list[int]) -> int | None:
        return round(sum(values) / len(values)) if values else None

    return {
        "started": started,
        "ended": ended,
        "mode": mode,
        "link": link,
        "interval_s": interval,
        "cycles": len(records),
        "connected_cycles": sum(1 for r in records if r.get("connected")),
        "vehicle_response_cycles": len(vehicle),
        "first_vehicle_response": vehicle[0]["ts"] if vehicle else None,
        "last_vehicle_response": vehicle[-1]["ts"] if vehicle else None,
        "longest_no_response_run": longest_off,
        "longest_no_response_s": round(longest_off * interval),
        "atrv_min_v": min(atrv) if atrv else None,
        "atrv_max_v": max(atrv) if atrv else None,
        "avg_scan_ms": avg(scan_ms),
        "avg_connect_ms": avg(connect_ms),
        "avg_poll_ms": avg(poll_ms),
        "avg_cycle_ms": avg(cycle_ms),
        "transport_errors": sum(1 for r in records if r.get("transport_error")),
        "records_with_errors": sum(1 for r in records if r.get("errors")),
    }


class SleepLogger:
    """Background sleep/power logger owned by the config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        mode: str,
        link: str,
        interval: float,
        duration: float,
        csv_enabled: bool,
        output: str,
        max_cycles: int,
    ) -> None:
        if duration <= 0 and max_cycles <= 0:
            raise ValueError("Set duration > 0 or max_cycles > 0")

        self.hass = hass
        self.address = address
        self.mode = mode
        self.interval = interval
        self.duration = duration
        self.max_cycles = max_cycles

        if mode == "cycle":
            self.link_mode = "cycle"
        elif mode == "persistent":
            self.link_mode = "persistent"
        else:
            self.link_mode = "persistent" if link == "auto" else link

        self.output_dir = Path(hass.config.path("www", SLEEP_LOG_WWW_DIR))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = Path(output).name if output else f"sleep-{stamp}.jsonl"
        if not filename.endswith(".jsonl"):
            filename += ".jsonl"

        self.jsonl_path = self.output_dir / filename
        self.csv_path = self.jsonl_path.with_suffix(".csv") if csv_enabled else None
        self.summary_path = self.jsonl_path.with_name(
            self.jsonl_path.stem + "-summary.json"
        )

        base_url = f"/local/{SLEEP_LOG_WWW_DIR}"
        self.jsonl_url = f"{base_url}/{self.jsonl_path.name}"
        self.csv_url = f"{base_url}/{self.csv_path.name}" if self.csv_path else None
        self.summary_url = f"{base_url}/{self.summary_path.name}"

        self.status: dict[str, Any] = {
            "state": "idle",
            "mode": self.mode,
            "link": self.link_mode,
            "interval": self.interval,
            "duration": self.duration,
            "cycles": 0,
            "last_ts": None,
            "last_vehicle_response": None,
            "last_atrv_v": None,
            "output": self.jsonl_url,
            "summary_url": self.summary_url,
            "error": None,
        }
        self.summary: dict[str, Any] | None = None

        self._task: asyncio.Task | None = None
        self._api: Connection | None = None
        self._device: Any = None
        self._records: list[dict[str, Any]] = []
        self._jsonl_file = None
        self._csv_file = None
        self._csv_writer: csv.DictWriter | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            raise RuntimeError("A sleep log is already running")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl_file = self.jsonl_path.open("w", encoding="utf-8")
        if self.csv_path:
            self._csv_file = self.csv_path.open("w", newline="", encoding="utf-8")
            self._csv_writer = csv.DictWriter(
                self._csv_file, fieldnames=FIELDS, extrasaction="ignore"
            )
            self._csv_writer.writeheader()

        self.status["state"] = "running"
        self.status["started"] = utcnow()
        self._task = self.hass.async_create_task(self._run())
        await asyncio.sleep(0)

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("Sleep logger stopped with an error")
        finally:
            self._task = None
            if self.status.get("state") == "running":
                self.status["state"] = "stopped"

    async def _async_close_api(self) -> None:
        api = self._api
        self._api = None
        if api is not None:
            try:
                await asyncio.shield(self.hass.async_add_executor_job(close_api, api))
            except Exception:
                _LOGGER.debug("Error closing sleep logger API", exc_info=True)

    async def _run(self) -> None:
        started_mono = time.monotonic()
        deadline = (
            started_mono + self.duration * 3600 if self.duration > 0 else None
        )
        cycle = 0

        try:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if self.max_cycles and cycle >= self.max_cycles:
                    break

                cycle += 1
                cycle_start = time.monotonic()
                record: dict[str, Any] = {field: None for field in FIELDS}
                record.update(
                    {
                        "ts": utcnow(),
                        "mono": round(cycle_start - started_mono, 3),
                        "cycle": cycle,
                        "mode": self.mode,
                        "link": self.link_mode,
                        "connected": False,
                        "errors": [],
                    }
                )

                connect_start: float | None = None
                try:
                    if self._api is None or not self._api.is_connected():
                        await self._async_close_api()
                        if self._device is None:
                            lookup_start = time.monotonic()
                            self._device = bluetooth.async_ble_device_from_address(
                                self.hass, self.address, True
                            )
                            record["scan_ms"] = round(
                                (time.monotonic() - lookup_start) * 1000
                            )
                            if self._device is None:
                                raise RuntimeError(
                                    "dongle not found in the Bluetooth cache"
                                )

                        connect_start = time.monotonic()
                        self._api = build_api(self._device, self.hass.loop, 6.0)
                        await self.hass.async_add_executor_job(
                            connect_api, self._api, "32"
                        )
                        record["connect_ms"] = round(
                            (time.monotonic() - connect_start) * 1000
                        )

                    record["connected"] = True
                    poll_start = time.monotonic()
                    fragment = await self.hass.async_add_executor_job(
                        poll, self._api, self.mode
                    )
                    record["poll_ms"] = round((time.monotonic() - poll_start) * 1000)
                    merge_record(record, fragment)
                except Exception as exc:
                    record["connected"] = False
                    if connect_start is not None and record["connect_ms"] == 0:
                        record["connect_ms"] = round(
                            (time.monotonic() - connect_start) * 1000
                        )
                    record["errors"].append(f"{type(exc).__name__}: {exc}")
                    await self._async_close_api()
                    self._device = None
                finally:
                    if self.link_mode == "cycle" or record.get("transport_error"):
                        await self._async_close_api()
                        if record.get("transport_error"):
                            self._device = None

                record["cycle_ms"] = round((time.monotonic() - cycle_start) * 1000)
                self._write_record(record)

                elapsed = time.monotonic() - cycle_start
                await asyncio.sleep(max(0.0, self.interval - elapsed))
        except asyncio.CancelledError:
            self.status["state"] = "stopped"
            raise
        except Exception as exc:
            self.status["state"] = "error"
            self.status["error"] = f"{type(exc).__name__}: {exc}"
            _LOGGER.exception("Sleep logger failed")
        finally:
            await self._async_cleanup()

    def _write_record(self, record: dict[str, Any]) -> None:
        self._records.append(record)
        if self._jsonl_file is not None:
            self._jsonl_file.write(json.dumps(record, default=str) + "\n")
            self._jsonl_file.flush()
        if self._csv_writer is not None and self._csv_file is not None:
            row = {
                key: (
                    "; ".join(record[key])
                    if key == "errors" and record[key]
                    else record[key]
                )
                for key in FIELDS
            }
            self._csv_writer.writerow(row)
            self._csv_file.flush()

        self.status.update(
            {
                "cycles": len(self._records),
                "last_ts": record["ts"],
                "last_atrv_v": record.get("atrv_v"),
            }
        )
        if record.get("vehicle_responding"):
            self.status["last_vehicle_response"] = record["ts"]

    async def _async_cleanup(self) -> None:
        await self._async_close_api()

        if self._jsonl_file is not None:
            self._jsonl_file.close()
            self._jsonl_file = None
        if self._csv_file is not None:
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None

        self.status["ended"] = utcnow()
        self.summary = summarize(
            self._records,
            self.mode,
            self.link_mode,
            self.interval,
            self.status.get("started"),
            self.status["ended"],
        )
        try:
            self.summary_path.write_text(
                json.dumps(self.summary, indent=2) + "\n", encoding="utf-8"
            )
        except Exception:
            _LOGGER.exception("Could not write sleep logger summary")

        if self.status.get("state") == "running":
            self.status["state"] = "complete"
