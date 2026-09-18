"""Scoped fallback for the Microsoft Family Safety roster resolution 404.

Background
----------
Microsoft's mobile aggregator endpoint ``GET {BASE_URL}/v2/roster`` (the
library's ``get_accounts`` endpoint) returns **HTTP 404** with
``Family.UnableToFindTargetResource`` / ``RosterError`` when the family
contains a device enrolled in a work/school (Entra ID / MDM) tenant that
Microsoft can no longer resolve. ``FamilySafety.create()`` propagates that
as ``HttpException``, config entry setup fails, and Home Assistant enters an
endless ``ConfigEntryNotReady`` backoff. The offending device is not visible
in the Family Safety UI and cannot be removed by the user.

Design
------
This is a **fallback, not a replacement**. It wraps
``FamilySafetyAPI.send_request`` and does nothing at all unless *every* one
of the following holds:

1. the call raised ``HttpException``;
2. the endpoint was exactly ``get_accounts``;
3. the raw Microsoft response body carries a stale-roster marker;
4. a roster provider has been registered.

Any other call, endpoint or error re-raises completely unchanged. In
particular this never suppresses errors for ``get_screentime_usage`` or
``get_override_device_restrictions``, whose callers index into the response
and would raise ``KeyError`` / ``TypeError`` on an empty payload.

The provider returns the *web* roster
(``account.microsoft.com/family/api/roster``, reached through the
Playwright app's authenticated browser session, the only transport that
works for account-level calls). Its member shape differs from the mobile
one, so the payload is translated rather than substituted --
``Account.from_dict`` is left untouched.

This module also soft-fails ``FamilySafety._get_pending_requests``, which
is called from ``create()`` and again from ``update()`` on every poll and
fails the same way for the same families.

Privacy
-------
The roster payload contains per-member ``jsonWebToken`` relationship
tokens, children's ages, and a ``primaryId`` that is typically an email
address. It is never logged. Only member counts are logged.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from pyfamilysafety.api import FamilySafetyAPI
from pyfamilysafety.exceptions import HttpException

_LOGGER = logging.getLogger(__name__)

#: Markers matched against the RAW Microsoft response body. ``str()`` of a
#: 3-argument ``HttpException`` renders the whole tuple, so the body -- and
#: therefore these markers -- are present in it.
_STALE_ROSTER_MARKERS = (
    "unabletofindtargetresource",
    "rostererror",
    "unable to find the node",
)

#: Async callable returning the raw web-roster ``data`` dict, or None.
RosterProvider = Callable[[], Awaitable[dict | None]]

#: Set on the replacement so a second install is a no-op.
_PENDING_PATCH_MARKER = "_hafs_pending_requests_soft_fail"

_roster_provider: RosterProvider | None = None
_patch_applied = False


def is_stale_roster_error(text: str) -> bool:
    """Return True for Microsoft's roster-resolution 404.

    ``text`` must be the RAW response body (or a string containing it), not
    a rewritten human-readable message -- rewritten prose does not carry
    these markers and will never match.
    """
    lowered = text.lower()
    return any(marker in lowered for marker in _STALE_ROSTER_MARKERS)


def set_roster_provider(provider: RosterProvider | None) -> None:
    """Register the fallback roster source.

    Note: this is process-global. With multiple config entries the most
    recently set up entry provides the fallback for all of them.
    """
    global _roster_provider  # noqa: PLW0603
    _roster_provider = provider


def clear_roster_provider() -> None:
    """Remove the fallback roster source (used on unload)."""
    set_roster_provider(None)


def translate_web_roster(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert a web-roster payload into the mobile shape.

    ``Account.from_dict`` reads ``id``, ``role``, ``profilePicUrl``,
    ``isDigitalSafetyEnabled`` and a nested ``user`` object. The web payload
    has ``puid`` instead of ``id`` and carries the names flat, with no
    ``user`` object and no profile picture, so handing it over untranslated
    would set ``user_id = None`` and then raise ``AttributeError`` on
    ``member["user"]["firstName"]``.

    ``puid`` is the identifier the mobile API uses: it is the value already
    present in this integration's existing entity ``unique_id`` values,
    whereas ``primaryId`` and ``cid`` are not.

    The id is emitted as an **int** when it is all digits, because that is
    what the mobile roster returns and therefore what existing installs have
    in their device registry. This matters: account ``DeviceInfo`` builds
    ``identifiers={(DOMAIN, account_id)}`` from a bare variable, so an int id
    and a str id are two different devices, while every entity ``unique_id``
    is an f-string and is unaffected. Emitting a str here silently created a
    second, empty device per family member and re-homed their entities to it.
    """
    members: list[dict[str, Any]] = []
    for member in payload.get("members") or []:
        if not isinstance(member, dict):
            continue
        puid = member.get("puid")
        if puid is None:
            continue
        # Preserve the mobile roster's type -- see the note in the docstring.
        if isinstance(puid, str) and puid.isdigit():
            account_id: Any = int(puid)
        else:
            account_id = puid
        members.append(
            {
                "id": account_id,
                "role": member.get("role"),
                "profilePicUrl": None,
                "isDigitalSafetyEnabled": member.get("isDigitalSafetyEnabled"),
                "user": {
                    "firstName": member.get("firstName"),
                    "lastName": member.get("lastName"),
                },
            }
        )
    return {"members": members}


async def _fallback_roster_response() -> dict[str, Any] | None:
    """Build a send_request-shaped response from the web roster, or None."""
    provider = _roster_provider
    if provider is None:
        _LOGGER.debug("Roster fallback unavailable: no provider registered")
        return None

    try:
        payload = await provider()
    except Exception as err:  # noqa: BLE001 - fallback must never mask the 404
        _LOGGER.warning("Roster fallback provider failed: %s", err)
        return None

    if not isinstance(payload, dict):
        _LOGGER.warning("Roster fallback returned no usable payload")
        return None

    translated = translate_web_roster(payload)
    if not translated["members"]:
        _LOGGER.warning("Roster fallback returned zero usable members")
        return None

    _LOGGER.info(
        "Roster resolution 404 recovered via the Family Safety app: "
        "%d of %d members usable",
        len(translated["members"]),
        len(payload.get("members") or []),
    )
    # Mirror FamilySafetyAPI.send_request's success shape exactly.
    return {
        "status": 200,
        "text": json.dumps(translated),
        "json": translated,
        "headers": {},
    }


def _install_pending_requests_soft_fail() -> None:
    """Stop unresolvable pending requests aborting setup and every poll.

    ``FamilySafety._get_pending_requests`` is called from ``create()`` and
    again from ``update()`` on every poll, and ``update()`` only swallows
    ``AggregatorException``. When Microsoft cannot resolve the family on the
    mobile aggregator, ``/v1/PendingRequests`` fails the same way the roster
    does, which kills setup and then every refresh.

    Pending requests are an optional dataset: none is a valid answer. Only
    the specific "cannot resolve" errors are softened; anything else raises.
    """
    from pyfamilysafety import FamilySafety
    from ._pyfamilysafety_compat import is_unresolvable_member_error

    original = FamilySafety._get_pending_requests
    if getattr(original, _PENDING_PATCH_MARKER, False):
        return

    async def _patched_get_pending_requests(self: Any):
        try:
            return await original(self)
        except HttpException as err:
            if not is_unresolvable_member_error(str(err)):
                raise
            _LOGGER.warning(
                "Microsoft could not resolve pending screen-time requests for "
                "this family (%s); continuing with none. Account data is "
                "unaffected.",
                str(err)[:120],
            )
            self.pending_requests = []
            return []

    setattr(_patched_get_pending_requests, _PENDING_PATCH_MARKER, True)
    FamilySafety._get_pending_requests = _patched_get_pending_requests
    _LOGGER.debug("Pending-requests soft fail installed")


def install_roster_fallback() -> None:
    """Install the scoped get_accounts patch only. Idempotent.

    Separated from :func:`apply_roster_patches` so the patch can be
    exercised without a Home Assistant instance.
    """
    global _patch_applied  # noqa: PLW0603
    if _patch_applied:
        return

    original_send_request = FamilySafetyAPI.send_request

    async def _send_request_with_roster_fallback(
        self: FamilySafetyAPI,
        endpoint: str,
        body: object = None,
        headers: dict | None = None,
        platform: str | None = None,
        **kwargs: Any,
    ):
        try:
            return await original_send_request(
                self,
                endpoint,
                body=body,
                headers=headers,
                platform=platform,
                **kwargs,
            )
        except HttpException as err:
            # Scope gate 1: only the roster listing.
            if endpoint != "get_accounts":
                raise
            # Scope gate 2: only Microsoft's roster-resolution failure.
            if not is_stale_roster_error(str(err)):
                raise
            fallback = await _fallback_roster_response()
            if fallback is None:
                raise
            return fallback

    _send_request_with_roster_fallback.__wrapped__ = original_send_request
    FamilySafetyAPI.send_request = _send_request_with_roster_fallback
    _install_pending_requests_soft_fail()
    _patch_applied = True
    _LOGGER.debug("Scoped get_accounts roster fallback installed")


def apply_roster_patches(hass: Any, entry: Any) -> None:
    """Install the fallback and point it at this entry's Family Safety app.

    Must run before ``FamilySafety.create()``.

    The app client is constructed here rather than reusing the
    coordinator's, so the coordinator needs no modification. It is a thin
    object: it resolves the app URL through the Supervisor API and reads
    the shared API key on first use, and holds no other state.
    """
    install_roster_fallback()

    # Imported lazily: this module is imported from the package __init__,
    # and a lazy import keeps that path free of any ordering concern.
    from .auth.addon_client import AddonCookieClient
    from .const import CONF_API_KEY, CONF_AUTH_URL

    # Same precedence the coordinator uses, deliberately mirrored rather
    # than "corrected" here.
    auth_url = entry.options.get(CONF_AUTH_URL) or entry.data.get(CONF_AUTH_URL)
    api_key = entry.options.get(CONF_API_KEY) or entry.data.get(CONF_API_KEY)
    client = AddonCookieClient(hass, auth_url=auth_url, api_key=api_key)
    set_roster_provider(client.fetch_roster)
