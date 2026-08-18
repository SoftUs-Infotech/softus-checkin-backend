"""Test fixtures.

Two tiers:

* Tests that need no data use ``client`` alone. The ASGI transport does not run
  lifespan events, so no database is touched.
* Tests that need data request ``db``, which connects to a **separate test
  database**, empties it, and skips the test outright when MongoDB is not
  running â€” so the suite stays green on a machine without Mongo.
"""

import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

# Set before app modules import Settings.
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-for-validation")
os.environ["MONGO_DB"] = "softus_checkin_test"
os.environ.setdefault("ORG_TIMEZONE", "Asia/Kolkata")
os.environ.setdefault("EMAIL_PROVIDER", "console")
os.environ.setdefault("LOG_DIR", str(BACKEND_ROOT / "logs"))

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.core import rate_limit  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db import mongo  # noqa: E402
from app.db.indexes import create_indexes  # noqa: E402
from app.main import app  # noqa: E402
from app.services import auth as auth_service  # noqa: E402

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "AdminPass123"


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

    yield database

    await mongo.close_mongo_connection()


@pytest.fixture
async def admin(db) -> dict:
    """The seeded administrator."""
    return await auth_service.bootstrap_admin(
        email=ADMIN_EMAIL,
        password=ADMIN_PASSWORD,
        first_name="Test",
        last_name="Admin",
    )


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
) -> dict:
    """Run the full invite â†’ accept flow and return the resulting session payload."""
    invite = await admin_client.post(
        f"{api_prefix}/auth/invite",
        json={"email": email, "first_name": first_name, "role": role},
    )
    assert invite.status_code == 201, invite.text
    token = invite.json()["data"]["invite_url"].split("token=")[1]

    accepted = await admin_client.post(
        f"{api_prefix}/auth/accept-invite",
        json={"token": token, "password": password, "first_name": first_name, "last_name": "L"},
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()["data"]
