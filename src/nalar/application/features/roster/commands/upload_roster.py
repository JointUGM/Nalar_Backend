from dataclasses import dataclass
from uuid import UUID, uuid4

from nalar.application.errors import InvalidInput, NotFound
from nalar.application.features.roster.messages import ROSTER_KIND, roster_message
from nalar.application.ports.queue import DEFAULT_QUEUE
from nalar.application.ports.storage import ROSTER_BUCKET, ObjectStorage
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class RosterLimits:
    max_bytes: int
    stale_after_s: float
    student_login_domain: str
    invitation_queue_ttl_s: float


@dataclass(frozen=True)
class UploadRoster:
    actor_id: UUID
    school_id: UUID
    academic_year_id: UUID
    data: bytes


@dataclass(frozen=True)
class RosterQueued:
    import_id: UUID
    job_id: UUID


class UploadRosterHandler:
    def __init__(self, uow: UnitOfWork, storage: ObjectStorage, limits: RosterLimits) -> None:
        self._uow = uow
        self._storage = storage
        self._limits = limits

    async def execute(self, cmd: UploadRoster) -> RosterQueued:
        async with self._uow:
            if not await self._uow.authz.is_school_admin(cmd.actor_id, cmd.school_id):
                raise NotFound()
            if not await self._uow.roster.year_in_school(cmd.academic_year_id, cmd.school_id):
                raise InvalidInput(details={"academic_year_id": "unknown"})
        if len(cmd.data) > self._limits.max_bytes:
            raise InvalidInput("FILE_TOO_LARGE", "Berkas lebih dari 5 MB.")
        try:
            cmd.data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise InvalidInput(
                "FILE_NOT_UTF8", "Simpan berkas CSV dengan pengodean UTF-8."
            ) from exc
        import_id = uuid4()
        path = f"{cmd.school_id}/{import_id}.csv"
        await self._storage.upload(ROSTER_BUCKET, path, cmd.data, "text/csv")
        async with self._uow:
            if not await self._uow.authz.is_school_admin(cmd.actor_id, cmd.school_id):
                raise NotFound()
            await self._uow.roster.create_import(
                import_id, cmd.school_id, cmd.academic_year_id, cmd.actor_id, path
            )
            job_id = await self._uow.jobs.create(
                kind=ROSTER_KIND,
                entity_type="roster_imports",
                entity_id=import_id,
                school_id=cmd.school_id,
                requested_by=cmd.actor_id,
            )
            await self._uow.queue.send(DEFAULT_QUEUE, roster_message(import_id, job_id))
        return RosterQueued(import_id, job_id)
