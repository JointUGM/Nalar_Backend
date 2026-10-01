from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import Conflict, DependencyUnavailable, NotFound
from nalar.application.features.missions.commands.create_version import invalid, require_creator
from nalar.application.ports.ai import AiGateway, AiServiceError
from nalar.application.ports.ai_contract import ContextPackIn, WarmIn
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.context_pack import (
    VersionContent,
    build_context_pack,
    question_bank_from_pack,
    validate_version,
)


@dataclass(frozen=True)
class VersionReviewed:
    version_id: UUID
    reviewed_at: datetime


class ReviewVersionHandler:
    """TC-5: validate, then the AI's warm call is the final validator, then freeze the pack."""

    def __init__(self, uow: UnitOfWork, ai: AiGateway, clock: Clock) -> None:
        self._uow = uow
        self._ai = ai
        self._clock = clock

    async def execute(self, actor_id: UUID, mission_id: UUID, number: int) -> VersionReviewed:
        async with self._uow:
            mission = await require_creator(self._uow, actor_id, mission_id)
            version = await self._uow.missions.version(mission_id, number)
            if version is None:
                raise NotFound()
            if version.status == "locked":
                raise Conflict("VERSION_LOCKED", "Versi ini sudah dipublikasikan.")
            if version.reviewed_at is not None:
                return VersionReviewed(version.id, version.reviewed_at)
            targets, misconceptions = await self._uow.missions.version_items(version.id)
        d = version.draft
        missing = (set(d.target_concept_ids) - {t.id for t in targets}) | (
            set(d.misconception_ids) - {m.id for m in misconceptions}
        )
        content = VersionContent(
            anchor_problem=d.anchor_problem,
            reference_reasoning=d.reference_reasoning,
            rubric=d.rubric,
            targets=targets,
            misconceptions=misconceptions,
            question_bank=question_bank_from_pack({"question_bank": d.question_bank}),
            answer_terms=d.answer_terms,
            max_turns=d.max_turns,
            max_duration_minutes=d.max_duration_minutes,
        )
        problems = [("ITEM_NOT_APPROVED", str(i)) for i in sorted(missing, key=str)]
        problems += [(p.code, p.detail) for p in validate_version(content)]
        if problems:
            raise invalid(problems)
        pack = build_context_pack(content)
        try:
            reply = await self._ai.warm_run(
                WarmIn(context_pack=ContextPackIn.model_validate(pack)), f"review-{version.id}"
            )
        except AiServiceError as error:
            async with self._uow:
                await self._uow.ai_invocations.record(mission.school_id, error.invocations)
            if error.http_status == 422:
                raise invalid([("AI_PACK_REJECTED", error.message[:500])]) from error
            raise DependencyUnavailable() from error
        now = self._clock.now()
        async with self._uow:
            await self._uow.ai_invocations.record(mission.school_id, reply.invocations)
        async with self._uow:
            await require_creator(self._uow, actor_id, mission_id)
            if not await self._uow.missions.mark_reviewed(version.id, pack, actor_id, now):
                current = await self._uow.missions.version(mission_id, number)
                assert current is not None
                if current.status == "locked":
                    raise Conflict("VERSION_LOCKED")
                assert current.reviewed_at is not None
                return VersionReviewed(current.id, current.reviewed_at)
        return VersionReviewed(version.id, now)
