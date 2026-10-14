"""Read-only aggregate KPIs for the teacher learning cycle (never per teacher).

uv run python scripts/kpi_teacher_cycle.py --school <uuid> --since 2026-10-15
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

from nalar.bootstrap.kpi import teacher_cycle
from nalar.bootstrap.settings import Settings
from nalar.infrastructure.db.pool import create_pool


async def main(school_id: UUID, since: datetime) -> None:
    cfg = Settings()
    pool = await create_pool(
        cfg.database_url.get_secret_value(),
        min_size=1,
        max_size=1,
        statement_cache_size=cfg.db_statement_cache_size,
        command_timeout=cfg.db_command_timeout_s,
    )
    try:
        async with pool.acquire() as conn, conn.transaction(readonly=True):
            print(json.dumps(await teacher_cycle(conn, school_id, since), indent=2))
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--school", type=UUID, required=True)
    parser.add_argument("--since", type=datetime.fromisoformat, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.school, args.since.replace(tzinfo=args.since.tzinfo or UTC)))
