"""A production API refuses to start with settings that would break it quietly.

Every case here is a deployment that would have come up "healthy" and then
handed out invite links to a laptop, or answered every browser with a CORS
error. Failing at start-up turns each into a one-line log message.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

SECRET = "x" * 48
GOOD = {
    "SECRET_KEY": SECRET,
    "ENVIRONMENT": "production",
    "DEBUG": False,
    "FRONTEND_URL": "https://checkin.example.com",
    "CORS_ORIGINS": "https://checkin.example.com",
}


def build(**overrides) -> Settings:
    # _env_file=None: the developer's own .env must not decide these tests.
    return Settings(_env_file=None, **{**GOOD, **overrides})


def test_a_complete_production_configuration_starts():
    settings = build()

    assert settings.is_production


@pytest.mark.parametrize(
    ("overrides", "complaint"),
    [
        ({"DEBUG": True}, "DEBUG must be false"),
        ({"FRONTEND_URL": "http://checkin.example.com"}, "FRONTEND_URL"),
        ({"FRONTEND_URL": "https://localhost:5173"}, "FRONTEND_URL"),
        ({"CORS_ORIGINS": "*"}, "cannot be '*'"),
        ({"CORS_ORIGINS": ""}, "CORS_ORIGINS must list"),
    ],
)
def test_a_production_configuration_that_would_break_is_refused(overrides, complaint):
    with pytest.raises(ValidationError, match=complaint):
        build(**overrides)


def test_an_origin_pattern_is_refused_in_production():
    # Previews belong on a staging API; a pattern on the real one is one
    # careless character away from admitting somebody else's site.
    with pytest.raises(ValidationError, match="CORS_ORIGIN_REGEX is not allowed"):
        build(CORS_ORIGIN_REGEX=r"https://softus-checkin-[a-z0-9]+-team\.vercel\.app")


def test_an_origin_pattern_still_works_outside_production():
    settings = build(
        ENVIRONMENT="development",
        CORS_ORIGIN_REGEX=r"https://softus-checkin-[a-z0-9]+-team\.vercel\.app",
    )

    assert settings.CORS_ORIGIN_REGEX


def test_development_is_not_held_to_production_rules():
    settings = build(ENVIRONMENT="development", DEBUG=True, FRONTEND_URL="http://localhost:5173")

    assert not settings.is_production


def test_an_unknown_environment_name_is_refused():
    with pytest.raises(ValidationError, match="ENVIRONMENT"):
        build(ENVIRONMENT="staging")
