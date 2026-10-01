from contextlib import suppress

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Request, Response

from nalar.application.errors import DependencyUnavailable, TooManyRequests
from nalar.application.ports.auth import (
    AuthTokens,
    BrowserSessions,
    IdentityProvider,
    LoginAttempts,
)
from nalar.presentation.api.deps import SessionCookie
from nalar.presentation.api.schemas.auth import LoginIn, SessionOut

router = APIRouter(prefix="/auth", tags=["auth"], route_class=DishkaRoute)


def _metadata(tokens: AuthTokens, response: Response) -> SessionOut:
    response.headers["Cache-Control"] = "no-store"
    return SessionOut(user_id=tokens.user_id, expires_at=tokens.expires_at)


@router.post("/login", response_model=SessionOut)
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    provider: FromDishka[IdentityProvider],
    attempts: FromDishka[LoginAttempts],
    sessions: FromDishka[BrowserSessions],
) -> SessionOut:
    email = body.email.strip().lower()
    if not await attempts.allow(email):
        raise TooManyRequests()
    tokens = await provider.sign_in(email, body.password.get_secret_value())
    previous = request.cookies.get(request.app.state.session_cookie_name)
    if previous:
        await sessions.delete(previous)
    session_id = await sessions.create(tokens)
    response.set_cookie(
        request.app.state.session_cookie_name,
        session_id,
        max_age=request.app.state.session_lifetime_s,
        httponly=True,
        secure=request.app.state.secure_session_cookie,
        samesite="lax",
        path="/",
    )
    return _metadata(tokens, response)


@router.get("/session", response_model=SessionOut)
@router.post("/refresh", response_model=SessionOut)
async def session(
    session_id: SessionCookie, response: Response, sessions: FromDishka[BrowserSessions]
) -> SessionOut:
    return _metadata(await sessions.resolve(session_id), response)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    sessions: FromDishka[BrowserSessions],
    provider: FromDishka[IdentityProvider],
) -> None:
    session_id = request.cookies.get(request.app.state.session_cookie_name)
    tokens = await sessions.delete(session_id) if session_id else None
    response.delete_cookie(
        request.app.state.session_cookie_name,
        path="/",
        httponly=True,
        secure=request.app.state.secure_session_cookie,
        samesite="lax",
    )
    if tokens:
        with suppress(DependencyUnavailable, TooManyRequests):
            await provider.sign_out(tokens.access_token)
