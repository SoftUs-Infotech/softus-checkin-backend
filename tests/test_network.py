"""The office-network gate: which address, which allowlist, and who is exempt."""

from datetime import date

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

from app.core.config import settings as app_settings
from app.utils.network import client_ip

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


def test_a_forged_entry_ahead_of_the_real_one_changes_nothing(behind_a_proxy):
    """What an attacker actually sends: the office's address, then their own,
    appended by the proxy behind them."""
    request = make_request({"x-forwarded-for": "203.0.113.250, 198.51.100.4"})

    assert client_ip(request) == "198.51.100.4"
