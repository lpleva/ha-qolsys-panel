"""QolsysZone availability helpers: our own version of upstream library 1.13.3b3.

Upstream integration cb11be0 (#183/#184) reads four helpers from the library to
decide when a zone's entities are unavailable. This fork vendors the library,
so the helpers are written into our copy (vendor/VENDORED.md) with the same
meaning as upstream's 1.13.3b3:

- is_tampered: the panel reports the zone Tampered (cover off).
- is_available_reading: the zone's own readings (battery, AC, dBm, PowerG) are
  current, i.e. the panel has a working link to the sensor (not is_trouble).
- is_available_status: the open/closed state is known: not in trouble, not
  tampered and not Unknown, because trouble and tamper are reported in the same
  field that otherwise carries Open/Closed.
- is_available_tamper: the tamper state is known: tampered, or the status is.
"""

from unittest.mock import MagicMock

import pytest

from custom_components.qolsys_panel.vendor.qolsys_controller.enum_qolsys import (
    ConnectionTroubleZoneStatus,
    ZoneStatus,
)
from custom_components.qolsys_panel.vendor.qolsys_controller.zone import QolsysZone

TROUBLE = {
    ZoneStatus.UNREACHABLE,
    ZoneStatus.FAILURE,
    ZoneStatus.NOT_NETWORKED,
    ZoneStatus.TROUBLE,
    ZoneStatus.SYNCHRONIZING,
    ZoneStatus.DISCONNECTED,
}

# status: (is_tampered, is_available_reading, is_available_status, is_available_tamper)
# A status missing here fails test_every_status_is_in_the_table.
TABLE: dict[ZoneStatus, tuple[bool, bool, bool, bool]] = {
    **{s: (False, False, False, False) for s in TROUBLE},
    ZoneStatus.TAMPERED: (True, True, False, True),
    ZoneStatus.UNKNOWN: (False, True, False, False),
    **{
        s: (False, True, True, True)
        for s in (
            ZoneStatus.ACTIVE,
            ZoneStatus.ACTIVATED,
            ZoneStatus.ALARMED,
            ZoneStatus.ARM_AWAY,
            ZoneStatus.ARM_STAY,
            ZoneStatus.AUXILIARY_EMERGENCY,
            ZoneStatus.BELL_TROUBLE,
            ZoneStatus.CLOSED,
            ZoneStatus.CONNECTED,
            ZoneStatus.DISARM,
            ZoneStatus.FIRE_EMERGENCY,
            ZoneStatus.OPEN,
            ZoneStatus.OCCUPIED,
            ZoneStatus.POLICE_EMERGENCY,
            ZoneStatus.INACTIVE,
            ZoneStatus.IDLE,
            ZoneStatus.NORMAL,
            ZoneStatus.SILENT_POLICE_EMERGENCY,
            ZoneStatus.SILENT_AUXILIARY_EMERGENCY,
            ZoneStatus.HIGH_TEMP_ALERT,
            ZoneStatus.VACANT,
        )
    },
}


def _zone(status: str) -> QolsysZone:
    """A real door/window zone built from a panel row."""
    return QolsysZone(
        {
            "zoneid": "4",
            "sensorname": "Front Door",
            "sensorstatus": status,
            "sensorgroup": "entryexitdelay",
            "sensortype": "Door_Window",
            "partition_id": "1",
        },
        MagicMock(),
    )


def test_every_status_is_in_the_table() -> None:
    """A new ZoneStatus forces a decision here instead of a silent default."""
    assert set(TABLE) == set(ZoneStatus)


def test_trouble_set_is_the_librarys() -> None:
    """The table's trouble rows are exactly the set is_trouble() reads."""
    assert TROUBLE == set(ConnectionTroubleZoneStatus)


@pytest.mark.parametrize(("status", "expected"), sorted(TABLE.items()))
def test_availability_helpers(
    status: ZoneStatus, expected: tuple[bool, bool, bool, bool]
) -> None:
    """Each status gives upstream 1.13.3b3's answer on a real zone."""
    zone = _zone(status.value)
    assert zone.sensorstatus is status
    assert (
        zone.is_tampered(),
        zone.is_available_reading(),
        zone.is_available_status(),
        zone.is_available_tamper(),
    ) == expected


def test_unknown_panel_value_hides_the_open_closed_state() -> None:
    """A status string the library has never seen parses to Unknown: state unknown."""
    zone = _zone("Something New")
    assert zone.sensorstatus is ZoneStatus.UNKNOWN
    assert zone.is_available_status() is False
    assert zone.is_available_reading() is True
