from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import Conflict, NotFound
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.release import readiness


@dataclass(frozen=True)
class Release:
    actor_id: UUID
    publication_id: UUID
    expected_eligible_count: int


@dataclass(frozen=True)
class Released:
    released_to_parents_at: datetime
    summary_count: int


class ReleaseHandler:
    """TC-18: atomic and idempotent. Readiness and the count are re-checked under the row lock."""

    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def execute(self, cmd: Release) -> Released:
        async with self._uow:
            if not await self._uow.authz.teaches_publication(cmd.actor_id, cmd.publication_id):
                raise NotFound()
            pub = await self._uow.release.lock(cmd.publication_id)
            assert pub is not None
            counts = await self._uow.release.counts(cmd.publication_id)
            if pub.released_at is not None:
                return Released(pub.released_at, counts.eligible)
            state = readiness(counts)
            if not state.ready or counts.eligible != cmd.expected_eligible_count:
                raise Conflict(
                    "RELEASE_NOT_READY",
                    "Hasil belum bisa dibagikan ke orang tua.",
                    {
                        "blockers": [{"code": c, "count": n} for c, n in state.blockers],
                        "eligible_count": counts.eligible,
                    },
                )
            now = self._clock.now()
            await self._uow.release.release(cmd.publication_id, cmd.actor_id, now)
            await self._uow.audit.record(
                pub.school_id,
                cmd.actor_id,
                "publication.released",
                "publications",
                pub.id,
                {"eligible_count": counts.eligible},
            )
        return Released(now, counts.eligible)
