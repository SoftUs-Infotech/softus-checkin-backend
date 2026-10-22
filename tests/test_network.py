"""The office-network gate: which address, which allowlist, and who is exempt."""

from datetime import date

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

from app.core.config import settings as app_settings
from app.utils import network
from app.utils.network import UNKNOWN_IP, client_ip, is_office_ip, wfh_active

TODAY = date(2026, 8, 19)


def make_request(headers: dict[str, str], *, client: tuple[str, int] | None = ("10.1.1.5", 5000)):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/attendance/punch-in",
        "headers": Headers(headers).raw,
        "query_string": b"",
        "client": client,
    }
    return Request(scope)


# --------------------------------------------------------------------------- #
# finding the caller's address
# --------------------------------------------------------------------------- #
@pytest.fixture
def behind_a_proxy(monkeypatch):
    """The API is reached through a reverse proxy on 10.1.1.5."""
    monkeypatch.setattr(app_settings, "TRUSTED_PROXIES", "10.1.1.5")


def test_the_socket_address_is_used_when_there_is_no_proxy():
    assert client_ip(make_request({})) == "10.1.1.5"


def test_a_forwarded_header_from_an_untrusted_caller_is_ignored():
    """The bypass this guard exists for.

    `X-Forwarded-For` is a header, so it is whatever the sender typed. Believing
    it from anybody meant an employee could punch from anywhere by naming the
    office's address in one, and the office-network rule was decorative.
    """
    request = make_request({"x-forwarded-for": "203.0.113.7"})

    assert client_ip(request) == "10.1.1.5"


def test_a_real_ip_header_from_an_untrusted_caller_is_ignored_too():
    assert client_ip(make_request({"x-real-ip": "203.0.113.9"})) == "10.1.1.5"


def test_the_last_untrusted_hop_is_the_caller(behind_a_proxy):
    """A proxy *appends* the peer it saw, so the trustworthy end is the right.

    Everything left of what infrastructure added is the client's own text —
    which is exactly where a forged address would sit.
    """
    request = make_request({"x-forwarded-for": "203.0.113.7, 198.51.100.4"})

    assert client_ip(request) == "198.51.100.4"


def test_a_forged_entry_ahead_of_the_real_one_changes_nothing(behind_a_proxy):
    """What an attacker actually sends: the office's address, then their own,
    appended by the proxy behind them."""
    request = make_request({"x-forwarded-for": "203.0.113.250, 198.51.100.4"})

    assert client_ip(request) == "198.51.100.4"


def test_a_single_entry_from_a_trusted_proxy_is_the_caller(behind_a_proxy):
    assert client_ip(make_request({"x-forwarded-for": "203.0.113.7"})) == "203.0.113.7"


def test_trusted_hops_are_skipped_over(behind_a_proxy, monkeypatch):
    """Two proxies in the chain, both ours. Neither of them is the caller."""
    monkeypatch.setattr(app_settings, "TRUSTED_PROXIES", "10.1.1.5,10.2.0.0/16")
    request = make_request({"x-forwarded-for": "198.51.100.4, 10.2.0.7, 10.1.1.5"})

    assert client_ip(request) == "198.51.100.4"


def test_a_chain_of_nothing_but_proxies_falls_back_to_the_socket(behind_a_proxy, monkeypatch):
    """Never fall back to something the caller writes — that is the hole again."""
    monkeypatch.setattr(app_settings, "TRUSTED_PROXIES", "10.1.1.5,10.2.0.0/16")
    request = make_request({"x-forwarded-for": "10.2.0.7"})

    assert client_ip(request) == "10.1.1.5"


def test_a_real_ip_header_is_honoured_from_a_trusted_proxy(behind_a_proxy):
    assert client_ip(make_request({"x-real-ip": "203.0.113.9"})) == "203.0.113.9"


def test_a_cidr_range_of_proxies_is_trusted(monkeypatch):
    monkeypatch.setattr(app_settings, "TRUSTED_PROXIES", "10.1.0.0/16")

    assert client_ip(make_request({"x-forwarded-for": "203.0.113.7"})) == "203.0.113.7"


def test_trusting_nothing_means_the_socket_always_wins(monkeypatch):
    monkeypatch.setattr(app_settings, "TRUSTED_PROXIES", "")
    request = make_request({"x-forwarded-for": "203.0.113.7"}, client=("127.0.0.1", 5000))

    assert client_ip(request) == "127.0.0.1"


def test_loopback_is_trusted_by_default():
    """The API only ever sees loopback when something on the same host — a
    reverse proxy, a tunnel client — forwarded the request."""
    request = make_request({"x-forwarded-for": "203.0.113.7"}, client=("127.0.0.1", 5000))

    assert client_ip(request) == "203.0.113.7"


def test_an_empty_forwarded_header_falls_back_to_the_socket():
    assert client_ip(make_request({"x-forwarded-for": "  "})) == "10.1.1.5"


def test_a_request_with_no_client_at_all_is_unknown():
    assert client_ip(make_request({}, client=None)) == UNKNOWN_IP


def test_a_headerless_request_with_no_client_is_unknown_even_from_a_proxy(monkeypatch):
    monkeypatch.setattr(app_settings, "TRUSTED_PROXIES", "10.1.1.5")

    assert client_ip(make_request({}, client=None)) == UNKNOWN_IP


# --------------------------------------------------------------------------- #
# matching the allowlist
# --------------------------------------------------------------------------- #
def test_an_exact_address_matches():
    assert is_office_ip("203.0.113.7", ["203.0.113.7"]) is True


def test_an_address_outside_the_allowlist_does_not_match():
    assert is_office_ip("198.51.100.4", ["203.0.113.7"]) is False


def test_a_cidr_range_covers_every_address_in_it():
    assert is_office_ip("203.0.113.42", ["203.0.113.0/24"]) is True


def test_an_address_just_outside_a_cidr_range_does_not_match():
    assert is_office_ip("203.0.114.1", ["203.0.113.0/24"]) is False


def test_any_one_entry_is_enough():
    allowlist = ["198.51.100.0/24", "203.0.113.7", "192.0.2.0/25"]

    assert is_office_ip("203.0.113.7", allowlist) is True


def test_an_empty_allowlist_can_never_match():
    """Otherwise enforcement would pass vacuously the moment it was switched on."""
    assert is_office_ip("203.0.113.7", []) is False
    assert is_office_ip("203.0.113.7", None) is False


def test_a_malformed_entry_is_ignored_rather_than_crashing_the_punch():
    assert is_office_ip("203.0.113.7", ["not-an-address", "203.0.113.0/24"]) is True


def test_an_allowlist_of_nothing_but_rubbish_matches_nothing():
    assert is_office_ip("203.0.113.7", ["not-an-address", "999.999.999.999/8"]) is False


def test_an_unparseable_caller_address_never_matches():
    assert is_office_ip("unknown", ["0.0.0.0/0"]) is False


def test_ipv6_is_matched_on_its_own_terms():
    assert is_office_ip("2001:db8::5", ["2001:db8::/32"]) is True
    # A v4 allowlist must not accidentally cover a v6 caller.
    assert is_office_ip("2001:db8::5", ["203.0.113.0/24"]) is False


# --------------------------------------------------------------------------- #
# the work-from-home exemption
# --------------------------------------------------------------------------- #
def test_an_employee_without_the_flag_is_not_exempt():
    assert wfh_active({"wfh_enabled": False}, TODAY) is False


def test_an_open_ended_arrangement_covers_today():
    assert wfh_active({"wfh_enabled": True}, TODAY) is True


def test_a_window_covering_today_exempts_the_employee():
    user = {"wfh_enabled": True, "wfh_from": "2026-08-01", "wfh_to": "2026-08-31"}

    assert wfh_active(user, TODAY) is True


def test_an_expired_window_no_longer_exempts_the_employee():
    user = {"wfh_enabled": True, "wfh_from": "2026-07-01", "wfh_to": "2026-07-31"}

    assert wfh_active(user, TODAY) is False


def test_a_window_that_has_not_started_yet_does_not_exempt_the_employee():
    user = {"wfh_enabled": True, "wfh_from": "2026-09-01", "wfh_to": "2026-09-30"}

    assert wfh_active(user, TODAY) is False


def test_the_last_day_of_the_window_is_still_covered():
    user = {"wfh_enabled": True, "wfh_to": "2026-08-19"}

    assert wfh_active(user, TODAY) is True


@pytest.mark.parametrize("stored", [date(2026, 8, 1), "2026-08-01"])
def test_the_window_reads_either_a_stored_string_or_a_real_date(stored):
    assert wfh_active({"wfh_enabled": True, "wfh_from": stored}, TODAY) is True


# --------------------------------------------------------------------------- #
# telling an administrator what to put in the allowlist
# --------------------------------------------------------------------------- #
def test_an_ipv4_office_is_suggested_as_the_address_itself():
    """A small office has one public address; a /24 would let in the neighbours."""
    assert network.suggest_range("49.36.180.22") == "49.36.180.22"


def test_an_ipv6_office_is_suggested_as_its_sixty_four_bit_prefix():
    """The interface identifier rotates. Pinning the full address locks the
    office out within a day; the delegated prefix is the part that stays."""
    assert (
        network.suggest_range("2405:f600:37:8c0d:35e9:a3fb:2148:9c2f")
        == "2405:f600:37:8c0d::/64"
    )


def test_the_suggested_prefix_actually_matches_the_address_it_came_from():
    """The suggestion is useless if pasting it does not let this machine punch."""
    address = "2405:f600:37:8c0d:35e9:a3fb:2148:9c2f"

    assert network.is_office_ip(address, [network.suggest_range(address)]) is True


def test_a_request_with_no_usable_address_suggests_nothing():
    assert network.suggest_range(None) is None
    assert network.suggest_range(network.UNKNOWN_IP) is None
    assert network.suggest_range("not-an-address") is None


async def test_the_network_view_reports_the_address_the_allowlist_judges(db):
    from app.services import settings as settings_service

    settings_service.clear_cache()
    view = await network.network_view("49.36.180.22")

    assert view["your_ip"] == "49.36.180.22"
    assert view["suggested_range"] == "49.36.180.22"
    assert view["matches_allowlist"] is False


async def test_the_network_view_says_so_once_the_address_is_allowed(db):
    from app.db.mongo import Collections
    from app.services import settings as settings_service

    await settings_service.get_settings()
    await db[Collections.SETTINGS].update_one(
        {"key": "organisation"}, {"$set": {"office_ip_ranges": ["49.36.180.0/24"]}}
    )
    settings_service.clear_cache()

    view = await network.network_view("49.36.180.22")

    assert view["matches_allowlist"] is True
    assert view["office_ip_ranges"] == ["49.36.180.0/24"]
    settings_service.clear_cache()
