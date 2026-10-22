"""Where a request came from, and whether that counts as the office.

The office restriction is opt-in policy an administrator turns on, so the
decision is deliberately two-sided: the caller's address must match the
allowlist, *and* the allowlist must contain something. An organisation that
enables enforcement before entering a single range would otherwise let the whole
internet through while believing itself locked down — the failure would be
silent, which is the worst kind.

A malformed entry is skipped rather than raised. Somebody mistyping a range in
the settings screen must not be able to stop the office punching in.

Work from home is the escape hatch, granted per person and optionally
date-bounded. Everything else — the calendar, leave, tasks — stays reachable
from anywhere; only punching and filing a correction are pinned to the network.
"""

import ipaddress
from collections.abc import Iterable
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from fastapi import Request

from app.core.config import settings
from app.core.datetime import date_key, org_today
from app.core.exceptions import PermissionDeniedError
from app.core.logging import get_logger

logger = get_logger(__name__)

UNKNOWN_IP = "unknown"


class NetworkGate(StrEnum):
    """Which action is being restricted, and therefore which setting applies."""

    PUNCH = "PUNCH"
    CORRECTION = "CORRECTION"


_SETTING_FOR_GATE = {
    NetworkGate.PUNCH: "enforce_office_network_for_punch",
    NetworkGate.CORRECTION: "enforce_office_network_for_corrections",
}

# Each message ends with the route that is still open to the employee. Being
# told "no" without being told "instead" is what turns a policy into a support
# ticket.
_MESSAGE_FOR_GATE = {
    NetworkGate.PUNCH: (
        "Punching in and out is only allowed on the office network, and this "
        "request did not come from it. If you worked from somewhere else, file "
        "a correction request for the day and an administrator will regularise "
        "it for you."
    ),
    NetworkGate.CORRECTION: (
        "Correction requests can only be filed from the office network, and "
        "this request did not come from it. Ask your administrator to adjust "
        "the day for you, or file it again from the office."
    ),
}


def _is_trusted_proxy(ip: str | None) -> bool:
    return is_office_ip(ip, settings.trusted_proxies)


def client_ip(request: Request) -> str:
    """The caller's address, believing forwarding headers only where earned.

    This is the single definition of "who is calling" — the audit trail records
    it and the office allowlist judges it, and two definitions would mean those
    two could disagree about the same request.

    It used to read the left-most entry of ``X-Forwarded-For`` unconditionally,
    which made the office-network rule decorative: a header is whatever the
    sender typed, so anybody could punch from anywhere by naming the office's
    address in one. Two things fix that, and both are needed.

    *Who* is asking. The header is consulted only when the machine that opened
    the connection is a configured trusted proxy. Otherwise the socket's own
    peer address is the answer, because that is the one thing on a request that
    cannot be forged.

    *Which entry*. A proxy **appends** the peer it saw, so the chain reads
    ``<whatever the client sent>, <what proxy one saw>, <what proxy two saw>``.
    Only the right-hand entries were written by infrastructure; everything left
    of them is the client's own text. So the walk is from the right, skipping
    hops that are themselves trusted proxies, and the first address that is not
    one is the real caller.

    If every entry is a trusted proxy — or the header is malformed — the peer
    address stands. Falling back to something a caller controls would put the
    hole straight back.
    """
    peer = request.client.host if request.client else None

    if not _is_trusted_proxy(peer):
        return peer or UNKNOWN_IP

    forwarded = request.headers.get("x-forwarded-for") or ""
    for entry in reversed([hop.strip() for hop in forwarded.split(",") if hop.strip()]):
        if not _is_trusted_proxy(entry):
            return entry

    # `X-Real-Ip` carries a single address rather than a chain, so there is no
    # walk to do — but it is still only read from a trusted hop.
    real_ip = (request.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip

    return peer or UNKNOWN_IP


def is_office_ip(ip: str | None, allowlist: Iterable[str] | None) -> bool:
    """Is this address covered by the allowlist? Plain addresses and CIDR both."""
    entries = [str(entry).strip() for entry in (allowlist or []) if str(entry).strip()]
    if not ip or not entries:
        return False

    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False

    for entry in entries:
        try:
            # A bare address parses as a single-host network, so both forms of
            # allowlist entry go through the same comparison.
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            logger.warning("Ignoring malformed office IP range %r.", entry)
            continue
        if address in network:
            return True
    return False


def _as_date_key(value: Any) -> str | None:
    """Dates are stored as ``YYYY-MM-DD`` strings, but tolerate real dates too."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return date_key(value.date())
    if isinstance(value, date):
        return date_key(value)
    text = str(value).strip()
    return text[:10] or None


def wfh_active(user: dict[str, Any], today: date) -> bool:
    """Is this employee approved to work from home on this day?

    Absent dates mean open-ended, which is how a permanently remote employee is
    represented without inventing a sentinel year.
    """
    if not user.get("wfh_enabled"):
        return False

    day = date_key(today)
    starts = _as_date_key(user.get("wfh_from"))
    ends = _as_date_key(user.get("wfh_to"))

    started = not starts or starts <= day
    not_finished = not ends or day <= ends
    return started and not_finished


async def require_office_network(
    user: dict[str, Any], ip: str | None, *, gate: NetworkGate
) -> None:
    """Enforce the office allowlist for one action. Silent when it is turned off."""
    from app.services import settings as settings_service

    org_settings = await settings_service.get_settings()
    if not org_settings.get(_SETTING_FOR_GATE[gate], False):
        return

    if wfh_active(user, org_today()):
        return

    if is_office_ip(ip, org_settings.get("office_ip_ranges")):
        return

    logger.info(
        "Blocked %s from %s for %s — outside the office network.",
        gate.value,
        ip,
        user.get("email"),
    )
    raise PermissionDeniedError(
        _MESSAGE_FOR_GATE[gate],
        code="OFFICE_NETWORK_REQUIRED",
        details={"ip": ip},
    )


def suggest_range(ip: str | None) -> str | None:
    """The allowlist entry an administrator standing on this network should add.

    A single address is almost never the right entry. An office router's IPv6
    address changes its interface identifier constantly — the ``/64`` prefix the
    provider delegates is the part that stays put, so pinning the full address
    would lock the office out within a day. IPv4 is the opposite: a small office
    has one public address and a ``/24`` would let in the whole neighbouring
    block, so the address itself is the honest entry.

    Returns ``None`` for anything unparseable, including a request that reached
    the API without an address at all.
    """
    if not ip or ip == UNKNOWN_IP:
        return None
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None

    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


async def network_view(ip: str | None) -> dict[str, Any]:
    """What the office-network policy looks like from where this request came.

    Written for the settings screen: an administrator cannot enter the office's
    address if nothing will tell them what it is, and typing what a "what is my
    IP" website reports is wrong the moment a proxy sits in front of the API.
    This reports the address *this service* judges them by, which is the only
    one the allowlist will ever compare against.
    """
    from app.services import settings as settings_service

    org_settings = await settings_service.get_settings()
    ranges = list(org_settings.get("office_ip_ranges") or [])
    resolved = None if not ip or ip == UNKNOWN_IP else ip

    return {
        "your_ip": resolved,
        "suggested_range": suggest_range(resolved),
        "matches_allowlist": is_office_ip(resolved, ranges),
        "office_ip_ranges": ranges,
        "enforce_office_network_for_punch": bool(
            org_settings.get("enforce_office_network_for_punch")
        ),
        "enforce_office_network_for_corrections": bool(
            org_settings.get("enforce_office_network_for_corrections")
        ),
    }
