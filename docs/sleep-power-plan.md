# Sleep and power measurement plan

The integration's sleep/wake strategy should be driven by measurements, not
guesses. `tools/gm_ultium_sleep_log.py` collects the data needed to choose the
polling interval, the reconnect mode, and the 12 V guard threshold.

## What the logger records

Per cycle, as JSONL and optional CSV:

- `ts`, monotonic time, cycle number, mode
- `scan_ms`, `connect_ms`, `poll_ms`, `cycle_ms`
- `atrv_v` — the dongle's own supply voltage (`ATRV`)
- `atign` — the adapter's ignition input (`ATIGN`)
- `vehicle_responding` — whether the BEC answered the SOC probe
- `soc_pct`, `energy_kwh`, `battery_12v_v`, `charge_state`
- `pack_v`, `pack_a`, derived `power_kw`
- `transport_error`, `errors`

The summary adds first/last vehicle response, longest no-response run, ATRV
range, and average scan/connect/poll/cycle timings.

## Important limitation

The logger cannot measure current draw. Use a clamp meter or a 12 V power
supply with a current display on the OBD adapter's supply wire. Record the
dongle current in four states:

1. unplugged (baseline vehicle drain without the dongle)
2. plugged in, BLE disconnected
3. plugged in, BLE connected, no queries
4. plugged in and polling at the chosen interval

Multiply by 12 V and 24 h to get Wh/day. The vehicle-awake cost is usually much
larger than the dongle itself, which is why the response-time data matters.

## Preparation

- Stop Car Scanner and any other BLE client. Only one central can hold the
  dongle.
- Run the logger on a machine that will not suspend, within BLE range, with the
  dongle plugged into the van.
- On Linux, prevent suspend for the run:

  ```bash
  systemd-inhibit --what=sleep:idle --why="OBD sleep log" \
    python tools/gm_ultium_sleep_log.py ...
  ```

- On macOS, `caffeinate -i python tools/gm_ultium_sleep_log.py ...`.
- Use `tmux` or `nohup` if you need to disconnect your SSH session.
- The van should be parked in its normal state (not on a charger) for the
  baseline runs.

## Three runs

Run each for 24 h with a 60 s interval and CSV output. Use separate files.

### 1. Dongle-only baseline

Only queries `ATRV`/`ATIGN`; does not touch the vehicle bus.

```bash
python tools/gm_ultium_sleep_log.py \
  --mode dongle-only --interval 60 --duration 24 --csv \
  --output logs/dongle-only.jsonl
```

This answers: how stable is the dongle, what does the 12 V supply do, and does
the adapter stay connected without waking the car?

### 2. Connect / poll / disconnect

```bash
python tools/gm_ultium_sleep_log.py \
  --mode cycle --interval 60 --duration 24 --csv \
  --output logs/cycle-60s.jsonl
```

This measures reconnect reliability and how long the vehicle stays awake after
each poll. It is the closest match to a power-saving HA design.

### 3. Persistent link

```bash
python tools/gm_ultium_sleep_log.py \
  --mode persistent --interval 60 --duration 24 --csv \
  --output logs/persistent-60s.jsonl
```

This measures whether keeping the BLE link open changes the vehicle's sleep
behavior or the dongle's own consumption.

## Optional: charge session

Run a shorter log while the van is charging, at 30 s:

```bash
python tools/gm_ultium_sleep_log.py \
  --mode persistent --interval 30 --duration 3 --csv \
  --output logs/charge-session.jsonl
```

This validates the `5401` charge-state values, the current sign, and the
derived power against the charger's display.

## What to look for

- **`last_vehicle_response`** — when the BEC stopped answering after
  ignition-off.
- **`longest_no_response_run` / `_s`** — how long the vehicle stayed silent.
- **`atrv_min_v` / `atrv_max_v`** and the slope over the run — 12 V health.
- **`connected_cycles` vs `cycles`** — reconnect reliability.
- **`avg_scan_ms`, `avg_connect_ms`, `avg_poll_ms`** — duty cycle.
- **Cycle vs persistent** — does the persistent run keep
  `vehicle_responding` true longer? If yes, the BLE link itself is keeping
  something awake.
- **`atign`** — does it reliably report `OFF` when the vehicle is off? If it
  does, it is a cheap state signal.
- **`transport_errors`** — whether timeouts correlate with vehicle sleep or
  BLE instability.

## Decision criteria for the integration

- If the vehicle stops answering within a few minutes of ignition-off, use a
  short probe and exponential backoff rather than a fixed 60 s poll all night.
- If a persistent link keeps the vehicle awake, disconnect after each poll and
  reconnect on the next cycle.
- If reconnect is unreliable, keep the link open but lengthen the parked
  interval, or use a local bridge that owns the dongle.
- Set the 12 V guard from the measured `ATRV` floor, not from a generic number.
  A reasonable starting point is 12.0–12.2 V, but confirm against the van.
- Choose the fast interval from the charge-session log and the slow/deep
  intervals from the parked log.

## Files

Each run writes:

- `*.jsonl` — one record per cycle, append-only
- `*.csv` — same data for spreadsheets
- `*-summary.json` — aggregate metrics

Keep the raw JSONL; it is the evidence for the final strategy.
