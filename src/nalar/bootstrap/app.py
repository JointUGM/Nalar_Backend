from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dishka import AsyncContainer
from dishka.integrations.fastapi import setup_dishka
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from nalar.bootstrap.container import build_container
from nalar.bootstrap.settings import Settings
from nalar.presentation.api.middleware.csrf import CsrfMiddleware
from nalar.presentation.api.middleware.errors import install_error_handlers
from nalar.presentation.api.middleware.request_context import RequestContextMiddleware
from nalar.presentation.api.routers import (
    account_invitations,
    admin,
    administration,
    auth,
    config,
    health,
    jobs,
    knowledge_base,
    me,
    missions,
    operations,
    parent,
    release,
    results,
    runs,
    student,
    teacher,
)

API_PREFIX = "/api/v1"


def create_app(
    settings: Settings | None = None, container: AsyncContainer | None = None
) -> FastAPI:
    settings = settings or Settings()
    container = container or build_container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await container.close()

    app = FastAPI(title="NALAR Backend", version="0.1.0", lifespan=lifespan)
    app.state.public_config = {
        "password_reset_enabled": settings.password_reset_enabled,
        "account_email_enabled": settings.account_email_enabled,
        "weekly_digest_enabled": settings.email_enabled,
    }
    app.state.session_cookie_name = settings.session_cookie_name
    app.state.session_lifetime_s = settings.session_lifetime_s
    app.state.secure_session_cookie = settings.env in ("staging", "prod")
    app.add_middleware(CsrfMiddleware, origins=settings.cors_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=[
            "Content-Type",
            "X-Request-Id",
            "X-Nalar-CSRF",
            "Idempotency-Key",
            "If-None-Match",
        ],
        allow_credentials=True,
        expose_headers=["X-Request-Id", "ETag", "Date"],
    )
    # Added last so it is outermost: every response, including a 500, gets X-Request-Id.
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    for router in (
        account_invitations.router,
        admin.router,
        administration.router,
        auth.router,
        config.router,
        me.router,
        teacher.router,
        runs.router,
        student.router,
        results.router,
        release.router,
        parent.router,
        operations.router,
        missions.router,
        knowledge_base.router,
        jobs.router,
    ):
        app.include_router(router, prefix=API_PREFIX)
    setup_dishka(container, app)
    return app
