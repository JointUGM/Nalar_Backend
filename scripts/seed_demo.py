"""Seed the demo school and the Gaya dan Gerak mission (idempotent); --warm checks the pack."""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import httpx

from nalar.application.ports.ai_contract import ContextPackIn, WarmIn
from nalar.bootstrap.seed import seed
from nalar.bootstrap.settings import Settings
from nalar.infrastructure.ai.client import AiServiceClient, AiTimeouts
from nalar.infrastructure.auth.admin import SupabaseAuthAdmin
from nalar.infrastructure.db.repositories.ai_invocations import PgAiInvocationLog
from nalar.infrastructure.storage.supabase_storage import SupabaseStorage

CONTENT = Path(__file__).resolve().parents[1] / "supabase" / "seed" / "gaya_dan_gerak.json"


async def main(warm: bool) -> None:
    settings = Settings()
    password = settings.seed_password.get_secret_value()
    if not password:
        raise SystemExit("set NALAR_SEED_PASSWORD")
    content = json.loads(CONTENT.read_text(encoding="utf-8"))
    key = settings.supabase_service_role_key.get_secret_value()
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(
        base_url=settings.supabase_url, headers=headers, timeout=30
    ) as http:
        conn = await asyncpg.connect(settings.database_url.get_secret_value())
        try:
            report = await seed(
                conn, content, SupabaseAuthAdmin(http), SupabaseStorage(http), password
            )
            print(f"seeded version {report.version_id}")
            if warm:
                await _warm(settings, conn, report.pack, UUID(content["school"]["id"]))
        finally:
            await conn.close()


async def _warm(
    settings: Settings, conn: asyncpg.Connection, pack: dict[str, Any], school_id: UUID
) -> None:
    async with httpx.AsyncClient(
        base_url=settings.ai_base_url,
        headers={"X-Service-Key": settings.ai_service_key.get_secret_value()},
    ) as http:
        client = AiServiceClient(
            http,
            AiTimeouts(
                turn_s=settings.ai_turn_timeout_s,
                warm_s=settings.ai_warm_timeout_s,
                evaluate_s=settings.ai_evaluate_timeout_s,
                embed_s=settings.ai_embed_timeout_s,
                s5_insight_s=settings.ai_s5_insight_timeout_s,
                s5_summary_s=settings.ai_s5_summary_timeout_s,
            ),
        )
        result = await client.warm_run(
            WarmIn(context_pack=ContextPackIn.model_validate(pack)), request_id="seed-warm"
        )
        await PgAiInvocationLog(conn).record(school_id, result.invocations)
        print(f"warm: {result.result.warmed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--warm", action="store_true", help="validate the pack on the AI service")
    asyncio.run(main(parser.parse_args().warm))
