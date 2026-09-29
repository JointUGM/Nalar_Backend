import logging
from uuid import UUID

from nalar.application.ports.ai import AiGateway, AiServiceError
from nalar.application.ports.ai_contract import ContextPackIn, InvocationOut, WarmIn
from nalar.application.ports.uow import UnitOfWork

log = logging.getLogger(__name__)


class WarmRunHandler:
    def __init__(self, uow: UnitOfWork, ai: AiGateway) -> None:
        self._uow = uow
        self._ai = ai

    async def execute(self, run_id: UUID) -> None:
        # No authz: only a Start that already passed its authz check schedules this.
        async with self._uow:
            data = await self._uow.runs.warm_input(run_id)
        if data is None:
            return
        try:
            request = WarmIn(context_pack=ContextPackIn.model_validate(data.context_pack))
        except ValueError:
            log.exception("stored context pack is invalid", extra={"run_id": str(run_id)})
            return
        invocations: list[InvocationOut]
        try:
            invocations = (
                await self._ai.warm_run(request, request_id=f"warm-{run_id}")
            ).invocations
        except AiServiceError as error:
            log.warning("warm failed", extra={"run_id": str(run_id), "code": error.code})
            invocations = error.invocations
        async with self._uow:
            await self._uow.ai_invocations.record(data.school_id, invocations)
