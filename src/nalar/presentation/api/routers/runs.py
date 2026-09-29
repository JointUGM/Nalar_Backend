from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter

from nalar.application.features.runs.commands.close_run import CloseRun, CloseRunHandler
from nalar.application.features.runs.commands.open_lobby import OpenLobby, OpenLobbyHandler
from nalar.application.features.runs.commands.start_run import StartRun, StartRunHandler
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.runs import ClosedOut, LobbyOut, StartedOut

router = APIRouter(prefix="/runs", tags=["runs"], route_class=DishkaRoute)


@router.post("/{run_id}/open-lobby", response_model=LobbyOut)
async def open_lobby(
    run_id: UUID, user: CurrentUser, handler: FromDishka[OpenLobbyHandler]
) -> LobbyOut:
    result = await handler.execute(OpenLobby(user.id, run_id))
    return LobbyOut(
        status="lobby", join_code=result.join_code, lobby_opened_at=result.lobby_opened_at
    )


@router.post("/{run_id}/start", response_model=StartedOut)
async def start(
    run_id: UUID, user: CurrentUser, handler: FromDishka[StartRunHandler]
) -> StartedOut:
    result = await handler.execute(StartRun(user.id, run_id))
    return StartedOut(
        status="open", started_at=result.started_at, started_count=result.started_count
    )


@router.post("/{run_id}/close", response_model=ClosedOut)
async def close(run_id: UUID, user: CurrentUser, handler: FromDishka[CloseRunHandler]) -> ClosedOut:
    result = await handler.execute(CloseRun(user.id, run_id))
    return ClosedOut(status="closed", closed_at=result.closed_at)
