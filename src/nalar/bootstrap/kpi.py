from datetime import datetime
from typing import Any
from uuid import UUID

from nalar.domain.misconception_trail import trail
from nalar.infrastructure.db.pool import DbConnection
from nalar.infrastructure.db.repositories.results import PgResultsRepo

MIN_GROUP = 3


async def teacher_cycle(conn: DbConnection, school_id: UUID, since: datetime) -> dict[str, Any]:
    """Aggregates for the team only: never per teacher, suppressed below MIN_GROUP teachers
    and classes (design rule 1). No AI call; counts come from SQL and the trail domain."""
    scope = await conn.fetchrow(
        "select count(distinct mi.created_by) as teachers, count(distinct p.class_id) as classes"
        " from publications p join mission_versions mv on mv.id = p.mission_version_id"
        " join missions mi on mi.id = mv.mission_id"
        " where p.school_id = $1 and p.created_at >= $2 and p.cancelled_at is null",
        school_id,
        since,
    )
    assert scope is not None
    if scope["teachers"] < MIN_GROUP and scope["classes"] < MIN_GROUP:
        return {"suppressed": True, "min_group": MIN_GROUP}
    repo = PgResultsRepo(conn)
    publications = await conn.fetch(
        "select id from publications where school_id = $1 and created_at >= $2"
        " and cancelled_at is null order by created_at, id",
        school_id,
        since,
    )
    pairs = held = resolved = still_held = retesting = 0
    for row in publications:
        data = await repo.misconception_trail_input(row["id"], None)
        if data is None or data.earlier is None:
            continue
        pairs += 1
        found = trail(
            [(m, c) for m, c, _ in data.earlier.misconceptions],
            {c for c, _ in data.concepts},
            {m for m, _, _ in data.to.misconceptions},
            data.earlier.attempts,
            data.to.attempts,
        )
        retesting += bool(found.items)
        held += sum(i.held_count for i in found.items)
        resolved += sum(i.resolved_count for i in found.items)
        still_held += sum(len(i.still_held_ids) for i in found.items)
    versions = await conn.fetchrow(
        "select count(*) as total,"
        " count(*) filter (where base_version_id is not null or version_number > 1) as revised"
        " from mission_versions where school_id = $1 and created_at >= $2",
        school_id,
        since,
    )
    scores = await conn.fetchrow(
        "select count(*) as total, (select count(*) from score_overrides o"
        " where o.school_id = $1 and o.created_at >= $2) as overridden"
        " from evaluation_scores s join session_evaluations e on e.id = s.evaluation_id"
        " where s.school_id = $1 and e.evaluated_at >= $2",
        school_id,
        since,
    )
    assert versions is not None and scores is not None
    return {
        "suppressed": False,
        "follow_up_pairs": pairs,
        "pairs_retesting_a_held_misconception": retesting,
        "held": held,
        "no_longer_shown": resolved,
        "still_held": still_held,
        "versions": versions["total"],
        "versions_revised": versions["revised"],
        "scores": scores["total"],
        "scores_overridden_with_reason": scores["overridden"],
    }
