from typing import Annotated

from dishka import AsyncContainer
from fastapi import Depends, Request
from fastapi.security import APIKeyCookie

from nalar.application.errors import Unauthenticated
from nalar.application.ports.auth import AuthUser, BrowserSessions

_cookie = APIKeyCookie(name="__Host-nalar_session", auto_error=False)


async def session_cookie(request: Request, _: Annotated[str | None, Depends(_cookie)]) -> str:
    session_id = request.cookies.get(request.app.state.session_cookie_name)
    if not session_id:
        raise Unauthenticated()
    return session_id


SessionCookie = Annotated[str, Depends(session_cookie)]


async def current_user(request: Request, session_id: SessionCookie) -> AuthUser:
    container: AsyncContainer = request.state.dishka_container
    sessions = await container.get(BrowserSessions)
    tokens = await sessions.resolve(session_id)
    return AuthUser(id=tokens.user_id)


CurrentUser = Annotated[AuthUser, Depends(current_user)]
