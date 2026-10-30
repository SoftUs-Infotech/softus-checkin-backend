"""Email delivery, against a fake SMTP server — never a real one.

Test mail that reaches a real server bounces off made-up addresses and lands in
spam folders, and both count against the sending domain's reputation. Every
test here swaps ``smtplib.SMTP`` for a recorder, so what would have been sent
can be inspected and nothing leaves the machine.
"""

import smtplib

import pytest

from app.core.config import settings
from app.utils.email import EmailMessage

MESSAGE = EmailMessage(
    to="someone@example.com",
    subject="Your leave was approved",
    html_body="<p>Approved.</p>",
    text_body="Approved.",
)


class FakeSMTP:
    """Stands in for ``smtplib.SMTP`` and remembers every call made on it."""

    instances: list["FakeSMTP"] = []
    fail_with: Exception | None = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.started_tls = False
        self.logged_in_as: tuple[str, str] | None = None
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in_as = (user, password)

    def send_message(self, mime):
        if FakeSMTP.fail_with is not None:
            raise FakeSMTP.fail_with
        self.sent.append(mime)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_with = None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.test.invalid")
    monkeypatch.setattr(settings, "SMTP_PORT", 587)
    monkeypatch.setattr(settings, "SMTP_USER", "mailer@test.invalid")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "not-a-real-password")
    monkeypatch.setattr(settings, "SMTP_TLS", True)
    return FakeSMTP


def test_the_suite_refuses_a_real_smtp_connection():
    # The autouse guard in conftest: without a fake installed, SMTP is a failure.
    with pytest.raises(AssertionError, match="real SMTP"):
        smtplib.SMTP("smtp.gmail.com", 587)
