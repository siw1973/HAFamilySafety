"""Roster-list tolerance patch for ``pyfamilysafety.FamilySafety`` (1.1.2).

``_pyfamilysafety_compat._patch_account_roster_tolerance`` already makes the
*per-member* requests survive Microsoft's
``Family.UnableToFindTargetResource`` / ``RosterError`` 404 (issue #42).  It
patches ``Account.update``, which is the second half of the roster load.

It does not, and cannot, protect the *first* half.  ``FamilySafety.create``
fetches the roster **list** (``GET {BASE_URL}/v2/roster``) before any
``Account`` object exists.  Verbatim from the 1.1.2 tag::

    self = cls(await FamilySafetyAPI.create(token, use_refresh_token))
    accounts = await self.api.send_request("get_accounts")     # <-- 404 here
    self.accounts = await Account.from_dict(self.api, accounts.get("json"),
                                            experimental)

When a device Microsoft can no longer resolve is enrolled in the family --- a
reset machine, or a school/work (Entra ID / Intune) device that is invisible in
the Family Safety UI and therefore cannot be removed by the parent --- that
first call returns the same 404.  ``create`` has no handler, so the
``HttpException`` escapes, the coordinator turns it into ``UpdateFailed``, Home
Assistant turns *that* into ``ConfigEntryNotReady``, and the integration
retries with exponential backoff forever.  Not one entity is ever created, even
though the browser/add-on web path is healthy and every other family member
resolves fine.

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

Version safety.  ``create`` is re-implemented rather than wrapped, because the
tolerant behaviour has to sit *between* two statements inside it, and because
wrapping would require a second ``FamilySafetyAPI.create`` on the failure path
--- which performs a second token exchange and would burn the rotated refresh
token.  Re-implementation is only safe while the installed ``create`` matches
the one above, so ``_create_is_patchable`` verifies the shape first and the
patch is skipped (loudly) if it does not.  An unpatched instance behaves
exactly as it did before, so a future pyfamilysafety release degrades to the
old behaviour instead of breaking in a new way.

The patch is idempotent and safe to call on every setup.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any

from pyfamilysafety import FamilySafety
from pyfamilysafety.account import Account
from pyfamilysafety.api import FamilySafetyAPI

from ._pyfamilysafety_compat import is_stale_roster_error

_LOGGER = logging.getLogger(__name__)

_CREATE_PATCH_MARKER = "_hafs_roster_list_tolerance_patch"
_PENDING_PATCH_MARKER = "_hafs_pending_requests_tolerance_patch"
_STUB_PATCH_MARKER = "_hafs_empty_account_stub_name_patch"

#: Suffix ``sensor.py``/``switch.py`` append when they build the device name.
_DEVICE_NAME_SUFFIX = " (Family Safety)"

#: ``create`` signature this module re-implements (pyfamilysafety 1.1.2).
_EXPECTED_CREATE_PARAMS = ("cls", "token", "use_refresh_token", "experimental")

#: Logged once per process so a permanently unresolvable roster does not fill
#: the log with an identical WARNING on every restart of the config entry.
_CREATE_WARNED = False


def _installed_version() -> str:
    """Best-effort pyfamilysafety version, for diagnostics only."""
    try:
        from importlib.metadata import version

        return version("pyfamilysafety")
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return "unknown"


def _stale(err: Exception) -> bool:
    """True when ``err`` is Microsoft's roster "cannot resolve node" answer.

    Deliberately tested against the *raw* Microsoft text.  In 1.1.2 a non-2xx
    roster response raises ``HttpException("HTTP Error", status, body)``, and
    ``str()`` of that renders the whole argument tuple, so the response body
    --- which carries ``Family.UnableToFindTargetResource`` --- is present.

    Never call this on a message the integration has already rewritten for the
    user: the human-readable string contains none of the markers and the test
    silently returns False.  That exact mistake is what this module exists to
    stop being load-bearing.
    """
    return is_stale_roster_error(str(err))


async def _fetch_roster(api: Any) -> Any:
    """Fetch the roster list the way the installed library does.

    1.1.2 calls ``send_request("get_accounts")``.  Newer trees expose
    ``async_get_accounts()``.  Prefer the pinned form and fall back, so the
    patch does not depend on a method name that may not exist --- the defect
    that made the first version of this module fail with ``AttributeError``.
    """
    if hasattr(api, "send_request"):
        return await api.send_request("get_accounts")
    fetch = getattr(api, "async_get_accounts", None)
    if fetch is None:
        raise AttributeError(
            "pyfamilysafety API object exposes neither send_request() nor "
            "async_get_accounts(); cannot fetch the family roster"
        )
    return await fetch()


def _create_is_patchable() -> tuple[bool, str]:
    """Verify the installed ``create`` matches the one re-implemented here."""
    raw = FamilySafety.__dict__.get("create")
    if raw is None:
        return False, "FamilySafety.create() does not exist in this version"
    if not isinstance(raw, classmethod):
        return False, "FamilySafety.create is not a classmethod"
    try:
        params = tuple(inspect.signature(raw.__func__).parameters)
    except (TypeError, ValueError):
        return False, "FamilySafety.create signature could not be inspected"
    if params[: len(_EXPECTED_CREATE_PARAMS)] != _EXPECTED_CREATE_PARAMS:
        return False, f"unexpected FamilySafety.create signature {params}"
    return True, ""


def _patch_roster_list_tolerance() -> bool:
    """Let ``FamilySafety.create`` survive a 404 on the roster list."""
    if getattr(FamilySafety.create, _CREATE_PATCH_MARKER, False):
        return False

    ok, why = _create_is_patchable()
    if not ok:
        _LOGGER.error(
            "Not applying the Family Safety roster-list tolerance patch: %s "
            "(pyfamilysafety %s). The integration keeps the unpatched "
            "behaviour, which means an unresolvable device in the family "
            "roster will still prevent setup.",
            why,
            _installed_version(),
        )
        return False

    async def _patched_create(
        cls,
        token: Any,
        use_refresh_token: bool = False,
        experimental: bool = False,
    ) -> FamilySafety:
        """Tolerant re-implementation of pyfamilysafety 1.1.2's create()."""
        global _CREATE_WARNED

        self = cls(await FamilySafetyAPI.create(token, use_refresh_token))

        try:
            accounts = await _fetch_roster(self.api)
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
                str(err)[:200],
            )
            _CREATE_WARNED = True

        # from_dict can return None for an empty or unexpected payload, and
        # every downstream consumer iterates self.accounts unguarded.
        if self.accounts is None:
            self.accounts = []

        self.experimental = experimental
        if experimental:
            try:
                await self._get_pending_requests()
            except Exception as err:  # noqa: BLE001 - re-raised unless stale
                if not _stale(err):
                    raise
                self.pending_requests = []
                _LOGGER.debug(
                    "Pending-requests fetch hit the same roster error during "
                    "setup; continuing with none: %s",
                    str(err)[:200],
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
    an unhandled 404 there would once again abort every coordinator poll ---
    reintroducing the original failure one layer further down, on the first
    poll after a successful start.
    """
    original = getattr(FamilySafety, "_get_pending_requests", None)
    if original is None:
        _LOGGER.debug(
            "FamilySafety._get_pending_requests() absent; skipping that patch"
        )
        return False
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
                str(err)[:200],
            )
            self.pending_requests = []
            return self.pending_requests

    setattr(_patched_get_pending_requests, _PENDING_PATCH_MARKER, True)
    FamilySafety._get_pending_requests = _patched_get_pending_requests
    return True


def _account_name_from_registry(hass: Any, account_id: str) -> tuple[Any, Any]:
    """Recover ``(first_name, surname)`` for a family member.

    Degraded mode has no roster, so ``_empty_account_stub`` fills the account
    dict with ``first_name: None`` / ``surname: None``.  Both entity platforms
    then read it with ``account_data.get(ATTR_FIRST_NAME, "Unknown")`` --- and
    because the key *is* present, ``.get`` returns ``None`` rather than the
    default.  Entities are therefore named ``"None Screen Time"`` and, worse,
    ``DeviceInfo(name=f"{first} {surname} (Family Safety)")`` renames the whole
    device to ``"None None (Family Safety)"``, taking every entity registered
    against it with it.

    The device registry still holds the real name from the last healthy poll,
    so read it back and rebuild the two fields.  Returns ``(None, None)`` when
    the name cannot be recovered, which leaves the stub exactly as it was.
    """
    try:
        from homeassistant.helpers import device_registry as dr

        from .const import DOMAIN

        registry = dr.async_get(hass)
        device = registry.async_get_device(identifiers={(DOMAIN, str(account_id))})
        if device is None:
            return None, None
        raw = (device.name_by_user or device.name or "").strip()
        if raw.endswith(_DEVICE_NAME_SUFFIX):
            raw = raw[: -len(_DEVICE_NAME_SUFFIX)].strip()
        # Never re-absorb a name a previous degraded run already broke.
        parts = [p for p in raw.split() if p and p != "None"]
        if not parts:
            return None, None
        return parts[0], (" ".join(parts[1:]) or None)
    except Exception as err:  # noqa: BLE001 - naming must never break a poll
        _LOGGER.debug("Could not recover the account name for %s: %r", account_id, err)
        return None, None


def _patch_empty_account_stub() -> bool:
    """Keep real family-member names on the degraded-mode account stub.

    Replaces the ``staticmethod`` with a plain function so it binds as an
    instance method and can reach ``self.hass``.  The single call site is
    ``self._empty_account_stub(account_id)``, which is satisfied by either
    form, so this is transparent.
    """
    try:
        from .coordinator import FamilySafetyDataUpdateCoordinator as _Coordinator
    except (ImportError, AttributeError):
        return False

    raw = _Coordinator.__dict__.get("_empty_account_stub")
    if raw is None:
        _LOGGER.debug("Coordinator._empty_account_stub absent; skipping name patch")
        return False
    original = raw.__func__ if isinstance(raw, staticmethod) else raw
    if getattr(original, _STUB_PATCH_MARKER, False):
        return False

    def _patched_empty_account_stub(self, account_id: str) -> dict:
        data = original(account_id)
        first, surname = _account_name_from_registry(self.hass, account_id)
        if first:
            data["first_name"] = first
            if surname:
                data["surname"] = surname
        return data

    setattr(_patched_empty_account_stub, _STUB_PATCH_MARKER, True)
    _Coordinator._empty_account_stub = _patched_empty_account_stub
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
        if _patch_empty_account_stub():
            applied.append("degraded-mode account name preservation")
    except Exception:  # noqa: BLE001 - never let a patch failure break setup
        _LOGGER.exception(
            "Could not apply the Family Safety roster tolerance patches; "
            "the integration will fall back to the unpatched pyfamilysafety "
            "behaviour and may fail to set up while the roster is broken"
        )
        return

    if applied:
        _LOGGER.debug(
            "Applied Family Safety roster tolerance patches for "
            "pyfamilysafety %s (%s)",
            _installed_version(),
            "; ".join(applied),
        )
