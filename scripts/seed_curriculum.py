"""Load CP statements and the misconception library through the AI embedding service.

uv run python scripts/seed_curriculum.py supabase/seed/curriculum/cp_ipa_fase_d.json \
    supabase/seed/curriculum/misconception_library_ipa.json [--map-school-subject <uuid>]
uv run python scripts/seed_curriculum.py --library-only <library.json>   # CP already seeded
uv run python scripts/seed_curriculum.py --check [--library-only] <files>  # validate, free

Every library entry needs at least one real citation and a paraphrased wording written by the
pedagogy lead; embedding costs a few cents, so ask first.
"""

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import httpx

from nalar.application.ports.ai_contract import InvocationOut
from nalar.bootstrap.curriculum import Curriculum, seed_curriculum, seed_library, validate_library
from nalar.bootstrap.settings import Settings
from nalar.infrastructure.ai.client import AiServiceClient, AiTimeouts
from nalar.infrastructure.db.pool import create_pool
from nalar.infrastructure.db.repositories.ai_invocations import PgAiInvocationLog


def check(cp_path: Path | None, library_path: Path) -> None:
    if cp_path is not None:
        Curriculum.model_validate(json.loads(cp_path.read_text(encoding="utf-8")))
    entries = validate_library(json.loads(library_path.read_text(encoding="utf-8")))
    print(f"valid: {entries} distinct library entries, each with at least one citation")


async def main_library(library_path: Path) -> None:
    s = Settings()
    library = json.loads(await asyncio.to_thread(library_path.read_text, encoding="utf-8"))
    validate_library(library)
    pool = await create_pool(
        s.database_url.get_secret_value(),
        min_size=1,
        max_size=2,
        statement_cache_size=s.db_statement_cache_size,
        command_timeout=s.db_command_timeout_s,
    )

    async def record(invocations: Sequence[InvocationOut]) -> None:
        async with pool.acquire() as log_conn, log_conn.transaction():
            await PgAiInvocationLog(log_conn).record(None, invocations)

    try:
        async with httpx.AsyncClient(
            base_url=s.ai_base_url, headers={"X-Service-Key": s.ai_service_key.get_secret_value()}
        ) as http:
            ai = AiServiceClient(http, _timeouts(s))
            async with pool.acquire() as conn, conn.transaction():
                added = await seed_library(conn, ai, library, s.embedding_model, record)
    finally:
        await pool.close()
    print(f"library entries added: {added}")


def _timeouts(s: Settings) -> AiTimeouts:
    return AiTimeouts(
        turn_s=s.ai_turn_timeout_s,
        warm_s=s.ai_warm_timeout_s,
        evaluate_s=s.ai_evaluate_timeout_s,
        embed_s=s.ai_embed_timeout_s,
        s5_insight_s=s.ai_s5_insight_timeout_s,
        s5_summary_s=s.ai_s5_summary_timeout_s,
        s1_step_s=s.ai_s1_step_timeout_s,
    )


async def main(cp_path: Path, library_path: Path, school_subject: UUID | None) -> None:
    s = Settings()
    cp = json.loads(await asyncio.to_thread(cp_path.read_text, encoding="utf-8"))
    library = json.loads(await asyncio.to_thread(library_path.read_text, encoding="utf-8"))
    pool = await create_pool(
        s.database_url.get_secret_value(),
        min_size=1,
        max_size=2,
        statement_cache_size=s.db_statement_cache_size,
        command_timeout=s.db_command_timeout_s,
    )

    async def record(invocations: Sequence[InvocationOut]) -> None:
        async with pool.acquire() as log_conn, log_conn.transaction():
            await PgAiInvocationLog(log_conn).record(None, invocations)

    try:
        async with httpx.AsyncClient(
            base_url=s.ai_base_url, headers={"X-Service-Key": s.ai_service_key.get_secret_value()}
        ) as http:
            ai = AiServiceClient(http, _timeouts(s))
            async with pool.acquire() as conn, conn.transaction():
                if school_subject is not None and not await conn.fetchval(
                    "select exists (select 1 from school_subjects where id = $1)", school_subject
                ):
                    raise ValueError("school subject does not exist")
                counts = await seed_curriculum(conn, ai, cp, library, s.embedding_model, record)
                if school_subject is not None:
                    await conn.execute(
                        "update school_subjects set cp_subject_id = (select s.id from cp_subjects s"
                        " join cp_versions v on v.id = s.cp_version_id where v.decree_code = $2"
                        " and s.name = $3 and s.phase = $4) where id = $1",
                        school_subject,
                        cp["decree_code"],
                        cp["subject"],
                        cp["phase"],
                    )
    finally:
        await pool.close()
    print(f"statements added: {counts.statements}, library entries added: {counts.library}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("files", type=Path, nargs="+", help="<cp.json> <library.json>")
    parser.add_argument("--map-school-subject", type=UUID, default=None)
    parser.add_argument(
        "--library-only", action="store_true", help="seed only <library.json>; CP is left as is"
    )
    parser.add_argument(
        "--check", action="store_true", help="validate the files; no database, no AI call, no cost"
    )
    args = parser.parse_args()
    if len(args.files) != (1 if args.library_only else 2):
        parser.error("give <cp.json> <library.json>, or only <library.json> with --library-only")
    library_path = args.files[-1]
    cp_path = None if args.library_only else args.files[0]
    if args.check:
        check(cp_path, library_path)
    elif cp_path is None:
        if args.map_school_subject is not None:
            parser.error("--map-school-subject needs the CP file")
        asyncio.run(main_library(library_path))
    else:
        asyncio.run(main(cp_path, library_path, args.map_school_subject))
