import logging
import re
import time
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nalar.presentation.api.middleware.errors import error_body

log = logging.getLogger(__name__)

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            return candidate if _VALID_REQUEST_ID.match(candidate) else None
    return None


class RequestContextMiddleware:
    """Assigns X-Request-Id, adds Server-Timing and turns unhandled errors into the 500 envelope."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid4().hex
        started = time.perf_counter()
        scope.setdefault("state", {})["request_id"] = request_id
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)
                headers.append("X-Request-Id", request_id)
                # NFR-P1: lets the load test split the 300 ms ack into server and network time.
                headers.append(
                    "Server-Timing", f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
                )
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            log.exception("unhandled error", extra={"request_id": request_id})
            if response_started:
                raise
            response = JSONResponse(
                error_body("INTERNAL", "Terjadi kesalahan pada server.", {}, request_id),
                status_code=500,
            )
            await response(scope, receive, send_with_request_id)
