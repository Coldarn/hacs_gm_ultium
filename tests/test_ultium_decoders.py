"""Decoder tests using real responses captured from a 2024 BrightDrop."""

import sys
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "gm_ultium"
sys.path.insert(0, str(COMPONENT / "obdii"))

import ultium_commands as uc


def test_command_names_and_headers():
    names = {command.name for command in uc.ULTIUM_COMMANDS}
    assert names == set(uc.ULTIUM_COMMAND_CAN_HEADERS)
    assert all(len(header) == 6 for header in uc.ULTIUM_COMMAND_CAN_HEADERS.values())


def test_soc_high_resolution():
    # 27C6 -> AC B2; raw 67.460... is rounded to 3 significant figures.
    value = uc.GMUltiumCommands.STATE_OF_CHARGE.resolver([0xAC, 0xB2])
    assert value == pytest.approx(67.5, abs=0.01)


def test_soc_rounds_to_three_significant_figures():
    assert uc._sig_figs(66.9611657892729, 3) == pytest.approx(67.0)
    assert uc._sig_figs(67.46063172350652, 3) == pytest.approx(67.5)
    assert uc._sig_figs(6.69611657892729, 3) == pytest.approx(6.7)


def test_energy_remaining():
    # 27AF -> 2D 8D
    value = uc.GMUltiumCommands.ENERGY_REMAINING.resolver([0x2D, 0x8D])
    assert value == pytest.approx(116.61, abs=0.01)


def test_pack_voltage():
    # 2885 -> 7A 03
    value = uc.GMUltiumCommands.PACK_VOLTAGE.resolver([0x7A, 0x03])
    assert value == pytest.approx(312.35, abs=0.01)


def test_pack_current_signed():
    # 2414 -> 00 12 = +0.9 A (discharging)
    assert uc.GMUltiumCommands.PACK_CURRENT.resolver([0x00, 0x12]) == pytest.approx(0.9)
    # FE 39 = -455 / 20 = -22.75 A (charging), from the Lyriq/Hummer reference
    assert uc.GMUltiumCommands.PACK_CURRENT.resolver([0xFE, 0x39]) == pytest.approx(-22.75)


def test_cell_voltages():
    payload = [0x98, 0x7D, 0x98, 0x68, 0x98, 0x85, 0x57, 0x0B, 0x51, 0x0B]
    assert uc.GMUltiumCommands.CELL_VOLTAGE_AVG.resolver(payload) == pytest.approx(3.9037, abs=1e-4)
    assert uc.GMUltiumCommands.CELL_VOLTAGE_MIN.resolver(payload) == pytest.approx(3.9016, abs=1e-4)
    assert uc.GMUltiumCommands.CELL_VOLTAGE_MAX.resolver(payload) == pytest.approx(3.9045, abs=1e-4)


def test_charge_state_and_12v():
    assert uc.GMUltiumCommands.CHARGE_STATE.resolver([0x00]) == "idle"
    assert uc.GMUltiumCommands.CHARGE_STATE.resolver([0x0D]) == "charging"
    assert uc.GMUltiumCommands.CHARGE_STATE.resolver([0x91]) == "charging"
    assert uc.GMUltiumCommands.CHARGE_STATE.resolver([0x50]) == "0x50"
    assert uc.GMUltiumCommands.LOW_VOLTAGE.resolver([0x88]) == pytest.approx(13.6)


def test_range_matches_dashboard():
    # 27C7 -> 00 52 BF; dashboard showed 206 mi
    value = uc.GMUltiumCommands.RANGE_MILES.resolver([0x00, 0x52, 0xBF])
    assert value == pytest.approx(205.66, abs=0.01)


def test_module_temperatures():
    payload = [0x5A, 0x59, 0x59, 0x5A, 0x5A, 0x5A]
    assert uc.GMUltiumCommands.MODULE_TEMP_MIN.resolver(payload) == pytest.approx(24.5)
    assert uc.GMUltiumCommands.MODULE_TEMP_MAX.resolver(payload) == pytest.approx(25.0)


def test_short_payloads_return_none():
    assert uc.GMUltiumCommands.PACK_CURRENT.resolver([]) is None
    assert uc.GMUltiumCommands.CELL_VOLTAGE_MAX.resolver([0x98]) is None
    assert uc.GMUltiumCommands.RANGE_MILES.resolver([0x00]) is None
