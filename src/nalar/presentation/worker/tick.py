from collections.abc import Mapping
from typing import Any

from dishka import AsyncContainer

from nalar.application.features.auth.commands.send_password_reset import SendPasswordResetHandler
from nalar.application.features.onboarding.commands.send_invitation import (
    SendAccountInvitationHandler,
)
from nalar.application.features.scheduler.commands.tick import TickHandler
from nalar.presentation.worker.runner import Handler


def cron_handlers(container: AsyncContainer) -> Mapping[str, Handler]:
    async def scheduler_tick(message: dict[str, Any]) -> None:
        async with container() as scope:
            await (await scope.get(TickHandler)).execute()
            await (await scope.get(SendAccountInvitationHandler)).dispatch_due()
            await (await scope.get(SendPasswordResetHandler)).dispatch_due()

    return {
        "scheduler_tick": scheduler_tick,
    }
