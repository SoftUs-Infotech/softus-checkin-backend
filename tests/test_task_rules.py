"""The derived task fields — days open, staleness and carry-forward.

Pure functions, no database: every carry-forward rule is decidable from a task
document and the day being looked at, and that is worth being able to prove
without a server in the loop.
"""

from datetime import date

from app.services.task import decorate


def _task(logged: str, completed: str | None = None) -> dict:
    return {"date": logged, "carried_from_date": logged, "completed_on": completed}


# --------------------------------------------------------------------------- #
# carry-forward
# --------------------------------------------------------------------------- #
def test_an_open_task_is_carried_into_every_later_day():
    task = _task("2026-08-03")

    assert decorate(task, on=date(2026, 8, 4))["is_carried_forward"] is True
    assert decorate(task, on=date(2026, 8, 20))["is_carried_forward"] is True
