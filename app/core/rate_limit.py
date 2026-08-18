"""In-process sliding-window rate limiting.

Scope: this counts per worker process. It is enough to stop credential stuffing
and invite-link farming from a single client, which is what v1 needs. If the API
is ever scaled to multiple workers behind a load balancer, swap the backing store
for Redis — the ``hit`` signature stays the same.
"""

import time
from collections import defaultdict, deque
from dataclasses import dataclass

from app.core.exceptions import RateLimitError


@dataclass(frozen=True, slots=True)
class RateLimit:
    max_attempts: int
    window_seconds: int
    message: str = "Too many attempts. Please try again later."


LOGIN = RateLimit(10, 300, "Too many sign-in attempts. Try again in a few minutes.")
FORGOT_PASSWORD = RateLimit(5, 900, "Too many password reset requests. Try again later.")
INVITE_LOOKUP = RateLimit(20, 600, "Too many attempts. Try again later.")
INVITE_SEND = RateLimit(60, 3600, "Invitation limit reached for now. Try again later.")

_hits: dict[str, deque[float]] = defaultdict(deque)


def hit(scope: str, identifier: str, limit: RateLimit) -> None:
    """Record an attempt and raise once the window is saturated."""
    key = f"{scope}:{identifier}"
    now = time.monotonic()
    window_start = now - limit.window_seconds

    attempts = _hits[key]
    while attempts and attempts[0] < window_start:
        attempts.popleft()

    if len(attempts) >= limit.max_attempts:
        retry_after = int(attempts[0] + limit.window_seconds - now) + 1
        raise RateLimitError(limit.message, details={"retry_after_seconds": retry_after})

    attempts.append(now)


def reset(scope: str, identifier: str) -> None:
    """Clear a counter — called after a successful sign-in."""
    _hits.pop(f"{scope}:{identifier}", None)


def reset_all() -> None:
    """Test helper."""
    _hits.clear()
