import asyncpg
import pytest

from nalar.domain.labels import (
    PROBE_MOVES,
    EvaluationStatus,
    ParticipantStatus,
    RunMode,
    RunStatus,
    SessionEndReason,
    SessionStatus,
)

CASES = [
    ("run_status", {s.value for s in RunStatus}),
    ("run_mode", {s.value for s in RunMode}),
    ("session_status", {s.value for s in SessionStatus}),
    ("session_end_reason", {s.value for s in SessionEndReason}),
    ("participant_status", {s.value for s in ParticipantStatus}),
    ("evaluation_status", {s.value for s in EvaluationStatus}),
    ("probe_strategy", set(PROBE_MOVES)),
]


@pytest.mark.parametrize(("type_name", "labels"), CASES, ids=[c[0] for c in CASES])
async def test_domain_labels_exist_in_the_database(
    conn: asyncpg.Connection, type_name: str, labels: set[str]
) -> None:
    rows = await conn.fetch(
        "select e.enumlabel from pg_enum e join pg_type t on t.oid = e.enumtypid"
        " where t.typname = $1",
        type_name,
    )
    assert labels <= {row["enumlabel"] for row in rows}
