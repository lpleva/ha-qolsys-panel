"""Zones read unavailable instead of a misleading closed (our version of upstream cb11be0).

Rules Lukas approved on 2026-11-21:
Z1. A zone the panel reports in trouble (Failure, Unreachable, disconnected and
    the rest of is_trouble) or Tampered: its main entity (ZonesSensor) reads
    unavailable instead of closed/off.
Z2. While the zone is in trouble, its battery status, AC status, battery level,
    battery voltage, latest/average dBm, PowerG temperature and PowerG light
    entities read unavailable instead of the last value.
Z3. The Tamper and Status (connectivity) entities stay available and show why;
    Tamper is unavailable only while the tamper state itself is unknown.
Z4. When the zone reports Open/Closed again, every one of them comes back.
Z5. Unchanged: the keypad exception, every entity id and name, and a
    whole-panel disconnect (already unavailable). Arming reads the library's
    TroubleZoneStatus, which test_vendor_zone_status pins.

These tests use real vendored QolsysZone objects, so they exercise the
library helpers and the entities together.
"""

from typing import Any
from unittest.mock import MagicMock

from conftest import PANEL_MAC
import pytest

from custom_components.qolsys_panel.binary_sensor import (
    ZoneSensor_ACStatus,
    ZoneSensor_BatteryStatus,
    ZoneSensor_Tamper,
    ZoneSensor_Unreachable,
    ZonesSensor,
)
from custom_components.qolsys_panel.sensor import (
    ZoneSensor_AverageDBM,
    ZoneSensor_BatteryLevel,
    ZoneSensor_BatteryVoltage,
    ZoneSensor_LatestDBM,
    ZoneSensor_PowerG_Light,
    ZoneSensor_PowerG_Temperature,
)
from custom_components.qolsys_panel.vendor.qolsys_controller.enum_qolsys import (
    ConnectionTroubleZoneStatus,
    ControllerState,
    ZoneStatus,
)
from custom_components.qolsys_panel.vendor.qolsys_controller.zone import QolsysZone

UID = PANEL_MAC

READING_ENTITIES: list[type[Any]] = [
    ZoneSensor_BatteryStatus,
    ZoneSensor_ACStatus,
    ZoneSensor_BatteryLevel,
    ZoneSensor_BatteryVoltage,
    ZoneSensor_LatestDBM,
    ZoneSensor_AverageDBM,
    ZoneSensor_PowerG_Temperature,
    ZoneSensor_PowerG_Light,
]
ALL_ZONE_ENTITIES: list[type[Any]] = [
    ZonesSensor,
    ZoneSensor_Tamper,
    ZoneSensor_Unreachable,
    *READING_ENTITIES,
]
TROUBLE = sorted(ConnectionTroubleZoneStatus)
# Statuses where the open/closed state is known.
KNOWN = sorted(
    set(ZoneStatus) - set(ConnectionTroubleZoneStatus) - {ZoneStatus.TAMPERED, ZoneStatus.UNKNOWN}
)


def _zone(status: str, sensortype: str = "Door_Window") -> QolsysZone:
    return QolsysZone(
        {
            "zoneid": "1",
            "sensorname": "Front Door",
            "sensorstatus": status,
            "sensorgroup": "entryexitdelay",
            "sensortype": sensortype,
            "partition_id": "1",
            "battery_status": "Normal",
            "ac_status": "Normal",
            "latestdBm": "-60",
            "averagedBm": "-62",
        },
        MagicMock(),
    )


def _controller(zone: QolsysZone, state: ControllerState = ControllerState.CONNECTED) -> MagicMock:
    c = MagicMock()
    c.controller_state = state
    c.state.zone.return_value = zone
    c.state.zones = [zone]
    return c


def _set_status(zone: QolsysZone, status: ZoneStatus) -> None:
    """Change the status the way a panel update does."""
    zone.update({"zoneid": "1", "sensorstatus": status.value})
    assert zone.sensorstatus is status


# --- Z1 -----------------------------------------------------------------------


@pytest.mark.parametrize("status", [*TROUBLE, ZoneStatus.TAMPERED])
@pytest.mark.parametrize("sensortype", ["Door_Window", "SmokeDetector"])
def test_z1_main_entity_unavailable_in_trouble_or_tamper(
    status: ZoneStatus, sensortype: str
) -> None:
    """A door or smoke zone in trouble or tampered reads unavailable, not closed."""
    zone = _zone(status.value, sensortype)
    assert ZonesSensor(_controller(zone), "1", UID).available is False


@pytest.mark.parametrize("status", KNOWN)
def test_z1_main_entity_available_for_every_known_state(status: ZoneStatus) -> None:
    """Open, Closed, Active, the emergency words and the rest stay available."""
    zone = _zone(status.value)
    assert ZonesSensor(_controller(zone), "1", UID).available is True


def test_z1_unknown_status_reads_unavailable() -> None:
    """A status word the library does not know hides the state, as upstream does."""
    zone = _zone("Something New")
    assert zone.sensorstatus is ZoneStatus.UNKNOWN
    assert ZonesSensor(_controller(zone), "1", UID).available is False


# --- Z2 -----------------------------------------------------------------------


@pytest.mark.parametrize("status", TROUBLE)
@pytest.mark.parametrize("entity_cls", READING_ENTITIES)
def test_z2_readings_unavailable_while_lost(entity_cls: type[Any], status: ZoneStatus) -> None:
    """Battery, AC, dBm and PowerG readings of a lost zone are stale: unavailable."""
    zone = _zone(status.value)
    assert entity_cls(_controller(zone), "1", UID).available is False


@pytest.mark.parametrize(
    "status", [ZoneStatus.CLOSED, ZoneStatus.OPEN, ZoneStatus.TAMPERED, ZoneStatus.UNKNOWN]
)
@pytest.mark.parametrize("entity_cls", READING_ENTITIES)
def test_z2_readings_stay_available_when_the_link_is_fine(
    entity_cls: type[Any], status: ZoneStatus
) -> None:
    """A tamper or an unknown word says nothing about the readings: they stay."""
    zone = _zone(status.value)
    assert entity_cls(_controller(zone), "1", UID).available is True


# --- Z3 -----------------------------------------------------------------------


@pytest.mark.parametrize("status", [*TROUBLE, ZoneStatus.TAMPERED, ZoneStatus.CLOSED])
def test_z3_status_entity_stays_available_and_shows_the_link(status: ZoneStatus) -> None:
    """The connectivity entity is always available and reads off while lost."""
    zone = _zone(status.value)
    entity = ZoneSensor_Unreachable(_controller(zone), "1", UID)
    assert entity.available is True
    assert entity.is_on is (status not in ConnectionTroubleZoneStatus)


def test_z3_tampered_zone_tamper_entity_available_and_on() -> None:
    """Cover off: the main entity is unavailable while Tamper shows why."""
    zone = _zone("Tampered")
    c = _controller(zone)
    assert ZonesSensor(c, "1", UID).available is False
    tamper = ZoneSensor_Tamper(c, "1", UID)
    assert tamper.available is True
    assert tamper.is_on is True
    assert ZoneSensor_Unreachable(c, "1", UID).available is True


@pytest.mark.parametrize("status", [*TROUBLE, ZoneStatus.UNKNOWN])
def test_z3_tamper_unavailable_only_while_its_state_is_unknown(status: ZoneStatus) -> None:
    """Lost or Unknown: nobody knows whether the cover is off, so Tamper is unavailable."""
    zone = _zone(status.value)
    assert ZoneSensor_Tamper(_controller(zone), "1", UID).available is False


@pytest.mark.parametrize("status", KNOWN)
def test_z3_tamper_available_and_off_for_known_states(status: ZoneStatus) -> None:
    zone = _zone(status.value)
    tamper = ZoneSensor_Tamper(_controller(zone), "1", UID)
    assert tamper.available is True
    assert tamper.is_on is False


# --- Z4 -----------------------------------------------------------------------


@pytest.mark.parametrize("bad", [ZoneStatus.TAMPERED, *TROUBLE])
@pytest.mark.parametrize("good", [ZoneStatus.CLOSED, ZoneStatus.OPEN])
def test_z4_everything_comes_back_on_its_own(bad: ZoneStatus, good: ZoneStatus) -> None:
    """Tamper then recovery, lost then recovery: every entity is available again."""
    zone = _zone("Closed")
    c = _controller(zone)
    entities = [cls(c, "1", UID) for cls in ALL_ZONE_ENTITIES]
    assert all(e.available for e in entities)

    _set_status(zone, bad)
    assert entities[0].available is False  # ZonesSensor

    _set_status(zone, good)
    assert [type(e).__name__ for e in entities if not e.available] == []
    assert entities[0].is_on is (good is ZoneStatus.OPEN)
    assert entities[1].is_on is False  # Tamper cleared


# --- Z5 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "available", "on"),
    [
        (ZoneStatus.ACTIVE, True, False),  # a keypad at rest: available and off
        (ZoneStatus.IDLE, True, False),
        (ZoneStatus.OPEN, True, True),
        (ZoneStatus.POLICE_EMERGENCY, True, True),
        (ZoneStatus.FAILURE, False, None),
        (ZoneStatus.TAMPERED, False, None),
    ],
)
def test_z5_keypad_keeps_its_exception(status: ZoneStatus, available: bool, on: bool | None) -> None:
    """A keypad still reads Active as off; only its availability follows Z1."""
    zone = _zone(status.value, "Keypad")
    entity = ZonesSensor(_controller(zone), "1", UID)
    assert entity.available is available
    if on is not None:
        assert entity.is_on is on


@pytest.mark.parametrize(
    "state",
    [
        ControllerState.RECONNECTING,
        ControllerState.CONNECTING,
        ControllerState.STOPPED,
    ],
)
@pytest.mark.parametrize("entity_cls", ALL_ZONE_ENTITIES)
def test_z5_panel_disconnect_still_makes_every_zone_entity_unavailable(
    entity_cls: type[Any], state: ControllerState
) -> None:
    """A lost panel connection wins over a healthy zone, as before."""
    zone = _zone("Closed")
    assert entity_cls(_controller(zone, state), "1", UID).available is False


EXPECTED_UNIQUE_IDS = {
    ZonesSensor: f"{UID}_zone1",
    ZoneSensor_Tamper: f"{UID}_zone1_tamper",
    ZoneSensor_Unreachable: f"{UID}_zone1_unreachable",
    ZoneSensor_BatteryStatus: f"{UID}_zone1_battery_status",
    ZoneSensor_ACStatus: f"{UID}_zone1_ac_status",
    ZoneSensor_LatestDBM: f"{UID}_zone1_latestdBm",
    ZoneSensor_AverageDBM: f"{UID}_zone1_averagedBm",
}


@pytest.mark.parametrize(("entity_cls", "unique_id"), list(EXPECTED_UNIQUE_IDS.items()))
def test_z5_entity_ids_unchanged(entity_cls: type[Any], unique_id: str) -> None:
    """Availability is new; ids, which entity ids derive from, are not."""
    zone = _zone("Closed")
    assert entity_cls(_controller(zone), "1", UID).unique_id == unique_id
