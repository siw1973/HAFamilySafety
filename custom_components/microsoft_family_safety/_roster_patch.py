"""Roster-list tolerance patch for ``pyfamilysafety.FamilySafety``.

``_pyfamilysafety_compat._patch_account_roster_tolerance`` already makes the
*per-member* requests survive Microsoft's
``Family.UnableToFindTargetResource`` / ``RosterError`` 404 (issue #42).  It
patches ``Account.update``, which is the second half of the roster load.

It does not, and cannot, protect the *first* half.  ``FamilySafety.create``
fetches the roster **list** before any ``Account`` object exists::

    self = cls(await FamilySafetyAPI.create(token, use_refresh_token))
    accounts = await self.api.async_get_accounts()          # <-- 404 here
    self.accounts = await Account.from_dict(self.api, ...)

When a device Microsoft can no longer resolve is enrolled in the family --- a
reset machine, or, as seen here, a school/work (Entra ID / Intune) device that
is invisible in the Family Safety UI and therefore cannot be removed by the
parent --- that first call returns the same 404.  ``create`` has no handler, so
the ``HttpException`` escapes, the coordinator turns it into ``UpdateFailed``,
Home Assistant turns *that* into ``ConfigEntryNotReady`` and the integration
retries with exponential backoff forever.  Not one entity is ever created, even
though the browser/add-on web path is perfectly healthy and every other family
member resolves fine.

This module closes that gap at the only layer that is actually deployable.  The
equivalent library fix exists on ``siw1973/pyfamilysafety`` branch
``fix/roster-tolerance``, but ``pyfamilysafety`` is installed inside the Home
Assistant container and cannot be replaced from the HAOS shell, so it cannot be
delivered to a running instance.  ``custom_components`` can, which is precisely
why ``_pyfamilysafety_compat`` exists for seven other library defects.

Behaviour after patching:

* a stale-roster 404 on the roster list leaves ``FamilySafety.accounts == []``
  instead of raising, so ``FamilySafety.create`` returns an object;
* the coordinator's ``_async_setup_api`` therefore completes, which means
  ``self.api`` **and** ``self.web_api`` are both constructed --- the latter
  matters, because ``web_api`` is built from ``self.api.api.authenticator`` on
  the line after the one that used to throw;
* ``_async_update_data`` then finds an empty roster and enters the existing
  degraded-mode branch, recovering members from the entity registry and
  fetching schedule/policy data over the web/add-on path;
* any error that is *not* a stale-roster 404 propagates completely unchanged.

The patch is idempotent and safe to call on every setup.
"""
from __future__ import annotations

import logging
from typing import Any

from pyfamilysafety import FamilySafety
from pyfamilysafety.account import Account
from pyfamilysafety.api import FamilySafetyAPI

from ._pyfamilysafety_compat import is_stale_roster_error

_LOGGER = logging.getLogger(__name__)

_CREATE_PATCH_MARKER = "_hafs_roster_list_tolerance_patch"
_PENDING_PATCH_MARKER = "_hafs_pending_requests_tolerance_patch"

#: Logged once per process so a permanently unresolvable roster does not fill
#: the log with an identical WARNING on every restart of the config entry.
_CREATE_WARNED = False


def _stale(err: Exception) -> bool:
    """True when ``err`` is Microsoft's roster "cannot resolve node" answer.

    Deliberately typed on the *raw* Microsoft text.  Never call this on a
    message the integration has already rewritten for the user: the
    human-readable string contains none of the markers and the test silently
    returns False.
    """
    return is_stale_roster_error(str(err))


def _patch_roster_list_tolerance() -> bool:
    """Let ``FamilySafety.create`` survive a 404 on the roster list."""
    if getattr(FamilySafety.create, _CREATE_PATCH_MARKER, False):
        return False

    async def _patched_create(
        cls,
        token: Any,
        use_refresh_token: bool = False,
        experimental: bool = False,
    ) -> FamilySafety:
        """Tolerant re-implementation of ``FamilySafety.create``.

        Mirrors pyfamilysafety 1.1.2 step for step; the only difference is
        that a stale-roster failure yields an empty roster instead of
        aborting the whole integration.
        """
        global _CREATE_WARNED

        self = cls(await FamilySafetyAPI.create(token, use_refresh_token))

        try:
            accounts = await self.api.async_get_accounts()
            self.accounts = await Account.from_dict(
                self.api, accounts.get("json"), experimental
            )
        except Exception as err:  # noqa: BLE001 - re-raised unless stale roster
            if not _stale(err):
                raise
            self.accounts = []
            _LOGGER.log(
                logging.DEBUG if _CREATE_WARNED else logging.WARNING,
                "Microsoft could not resolve the Family Safety roster list "
                "(%s). Starting with an empty roster so the integration can "
                "run in degraded mode over the web/add-on path instead of "
                "failing setup. This is caused by a device still enrolled in "
                "the family that Microsoft cannot resolve - typically a reset "
                "machine, or a school/work (Entra ID / Intune) device that is "
                "not listed in the Family Safety UI and so cannot be removed "
                "by a parent. Screen-time usage and per-device data stay "
                "unavailable until Microsoft resolves the roster again; "
                "schedules and policy continue to work.",
                str(err)[:160],
            )
            _CREATE_WARNED = True

        # from_dict returns None for an empty/garbled payload in 1.1.2; every
        # downstream consumer iterates self.accounts unguarded.
        if self.accounts is None:
            self.accounts = []

        self.experimental = experimental
        if experimental:
            try:
                await self._get_pending_requests()
            except Exception as err:  # noqa: BLE001 - re-raised unless stale roster
                if not _stale(err):
                    raise
                self.pending_requests = []
                _LOGGER.debug(
                    "Pending-requests fetch hit the same roster error during "
                    "setup; continuing with none: %s",
                    str(err)[:160],
                )

        return self

    setattr(_patched_create, _CREATE_PATCH_MARKER, True)
    FamilySafety.create = classmethod(_patched_create)
    return True


def _patch_pending_requests_tolerance() -> bool:
    """Keep ``FamilySafety.update`` alive when pending requests 404.

    ``FamilySafety.update`` gathers ``_get_pending_requests()`` together with
    every ``account.update()`` and only catches ``AggregatorException``.  With
    an empty roster the pending-requests call is the *only* coroutine left, so
    an unhandled 404 there would once again abort every coordinator poll --
    reintroducing the original failure one layer further down.
    """
    original = FamilySafety._get_pending_requests
    if getattr(original, _PENDING_PATCH_MARKER, False):
        return False

    async def _patched_get_pending_requests(self) -> list:
        try:
            return await original(self)
        except Exception as err:  # noqa: BLE001 - re-raised unless stale roster
            if not _stale(err):
                raise
            _LOGGER.debug(
                "Microsoft could not resolve pending requests (%s); "
                "continuing with none.",
                str(err)[:160],
            )
            self.pending_requests = []
            return self.pending_requests

    setattr(_patched_get_pending_requests, _PENDING_PATCH_MARKER, True)
    FamilySafety._get_pending_requests = _patched_get_pending_requests
    return True


def apply_roster_patches() -> None:
    """Apply the roster-list tolerance patches (idempotent).

    Failure to patch is logged but never fatal: an unpatched instance behaves
    exactly as it did before, so a future pyfamilysafety refactor degrades to
    the old behaviour rather than breaking setup outright.
    """
    applied: list[str] = []
    try:
        if _patch_roster_list_tolerance():
            applied.append("roster list 404 tolerance")
        if _patch_pending_requests_tolerance():
            applied.append("pending requests 404 tolerance")
    except Exception:  # noqa: BLE001 - never let a patch failure break setup
        _LOGGER.exception(
            "Could not apply the Family Safety roster tolerance patches; "
            "the integration will fall back to the unpatched pyfamilysafety "
            "behaviour and may fail to set up while the roster is broken"
        )
        return

    if applied:
        _LOGGER.debug(
            "Applied Family Safety roster tolerance patches (%s)",
            "; ".join(applied),
        )
