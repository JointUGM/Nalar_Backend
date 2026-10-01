from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from nalar.presentation.api.middleware.errors import error_body


class CsrfMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, origins: list[str]) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._origins = set(origins)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (
            request.headers.get("Origin") not in self._origins
            or request.headers.get("X-Nalar-CSRF") != "1"
        ):
            return JSONResponse(
                error_body(
                    "CSRF_REJECTED",
                    "Permintaan tidak diizinkan.",
                    {},
                    getattr(request.state, "request_id", None),
                ),
                status_code=403,
            )
        response = await call_next(request)
        if request.cookies or request.url.path.startswith("/api/v1/auth/"):
            response.headers["Cache-Control"] = "no-store"
        return response
