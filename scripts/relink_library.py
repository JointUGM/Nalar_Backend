"""Link existing misconceptions to published library entries after a human review.

uv run python scripts/relink_library.py --kb <uuid> > review.csv        # dry run: writes nothing
uv run python scripts/relink_library.py --kb <uuid> --apply review.csv  # rows marked approved=yes
"""

import argparse
import asyncio
from pathlib import Path
from uuid import UUID

from nalar.bootstrap.library_relink import apply, approved_pairs, propose, review_sheet
from nalar.bootstrap.settings import Settings
from nalar.infrastructure.db.pool import create_pool


async def main(
    kb_id: UUID, subject: str, phase: str, min_similarity: float, sheet: Path | None
) -> None:
    pool = await create_pool(Settings().database_url.get_secret_value(), min_size=1, max_size=1)
    try:
        async with pool.acquire() as conn, conn.transaction():
            if sheet is None:
                print(review_sheet(await propose(conn, kb_id, subject, phase, min_similarity)))
                return
            pairs = approved_pairs(await asyncio.to_thread(sheet.read_text, encoding="utf-8"))
            print(f"linked {await apply(conn, kb_id, pairs)} of {len(pairs)} approved rows")
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb", type=UUID, required=True)
    parser.add_argument("--subject", default="Ilmu Pengetahuan Alam")
    parser.add_argument("--phase", default="D")
    parser.add_argument("--min-similarity", type=float, default=0.8)
    parser.add_argument("--apply", type=Path, default=None, dest="sheet")
    args = parser.parse_args()
    asyncio.run(main(args.kb, args.subject, args.phase, args.min_similarity, args.sheet))
