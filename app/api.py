"""The one place every router is mounted.

Adding a feature means adding exactly one ``include_router`` line here.
"""

from fastapi import APIRouter

from app.routes import auth, health, holiday, leave_type, settings, shift, user

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(user.router)
api_router.include_router(shift.router)
api_router.include_router(holiday.router)
api_router.include_router(leave_type.router)
api_router.include_router(settings.router)
