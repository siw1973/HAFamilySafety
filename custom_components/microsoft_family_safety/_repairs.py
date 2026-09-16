"""Home Assistant Repairs issue for unavailable Family Safety device data.

Microsoft's legacy mobile aggregator stops returning per-member device and
device-usage data for a family that contains a device enrolled in a
work/school (Entra ID) tenant. The integration keeps running -- names,
screen-time schedules and other account settings come from the Family web
API -- but device entities have nothing to report.

That is a state the user can act on, so it belongs in Repairs rather than in
a log line. This module owns the issue and replaces the older persistent
notification, so there is exactly one prompt for one cause.

No device is ever named. The only device-like identifier in Microsoft's
error body is the ``MachineName`` field, which is Microsoft's own front-end
server and differs on every request.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_DEVICE_DATA_UNAVAILABLE = "device_data_unavailable"
LEARN_MORE_URL = "https://github.com/noiwid/HAFamilySafety/issues/42"

_REPAIRS_PATCH_MARKER = "_hafs_device_data_repairs_patch"

#: Per-member endpoints whose failure means "device data is missing".
#: Deliberately narrower than "any roster error": a member who is only
#: missing spending data still has working device entities, and should not
#: raise a device-data issue.
_DEVICE_ENDPOINTS = frozenset({"devices", "screentime_usage"})


def affected_members(accounts_data: dict[str, Any] | None) -> list[str]:
    """Return the display names of members with no device data.

    Sorted so the issue's placeholder text is stable across polls and the
    Repairs card does not appear to change when nothing has.
    """
    names: list[str] = []
    for account_id, account in (accounts_data or {}).items():
        if not isinstance(account, dict):
            continue
        errors = account.get("roster_errors") or {}
        if not _DEVICE_ENDPOINTS.intersection(errors):
            continue
        names.append(str(account.get("first_name") or account_id))
    return sorted(names)


@callback
def async_sync_device_data_issue(
    hass: HomeAssistant, accounts_data: dict[str, Any] | None
) -> None:
    """Raise or clear the device-data issue for the current poll."""
    names = affected_members(accounts_data)
    if not names:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_DEVICE_DATA_UNAVAILABLE)
        return

    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_DEVICE_DATA_UNAVAILABLE,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_DEVICE_DATA_UNAVAILABLE,
        translation_placeholders={"members": ", ".join(names)},
        learn_more_url=LEARN_MORE_URL,
    )


def apply_repairs_patch() -> bool:
    """Swap the roster persistent notification for the Repairs issue.

    Returns True if the patch was installed by this call. Idempotent.
    """
    try:
        # ROSTER_NOTIFICATION_ID is a module-level constant in coordinator.py,
        # not in const.py -- import it from where it actually lives.
        from .coordinator import (
            ROSTER_NOTIFICATION_ID,
            FamilySafetyDataUpdateCoordinator,
        )
    except (ImportError, AttributeError):
        return False

    original = FamilySafetyDataUpdateCoordinator._async_sync_roster_notification
    if getattr(original, _REPAIRS_PATCH_MARKER, False):
        return False

    async def _patched_sync_roster_notification(self, accounts_data) -> None:
        # Clear the legacy persistent notification once per coordinator, so
        # an install upgrading from the notification-based version does not
        # end up with both surfaces showing the same thing.
        if not getattr(self, "_hafs_legacy_notification_cleared", False):
            self._hafs_legacy_notification_cleared = True
            self._roster_notification_sent = False
            try:
                await self.hass.services.async_call(
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": ROSTER_NOTIFICATION_ID},
                )
            except Exception as err:  # noqa: BLE001 - dismissal is best effort
                _LOGGER.debug("Could not dismiss legacy roster notification: %s", err)

        async_sync_device_data_issue(self.hass, accounts_data)

    setattr(_patched_sync_roster_notification, _REPAIRS_PATCH_MARKER, True)
    FamilySafetyDataUpdateCoordinator._async_sync_roster_notification = (
        _patched_sync_roster_notification
    )
    _LOGGER.debug("Device-data Repairs issue installed")
    return True
