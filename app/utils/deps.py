"""Reusable route dependencies: the authenticated user, role gates, pagination.

``DesktopOnly`` — the dependency that blocks punch-in/out from phones — lands in
``app/utils/device.py`` in the attendance phase and is applied only to those two
routes, so the rest of the app stays usable on a phone.
"""

from typing import Annotated, Any

from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.security import decode_token
from app.models.user import UserRole, UserStatus
from app.schemas.common import PaginationParams
from app.services import user as user_service
from app.services.audit import Actor

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict[str, Any]:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Sign in to continue.", code="MISSING_TOKEN")

    payload = decode_token(credentials.credentials, expected_type="access")
    account = await user_service.get_user_by_id(payload["sub"])

    if account is None:
        raise AuthenticationError("This account no longer exists.", code="USER_NOT_FOUND")
    if account.get("status") == UserStatus.SUSPENDED:
        raise PermissionDeniedError(
            "This account has been suspended. Contact your administrator.",
            code="ACCOUNT_SUSPENDED",
        )
    if account.get("status") == UserStatus.INVITED:
        raise PermissionDeniedError(
            "Finish setting up your account using the link in your invitation email.",
            code="ACCOUNT_NOT_ACTIVATED",
        )
    return account


async def get_current_admin(
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> dict[str, Any]:
    if current_user.get("role") != UserRole.ADMIN:
        raise PermissionDeniedError("This action is restricted to administrators.")
    return current_user


async def get_current_approver(
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> dict[str, Any]:
    """Admins and reporting managers — the two roles that decide requests.

    The gate only says "you approve *something*". Which people are in scope is a
    question about the request being decided, so it is answered in the service
    that has the request in hand, not here.
    """
    if current_user.get("role") not in (UserRole.ADMIN, UserRole.MANAGER):
        raise PermissionDeniedError(
            "This action is restricted to administrators and reporting managers."
        )
    return current_user


def get_pagination(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PaginationParams:
    return PaginationParams(page=page, page_size=page_size)


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def get_client_ip(request: Request) -> str:
    return getattr(request.state, "client_ip", "unknown")


async def get_actor(
    request: Request,
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> Actor:
    """Who is performing this request, for the audit trail."""
    return Actor.from_user(current_user, get_client_ip(request))


CurrentUser = Annotated[dict[str, Any], Depends(get_current_user)]
CurrentAdmin = Annotated[dict[str, Any], Depends(get_current_admin)]
CurrentApprover = Annotated[dict[str, Any], Depends(get_current_approver)]
Pagination = Annotated[PaginationParams, Depends(get_pagination)]
ClientIP = Annotated[str, Depends(get_client_ip)]
CurrentActor = Annotated[Actor, Depends(get_actor)]
