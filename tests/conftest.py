"""Test fixtures.

Two tiers:

* Tests that need no data use ``client`` alone. The ASGI transport does not run
  lifespan events, so no database is touched.
* Tests that need data request ``db``, which connects to a **separate test
  database**, empties it, and skips the test outright when MongoDB is not
  running — so the suite stays green on a machine without Mongo.
"""

import json
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

# Set before app modules import Settings.
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-for-validation")
# One database per test process. The suite drops every collection between
# tests, so two runs sharing a name would tear each other's fixtures down —
# which is exactly what happens when several people run pytest at once.
os.environ["MONGO_DB"] = f"softus_checkin_test_{os.getpid()}"
os.environ.setdefault("ORG_TIMEZONE", "Asia/Kolkata")
# Forced, not defaulted: a shell with EMAIL_PROVIDER=smtp exported must never
# turn a test run into real mail from the organisation's domain.
os.environ["EMAIL_PROVIDER"] = "console"
os.environ.setdefault("LOG_DIR", str(BACKEND_ROOT / "logs"))

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.core import rate_limit  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db import mongo  # noqa: E402
from app.db.indexes import create_indexes  # noqa: E402
from app.main import app  # noqa: E402
from app.services import auth as auth_service  # noqa: E402
from app.services import settings as settings_service  # noqa: E402

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "AdminPass123"

_DESKTOP_PROFILE = json.dumps(
    {
        "maxTouchPoints": 0,
        "pointerCoarse": False,
        "screenWidth": 1920,
        "screenHeight": 1080,
        "platform": "Win32",
    }
)
_PHONE_PROFILE = json.dumps(
    {
        "maxTouchPoints": 5,
        "pointerCoarse": True,
        "screenWidth": 390,
        "screenHeight": 844,
        "platform": "iPhone",
    }
)

#: Headers a real desktop browser sends, used wherever a test needs to punch.
DESKTOP_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "x-device-profile": _DESKTOP_PROFILE,
}

MOBILE_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1"
    ),
    "sec-ch-ua-mobile": "?1",
    "x-device-profile": _PHONE_PROFILE,
}

#: A valid location payload — punching requires one.
OFFICE_LOCATION = {
    "latitude": 23.0225,
    "longitude": 72.5714,
    "accuracy_metres": 12.5,
    "address": "Ahmedabad, Gujarat",
}


@pytest.fixture(autouse=True)
def _no_real_mail(monkeypatch):
    """Fail loudly if anything reaches an SMTP server.

    Every outgoing message in the suite goes to the console provider; this is
    the second lock, for a test that builds an ``SmtpEmailProvider`` itself.
    Bounces and spam reports from test mail damage the sending domain's
    reputation, so a test that wants to check delivery mocks the transport.
    """
    import smtplib

    def refuse(*args, **kwargs):
        raise AssertionError("A test tried to open a real SMTP connection — mock it.")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    rate_limit.reset_all()
    yield
    rate_limit.reset_all()


@pytest.fixture
def api_prefix() -> str:
    return settings.API_PREFIX


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def db():
    """A clean test database, or a skip when MongoDB is not running."""
    try:
        await mongo.connect_to_mongo()
    except Exception:  # noqa: BLE001 - any connection failure means "no database"
        pytest.skip("MongoDB is not reachable; skipping database-backed test.")

    database = mongo.get_database()
    for name in await database.list_collection_names():
        await database.drop_collection(name)
    await create_indexes()
    # The settings singleton is cached in-process; a dropped database must not
    # leave a stale copy behind for the next test.
    settings_service.clear_cache()

    yield database

    settings_service.clear_cache()
    await mongo.close_mongo_connection()


@pytest.fixture
async def admin(db) -> dict:
    """The seeded administrator, on the books since before any month a test judges.

    Bootstrapping activates the account today, and days before somebody started
    are not theirs to be absent from — so without a joining date every past
    month in the suite would read as "before joining".
    """
    user = await auth_service.bootstrap_admin(
        email=ADMIN_EMAIL,
        password=ADMIN_PASSWORD,
        first_name="Test",
        last_name="Admin",
    )
    await db[mongo.Collections.USERS].update_one(
        {"email": ADMIN_EMAIL}, {"$set": {"joining_date": "2025-01-01"}}
    )
    return {**user, "joining_date": "2025-01-01"}


@pytest.fixture
async def admin_client(client: AsyncClient, admin, api_prefix: str) -> AsyncClient:
    """A client already carrying the administrator's access token."""
    response = await client.post(
        f"{api_prefix}/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    token = response.json()["data"]["tokens"]["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def invite_and_accept(
    admin_client: AsyncClient,
    api_prefix: str,
    *,
    email: str,
    password: str = "Employee123",
    first_name: str = "Ada",
    role: str = "EMPLOYEE",
    joining_date: str | None = "2025-01-01",
) -> dict:
    """Run the full invite → accept flow and return the resulting session payload.

    The employee joined well before any month a test judges, unless the test
    says otherwise: days before somebody's joining date are not absences, and a
    suite written about March would otherwise be about nobody's March.
    """
    invite = await admin_client.post(
        f"{api_prefix}/auth/invite",
        json={
            "email": email,
            "first_name": first_name,
            "role": role,
            **({"joining_date": joining_date} if joining_date else {}),
        },
    )
    assert invite.status_code == 201, invite.text
    token = invite.json()["data"]["invite_url"].split("token=")[1]

    accepted = await admin_client.post(
        f"{api_prefix}/auth/accept-invite",
        json={"token": token, "password": password, "first_name": first_name, "last_name": "L"},
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()["data"]


def authed_client(access_token: str, *, desktop: bool = True) -> AsyncClient:
    """A fresh client carrying a token and, by default, desktop device headers."""
    headers = {"Authorization": f"Bearer {access_token}"}
    headers.update(DESKTOP_HEADERS if desktop else MOBILE_HEADERS)
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers
    )
