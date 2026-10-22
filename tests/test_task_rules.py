"""The derived task fields — days open, staleness and carry-forward.

Pure functions, no database: every carry-forward rule is decidable from a task
document and the day being looked at, and that is worth being able to prove
without a server in the loop.
"""

from datetime import date

from app.services.task import STALE_AFTER_DAYS, decorate, month_weeks


def _task(logged: str, completed: str | None = None) -> dict:
    return {"date": logged, "carried_from_date": logged, "completed_on": completed}


# --------------------------------------------------------------------------- #
# days open and staleness
# --------------------------------------------------------------------------- #
def test_a_task_logged_today_has_been_open_for_no_days():
    decorated = decorate(_task("2026-08-19"), on=date(2026, 8, 19))

    assert decorated["days_open"] == 0
    assert decorated["is_stale"] is False
    assert decorated["is_carried_forward"] is False


def test_an_open_task_ages_against_the_day_being_viewed():
    decorated = decorate(_task("2026-08-01"), on=date(2026, 8, 11))

    assert decorated["days_open"] == 10
    assert decorated["is_carried_forward"] is True


def test_a_task_stops_ageing_on_the_day_it_was_finished():
    task = _task("2026-08-01", completed="2026-08-04")

    assert decorate(task, on=date(2026, 8, 4))["days_open"] == 3
    assert decorate(task, on=date(2026, 8, 30))["days_open"] == 3


def test_an_open_task_older_than_the_threshold_is_stale():
    just_short = decorate(
        _task("2026-08-01"), on=date(2026, 8, 1 + STALE_AFTER_DAYS - 1)
    )
    at_threshold = decorate(_task("2026-08-01"), on=date(2026, 8, 1 + STALE_AFTER_DAYS))

    assert just_short["is_stale"] is False
    assert at_threshold["is_stale"] is True


def test_a_finished_task_is_never_stale_however_long_it_took():
    decorated = decorate(_task("2026-01-01", completed="2026-06-01"), on=date(2026, 8, 19))

    assert decorated["days_open"] == 151
    assert decorated["is_stale"] is False


def test_a_row_written_before_carry_forward_existed_falls_back_to_its_date():
    decorated = decorate({"date": "2026-08-10"}, on=date(2026, 8, 12))

    assert decorated["carried_from_date"] == "2026-08-10"
    assert decorated["days_open"] == 2


# --------------------------------------------------------------------------- #
# carry-forward
# --------------------------------------------------------------------------- #
def test_an_open_task_is_carried_into_every_later_day():
    task = _task("2026-08-03")

    assert decorate(task, on=date(2026, 8, 4))["is_carried_forward"] is True
    assert decorate(task, on=date(2026, 8, 20))["is_carried_forward"] is True


def test_the_day_a_task_was_finished_still_counts_as_carried_in():
    task = _task("2026-08-03", completed="2026-08-05")

    assert decorate(task, on=date(2026, 8, 5))["is_carried_forward"] is True
    assert decorate(task, on=date(2026, 8, 6))["is_carried_forward"] is False


def test_a_task_is_not_carried_into_days_before_it_was_logged():
    decorated = decorate(_task("2026-08-10"), on=date(2026, 8, 9))

    assert decorated["is_carried_forward"] is False


# --------------------------------------------------------------------------- #
# week boundaries
# --------------------------------------------------------------------------- #
def test_a_month_is_cut_into_weeks_clipped_at_its_edges():
    # August 2026 opens on a Saturday, so the first week is two days long.
    weeks = month_weeks(date(2026, 8, 1), date(2026, 8, 31))

    assert weeks[0] == (date(2026, 8, 1), date(2026, 8, 2))
    assert weeks[1] == (date(2026, 8, 3), date(2026, 8, 9))
    assert weeks[-1] == (date(2026, 8, 31), date(2026, 8, 31))
    assert sum((end - start).days + 1 for start, end in weeks) == 31


def test_a_month_starting_on_a_monday_has_whole_weeks():
    weeks = month_weeks(date(2026, 6, 1), date(2026, 6, 30))

    assert weeks[0] == (date(2026, 6, 1), date(2026, 6, 7))
    assert sum((end - start).days + 1 for start, end in weeks) == 30
