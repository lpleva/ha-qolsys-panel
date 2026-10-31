"""QolsysZone.is_on() and is_trouble(): our own version of upstream library 1.12.6.

Written for the integration's 687ab08 (#170) without taking the 1.12.6 library,
whose arm() still has the audit L1 safety-zone guard commented out.
"""

from unittest.mock import MagicMock

import pytest

from custom_components.qolsys_panel.vendor.qolsys_controller.enum_qolsys import (
    ConnectionTroubleZoneStatus,
    OnZoneStatus,
    TroubleZoneStatus,
    ZoneStatus,
)
from custom_components.qolsys_panel.vendor.qolsys_controller.zone import QolsysZone

# Every ZoneStatus the library knows, with the two answers we expect for it.
# A status missing from this table fails test_every_status_is_classified, so a
# new panel value forces a decision instead of silently reading closed/connected.
CLASSIFICATION: dict[ZoneStatus, tuple[bool, bool]] = {
    # status: (is_on, is_trouble)
    ZoneStatus.ACTIVE: (True, False),
    ZoneStatus.ACTIVATED: (True, False),
    ZoneStatus.ALARMED: (True, False),
    ZoneStatus.ARM_AWAY: (False, False),
    ZoneStatus.ARM_STAY: (False, False),
    ZoneStatus.AUXILIARY_EMERGENCY: (False, False),
    ZoneStatus.BELL_TROUBLE: (False, False),
    ZoneStatus.CLOSED: (False, False),
    ZoneStatus.CONNECTED: (False, False),
    ZoneStatus.DISARM: (False, False),
    ZoneStatus.FAILURE: (False, True),
    ZoneStatus.FIRE_EMERGENCY: (False, False),
    ZoneStatus.OPEN: (True, False),
    ZoneStatus.OCCUPIED: (True, False),
    ZoneStatus.POLICE_EMERGENCY: (False, False),
    ZoneStatus.INACTIVE: (False, False),
    ZoneStatus.IDLE: (False, False),
    ZoneStatus.NORMAL: (False, False),
    ZoneStatus.NOT_NETWORKED: (False, True),
    ZoneStatus.UNREACHABLE: (False, True),
    ZoneStatus.SILENT_POLICE_EMERGENCY: (False, False),
    ZoneStatus.SILENT_AUXILIARY_EMERGENCY: (False, False),
    ZoneStatus.TAMPERED: (False, False),
    ZoneStatus.SYNCHRONIZING: (False, True),
    ZoneStatus.DISCONNECTED: (False, True),
    ZoneStatus.HIGH_TEMP_ALERT: (False, False),
    ZoneStatus.TROUBLE: (False, True),
    ZoneStatus.UNKNOWN: (False, False),
    ZoneStatus.VACANT: (False, False),
}


def _zone(status: str) -> QolsysZone:
    """A real door/window zone built from a panel row; the status goes in at construction.

    Built from the row, not through the sensorstatus setter: the setter starts a
    motion-delay task for motion types and this is a plain door, but keeping the
    test off the setter means it tests the classification and nothing else.
    """
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


@pytest.mark.parametrize(("status", "expected"), sorted(CLASSIFICATION.items()))
def test_is_on_and_is_trouble(status: ZoneStatus, expected: tuple[bool, bool]) -> None:
    """Each status gives the expected (is_on, is_trouble) pair on a real zone."""
    zone = _zone(status.value)
    assert zone.sensorstatus is status
    assert (zone.is_on(), zone.is_trouble()) == expected


def test_every_status_is_classified() -> None:
    """The table above names every ZoneStatus, so a new value cannot slip through."""
    assert set(CLASSIFICATION) == set(ZoneStatus)


def test_the_two_sets_match_the_table_and_do_not_overlap() -> None:
    """The exported sets are exactly the table's True columns, and no status is both."""
    assert OnZoneStatus == {s for s, (on, _) in CLASSIFICATION.items() if on}
    assert ConnectionTroubleZoneStatus == {s for s, (_, trouble) in CLASSIFICATION.items() if trouble}
    assert not (OnZoneStatus & ConnectionTroubleZoneStatus)


def test_a_lost_sensor_reads_closed_not_open() -> None:
    """The visible change: a door sensor that drops off the panel is closed, with trouble set."""
    lost = _zone("disconnected")
    assert lost.is_on() is False
    assert lost.is_trouble() is True

    unreachable = _zone("Unreachable")
    assert unreachable.is_on() is False
    assert unreachable.is_trouble() is True


def test_an_unknown_panel_value_is_neither_on_nor_trouble() -> None:
    """A status string the library has never seen parses to UNKNOWN and raises no flag."""
    zone = _zone("Something New")
    assert zone.sensorstatus is ZoneStatus.UNKNOWN
    assert zone.is_on() is False
    assert zone.is_trouble() is False


def test_arm_readiness_list_is_unchanged() -> None:
    """TroubleZoneStatus feeds the arm() guard (audit L1); the new sets did not touch it."""
    assert TroubleZoneStatus == [
        ZoneStatus.OPEN,
        ZoneStatus.TAMPERED,
        ZoneStatus.FAILURE,
        ZoneStatus.SYNCHRONIZING,
        ZoneStatus.NOT_NETWORKED,
    ]
