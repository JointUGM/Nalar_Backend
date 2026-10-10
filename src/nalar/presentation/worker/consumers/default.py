from collections.abc import Mapping
from typing import Any
from uuid import UUID

from dishka import AsyncContainer

from nalar.application.features.auth.commands.send_password_reset import SendPasswordResetHandler
from nalar.application.features.integrity.commands.compute_live_session_flags import (
    ComputeLiveSessionFlagsHandler,
)
from nalar.application.features.integrity.commands.compute_session_flags import (
    ComputeSessionFlagsHandler,
)
from nalar.application.features.integrity.messages import (
    LIVE_SESSION_FLAGS_KIND,
    SESSION_FLAGS_KIND,
)
from nalar.application.features.missions.commands.build_revision import BuildRevisionHandler
from nalar.application.features.missions.commands.generate_mission import (
    GENERATION_KIND,
    BuildMissionHandler,
)
from nalar.application.features.missions.commands.revise_mission import REVISION_KIND
from nalar.application.features.onboarding.commands.send_invitation import (
    SendAccountInvitationHandler,
)
from nalar.application.features.onboarding.messages import ACCOUNT_INVITATION_KIND
from nalar.application.features.release.commands.finalize_publication import (
    FinalizePublicationHandler,
)
from nalar.application.features.release.messages import FINALIZE_KIND
from nalar.application.features.roster.commands.import_roster import ImportRosterHandler
from nalar.application.features.roster.messages import ROSTER_KIND
from nalar.application.features.scheduler.commands.deliver_digest import DeliverDigestHandler
from nalar.application.features.scheduler.messages import DIGEST_DELIVERY_KIND
from nalar.presentation.worker.runner import Handler


def default_handlers(container: AsyncContainer) -> Mapping[str, Handler]:
    async def send_password_reset(message: dict[str, Any]) -> None:
        async with container() as scope:
            handler = await scope.get(SendPasswordResetHandler)
            await handler.execute(UUID(str(message["reset_id"])))

    async def send_invitation(message: dict[str, Any]) -> None:
        async with container() as scope:
            handler = await scope.get(SendAccountInvitationHandler)
            await handler.execute(UUID(str(message["notification_id"])))

    async def deliver_digest(message: dict[str, Any]) -> None:
        async with container() as scope:
            handler = await scope.get(DeliverDigestHandler)
            await handler.execute(UUID(str(message["notification_id"])))

    async def revise_mission(message: dict[str, Any]) -> None:
        async with container() as scope:
            handler = await scope.get(BuildRevisionHandler)
            await handler.execute(UUID(str(message["mission_id"])), UUID(str(message["job_id"])))

    async def generate_mission(message: dict[str, Any]) -> None:
        async with container() as scope:
            handler = await scope.get(BuildMissionHandler)
            await handler.execute(UUID(str(message["mission_id"])), UUID(str(message["job_id"])))

    async def compute_session_flags(message: dict[str, Any]) -> None:
        async with container() as scope:
            handler = await scope.get(ComputeSessionFlagsHandler)
            await handler.execute(UUID(str(message["session_id"])))

    async def compute_live_session_flags(message: dict[str, Any]) -> None:
        async with container() as scope:
            handler = await scope.get(ComputeLiveSessionFlagsHandler)
            await handler.execute(UUID(str(message["session_id"])))

    async def finalize(message: dict[str, Any]) -> None:
        async with container() as scope:
            handler = await scope.get(FinalizePublicationHandler)
            await handler.execute(
                UUID(str(message["publication_id"])), UUID(str(message["job_id"]))
            )

    async def import_roster(message: dict[str, Any]) -> None:
        async with container() as scope:
            handler = await scope.get(ImportRosterHandler)
            await handler.execute(UUID(str(message["import_id"])), UUID(str(message["job_id"])))

    return {
        "send_password_reset": send_password_reset,
        ACCOUNT_INVITATION_KIND: send_invitation,
        DIGEST_DELIVERY_KIND: deliver_digest,
        GENERATION_KIND: generate_mission,
        REVISION_KIND: revise_mission,
        SESSION_FLAGS_KIND: compute_session_flags,
        LIVE_SESSION_FLAGS_KIND: compute_live_session_flags,
        FINALIZE_KIND: finalize,
        ROSTER_KIND: import_roster,
    }
