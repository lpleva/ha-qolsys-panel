"""A device added to or deleted on the panel reloads the alarm integration.

Our version of upstream 79e61727e's automatic-reload half (#195), approved by
Lukas 2026-11-28. Entities are only created at setup, so a partition, zone or
Z-Wave device added at the panel used to appear only after a manual reload.
The rules this file pins (from the approval):

1. PARTITION_ADD/DELETE, ZONE_ADD/DELETE and AUTOMATION_ADD/DELETE (only
   those; the vendored library has no camera events) reload the entry once,
   10 seconds after the last such report, so a burst gives one reload.
2. No reload while the controller is reconnecting or not yet connected.
3. Unloading the integration drops any reload still waiting.
4. A reload never deletes a device (the 7-day stale-zone Repair is untouched).
5. Everything else stays the same (the rest of the suite pins that).
"""

import asyncio
from collections.abc import Callable, Generator
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.qolsys_panel import (
    _DEVICE_CHANGE_NOTIFICATIONS,
    _RELOAD_DELAY_SECONDS,
)
from custom_components.qolsys_panel.const import DOMAIN
from custom_components.qolsys_panel.vendor.qolsys_controller.enum_qolsys import (
    ControllerState,
    QolsysNotification,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

THE_SIX = {
    QolsysNotification.PARTITION_ADD,
    QolsysNotification.PARTITION_DELETE,
    QolsysNotification.ZONE_ADD,
    QolsysNotification.ZONE_DELETE,
    QolsysNotification.AUTOMATION_ADD,
    QolsysNotification.AUTOMATION_DELETE,
}


@pytest.fixture(autouse=True)
def _zeroconf(mock_async_zeroconf: MagicMock) -> None:
    """Mock zeroconf: the integration manifest depends on it."""


class _CountingRun:
    """Stand-in for run_forever that runs until cancelled, counting both."""

    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = 0

    async def run(self, *_args, **_kwargs) -> None:
        self.calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


def _panel_lists(controller: MagicMock, *zone_ids: str) -> None:
    """Make the controller's state report exactly these zones (its full table)."""
    zones = []
    for zone_id in zone_ids:
        zone = MagicMock()
        zone.zone_id = zone_id
        zones.append(zone)
    controller.state.zones = zones
    controller.state.zone.side_effect = lambda zid: next(
        (z for z in zones if z.zone_id == zid), None
    )


@pytest.fixture
def run() -> _CountingRun:
    return _CountingRun()


@pytest.fixture
def mock_controller(run: _CountingRun) -> Generator[MagicMock]:
    """A connected controller listing zones 1 to 5; the same mock for every setup."""
    controller = MagicMock()
    controller.run_forever = AsyncMock(side_effect=run.run)
    controller.wait_until_connected = AsyncMock()
    controller.stop = AsyncMock()
    controller.controller_state = ControllerState.CONNECTED
    controller.panel.HARDWARE_VERSION = "IQ Panel 4"
    controller.panel.users = [MagicMock()]
    controller.panel.users_file_malformed_rows = 0
    _panel_lists(controller, "1", "2", "3", "4", "5")
    controller.state.partition.side_effect = lambda pid: object() if pid == "0" else None
    controller.state.automation_device.side_effect = lambda _nid: None

    with (
        patch(
            "custom_components.qolsys_panel.qolsys_controller",
            return_value=controller,
        ),
        patch(
            "custom_components.qolsys_panel.get_local_ip",
            return_value="192.168.1.2",
        ),
        patch("custom_components.qolsys_panel.PLATFORMS", []),
    ):
        yield controller


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


def _callbacks(controller: MagicMock) -> dict[QolsysNotification, Callable[[], None]]:
    """The latest callback registered for each add/delete notification."""
    found: dict[QolsysNotification, Callable[[], None]] = {}
    for call in controller.state.register.call_args_list:
        if call.args[0] in THE_SIX:
            found[call.args[0]] = call.args[1]
    return found


def _status_callback(controller: MagicMock) -> Callable[[], None]:
    """The latest PANEL_STATUS_UPDATE callback registered."""
    return [
        call.args[1]
        for call in controller.state.register.call_args_list
        if call.args[0] is QolsysNotification.PANEL_STATUS_UPDATE
    ][-1]


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _set_state(
    hass: HomeAssistant, controller: MagicMock, state: ControllerState
) -> None:
    """Change the controller state and deliver the panel status update."""
    controller.controller_state = state
    _status_callback(controller)()
    await hass.async_block_till_done()


# --------------------------------------------------------------------------
# Rule 1: one reload per report, 10 s after the last one


def test_the_delay_is_ten_seconds() -> None:
    assert _RELOAD_DELAY_SECONDS == 10


async def test_only_the_six_add_delete_notifications_are_listened_to(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_controller: MagicMock
) -> None:
    """Partitions, zones and Z-Wave devices; not scenes, sensors or meters."""
    assert set(_DEVICE_CHANGE_NOTIFICATIONS) == THE_SIX
    await _setup(hass, mock_config_entry)
    reload_callbacks = _callbacks(mock_controller)
    assert set(reload_callbacks) == THE_SIX
    # One callback for all six, and nothing else is wired to it.
    assert len(set(reload_callbacks.values())) == 1
    the_callback = next(iter(reload_callbacks.values()))
    others = {
        call.args[0]
        for call in mock_controller.state.register.call_args_list
        if call.args[1] is the_callback
    }
    assert others == THE_SIX


@pytest.mark.parametrize("notification", sorted(THE_SIX, key=str))
async def test_one_report_reloads_once_ten_seconds_later(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    freezer: FrozenDateTimeFactory,
    notification: QolsysNotification,
) -> None:
    await _setup(hass, mock_config_entry)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        _callbacks(mock_controller)[notification]()
        await hass.async_block_till_done()
        reload.assert_not_called()

        await _tick(hass, freezer, 9)
        reload.assert_not_called()

        await _tick(hass, freezer, 1.5)
        reload.assert_called_once_with(mock_config_entry.entry_id)

        await _tick(hass, freezer, 60)
        reload.assert_called_once_with(mock_config_entry.entry_id)


async def test_a_burst_reloads_once_ten_seconds_after_the_last_report(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Reports at 0, 6 and 12 s (a partition deleted with its zones): one reload at 22 s."""
    await _setup(hass, mock_config_entry)
    callbacks = _callbacks(mock_controller)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        callbacks[QolsysNotification.ZONE_DELETE]()
        callbacks[QolsysNotification.ZONE_DELETE]()
        await _tick(hass, freezer, 6)
        callbacks[QolsysNotification.ZONE_DELETE]()
        await _tick(hass, freezer, 6)
        callbacks[QolsysNotification.PARTITION_DELETE]()
        await _tick(hass, freezer, 9)  # 21 s: 9 s after the last report
        reload.assert_not_called()

        await _tick(hass, freezer, 1.5)
        reload.assert_called_once_with(mock_config_entry.entry_id)

        await _tick(hass, freezer, 60)
        reload.assert_called_once_with(mock_config_entry.entry_id)


# --------------------------------------------------------------------------
# Rule 2: no reload while reconnecting or not yet connected


@pytest.mark.parametrize(
    "state",
    [ControllerState.RECONNECTING, ControllerState.CONNECTING, ControllerState.CONFIGURING],
)
async def test_a_report_while_not_connected_is_ignored(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    freezer: FrozenDateTimeFactory,
    state: ControllerState,
) -> None:
    """The resync after a reconnect re-adds every zone; none of that reloads."""
    await _setup(hass, mock_config_entry)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        mock_controller.controller_state = state
        for callback in _callbacks(mock_controller).values():
            callback()
        await _tick(hass, freezer, 60)
        reload.assert_not_called()


async def test_a_reload_due_while_reconnecting_waits_for_the_connection(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Connection lost inside the 10 s: no reload then, one 10 s after it is back."""
    await _setup(hass, mock_config_entry)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        _callbacks(mock_controller)[QolsysNotification.ZONE_ADD]()
        await _tick(hass, freezer, 5)
        await _set_state(hass, mock_controller, ControllerState.RECONNECTING)
        await _tick(hass, freezer, 60)
        reload.assert_not_called()

        await _set_state(hass, mock_controller, ControllerState.CONNECTED)
        await _tick(hass, freezer, 9)
        reload.assert_not_called()
        await _tick(hass, freezer, 1.5)
        reload.assert_called_once_with(mock_config_entry.entry_id)


async def test_a_reconnect_with_nothing_pending_does_not_reload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, mock_config_entry)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await _set_state(hass, mock_controller, ControllerState.RECONNECTING)
        await _set_state(hass, mock_controller, ControllerState.CONNECTED)
        await _tick(hass, freezer, 60)
        reload.assert_not_called()


# --------------------------------------------------------------------------
# Rule 3: unloading drops a waiting reload


async def test_unload_drops_a_waiting_reload_and_unregisters(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, mock_config_entry)
    callbacks = _callbacks(mock_controller)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        callbacks[QolsysNotification.ZONE_ADD]()
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        for notification, callback in callbacks.items():
            mock_controller.state.unregister.assert_any_call(notification, callback)

        await _tick(hass, freezer, 60)
        # A report that slips in after the unload does nothing either.
        callbacks[QolsysNotification.ZONE_ADD]()
        await _tick(hass, freezer, 60)
        reload.assert_not_called()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


# --------------------------------------------------------------------------
# Rules 1 and 4 end to end: the real reload, and no device deleted


async def test_a_zone_report_reloads_the_entry_end_to_end_and_keeps_devices(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    run: _CountingRun,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Zone 5 deleted and zone 6 added at the panel: the entry reloads once,
    the controller restarts, and zone 5's device is still in the registry."""
    await _setup(hass, mock_config_entry)
    registry = dr.async_get(hass)
    zone5 = registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, f"{mock_config_entry.unique_id}_zone5")},
        name="Zone 5",
    )
    first = _callbacks(mock_controller)
    assert run.calls == 1

    _panel_lists(mock_controller, "1", "2", "3", "4", "6")
    first[QolsysNotification.ZONE_DELETE]()
    first[QolsysNotification.ZONE_ADD]()
    async with asyncio.timeout(10):
        await _tick(hass, freezer, _RELOAD_DELAY_SECONDS + 0.5)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert run.calls == 2
    assert run.cancelled == 1
    mock_controller.stop.assert_not_awaited()
    assert registry.async_get(zone5.id) is not None

    # The reloaded entry listens again, with fresh callbacks, and still reloads.
    second = _callbacks(mock_controller)
    assert set(second) == THE_SIX
    assert second[QolsysNotification.ZONE_ADD] is not first[QolsysNotification.ZONE_ADD]
    # The old entry's callbacks are gone: a report through one does nothing.
    first[QolsysNotification.ZONE_ADD]()
    await _tick(hass, freezer, 60)
    assert run.calls == 2
