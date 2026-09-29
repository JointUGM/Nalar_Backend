from typing import Any

import asyncpg

from nalar.application.ports.ai_contract import AiPurpose, InvocationOut
from nalar.infrastructure.db.repositories.ai_invocations import PgAiInvocationLog
from tests.integration.support.factories import World


def invocation(**overrides: Any) -> InvocationOut:
    return InvocationOut.model_validate(
        {
            "purpose": "turn_analyze",
            "model": "gpt-5.4-mini",
            "prompt_version": "classify_answer.v1",
            "provider": "sumopod",
            "status": "success",
            "input_tokens": 900,
            "output_tokens": 40,
            "cache_read_tokens": 800,
            "cache_write_tokens": 0,
            "latency_ms": 1340,
            "cost_usd": 0.001012,
            "request_id": "req-9",
            "error_message": None,
            "retrieval": [
                {
                    "source": "material_chunk",
                    "id": "3f2a4f0c-2f38-4c55-9e0b-0b1bb2d3c5a1",
                    "path": "link",
                    "rank": 1,
                    "score": 0.91,
                }
            ],
            **overrides,
        }
    )


async def test_every_ai_purpose_is_a_database_label(conn: asyncpg.Connection) -> None:
    labels = {
        row["enumlabel"]
        for row in await conn.fetch(
            "select e.enumlabel from pg_enum e join pg_type t on t.oid = e.enumtypid"
            " where t.typname = 'ai_purpose'"
        )
    }
    assert {purpose.value for purpose in AiPurpose} <= labels


async def test_success_and_failure_are_both_recorded(
    conn: asyncpg.Connection, world: World
) -> None:
    failed = invocation(status="error", error_message="timeout after 2.0 s", output_tokens=0)
    ids = await PgAiInvocationLog(conn).record(world.school_id, [invocation(), failed])

    rows = await conn.fetch(
        "select status::text as status, error_message, cache_read_tokens, cost_usd, retrieval"
        " from ai_invocations where id = any($1::uuid[]) order by created_at, status desc",
        ids,
    )
    assert len(ids) == 2
    assert sorted(row["status"] for row in rows) == ["failed", "succeeded"]
    succeeded = next(row for row in rows if row["status"] == "succeeded")
    assert succeeded["cache_read_tokens"] == 800
    assert str(succeeded["cost_usd"]) == "0.001012"
    assert '"source": "material_chunk"' in succeeded["retrieval"]
