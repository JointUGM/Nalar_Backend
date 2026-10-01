from collections.abc import Mapping
from typing import Any
from uuid import UUID

from dishka import AsyncContainer

from nalar.application.features.integrity.commands.compute_session_flags import (
    ComputeSessionFlagsHandler,
)
from nalar.application.features.integrity.messages import SESSION_FLAGS_KIND
from nalar.presentation.worker.runner import Handler


def default_handlers(container: AsyncContainer) -> Mapping[str, Handler]:
    async def compute_session_flags(message: dict[str, Any]) -> None:
        async with container() as scope:
            handler = await scope.get(ComputeSessionFlagsHandler)
            await handler.execute(UUID(str(message["session_id"])))

    return {SESSION_FLAGS_KIND: compute_session_flags}
