"""Bluetooth advertisement matching tests using a real VEEPEAK advertisement."""

import sys
from pathlib import Path
from types import SimpleNamespace

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "gm_ultium"
sys.path.insert(0, str(COMPONENT / "obdii"))

import transport_ble_identifiers as ids

VEEPEAK_ADV = SimpleNamespace(
    local_name="VEEPEAK",
    service_uuids=["0000fff0-0000-1000-8000-00805f9b34fb"],
    service_data={},
    manufacturer_data={},
)


def test_veepeak_matches_generic_fff0_profile():
    matches = [
        ids.advertisement_matches(matcher, VEEPEAK_ADV, "66:1E:87:05:B3:41")
        for matcher in ids.OBD2_BLE.matcher_dict_list()
    ]
    assert any(matches)


def test_vlink_profile_does_not_match_veepeak():
    matches = [
        ids.advertisement_matches(matcher, VEEPEAK_ADV, "66:1E:87:05:B3:41")
        for matcher in ids.VlinkOBD2_BLE.matcher_dict_list()
    ]
    assert not any(matches)
