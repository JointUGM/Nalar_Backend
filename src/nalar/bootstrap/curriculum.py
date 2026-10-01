from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import batched
from math import isfinite
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from nalar.application.features.knowledge_base.s1_calls import Record
from nalar.application.ports.ai import AiGateway, AiServiceError
from nalar.application.ports.ai_contract import EmbedIn, InvocationOut, Tag
from nalar.infrastructure.db.pool import DbConnection
from nalar.infrastructure.db.repositories.ai_invocations import PgAiInvocationLog


class CurriculumElement(BaseModel):
    element: str = Field(min_length=1)
    description: str = Field(min_length=1)
    statements: list[str]


class Curriculum(BaseModel):
    decree_code: str = Field(min_length=1)
    title: str = Field(min_length=1)
    effective_on: date
    subject: str = Field(min_length=1)
    phase: str = Field(pattern="^[A-F]$")
    elements: list[CurriculumElement]


class LibraryEntry(BaseModel):
    topic: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    correct_understanding: str = Field(min_length=1)
    student_phrasings: list[str]
    counter_examples: list[str]
    source_citations: list[str] = Field(min_length=1)


class CurriculumLibrary(BaseModel):
    subject: str = Field(min_length=1)
    phase: str = Field(pattern="^[A-F]$")
    entries: list[LibraryEntry]


@dataclass(frozen=True)
class SeedCounts:
    statements: int
    library: int


async def _embed(
    ai: AiGateway, tag: Tag, texts: Sequence[str], model: str, record: Record
) -> list[list[float]]:
    vectors: list[list[float]] = []
    for batch in batched(texts, 256):
        try:
            reply = await ai.embed(
                EmbedIn.model_validate({"tag": tag, "texts": list(batch)}), f"seed-{uuid4()}"
            )
        except AiServiceError as error:
            await record(error.invocations)
            raise
        await record(reply.invocations)
        if reply.result.embedding_model != model:
            raise RuntimeError(f"embedding model {reply.result.embedding_model} != {model}")
        if (
            reply.result.dimensions != 1536
            or len(reply.result.vectors) != len(batch)
            or any(len(v) != 1536 or not all(map(isfinite, v)) for v in reply.result.vectors)
        ):
            raise RuntimeError("invalid curriculum embedding dimensions or count")
        vectors.extend(reply.result.vectors)
    return vectors


async def seed_curriculum(
    conn: DbConnection,
    ai: AiGateway,
    cp: Mapping[str, Any],
    library: Mapping[str, Any],
    model: str,
    record: Record | None = None,
) -> SeedCounts:
    """Idempotent: re-running adds and embeds only what is missing (D-S26-16)."""
    curriculum = Curriculum.model_validate(cp)
    entries = CurriculumLibrary.model_validate(library)
    if (curriculum.subject, curriculum.phase) != (entries.subject, entries.phase):
        raise ValueError("CP and library subject and phase must match")
    texts = [s for e in curriculum.elements for s in e.statements]
    texts += [e.statement for e in entries.entries]
    if any(not t.strip() or len(t) > 30_000 for t in texts):
        raise ValueError("curriculum embedding texts must contain 1–30000 characters")
    if any(not c.strip() for e in entries.entries for c in e.source_citations):
        raise ValueError("library citations must not be blank")
    cp = curriculum.model_dump(mode="json")
    library = entries.model_dump(mode="json")
    if record is None:

        async def record_local(invocations: Sequence[InvocationOut]) -> None:
            await PgAiInvocationLog(conn).record(None, invocations)

        record = record_local
    await conn.execute("select pg_advisory_xact_lock(hashtext('nalar-curriculum-seed'))")
    version_id = await conn.fetchval(
        "insert into cp_versions (decree_code, title, effective_on, status, published_at)"
        " values ($1, $2, $3::date, 'published', now())"
        " on conflict (decree_code) do update set title = excluded.title returning id",
        cp["decree_code"],
        cp["title"],
        date.fromisoformat(cp["effective_on"]),
    )
    subject_id = await conn.fetchval(
        "insert into cp_subjects (cp_version_id, name, phase) values ($1, $2, $3)"
        " on conflict (cp_version_id, name, phase) do update set name = excluded.name returning id",
        version_id,
        cp["subject"],
        cp["phase"],
    )
    missing: list[tuple[str, str, Any]] = []
    seen_statements: set[str] = set()
    for ordinal, element in enumerate(cp["elements"]):
        element_id = await conn.fetchval(
            "select id from cp_learning_outcomes where cp_subject_id = $1 and grain = 'element'"
            " and description = $2",
            subject_id,
            element["description"],
        ) or await conn.fetchval(
            "insert into cp_learning_outcomes (cp_subject_id, element, description, ordinal, grain)"
            " values ($1, $2, $3, $4, 'element') returning id",
            subject_id,
            element["element"],
            element["description"],
            ordinal,
        )
        for statement in element["statements"]:
            if statement in seen_statements:
                continue
            seen_statements.add(statement)
            exists = await conn.fetchval(
                "select exists (select 1 from cp_learning_outcomes where cp_subject_id = $1"
                " and grain = 'statement' and description = $2)",
                subject_id,
                statement,
            )
            if not exists:
                missing.append((element["element"], statement, element_id))
    vectors = await _embed(ai, Tag.cp_statement, [s for _, s, _ in missing], model, record)
    for (element, statement, parent_id), vec in zip(missing, vectors, strict=True):
        await conn.execute(
            "insert into cp_learning_outcomes (cp_subject_id, element, description, grain,"
            " parent_id, embedding, embedding_model) values ($1, $2, $3, 'statement', $4, $5, $6)",
            subject_id,
            element,
            statement,
            parent_id,
            vec,
            model,
        )
    new_entries = [
        e
        for e in {(e["topic"], e["statement"]): e for e in library["entries"]}.values()
        if not await conn.fetchval(
            "select exists (select 1 from misconception_library where subject = $1 and phase = $2"
            " and topic = $3 and statement = $4)",
            library["subject"],
            library["phase"],
            e["topic"],
            e["statement"],
        )
    ]
    lib_vectors = await _embed(
        ai, Tag.library, [e["statement"] for e in new_entries], model, record
    )
    for e, vec in zip(new_entries, lib_vectors, strict=True):
        await conn.execute(
            "insert into misconception_library (subject, phase, topic, statement,"
            " correct_understanding, student_phrasings, counter_examples, source_citations,"
            " status, embedding, embedding_model, published_at)"
            " values ($1, $2, $3, $4, $5, $6, $7, $8, 'published', $9, $10, now())",
            library["subject"],
            library["phase"],
            e["topic"],
            e["statement"],
            e["correct_understanding"],
            e["student_phrasings"],
            e["counter_examples"],
            e["source_citations"],
            vec,
            model,
        )
    return SeedCounts(len(missing), len(new_entries))
