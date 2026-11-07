"""Devices the panel no longer lists: manual Delete, a Repair after seven days, never automatic.

Our own version of upstream dc5130c (#175). Upstream deleted every device the
panel did not report at each reload; this fork never deletes (rule 2), lets
the device page's Delete through only for a device the panel's complete table
lacks (rule 1), raises a Repair after seven days of consistent readings (rule
3) and ignores a reading that looks like a glitch (rule 4).
"""

from collections.abc import Generator
from datetime import timedelta
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.qolsys_panel import async_remove_config_entry_device
from custom_components.qolsys_panel.const import DOMAIN
from custom_components.qolsys_panel.stale_devices import (
    CHECK_INTERVAL,
    MAX_MISSING_FLOOR,
    MAX_MISSING_SHARE,
    STALE_AFTER,
    StaleZoneWatch,
    get_watch,
    issue_id,
    panel_has_device,
    storage_key,
)
from custom_components.qolsys_panel.vendor.qolsys_controller.enum_qolsys import (
    ControllerState,
    QolsysNotification,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir

A_DAY = timedelta(days=1)


@pytest.fixture(autouse=True)
def _zeroconf(mock_async_zeroconf: MagicMock) -> None:
    """Mock zeroconf: the integration manifest depends on it."""


@pytest.fixture
def mock_controller() -> Generator[MagicMock]:
    """A connected controller whose panel lists zones 1 to 5 and partition 0."""
    controller = MagicMock()
    controller.run_forever = AsyncMock()
    controller.wait_until_connected = AsyncMock()
    controller.stop = AsyncMock()
    controller.controller_state = ControllerState.CONNECTED
    controller.panel.HARDWARE_VERSION = "IQ Panel 4"
    _panel_lists(controller, "1", "2", "3", "4", "5")
    _panel_partitions(controller, "0")
    _panel_automation_devices(controller)

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


def _panel_partitions(controller: MagicMock, *partition_ids: str) -> None:
    controller.state.partition.side_effect = lambda pid: (
        object() if pid in partition_ids else None
    )


def _panel_automation_devices(controller: MagicMock, *node_ids: str) -> None:
    controller.state.automation_device.side_effect = lambda nid: (
        object() if nid in node_ids else None
    )


def _add_device(
    hass: HomeAssistant, entry: MockConfigEntry, suffix: str | None
) -> dr.DeviceEntry:
    """Register a device for the entry: the panel root (suffix None) or a child."""
    ident = entry.unique_id if suffix is None else f"{entry.unique_id}_{suffix}"
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, ident)},
        name=f"Device {ident}",
    )


def _add_zone_devices(
    hass: HomeAssistant, entry: MockConfigEntry, *zone_ids: str
) -> dict[str, dr.DeviceEntry]:
    return {zid: _add_device(hass, entry, f"zone{zid}") for zid in zone_ids}


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> StaleZoneWatch:
    if entry.entry_id not in hass.config_entries.async_entry_ids():
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    watch = get_watch(hass, entry.entry_id)
    assert watch is not None
    return watch


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    """Advance the clock and fire the periodic check that is now due."""
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant, device: dr.DeviceEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id(device.id))


def _device_exists(hass: HomeAssistant, device: dr.DeviceEntry) -> bool:
    return dr.async_get(hass).async_get(device.id) is not None


# --------------------------------------------------------------------------
# Rule 1: the device page's Delete


async def test_delete_refused_for_a_zone_the_panel_still_lists(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_controller: MagicMock
) -> None:
    """Zone 3 is in the panel's table, so its device stays."""
    await _setup(hass, mock_config_entry)
    device = _add_device(hass, mock_config_entry, "zone3")
    assert await async_remove_config_entry_device(hass, mock_config_entry, device) is False
    mock_controller.state.zone.assert_any_call("3")


async def test_delete_allowed_for_a_zone_the_panel_no_longer_lists(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Zone 9 is not in the table: Home Assistant may remove the device."""
    await _setup(hass, mock_config_entry)
    device = _add_device(hass, mock_config_entry, "zone9")
    caplog.set_level(logging.INFO)
    assert await async_remove_config_entry_device(hass, mock_config_entry, device) is True
    assert "the panel no longer lists it" in caplog.text


async def test_delete_refused_for_the_panel_itself(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_controller: MagicMock
) -> None:
    """The root device goes only with the integration."""
    await _setup(hass, mock_config_entry)
    root = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, mock_config_entry.unique_id), mock_config_entry.entry_id
    )
    assert root is not None
    assert await async_remove_config_entry_device(hass, mock_config_entry, root) is False


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        ("partition0", False),  # the panel has partition 0
        ("partition7", True),  # it does not have partition 7
        ("autdev_12", False),  # automation device 12 is paired
        ("autdev_99", True),  # 99 is not
    ],
)
async def test_delete_judges_partitions_and_automation_devices(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    suffix: str,
    expected: bool,
) -> None:
    """The other device kinds upstream covers are judged against the same table."""
    _panel_automation_devices(mock_controller, "12")
    await _setup(hass, mock_config_entry)
    device = _add_device(hass, mock_config_entry, suffix)
    assert (
        await async_remove_config_entry_device(hass, mock_config_entry, device)
        is expected
    )


@pytest.mark.parametrize("suffix", ["camera3", "mystery9"])
async def test_delete_refused_for_a_kind_we_cannot_check(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    caplog: pytest.LogCaptureFixture,
    suffix: str,
) -> None:
    """A camera device does not crash (upstream called state.ip_camera(), which we lack); it is kept."""
    await _setup(hass, mock_config_entry)
    # The vendored library has no ip_camera; make the mock honest about it.
    del mock_controller.state.ip_camera
    device = _add_device(hass, mock_config_entry, suffix)
    caplog.set_level(logging.INFO)
    assert await async_remove_config_entry_device(hass, mock_config_entry, device) is False
    assert "cannot be checked" in caplog.text


async def test_delete_refused_when_the_integration_is_not_loaded(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """No controller, no answer: keep the device (upstream let it go)."""
    mock_config_entry.add_to_hass(hass)
    device = _add_device(hass, mock_config_entry, "zone9")
    assert not hasattr(mock_config_entry, "runtime_data")
    assert await async_remove_config_entry_device(hass, mock_config_entry, device) is False


async def test_delete_refused_while_the_panel_is_not_connected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_controller: MagicMock
) -> None:
    """A disconnected controller's table is stale or still loading: keep the device."""
    await _setup(hass, mock_config_entry)
    device = _add_device(hass, mock_config_entry, "zone9")
    mock_controller.controller_state = ControllerState.RECONNECTING
    assert await async_remove_config_entry_device(hass, mock_config_entry, device) is False


async def test_delete_refused_when_the_panel_lists_no_zones(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_controller: MagicMock
) -> None:
    """An empty table is a bad reading, not a house with no sensors."""
    await _setup(hass, mock_config_entry)
    device = _add_device(hass, mock_config_entry, "zone3")
    _panel_lists(mock_controller)
    assert await async_remove_config_entry_device(hass, mock_config_entry, device) is False


def test_panel_has_device_answers_for_every_kind() -> None:
    """The three answers of the helper, on a bare controller mock."""
    controller = MagicMock()
    controller.controller_state = ControllerState.CONNECTED
    _panel_lists(controller, "1")
    _panel_partitions(controller, "0")
    _panel_automation_devices(controller, "12")
    uid = "aa:bb"

    def dev(*idents: str) -> dr.DeviceEntry:
        return dr.DeviceEntry(
            config_entry_id="e", identifiers={(DOMAIN, i) for i in idents}, name="d"
        )

    assert panel_has_device(controller, uid, dev(uid)) is True
    assert panel_has_device(controller, uid, dev(f"{uid}_zone1")) is True
    assert panel_has_device(controller, uid, dev(f"{uid}_zone2")) is False
    assert panel_has_device(controller, uid, dev(f"{uid}_partition0")) is True
    assert panel_has_device(controller, uid, dev(f"{uid}_partition1")) is False
    assert panel_has_device(controller, uid, dev(f"{uid}_autdev_12")) is True
    assert panel_has_device(controller, uid, dev(f"{uid}_autdev_13")) is False
    assert panel_has_device(controller, uid, dev(f"{uid}_camera3")) is None
    assert panel_has_device(controller, uid, dev("other:id_zone1")) is None
    # Another integration's identifier on the same device is ignored.
    other = dr.DeviceEntry(
        config_entry_id="e",
        identifiers={("other_domain", f"{uid}_zone2"), (DOMAIN, f"{uid}_zone1")},
        name="d",
    )
    assert panel_has_device(controller, uid, other) is True


# --------------------------------------------------------------------------
# Rule 3: the timer and the Repair


async def test_timer_starts_for_a_missing_zone_and_no_issue_yet(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    hass_storage: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """First valid reading without zone 9: its clock starts, nothing is raised, the time is saved."""
    mock_config_entry.add_to_hass(hass)
    devices = _add_zone_devices(hass, mock_config_entry, "1", "9")
    caplog.set_level(logging.INFO)
    watch = await _setup(hass, mock_config_entry)

    assert set(watch.first_missing) == {devices["9"].id}
    assert _issue(hass, devices["9"]) is None
    assert _issue(hass, devices["1"]) is None
    assert "a repair is raised if it stays missing for 7 days" in caplog.text
    saved = hass_storage[storage_key(mock_config_entry.entry_id)]["data"]
    assert set(saved["first_missing"]) == {devices["9"].id}


async def test_issue_is_raised_at_seven_days_and_not_before(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Every check up to 6 days 18 hours raises nothing; the one at 7 days raises."""
    mock_config_entry.add_to_hass(hass)
    devices = _add_zone_devices(hass, mock_config_entry, "1", "9")
    await _setup(hass, mock_config_entry)

    # One check per CHECK_INTERVAL (6 hours): 27 of them end at 6 days 18 hours.
    checks_before = STALE_AFTER // CHECK_INTERVAL - 1
    assert checks_before == 27
    for _ in range(checks_before):
        await _tick(hass, freezer, CHECK_INTERVAL)
        assert _issue(hass, devices["9"]) is None

    await _tick(hass, freezer, CHECK_INTERVAL)  # 7 days exactly
    issue = _issue(hass, devices["9"])
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "stale_zone"
    assert issue.translation_placeholders["zone_id"] == "9"
    assert issue.translation_placeholders["name"] == devices["9"].name
    assert issue.translation_placeholders["days"] == "7"
    assert _issue(hass, devices["1"]) is None


async def test_the_periodic_check_runs_every_interval(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The judgment is wired to the clock: a zone that reappears is noticed at the next interval."""
    mock_config_entry.add_to_hass(hass)
    devices = _add_zone_devices(hass, mock_config_entry, "1", "9")
    watch = await _setup(hass, mock_config_entry)
    assert devices["9"].id in watch.first_missing

    _panel_lists(mock_controller, "1", "9")
    await _tick(hass, freezer, CHECK_INTERVAL - timedelta(minutes=1))
    assert devices["9"].id in watch.first_missing  # not due yet
    await _tick(hass, freezer, timedelta(minutes=2))
    assert watch.first_missing == {}


async def test_timer_persists_across_a_reload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Unload, wait seven days, load again: the saved first-missing time raises the issue at once."""
    mock_config_entry.add_to_hass(hass)
    devices = _add_zone_devices(hass, mock_config_entry, "1", "9")
    await _setup(hass, mock_config_entry)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert get_watch(hass, mock_config_entry.entry_id) is None

    freezer.tick(STALE_AFTER + timedelta(hours=1))
    watch = await _setup(hass, mock_config_entry)

    assert devices["9"].id in watch.first_missing
    issue = _issue(hass, devices["9"])
    assert issue is not None
    assert issue.translation_placeholders["days"] == "7"


async def test_issue_and_timer_clear_when_the_zone_reappears(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The panel lists zone 9 again: Repair gone, clock gone, device untouched."""
    mock_config_entry.add_to_hass(hass)
    devices = _add_zone_devices(hass, mock_config_entry, "1", "9")
    watch = await _setup(hass, mock_config_entry)
    await _tick(hass, freezer, STALE_AFTER + timedelta(hours=1))
    assert _issue(hass, devices["9"]) is not None

    caplog.set_level(logging.INFO)
    _panel_lists(mock_controller, "1", "9")
    await _tick(hass, freezer, CHECK_INTERVAL)

    assert _issue(hass, devices["9"]) is None
    assert watch.first_missing == {}
    assert "is back on the panel" in caplog.text
    assert _device_exists(hass, devices["9"])


async def test_issue_and_timer_clear_when_the_device_is_deleted(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict,
) -> None:
    """Delete from the device page: the integration says yes and forgets the zone."""
    mock_config_entry.add_to_hass(hass)
    devices = _add_zone_devices(hass, mock_config_entry, "1", "9")
    watch = await _setup(hass, mock_config_entry)
    await _tick(hass, freezer, STALE_AFTER + timedelta(hours=1))
    assert _issue(hass, devices["9"]) is not None

    assert (
        await async_remove_config_entry_device(hass, mock_config_entry, devices["9"])
        is True
    )
    assert _issue(hass, devices["9"]) is None
    assert watch.first_missing == {}
    saved = hass_storage[storage_key(mock_config_entry.entry_id)]["data"]
    assert saved["first_missing"] == {}


async def test_a_device_removed_by_another_route_is_forgotten(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A device gone from the registry (removed with its entities, say) drops its clock and Repair at the next check."""
    mock_config_entry.add_to_hass(hass)
    devices = _add_zone_devices(hass, mock_config_entry, "1", "9")
    watch = await _setup(hass, mock_config_entry)
    await _tick(hass, freezer, STALE_AFTER + timedelta(hours=1))
    assert _issue(hass, devices["9"]) is not None

    dr.async_get(hass).async_remove_device(devices["9"].id)
    await _tick(hass, freezer, CHECK_INTERVAL)

    assert _issue(hass, devices["9"]) is None
    assert watch.first_missing == {}


async def test_nothing_is_ever_deleted_automatically(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Rule 2: seven days, a reload and a reconnect later the stale device is still there."""
    mock_config_entry.add_to_hass(hass)
    devices = _add_zone_devices(hass, mock_config_entry, "1", "9")
    await _setup(hass, mock_config_entry)
    await _tick(hass, freezer, STALE_AFTER + A_DAY)
    assert _issue(hass, devices["9"]) is not None

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _device_exists(hass, devices["9"])

    with patch(
        "homeassistant.helpers.device_registry.DeviceRegistry.async_remove_device"
    ) as remove:
        await _tick(hass, freezer, CHECK_INTERVAL)
        remove.assert_not_called()
    assert _device_exists(hass, devices["9"])
    assert _device_exists(hass, devices["1"])


async def test_a_reconnect_triggers_a_check(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
) -> None:
    """RECONNECTING then CONNECTED (a full re-read of the table) judges right away."""
    mock_config_entry.add_to_hass(hass)
    devices = _add_zone_devices(hass, mock_config_entry, "1", "9")
    watch = await _setup(hass, mock_config_entry)
    assert devices["9"].id in watch.first_missing

    status_callback = next(
        call.args[1]
        for call in mock_controller.state.register.call_args_list
        if call.args[0] is QolsysNotification.PANEL_STATUS_UPDATE
    )
    _panel_lists(mock_controller, "1", "9")
    mock_controller.controller_state = ControllerState.RECONNECTING
    status_callback()
    await hass.async_block_till_done()
    assert devices["9"].id in watch.first_missing  # no judgment while reconnecting

    mock_controller.controller_state = ControllerState.CONNECTED
    status_callback()
    await hass.async_block_till_done()
    assert watch.first_missing == {}


# --------------------------------------------------------------------------
# Rule 4: a bad reading never counts


async def test_no_judgment_while_not_connected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_controller: MagicMock
) -> None:
    """Mid-connect the table may be partial: nothing starts."""
    mock_config_entry.add_to_hass(hass)
    _add_zone_devices(hass, mock_config_entry, "1", "9")
    watch = await _setup(hass, mock_config_entry)
    watch._first_missing.clear()  # start from nothing

    mock_controller.controller_state = ControllerState.CONNECTING
    await watch.async_check()
    assert watch.first_missing == {}


async def test_an_empty_zone_list_starts_no_timer(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_controller: MagicMock
) -> None:
    """The panel lists nothing: every known zone is "missing", and none gets a clock."""
    _panel_lists(mock_controller)
    mock_config_entry.add_to_hass(hass)
    _add_zone_devices(hass, mock_config_entry, "1", "2", "3")
    watch = await _setup(hass, mock_config_entry)
    assert watch.first_missing == {}


async def test_an_empty_zone_list_neither_raises_nor_clears(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A running clock is left alone through an empty reading; a later good reading judges it."""
    mock_config_entry.add_to_hass(hass)
    devices = _add_zone_devices(hass, mock_config_entry, "1", "9")
    watch = await _setup(hass, mock_config_entry)
    started = watch.first_missing[devices["9"].id]

    _panel_lists(mock_controller)
    await _tick(hass, freezer, STALE_AFTER + A_DAY)
    assert _issue(hass, devices["9"]) is None  # not raised from a bad reading
    assert watch.first_missing == {devices["9"].id: started}  # not cleared either
    assert _issue(hass, devices["1"]) is None

    _panel_lists(mock_controller, "1")
    await _tick(hass, freezer, CHECK_INTERVAL)
    assert _issue(hass, devices["9"]) is not None  # the good reading judges


async def test_mass_missing_is_ignored(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Five known zones, the panel lists two: three missing is more than max(2, 1), so no clock starts."""
    _panel_lists(mock_controller, "1", "2")
    mock_config_entry.add_to_hass(hass)
    _add_zone_devices(hass, mock_config_entry, "1", "2", "3", "4", "5")
    caplog.set_level(logging.WARNING)
    watch = await _setup(hass, mock_config_entry)
    assert watch.first_missing == {}
    assert "3 missing at once is more than 2, so this reading is ignored" in caplog.text


async def test_the_glitch_threshold_scales_with_the_install(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_controller: MagicMock
) -> None:
    """Twenty known zones allow five missing (a quarter); six is a glitch. Two is always allowed."""
    assert MAX_MISSING_FLOOR == 2
    assert MAX_MISSING_SHARE == 0.25
    all_ids = [str(i) for i in range(1, 21)]
    mock_config_entry.add_to_hass(hass)
    devices = _add_zone_devices(hass, mock_config_entry, *all_ids)

    _panel_lists(mock_controller, *all_ids[6:])  # 6 missing of 20
    watch = await _setup(hass, mock_config_entry)
    assert watch.first_missing == {}

    _panel_lists(mock_controller, *all_ids[5:])  # 5 missing of 20
    await watch.async_check()
    assert set(watch.first_missing) == {devices[z].id for z in all_ids[:5]}

    # A small install: 3 known, 2 missing is allowed (the floor), 3 is empty-list territory.
    watch._first_missing.clear()
    for zid in all_ids[3:]:
        dr.async_get(hass).async_remove_device(devices[zid].id)
    _panel_lists(mock_controller, "1")
    await watch.async_check()
    assert set(watch.first_missing) == {devices["2"].id, devices["3"].id}


async def test_removing_the_integration_removes_its_repairs_and_store(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict,
) -> None:
    """Home Assistant does not clear an integration's issues on entry removal; we do, then the store goes."""
    mock_config_entry.add_to_hass(hass)
    devices = _add_zone_devices(hass, mock_config_entry, "1", "9")
    await _setup(hass, mock_config_entry)
    await _tick(hass, freezer, STALE_AFTER + A_DAY)
    assert _issue(hass, devices["9"]) is not None
    key = storage_key(mock_config_entry.entry_id)
    assert key in hass_storage

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert _issue(hass, devices["9"]) is None
    assert key not in hass_storage


async def test_a_corrupt_store_is_ignored(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_controller: MagicMock,
    hass_storage: dict,
) -> None:
    """Garbage in the store starts from nothing instead of failing setup."""
    hass_storage[storage_key(mock_config_entry.entry_id)] = {
        "version": 1,
        "data": {"first_missing": "not a dict"},
    }
    mock_config_entry.add_to_hass(hass)
    devices = _add_zone_devices(hass, mock_config_entry, "9")
    watch = await _setup(hass, mock_config_entry)
    assert set(watch.first_missing) == {devices["9"].id}
