from dataclasses import dataclass
from uuid import UUID

from nalar.application.errors import DependencyUnavailable, NotFound
from nalar.application.ports.storage import ObjectStorage
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class MaterialFilePolicy:
    material_url_expiry_s: int


class MaterialFileQuery:
    def __init__(self, uow: UnitOfWork, storage: ObjectStorage) -> None:
        self._uow = uow
        self._storage = storage

    async def execute(self, actor_id: UUID, kb_id: UUID, material_id: UUID, expires_in: int) -> str:
        async with self._uow:
            if not await self._uow.authz.can_read_kb(actor_id, kb_id):
                raise NotFound()
            file = await self._uow.knowledge.material_file(kb_id, material_id)
        if file is None:
            raise NotFound()
        try:
            return await self._storage.signed_url(*file, expires_in)
        except Exception as exc:
            raise DependencyUnavailable("STORAGE_UNAVAILABLE") from exc
