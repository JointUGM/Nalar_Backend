from typing import Annotated

from dishka import AsyncContainer
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from nalar.application.errors import Unauthenticated
from nalar.application.ports.auth import AuthUser, SessionRevocations, TokenVerifier

_bearer = HTTPBearer(auto_error=False)


async def bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    if credentials is None:
        raise Unauthenticated()
    return credentials.credentials


BearerToken = Annotated[str, Depends(bearer_token)]


async def current_user(request: Request, token: BearerToken) -> AuthUser:
    container: AsyncContainer = request.state.dishka_container
    verifier: TokenVerifier = await container.get(TokenVerifier)
    user: AuthUser = await verifier.verify(token)
    revocations: SessionRevocations = await container.get(SessionRevocations)
    if user.session_id is not None and await revocations.is_revoked(user.session_id):
        raise Unauthenticated()
    return user


CurrentUser = Annotated[AuthUser, Depends(current_user)]
