import base64
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import InvalidInput, NotFound
from nalar.application.ports.results import AttentionCounts, AttentionCursor, AttentionItem
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class TeacherAttention:
    actor_id: UUID
    school_id: UUID
    limit: int
    cursor: str | None = None


@dataclass(frozen=True)
class AttentionPage:
    items: list[AttentionItem]
    counts: AttentionCounts
    next_cursor: str | None


def _decode(cursor: str, actor_id: UUID, school_id: UUID) -> AttentionCursor:
    try:
        version, actor, school, stamp, kind, item = (
            base64.b64decode(cursor, altchars=b"-_", validate=True).decode().split("|")
        )
        created_at = datetime.fromisoformat(stamp)
        if (
            version != "1"
            or UUID(actor) != actor_id
            or UUID(school) != school_id
            or created_at.utcoffset() is None
            or kind not in ("safety", "flag", "kb_review", "release_ready")
        ):
            raise ValueError
        return created_at, kind, UUID(item)
    except (ValueError, UnicodeError):
        raise InvalidInput(details={"cursor": "invalid"}) from None


class TeacherAttentionQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, q: TeacherAttention) -> AttentionPage:
        async with self._uow:
            if not await self._uow.authz.is_school_teacher(q.actor_id, q.school_id):
                raise NotFound()
            after = _decode(q.cursor, q.actor_id, q.school_id) if q.cursor else None
            rows, counts = await self._uow.results.teacher_attention(
                q.actor_id, q.school_id, q.limit + 1, after
            )
        items = rows[: q.limit]
        cursor = None
        if len(rows) > q.limit:
            last = items[-1]
            cursor = base64.urlsafe_b64encode(
                f"1|{q.actor_id}|{q.school_id}|{last.created_at.isoformat()}|"
                f"{last.kind}|{last.item_id}".encode()
            ).decode()
        return AttentionPage(items, counts, cursor)
