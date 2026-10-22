"""Reverse geocoding and the office geofence.

No test here reaches the network: ``geocode._fetch_place`` is the single seam
where a call to a geocoding provider would happen, and every test replaces it.
"""

import httpx
import pytest

from app.core.config import settings
from app.db.mongo import Collections
from app.services import geocode

OFFICE = (23.0225, 72.5714)


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_MAPS_API_KEY", "test-key")


@pytest.fixture
def calls(monkeypatch):
    """Record every lookup that would have gone to Google, and answer for it."""
    recorded: list[tuple[float, float]] = []

    async def fake_fetch(latitude, longitude):
        recorded.append((latitude, longitude))
        return geocode.Place(address="12 Ashram Road, Ahmedabad", city="Ahmedabad")

    monkeypatch.setattr(geocode, "_fetch_place", fake_fetch)
    return recorded


# --------------------------------------------------------------------------- #
# distance
# --------------------------------------------------------------------------- #
def test_the_distance_from_a_point_to_itself_is_nothing():
    assert geocode.distance_metres(*OFFICE, *OFFICE) == pytest.approx(0, abs=0.001)


def test_one_degree_of_latitude_is_about_a_hundred_and_eleven_kilometres():
    distance = geocode.distance_metres(0.0, 0.0, 1.0, 0.0)

    assert distance == pytest.approx(111_195, rel=0.001)


def test_a_few_streets_away_is_a_few_hundred_metres():
    distance = geocode.distance_metres(*OFFICE, OFFICE[0] + 0.0027, OFFICE[1])

    assert 250 < distance < 350


# --------------------------------------------------------------------------- #
# the cache key
# --------------------------------------------------------------------------- #
def test_the_cache_key_is_rounded_to_four_decimal_places():
    assert geocode.cache_key(23.02254321, 72.57139876) == "23.0225,72.5714"


def test_two_punches_metres_apart_share_one_cache_key():
    """Four decimals is about eleven metres — one building, one cached answer."""
    assert geocode.cache_key(23.02250, 72.57140) == geocode.cache_key(23.022504, 72.571403)


# --------------------------------------------------------------------------- #
# looking an address up
# --------------------------------------------------------------------------- #
async def test_a_cache_miss_asks_the_provider_and_stores_the_answer(db, api_key, calls):
    place = await geocode.reverse_place(*OFFICE)

    assert place.address == "12 Ashram Road, Ahmedabad"
    assert place.city == "Ahmedabad"
    assert calls == [OFFICE]
    cached = await db[Collections.GEOCODE_CACHE].find_one({"key": "23.0225,72.5714"})
    assert cached["address"] == "12 Ashram Road, Ahmedabad"
    assert cached["city"] == "Ahmedabad"
    assert cached["expires_at"] > cached["updated_at"]


async def test_a_cache_hit_costs_nothing(db, api_key, calls):
    await geocode.reverse_place(*OFFICE)
    place = await geocode.reverse_place(OFFICE[0] + 0.000001, OFFICE[1])

    assert place.address == "12 Ashram Road, Ahmedabad"
    assert place.city == "Ahmedabad"
    assert len(calls) == 1


async def test_without_an_api_key_the_keyless_provider_answers(db, calls, monkeypatch):
    """An organisation with no Google key still gets a city against its punches."""
    monkeypatch.setattr(settings, "GOOGLE_MAPS_API_KEY", "")

    place = await geocode.reverse_place(*OFFICE)

    assert place.city == "Ahmedabad"
    assert calls == [OFFICE]


async def test_a_failing_api_answers_none_instead_of_raising(db, api_key, monkeypatch):
    async def explode(latitude, longitude):
        raise httpx.HTTPError("502 from the geocoding API")

    monkeypatch.setattr(geocode, "_fetch_place", explode)

    assert await geocode.reverse_geocode(*OFFICE) is None


async def test_a_timeout_answers_none_instead_of_raising(db, api_key, monkeypatch):
    async def time_out(latitude, longitude):
        raise httpx.ReadTimeout("the geocoding API took too long")

    monkeypatch.setattr(geocode, "_fetch_place", time_out)

    assert await geocode.reverse_geocode(*OFFICE) is None


async def test_an_address_google_cannot_resolve_is_not_cached(db, api_key, monkeypatch):
    """A blank answer is usually transient — caching it would hide the address for a month."""

    async def no_result(latitude, longitude):
        return geocode.NOWHERE

    monkeypatch.setattr(geocode, "_fetch_place", no_result)

    assert await geocode.reverse_geocode(*OFFICE) is None
    assert await db[Collections.GEOCODE_CACHE].count_documents({}) == 0


# --------------------------------------------------------------------------- #
# which provider answers
# --------------------------------------------------------------------------- #
@pytest.fixture
def providers(monkeypatch):
    """Replace both providers and record which one was reached."""
    reached: list[str] = []

    async def google(latitude, longitude, api_key):
        reached.append("google")
        return geocode.Place(address="Google's answer", city="Ahmedabad")

    async def osm(latitude, longitude):
        reached.append("osm")
        return geocode.Place(address="OpenStreetMap's answer", city="Surat")

    monkeypatch.setattr(geocode, "_fetch_google", google)
    monkeypatch.setattr(geocode, "_fetch_osm", osm)
    return reached


async def test_google_answers_when_there_is_a_key(providers, api_key):
    place = await geocode._fetch_place(*OFFICE)

    assert providers == ["google"]
    assert place.city == "Ahmedabad"


async def test_the_keyless_provider_answers_when_there_is_not(providers, monkeypatch):
    """The whole point of the fallback: no key still means a named place."""
    monkeypatch.setattr(settings, "GOOGLE_MAPS_API_KEY", "")

    place = await geocode._fetch_place(*OFFICE)

    assert providers == ["osm"]
    assert place.city == "Surat"


# --------------------------------------------------------------------------- #
# naming the place
# --------------------------------------------------------------------------- #
def test_the_most_specific_name_available_wins():
    """A punch in a village should say the village, not the state it sits in."""
    address = {"village": "Kamrej", "county": "Surat", "state": "Gujarat"}

    assert geocode._pick(address, geocode._OSM_PLACE_KEYS) == "Kamrej"


def test_a_wider_name_is_used_when_the_narrow_ones_are_missing():
    assert geocode._pick({"state": "Gujarat"}, geocode._OSM_PLACE_KEYS) == "Gujarat"


def test_an_empty_value_is_skipped_rather_than_shown():
    address = {"city": "", "town": "Bardoli"}

    assert geocode._pick(address, geocode._OSM_PLACE_KEYS) == "Bardoli"


def test_coordinates_in_the_middle_of_nowhere_name_nothing():
    assert geocode._pick({}, geocode._OSM_PLACE_KEYS) is None


def test_a_place_with_neither_field_is_falsy():
    """`_lookup` uses this to decide whether an answer is worth caching."""
    assert not geocode.NOWHERE
    assert geocode.Place(city="Surat")


def test_an_indian_address_is_named_by_its_city_not_its_taluka():
    """Exactly what Nominatim returns for the Surat office. ``county`` there is
    a taluka — an administrative unit nobody uses to say where they were."""
    address = {
        "suburb": "Chhaprabhatha",
        "county": "Adajan Taluka",
        "state_district": "Surat",
        "state": "Gujarat",
    }

    assert geocode._pick(address, geocode._OSM_PLACE_KEYS) == "Surat"
