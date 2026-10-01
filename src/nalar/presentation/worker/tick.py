from collections.abc import Mapping
from typing import Any

from dishka import AsyncContainer

from nalar.application.features.release.commands.release_reminders import (
    ReleaseRemindersHandler,
)
from nalar.application.features.scheduler.commands.tick import TickHandler
from nalar.presentation.worker.runner import Handler


def cron_handlers(container: AsyncContainer) -> Mapping[str, Handler]:
    async def scheduler_tick(message: dict[str, Any]) -> None:
        async with container() as scope:
            await (await scope.get(TickHandler)).execute()

    async def release_reminders(message: dict[str, Any]) -> None:
        async with container() as scope:
            await (await scope.get(ReleaseRemindersHandler)).execute()

    async def not_scheduled_yet(message: dict[str, Any]) -> None:
        return None

    # TODO(S35): route weekly_digest.
    return {
        "scheduler_tick": scheduler_tick,
        "release_reminders": release_reminders,
        "weekly_digest": not_scheduled_yet,
    }
