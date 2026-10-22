"""The one place every router is mounted.

Adding a feature means adding exactly one ``include_router`` line here.
"""

from fastapi import APIRouter

from app.routes import (
    announcement,
    attendance,
    auth,
    correction,
    health,
    holiday,
    leave,
    leave_type,
    salary,
    settings,
    shift,
    task,
    user,
    work_calendar,
)

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(user.router)
api_router.include_router(attendance.router)
api_router.include_router(correction.router)
api_router.include_router(task.router)
api_router.include_router(leave.router)
api_router.include_router(announcement.router)
api_router.include_router(shift.router)
api_router.include_router(holiday.router)
api_router.include_router(leave_type.router)
api_router.include_router(work_calendar.router)
api_router.include_router(salary.router)
api_router.include_router(settings.router)
