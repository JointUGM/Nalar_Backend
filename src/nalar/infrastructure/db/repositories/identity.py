from uuid import UUID

from nalar.application.ports.identity import Me, RoleRef
from nalar.infrastructure.db.pool import DbConnection


class PgIdentityRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def me(self, user_id: UUID) -> Me | None:
        profile = await self._conn.fetchrow(
            "select p.full_name, p.is_platform_admin,"
            " exists(select 1 from parent_student_links l join schools sc on sc.id = l.school_id"
            " and sc.is_active where l.parent_id = p.id and l.deactivated_at is null) as is_parent"
            " from profiles p where p.id = $1",
            user_id,
        )
        if profile is None:
            return None
        roles = await self._conn.fetch(
            "select m.role::text as role, m.school_id, s.name from school_memberships m"
            " join schools s on s.id = m.school_id"
            " where m.user_id = $1 and m.status = 'active' and s.is_active"
            " order by s.name, m.role",
            user_id,
        )
        return Me(
            user_id=user_id,
            full_name=profile["full_name"],
            roles=tuple(RoleRef(r["role"], r["school_id"], r["name"]) for r in roles),
            is_parent=profile["is_parent"],
            is_platform_admin=profile["is_platform_admin"],
        )
