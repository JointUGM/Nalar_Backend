import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol
from uuid import UUID, uuid5

import asyncpg

from nalar.domain.context_pack import (
    BankQuestion,
    PackMisconception,
    Target,
    VersionContent,
    build_context_pack,
    derive_probe_plan,
    validate_version,
)

BUCKETS = ("teaching-materials", "roster-imports")


class UserDirectory(Protocol):
    async def ensure_user(self, email: str, password: str, full_name: str) -> UUID: ...


class BucketAdmin(Protocol):
    async def ensure_bucket(self, name: str) -> None: ...


@dataclass(frozen=True)
class SeedReport:
    version_id: UUID
    pack: dict[str, Any]


def version_content(content: dict[str, Any]) -> VersionContent:
    pack, mission = content["pack"], content["mission"]
    return VersionContent(
        anchor_problem=pack["anchor_problem"],
        reference_reasoning=pack["reference_reasoning"],
        rubric=content["rubric"],
        targets=tuple(
            Target(UUID(t["id"]), t["name"], t.get("description") or "") for t in pack["targets"]
        ),
        misconceptions=tuple(
            PackMisconception(
                UUID(m["id"]),
                UUID(m["concept_id"]),
                m["statement"],
                tuple(m.get("detection_cues") or ()),
            )
            for m in pack.get("misconceptions") or []
        ),
        question_bank=tuple(
            BankQuestion(
                q["id"],
                UUID(q["concept_id"]),
                q["move"],
                q["text"],
                UUID(q["misconception_id"]) if q.get("misconception_id") else None,
            )
            for q in pack["question_bank"]
        ),
        answer_terms=tuple(pack["answer_terms"]),
        max_turns=mission["max_turns"],
        max_duration_minutes=mission["max_duration_minutes"],
    )


async def seed(
    conn: asyncpg.Connection,
    content: dict[str, Any],
    users: UserDirectory,
    buckets: BucketAdmin,
    password: str,
) -> SeedReport:
    """Idempotent: fixed ids and natural keys, so a second run changes nothing."""
    version = version_content(content)
    problems = validate_version(version)
    if problems:
        raise ValueError(f"seed pack is invalid: {problems}")
    pack = build_context_pack(version)

    async with conn.transaction():
        school_id, class_id, subject_id, year_id = await _school(conn, content)
        people = content["users"]
        teacher_id = await _person(conn, users, people["teacher"], password)
        await _member(conn, school_id, teacher_id, "teacher")
        admin_id = await _person(conn, users, people["admin"], password)
        await _member(conn, school_id, admin_id, "school_admin")
        await conn.execute(
            "insert into teaching_assignments (school_id, class_id, school_subject_id, teacher_id)"
            " values ($1, $2, $3, $4) on conflict do nothing",
            school_id,
            class_id,
            subject_id,
            teacher_id,
        )
        await _students(conn, users, people["students"], password, school_id, class_id, year_id)

        kb_id = await _knowledge_base(conn, content, version, school_id, subject_id, teacher_id)
        version_id = await _mission(conn, content, version, pack, school_id, kb_id, teacher_id)

    for bucket in BUCKETS:
        await buckets.ensure_bucket(bucket)
    return SeedReport(version_id=version_id, pack=pack)


async def _school(
    conn: asyncpg.Connection, content: dict[str, Any]
) -> tuple[UUID, UUID, UUID, UUID]:
    school, year, klass, subject = (
        content["school"],
        content["academic_year"],
        content["class"],
        content["subject"],
    )
    school_id, year_id = UUID(school["id"]), UUID(year["id"])
    class_id, subject_id = UUID(klass["id"]), UUID(subject["id"])
    await conn.execute(
        "insert into schools (id, name) values ($1, $2) on conflict (id) do nothing",
        school_id,
        school["name"],
    )
    await conn.execute(
        "insert into academic_years (id, school_id, label, starts_on, ends_on, is_current)"
        " values ($1, $2, $3, $4, $5, true) on conflict (id) do nothing",
        year_id,
        school_id,
        year["label"],
        date.fromisoformat(year["starts_on"]),
        date.fromisoformat(year["ends_on"]),
    )
    await conn.execute(
        "insert into classes (id, school_id, academic_year_id, name, grade_level)"
        " values ($1, $2, $3, $4, $5) on conflict (id) do nothing",
        class_id,
        school_id,
        year_id,
        klass["name"],
        klass["grade_level"],
    )
    await conn.execute(
        "insert into school_subjects (id, school_id, name) values ($1, $2, $3)"
        " on conflict (id) do nothing",
        subject_id,
        school_id,
        subject["name"],
    )
    return school_id, class_id, subject_id, year_id


async def _students(
    conn: asyncpg.Connection,
    users: UserDirectory,
    spec: dict[str, Any],
    password: str,
    school_id: UUID,
    class_id: UUID,
    year_id: UUID,
) -> dict[int, UUID]:
    students: dict[int, UUID] = {}
    for n in range(1, spec["count"] + 1):
        person = {"email": spec["email"].format(n=n), "full_name": spec["full_name"].format(n=n)}
        student_id = await _person(conn, users, person, password)
        students[n] = student_id
        await _member(conn, school_id, student_id, "student")
        await conn.execute(
            "insert into student_profiles (user_id, nisn) values ($1, $2)"
            " on conflict (user_id) do nothing",
            student_id,
            spec["nisn"].format(n=n),
        )
        await conn.execute(
            "insert into class_enrollments (school_id, academic_year_id, class_id, student_id)"
            " select $1, $2, $3, $4 where not exists (select 1 from class_enrollments"
            " where student_id = $4 and academic_year_id = $2 and status = 'active')",
            school_id,
            year_id,
            class_id,
            student_id,
        )
    return students


async def _knowledge_base(
    conn: asyncpg.Connection,
    content: dict[str, Any],
    version: VersionContent,
    school_id: UUID,
    subject_id: UUID,
    teacher_id: UUID,
) -> UUID:
    kb = content["knowledge_base"]
    kb_id = UUID(kb["id"])
    await conn.execute(
        "insert into knowledge_bases (id, school_id, school_subject_id, owner_teacher_id,"
        " topic_key, topic_title, status, approved_by, approved_at)"
        " values ($1, $2, $3, $4, $5, $6, 'approved', $4, now())"
        " on conflict (id) do nothing",
        kb_id,
        school_id,
        subject_id,
        teacher_id,
        kb["topic_key"],
        kb["topic_title"],
    )
    for t in version.targets:
        await conn.execute(
            "insert into concepts (id, school_id, knowledge_base_id, name, description,"
            " origin, review_status, reviewed_by, reviewed_at)"
            " values ($1, $2, $3, $4, $5, 'seeded', 'approved', $6, now())"
            " on conflict (id) do nothing",
            t.id,
            school_id,
            kb_id,
            t.name,
            t.description,
            teacher_id,
        )
    reviewed = content.get("misconception_content", {})
    for m in version.misconceptions:
        extra = reviewed.get(str(m.id), {})
        # Inserted approved after their concepts, which is what migration 003's trigger requires.
        await conn.execute(
            "insert into misconceptions (id, school_id, knowledge_base_id, concept_id,"
            " statement, correct_understanding, detection_cues, counter_examples, origin,"
            " review_status, reviewed_by, reviewed_at) values ($1, $2, $3, $4, $5, $6, $7, $8,"
            " 'seeded', 'approved', $9, now()) on conflict (id) do nothing",
            m.id,
            school_id,
            kb_id,
            m.concept_id,
            m.statement,
            extra.get("correct_understanding", content["correct_understanding"]),
            list(m.detection_cues),
            extra.get("counter_examples", []),
            teacher_id,
        )
    return kb_id


async def _mission(
    conn: asyncpg.Connection,
    content: dict[str, Any],
    version: VersionContent,
    pack: dict[str, Any],
    school_id: UUID,
    kb_id: UUID,
    teacher_id: UUID,
) -> UUID:
    mission = content["mission"]
    mission_id, version_id = UUID(mission["id"]), UUID(mission["version_id"])
    await conn.execute(
        "insert into missions (id, school_id, knowledge_base_id, created_by, title,"
        " learning_objective) values ($1, $2, $3, $4, $5, $6) on conflict (id) do nothing",
        mission_id,
        school_id,
        kb_id,
        teacher_id,
        mission["title"],
        mission["learning_objective"],
    )
    await conn.execute(
        "insert into mission_versions (id, school_id, mission_id, version_number,"
        " anchor_problem, rubric, probe_plan, max_turns, max_duration_minutes, created_by,"
        " reference_reasoning, context_pack, question_bank, answer_terms, live_warmup,"
        " reviewed_at, reviewed_by) values ($1, $2, $3, 1, $4, $5::jsonb, $6::jsonb, $7,"
        " $8, $9, $10, $11::jsonb, $12::jsonb, $13, $14::jsonb, now(), $9)"
        " on conflict (id) do nothing",
        version_id,
        school_id,
        mission_id,
        version.anchor_problem,
        json.dumps(version.rubric),
        json.dumps(derive_probe_plan(version.question_bank)),
        version.max_turns,
        version.max_duration_minutes,
        teacher_id,
        version.reference_reasoning,
        json.dumps(pack),
        json.dumps(pack["question_bank"]),
        list(version.answer_terms),
        json.dumps(mission["live_warmup"]),
    )
    for t in version.targets:
        await conn.execute(
            "insert into mission_version_concepts (mission_version_id, concept_id)"
            " values ($1, $2) on conflict do nothing",
            version_id,
            t.id,
        )
    for m in version.misconceptions:
        await conn.execute(
            "insert into mission_version_misconceptions (mission_version_id, misconception_id)"
            " values ($1, $2) on conflict do nothing",
            version_id,
            m.id,
        )
    return version_id


async def _person(
    conn: asyncpg.Connection, users: UserDirectory, person: dict[str, Any], password: str
) -> UUID:
    user_id = await users.ensure_user(person["email"], password, person["full_name"])
    await conn.execute(
        "insert into profiles (id, full_name, contact_email) values ($1, $2, $3)"
        " on conflict (id) do update set full_name = excluded.full_name",
        user_id,
        person["full_name"],
        person["email"],
    )
    return user_id


async def _member(conn: asyncpg.Connection, school_id: UUID, user_id: UUID, role: str) -> None:
    await conn.execute(
        "insert into school_memberships (school_id, user_id, role)"
        " values ($1, $2, $3::membership_role)"
        " on conflict (school_id, user_id, role) do update set status = 'active'",
        school_id,
        user_id,
        role,
    )


TRAIL_NAMESPACE = UUID("5a3f1c0e-7b6d-4d2a-9c58-0e1f2a3b4c5d")
TRAIL_MARK = "(contoh)"


def _trail_id(*parts: Any) -> UUID:
    return uuid5(TRAIL_NAMESPACE, "/".join(str(p) for p in parts))


async def seed_trail_demo(conn: asyncpg.Connection, content: dict[str, Any]) -> None:
    """Synthetic, labelled follow-up data for the demo class: 14 students hold one wrong idea
    in the first mission, 9 no longer show it in the second, 4 still do, 1 was not observed.
    Idempotent through fixed ids; no AI call and no school other than the demo school."""
    version = version_content(content)
    pack = build_context_pack(version)
    school_id = UUID(content["school"]["id"])
    class_id, kb_id = UUID(content["class"]["id"]), UUID(content["knowledge_base"]["id"])
    teacher_id = await conn.fetchval(
        "select id from profiles where contact_email = $1", content["users"]["teacher"]["email"]
    )
    held = version.misconceptions[0]
    students = await conn.fetch(
        "select student_id from class_enrollments where class_id = $1 and status = 'active'"
        " order by student_id limit 14",
        class_id,
    )
    if teacher_id is None or len(students) < 14:
        raise ValueError("seed the demo school before the trail demo")
    plan = (
        ("pertama", 14, 0),
        ("lanjutan", 7, 1),
    )
    outcomes = ["mastered"] * 6 + ["developing"] * 3 + ["misconception"] * 4 + [None]
    for label, days_ago, index in plan:
        title = f"{content['mission']['title']} {label} {TRAIL_MARK}"
        mission = {
            **content["mission"],
            "id": str(_trail_id("mission", label)),
            "version_id": str(_trail_id("version", label)),
            "title": title,
        }
        version_id = await _mission(
            conn, {**content, "mission": mission}, version, pack, school_id, kb_id, teacher_id
        )
        publication_id = _trail_id("publication", label)
        await conn.execute(
            "insert into publications (id, school_id, mission_version_id, class_id, published_by,"
            " created_at) values ($1, $2, $3, $4, $5, now() - make_interval(days => $6))"
            " on conflict (id) do nothing",
            publication_id,
            school_id,
            version_id,
            class_id,
            teacher_id,
            days_ago,
        )
        run_id = _trail_id("run", label)
        await conn.execute(
            "insert into publication_runs (id, school_id, publication_id, kind, mode, status,"
            " opens_at, closes_at, closed_at) values ($1, $2, $3, 'primary', 'window', 'closed',"
            " now() - make_interval(days => $4), now() - make_interval(days => $4, hours => -1),"
            " now() - make_interval(days => $4, hours => -1)) on conflict (id) do nothing",
            run_id,
            school_id,
            publication_id,
            days_ago,
        )
        for n, student in enumerate(students):
            outcome = "misconception" if index == 0 else outcomes[n]
            if outcome is None:
                continue
            session_id = _trail_id("session", label, student["student_id"])
            await conn.execute(
                "insert into sessions (id, school_id, publication_id, run_id, student_id,"
                " attempt_number, status, end_reason, started_at, ended_at, deadline_at)"
                " values ($1, $2, $3, $4, $5, 1, 'completed', 'student_completed',"
                " now() - make_interval(days => $6, mins => 30),"
                " now() - make_interval(days => $6, mins => 10),"
                " now() - make_interval(days => $6, mins => 10)) on conflict (id) do nothing",
                session_id,
                school_id,
                publication_id,
                run_id,
                student["student_id"],
                days_ago,
            )
            await conn.execute(
                "insert into session_turns (school_id, session_id, turn_index, prompt_kind,"
                " prompt_text) values ($1, $2, 0, 'anchor', $3) on conflict do nothing",
                school_id,
                session_id,
                version.anchor_problem,
            )
            await conn.execute(
                "insert into session_evaluations (school_id, session_id) values ($1, $2)"
                " on conflict do nothing",
                school_id,
                session_id,
            )
            await conn.execute(
                "insert into session_concept_results (school_id, session_id, concept_id,"
                " outcome, misconception_id) values ($1, $2, $3, $4::concept_outcome, $5)"
                " on conflict (session_id, concept_id) do nothing",
                school_id,
                session_id,
                held.concept_id,
                outcome,
                held.id if outcome == "misconception" else None,
            )
