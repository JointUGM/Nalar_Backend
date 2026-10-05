from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.features.administration.commands.upload_reference import require_platform
from nalar.application.ports.national_references import ReferenceRow
from nalar.application.ports.storage import MATERIALS_BUCKET, ObjectStorage
from nalar.application.ports.uow import UnitOfWork


class ReferencesQuery:
    def __init__(self, uow: UnitOfWork, storage: ObjectStorage) -> None:
        self._uow = uow
        self._storage = storage

    async def list(self, actor_id: UUID, school_id: UUID | None = None) -> list[ReferenceRow]:
        async with self._uow:
            if school_id is None:
                await require_platform(self._uow, actor_id)
            elif not await self._uow.authz.is_school_teacher(actor_id, school_id):
                raise NotFound()
            return await self._uow.national_references.documents(
                published_only=school_id is not None
            )

    async def get(self, actor_id: UUID, document_id: UUID) -> ReferenceRow:
        async with self._uow:
            await require_platform(self._uow, actor_id)
            row = await self._uow.national_references.get(document_id)
            if row is None:
                raise NotFound()
            pages = await self._uow.national_references.pages(document_id)
        return row | {"pages": [{"page_number": n, "text": text} for n, text in pages.items()]}

    async def file(self, actor_id: UUID, document_id: UUID) -> bytes:
        row = await self.get(actor_id, document_id)
        return await self._storage.download(MATERIALS_BUCKET, row["storage_path"])
