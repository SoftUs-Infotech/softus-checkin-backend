"""The one place every router is mounted.

Adding a feature means adding exactly one ``include_router`` line here.
"""

from fastapi import APIRouter

from app.routes import health

api_router = APIRouter()

api_router.include_router(health.router)
