from contextlib import suppress

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Request, Response

from nalar.application.errors import AppError, DependencyUnavailable, TooManyRequests
from nalar.application.features.auth.commands.change_password import (
    ChangePasswordHandler,
    PasswordMutationHandler,
)
from nalar.application.features.auth.commands.confirm_password_reset import (
    ConfirmPasswordResetHandler,
)
from nalar.application.features.auth.commands.request_password_reset import (
    RequestPasswordResetHandler,
)
from nalar.application.features.onboarding.commands.activate_account import ActivateAccountHandler
from nalar.application.features.onboarding.commands.reconcile_login import (
    ReconcileOnboardingHandler,
)
from nalar.application.ports.auth import (
    AuthTokens,
    BrowserSessions,
    IdentityProvider,
    LoginAttempts,
)
from nalar.application.ports.password_resets import CredentialState
from nalar.presentation.api.deps import CurrentUser, SessionCookie
from nalar.presentation.api.schemas.auth import (
    ActivateIn,
    LoginIn,
    PasswordChangeIn,
    PasswordResetConfirmIn,
    PasswordResetIn,
    SessionOut,
)

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
    onboarding: FromDishka[ReconcileOnboardingHandler],
    credentials: FromDishka[CredentialState],
    mutation: FromDishka[PasswordMutationHandler],
) -> SessionOut:
    email = body.email.strip().lower()
    if not await attempts.allow(email):
        raise TooManyRequests()
    revision = await credentials.login_revision(email)
    tokens = await provider.sign_in(email, body.password.get_secret_value())
    try:
        if await mutation.reconcile_login(tokens):
            revision = await credentials.login_revision(email)
            tokens = await provider.sign_in(email, body.password.get_secret_value())
        await onboarding.execute(tokens.user_id)
        previous = request.cookies.get(request.app.state.session_cookie_name)
        if previous:
            await sessions.delete(previous)
        session_id = await sessions.create(tokens, expected_revision=revision)
    except (AppError, TimeoutError) as exc:
        with suppress(DependencyUnavailable, TooManyRequests):
            await provider.sign_out(tokens.access_token)
        if isinstance(exc, TimeoutError):
            raise DependencyUnavailable() from exc
        raise
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


@router.post("/password-reset", status_code=202, response_class=Response)
async def request_password_reset(
    body: PasswordResetIn, request: Request, handler: FromDishka[RequestPasswordResetHandler]
) -> None:
    await handler.execute(body.email, request.client.host if request.client else "unknown")


@router.post("/password-reset/confirm", status_code=204)
async def confirm_password_reset(
    body: PasswordResetConfirmIn,
    request: Request,
    response: Response,
    handler: FromDishka[ConfirmPasswordResetHandler],
    attempts: FromDishka[LoginAttempts],
) -> None:
    if not await attempts.allow(f"password-reset:confirm:{body.reset_id}"):
        raise TooManyRequests()
    await handler.execute(
        body.reset_id, body.token_hash.get_secret_value(), body.password.get_secret_value()
    )
    _clear_cookie(request, response)


@router.post("/password", status_code=204)
async def change_password(
    body: PasswordChangeIn,
    user: CurrentUser,
    request: Request,
    response: Response,
    handler: FromDishka[ChangePasswordHandler],
    attempts: FromDishka[LoginAttempts],
) -> None:
    if not await attempts.allow(f"password-change:{user.id}"):
        raise TooManyRequests()
    await handler.execute(
        user.id, body.current_password.get_secret_value(), body.new_password.get_secret_value()
    )
    _clear_cookie(request, response)


def _clear_cookie(request: Request, response: Response) -> None:
    response.delete_cookie(
        request.app.state.session_cookie_name,
        path="/",
        httponly=True,
        secure=request.app.state.secure_session_cookie,
        samesite="lax",
    )


@router.post("/activate", status_code=204)
async def activate(
    body: ActivateIn,
    handler: FromDishka[ActivateAccountHandler],
    attempts: FromDishka[LoginAttempts],
) -> None:
    if not await attempts.allow(f"activation:{body.activation_id}"):
        raise TooManyRequests()
    await handler.execute(
        body.activation_id, body.token_hash.get_secret_value(), body.password.get_secret_value()
    )


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
