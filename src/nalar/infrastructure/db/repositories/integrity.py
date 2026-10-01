import json
from collections.abc import Sequence
from uuid import UUID

from nalar.application.ports.integrity import SessionIntegrityInput
from nalar.domain.integrity import AnswerFacts, FlagDraft
from nalar.domain.telemetry import TurnMetrics
from nalar.infrastructure.db.pool import DbConnection

_TERMINAL = "('completed', 'timed_out', 'ended_safety')"


class PgIntegrityRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def session_input(self, session_id: UUID) -> SessionIntegrityInput | None:
        head = await self._conn.fetchrow(
            "select s.school_id, e.turn_quality from sessions s"
            "  join session_evaluations e on e.session_id = s.id"
            f" where s.id = $1 and s.status in {_TERMINAL}",
            session_id,
        )
        if head is None:
            return None
        turns = await self._conn.fetch(
            "select id, turn_index, answer_text, safety_paused from session_turns"
            " where session_id = $1 order by turn_index",
            session_id,
        )
        batches = await self._conn.fetch(
            "select turn_id, events from telemetry_batches"
            " where session_id = $1 order by client_seq",
            session_id,
        )
        quality = {UUID(q["turn_id"]): int(q["quality"]) for q in json.loads(head["turn_quality"])}
        return SessionIntegrityInput(
            school_id=head["school_id"],
            turns=tuple(
                (t["id"], t["turn_index"], t["answer_text"], t["safety_paused"]) for t in turns
            ),
            quality=quality,
            batches=tuple((b["turn_id"], json.loads(b["events"])) for b in batches),
        )

    async def upsert_metrics(self, school_id: UUID, metrics: dict[UUID, TurnMetrics]) -> None:
        await self._conn.executemany(
            "insert into turn_metrics (turn_id, school_id, typing_duration_ms, chars_typed,"
            " chars_pasted, paste_events, tab_hidden_events, tab_hidden_ms, disconnect_events)"
            " values ($1, $2, $3, $4, $5, $6, $7, $8, $9)"
            " on conflict (turn_id) do update set typing_duration_ms = excluded.typing_duration_ms,"
            " chars_typed = excluded.chars_typed, chars_pasted = excluded.chars_pasted,"
            " paste_events = excluded.paste_events, tab_hidden_events = excluded.tab_hidden_events,"
            " tab_hidden_ms = excluded.tab_hidden_ms,"
            " disconnect_events = excluded.disconnect_events,"
            " computed_at = now()",
            [
                (
                    turn_id,
                    school_id,
                    m.typing_duration_ms,
                    m.chars_typed,
                    m.chars_pasted,
                    m.paste_events,
                    m.tab_hidden_events,
                    m.tab_hidden_ms,
                    m.disconnect_events,
                )
                for turn_id, m in metrics.items()
            ],
        )

    async def insert_flags(
        self, school_id: UUID, session_id: UUID, flags: Sequence[FlagDraft]
    ) -> int:
        added = 0
        for f in flags:
            # NFR-R3: the duplicate check lives in the statement, so a re-run adds nothing.
            row = await self._conn.fetchval(
                "insert into authenticity_flags"
                " (school_id, session_id, turn_id, flag_type, severity, evidence)"
                " select $1, $2, $3, $4::flag_type, $5::flag_severity, $6::jsonb"
                " where not exists (select 1 from authenticity_flags where session_id = $2"
                "   and flag_type = $4::flag_type and turn_id is not distinct from $3)"
                " returning id",
                school_id,
                session_id,
                f.turn_id,
                f.flag_type,
                f.severity,
                json.dumps(f.evidence),
            )
            added += row is not None
        return added

    async def publication_answers(self, publication_id: UUID) -> list[AnswerFacts]:
        rows = await self._conn.fetch(
            "select s.id as session_id, t.id as turn_id, t.turn_index, t.answer_text"
            "  from sessions s join session_evaluations e on e.session_id = s.id"
            "  join session_turns t on t.session_id = s.id"
            f" where s.publication_id = $1 and s.status in {_TERMINAL}"
            "   and t.answer_text is not null and not t.safety_paused",
            publication_id,
        )
        return [
            AnswerFacts(r["session_id"], r["turn_id"], r["turn_index"], r["answer_text"])
            for r in rows
        ]

    async def school_of_publication(self, publication_id: UUID) -> UUID | None:
        school_id: UUID | None = await self._conn.fetchval(
            "select school_id from publications where id = $1", publication_id
        )
        return school_id
