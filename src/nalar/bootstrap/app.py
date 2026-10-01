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
    admin,
    auth,
    health,
    jobs,
    knowledge_base,
    me,
    missions,
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
    app.state.session_cookie_name = settings.session_cookie_name
    app.state.session_lifetime_s = settings.session_lifetime_s
    app.state.secure_session_cookie = settings.env in ("staging", "prod")
    app.add_middleware(CsrfMiddleware, origins=settings.cors_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["Content-Type", "X-Request-Id", "X-Nalar-CSRF"],
        allow_credentials=True,
        expose_headers=["X-Request-Id"],
    )
    # Added last so it is outermost: every response, including a 500, gets X-Request-Id.
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    for router in (
        admin.router,
        auth.router,
        me.router,
        teacher.router,
        runs.router,
        student.router,
        results.router,
        release.router,
        parent.router,
        missions.router,
        knowledge_base.router,
        jobs.router,
    ):
        app.include_router(router, prefix=API_PREFIX)
    setup_dishka(container, app)
    return app
