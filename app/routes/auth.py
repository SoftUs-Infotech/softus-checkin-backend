"""Authentication endpoints.

Thin by design: read the request, call a service, wrap the result. Failures are
raised by services and rendered by the global handlers.
"""

from typing import Annotated

from fastapi import APIRouter, Body, Request, status

from app.core.datetime import utc_now
from app.schemas.auth import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    InviteAccept,
    InviteCreate,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    ResetPasswordRequest,
    TwoFactorChallengeOut,
    TwoFactorCodeRequest,
    TwoFactorDisableRequest,
    TwoFactorVerifyRequest,
)
from app.schemas.user import UserOut, UserProfileUpdate
from app.services import auth as auth_service
from app.services import user as user_service
from app.utils.deps import CurrentAdmin, CurrentUser
from app.utils.response import success

router = APIRouter(prefix="/auth", tags=["auth"])


def _agent(request: Request) -> str:
    return request.headers.get("user-agent", "")


def _ip(request: Request) -> str:
    return getattr(request.state, "client_ip", "unknown")


# --------------------------------------------------------------------------- #
# invitations
# --------------------------------------------------------------------------- #
@router.post("/invite", status_code=status.HTTP_201_CREATED, summary="Invite an employee")
async def invite(payload: InviteCreate, admin: CurrentAdmin) -> dict:
    invite_record = await auth_service.invite_user(payload, invited_by=admin["id"])
    return success(invite_record, message="Invitation sent.")


@router.get("/invite/{token}", summary="Preview an invitation (public)")
async def preview_invite(token: str, request: Request) -> dict:
    preview = await auth_service.preview_invite(token, client_ip=_ip(request))
    return success(preview, message="Invitation is valid.")


@router.post("/accept-invite", summary="Accept an invitation and activate the account")
async def accept_invite(payload: InviteAccept, request: Request) -> dict:
    session = await auth_service.accept_invite(
        payload, user_agent=_agent(request), ip=_ip(request)
    )
    return success(session, message="Your account is ready.")


@router.get("/invites", summary="List invitations")
async def list_invites(_: CurrentAdmin, invite_status: str | None = None) -> dict:
    return success(await auth_service.list_invites(invite_status))


@router.delete("/invites/{invite_id}", summary="Revoke a pending invitation")
async def revoke_invite(invite_id: str, _: CurrentAdmin) -> dict:
    await auth_service.revoke_invite(invite_id)
    return success(message="Invitation revoked.")


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
@router.post("/login", summary="Sign in")
async def login(payload: LoginRequest, request: Request) -> dict:
    result = await auth_service.login(
        payload.email, payload.password, user_agent=_agent(request), ip=_ip(request)
    )
    if isinstance(result, TwoFactorChallengeOut):
        return success(result, message="Enter the code from your authenticator app.")
    return success(result, message="Signed in.")


@router.post("/refresh", summary="Exchange a refresh token for a new pair")
async def refresh(payload: RefreshRequest, request: Request) -> dict:
    session = await auth_service.refresh_session(
        payload.refresh_token, user_agent=_agent(request), ip=_ip(request)
    )
    return success(session, message="Session refreshed.")


@router.post("/logout", summary="Sign out of this or every device")
async def logout(
    current_user: CurrentUser,
    payload: Annotated[LogoutRequest, Body()] = LogoutRequest(),
) -> dict:
    await auth_service.logout(
        user_id=current_user["id"],
        refresh_token=payload.refresh_token,
        all_devices=payload.all_devices,
    )
    return success(message="Signed out.")


# --------------------------------------------------------------------------- #
# two-factor authentication — administrators only
# --------------------------------------------------------------------------- #
@router.get("/2fa", summary="Whether two-factor authentication is on")
async def two_factor_status(admin: CurrentAdmin) -> dict:
    return success(auth_service.two_factor_status(admin))


@router.post("/2fa/setup", summary="Generate a secret and recovery codes")
async def two_factor_setup(admin: CurrentAdmin) -> dict:
    setup = await auth_service.start_two_factor_setup(admin)
    return success(
        setup,
        message="Scan the code, then confirm. Save the recovery codes — "
        "they are not shown again.",
    )


@router.post("/2fa/confirm", summary="Switch two-factor authentication on")
async def two_factor_confirm(payload: TwoFactorCodeRequest, admin: CurrentAdmin) -> dict:
    status_out = await auth_service.confirm_two_factor(admin["id"], payload.code)
    return success(status_out, message="Two-factor authentication is on.")


@router.post("/2fa/disable", summary="Switch two-factor authentication off")
async def two_factor_disable(payload: TwoFactorDisableRequest, admin: CurrentAdmin) -> dict:
    status_out = await auth_service.disable_two_factor(admin["id"], payload.password)
    return success(status_out, message="Two-factor authentication is off.")


@router.post("/2fa/verify", summary="Finish a sign-in with a code (public)")
async def two_factor_verify(payload: TwoFactorVerifyRequest, request: Request) -> dict:
    session = await auth_service.verify_two_factor(
        payload.challenge_token,
        payload.code,
        user_agent=_agent(request),
        ip=_ip(request),
    )
    return success(session, message="Signed in.")


# --------------------------------------------------------------------------- #
# the signed-in user
# --------------------------------------------------------------------------- #
@router.get("/me", summary="The signed-in employee")
async def me(current_user: CurrentUser) -> dict:
    return success(UserOut.from_document(current_user))


@router.patch("/me", summary="Update your own profile")
async def update_me(payload: UserProfileUpdate, current_user: CurrentUser) -> dict:
    updated = await user_service.update_user(
        current_user["id"], payload.model_dump(exclude_unset=True)
    )
    return success(UserOut.from_document(updated), message="Profile updated.")


# --------------------------------------------------------------------------- #
# passwords
# --------------------------------------------------------------------------- #
@router.post("/change-password", summary="Change your password")
async def change_password(payload: ChangePasswordRequest, current_user: CurrentUser) -> dict:
    await auth_service.change_password(
        current_user["id"], payload.current_password, payload.new_password
    )
    return success(message="Password changed. Please sign in again.")


@router.post("/forgot-password", summary="Request a password reset link")
async def forgot_password(payload: ForgotPasswordRequest, request: Request) -> dict:
    await auth_service.forgot_password(payload.email, ip=_ip(request))
    # Deliberately identical whether or not the address exists.
    return success(
        {"requested_at": utc_now()},
        message="If that email address has an account, a reset link is on its way.",
    )


@router.post("/reset-password", summary="Set a new password using a reset link")
async def reset_password(payload: ResetPasswordRequest) -> dict:
    await auth_service.reset_password(payload.token, payload.new_password)
    return success(message="Password updated. You can now sign in.")
