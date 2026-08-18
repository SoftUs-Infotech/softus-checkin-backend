"""Test fixtures.

The ASGI transport does not run lifespan events, so these tests exercise the app
without a live MongoDB. Tests that need real data get a database fixture in a
later phase.
"""

import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

# Set before app modules import Settings.
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-for-validation")
os.environ.setdefault("MONGO_DB", "softus_checkin_test")
os.environ.setdefault("ORG_TIMEZONE", "Asia/Kolkata")
os.environ.setdefault("LOG_DIR", str(BACKEND_ROOT / "logs"))

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
def api_prefix() -> str:
    from app.core.config import settings

    return settings.API_PREFIX
