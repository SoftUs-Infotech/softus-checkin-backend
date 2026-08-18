"""Development provider: logs the email instead of sending it.

Invite and reset links are printed in full so a developer can click through
without any mail infrastructure.
"""

from app.core.logging import get_logger
from app.utils.email.base import EmailMessage, EmailProvider

logger = get_logger("app.email")


class ConsoleEmailProvider(EmailProvider):
    name = "console"

    async def send(self, message: EmailMessage) -> None:
        logger.info(
            "\n%s\nEMAIL (not sent — console provider)\nTo:      %s\nSubject: %s\n%s\n%s\n%s",
            "=" * 78,
            message.to,
            message.subject,
            "-" * 78,
            message.text_body.strip(),
            "=" * 78,
        )
