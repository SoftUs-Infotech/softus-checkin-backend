"""Coordinates turned into something a person can read.

Two jobs, both about the same pair of numbers: how far a punch was from the
office, and what street it happened on.

Two things come back from a lookup: the full address, which belongs in the
day's detail, and the *place* — a town or a city — which is what a register
column has room for and what an administrator actually reads. "Surat" answers
"where was this punch" far better than a street line, and immeasurably better
than the raw IP address that used to sit in that column.

Reverse geocoding is a convenience and never a requirement. With
``GOOGLE_MAPS_API_KEY`` set, Google answers; without it the lookup falls back to
OpenStreetMap's Nominatim, which needs no key. If both are slow, rate-limiting
or simply down, the punch is stored with its coordinates and nothing else.
Failing somebody's attendance because a third party had a bad afternoon would be
indefensible, so :func:`reverse_geocode` swallows everything and answers a place
with both fields empty.

Answers are cached by coordinate rounded to four decimal places — roughly eleven
metres, so everybody punching from the same building shares one row — and expire
after thirty days, because street names and business names do change.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx

from app.core.config import settings
from app.core.datetime import utc_now
from app.core.logging import get_logger
from app.db.mongo import Collections, get_collection

logger = get_logger(__name__)

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

#: The keyless fallback. Nominatim asks callers to identify themselves and to
#: stay under one request a second; the coordinate cache below keeps a whole
#: office to a single lookup, which sits comfortably inside that.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_USER_AGENT = "SoftusCheckin/1.0 (attendance; reverse geocoding for punch records)"

#: Google's component types that mean "the place a person would name", most
#: specific first. A punch in a village should say the village, not the state.
_GOOGLE_PLACE_TYPES = (
    "locality",
    "postal_town",
    "administrative_area_level_3",
    "administrative_area_level_2",
)

#: The same idea in Nominatim's vocabulary, and the order matters more than it
#: looks. ``state_district`` sits above ``county`` because in India that is where
#: the city goes: a punch in Surat comes back as ``county: "Adajan Taluka"`` and
#: ``state_district: "Surat"``, and a taluka is not how anybody says where they
#: were. Elsewhere ``city``/``town`` answer first and the question never arises.
_OSM_PLACE_KEYS = (
    "city",
    "town",
    "village",
    "municipality",
    "state_district",
    "county",
    "state",
)

#: Four decimals ≈ 11 m. Coarse enough to share a cache row across a building,
#: fine enough that two neighbouring offices do not collide.
CACHE_PRECISION = 4
CACHE_TTL_SECONDS = 30 * 24 * 60 * 60

#: Short on purpose: this call sits inside a punch request.
REQUEST_TIMEOUT_SECONDS = 4.0



@dataclass(frozen=True)
class Place:
    """What a pair of coordinates resolves to. Either field may be empty."""

    address: str | None = None
    city: str | None = None

    def __bool__(self) -> bool:
        return bool(self.address or self.city)


NOWHERE = Place()


def _cache():
    return get_collection(Collections.GEOCODE_CACHE)


def cache_key(latitude: float, longitude: float) -> str:
    return f"{latitude:.{CACHE_PRECISION}f},{longitude:.{CACHE_PRECISION}f}"



def _pick(mapping: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """The first key that has a value, in the order given. Most specific wins."""
    for key in keys:
        value = mapping.get(key)
        if value:
            return str(value)
    return None


async def _fetch_google(latitude: float, longitude: float, api_key: str) -> Place:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.get(
            GEOCODE_URL,
            params={"latlng": f"{latitude},{longitude}", "key": api_key},
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()

    if body.get("status") != "OK":
        logger.info("Geocoding returned %s for %s,%s.", body.get("status"), latitude, longitude)
        return NOWHERE

    results = body.get("results") or []
    if not results:
        return NOWHERE

    # The place can sit in any of the results, not only the first: the nearest
    # match to a coordinate is often a building, whose components stop at the
    # street. Walking outwards is exactly the widening Google's own ordering
    # describes.
    city = None
    for result in results:
        for component in result.get("address_components") or []:
            types = tuple(component.get("types") or ())
            if any(place_type in types for place_type in _GOOGLE_PLACE_TYPES):
                city = component.get("long_name")
                break
        if city:
            break

    return Place(address=results[0].get("formatted_address") or None, city=city or None)


async def _fetch_osm(latitude: float, longitude: float) -> Place:
    """The keyless provider. Slower and coarser than Google, and free."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.get(
            NOMINATIM_URL,
            params={
                "lat": latitude,
                "lon": longitude,
                "format": "jsonv2",
                # 10 is roughly a town — enough detail to name the place without
                # dragging in a house number nobody reads in a register column.
                "zoom": 14,
                "addressdetails": 1,
            },
            headers={"User-Agent": NOMINATIM_USER_AGENT, "Accept-Language": "en"},
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()

    if body.get("error"):
        logger.info("Nominatim returned %s for %s,%s.", body["error"], latitude, longitude)
        return NOWHERE

    address = body.get("address") or {}
    return Place(
        address=body.get("display_name") or None,
        city=_pick(address, _OSM_PLACE_KEYS),
    )


async def _fetch_place(latitude: float, longitude: float) -> Place:
    """The one seam that reaches the network. Tests replace this and nothing else.

    Google when there is a key for it, OpenStreetMap otherwise — so an
    organisation that has not bought a key still gets "Surat" against a punch
    rather than a bare pair of numbers.
    """
    if settings.GOOGLE_MAPS_API_KEY:
        return await _fetch_google(latitude, longitude, settings.GOOGLE_MAPS_API_KEY)
    return await _fetch_osm(latitude, longitude)


async def _lookup(latitude: float, longitude: float) -> Place:
    key = cache_key(latitude, longitude)

    cached = await _cache().find_one({"key": key})
    if cached is not None:
        return Place(address=cached.get("address"), city=cached.get("city"))

    place = await _fetch_place(latitude, longitude)
    if not place:
        # Not cached: a failed lookup is usually transient, and storing it would
        # keep an address hidden for thirty days over one bad response.
        return NOWHERE

    now = utc_now()
    await _cache().update_one(
        {"key": key},
        {
            "$set": {
                "key": key,
                "address": place.address,
                "city": place.city,
                "latitude": latitude,
                "longitude": longitude,
                "updated_at": now,
                "expires_at": now + timedelta(seconds=CACHE_TTL_SECONDS),
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return place


async def reverse_place(latitude: float, longitude: float) -> Place:
    """The address and the city for these coordinates. Never raises.

    The blanket catch is the point of this function: every caller is in the
    middle of recording a punch, and a punch is never allowed to fail because
    geocoding did.
    """
    try:
        return await _lookup(latitude, longitude)
    except Exception:  # noqa: BLE001 - geocoding must never fail a punch
        logger.warning(
            "Reverse geocoding failed for %s,%s; storing coordinates only.",
            latitude,
            longitude,
            exc_info=True,
        )
        return NOWHERE


async def reverse_geocode(latitude: float, longitude: float) -> str | None:
    """Just the street address, for callers that have no use for the city."""
    return (await reverse_place(latitude, longitude)).address
