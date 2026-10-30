# GM Ultium for Home Assistant

Read battery state from GM Ultium (BT1/BEV3) vehicles over a BLE OBD-II
dongle, without the manufacturer app.

This project is derived from
[dala318/obd2_ble](https://github.com/dala318/obd2_ble) (MIT). The BLE
transport, config flow, adaptive polling, and HACS packaging come from that
project. The GM Ultium command set, 29-bit header handling, and validated
signal definitions are new here.

> **Status:** the protocol layer and standalone CLI are validated live against
> a 2024 BrightDrop Zevo with a VEEPEAK BLE dongle (ELM327 v1.5). The Home
> Assistant entity layer is the next milestone.

## Validated signals

All values below were read live through the OBD-II port. Requests use UDS
service `0x22` (read-only) over 29-bit CAN at 500 kbit.

| Signal | ECU | Request | Decode | Live value |
|---|---|---|---|---|
| State of charge | BEC `0xCB` | `22 27C6` | `u16 × 100 / 65535` | 67.46 % |
| Energy remaining | BEC `0xCB` | `22 27AF` | `u16 / 100` | 116.58 kWh |
| Cell voltage avg/min/max | BEC `0xCB` | `22 2AF5` | `u16 / 10000` | 3.9035 / 3.9014 / 3.9043 V |
| Module temperature min/max | BEC `0xCB` | `22 2AF1` | `(byte − 40) / 2` (experimental) | 24.5 / 25.0 °C |
| Range | BEC `0xCB` | `22 27C7` | `u24 / 103` miles | 205 mi |
| Charge state | BEC `0xCB` | `22 5401` | `0x00` idle, `0x91–0x99` charging | idle |
| Pack voltage | DMCM `0x17` | `22 2885` | `u16 / 100` | 312.35 V |
| Pack current | DMCM `0x17` | `22 2414` | `s16 / 20` A, positive = discharge | +0.90 A |
| 12 V rail | BEC `0xCB` | `22 33E5` | `byte / 10` | 13.6 V |

Derived in software:

- **Battery power** = `−(pack voltage × pack current) / 1000` kW.
  Positive means charging, negative means discharging.
- **Estimated usable capacity** = `energy remaining ÷ (SOC / 100)` kWh.
  The live sample gives 172.8 kWh, consistent with the pack size.
- **Charging** = power above a small threshold, negative current, or charge
  state `0x91–0x99`.

The dashboard SOC (67 %) matches `27C6` (67.46 %, truncated by the dash), so
`27C6` is the canonical SOC. `2B43` is intentionally not used: the BEC emits a
malformed ISO-TP length for that 29-byte response on this vehicle.

## Hardware and protocol notes

- Dongle: VEEPEAK BLE (`FFF0` service, `FFF2` write, `FFF1` notify).
- Adapter: ELM327 v1.5 clone reporting `ATDPN 7` (ISO 15765-4 CAN, 29-bit,
  500 kbit).
- **This clone rejects the 4-byte `ATSH 18DACBF1` form.** Use the 3-byte form
  `ATSH DACBF1` with `ATSP7`; the adapter supplies the 29-bit priority.
- `py-obdii` must receive **hex strings**, not integers, when building `ATSH`.
  Integers are formatted as decimal and the adapter replies `?`.
- Only one BLE central can hold the dongle. Stop Car Scanner (or any other OBD
  app) before running Home Assistant or the CLI.

## Standalone CLI

This is the fastest way to verify the vehicle and dongle. It uses the same
`TransportBLE` + `py-obdii` stack as the integration.

```bash
python3 -m venv .venv
.venv/bin/pip install py-obdii==0.10.5b0 bleak bleak-retry-connector

.venv/bin/python tools/gm_ultium_cli.py
.venv/bin/python tools/gm_ultium_cli.py --json
.venv/bin/python tools/gm_ultium_cli.py AA:BB:CC:DD:EE:FF
```

Example output:

```
GM Ultium / BrightDrop
----------------------
SOC                    67.46 %
Energy remaining       116.58 kWh
Estimated capacity     172.8 kWh
Pack voltage           312.35 V
Pack current           0.90 A (positive = discharge)
Battery power          -0.28 kW (discharging)
Cell voltage (a/m/M)   3.9035 V / 3.9014 V / 3.9043 V
Module temp (min/max)  24.5 / 25.0 C
Range                  205.0 mi / 330.0 km
Charge state           idle (0x00)
12 V rail              13.6 V
```

## Sleep and power logging

The OBD-II port is permanently powered, and polling can keep the vehicle's
gateway/BEC awake. `tools/gm_ultium_sleep_log.py` records `ATRV`, `ATIGN`, the
vehicle response state, SOC, pack voltage/current, and timing over long runs so
the polling strategy can be chosen from data.

```bash
# 6-cycle smoke test
.venv/bin/python tools/gm_ultium_sleep_log.py \
  --mode cycle --interval 10 --duration 0 --max-cycles 6 --csv

# 24 h baseline without touching the vehicle bus
.venv/bin/python tools/gm_ultium_sleep_log.py \
  --mode dongle-only --interval 60 --duration 24 --csv

# 24 h connect/poll/disconnect
.venv/bin/python tools/gm_ultium_sleep_log.py \
  --mode cycle --interval 60 --duration 24 --csv

# 24 h persistent BLE link
.venv/bin/python tools/gm_ultium_sleep_log.py \
  --mode persistent --interval 60 --duration 24 --csv
```

See [docs/sleep-power-plan.md](docs/sleep-power-plan.md) for the clamp-meter
measurements, the three-run comparison, and the decision criteria.

## Home Assistant installation

The component is laid out as a HACS custom integration:

```text
custom_components/gm_ultium/
tools/gm_ultium_cli.py
tests/
```

1. Create a GitHub repository for this fork and replace `your-github-user` in
   `custom_components/gm_ultium/manifest.json`.
2. Add the repository to HACS as a custom integration repository.
3. Install **GM Ultium**, restart Home Assistant, and add the integration.
4. Select the VEEPEAK device (or enter its MAC address).

The integration discovers `VEEPEAK*` advertisements and defaults the write
characteristic to `FFF2`. The entity layer (SOC, remaining charge, estimated
capacity, signed power, charging, cell voltages, range, 12 V) is being built on
top of the validated command set.

## Power and sleep behavior

The OBD-II port is permanently powered on these vehicles. The dongle itself
draws only a few tens of milliamps, but polling can keep the vehicle's gateway
and BEC awake, which costs far more. The integration therefore needs a
sleep/wake strategy, not just a fixed poll interval:

- fast polling while charging or ignition-on,
- slow polling while parked,
- deep sleep with a cheap `ATRV` probe when the vehicle stops answering,
- a 12 V guard that stops vehicle queries below a safe threshold.

The exact thresholds need measurement on the vehicle; the CLI is the tool for
collecting that data.

## Safety

- Read-only: the integration only uses OBD services `01`, `09`, and `22`.
- No DTC clearing, routines, actuator tests, or writes.
- Do not send arbitrary Mode 22 DIDs without checking the service semantics.

## Development

```bash
# decoder tests (no vehicle needed)
.venv/bin/python -m pytest -q -o addopts="" tests/

# live protocol validation
.venv/bin/python tools/gm_ultium_cli.py
```

## Attribution and license

MIT. See [LICENSE](LICENSE).

Based on [dala318/obd2_ble](https://github.com/dala318/obd2_ble),
copyright (c) 2026 DanielV. The upstream project supplies the BLE transport,
config flow, coordinator, and HACS structure.
