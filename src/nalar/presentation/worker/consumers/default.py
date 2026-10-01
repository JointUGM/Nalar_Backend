from collections.abc import Mapping
from typing import Any
from uuid import UUID

from dishka import AsyncContainer

from nalar.application.features.integrity.commands.compute_session_flags import (
    ComputeSessionFlagsHandler,
)
from nalar.application.features.integrity.messages import SESSION_FLAGS_KIND
from nalar.application.features.release.commands.finalize_publication import (
    FinalizePublicationHandler,
)
from nalar.application.features.release.messages import FINALIZE_KIND
from nalar.presentation.worker.runner import Handler


def default_handlers(container: AsyncContainer) -> Mapping[str, Handler]:
    async def compute_session_flags(message: dict[str, Any]) -> None:
        async with container() as scope:
            handler = await scope.get(ComputeSessionFlagsHandler)
            await handler.execute(UUID(str(message["session_id"])))

    async def finalize(message: dict[str, Any]) -> None:
        async with container() as scope:
            handler = await scope.get(FinalizePublicationHandler)
            await handler.execute(
                UUID(str(message["publication_id"])), UUID(str(message["job_id"]))
            )

    return {SESSION_FLAGS_KIND: compute_session_flags, FINALIZE_KIND: finalize}
