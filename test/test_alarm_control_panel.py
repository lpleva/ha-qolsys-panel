"""Tests for the Qolsys Panel alarm control panel."""

import json
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from conftest import PANEL_MAC
import pytest

from custom_components.qolsys_panel.alarm_control_panel import (
    PartitionAlarmControlPanel,
    async_setup_entry,
)
from custom_components.qolsys_panel.const import DEFAULT_DISARM_CODE_REQUIRED
from custom_components.qolsys_panel.vendor.qolsys_controller.enum_qolsys import (
    PartitionAlarmState,
    PartitionArmingType,
    PartitionSystemStatus,
    ZoneStatus,
)
from custom_components.qolsys_panel.vendor.qolsys_controller.errors import (
    QolsysOperationTimeoutError,
    QolsysUserCodeError,
    QolsysZoneBypassError,
)
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

UID = PANEL_MAC


@pytest.fixture
def controller() -> MagicMock:
    """A controller mock with one partition."""
    c = MagicMock()
    partition = MagicMock()
    partition.id = "1"
    c.state.partitions = [partition]
    return c


def _panel(controller: MagicMock) -> PartitionAlarmControlPanel:
    """Build the alarm entity with awaitable partition commands."""
    entity = PartitionAlarmControlPanel(controller, "1", UID)
    entity._partition.arm = AsyncMock()
    entity._partition.disarm = AsyncMock()
    return entity


async def test_async_setup_entry_creates_entities(
    hass: HomeAssistant, controller: MagicMock
) -> None:
    """Setup builds one alarm control panel per partition."""
    config_entry = MagicMock()
    config_entry.runtime_data = controller
    config_entry.unique_id = UID
    add_entities = MagicMock()

    await async_setup_entry(hass, config_entry, add_entities)

    add_entities.assert_called_once()
    entities = add_entities.call_args.args[0]
    assert len(entities) == 1
    assert isinstance(entities[0], PartitionAlarmControlPanel)


@pytest.mark.parametrize(
    ("alarm_state", "system_status", "expected"),
    [
        (
            PartitionAlarmState.ALARM,
            PartitionSystemStatus.ARM_AWAY,
            AlarmControlPanelState.TRIGGERED,
        ),
        (
            PartitionAlarmState.NONE,
            PartitionSystemStatus.DISARM,
            AlarmControlPanelState.DISARMED,
        ),
        (
            PartitionAlarmState.NONE,
            PartitionSystemStatus.ARM_AWAY_EXIT_DELAY,
            AlarmControlPanelState.ARMING,
        ),
        (
            PartitionAlarmState.NONE,
            PartitionSystemStatus.ARM_STAY_EXIT_DELAY,
            AlarmControlPanelState.ARMING,
        ),
        (
            PartitionAlarmState.NONE,
            PartitionSystemStatus.ARM_NIGHT_EXIT_DELAY,
            AlarmControlPanelState.ARMING,
        ),
        (
            PartitionAlarmState.NONE,
            PartitionSystemStatus.ARM_STAY,
            AlarmControlPanelState.ARMED_HOME,
        ),
        (
            PartitionAlarmState.NONE,
            PartitionSystemStatus.ARM_AWAY,
            AlarmControlPanelState.ARMED_AWAY,
        ),
        (
            PartitionAlarmState.NONE,
            PartitionSystemStatus.ARM_NIGHT,
            AlarmControlPanelState.ARMED_NIGHT,
        ),
        (PartitionAlarmState.NONE, PartitionSystemStatus.UNKNOWN, None),
    ],
)
def test_alarm_state(
    controller: MagicMock, alarm_state, system_status, expected
) -> None:
    """The alarm state maps the partition alarm/system status."""
    entity = _panel(controller)
    entity._partition.alarm_state = alarm_state
    entity._partition.system_status = system_status
    assert entity.alarm_state == expected


@pytest.mark.parametrize("required", [True, False])
def test_code_required_when_arming(controller: MagicMock, required: bool) -> None:
    """When disarmed, the arm code option drives code requirement/format."""
    entity = _panel(controller)
    entity._partition.system_status = PartitionSystemStatus.DISARM
    entity._partition.alarm_state = PartitionAlarmState.NONE
    controller.settings.check_user_code_on_arm = required

    assert entity.code_arm_required is required
    assert entity.code_format == (CodeFormat.NUMBER if required else None)


@pytest.mark.parametrize("required", [True, False])
def test_code_required_when_disarming(controller: MagicMock, required: bool) -> None:
    """When armed, the disarm code option drives code requirement/format."""
    entity = _panel(controller)
    entity._partition.system_status = PartitionSystemStatus.ARM_AWAY
    entity._partition.alarm_state = PartitionAlarmState.NONE
    controller.settings.check_user_code_on_disarm = required

    assert entity.code_arm_required is required
    assert entity.code_format == (CodeFormat.NUMBER if required else None)


async def test_disarm(controller: MagicMock) -> None:
    """Disarm forwards the code to the partition."""
    entity = _panel(controller)
    await entity.async_alarm_disarm("1234")
    cast(AsyncMock, entity._partition.disarm).assert_awaited_once_with(user_code="1234")


def test_disarm_requires_a_code_by_default() -> None:
    """A fresh install must not offer a one-click disarm (audit C1)."""
    assert DEFAULT_DISARM_CODE_REQUIRED is True


async def test_disarm_without_code_is_rejected(controller: MagicMock) -> None:
    """With the code check on, an empty code never reaches the panel (audit C1)."""
    entity = _panel(controller)
    controller.settings.check_user_code_on_disarm = True

    with pytest.raises(HomeAssistantError):
        await entity.async_alarm_disarm(None)

    cast(AsyncMock, entity._partition.disarm).assert_not_awaited()


async def test_disarm_with_unknown_code_is_rejected(controller: MagicMock) -> None:
    """An unknown code is refused by the integration, not by the panel (audit C1)."""
    entity = _panel(controller)
    controller.settings.check_user_code_on_disarm = True
    controller.panel.check_user = MagicMock(return_value=-1)

    with pytest.raises(HomeAssistantError):
        await entity.async_alarm_disarm("9999")

    controller.panel.check_user.assert_called_once_with("9999")
    cast(AsyncMock, entity._partition.disarm).assert_not_awaited()


async def test_disarm_with_valid_code_is_sent(controller: MagicMock) -> None:
    """A code the panel database knows is forwarded (audit C1)."""
    entity = _panel(controller)
    controller.settings.check_user_code_on_disarm = True
    controller.panel.check_user = MagicMock(return_value=2)

    await entity.async_alarm_disarm("1234")

    cast(AsyncMock, entity._partition.disarm).assert_awaited_once_with(user_code="1234")


async def test_arm_without_code_is_rejected_when_required(
    controller: MagicMock,
) -> None:
    """Arming with the code option on also validates before sending (audit C1)."""
    entity = _panel(controller)
    controller.settings.check_user_code_on_arm = True

    with pytest.raises(HomeAssistantError):
        await entity.async_alarm_arm_away(None)

    cast(AsyncMock, entity._partition.arm).assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [QolsysUserCodeError(), QolsysOperationTimeoutError(), RuntimeError("boom")],
)
async def test_disarm_errors(controller: MagicMock, error: Exception) -> None:
    """Disarm failures surface as HomeAssistantError."""
    entity = _panel(controller)
    cast(AsyncMock, entity._partition.disarm).side_effect = error
    with pytest.raises(HomeAssistantError):
        await entity.async_alarm_disarm("1234")


@pytest.mark.parametrize(
    ("method", "arm_mode"),
    [
        ("async_alarm_arm_home", PartitionArmingType.ARM_STAY),
        ("async_alarm_arm_away", PartitionArmingType.ARM_AWAY),
        ("async_alarm_arm_night", PartitionArmingType.ARM_NIGHT),
    ],
)
async def test_arm(controller: MagicMock, method: str, arm_mode) -> None:
    """Each arm method forwards the right arming mode and code."""
    entity = _panel(controller)
    await getattr(entity, method)("1234")
    cast(AsyncMock, entity._partition.arm).assert_awaited_once_with(
        arm_mode, user_code="1234"
    )


@pytest.mark.parametrize(
    "error",
    [
        QolsysUserCodeError(),
        QolsysOperationTimeoutError(),
        QolsysZoneBypassError(["1"]),
        RuntimeError("boom"),
    ],
)
async def test_arm_errors(controller: MagicMock, error: Exception) -> None:
    """Arm failures surface as HomeAssistantError."""
    entity = _panel(controller)
    cast(AsyncMock, entity._partition.arm).side_effect = error
    with pytest.raises(HomeAssistantError):
        await entity.async_alarm_arm_away("1234")


COMPONENT = Path("custom_components/qolsys_panel")
ARM_BLOCKED_KEYS = ("arm_blocked_safety_zones", "arm_blocked_open_zones")


def _rendered(key: str, placeholders: dict[str, str]) -> list[str]:
    """The sentence a user sees for a translated error, English then French."""
    texts = []
    for name in ("translations/en.json", "translations/fr-CA.json"):
        data = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        texts.append(data["exceptions"][key]["message"].format(**placeholders))
    return texts


def _assert_no_way_around(texts: list[str]) -> None:
    """A refusal never tells the user how to arm anyway."""
    for text in texts:
        lowered = text.lower()
        assert "force" not in lowered
        assert "forc" not in lowered  # forcer / forcé
        assert "anyway" not in lowered
        assert "quand même" not in lowered


def _zone(name: str, *, safety: bool, status: ZoneStatus = ZoneStatus.OPEN) -> MagicMock:
    """A zone as state.zone() returns it."""
    zone = MagicMock()
    zone.sensorname = name
    zone.sensorstatus = status
    zone.is_safety.return_value = safety
    return zone


@pytest.mark.parametrize(
    ("method", "action"),
    [("async_alarm_arm_away", "arm away"), ("async_alarm_arm_home", "arm home")],
)
async def test_arm_refused_by_a_tripped_safety_zone_names_it(
    controller: MagicMock, method: str, action: str
) -> None:
    """The L1 refusal names the tripped safety zone and says to clear it first.

    The library raises QolsysZoneBypassError with zone ids; the user sees the
    zone's name, that it cannot be bypassed, and no hint to arm anyway.
    """
    entity = _panel(controller)
    controller.state.zone.side_effect = {"4": _zone("Hallway Smoke", safety=True)}.get
    cast(AsyncMock, entity._partition.arm).side_effect = QolsysZoneBypassError(["4"])

    with pytest.raises(HomeAssistantError) as raised:
        await getattr(entity, method)("1234")

    assert raised.value.translation_key == "arm_blocked_safety_zones"
    assert raised.value.translation_placeholders == {
        "action": action,
        "zones": "Hallway Smoke (open)",
    }
    english, french = _rendered(
        "arm_blocked_safety_zones", raised.value.translation_placeholders
    )
    assert f"Could not {action}" in english
    assert "Hallway Smoke (open)" in english
    assert "cannot be bypassed; clear them first" in english
    assert "Hallway Smoke (open)" in french
    assert "ne peuvent pas être contournées" in french
    _assert_no_way_around([english, french])


async def test_arm_refused_by_open_zones_names_them(controller: MagicMock) -> None:
    """The Auto Bypass refusal lists every blocking zone by name and status."""
    entity = _panel(controller)
    controller.state.zone.side_effect = {
        "2": _zone("Front Door", safety=False),
        "7": _zone("Garage Window", safety=False, status=ZoneStatus.TAMPERED),
    }.get
    cast(AsyncMock, entity._partition.arm).side_effect = QolsysZoneBypassError(
        ["2", "7"]
    )

    with pytest.raises(HomeAssistantError) as raised:
        await entity.async_alarm_arm_away("1234")

    assert raised.value.translation_key == "arm_blocked_open_zones"
    assert raised.value.translation_placeholders == {
        "action": "arm away",
        "zones": "Front Door (open), Garage Window (tampered)",
    }
    texts = _rendered("arm_blocked_open_zones", raised.value.translation_placeholders)
    for text in texts:
        assert "Front Door (open), Garage Window (tampered)" in text
        # Bypass is a panel setting, not something the message steers towards.
        assert "bypass" not in text.lower()
        assert "contourn" not in text.lower()
    _assert_no_way_around(texts)


async def test_arm_refused_by_an_unknown_zone_falls_back_to_its_id(
    controller: MagicMock,
) -> None:
    """A zone id the state no longer holds is still reported, as its id."""
    entity = _panel(controller)
    controller.state.zone.return_value = None
    cast(AsyncMock, entity._partition.arm).side_effect = QolsysZoneBypassError(["9"])

    with pytest.raises(HomeAssistantError) as raised:
        await entity.async_alarm_arm_night("1234")

    assert raised.value.translation_key == "arm_blocked_open_zones"
    assert raised.value.translation_placeholders == {
        "action": "arm night",
        "zones": "zone 9",
    }


@pytest.mark.parametrize("key", ARM_BLOCKED_KEYS)
def test_arm_refusal_templates_never_suggest_forcing(key: str) -> None:
    """The raw templates, in both languages, carry no force-arm wording."""
    _assert_no_way_around(_rendered(key, {"action": "arm away", "zones": "Zone"}))


async def test_setup_without_unique_id_raises_value_error(
    hass: HomeAssistant, controller: MagicMock
) -> None:
    """A forwarded platform reports a bad entry with ValueError (review N5).

    ConfigEntryNotReady from a forwarded platform is not retried by Home
    Assistant, it is logged as a mistake.
    """
    config_entry = MagicMock()
    config_entry.runtime_data = controller
    config_entry.unique_id = None

    with pytest.raises(ValueError):
        await async_setup_entry(hass, config_entry, MagicMock())
