"""Email provider interface.

Adding SendGrid/SES later means writing one more subclass and one line in
``get_email_provider`` — nothing that calls ``send`` has to change.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(slots=True)
class EmailMessage:
    to: str
    subject: str
    html_body: str
    text_body: str


class EmailProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def send(self, message: EmailMessage) -> None:
        """Deliver the message, or raise if delivery is impossible."""
