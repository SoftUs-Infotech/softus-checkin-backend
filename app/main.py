"""FastAPI application assembly: lifespan, CORS, middleware, handlers, routes."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import get_logger, setup_logging
from app.core.scheduler import shutdown_scheduler, start_scheduler
from app.db.indexes import create_indexes
from app.db.mongo import close_mongo_connection, connect_to_mongo
from app.utils.middleware import RequestContextMiddleware

setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info(
        "Starting %s (environment=%s, debug=%s)",
        settings.APP_NAME,
        settings.ENVIRONMENT,
        settings.DEBUG,
    )
    await connect_to_mongo()
    await create_indexes()
    start_scheduler()
    try:
        yield
    finally:
        shutdown_scheduler()
        await close_mongo_connection()
        logger.info("%s stopped.", settings.APP_NAME)


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version="0.1.0",
        # A production API does not publish a map of itself to the internet.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=settings.CORS_ORIGIN_REGEX or None,
        # Bearer tokens in a header, never cookies — so no request needs the
        # browser's credentials, and refusing them keeps a permissive origin
        # from ever being able to ride on a session.
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Response-Time-ms"],
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.API_PREFIX)

    return app


app = create_app()
