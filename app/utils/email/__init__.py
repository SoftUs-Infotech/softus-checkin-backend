"""Email entrypoint.

``send_email`` never lets a delivery failure break the operation that triggered
it — an invite is still created if the mail bounces, and the admin sees the
invite link in the response.
"""

import asyncio
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


# Strong references: the event loop only keeps weak ones to tasks, and a
# notification email garbage-collected mid-send is simply never delivered.
_in_flight: set[asyncio.Task[bool]] = set()


def send_email_later(message: EmailMessage) -> None:
    """Send without making the caller wait for the mail server.

    An SMTP round trip is seconds, not milliseconds — awaiting it inside an
    approval held every decision for about five seconds, and a bulk decision
    for five seconds per record. It also gave ``forgot_password`` away: a real
    account answered slowly and an unknown address answered at once. Use
    ``send_email`` only where the caller reports whether delivery worked.
    """
    task = asyncio.get_running_loop().create_task(send_email(message))
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)


__all__ = ["EmailMessage", "EmailProvider", "get_email_provider", "send_email", "send_email_later"]
