from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.uow import UnitOfWork


async def authorize_school_write(uow: UnitOfWork, actor_id: UUID, school_id: UUID) -> None:
    if not await uow.authz.is_school_admin(actor_id, school_id):
        raise NotFound()
    if not await uow.administration.lock_school(school_id):
        raise NotFound()
    if not await uow.authz.is_school_admin(actor_id, school_id):
        raise NotFound()
