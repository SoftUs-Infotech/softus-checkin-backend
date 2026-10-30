"""Who is calling.

The one definition of a request's address — the audit trail records it, so it
must not be something the caller can type. Punching is not restricted by
network or location: any computer may punch, and phones and tablets are kept
out by the device rule instead (``app.utils.device``).
"""

import ipaddress
from collections.abc import Iterable

from fastapi import Request

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

UNKNOWN_IP = "unknown"


def _is_trusted_proxy(ip: str | None) -> bool:
    return _in_ranges(ip, settings.trusted_proxies)


def client_ip(request: Request) -> str:
    """The caller's address, believing forwarding headers only where earned.

    This is the single definition of "who is calling", which the audit trail
    records. A header is whatever the sender typed, so reading the left-most
    entry of ``X-Forwarded-For`` would let anybody write any address into the
    record. Two things prevent that, and both are needed.

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


def _in_ranges(ip: str | None, allowlist: Iterable[str] | None) -> bool:
    """Is this address covered by the list? Plain addresses and CIDR both."""
    entries = [str(entry).strip() for entry in (allowlist or []) if str(entry).strip()]
    if not ip or not entries:
        return False

    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False

    for entry in entries:
        try:
            # A bare address parses as a single-host network, so both forms
            # go through the same comparison.
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            logger.warning("Ignoring malformed trusted proxy entry %r.", entry)
            continue
        if address in network:
            return True
    return False
