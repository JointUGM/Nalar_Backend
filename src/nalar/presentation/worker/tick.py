from collections.abc import Mapping
from typing import Any

from dishka import AsyncContainer

from nalar.application.features.release.commands.release_reminders import (
    ReleaseRemindersHandler,
)
from nalar.application.features.scheduler.commands.tick import TickHandler
from nalar.application.features.scheduler.commands.weekly_digest import WeeklyDigestHandler
from nalar.presentation.worker.runner import Handler


def cron_handlers(container: AsyncContainer) -> Mapping[str, Handler]:
    async def scheduler_tick(message: dict[str, Any]) -> None:
        async with container() as scope:
            await (await scope.get(TickHandler)).execute()
            await (await scope.get(WeeklyDigestHandler)).dispatch_due()

    async def release_reminders(message: dict[str, Any]) -> None:
        async with container() as scope:
            await (await scope.get(ReleaseRemindersHandler)).execute()

    async def weekly_digest(message: dict[str, Any]) -> None:
        async with container() as scope:
            await (await scope.get(WeeklyDigestHandler)).execute()

    return {
        "scheduler_tick": scheduler_tick,
        "release_reminders": release_reminders,
        "weekly_digest": weekly_digest,
    }
