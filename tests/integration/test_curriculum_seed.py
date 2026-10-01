from copy import deepcopy
from typing import Any

import asyncpg
import pytest

from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import EmbedOut
from nalar.bootstrap.curriculum import seed_curriculum
from tests.unit.application.fakes import ScriptedAiGateway, invocation, vector

MODEL = "text-embedding-3-small"
CP: dict[str, Any] = {
    "decree_code": "UJI/046",
    "title": "CP Uji",
    "effective_on": "2025-07-01",
    "subject": "Ilmu Pengetahuan Alam",
    "phase": "D",
    "elements": [
        {
            "element": "Pemahaman IPA",
            "description": "Paragraf elemen.",
            "statements": ["Peserta didik menjelaskan gaya.", "Peserta didik menjelaskan gerak."],
        }
    ],
}
LIBRARY: dict[str, Any] = {
    "subject": "Ilmu Pengetahuan Alam",
    "phase": "D",
    "entries": [
        {
            "topic": "Gaya dan Gerak",
            "statement": "Benda diam tidak dikenai gaya.",
            "correct_understanding": "Gaya-gaya pada benda diam seimbang.",
            "student_phrasings": ["kalau diam berarti tidak ada gaya"],
            "counter_examples": [],
            "source_citations": ["Driver et al. (1994)"],
        }
    ],
}


def embedded(n: int) -> AiResult[EmbedOut]:
    return AiResult(
        EmbedOut(dimensions=1536, embedding_model=MODEL, vectors=[vector(i) for i in range(n)]),
        [invocation("embedding")],
    )


async def test_seed_is_idempotent_and_embeds_once(conn: asyncpg.Connection) -> None:
    ai = ScriptedAiGateway()
    ai.script("embed", embedded(2), embedded(1))
    first = await seed_curriculum(conn, ai, CP, LIBRARY, MODEL)
    second = await seed_curriculum(conn, ai, CP, LIBRARY, MODEL)
    assert (first.statements, first.library) == (2, 1)
    assert (second.statements, second.library) == (0, 0)
    assert len(ai.calls) == 2
    assert await conn.fetchval("select count(*) from ai_invocations where school_id is null") >= 2
    assert (
        await conn.fetchval(
            "select count(*) from cp_learning_outcomes o"
            " join cp_subjects s on s.id = o.cp_subject_id"
            " join cp_versions v on v.id = s.cp_version_id where v.decree_code = 'UJI/046'"
            " and o.grain = 'statement' and o.embedding_model = $1",
            MODEL,
        )
        == 2
    )


async def test_seed_logs_failed_embedding(conn: asyncpg.Connection) -> None:
    ai = ScriptedAiGateway()
    failure = invocation("embedding", "error")
    ai.script("embed", AiServiceError("upstream_unavailable", 503, [failure]))
    with pytest.raises(AiServiceError):
        await seed_curriculum(conn, ai, CP, LIBRARY, MODEL)
    assert await conn.fetchval(
        "select exists (select 1 from ai_invocations where request_id = $1 and status = 'failed')",
        failure.request_id,
    )


async def test_seed_rejects_missing_citations_before_ai_or_database_writes(
    conn: asyncpg.Connection,
) -> None:
    library = deepcopy(LIBRARY)
    library["entries"][0]["source_citations"] = []
    ai = ScriptedAiGateway()
    with pytest.raises(ValueError):
        await seed_curriculum(conn, ai, CP, library, MODEL)
    assert ai.calls == []
    assert not await conn.fetchval(
        "select exists (select 1 from cp_versions where decree_code = $1)", CP["decree_code"]
    )


async def test_seed_deduplicates_repeated_input_entries(conn: asyncpg.Connection) -> None:
    cp = deepcopy(CP)
    cp["elements"][0]["statements"].append("Peserta didik menjelaskan gaya.")
    library = deepcopy(LIBRARY)
    library["entries"].append(library["entries"][0])
    ai = ScriptedAiGateway()
    ai.script("embed", embedded(2), embedded(1))
    result = await seed_curriculum(conn, ai, cp, library, MODEL)
    assert (result.statements, result.library) == (2, 1)
