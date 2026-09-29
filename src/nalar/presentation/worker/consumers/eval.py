from collections.abc import Mapping
from typing import Any
from uuid import UUID

from dishka import AsyncContainer

from nalar.application.features.evaluation.commands.evaluate_session import EvaluateSessionHandler
from nalar.presentation.worker.runner import Handler


def eval_handlers(container: AsyncContainer) -> Mapping[str, Handler]:
    async def evaluate_session(message: dict[str, Any]) -> None:
        async with container() as scope:
            handler = await scope.get(EvaluateSessionHandler)
            await handler.execute(UUID(str(message["session_id"])))

    return {"evaluate_session": evaluate_session}
