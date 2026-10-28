"""Zone sensor groups the vendored library knows (upstream 1.12.5 added garageTilt2)."""

import logging
from unittest.mock import MagicMock

import pytest

from custom_components.qolsys_panel.vendor.qolsys_controller.enum_qolsys import ZoneSensorGroup
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
