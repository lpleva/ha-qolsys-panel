"""Library-side arm and disarm guards (audit L1, C1 defence in depth)."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.qolsys_panel.vendor.qolsys_controller.commands.panel import (
    PanelCommands,
)
from custom_components.qolsys_panel.vendor.qolsys_controller.enum_qolsys import (
    PartitionArmingType,
    TroubleZoneStatus,
    ZoneStatus,
)
from custom_components.qolsys_panel.vendor.qolsys_controller.errors import (
    QolsysUserCodeError,
    QolsysZoneBypassError,
)
from custom_components.qolsys_panel.vendor.qolsys_controller.zone import QolsysZone

SEND = "custom_components.qolsys_panel.vendor.qolsys_controller.commands.panel.MQTTCommand_Panel"
PANEL_LOGGER = "custom_components.qolsys_panel.vendor.qolsys_controller.commands.panel"


def _zone(*, safety: bool, bypassable: bool) -> MagicMock:
    """An open zone in partition 1."""
    zone = MagicMock()
    zone.partition_id = "1"
    zone.sensorstatus = TroubleZoneStatus[0]
    zone.is_bypassable.return_value = bypassable
    zone.is_safety.return_value = safety
    zone.zone_id = "4"
    return zone


@pytest.fixture
def commands() -> PanelCommands:
    """A panel command service with one partition and no zones."""
    controller = MagicMock()
    controller.state.zones = []
    controller.settings.check_user_code_on_arm = False
    controller.panel.AUTO_BYPASS = "true"
    return PanelCommands(controller)


async def test_arm_refuses_an_open_safety_zone(commands: PanelCommands) -> None:
    """An open smoke, CO or water sensor stops arming (audit L1)."""
    commands._controller.state.zones = [_zone(safety=True, bypassable=False)]

    with pytest.raises(QolsysZoneBypassError) as err:
        await commands.arm("1", PartitionArmingType.ARM_AWAY)

    assert err.value.zones == ["4"]


async def test_arm_refuses_open_zones_when_auto_bypass_is_off(
    commands: PanelCommands,
) -> None:
    """The neighbouring auto-bypass guard still behaves the same way."""
    commands._controller.panel.AUTO_BYPASS = "false"
    commands._controller.state.zones = [_zone(safety=False, bypassable=True)]

    with pytest.raises(QolsysZoneBypassError):
        await commands.arm("1", PartitionArmingType.ARM_AWAY)


async def test_disarm_refuses_an_unknown_code_in_the_library(
    commands: PanelCommands,
) -> None:
    """The library re-checks the code, so the entity guard cannot be bypassed.

    The integration validates before calling _partition.disarm (audit C1); this
    asserts the second line of defence for a caller that reaches the command
    service directly.
    """
    commands._controller.settings.check_user_code_on_disarm = True
    commands._controller.panel.check_user = MagicMock(return_value=-1)

    with pytest.raises(QolsysUserCodeError):
        await commands.disarm("1", "9999")

    commands._controller.panel.check_user.assert_called_once_with("9999")


async def test_disarm_refuses_an_empty_code_in_the_library(
    commands: PanelCommands,
) -> None:
    """An empty code is not a valid code either."""
    commands._controller.settings.check_user_code_on_disarm = True
    commands._controller.panel.check_user = MagicMock(return_value=-1)

    with pytest.raises(QolsysUserCodeError):
        await commands.disarm("1", "")


# --- The L1 guard re-proven on real zones (regression for 687ab08) -----------
#
# The zone helpers is_on()/is_trouble() were written here instead of taking
# library 1.12.6, whose arm() has this guard commented out again. These cases
# run the real QolsysZone (is_safety, is_bypassable) through the real arm(), so
# a future library bump that drops the guard, or a change to the zone class that
# stops a safety zone counting as one, fails here and not at the front door.


def _real_zone(*, zone_id: str, sensortype: str, sensorgroup: str, status: str) -> QolsysZone:
    """A zone built from a panel row, in partition 1."""
    return QolsysZone(
        {
            "zoneid": zone_id,
            "sensorname": f"Zone {zone_id}",
            "sensorstatus": status,
            "sensorgroup": sensorgroup,
            "sensortype": sensortype,
            "partition_id": "1",
        },
        MagicMock(),
    )


def _smoke(status: str) -> QolsysZone:
    """A reporting smoke detector; 'reportingsafety' is in SafetyZoneSensorGroup."""
    return _real_zone(zone_id="7", sensortype="Smoke_M", sensorgroup="reportingsafety", status=status)


def _door(status: str) -> QolsysZone:
    """An entry door, bypassable while open."""
    return _real_zone(zone_id="4", sensortype="Door_Window", sensorgroup="entryexitdelay", status=status)


async def test_arm_refuses_a_real_open_smoke_zone(commands: PanelCommands) -> None:
    """A real safety zone reporting Open stops arming, by zone id (audit L1)."""
    smoke = _smoke("Open")
    assert smoke.is_safety() is True
    assert smoke.is_bypassable() is False
    commands._controller.state.zones = [smoke]

    with patch(SEND) as send:
        with pytest.raises(QolsysZoneBypassError) as err:
            await commands.arm("1", PartitionArmingType.ARM_AWAY)
        send.assert_not_called()

    assert err.value.zones == ["7"]


async def test_arm_refuses_a_real_smoke_zone_in_failure(commands: PanelCommands) -> None:
    """Upstream #169's smoke listener: supervision lost reads Failure, and Failure blocks arming.

    Failure is both in TroubleZoneStatus (so the guard sees the zone) and in
    the new ConnectionTroubleZoneStatus (so is_trouble() is True). The two
    lists serve different callers and must both hold.
    """
    smoke = _smoke("Failure")
    assert smoke.sensorstatus is ZoneStatus.FAILURE
    assert smoke.is_trouble() is True
    assert smoke.is_on() is False
    commands._controller.state.zones = [smoke]

    with patch(SEND) as send:
        with pytest.raises(QolsysZoneBypassError) as err:
            await commands.arm("1", PartitionArmingType.ARM_AWAY)
        send.assert_not_called()

    assert err.value.zones == ["7"]


async def test_arm_sends_when_the_real_safety_zone_is_clear(commands: PanelCommands) -> None:
    """The control case: smoke Closed and a door Open with Auto Bypass on arms, bypassing the door."""
    commands._controller.state.zones = [_smoke("Closed"), _door("Open")]
    commands._controller.panel.AUTO_BYPASS = "true"
    commands._controller.settings.random_mac = "00:11:22:33:44:55"

    with patch(SEND) as send:
        send.return_value.send_command = AsyncMock(return_value={"ok": True})
        response = await commands.arm("1", PartitionArmingType.ARM_AWAY)

    assert response == {"ok": True}
    send.return_value.append_ipc_request.assert_called_once()
    request = send.return_value.append_ipc_request.call_args.args[0]
    assert '"bypass_zoneid_set": "[4]"' in request[0]["dataValue"]


# --- The three groups the list was missing (fork, 2026-10-31) ----------------
#
# SafetyZoneSensorGroup had the six upstream names but not the groups the panel
# actually puts smoke/heat detectors, CO detectors and reporting water sensors
# in. A tripped zone in one of those was neither bypassable nor safety, fell
# into arm()'s unread open_zone_list, and the arm command went out. Each case
# here is a real QolsysZone from a panel row, through the real arm(), with the
# MQTT command class patched so nothing can be sent.

NEWLY_GUARDED = [
    # (zone id, sensortype, sensorgroup)
    pytest.param("11", "SmokeDetector", "smoke_heat", id="smoke_heat"),
    pytest.param("12", "CODetector", "co", id="co"),
    pytest.param("13", "Water", "WaterSensor", id="WaterSensor"),
]


@pytest.mark.parametrize(("zone_id", "sensortype", "sensorgroup"), NEWLY_GUARDED)
@pytest.mark.parametrize("status", ["Open", "Failure", "Tampered"])
async def test_arm_refuses_a_tripped_zone_in_a_newly_guarded_group(
    commands: PanelCommands, zone_id: str, sensortype: str, sensorgroup: str, status: str
) -> None:
    """smoke_heat, co and WaterSensor zones refuse to arm exactly like the older safety groups.

    Same error, same zone id in it, and nothing sent. Open is the detector
    tripped; Failure and Tampered are the other TroubleZoneStatus words a
    detector reports in real life (supervision lost, cover off), and the
    existing reportingsafety smoke case already treats both as blocking.
    """
    zone = _real_zone(zone_id=zone_id, sensortype=sensortype, sensorgroup=sensorgroup, status=status)
    assert zone.is_safety() is True
    assert zone.is_bypassable() is False
    commands._controller.state.zones = [zone]

    with patch(SEND) as send:
        with pytest.raises(QolsysZoneBypassError) as err:
            await commands.arm("1", PartitionArmingType.ARM_AWAY)
        send.assert_not_called()

    assert err.value.zones == [zone_id]


@pytest.mark.parametrize(("zone_id", "sensortype", "sensorgroup"), NEWLY_GUARDED)
async def test_arm_refuses_a_newly_guarded_zone_even_with_auto_bypass_on(
    commands: PanelCommands, zone_id: str, sensortype: str, sensorgroup: str
) -> None:
    """Auto Bypass on does not help: the zone is not bypassable, so it is not in the bypass set either."""
    commands._controller.panel.AUTO_BYPASS = "true"
    commands._controller.state.zones = [
        _real_zone(zone_id=zone_id, sensortype=sensortype, sensorgroup=sensorgroup, status="Open"),
        _door("Open"),
    ]

    with patch(SEND) as send:
        with pytest.raises(QolsysZoneBypassError) as err:
            await commands.arm("1", PartitionArmingType.ARM_AWAY)
        send.assert_not_called()

    assert err.value.zones == [zone_id]


async def test_arm_sends_when_the_newly_guarded_zones_are_clear(commands: PanelCommands) -> None:
    """The control case: all three at Closed plus an open door with Auto Bypass on arms, bypassing the door only."""
    commands._controller.state.zones = [
        _real_zone(zone_id="11", sensortype="SmokeDetector", sensorgroup="smoke_heat", status="Closed"),
        _real_zone(zone_id="12", sensortype="CODetector", sensorgroup="co", status="Closed"),
        _real_zone(zone_id="13", sensortype="Water", sensorgroup="WaterSensor", status="Closed"),
        _door("Open"),
    ]
    commands._controller.panel.AUTO_BYPASS = "true"
    commands._controller.settings.random_mac = "00:11:22:33:44:55"

    with patch(SEND) as send:
        send.return_value.send_command = AsyncMock(return_value={"ok": "true"})
        response = await commands.arm("1", PartitionArmingType.ARM_AWAY)

    assert response == {"ok": "true"}
    request = send.return_value.append_ipc_request.call_args.args[0]
    assert '"bypass_zoneid_set": "[4]"' in request[0]["dataValue"]


async def test_arm_still_sends_over_a_non_bypassable_non_safety_zone(
    commands: PanelCommands, caplog: pytest.LogCaptureFixture
) -> None:
    """Ordinary arming is unchanged: a tampered motion sensor is neither bypassable nor safety, and the arm goes out.

    Upstream collects these zones in open_zone_list and never reads it; this
    fork keeps that decision (the panel rules on them, as from its own screen)
    and only logs the list, so the trace exists. The bypass set stays empty:
    nothing about these zones is sent to the panel.
    """
    motion = _real_zone(zone_id="9", sensortype="Motion", sensorgroup="awayinstantmotion", status="Tampered")
    assert motion.is_bypassable() is False
    assert motion.is_safety() is False
    commands._controller.state.zones = [motion]
    commands._controller.settings.random_mac = "00:11:22:33:44:55"

    with patch(SEND) as send, caplog.at_level(logging.WARNING, logger=PANEL_LOGGER):
        send.return_value.send_command = AsyncMock(return_value={"ok": "true"})
        response = await commands.arm("1", PartitionArmingType.ARM_AWAY)

    assert response == {"ok": "true"}
    request = send.return_value.append_ipc_request.call_args.args[0]
    assert '"bypass_zoneid_set": "[]"' in request[0]["dataValue"]
    assert [r.getMessage() for r in caplog.records] == [
        "MQTT Panel Client - Arming with zones not ready that cannot be bypassed: ['9']"
    ]


async def test_a_clean_arm_logs_no_warning(
    commands: PanelCommands, caplog: pytest.LogCaptureFixture
) -> None:
    """Every zone Closed: no warning line at all, so the new log line cannot become noise."""
    commands._controller.state.zones = [_smoke("Closed"), _door("Closed")]
    commands._controller.settings.random_mac = "00:11:22:33:44:55"

    with patch(SEND) as send, caplog.at_level(logging.WARNING, logger=PANEL_LOGGER):
        send.return_value.send_command = AsyncMock(return_value={"ok": "true"})
        await commands.arm("1", PartitionArmingType.ARM_AWAY)

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
