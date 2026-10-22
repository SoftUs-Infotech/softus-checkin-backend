"""Email bodies.

Plain functions returning an ``EmailMessage``. Every mail is sent as HTML with a
text alternative, because an invite that only renders in HTML is an invite some
employees never see.

**Everything interpolated into the HTML goes through :func:`_h` first.** These
bodies are assembled by hand, and several of the values are typed by people: an
approver's note, an employee's own name, a leave type an administrator named.
An unescaped one is not merely a broken layout — a note reading
``<a href="...">Reset your password</a>`` arrives inside a genuine message from
the company, signed by its domain, which is a better phishing page than anything
an attacker could host. The plain-text alternative is left alone; there is no
markup there to subvert.

The rule to keep: a parameter ending ``_html`` is markup the *caller* assembled
from already-escaped parts. Everything else is text, and this module escapes it.
"""

from html import escape

from app.core.config import settings
from app.utils.email.base import EmailMessage


def _h(value: object) -> str:
    """Escape a value for use inside HTML. Quotes too, for attribute contexts."""
    return escape(str(value), quote=True)


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
        f'<h2 style="margin:0 0 16px">{_h(heading)}</h2>'
        f"{body_html}"
        f'<hr style="border:none;border-top:1px solid #e4e7eb;margin:28px 0">'
        f'<p style="font-size:12px;color:#7b8794;margin:0">'
        f"{_h(settings.EMAIL_FROM_NAME)} · This is an automated message."
        f"</p></div>"
    )


def invite_email(*, to: str, name: str, invite_url: str, expires_hours: int) -> EmailMessage:
    greeting = f"Hi {name}," if name else "Hi,"
    safe_greeting, safe_url = _h(greeting), _h(invite_url)
    html = _wrap(
        f"Welcome to {settings.APP_NAME}",
        f"<p>{safe_greeting}</p>"
        f"<p>You have been invited to join <strong>{_h(settings.APP_NAME)}</strong>. "
        f"Set your password and complete your profile to get started.</p>"
        f'<p style="margin:26px 0"><a href="{safe_url}" style="{_BUTTON}">'
        f"Accept invitation</a></p>"
        f'<p style="font-size:13px;color:#52606d">This link expires in {expires_hours:d} hours. '
        f"If the button does not work, paste this into your browser:<br>"
        f'<span style="word-break:break-all">{safe_url}</span></p>',
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
    safe_url = _h(reset_url)
    html = _wrap(
        "Reset your password",
        f"<p>{_h(greeting)}</p>"
        f"<p>We received a request to reset your {settings.APP_NAME} password.</p>"
        f'<p style="margin:26px 0"><a href="{safe_url}" style="{_BUTTON}">'
        f"Reset password</a></p>"
        f'<p style="font-size:13px;color:#52606d">'
        f"This link expires in {expires_minutes:d} minutes. "
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
        f"<p>{_h(greeting)}</p>"
        f"<p>Your {settings.APP_NAME} account is active. You can now sign in, "
        f"punch in for the day, log your tasks and request leave.</p>"
        f'<p style="margin:26px 0"><a href="{_h(login_url)}" style="{_BUTTON}">Sign in</a></p>'
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
        f"<p>{_h(greeting)}</p>"
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


def _decision_email(
    *,
    to: str,
    name: str,
    subject: str,
    heading: str,
    approved: bool,
    what_html: str,
    what_text: str,
    detail_html: str,
    detail_text: str,
    note: str | None,
    link_path: str,
    next_step: str,
) -> EmailMessage:
    """The shape both approval decisions share.

    A decision email has one job: say what was decided, about which day, and
    what — if anything — the reader now has to do. The verdict goes in the
    subject line, because a decision somebody has to open an email to discover
    is a decision they will read late.

    The approver's note is carried through verbatim when there is one. On a
    refusal it is usually the only thing that explains why, and sending "your
    request was rejected" with no reason turns one decision into three messages
    and a conversation.
    """
    greeting = f"Hi {name}," if name else "Hi,"
    verdict = "approved" if approved else "not approved"
    colour = "#047857" if approved else "#b91c1c"
    url = f"{settings.FRONTEND_URL.rstrip('/')}{link_path}"

    # The approver typed this. Escaped, and newlines turned into breaks after
    # escaping — the other order would emit markup from the note's own text.
    note_html = (
        f'<p style="margin:18px 0;padding:12px 14px;background:#f5f7fa;'
        f'border-left:3px solid #cbd2d9"><strong>Note from your approver:</strong><br>'
        f"{_h(note).replace(chr(10), '<br>')}</p>"
        if note
        else ""
    )

    html = _wrap(
        heading,
        f"<p>{_h(greeting)}</p>"
        f"<p>Your {what_html} was "
        f'<strong style="color:{colour}">{verdict}</strong>.</p>'
        f'<p style="margin:14px 0;color:#52606d">{detail_html}</p>'
        f"{note_html}"
        f'<p style="margin:26px 0"><a href="{_h(url)}" style="{_BUTTON}">View the details</a></p>'
        f'<p style="font-size:13px;color:#52606d">{_h(next_step)}</p>',
    )
    text = (
        f"{greeting}\n\n"
        f"Your {what_text} was {verdict}.\n\n"
        f"{detail_text}\n"
        + (f"\nNote from your approver:\n{note}\n" if note else "")
        + f"\n{next_step}\n\nView the details: {url}\n"
    )
    return EmailMessage(to=to, subject=subject, html_body=html, text_body=text)


def correction_decision_email(
    *, to: str, name: str, day: str, approved: bool, note: str | None
) -> EmailMessage:
    """A regularisation request has been decided."""
    safe_day = _h(day)
    return _decision_email(
        to=to,
        name=name,
        subject=(
            f"Your correction for {day} was "
            f"{'approved' if approved else 'not approved'}"
        ),
        heading="Correction request decided",
        approved=approved,
        what_html=f"request to correct <strong>{safe_day}</strong>",
        what_text=f"request to correct {day}",
        detail_html=(
            f"The day has been recalculated with the times you asked for, so your "
            f"attendance for {safe_day} now reflects them."
            if approved
            else f"Your attendance for {safe_day} is unchanged."
        ),
        detail_text=(
            f"The day has been recalculated with the times you asked for, so your "
            f"attendance for {day} now reflects them."
            if approved
            else f"Your attendance for {day} is unchanged."
        ),
        note=note,
        link_path="/corrections",
        next_step=(
            "Nothing further is needed from you."
            if approved
            else "If this was not what you expected, speak to your administrator — they can "
            "adjust the day directly."
        ),
    )


def leave_decision_email(
    *,
    to: str,
    name: str,
    leave_type: str,
    from_date: str,
    to_date: str,
    total_days: float,
    approved: bool,
    note: str | None,
) -> EmailMessage:
    """A leave request has been decided."""
    span = from_date if from_date == to_date else f"{from_date} to {to_date}"
    safe_type, safe_span = _h(leave_type), _h(span)
    days = f"{total_days:g}"
    return _decision_email(
        to=to,
        name=name,
        subject=(
            f"Your {leave_type} for {span} was "
            f"{'approved' if approved else 'not approved'}"
        ),
        heading="Leave request decided",
        approved=approved,
        what_html=f"<strong>{safe_type}</strong> request for {safe_span}",
        what_text=f"{leave_type} request for {span}",
        detail_html=(
            f"{days} day(s) have been deducted from your balance and those dates "
            f"now show as leave on your calendar."
            if approved
            else f"{days} day(s) have been returned to your balance and those dates "
            f"remain ordinary working days — so you are expected to punch in."
        ),
        detail_text=(
            f"{days} day(s) have been deducted from your balance and those dates "
            f"now show as leave on your calendar."
            if approved
            else f"{days} day(s) have been returned to your balance and those dates "
            f"remain ordinary working days — so you are expected to punch in."
        ),
        note=note,
        link_path="/leave",
        next_step=(
            "Your leave balance has been updated."
            if approved
            else "If you cannot work those days, speak to your administrator before they pass."
        ),
    )
