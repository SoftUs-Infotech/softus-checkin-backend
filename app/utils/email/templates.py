"""Email bodies.

Plain functions returning an ``EmailMessage``. Every mail is sent as HTML with a
text alternative, because an invite that only renders in HTML is an invite some
employees never see.
"""

from app.core.config import settings
from app.utils.email.base import EmailMessage

_STYLE = (
    "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
    "line-height:1.6;color:#1f2933;max-width:560px;margin:0 auto;padding:24px"
)
_BUTTON = (
    "display:inline-block;padding:12px 22px;background:#2563eb;color:#ffffff;"
    "text-decoration:none;border-radius:6px;font-weight:600"
)


def _wrap(heading: str, body_html: str) -> str:
    return (
        f'<div style="{_STYLE}">'
        f'<h2 style="margin:0 0 16px">{heading}</h2>'
        f"{body_html}"
        f'<hr style="border:none;border-top:1px solid #e4e7eb;margin:28px 0">'
        f'<p style="font-size:12px;color:#7b8794;margin:0">'
        f"{settings.EMAIL_FROM_NAME} · This is an automated message."
        f"</p></div>"
    )


def invite_email(*, to: str, name: str, invite_url: str, expires_hours: int) -> EmailMessage:
    greeting = f"Hi {name}," if name else "Hi,"
    html = _wrap(
        f"Welcome to {settings.APP_NAME}",
        f"<p>{greeting}</p>"
        f"<p>You have been invited to join <strong>{settings.APP_NAME}</strong>. "
        f"Set your password and complete your profile to get started.</p>"
        f'<p style="margin:26px 0"><a href="{invite_url}" style="{_BUTTON}">'
        f"Accept invitation</a></p>"
        f'<p style="font-size:13px;color:#52606d">This link expires in {expires_hours} hours. '
        f"If the button does not work, paste this into your browser:<br>"
        f'<span style="word-break:break-all">{invite_url}</span></p>',
    )
    text = (
        f"{greeting}\n\n"
        f"You have been invited to join {settings.APP_NAME}.\n"
        f"Set your password and complete your profile here:\n\n{invite_url}\n\n"
        f"This link expires in {expires_hours} hours.\n"
    )
    return EmailMessage(
        to=to, subject=f"You are invited to {settings.APP_NAME}", html_body=html, text_body=text
    )


def password_reset_email(
    *, to: str, name: str, reset_url: str, expires_minutes: int
) -> EmailMessage:
    greeting = f"Hi {name}," if name else "Hi,"
    html = _wrap(
        "Reset your password",
        f"<p>{greeting}</p>"
        f"<p>We received a request to reset your {settings.APP_NAME} password.</p>"
        f'<p style="margin:26px 0"><a href="{reset_url}" style="{_BUTTON}">'
        f"Reset password</a></p>"
        f'<p style="font-size:13px;color:#52606d">This link expires in {expires_minutes} minutes. '
        f"If you did not request this, you can safely ignore this email — "
        f"your password will not change.</p>",
    )
    text = (
        f"{greeting}\n\n"
        f"Reset your {settings.APP_NAME} password here:\n\n{reset_url}\n\n"
        f"This link expires in {expires_minutes} minutes. "
        f"If you did not request it, ignore this email.\n"
    )
    return EmailMessage(
        to=to, subject=f"Reset your {settings.APP_NAME} password", html_body=html, text_body=text
    )


def welcome_email(*, to: str, name: str) -> EmailMessage:
    greeting = f"Hi {name}," if name else "Hi,"
    login_url = f"{settings.FRONTEND_URL.rstrip('/')}/login"
    html = _wrap(
        "Your account is ready",
        f"<p>{greeting}</p>"
        f"<p>Your {settings.APP_NAME} account is active. You can now sign in, "
        f"punch in for the day, log your tasks and request leave.</p>"
        f'<p style="margin:26px 0"><a href="{login_url}" style="{_BUTTON}">Sign in</a></p>'
        f'<p style="font-size:13px;color:#52606d">Note: punching in and out must be done '
        f"from a laptop or desktop computer — it is not available on phones or tablets.</p>",
    )
    text = (
        f"{greeting}\n\nYour {settings.APP_NAME} account is active. Sign in: {login_url}\n\n"
        f"Note: punch in/out must be done from a computer, not a phone or tablet.\n"
    )
    return EmailMessage(
        to=to, subject=f"Your {settings.APP_NAME} account is ready", html_body=html, text_body=text
    )


def password_changed_email(*, to: str, name: str) -> EmailMessage:
    greeting = f"Hi {name}," if name else "Hi,"
    html = _wrap(
        "Your password was changed",
        f"<p>{greeting}</p>"
        f"<p>The password on your {settings.APP_NAME} account was just changed. "
        f"If this was not you, contact your administrator immediately.</p>",
    )
    text = (
        f"{greeting}\n\nThe password on your {settings.APP_NAME} account was just changed.\n"
        f"If this was not you, contact your administrator immediately.\n"
    )
    return EmailMessage(
        to=to, subject=f"Your {settings.APP_NAME} password was changed",
        html_body=html, text_body=text,
    )
