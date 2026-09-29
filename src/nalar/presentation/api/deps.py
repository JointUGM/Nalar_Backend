from typing import Annotated

from dishka import AsyncContainer
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from nalar.application.errors import Unauthenticated
from nalar.application.ports.auth import AuthUser, TokenVerifier

_bearer = HTTPBearer(auto_error=False)


async def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthUser:
    if credentials is None:
        raise Unauthenticated()
    container: AsyncContainer = request.state.dishka_container
    verifier: TokenVerifier = await container.get(TokenVerifier)
    user: AuthUser = await verifier.verify(credentials.credentials)
    return user


CurrentUser = Annotated[AuthUser, Depends(current_user)]
