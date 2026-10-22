"""Request-id, timing, and access logging.

Every request gets an id that is attached to ``request.state``, echoed in the
``X-Request-ID`` response header, and included in error payloads — so a user can
quote one number and the matching log line is findable.
"""

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger
from app.utils import network

logger = get_logger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        request.state.client_ip = _client_ip(request)

        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers["X-Response-Time-ms"] = f"{duration_ms:.2f}"

        logger.info(
            "%s %s %s %.2fms rid=%s ip=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request_id,
            request.state.client_ip,
        )
        return response


def _client_ip(request: Request) -> str:
    """The caller's address. One definition, shared with the office gate.

    This used to be a second, subtly different copy of the same logic. The
    address the audit trail records and the address the allowlist judges have
    to be the same one, or an administrator reading the log cannot tell why a
    punch was refused — and the copy here trusted forwarding headers from
    anybody, which is what made the gate bypassable.
    """
    return network.client_ip(request)
