import json
from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

from nalar.application.ports.ai_contract import InvocationOut
from nalar.infrastructure.ai.mapping import CALL_STATUS_LABELS
from nalar.infrastructure.db.pool import DbConnection

_INSERT = """
    insert into ai_invocations (
        school_id, purpose, model, prompt_version, status, input_tokens, output_tokens,
        cache_read_tokens, cache_write_tokens, latency_ms, cost_usd, error_message,
        provider, request_id, retrieval)
    values ($1, $2::ai_purpose, $3, $4, $5::ai_call_status, $6, $7, $8, $9, $10, $11, $12,
            $13, $14, $15::jsonb)
    returning id
"""


class PgAiInvocationLog:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def record(
        self, school_id: UUID | None, invocations: Sequence[InvocationOut]
    ) -> list[UUID]:
        ids: list[UUID] = []
        for invocation in invocations:
            retrieval = [ref.model_dump(mode="json") for ref in invocation.retrieval]
            invocation_id: UUID = await self._conn.fetchval(
                _INSERT,
                school_id,
                invocation.purpose.value,
                invocation.model,
                invocation.prompt_version,
                CALL_STATUS_LABELS[invocation.status],
                invocation.input_tokens,
                invocation.output_tokens,
                invocation.cache_read_tokens,
                invocation.cache_write_tokens,
                invocation.latency_ms,
                Decimal(str(invocation.cost_usd)),
                invocation.error_message,
                invocation.provider,
                invocation.request_id,
                json.dumps(retrieval),
            )
            ids.append(invocation_id)
        return ids
