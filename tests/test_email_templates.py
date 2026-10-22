"""Email bodies, and the one rule that keeps them safe.

These bodies are assembled by hand out of f-strings, and several of the values
come from people: an approver's note, an employee's own name, a leave type an
administrator named. An unescaped one is not a broken layout — a note reading
``<a href="http://evil">Reset your password</a>`` arrives inside a genuine
message from the company, signed by its own domain. That is a better phishing
page than anything an attacker could host, and the recipient has every reason
to trust it.

So every test here that passes markup in asserts two things: the tags did not
survive into the HTML, and the text the person actually wrote still did. Escaping
that also swallows the content would be a different bug.
"""

from app.utils.email import templates

PAYLOAD = '<a href="http://evil.test">Reset your password</a>'
SCRIPT = "<script>alert(1)</script>"


# --------------------------------------------------------------------------- #
# the approver's note — the field most likely to carry an attack
# --------------------------------------------------------------------------- #
def test_a_note_containing_a_link_is_not_rendered_as_one():
    message = templates.leave_decision_email(
        to="riya@example.com",
        name="Riya",
        leave_type="Casual Leave",
        from_date="2026-09-10",
        to_date="2026-09-12",
        total_days=3,
        approved=False,
        note=PAYLOAD,
    )

    assert "<a href=\"http://evil.test\">" not in message.html_body
    # …but the reader still sees what was written, as text.
    assert "&lt;a href=&quot;http://evil.test&quot;&gt;" in message.html_body
    assert "Reset your password" in message.html_body


def test_a_note_containing_a_script_tag_is_neutralised():
    message = templates.correction_decision_email(
        to="riya@example.com", name="Riya", day="2026-09-01", approved=True, note=SCRIPT
    )

    assert "<script>" not in message.html_body
    assert "&lt;script&gt;" in message.html_body


def test_the_plain_text_alternative_keeps_the_note_verbatim():
    """Nothing to subvert in plain text, and escaping there would show the
    reader `&lt;` where their approver typed `<`."""
    message = templates.correction_decision_email(
        to="riya@example.com", name="Riya", day="2026-09-01", approved=False, note=PAYLOAD
    )

    assert PAYLOAD in message.text_body


def test_a_multi_line_note_keeps_its_line_breaks():
    message = templates.correction_decision_email(
        to="riya@example.com",
        name="Riya",
        day="2026-09-01",
        approved=False,
        note="Two reasons:\nThe first one.",
    )

    assert "Two reasons:<br>The first one." in message.html_body


def test_line_breaks_are_added_after_escaping_not_before():
    """The other order lets the note's own text become the markup."""
    message = templates.correction_decision_email(
        to="riya@example.com", name="Riya", day="2026-09-01", approved=False, note="a<br>b"
    )

    assert "a&lt;br&gt;b" in message.html_body


def test_no_note_means_no_empty_note_block():
    message = templates.correction_decision_email(
        to="riya@example.com", name="Riya", day="2026-09-01", approved=True, note=None
    )

    assert "Note from your approver" not in message.html_body


# --------------------------------------------------------------------------- #
# every other value a person can set
# --------------------------------------------------------------------------- #
def test_an_employee_name_is_escaped():
    """A name is set at invite time and edited on the profile — user data."""
    message = templates.invite_email(
        to="new@example.com", name=SCRIPT, invite_url="https://example.test/x", expires_hours=72
    )

    assert "<script>" not in message.html_body


def test_a_leave_type_name_is_escaped():
    """An administrator names these, so they are typed rather than fixed."""
    message = templates.leave_decision_email(
        to="riya@example.com",
        name="Riya",
        leave_type=SCRIPT,
        from_date="2026-09-10",
        to_date="2026-09-10",
        total_days=1,
        approved=True,
        note=None,
    )

    assert "<script>" not in message.html_body


def test_a_name_in_a_password_email_is_escaped():
    for message in (
        templates.password_changed_email(to="riya@example.com", name=SCRIPT),
        templates.welcome_email(to="riya@example.com", name=SCRIPT),
        templates.password_reset_email(
            to="riya@example.com", name=SCRIPT, reset_url="https://example.test/r", expires_minutes=60
        ),
    ):
        assert "<script>" not in message.html_body


def test_a_url_is_escaped_inside_the_href_attribute():
    """Quotes are escaped too — otherwise a crafted URL breaks out of the
    attribute and adds its own."""
    message = templates.invite_email(
        to="new@example.com",
        name="Sohil",
        invite_url='https://example.test/x" onmouseover="alert(1)',
        expires_hours=72,
    )

    assert 'onmouseover="alert(1)"' not in message.html_body
    assert "&quot;" in message.html_body


# --------------------------------------------------------------------------- #
# the mails still say what they are supposed to say
# --------------------------------------------------------------------------- #
def test_the_verdict_is_in_the_subject_line():
    """A decision somebody has to open the mail to discover is read late."""
    approved = templates.leave_decision_email(
        to="r@example.com", name="Riya", leave_type="Casual Leave",
        from_date="2026-09-10", to_date="2026-09-12", total_days=3,
        approved=True, note=None,
    )
    refused = templates.leave_decision_email(
        to="r@example.com", name="Riya", leave_type="Casual Leave",
        from_date="2026-09-10", to_date="2026-09-12", total_days=3,
        approved=False, note=None,
    )

    assert "was approved" in approved.subject
    assert "was not approved" in refused.subject


def test_a_refusal_says_the_days_are_working_days_again():
    """The consequence that actually costs somebody: those dates are back to
    ordinary working days, and nobody told them to turn up."""
    message = templates.leave_decision_email(
        to="r@example.com", name="Riya", leave_type="Casual Leave",
        from_date="2026-09-10", to_date="2026-09-12", total_days=3,
        approved=False, note=None,
    )

    assert "expected to punch in" in message.text_body


def test_a_single_day_leave_reads_as_one_date_not_a_range():
    message = templates.leave_decision_email(
        to="r@example.com", name="Riya", leave_type="Casual Leave",
        from_date="2026-09-10", to_date="2026-09-10", total_days=1,
        approved=True, note=None,
    )

    assert "2026-09-10 to 2026-09-10" not in message.subject
    assert "2026-09-10" in message.subject


def test_both_bodies_are_always_present():
    """An HTML-only mail is a mail some clients show as blank."""
    message = templates.correction_decision_email(
        to="r@example.com", name="Riya", day="2026-09-01", approved=True, note="Fine."
    )

    assert message.html_body.strip()
    assert message.text_body.strip()
    assert "Fine." in message.text_body
