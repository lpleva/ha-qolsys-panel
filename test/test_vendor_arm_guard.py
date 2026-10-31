"""Library-side arm and disarm guards (audit L1, C1 defence in depth)."""

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
