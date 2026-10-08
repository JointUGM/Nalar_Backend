from uuid import UUID

from nalar.application.errors import InvalidInput
from nalar.application.features.administration.commands.authorize import authorize_school_write
from nalar.application.features.administration.commands.request_digest import request_digest
from nalar.application.ports.uow import UnitOfWork

MAX_NAME = 120


class CreateSubjectHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        actor_id: UUID,
        school_id: UUID,
        name: str,
        version_id: UUID,
        cp_subject_id: UUID | None,
        key: UUID,
    ) -> UUID:
        async with self._uow:
            await authorize_school_write(self._uow, actor_id, school_id)
            clean = " ".join(name.split())
            if not clean or len(clean) > MAX_NAME:
                raise InvalidInput("NAME_REQUIRED")
            if cp_subject_id is None:
                raise InvalidInput("CURRICULUM_SUBJECT_REQUIRED")
            request_id, result = await self._uow.administration.request(
                actor_id,
                "subject",
                school_id,
                key,
                request_digest(f"{clean}|{version_id}|{cp_subject_id}"),
            )
            if result:
                return result
            result = await self._uow.administration.create_subject(
                school_id, clean, version_id, cp_subject_id
            )
            await self._uow.administration.finish_request(request_id, result)
            await self._uow.audit.record(
                school_id,
                actor_id,
                "subject.created",
                "school_subjects",
                result,
                {
                    "name": clean,
                    "cp_version_id": str(version_id),
                    "cp_subject_id": str(cp_subject_id),
                },
            )
            return result
