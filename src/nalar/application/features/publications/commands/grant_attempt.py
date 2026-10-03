import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from nalar.application.errors import Conflict, InvalidInput, NotFound
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.labels import SessionStatus


@dataclass(frozen=True)
class GrantTiming:
    window: timedelta


@dataclass(frozen=True)
class GrantAttempt:
    actor_id: UUID
    publication_id: UUID
    student_id: UUID
    reason: str
    request_key: UUID
    opens_at: datetime | None = None
    closes_at: datetime | None = None


@dataclass(frozen=True)
class AttemptGranted:
    grant_id: UUID
    run_id: UUID


class GrantAttemptHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock, timing: GrantTiming) -> None:
        self._uow, self._clock, self._timing = uow, clock, timing

    async def execute(self, cmd: GrantAttempt) -> AttemptGranted:
        async with self._uow:
            uow = self._uow
            if not await uow.authz.teaches_publication(cmd.actor_id, cmd.publication_id):
                raise NotFound()
            publication = await uow.publications.lock_for_grant(cmd.publication_id)
            if publication is None or not await uow.authz.is_enrolled(
                cmd.student_id, publication.class_id
            ):
                raise NotFound()
            reason = cmd.reason.strip()
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "student_id": str(cmd.student_id),
                        "reason": reason,
                        "opens_at": cmd.opens_at.astimezone(UTC).isoformat()
                        if cmd.opens_at
                        else None,
                        "closes_at": cmd.closes_at.astimezone(UTC).isoformat()
                        if cmd.closes_at
                        else None,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            prior = await uow.publications.grant_by_key(
                cmd.publication_id, cmd.actor_id, cmd.request_key
            )
            if prior:
                if prior.digest != digest:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return AttemptGranted(prior.run_id, prior.run_id)
            if publication.cancelled_at or publication.released_to_parents_at:
                raise Conflict("PUBLICATION_NOT_GRANTABLE")
            latest = await uow.sessions.latest_for_student(cmd.publication_id, cmd.student_id)
            now = self._clock.now()
            if (
                latest is None
                or latest.status in (SessionStatus.in_progress, SessionStatus.paused_safety)
                or await uow.publications.has_outstanding_grant(
                    cmd.publication_id, cmd.student_id, now
                )
            ):
                raise Conflict("ATTEMPT_NOT_GRANTABLE")
            opens_at = cmd.opens_at or now
            closes_at = cmd.closes_at or now + self._timing.window
            if (
                not reason
                or (cmd.opens_at is None) != (cmd.closes_at is None)
                or closes_at <= opens_at
                or closes_at <= now
            ):
                raise InvalidInput(details={"window": "invalid"})
            run = await uow.publications.create_grant(
                cmd.publication_id,
                publication.school_id,
                cmd.student_id,
                cmd.actor_id,
                cmd.request_key,
                digest,
                reason,
                opens_at,
                closes_at,
                now,
            )
            await uow.audit.record(
                publication.school_id,
                cmd.actor_id,
                "publication.attempt_granted",
                "publications",
                cmd.publication_id,
                {"run_id": str(run), "student_id": str(cmd.student_id), "reason": reason},
            )
        return AttemptGranted(run, run)
