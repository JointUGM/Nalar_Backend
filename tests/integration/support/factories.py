import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg

RUBRIC = {
    dimension: [f"{dimension} level {level}" for level in range(5)]
    for dimension in ("claim", "evidence", "mechanism", "transfer")
}


async def create_user(conn: asyncpg.Connection, full_name: str = "Pengguna Uji") -> UUID:
    user_id = uuid4()
    email = f"{user_id}@test.nalar"
    await conn.execute("insert into auth.users (id, email) values ($1, $2)", user_id, email)
    await conn.execute(
        "insert into profiles (id, full_name, contact_email) values ($1, $2, $3)",
        user_id,
        full_name,
        email,
    )
    return user_id


async def create_school(conn: asyncpg.Connection, name: str = "SMP Uji") -> UUID:
    school_id: UUID = await conn.fetchval(
        "insert into schools (name) values ($1) returning id", name
    )
    return school_id


async def add_membership(
    conn: asyncpg.Connection,
    school_id: UUID,
    user_id: UUID,
    role: str,
    status: str = "active",
) -> None:
    await conn.execute(
        "insert into school_memberships (school_id, user_id, role, status)"
        " values ($1, $2, $3::membership_role, $4::membership_status)",
        school_id,
        user_id,
        role,
        status,
    )


async def create_academic_year(conn: asyncpg.Connection, school_id: UUID) -> UUID:
    year_id: UUID = await conn.fetchval(
        "insert into academic_years (school_id, label, starts_on, ends_on, is_current)"
        " values ($1, '2026/2027', '2026-07-13', '2027-06-30', true) returning id",
        school_id,
    )
    return year_id


async def create_class(
    conn: asyncpg.Connection, school_id: UUID, year_id: UUID, name: str = "8A"
) -> UUID:
    class_id: UUID = await conn.fetchval(
        "insert into classes (school_id, academic_year_id, name, grade_level)"
        " values ($1, $2, $3, 8) returning id",
        school_id,
        year_id,
        name,
    )
    return class_id


async def create_subject(conn: asyncpg.Connection, school_id: UUID, name: str = "IPA") -> UUID:
    subject_id: UUID = await conn.fetchval(
        "insert into school_subjects (school_id, name) values ($1, $2) returning id",
        school_id,
        name,
    )
    return subject_id


async def assign_teacher(
    conn: asyncpg.Connection,
    school_id: UUID,
    class_id: UUID,
    subject_id: UUID,
    teacher_id: UUID,
) -> None:
    await conn.execute(
        "insert into teaching_assignments (school_id, class_id, school_subject_id, teacher_id)"
        " values ($1, $2, $3, $4)",
        school_id,
        class_id,
        subject_id,
        teacher_id,
    )


async def create_teacher(conn: asyncpg.Connection, school_id: UUID) -> UUID:
    teacher_id = await create_user(conn, "Bu Sari")
    await add_membership(conn, school_id, teacher_id, "teacher")
    return teacher_id


async def create_student(
    conn: asyncpg.Connection, school_id: UUID, class_id: UUID, year_id: UUID
) -> UUID:
    student_id = await create_user(conn, "Siswa Uji")
    await add_membership(conn, school_id, student_id, "student")
    await conn.execute(
        "insert into student_profiles (user_id, nisn) values ($1, $2)",
        student_id,
        str(secrets.randbelow(10**10)).zfill(10),
    )
    await conn.execute(
        "insert into class_enrollments (school_id, academic_year_id, class_id, student_id)"
        " values ($1, $2, $3, $4)",
        school_id,
        year_id,
        class_id,
        student_id,
    )
    return student_id


async def link_parent(
    conn: asyncpg.Connection, school_id: UUID, parent_id: UUID, student_id: UUID
) -> None:
    await conn.execute(
        "insert into parent_student_links (parent_id, student_id, school_id) values ($1, $2, $3)",
        parent_id,
        student_id,
        school_id,
    )


async def create_knowledge_base(
    conn: asyncpg.Connection, school_id: UUID, subject_id: UUID, owner_id: UUID
) -> UUID:
    kb_id: UUID = await conn.fetchval(
        "insert into knowledge_bases (school_id, school_subject_id, owner_teacher_id)"
        " values ($1, $2, $3) returning id",
        school_id,
        subject_id,
        owner_id,
    )
    return kb_id


async def create_concept(
    conn: asyncpg.Connection, school_id: UUID, kb_id: UUID, review_status: str = "approved"
) -> UUID:
    concept_id: UUID = await conn.fetchval(
        "insert into concepts"
        " (school_id, knowledge_base_id, name, origin, review_status, reviewed_at)"
        " values ($1, $2, 'Gaya gesek', 'seeded', $3::review_status,"
        " case when $3 = 'pending' then null else now() end) returning id",
        school_id,
        kb_id,
        review_status,
    )
    return concept_id


async def create_misconception(
    conn: asyncpg.Connection,
    school_id: UUID,
    kb_id: UUID,
    concept_id: UUID,
    review_status: str = "pending",
) -> UUID:
    misconception_id: UUID = await conn.fetchval(
        "insert into misconceptions (school_id, knowledge_base_id, concept_id, statement,"
        " correct_understanding, origin, review_status, reviewed_at)"
        " values ($1, $2, $3, 'Gaya bisa habis', 'Benda tetap bergerak tanpa gaya', 'seeded',"
        " $4::review_status, case when $4 = 'pending' then null else now() end) returning id",
        school_id,
        kb_id,
        concept_id,
        review_status,
    )
    return misconception_id


async def create_mission_version(
    conn: asyncpg.Connection,
    school_id: UUID,
    kb_id: UUID,
    creator_id: UUID,
) -> tuple[UUID, UUID]:
    mission_id: UUID = await conn.fetchval(
        "insert into missions (school_id, knowledge_base_id, created_by, title, learning_objective)"
        " values ($1, $2, $3, 'Gaya dan Gerak', 'Menjelaskan gaya gesek') returning id",
        school_id,
        kb_id,
        creator_id,
    )
    version_id: UUID = await conn.fetchval(
        "insert into mission_versions (school_id, mission_id, version_number, anchor_problem,"
        " rubric, probe_plan, created_by)"
        " values ($1, $2, 1, 'Kenapa kelereng berhenti?', $3::jsonb, '{}'::jsonb, $4)"
        " returning id",
        school_id,
        mission_id,
        json.dumps(RUBRIC),
        creator_id,
    )
    return mission_id, version_id


async def publish(
    conn: asyncpg.Connection,
    school_id: UUID,
    version_id: UUID,
    class_id: UUID,
    publisher_id: UUID,
) -> UUID:
    publication_id: UUID = await conn.fetchval(
        "insert into publications (school_id, mission_version_id, class_id, published_by)"
        " values ($1, $2, $3, $4) returning id",
        school_id,
        version_id,
        class_id,
        publisher_id,
    )
    return publication_id


async def create_run(
    conn: asyncpg.Connection,
    school_id: UUID,
    publication_id: UUID,
    mode: str = "live",
    status: str = "scheduled",
    join_code: str | None = None,
) -> UUID:
    opens_at = datetime.now(UTC) if mode == "window" else None
    closes_at = opens_at + timedelta(hours=1) if opens_at else None
    run_id: UUID = await conn.fetchval(
        "insert into publication_runs"
        " (school_id, publication_id, kind, mode, status, join_code, opens_at, closes_at)"
        " values ($1, $2, 'primary', $3::run_mode, $4::run_status, $5, $6, $7) returning id",
        school_id,
        publication_id,
        mode,
        status,
        join_code,
        opens_at,
        closes_at,
    )
    return run_id


async def create_session(
    conn: asyncpg.Connection,
    school_id: UUID,
    publication_id: UUID,
    run_id: UUID,
    student_id: UUID,
) -> UUID:
    session_id: UUID = await conn.fetchval(
        "insert into sessions (school_id, publication_id, run_id, student_id, attempt_number)"
        " values ($1, $2, $3, $4, 1) returning id",
        school_id,
        publication_id,
        run_id,
        student_id,
    )
    return session_id


@dataclass(frozen=True)
class World:
    school_id: UUID
    year_id: UUID
    class_id: UUID
    subject_id: UUID
    teacher_id: UUID
    student_id: UUID
    parent_id: UUID
    admin_id: UUID
    kb_id: UUID
    concept_id: UUID
    mission_id: UUID
    version_id: UUID
    publication_id: UUID
    run_id: UUID
    session_id: UUID


async def build_world(conn: asyncpg.Connection, school_name: str = "SMP Uji") -> World:
    school_id = await create_school(conn, school_name)
    year_id = await create_academic_year(conn, school_id)
    class_id = await create_class(conn, school_id, year_id)
    subject_id = await create_subject(conn, school_id)
    teacher_id = await create_teacher(conn, school_id)
    await assign_teacher(conn, school_id, class_id, subject_id, teacher_id)
    student_id = await create_student(conn, school_id, class_id, year_id)
    parent_id = await create_user(conn, "Orang Tua Uji")
    await link_parent(conn, school_id, parent_id, student_id)
    admin_id = await create_user(conn, "Admin Uji")
    await add_membership(conn, school_id, admin_id, "school_admin")
    kb_id = await create_knowledge_base(conn, school_id, subject_id, teacher_id)
    concept_id = await create_concept(conn, school_id, kb_id)
    mission_id, version_id = await create_mission_version(conn, school_id, kb_id, teacher_id)
    await conn.execute(
        "insert into mission_version_concepts (mission_version_id, concept_id) values ($1, $2)",
        version_id,
        concept_id,
    )
    publication_id = await publish(conn, school_id, version_id, class_id, teacher_id)
    run_id = await create_run(conn, school_id, publication_id, join_code="WORLD1")
    session_id = await create_session(conn, school_id, publication_id, run_id, student_id)
    return World(
        school_id=school_id,
        year_id=year_id,
        class_id=class_id,
        subject_id=subject_id,
        teacher_id=teacher_id,
        student_id=student_id,
        parent_id=parent_id,
        admin_id=admin_id,
        kb_id=kb_id,
        concept_id=concept_id,
        mission_id=mission_id,
        version_id=version_id,
        publication_id=publication_id,
        run_id=run_id,
        session_id=session_id,
    )


async def act_as_authenticated(conn: asyncpg.Connection, user_id: UUID) -> None:
    await conn.execute("set local role authenticated")
    await conn.execute(
        "select set_config('request.jwt.claims', $1, true)",
        json.dumps({"sub": str(user_id), "role": "authenticated"}),
    )
