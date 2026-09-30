from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Response

from nalar.application.errors import TooManyRequests, Unauthenticated
from nalar.application.ports.auth import (
    AuthTokens,
    IdentityProvider,
    LoginAttempts,
    SessionRevocations,
    TokenVerifier,
)
from nalar.presentation.api.deps import BearerToken
from nalar.presentation.api.schemas.auth import LoginIn, RefreshIn, SessionOut

router = APIRouter(prefix="/auth", tags=["auth"], route_class=DishkaRoute)


def _session(tokens: AuthTokens, response: Response) -> SessionOut:
    # RFC 6749 §5.1: token responses must never be cached.
    response.headers["Cache-Control"] = "no-store"
    return SessionOut(
        user_id=tokens.user_id,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=tokens.expires_at,
    )


@router.post("/login", response_model=SessionOut)
async def login(
    body: LoginIn,
    response: Response,
    provider: FromDishka[IdentityProvider],
    attempts: FromDishka[LoginAttempts],
) -> SessionOut:
    email = body.email.strip().lower()
    if not await attempts.allow(email):
        raise TooManyRequests()
    return _session(await provider.sign_in(email, body.password.get_secret_value()), response)


@router.post("/refresh", response_model=SessionOut)
async def refresh(
    body: RefreshIn, response: Response, provider: FromDishka[IdentityProvider]
) -> SessionOut:
    return _session(await provider.refresh(body.refresh_token), response)


@router.post("/logout", status_code=204)
async def logout(
    token: BearerToken,
    verifier: FromDishka[TokenVerifier],
    revocations: FromDishka[SessionRevocations],
    provider: FromDishka[IdentityProvider],
) -> None:
    try:
        user = await verifier.verify(token)
    except Unauthenticated:
        return
    if user.session_id is not None:
        await revocations.revoke(user.session_id)
    await provider.sign_out(token)
