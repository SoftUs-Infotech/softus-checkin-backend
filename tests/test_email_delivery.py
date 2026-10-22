"""Email delivery, against a fake SMTP server — never a real one.

Test mail that reaches a real server bounces off made-up addresses and lands in
spam folders, and both count against the sending domain's reputation. Every
test here swaps ``smtplib.SMTP`` for a recorder, so what would have been sent
can be inspected and nothing leaves the machine.
"""

import asyncio
import smtplib

import pytest

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError
from app.utils import email as email_module
from app.utils.email import EmailMessage, send_email, send_email_later
from app.utils.email.smtp import SmtpEmailProvider

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


async def test_a_message_goes_out_over_tls_with_both_bodies(fake_smtp):
    await SmtpEmailProvider().send(MESSAGE)

    (server,) = fake_smtp.instances
    assert (server.host, server.port) == ("smtp.test.invalid", 587)
    assert server.started_tls
    assert server.logged_in_as == ("mailer@test.invalid", "not-a-real-password")

    (mime,) = server.sent
    assert mime["To"] == "someone@example.com"
    assert mime["Subject"] == "Your leave was approved"
    assert settings.EMAIL_FROM in mime["From"]
    # Plain text for clients that will not render HTML, and the HTML itself.
    assert [part.get_content_type() for part in mime.iter_parts()] == [
        "text/plain",
        "text/html",
    ]


async def test_no_tls_and_no_login_when_the_server_wants_neither(fake_smtp, monkeypatch):
    monkeypatch.setattr(settings, "SMTP_TLS", False)
    monkeypatch.setattr(settings, "SMTP_USER", "")

    await SmtpEmailProvider().send(MESSAGE)

    (server,) = fake_smtp.instances
    assert not server.started_tls
    assert server.logged_in_as is None
    assert len(server.sent) == 1


async def test_a_refused_message_is_a_clean_503_from_the_provider(fake_smtp):
    fake_smtp.fail_with = smtplib.SMTPRecipientsRefused({"someone@example.com": (550, b"no")})

    with pytest.raises(ServiceUnavailableError) as raised:
        await SmtpEmailProvider().send(MESSAGE)

    assert raised.value.code == "EMAIL_DELIVERY_FAILED"


async def test_send_email_never_lets_a_delivery_failure_reach_the_caller(fake_smtp, monkeypatch):
    fake_smtp.fail_with = smtplib.SMTPServerDisconnected("gone")
    monkeypatch.setattr(email_module, "get_email_provider", lambda: SmtpEmailProvider())

    assert await send_email(MESSAGE) is False


async def test_send_email_later_returns_before_the_mail_server_answers(monkeypatch):
    delivered = asyncio.Event()

    class SlowProvider:
        async def send(self, message):
            await asyncio.sleep(0.2)
            delivered.set()

    monkeypatch.setattr(email_module, "get_email_provider", lambda: SlowProvider())

    send_email_later(MESSAGE)

    assert not delivered.is_set()
    await asyncio.wait_for(delivered.wait(), timeout=2)


def test_the_suite_refuses_a_real_smtp_connection():
    # The autouse guard in conftest: without a fake installed, SMTP is a failure.
    with pytest.raises(AssertionError, match="real SMTP"):
        smtplib.SMTP("smtp.gmail.com", 587)


@pytest.mark.usefixtures("db")
async def test_an_invite_says_when_its_email_did_not_go_out(
    admin_client, api_prefix, fake_smtp, monkeypatch
):
    fake_smtp.fail_with = smtplib.SMTPServerDisconnected("gone")
    monkeypatch.setattr(email_module, "get_email_provider", lambda: SmtpEmailProvider())

    response = await admin_client.post(
        f"{api_prefix}/auth/invite", json={"email": "bounce@example.com", "first_name": "Bo"}
    )

    assert response.status_code == 201
    body = response.json()["data"]
    assert body["email_delivered"] is False
    # The administrator still gets a link to hand over by other means.
    assert "accept-invite?token=" in body["invite_url"]
