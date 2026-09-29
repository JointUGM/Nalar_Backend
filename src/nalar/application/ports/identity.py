from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class RoleRef:
    role: str
    school_id: UUID
    school_name: str


@dataclass(frozen=True)
class Me:
    user_id: UUID
    full_name: str
    roles: tuple[RoleRef, ...]
    is_parent: bool
    is_platform_admin: bool


class IdentityRepo(Protocol):
    async def me(self, user_id: UUID) -> Me | None: ...
