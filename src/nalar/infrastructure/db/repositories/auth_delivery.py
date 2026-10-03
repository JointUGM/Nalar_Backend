import hashlib
import json
from datetime import datetime, timedelta
from uuid import UUID

from nalar.infrastructure.db.pool import DbConnection


async def sender_lock(conn: DbConnection, sender_key: str) -> None:
    key = int.from_bytes(hashlib.sha256(sender_key.encode()).digest()[:8], signed=True)
    await conn.execute("select pg_advisory_xact_lock($1)", key)


async def sender_retry_at(
    conn: DbConnection,
    sender_key: str,
    now: datetime,
    current_id: UUID,
    expires_at: datetime,
    hourly_limit: int,
    daily_limit: int,
    spacing: timedelta,
) -> datetime | None:
    rows = await conn.fetch(
        "select id,status::text,payload from notifications"
        " where type='account_invitation' and payload->>'sender_key'=$1"
        " union all select id,case when status='submitting' then 'pending' else status end,payload"
        " from password_resets where payload->>'sender_key'=$1",
        sender_key,
    )
    events: list[datetime] = []
    retry_at = now
    for row in rows:
        payload = json.loads(row["payload"])
        if payload.get("sender_suspended"):
            return expires_at
        if hold := payload.get("sender_hold_until"):
            retry_at = max(retry_at, datetime.fromisoformat(hold))
        if row["id"] != current_id and row["status"] == "pending" and payload.get("lease_until"):
            retry_at = max(retry_at, datetime.fromisoformat(payload["lease_until"]))
        events.extend(datetime.fromisoformat(t) for t in payload.get("submission_times", []))
    events = sorted(t for t in events if t > now - timedelta(days=1))
    hourly = [t for t in events if t > now - timedelta(hours=1)]
    if len(hourly) >= hourly_limit:
        retry_at = max(retry_at, hourly[-hourly_limit] + timedelta(hours=1))
    if len(events) >= daily_limit:
        retry_at = max(retry_at, events[-daily_limit] + timedelta(days=1))
    if events:
        retry_at = max(retry_at, events[-1] + spacing)
    return retry_at if retry_at > now else None
