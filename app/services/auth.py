"""Authentication business logic.

Covers the whole account lifecycle: invite → accept → sign in → rotate → sign out,
plus password change and reset.

Two deliberate behaviours worth knowing:

* **Refresh rotation with reuse detection.** Every refresh issues a new token and
  marks the old one revoked. Presenting a token that is already revoked means it
  leaked, so the entire family is killed and the user must sign in again.
* **No account enumeration.** Sign-in and forgot-password give the same answer
  whether or not the email exists.
"""

from datetime import datetime, timedelta
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.core import rate_limit
from app.core.config import settings
from app.core.datetime import to_utc, utc_now
from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.db.mongo import Collections, get_collection
from app.models.base import serialize
from app.models.invite import Invite, InviteStatus, PasswordReset, RefreshToken
from app.models.user import User, UserRole, UserStatus
from app.schemas.auth import (
    AuthSession,
    InviteAccept,
    InviteCreate,
    InvitePreview,
    TokenPair,
)
from app.schemas.user import UserOut
from app.utils.email import send_email
from app.utils.email.templates import (
    invite_email,
    password_changed_email,
    password_reset_email,
    welcome_email,
)

logger = get_logger(__name__)

# A real hash, verified against when the email is unknown, so that a missing
# account costs the same wall-clock time as a wrong password.
_TIMING_DECOY_HASH = hash_password("timing-decoy-never-a-real-password")


def _users():
    return get_collection(Collections.USERS)


def _invites():
    return get_collection(Collections.INVITES)


def _refresh_tokens():
    return get_collection(Collections.REFRESH_TOKENS)


def _password_resets():
    return get_collection(Collections.PASSWORD_RESETS)


def _display_name(user: dict[str, Any]) -> str:
    return f"{user.get('first_name') or ''} {user.get('last_name') or ''}".strip()


# --------------------------------------------------------------------------- #
# invitations
# --------------------------------------------------------------------------- #
async def invite_user(payload: InviteCreate, *, invited_by: str) -> dict[str, Any]:
    """Create (or re-invite) an account and email a one-time setup link."""
    email = payload.email.strip().lower()
    rate_limit.hit("invite_send", invited_by, rate_limit.INVITE_SEND)

    existing = await _users().find_one({"email": email, "is_deleted": False})
    if existing and existing.get("status") != UserStatus.INVITED:
        raise ConflictError(
            "An active account already exists for this email address.",
            code="EMAIL_ALREADY_REGISTERED",
        )

    now = utc_now()
    if existing:
        # Still pending — refresh their details and issue a fresh link.
        user_id = existing["_id"]
        await _users().update_one(
            {"_id": user_id},
            {
                "$set": {
                    "first_name": payload.first_name,
                    "last_name": payload.last_name,
                    "role": payload.role.value,
                    "designation": payload.designation,
                    "department": payload.department,
                    "employee_code": payload.employee_code,
                    "shift_id": payload.shift_id,
                    "reporting_manager_id": payload.reporting_manager_id,
                    "joining_date": payload.joining_date.isoformat()
                    if payload.joining_date
                    else None,
                    "invited_by": invited_by,
                    "invited_at": now,
                    "updated_at": now,
                }
            },
        )
        await _invites().update_many(
            {"user_id": str(user_id), "status": InviteStatus.PENDING},
            {"$set": {"status": InviteStatus.REVOKED, "revoked_at": now, "updated_at": now}},
        )
    else:
        user = User(
            email=email,
            first_name=payload.first_name,
            last_name=payload.last_name,
            role=payload.role,
            status=UserStatus.INVITED,
            designation=payload.designation,
            department=payload.department,
            employee_code=payload.employee_code,
            shift_id=payload.shift_id,
            reporting_manager_id=payload.reporting_manager_id,
            joining_date=payload.joining_date,
            invited_by=invited_by,
            invited_at=now,
        )
        try:
            result = await _users().insert_one(user.to_mongo())
        except DuplicateKeyError as exc:
            raise ConflictError(
                "That email address or employee code is already in use.",
                code="DUPLICATE_USER",
            ) from exc
        user_id = result.inserted_id

    token = generate_token()
    invite = Invite(
        email=email,
        user_id=str(user_id),
        token_hash=hash_token(token),
        role=payload.role,
        expires_at=now + timedelta(hours=settings.INVITE_EXPIRE_HOURS),
        invited_by=invited_by,
    )
    invite_result = await _invites().insert_one(invite.to_mongo())

    invite_url = f"{settings.FRONTEND_URL.rstrip('/')}/accept-invite?token={token}"
    delivered = await send_email(
        invite_email(
            to=email,
            name=payload.first_name,
            invite_url=invite_url,
            expires_hours=settings.INVITE_EXPIRE_HOURS,
        )
    )

    logger.info("Invited %s (role=%s, email_delivered=%s)", email, payload.role, delivered)
    return {
        "id": str(invite_result.inserted_id),
        "user_id": str(user_id),
        "email": email,
        "role": payload.role.value,
        "status": InviteStatus.PENDING.value,
        "expires_at": invite.expires_at.isoformat(),
        "email_delivered": delivered,
        # Returned so an admin is never stuck when mail delivery is not configured.
        "invite_url": invite_url,
    }


async def _load_pending_invite(token: str) -> dict[str, Any]:
    invite = await _invites().find_one(
        {"token_hash": hash_token(token), "is_deleted": False}
    )
    if invite is None:
        raise NotFoundError("This invitation link is not valid.", code="INVITE_NOT_FOUND")
    if invite["status"] == InviteStatus.ACCEPTED:
        raise ConflictError(
            "This invitation has already been used. Try signing in instead.",
            code="INVITE_ALREADY_USED",
        )
    if invite["status"] == InviteStatus.REVOKED:
        raise ConflictError(
            "This invitation was replaced by a newer one. Use the most recent email.",
            code="INVITE_REVOKED",
        )
    if _expired(invite["expires_at"]):
        raise ConflictError(
            "This invitation has expired. Ask your administrator to send a new one.",
            code="INVITE_EXPIRED",
        )
    return invite


def _expired(value: datetime) -> bool:
    """Mongo hands back naive datetimes; ``to_utc`` treats those as UTC."""
    return to_utc(value) < utc_now()


async def preview_invite(token: str, *, client_ip: str) -> InvitePreview:
    rate_limit.hit("invite_lookup", client_ip, rate_limit.INVITE_LOOKUP)
    invite = await _load_pending_invite(token)
    user = await _users().find_one({"_id": ObjectId(invite["user_id"])})
    if user is None:
        raise NotFoundError("This invitation link is not valid.", code="INVITE_NOT_FOUND")

    return InvitePreview(
        email=invite["email"],
        first_name=user.get("first_name", ""),
        last_name=user.get("last_name", ""),
        role=invite["role"],
        expires_at=invite["expires_at"].isoformat(),
    )


async def accept_invite(payload: InviteAccept, *, user_agent: str, ip: str) -> AuthSession:
    invite = await _load_pending_invite(payload.token)
    user_id = ObjectId(invite["user_id"])
    now = utc_now()

    await _users().update_one(
        {"_id": user_id},
        {
            "$set": {
                "hashed_password": hash_password(payload.password),
                "first_name": payload.first_name,
                "last_name": payload.last_name,
                "phone": payload.phone,
                "designation": payload.designation,
                "department": payload.department,
                "status": UserStatus.ACTIVE.value,
                "profile_completed": True,
                "activated_at": now,
                "updated_at": now,
            }
        },
    )
    await _invites().update_one(
        {"_id": invite["_id"]},
        {"$set": {"status": InviteStatus.ACCEPTED.value, "accepted_at": now, "updated_at": now}},
    )

    user = serialize(await _users().find_one({"_id": user_id}))
    await send_email(welcome_email(to=user["email"], name=payload.first_name))
    logger.info("Invitation accepted by %s", user["email"])

    return await _issue_session(user, user_agent=user_agent, ip=ip)


async def revoke_invite(invite_id: str) -> None:
    if not ObjectId.is_valid(invite_id):
        raise NotFoundError("Invitation not found.", code="INVITE_NOT_FOUND")
    result = await _invites().update_one(
        {"_id": ObjectId(invite_id), "status": InviteStatus.PENDING.value},
        {"$set": {"status": InviteStatus.REVOKED.value, "revoked_at": utc_now()}},
    )
    if result.matched_count == 0:
        raise NotFoundError("No pending invitation was found.", code="INVITE_NOT_FOUND")


async def list_invites(status: str | None = None) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"is_deleted": False}
    if status:
        query["status"] = status
    cursor = _invites().find(query, {"token_hash": 0}).sort("created_at", -1).limit(200)
    return [serialize(document) for document in await cursor.to_list(length=200)]


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
async def _issue_session(
    user: dict[str, Any],
    *,
    user_agent: str,
    ip: str,
    family_id: str | None = None,
) -> AuthSession:
    access = create_access_token(user["id"], role=user["role"])
    refresh, family = create_refresh_token(user["id"], family_id=family_id)

    record = RefreshToken(
        user_id=user["id"],
        token_hash=hash_token(refresh),
        family_id=family,
        expires_at=utc_now() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        user_agent=user_agent[:400] if user_agent else None,
        ip=ip,
    )
    await _refresh_tokens().insert_one(record.to_mongo())

    return AuthSession(
        tokens=TokenPair(
            access_token=access,
            refresh_token=refresh,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        ),
        user=UserOut.from_document(user),
    )


async def login(email: str, password: str, *, user_agent: str, ip: str) -> AuthSession:
    normalised = email.strip().lower()
    rate_limit.hit("login", f"{ip}:{normalised}", rate_limit.LOGIN)

    user = serialize(await _users().find_one({"email": normalised, "is_deleted": False}))
    invalid = AuthenticationError(
        "Incorrect email address or password.", code="INVALID_CREDENTIALS"
    )

    if user is None or not user.get("hashed_password"):
        verify_password(password, _TIMING_DECOY_HASH)
        raise invalid
    if not verify_password(password, user["hashed_password"]):
        raise invalid

    if user.get("status") == UserStatus.SUSPENDED:
        raise PermissionDeniedError(
            "This account has been suspended. Contact your administrator.",
            code="ACCOUNT_SUSPENDED",
        )
    if user.get("status") == UserStatus.INVITED:
        raise PermissionDeniedError(
            "Finish setting up your account using the link in your invitation email.",
            code="ACCOUNT_NOT_ACTIVATED",
        )

    rate_limit.reset("login", f"{ip}:{normalised}")
    await _users().update_one(
        {"_id": ObjectId(user["id"])}, {"$set": {"last_login_at": utc_now()}}
    )
    logger.info("Sign-in succeeded for %s", normalised)
    return await _issue_session(user, user_agent=user_agent, ip=ip)


async def refresh_session(token: str, *, user_agent: str, ip: str) -> AuthSession:
    payload = decode_token(token, expected_type="refresh")
    token_hash = hash_token(token)
    stored = await _refresh_tokens().find_one({"token_hash": token_hash})

    if stored is None:
        raise AuthenticationError("Your session has expired. Sign in again.", code="INVALID_TOKEN")

    if stored.get("revoked_at") is not None:
        # A revoked token being presented means it leaked — kill the whole chain.
        await _refresh_tokens().update_many(
            {"family_id": stored["family_id"], "revoked_at": None},
            {"$set": {"revoked_at": utc_now()}},
        )
        logger.warning(
            "Refresh token reuse detected for user %s; family %s revoked.",
            stored["user_id"],
            stored["family_id"],
        )
        raise AuthenticationError(
            "Your session is no longer valid. Please sign in again.",
            code="TOKEN_REUSE_DETECTED",
        )

    user = serialize(
        await _users().find_one({"_id": ObjectId(payload["sub"]), "is_deleted": False})
    )
    if user is None:
        raise AuthenticationError("This account no longer exists.", code="USER_NOT_FOUND")
    if user.get("status") != UserStatus.ACTIVE:
        raise PermissionDeniedError(
            "This account is not active.", code="ACCOUNT_NOT_ACTIVE"
        )

    session = await _issue_session(
        user, user_agent=user_agent, ip=ip, family_id=stored["family_id"]
    )
    await _refresh_tokens().update_one(
        {"_id": stored["_id"]},
        {
            "$set": {
                "revoked_at": utc_now(),
                "replaced_by": hash_token(session.tokens.refresh_token),
            }
        },
    )
    return session


async def logout(*, user_id: str, refresh_token: str | None, all_devices: bool) -> None:
    now = utc_now()
    if all_devices:
        await _refresh_tokens().update_many(
            {"user_id": user_id, "revoked_at": None}, {"$set": {"revoked_at": now}}
        )
        return
    if refresh_token:
        await _refresh_tokens().update_one(
            {"token_hash": hash_token(refresh_token), "revoked_at": None},
            {"$set": {"revoked_at": now}},
        )


# --------------------------------------------------------------------------- #
# passwords
# --------------------------------------------------------------------------- #
async def change_password(user_id: str, current_password: str, new_password: str) -> None:
    user = await _users().find_one({"_id": ObjectId(user_id), "is_deleted": False})
    if user is None:
        raise NotFoundError("This user account no longer exists.", code="USER_NOT_FOUND")
    if not verify_password(current_password, user.get("hashed_password") or ""):
        raise ValidationError(
            "Your current password is incorrect.", code="INCORRECT_PASSWORD"
        )
    if verify_password(new_password, user.get("hashed_password") or ""):
        raise ValidationError(
            "Your new password must be different from your current one.",
            code="PASSWORD_UNCHANGED",
        )

    await _users().update_one(
        {"_id": user["_id"]},
        {"$set": {"hashed_password": hash_password(new_password), "updated_at": utc_now()}},
    )
    # Every other session is now stale.
    await _refresh_tokens().update_many(
        {"user_id": user_id, "revoked_at": None}, {"$set": {"revoked_at": utc_now()}}
    )
    await send_email(password_changed_email(to=user["email"], name=_display_name(user)))


async def forgot_password(email: str, *, ip: str) -> None:
    """Always succeeds from the caller's point of view — no account enumeration."""
    normalised = email.strip().lower()
    rate_limit.hit("forgot_password", ip, rate_limit.FORGOT_PASSWORD)

    user = await _users().find_one({"email": normalised, "is_deleted": False})
    if user is None or user.get("status") != UserStatus.ACTIVE:
        logger.info("Password reset requested for unknown/inactive address %s", normalised)
        return

    token = generate_token()
    reset = PasswordReset(
        user_id=str(user["_id"]),
        token_hash=hash_token(token),
        expires_at=utc_now() + timedelta(minutes=settings.PASSWORD_RESET_EXPIRE_MINUTES),
    )
    await _password_resets().insert_one(reset.to_mongo())

    reset_url = f"{settings.FRONTEND_URL.rstrip('/')}/reset-password?token={token}"
    await send_email(
        password_reset_email(
            to=user["email"],
            name=_display_name(user),
            reset_url=reset_url,
            expires_minutes=settings.PASSWORD_RESET_EXPIRE_MINUTES,
        )
    )


async def reset_password(token: str, new_password: str) -> None:
    record = await _password_resets().find_one({"token_hash": hash_token(token)})
    invalid = ConflictError(
        "This reset link is invalid or has expired. Request a new one.",
        code="RESET_TOKEN_INVALID",
    )
    if record is None or record.get("used_at") is not None or _expired(record["expires_at"]):
        raise invalid

    user = await _users().find_one({"_id": ObjectId(record["user_id"]), "is_deleted": False})
    if user is None:
        raise invalid

    now = utc_now()
    await _users().update_one(
        {"_id": user["_id"]},
        {"$set": {"hashed_password": hash_password(new_password), "updated_at": now}},
    )
    await _password_resets().update_one({"_id": record["_id"]}, {"$set": {"used_at": now}})
    await _refresh_tokens().update_many(
        {"user_id": str(user["_id"]), "revoked_at": None}, {"$set": {"revoked_at": now}}
    )
    await send_email(password_changed_email(to=user["email"], name=_display_name(user)))
    logger.info("Password reset completed for %s", user["email"])


async def bootstrap_admin(
    *, email: str, password: str, first_name: str, last_name: str
) -> dict[str, Any]:
    """Create the very first administrator. Used by the seed script only."""
    normalised = email.strip().lower()
    if await _users().find_one({"email": normalised}):
        raise ConflictError("An account with that email already exists.")

    admin = User(
        email=normalised,
        hashed_password=hash_password(password),
        first_name=first_name,
        last_name=last_name,
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
        profile_completed=True,
        activated_at=utc_now(),
    )
    result = await _users().insert_one(admin.to_mongo())
    return serialize(await _users().find_one({"_id": result.inserted_id}))
