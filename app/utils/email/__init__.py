"""Email entrypoint.

``send_email`` never lets a delivery failure break the operation that triggered
it — an invite is still created if the mail bounces, and the admin sees the
invite link in the response.
"""

from functools import lru_cache

from app.core.config import settings
from app.core.logging import get_logger
from app.utils.email.base import EmailMessage, EmailProvider
from app.utils.email.console import ConsoleEmailProvider

logger = get_logger("app.email")


@lru_cache
def get_email_provider() -> EmailProvider:
    if settings.EMAIL_PROVIDER == "smtp":
        from app.utils.email.smtp import SmtpEmailProvider

        return SmtpEmailProvider()
    return ConsoleEmailProvider()


async def send_email(message: EmailMessage) -> bool:
    """Best-effort send. Returns whether delivery succeeded."""
    try:
        await get_email_provider().send(message)
    except Exception:  # noqa: BLE001 - never fail the caller's operation
        logger.exception("Failed to send '%s' to %s.", message.subject, message.to)
        return False
    return True


__all__ = ["EmailMessage", "EmailProvider", "get_email_provider", "send_email"]
