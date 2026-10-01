from collections.abc import Mapping
from typing import Any
from uuid import UUID

from dishka import AsyncContainer

from nalar.application.features.knowledge_base.commands.detect_sections import (
    DetectSectionsHandler,
)
from nalar.application.features.knowledge_base.messages import DETECT_KIND
from nalar.presentation.worker.runner import Handler


def kb_handlers(container: AsyncContainer) -> Mapping[str, Handler]:
    async def detect(message: dict[str, Any]) -> None:
        async with container() as scope:
            handler = await scope.get(DetectSectionsHandler)
            await handler.execute(UUID(str(message["material_id"])), UUID(str(message["job_id"])))

    return {DETECT_KIND: detect}
