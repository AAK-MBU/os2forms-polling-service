"""Entrypoint: FastAPI management API + background poll loop, in one process.

The poll loop runs in a daemon thread started by the app lifespan and stopped on shutdown
(uvicorn handles SIGTERM/SIGINT → lifespan shutdown). Run a single instance of the worker;
the API itself is stateless and can scale.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.api import auth_routes, health, jobs_routes
from app.auth import ensure_bootstrap_admin
from app.config import get_settings
from app.db import bootstrap_state_table, session_scope
from app.logging import configure_logging, get_logger
from app.poller import PollingService

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info(
        "service.starting",
        app_env=settings.app_env,
        poll_interval_seconds=settings.poll_interval_seconds,
        max_attempts=settings.max_attempts,
    )

    # Create schema/tables and seed the admin key before the loop or API touch them.
    if settings.bootstrap_state_table and settings.database_url:
        bootstrap_state_table(settings)
        with session_scope() as session:
            ensure_bootstrap_admin(session, settings)

    service = PollingService(settings)
    thread = threading.Thread(target=service.run_forever, name="poll-loop", daemon=True)
    thread.start()
    app.state.poller = service
    try:
        yield
    finally:
        service.stop()
        thread.join(timeout=10)
        log.info("service.stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="OS2forms Polling Service",
        version="0.1.0",
        summary="Standalone poller: registers jobs, polls OS2forms, delivers to destinations",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(auth_routes.router)
    app.include_router(jobs_routes.router)
    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_config=None)


if __name__ == "__main__":
    main()
