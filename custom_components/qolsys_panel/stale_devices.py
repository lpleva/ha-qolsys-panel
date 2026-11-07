"""Devices the panel no longer reports: a manual Delete and a Repair, never automatic deletion.

Our own version of upstream dc5130c (#175, "remove devices no longer present
on the panel"). Upstream deletes, at every Home Assistant start and every
integration reload, every registry device the panel does not report at that
moment, and adds a Delete on the device page for the same set. Two problems
made the automatic pass unacceptable for a panel that guards a house: the pass
has no guard against the panel answering with a partial or empty zone list, so
one bad reload would wipe every door, window and smoke zone along with their
names, areas and dashboard spots; and it calls ``state.ip_camera()``, which the
vendored library does not have, so a camera device would crash it.

What this module does instead:

1. **Manual Delete** (``async_remove_config_entry_device`` in ``__init__``,
   which calls :func:`panel_has_device`): a device can be deleted from its
   device page only when the panel is connected and its full zone table says
   the zone, partition or automation device is gone. Anything the panel still
   lists, the panel's own root device, a kind we cannot judge (``camera``, an
   unknown suffix), a disconnected panel or an empty zone list all refuse.
   The share half of the glitch rule (point 4) does not apply to a manual
   Delete; :func:`panel_has_device` says why.
2. **No automatic deletion, ever.** Nothing here calls
   ``async_remove_device``.
3. **A Repair** once a zone has been missing from the panel for seven days in
   a row (:class:`StaleZoneWatch`). It is not fixable from the Repair itself;
   its text points at the device page's Delete, which is the one action that
   exists for this. The first-missing time is kept per device in a
   ``helpers.storage.Store`` so the seven days survive restarts.
4. **A bad reading never counts.** The judgment runs only while the controller
   is CONNECTED, which the library sets after the full database sync, so the
   zone table is complete; and a reading where the panel lists no zones at
   all, or where more than ``max(2, a quarter of the known zones)`` are missing
   at once, is treated as a glitch: no timer starts or advances, nothing is
   cleared either. Seven days of consistent readings are what raise the
   Repair, so a one-off bad table can only delay it.

Why a Store and not the config entry: entry data is the connection
configuration (host, MAC); writing it fires the entry's update listeners,
shows in diagnostics and would reload the entry in some flows. A Store is Home
Assistant's mechanism for exactly this kind of per-entry bookkeeping; it lives
in ``.storage/``, loads before the first judgment and is removed with the
entry (``async_remove_entry``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import math
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .vendor.qolsys_controller.enum_qolsys import ControllerState

if TYPE_CHECKING:
    from .types import QolsysPanelConfigEntry
    from .vendor.qolsys_controller import qolsys_controller

_LOGGER = logging.getLogger(__name__)

ISSUE_STALE_ZONE = "stale_zone"

# A zone has to be missing from every valid reading for this long before the
# Repair is raised.
STALE_AFTER = timedelta(days=7)
# How often the judgment runs while connected, on top of every (re)connect.
CHECK_INTERVAL = timedelta(hours=6)
# The glitch rule: a reading with more missing zones than this is ignored.
# Up to two zones can be missing in any install (so a small install with one
# or two sensors genuinely removed still gets its Repair); a quarter of the
# known zones once that is more than two (a 20-zone install allows five), so a
# whole-house wipe or a half-read table never starts a clock.
MAX_MISSING_FLOOR = 2
MAX_MISSING_SHARE = 0.25

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}.stale_zones"
DATA_KEY = f"{DOMAIN}_stale_zone_watch"

_ZONE = "zone"
_PARTITION = "partition"
_AUTDEV = "autdev_"


def storage_key(entry_id: str) -> str:
    """The Store key for one config entry."""
    return f"{STORAGE_KEY_PREFIX}.{entry_id}"


def issue_id(device_id: str) -> str:
    """The Repair id for one registry device."""
    return f"{ISSUE_STALE_ZONE}_{device_id}"


def zone_id_of(device: dr.DeviceEntry, unique_id: str) -> str | None:
    """The panel zone id a registry device stands for, or None if it is not a zone device."""
    prefix = f"{unique_id}_{_ZONE}"
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and identifier.startswith(prefix):
            return identifier[len(prefix) :]
    return None


def panel_has_device(
    controller: qolsys_controller, unique_id: str, device: dr.DeviceEntry
) -> bool | None:
    """Does the panel still report this registry device?

    True: the panel lists it (or it is the panel itself), keep it. False: the
    panel's complete table does not list it, it may go. None: cannot tell,
    which the caller must treat as "keep". None covers a controller that is
    not CONNECTED (its table is stale or still loading), a panel that lists no
    zones at all (the glitch rule), a ``camera`` device (the vendored library
    keeps no camera list; upstream's ``state.ip_camera()`` does not exist
    here, which is the crash we avoid) and any identifier we do not know.

    Only the empty-table half of the glitch rule applies here, not the
    ``max(2, a quarter)`` share that :class:`StaleZoneWatch` uses: the share
    protects an unattended clock from a half-read table, while this answers
    one person deleting one device on purpose, and refusing them whenever
    several sensors were replaced together would block the only cleanup
    path in exactly that case.
    """
    if controller.controller_state != ControllerState.CONNECTED:
        return None
    if not list(controller.state.zones):
        return None

    prefix = f"{unique_id}_"
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        if identifier == unique_id:
            return True  # the panel's own device; remove the integration instead
        if not identifier.startswith(prefix):
            continue
        suffix = identifier[len(prefix) :]
        if suffix.startswith(_ZONE):
            return controller.state.zone(suffix[len(_ZONE) :]) is not None
        if suffix.startswith(_PARTITION):
            return controller.state.partition(suffix[len(_PARTITION) :]) is not None
        if suffix.startswith(_AUTDEV):
            return (
                controller.state.automation_device(suffix[len(_AUTDEV) :])
                is not None
            )
    return None


class StaleZoneWatch:
    """Raise a Repair for a zone the panel has not listed for seven days; never delete."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: QolsysPanelConfigEntry,
        controller: qolsys_controller,
    ) -> None:
        """Bind to one config entry's controller."""
        self._hass = hass
        self._entry = entry
        self._controller = controller
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        # device registry id -> ISO timestamp of the first valid reading that
        # did not list the zone.
        self._first_missing: dict[str, str] = {}
        self._unsub_interval: CALLBACK_TYPE | None = None
        self._check_pending = False
        # True after a mass-missing reading was logged at WARNING; cleared by
        # the next valid reading, so a persistent condition logs once.
        self._glitch_reported = False

    @property
    def first_missing(self) -> dict[str, str]:
        """Read-only view of the timers, for tests and diagnostics."""
        return dict(self._first_missing)

    async def async_start(self) -> None:
        """Load the saved timers, judge once now, then every CHECK_INTERVAL."""
        data = await self._store.async_load()
        if isinstance(data, dict) and isinstance(data.get("first_missing"), dict):
            self._first_missing = {
                str(k): str(v) for k, v in data["first_missing"].items()
            }
        self._unsub_interval = async_track_time_interval(
            self._hass, self._on_interval, CHECK_INTERVAL, name="qolsys stale zones"
        )
        await self.async_check()

    @callback
    def stop(self) -> None:
        """Stop the periodic judgment (entry unload)."""
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

    @callback
    def schedule_check(self) -> None:
        """Run a judgment as a task; call on the event loop (``__init__`` does, via call_soon)."""
        if self._check_pending:
            return
        self._check_pending = True
        self._hass.async_create_task(self._async_scheduled_check())

    async def _async_scheduled_check(self) -> None:
        try:
            await self.async_check()
        finally:
            self._check_pending = False

    async def _on_interval(self, _now: datetime) -> None:
        await self.async_check()

    async def async_forget(self, device_id: str) -> None:
        """Drop the timer and the Repair for a device (it was deleted)."""
        ir.async_delete_issue(self._hass, DOMAIN, issue_id(device_id))
        if self._first_missing.pop(device_id, None) is not None:
            await self._async_save()

    async def async_check(self, now: datetime | None = None) -> None:
        """One judgment of the registry's zone devices against the panel's zone table."""
        if self._controller.controller_state != ControllerState.CONNECTED:
            _LOGGER.debug("Stale zone check skipped: panel not connected")
            return
        unique_id = self._entry.unique_id
        if unique_id is None:
            return

        registry = dr.async_get(self._hass)
        known: dict[str, tuple[str, dr.DeviceEntry]] = {}
        for device in dr.async_entries_for_config_entry(registry, self._entry.entry_id):
            zone_id = zone_id_of(device, unique_id)
            if zone_id is not None:
                known[device.id] = (zone_id, device)

        changed = False
        # A device deleted from the registry by any route takes its timer and
        # its Repair with it.
        for device_id in list(self._first_missing):
            if device_id not in known:
                del self._first_missing[device_id]
                ir.async_delete_issue(self._hass, DOMAIN, issue_id(device_id))
                changed = True

        if not known:
            if changed:
                await self._async_save()
            return

        # Glitch rule, part one: a panel that lists no zones at all did not
        # answer; nothing is judged from it.
        if not list(self._controller.state.zones):
            _LOGGER.debug("Stale zone check skipped: the panel lists no zones")
            if changed:
                await self._async_save()
            return

        missing = {
            device_id: (zone_id, device)
            for device_id, (zone_id, device) in known.items()
            if self._controller.state.zone(zone_id) is None
        }

        # Glitch rule, part two: too many gone at once is a bad table, not a
        # house that lost that many sensors.
        allowed = max(MAX_MISSING_FLOOR, math.floor(len(known) * MAX_MISSING_SHARE))
        if len(missing) > allowed:
            # Once at WARNING, then DEBUG while it persists: when that many
            # sensors really were removed, this reading repeats every
            # CHECK_INTERVAL until the devices are deleted.
            _LOGGER.log(
                logging.DEBUG if self._glitch_reported else logging.WARNING,
                "The panel lists %d of the %d zones Home Assistant knows; %d missing"
                " at once is more than %d, so this reading is ignored and no stale-zone"
                " repair will come from it. If those sensors really were removed from"
                " the panel, delete their devices from their device pages: %s",
                len(known) - len(missing),
                len(known),
                len(missing),
                allowed,
                ", ".join(
                    f"zone {zone_id} ({_name(device)})"
                    for zone_id, device in sorted(missing.values(), key=lambda m: m[0])
                ),
            )
            self._glitch_reported = True
            if changed:
                await self._async_save()
            return
        self._glitch_reported = False

        now = now or dt_util.utcnow()

        # Zones the panel lists again: clear the clock and the Repair.
        for device_id in list(self._first_missing):
            if device_id in known and device_id not in missing:
                del self._first_missing[device_id]
                ir.async_delete_issue(self._hass, DOMAIN, issue_id(device_id))
                _LOGGER.info(
                    "Zone %s (%s) is back on the panel", known[device_id][0],
                    _name(known[device_id][1]),
                )
                changed = True

        for device_id, (zone_id, device) in missing.items():
            since_text = self._first_missing.get(device_id)
            since = dt_util.parse_datetime(since_text) if since_text else None
            if since is None:
                self._first_missing[device_id] = now.isoformat()
                changed = True
                _LOGGER.info(
                    "Zone %s (%s) is not on the panel; a repair is raised if it"
                    " stays missing for %d days",
                    zone_id,
                    _name(device),
                    STALE_AFTER.days,
                )
                continue
            gone_for = now - since
            if gone_for < STALE_AFTER:
                continue
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id(device_id),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_STALE_ZONE,
                translation_placeholders={
                    "name": _name(device),
                    "zone_id": zone_id,
                    "days": str(gone_for.days),
                    "since": dt_util.as_local(since).strftime("%Y-%m-%d"),
                },
            )

        if changed:
            await self._async_save()

    async def _async_save(self) -> None:
        await self._store.async_save({"first_missing": dict(self._first_missing)})


async def async_remove_entry_data(hass: HomeAssistant, entry_id: str) -> None:
    """Delete an entry's stale-zone Repairs and its Store (the entry is being removed).

    Home Assistant does not clear an integration's issues when a config entry
    goes, so the Repairs would outlive the devices they point at; the saved
    timers name every device that has one.
    """
    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, storage_key(entry_id))
    data = await store.async_load()
    if isinstance(data, dict) and isinstance(data.get("first_missing"), dict):
        for device_id in data["first_missing"]:
            ir.async_delete_issue(hass, DOMAIN, issue_id(str(device_id)))
    await store.async_remove()


def _name(device: dr.DeviceEntry) -> str:
    return device.name_by_user or device.name or device.id


@callback
def get_watch(hass: HomeAssistant, entry_id: str) -> StaleZoneWatch | None:
    """The watch for a loaded entry, or None."""
    watches: dict[str, StaleZoneWatch] = hass.data.get(DATA_KEY, {})
    return watches.get(entry_id)


@callback
def set_watch(hass: HomeAssistant, entry_id: str, watch: StaleZoneWatch | None) -> None:
    """Register (or with None, forget) the watch for an entry."""
    watches = hass.data.setdefault(DATA_KEY, {})
    if watch is None:
        watches.pop(entry_id, None)
    else:
        watches[entry_id] = watch
