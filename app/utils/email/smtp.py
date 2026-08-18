"""SMTP provider.

``smtplib`` is synchronous, so sending runs in a worker thread to keep the event
loop free.
"""

import smtplib
from email.message import EmailMessage as MimeMessage

import anyio

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError
from app.core.logging import get_logger
from app.utils.email.base import EmailMessage, EmailProvider

logger = get_logger("app.email")


class SmtpEmailProvider(EmailProvider):
    name = "smtp"

    def __init__(self) -> None:
        if not settings.SMTP_HOST:
            raise ValueError("SMTP_HOST must be set when EMAIL_PROVIDER=smtp.")

    async def send(self, message: EmailMessage) -> None:
        try:
            await anyio.to_thread.run_sync(self._send_blocking, message)
        except Exception as exc:  # noqa: BLE001 - surfaced as a clean 503
            logger.exception("SMTP delivery to %s failed.", message.to)
            raise ServiceUnavailableError(
                "The email could not be sent. Please try again shortly.",
                code="EMAIL_DELIVERY_FAILED",
            ) from exc

    def _send_blocking(self, message: EmailMessage) -> None:
        mime = MimeMessage()
        mime["From"] = f"{settings.EMAIL_FROM_NAME} <{settings.EMAIL_FROM}>"
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.text_body)
        mime.add_alternative(message.html_body, subtype="html")

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as server:
            if settings.SMTP_TLS:
                server.starttls()
            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(mime)

        logger.info("Email sent to %s via SMTP: %s", message.to, message.subject)
