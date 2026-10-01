import base64
from datetime import datetime
from uuid import UUID

from nalar.application.errors import InvalidInput


def encode_cursor(created_at: datetime, row_id: UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{row_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        created_at, row_id = base64.urlsafe_b64decode(cursor).decode().split("|")
        return datetime.fromisoformat(created_at), UUID(row_id)
    except ValueError as exc:
        raise InvalidInput(details={"cursor": "invalid"}) from exc
