from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from nalar.application.errors import (
    AppError,
    Conflict,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    NotFound,
    Unauthenticated,
    Unprocessable,
)

_STATUS_BY_ERROR: Mapping[type[AppError], int] = {
    InvalidInput: 400,
    Unauthenticated: 401,
    Forbidden: 403,
    NotFound: 404,
    Conflict: 409,
    Unprocessable: 422,
    DependencyUnavailable: 503,
}

_CODE_BY_HTTP_STATUS: Mapping[int, str] = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}


def error_body(
    code: str, message: str, details: Mapping[str, Any], request_id: str | None
) -> dict[str, Any]:
    return {
        "error": {"code": code, "message": message, "details": dict(details)},
        "request_id": request_id,
    }


def status_for(error: AppError) -> int:
    for cls in type(error).__mro__:
        if cls in _STATUS_BY_ERROR:
            return _STATUS_BY_ERROR[cls]
    return 500


def _request_id(request: Request) -> str | None:
    request_id = getattr(request.state, "request_id", None)
    return request_id if isinstance(request_id, str) else None


async def _app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return JSONResponse(
        error_body(exc.code, exc.message, exc.details, _request_id(request)),
        status_code=status_for(exc),
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return JSONResponse(
        error_body(
            InvalidInput.code, InvalidInput.message, {"errors": errors}, _request_id(request)
        ),
        status_code=400,
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _CODE_BY_HTTP_STATUS.get(exc.status_code, "HTTP_ERROR")
    return JSONResponse(
        error_body(code, str(exc.detail), {}, _request_id(request)),
        status_code=exc.status_code,
        headers=exc.headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
