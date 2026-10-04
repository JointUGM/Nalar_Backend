import hashlib
import json
from dataclasses import asdict, dataclass
from uuid import UUID

from nalar.application.errors import Conflict, Forbidden, NotFound
from nalar.application.ports.publications import NewRun, Published
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.labels import RunStatus


@dataclass(frozen=True)
class PublishDefaults:
    planner_mode: str


@dataclass(frozen=True)
class Publish:
    actor_id: UUID
    version_id: UUID
    class_id: UUID
    run: NewRun
    request_key: UUID | None = None


class PublishHandler:
    def __init__(self, uow: UnitOfWork, defaults: PublishDefaults) -> None:
        self._uow = uow
        self._defaults = defaults

    async def execute(self, cmd: Publish) -> Published:
        async with self._uow:
            repo = self._uow.publications
            version = await repo.version_for_publish(cmd.version_id)
            klass = await repo.class_ref(cmd.class_id)
            if version is None or klass is None or version.school_id != klass.school_id:
                raise NotFound()
            if not await self._uow.authz.is_school_teacher(cmd.actor_id, klass.school_id):
                raise NotFound()
            if not version.reviewed and version.created_by != cmd.actor_id:
                raise NotFound()
            if not await self._uow.authz.teaches_class_subject(
                cmd.actor_id, cmd.class_id, version.school_subject_id
            ):
                raise Forbidden("NOT_ASSIGNED_TO_CLASS")
            if not version.reviewed:
                raise Conflict("VERSION_NOT_REVIEWED")
            request_id = None
            if cmd.request_key:
                digest = hashlib.sha256(
                    json.dumps(
                        [str(cmd.version_id), str(cmd.class_id), asdict(cmd.run)],
                        sort_keys=True,
                        default=str,
                    ).encode()
                ).hexdigest()
                request_id, _ = await self._uow.administration.request(
                    cmd.actor_id, "publication.create", cmd.class_id, cmd.request_key, digest
                )
                receipt = await self._uow.administration.request_receipt(request_id)
                if receipt:
                    return Published(
                        UUID(str(receipt["publication_id"])),
                        UUID(str(receipt["run_id"])),
                        RunStatus(receipt["run_status"]),
                    )
            await repo.lock_pair(cmd.version_id, cmd.class_id)
            existing = await repo.find_active(cmd.version_id, cmd.class_id)
            result = existing or await repo.create(
                school_id=klass.school_id,
                version_id=cmd.version_id,
                class_id=cmd.class_id,
                publisher_id=cmd.actor_id,
                run=cmd.run,
                planner_mode=cmd.run.planner_mode or self._defaults.planner_mode,
            )
            if request_id:
                await self._uow.administration.finish_receipt(
                    request_id, result.publication_id, asdict(result)
                )
            return result
