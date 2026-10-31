"""Zone sensor groups the vendored library knows (upstream 1.12.5 added garageTilt2), and which are safety zones."""

import logging
from unittest.mock import MagicMock

import pytest

from custom_components.qolsys_panel.vendor.qolsys_controller.enum_qolsys import (
    SafetyZoneSensorGroup,
    ZoneSensorGroup,
)
from custom_components.qolsys_panel.vendor.qolsys_controller.zone import QolsysZone

ZONE_LOGGER = "custom_components.qolsys_panel.vendor.qolsys_controller.zone"


def _zone(sensorgroup: str) -> QolsysZone:
    return QolsysZone(
        {
            "zoneid": "6",
            "sensorname": "Garage Overhead Door",
            "sensorstatus": "Closed",
            "sensorgroup": sensorgroup,
            "sensortype": "Tilt",
            "partition_id": "0",
        },
        MagicMock(),
    )


@pytest.mark.parametrize("group", ["garageTilt1", "garageTilt2"])
def test_garage_tilt_groups_are_known(group: str, caplog) -> None:
    """Both garage tilt groups parse, read as their enum name and are not safety zones."""
    zone = _zone("entryexitdelay")
    with caplog.at_level(logging.DEBUG, logger=ZONE_LOGGER):
        # The setter is where an unknown value logs an exception (a panel update
        # path); the constructor stores the raw string without checking it.
        zone.sensorgroup = group
        name = zone._safe_sensor_group()

    assert ZoneSensorGroup(group).value == group
    assert name == ZoneSensorGroup(group).name.lower()
    assert zone.is_safety() is False
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_an_unknown_group_still_reports(caplog) -> None:
    """A group the library has never seen keeps logging, so a new panel value is noticed."""
    zone = _zone("garageTilt3")
    with caplog.at_level(logging.DEBUG, logger=ZONE_LOGGER):
        assert zone._safe_sensor_group() == "garageTilt3"

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == ["Unknown ZoneSensorGroup: garageTilt3, please report"]


# --- Which groups are safety zones (audit L1), decided one by one ------------
#
# is_safety() is what refuses an arm with a tripped zone and what the refusal
# text sorts zones by, so every group the library knows is classified here on
# purpose. A group added later fails test_every_group_is_classified until
# someone decides. The three marked "fork" were added 2026-10-31: the audit and
# README said the guard covered smoke, CO and water, and these are the groups
# the panel actually files those detectors under.

SAFETY_BY_GROUP: dict[ZoneSensorGroup, bool] = {
    # Upstream's six.
    ZoneSensorGroup.DELAYED_REPORTING_SAFETY_SENSOR: True,
    ZoneSensorGroup.DELAYER_REPORTING_SAFETY: True,  # same string as the line above
    ZoneSensorGroup.SAFETY_MOTION: True,
    ZoneSensorGroup.LOCAL_SAFETY_SENSOR: True,
    ZoneSensorGroup.REPORTING_SAFETY_SENSOR: True,
    ZoneSensorGroup.WATER_NON_REPORTING: True,
    ZoneSensorGroup.HIGH_TEMPERATURE_NON_REPORTING: True,
    # Fork: smoke/heat detectors, CO detectors, reporting water sensors.
    ZoneSensorGroup.SMOKE_HEAT: True,
    ZoneSensorGroup.CO: True,
    ZoneSensorGroup.WATER: True,
    # Environmental monitoring, not life safety: a cold or warm room is no
    # reason to refuse arming. Left as the panel has them.
    ZoneSensorGroup.FREEZE: False,
    ZoneSensorGroup.FREEZE_NON_REPORTING: False,
    ZoneSensorGroup.TEMPERATURE: False,
    ZoneSensorGroup.TEMPERATURE_CMS: False,
    # Panic buttons on key fobs, keypads and pendants: they raise an alarm,
    # they are never "open".
    ZoneSensorGroup.FIXED_INTRUSION: False,
    ZoneSensorGroup.FIXED_SILENT: False,
    ZoneSensorGroup.FIXED_AUXILIARY: False,
    ZoneSensorGroup.FIXED_SILENT_AUXILIARY: False,
    ZoneSensorGroup.MOBILE_INTRUSION: False,
    ZoneSensorGroup.MOBILE_SILENT: False,
    ZoneSensorGroup.MOBILE_AUXILIARY: False,
    ZoneSensorGroup.MOBILE_SILENT_AUXILIARY: False,
    # Devices, not sensors.
    ZoneSensorGroup.SIREN: False,
    ZoneSensorGroup.TAKEOVER_MODULE: False,
    ZoneSensorGroup.TRANSLATOR: False,
    # Intrusion: doors, windows, motion, glass, shock, tilt, tamper. The panel
    # bypasses these or arms over them exactly as before.
    ZoneSensorGroup.AWAY_DELAY_MOTION: False,
    ZoneSensorGroup.AWAY_INSTANT_MOTION: False,
    ZoneSensorGroup.AWAY_INSTANT_FOLLOWER_DELAY: False,
    ZoneSensorGroup.ENTRY_EXIT_NORMAL_DELAY: False,
    ZoneSensorGroup.ENTRY_EXIT_LONG_DELAY: False,
    ZoneSensorGroup.GARAGE_TILT_1: False,
    ZoneSensorGroup.GARAGE_TILT_2: False,
    ZoneSensorGroup.GLASS_BREAK: False,
    ZoneSensorGroup.GLASS_BREAK_AWAY_ONLY: False,
    ZoneSensorGroup.INSTANT_PERIMETER_DW: False,
    ZoneSensorGroup.INSTANT_INTERIOR_DOOR: False,
    ZoneSensorGroup.NIGHT_MOTION: False,
    ZoneSensorGroup.SHOCK: False,
    ZoneSensorGroup.SHOCK_AWAY_ONLY: False,
    ZoneSensorGroup.STAY_INSTANT_MOTION: False,
    ZoneSensorGroup.STAY_DELAY_MOTION: False,
    ZoneSensorGroup.TAMPER_ZONE: False,
}


@pytest.mark.parametrize(("group", "safety"), sorted(SAFETY_BY_GROUP.items()))
def test_is_safety_by_group(group: ZoneSensorGroup, safety: bool) -> None:
    """A real zone in each group answers is_safety() as the table says, and a safety zone is never bypassable."""
    zone = _zone(group.value)
    assert zone.is_safety() is safety
    if safety:
        assert zone.is_bypassable() is False


def test_every_group_is_classified() -> None:
    """The table names every ZoneSensorGroup, so a new group forces a decision."""
    assert set(SAFETY_BY_GROUP) == set(ZoneSensorGroup)


def test_safety_list_matches_the_table() -> None:
    """SafetyZoneSensorGroup is exactly the table's True rows, the three fork additions included."""
    assert set(SafetyZoneSensorGroup) == {g for g, s in SAFETY_BY_GROUP.items() if s}
    for added in (ZoneSensorGroup.SMOKE_HEAT, ZoneSensorGroup.CO, ZoneSensorGroup.WATER):
        assert added in SafetyZoneSensorGroup
