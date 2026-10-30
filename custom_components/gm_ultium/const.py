"""Constants for the GM Ultium integration.

This integration is derived from dala318/obd2_ble (MIT). See README.md for
attribution and the upstream project link.
"""

# Base component constants
from typing import Final

from homeassistant.const import Platform

NAME = "GM Ultium"
DOMAIN = "gm_ultium"
DOMAIN_DATA = f"{DOMAIN}_data"

ATTRIBUTION = "Based on dala318/obd2_ble (MIT)"
ISSUE_URL = "https://github.com/Coldarn/hacs_gm_ultium/issues"

# Platforms
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

ACTION_ATTEMPT_CONNECT = "attempt_to_connect"

# Configuration and options
CONF_AUTO_CONFIGURE = "auto_configure"
CONF_CHARACTERISTIC_UUID_READ = "characteristic_uuid_read"
CONF_CHARACTERISTIC_UUID_WRITE = "characteristic_uuid_write"
CONF_PROTOCOL = "protocol"
CONF_ENABLED = "enabled"
CONF_HW_VERSION = "hw_version"

CONF_CACHED_VALUES = "cached_values"
CONF_SLOW_POLL = "slow_poll"
CONF_FAST_POLL = "fast_poll"
CONF_XS_POLL = "xs_poll"

CONF_COMMANDS = "commands"
CONF_COMMAND = "command"
CONF_ICON = "icon"
CONF_UNIT = "unit"
CONF_DEVICE_CLASS = "device_class"
CONF_STATE_CLASS = "state_class"

# Defaults
DEFAULT_NAME = DOMAIN
DEFAULT_CHARACTERISTIC_UUID_READ = "0000fff1-0000-1000-8000-00805f9b34fb"
DEFAULT_CHARACTERISTIC_UUID_WRITE = "0000fff2-0000-1000-8000-00805f9b34fb"
DEFAULT_CACHED_VALUES = False
# when the device is in range, and the car is on, poll quickly
DEFAULT_FAST_POLL = 10
# when the device is in range, but the car is off, we need to poll occasionally
DEFAULT_SLOW_POLL = 5 * 60
# when the device is out of range, use ultra slow polling since a bluetooth
# advertisement message will kick it back into life when back in range.
DEFAULT_XS_POLL = 1 * 60 * 60

ICON_KEYWORDS: Final[dict[str, str]] = {
    # --- Speed & Rotations ---
    "rpm": "mdi:engine",
    "speed": "mdi:speedometer",
    "velocity": "mdi:speedometer",

    # --- Temperature Metrics ---
    "temp": "mdi:thermometer",
    "temperature": "mdi:thermometer",
    "coolant": "mdi:thermometer",

    # --- Electrical / Battery Systems ---
    "voltage": "mdi:sine-wave",
    "volt": "mdi:sine-wave",
    "v": "mdi:sine-wave",
    "battery": "mdi:battery",
    "current": "mdi:current-ac",

    # --- Pressures & Gauges ---
    "pressure": "mdi:gauge",
    "bar": "mdi:gauge",
    "psi": "mdi:gauge",
    "kpa": "mdi:gauge",
    "vacuum": "mdi:gauge-empty",

    # --- Fuel & Air Dynamics ---
    "fuel": "mdi:gas-station",
    "ethanol": "mdi:gas-station",
    "rate": "mdi:gas-station-outline",       # e.g., FUEL_RATE
    "level": "mdi:water-percent",            # e.g., FUEL_LEVEL_INPUT_A_B
    "ratio": "mdi:aspect-ratio",             # e.g., AIR_FUEL_EQUIV_RATIO
    "equivalence": "mdi:aspect-ratio",
    "maf": "mdi:air-filter",                 # Mass Air Flow
    "flow": "mdi:air-filter",
    "air": "mdi:air-conditioner",
    "throttle": "mdi:accelerator",           # Throttle positions
    "egr": "mdi:pipe-valve",                 # Exhaust Gas Recirculation

    # --- Exhaust, Emissions & Environment ---
    "sensor": "mdi:leak",                    # Generic fallback sensor
    "sensors": "mdi:leak",
    "o2": "mdi:molecule",                    # Oxygen sensors
    "nox": "mdi:smog",                       # NOx emissions metrics
    "particulate": "mdi:scooter",            # DPF (Diesel Particulate Filter)
    "dpf": "mdi:smoke-detector-alert",
    "catalyst": "mdi:factory",

    # --- Odometers, Timers & Distances ---
    "time": "mdi:clock-outline",
    "runtime": "mdi:timer-outline",
    "count": "mdi:counter",
    "counters": "mdi:counter",
    "distance": "mdi:map-marker-distance",
    "mil": "mdi:engine-outline",             # Malfunction Indicator Lamp distance
    "odometer": "mdi:counter",

    # --- Engine Loads & Ratios ---
    "load": "mdi:weight",
    "torque": "mdi:torque",
    "trim": "mdi:tune",                      # e.g., SHORT_TERM_FUEL_TRIM
    "trims": "mdi:tune",
    "advance": "mdi:angle-acute",            # Timing advance

    # --- Vehicle Metadata (Mode 09) ---
    "vin": "mdi:card-account-details",       # Vehicle Identification Number
    "id": "mdi:identifier",                  # Calibration IDs
    "cvn": "mdi:shield-check",               # Calibration Verification Number

    # --- Trouble Codes (Mode 03 / Mode 04) ---
    "dtc": "mdi:alert-octagon",              # Diagnostic Trouble Code strings
    "clear": "mdi:alert-octagon-check",      # Clear DTC command
}

STARTUP_MESSAGE = f"""
-------------------------------------------------------------------
{NAME}
This is a custom integration!
If you have any issues with this you need to open an issue here:
{ISSUE_URL}
-------------------------------------------------------------------
"""
