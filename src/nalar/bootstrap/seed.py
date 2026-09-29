import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol
from uuid import UUID

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
        students = await _students(
            conn, users, people["students"], password, school_id, class_id, year_id
        )
        for parent in people["parents"]:
            parent_id = await _person(conn, users, parent, password)
            for child in parent["children"]:
                await conn.execute(
                    "insert into parent_student_links (parent_id, student_id, school_id)"
                    " values ($1, $2, $3) on conflict (parent_id, student_id) do nothing",
                    parent_id,
                    students[child],
                    school_id,
                )
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
    for m in version.misconceptions:
        # Inserted approved after their concepts, which is what migration 003's trigger requires.
        await conn.execute(
            "insert into misconceptions (id, school_id, knowledge_base_id, concept_id,"
            " statement, correct_understanding, detection_cues, origin, review_status,"
            " reviewed_by, reviewed_at) values ($1, $2, $3, $4, $5, $6, $7, 'seeded',"
            " 'approved', $8, now()) on conflict (id) do nothing",
            m.id,
            school_id,
            kb_id,
            m.concept_id,
            m.statement,
            content["correct_understanding"],
            list(m.detection_cues),
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
