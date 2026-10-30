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


@pytest.mark.parametrize(
    ("overrides", "complaint"),
    [
        ({"FRONTEND_URL": "http://checkin.example.com"}, "FRONTEND_URL"),
        ({"CORS_ORIGINS": "*"}, "cannot be '*'"),
    ],
)
def test_a_production_configuration_that_would_break_is_refused(overrides, complaint):
    with pytest.raises(ValidationError, match=complaint):
        build(**overrides)
