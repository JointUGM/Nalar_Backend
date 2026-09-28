-- =============================================================================
-- NALAR — Database Schema (Supabase / PostgreSQL 15+)
-- Derived from the NALAR Use Case Diagram and product decisions.
--
-- Design principles
--   1. Multi-tenant from day one: every school-owned row carries school_id,
--      and Row Level Security enforces tenant isolation in the database.
--   2. Tenant integrity by construction: core parent/child links use
--      composite foreign keys (id, school_id) so a row can never point
--      at another school's data, even through an application bug.
--   3. History is immutable where results depend on it: mission versions
--      lock on publication; evaluations keep AI score and teacher override
--      separately; overrides are logged.
--   4. Business rules live in the database where possible (RLS policies),
--      not only in application code.
-- =============================================================================

create extension if not exists vector;

-- =============================================================================
-- 0. ENUMS
-- =============================================================================
create type membership_role     as enum ('school_admin', 'teacher', 'student');
create type membership_status   as enum ('active', 'inactive');
create type activation_channel  as enum ('email', 'printed_slip');
create type import_status       as enum ('pending', 'processing', 'completed', 'failed');
create type cp_version_status   as enum ('draft', 'published', 'superseded');
create type kb_status           as enum ('draft', 'generating', 'pending_review', 'approved');
create type content_origin      as enum ('ai_generated', 'teacher', 'seeded');
create type material_status     as enum ('uploaded', 'processing', 'processed', 'failed');
create type ai_purpose          as enum ('knowledge_base_generation', 'mission_generation',
                                         'probe_generation', 'session_evaluation',
                                         'reflection_generation', 'class_map_insight',
                                         'follow_up_suggestion', 'parent_summary');
create type ai_call_status      as enum ('succeeded', 'failed');
create type run_kind            as enum ('primary', 'grant');
create type run_mode            as enum ('window', 'live');
create type run_status          as enum ('scheduled', 'open', 'closed');
create type session_status      as enum ('in_progress', 'completed', 'timed_out');
create type session_end_reason  as enum ('student_completed', 'max_turns_reached',
                                         'max_duration_reached', 'run_closed_grace_expired');
create type prompt_kind         as enum ('anchor', 'probe');
create type probe_strategy      as enum ('request_justification', 'counter_example',
                                         'vary_variable', 'transfer', 'decompose',
                                         'refuse_and_redirect');
create type answer_state        as enum ('correct_reasoned', 'correct_unreasoned',
                                         'misconception', 'evasive', 'manipulation_attempt');
create type rubric_dimension    as enum ('claim', 'evidence', 'mechanism', 'transfer');
create type concept_outcome     as enum ('mastered', 'developing', 'misconception', 'not_observed');
create type flag_type           as enum ('large_paste', 'tab_switching', 'inconsistency_gap',
                                         'style_shift', 'cross_student_similarity',
                                         'disconnect_pattern');
create type flag_severity       as enum ('low', 'medium', 'high');
create type flag_status         as enum ('open', 'cleared', 'concern_confirmed');
create type notification_type   as enum ('account_invitation', 'release_reminder',
                                         'parent_periodic_summary');
create type notification_status as enum ('pending', 'sent', 'failed');

-- =============================================================================
-- 1. PLATFORM, IDENTITY, MEMBERSHIP
-- =============================================================================

-- PL-1, PL-4. Platform Admin sees rows in this table only (metadata).
create table schools (
    id             uuid primary key default gen_random_uuid(),
    npsn           text unique,                      -- national school number
    name           text not null,
    is_active      boolean not null default true,    -- PL-4 suspend
    onboarded_by   uuid,                             -- platform admin profile
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

-- Generalized User. id = auth.users.id (Supabase Auth owns credentials).
-- A person can hold several roles: e.g. a teacher who is also a parent.
create table profiles (
    id                    uuid primary key references auth.users(id) on delete cascade,
    full_name             text not null,
    contact_email         text,              -- null for parents without email
    has_real_email        boolean not null default true,  -- false => synthetic login id
    is_platform_admin     boolean not null default false,
    must_change_password  boolean not null default false,  -- set for printed-slip logins
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now()
);
alter table schools add constraint schools_onboarded_by_fkey
    foreign key (onboarded_by) references profiles(id);

-- School Admin, Teacher, Student roles are school-scoped.
-- Teachers may belong to several schools (Permendikdasmen 11/2025).
create table school_memberships (
    id               uuid primary key default gen_random_uuid(),
    school_id        uuid not null references schools(id),
    user_id          uuid not null references profiles(id),
    role             membership_role not null,
    status           membership_status not null default 'active',   -- SA-4
    joined_at        timestamptz not null default now(),
    deactivated_at   timestamptz,
    unique (school_id, user_id, role)
);

-- Stable natural key for students across years and schools: NISN.
create table student_profiles (
    user_id   uuid primary key references profiles(id) on delete cascade,
    nisn      text not null unique
);

-- Parents are not school members; access flows through their children.
-- Links are created by roster import (matched by parent email) or SA-2.
create table parent_student_links (
    id              uuid primary key default gen_random_uuid(),
    parent_id       uuid not null references profiles(id),
    student_id      uuid not null references student_profiles(user_id),
    school_id       uuid not null references schools(id),   -- school that created the link
    relationship    text,                                    -- ayah / ibu / wali
    created_at      timestamptz not null default now(),
    unique (parent_id, student_id)
);

-- U-1 activation. Email invites are sent by Supabase Auth; printed slips
-- carry a one-time code whose hash is stored here (SA-1 extend, SA-3 extend).
create table account_activations (
    id             uuid primary key default gen_random_uuid(),
    user_id        uuid not null references profiles(id),
    school_id      uuid not null references schools(id),
    channel        activation_channel not null,
    code_hash      text,                                   -- printed_slip only
    issued_by      uuid references profiles(id),
    expires_at     timestamptz not null,
    consumed_at    timestamptz,
    created_at     timestamptz not null default now(),
    check (channel <> 'printed_slip' or code_hash is not null)
);

-- =============================================================================
-- 2. ACADEMIC STRUCTURE (School Admin: people and classes)
-- =============================================================================

-- SA-5. Classes are scoped to an academic year so history never mixes.
create table academic_years (
    id          uuid primary key default gen_random_uuid(),
    school_id   uuid not null references schools(id),
    label       text not null,                  -- '2026/2027'
    starts_on   date not null,
    ends_on     date not null,
    is_current  boolean not null default false,
    created_at  timestamptz not null default now(),
    unique (school_id, label),
    unique (id, school_id),
    check (ends_on > starts_on)
);
create unique index academic_years_one_current
    on academic_years (school_id) where is_current;

-- SA-1 roster file upload (stored in a Supabase Storage bucket).
create table roster_imports (
    id                uuid primary key default gen_random_uuid(),
    school_id         uuid not null references schools(id),
    academic_year_id  uuid not null,
    uploaded_by       uuid not null references profiles(id),
    storage_bucket    text not null default 'roster-imports',
    storage_path      text not null,
    status            import_status not null default 'pending',
    rows_total        integer,
    rows_succeeded    integer,
    rows_failed       integer,
    created_at        timestamptz not null default now(),
    completed_at      timestamptz,
    foreign key (academic_year_id, school_id) references academic_years(id, school_id)
);

create table roster_import_errors (
    id           uuid primary key default gen_random_uuid(),
    import_id    uuid not null references roster_imports(id) on delete cascade,
    row_number   integer not null,
    field        text,
    message      text not null
);

-- SA-6
create table classes (
    id                uuid primary key default gen_random_uuid(),
    school_id         uuid not null,
    academic_year_id  uuid not null,
    name              text not null,                          -- '8A'
    grade_level       smallint not null check (grade_level between 1 and 12),
    archived_at       timestamptz,
    created_at        timestamptz not null default now(),
    foreign key (academic_year_id, school_id) references academic_years(id, school_id),
    unique (academic_year_id, name),
    unique (id, school_id),
    unique (id, school_id, academic_year_id)
);

-- SA-1 / SA-2 placement. One active class per student per academic year.
create table class_enrollments (
    id                uuid primary key default gen_random_uuid(),
    school_id         uuid not null,
    academic_year_id  uuid not null,
    class_id          uuid not null,
    student_id        uuid not null references student_profiles(user_id),
    status            membership_status not null default 'active',
    enrolled_at       timestamptz not null default now(),
    ended_at          timestamptz,
    foreign key (class_id, school_id, academic_year_id)
        references classes(id, school_id, academic_year_id)
);
create unique index class_enrollments_one_active_per_year
    on class_enrollments (student_id, academic_year_id) where status = 'active';

-- =============================================================================
-- 3. NATIONAL CURRICULUM REFERENCE (Platform Admin, read-only for schools)
--    Versioned: BSKAP 046/2025 already replaced 032/2024 once.
-- =============================================================================
create table cp_versions (
    id            uuid primary key default gen_random_uuid(),
    decree_code   text not null unique,           -- '046/H/KR/2025'
    title         text not null,
    effective_on  date not null,
    status        cp_version_status not null default 'draft',
    published_at  timestamptz,
    created_at    timestamptz not null default now()
);

create table cp_subjects (
    id             uuid primary key default gen_random_uuid(),
    cp_version_id  uuid not null references cp_versions(id),
    name           text not null,                 -- 'Ilmu Pengetahuan Alam'
    phase          text not null,                 -- 'D' = SMP
    unique (cp_version_id, name, phase)
);

create table cp_learning_outcomes (
    id              uuid primary key default gen_random_uuid(),
    cp_subject_id   uuid not null references cp_subjects(id),
    element         text,                          -- CP element name
    description     text not null,
    ordinal         integer not null default 0,
    embedding       vector(1536)                   -- RAG grounding
);

-- SA-7. School-defined subject names, mapped once to a specific CP version.
create table school_subjects (
    id             uuid primary key default gen_random_uuid(),
    school_id      uuid not null references schools(id),
    name           text not null,                  -- 'IPA Terpadu'
    cp_subject_id  uuid references cp_subjects(id),   -- null until mapped
    created_at     timestamptz not null default now(),
    unique (school_id, name),
    unique (id, school_id)
);

-- SA-8. The source of "which teacher handles which student" (see view).
create table teaching_assignments (
    id                 uuid primary key default gen_random_uuid(),
    school_id          uuid not null,
    class_id           uuid not null,
    school_subject_id  uuid not null,
    teacher_id         uuid not null references profiles(id),
    created_at         timestamptz not null default now(),
    foreign key (class_id, school_id) references classes(id, school_id),
    foreign key (school_subject_id, school_id) references school_subjects(id, school_id),
    unique (class_id, school_subject_id, teacher_id)
);

-- =============================================================================
-- 4. AI PROVENANCE
--    Every AI output points here: model, prompt version, tokens, cost.
--    Enables auditability and the "cost per session" figure.
-- =============================================================================
create table ai_invocations (
    id              uuid primary key default gen_random_uuid(),
    school_id       uuid references schools(id),
    purpose         ai_purpose not null,
    model           text not null,
    prompt_version  text not null,
    status          ai_call_status not null,
    input_tokens    integer,
    output_tokens   integer,
    latency_ms      integer,
    cost_usd        numeric(12, 6),
    error_message   text,
    created_at      timestamptz not null default now()
);

-- =============================================================================
-- 5. KNOWLEDGE BASE (one per subject per school; owner teacher)
-- =============================================================================

-- TC-1, TC-2, TC-3, SA-9
create table knowledge_bases (
    id                 uuid primary key default gen_random_uuid(),
    school_id          uuid not null,
    school_subject_id  uuid not null unique,         -- one KB per school subject
    owner_teacher_id   uuid not null references profiles(id),
    status             kb_status not null default 'draft',
    approved_by        uuid references profiles(id),
    approved_at        timestamptz,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    foreign key (school_subject_id, school_id) references school_subjects(id, school_id),
    unique (id, school_id),
    check (status <> 'approved' or approved_at is not null)
);

-- Files live in Supabase Storage; the row holds the path.
create table teaching_materials (
    id                 uuid primary key default gen_random_uuid(),
    school_id          uuid not null,
    knowledge_base_id  uuid not null,
    uploaded_by        uuid not null references profiles(id),
    title              text not null,
    storage_bucket     text not null default 'teaching-materials',
    storage_path       text not null,
    mime_type          text not null,
    size_bytes         bigint,
    processing_status  material_status not null default 'uploaded',
    created_at         timestamptz not null default now(),
    foreign key (knowledge_base_id, school_id) references knowledge_bases(id, school_id),
    unique (storage_bucket, storage_path)
);

-- RAG chunks with pgvector embeddings.
create table material_chunks (
    id                 uuid primary key default gen_random_uuid(),
    school_id          uuid not null references schools(id),
    material_id        uuid not null references teaching_materials(id) on delete cascade,
    knowledge_base_id  uuid not null references knowledge_bases(id),
    chunk_index        integer not null,
    content            text not null,
    token_count        integer,
    embedding          vector(1536),
    unique (material_id, chunk_index)
);

-- Concept graph nodes. Soft-deleted only: locked mission versions reference them.
create table concepts (
    id                      uuid primary key default gen_random_uuid(),
    school_id               uuid not null references schools(id),
    knowledge_base_id       uuid not null references knowledge_bases(id),
    name                    text not null,
    description             text,
    cp_learning_outcome_id  uuid references cp_learning_outcomes(id),
    origin                  content_origin not null,
    archived_at             timestamptz,
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now(),
    unique (id, knowledge_base_id)
);

-- Concept graph edges: both ends must belong to the same knowledge base.
create table concept_prerequisites (
    knowledge_base_id         uuid not null,
    concept_id                uuid not null,
    prerequisite_concept_id   uuid not null,
    primary key (concept_id, prerequisite_concept_id),
    foreign key (concept_id, knowledge_base_id)
        references concepts(id, knowledge_base_id),
    foreign key (prerequisite_concept_id, knowledge_base_id)
        references concepts(id, knowledge_base_id),
    check (concept_id <> prerequisite_concept_id)
);

-- Misconception bank, attached to a concept.
create table misconceptions (
    id                     uuid primary key default gen_random_uuid(),
    school_id              uuid not null references schools(id),
    knowledge_base_id      uuid not null,
    concept_id             uuid not null,
    statement              text not null,        -- 'Force is needed to keep motion'
    correct_understanding  text not null,
    origin                 content_origin not null,
    archived_at            timestamptz,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    foreign key (concept_id, knowledge_base_id) references concepts(id, knowledge_base_id)
);

-- =============================================================================
-- 6. MISSIONS (reusable) and MISSION VERSIONS (immutable once published)
-- =============================================================================

-- TC-4
create table missions (
    id                  uuid primary key default gen_random_uuid(),
    school_id           uuid not null,
    knowledge_base_id   uuid not null,
    created_by          uuid not null references profiles(id),
    title               text not null,
    learning_objective  text not null,          -- Tujuan Pembelajaran
    archived_at         timestamptz,
    created_at          timestamptz not null default now(),
    foreign key (knowledge_base_id, school_id) references knowledge_bases(id, school_id),
    unique (id, school_id)
);

-- TC-5 edits create a new version. locked_at is set on first publication.
create table mission_versions (
    id                     uuid primary key default gen_random_uuid(),
    school_id              uuid not null,
    mission_id             uuid not null,
    version_number         integer not null,
    anchor_problem         text not null,
    rubric                 jsonb not null,       -- descriptors for 4 dimensions x levels 0..4
    probe_plan             jsonb not null,       -- state -> strategy -> probe templates
    max_turns              smallint not null default 6 check (max_turns between 2 and 12),
    max_duration_minutes   smallint not null default 15 check (max_duration_minutes between 5 and 60),
    created_by             uuid not null references profiles(id),
    ai_invocation_id       uuid references ai_invocations(id),
    locked_at              timestamptz,
    created_at             timestamptz not null default now(),
    foreign key (mission_id, school_id) references missions(id, school_id),
    unique (mission_id, version_number),
    unique (id, school_id),
    check (rubric ?& array['claim', 'evidence', 'mechanism', 'transfer'])
);

create table mission_version_concepts (
    mission_version_id  uuid not null references mission_versions(id) on delete cascade,
    concept_id          uuid not null references concepts(id),
    primary key (mission_version_id, concept_id)
);

create table mission_version_misconceptions (
    mission_version_id  uuid not null references mission_versions(id) on delete cascade,
    misconception_id    uuid not null references misconceptions(id),
    primary key (mission_version_id, misconception_id)
);

-- =============================================================================
-- 7. PUBLICATIONS and RUNS
--    A publication = one mission version sent to one class.
--    A run = one opening of it: the class-wide primary run, or a grant run
--    for one student (TC-17). Each run is either a window or a live session.
-- =============================================================================

-- TC-6, TC-18
create table publications (
    id                        uuid primary key default gen_random_uuid(),
    school_id                 uuid not null,
    mission_version_id        uuid not null,
    class_id                  uuid not null,
    published_by              uuid not null references profiles(id),
    released_to_parents_at    timestamptz,       -- TC-18 release gate
    released_by               uuid references profiles(id),
    cancelled_at              timestamptz,
    created_at                timestamptz not null default now(),
    foreign key (mission_version_id, school_id) references mission_versions(id, school_id),
    foreign key (class_id, school_id) references classes(id, school_id),
    unique (id, school_id),
    check ((released_to_parents_at is null) = (released_by is null))
);

-- TC-7, TC-17, and Scheduler "close scheduled window"
create table publication_runs (
    id                uuid primary key default gen_random_uuid(),
    school_id         uuid not null,
    publication_id    uuid not null,
    kind              run_kind not null,
    mode              run_mode not null,
    status            run_status not null default 'scheduled',
    opens_at          timestamptz,             -- window mode
    closes_at         timestamptz,             -- window mode
    started_at        timestamptz,             -- live mode: teacher pressed Start
    closed_at         timestamptz,             -- when the run actually closed
    join_code         text,                    -- live mode, shown on projector
    grant_student_id  uuid references student_profiles(user_id),
    granted_by        uuid references profiles(id),
    grant_reason      text,
    created_at        timestamptz not null default now(),
    foreign key (publication_id, school_id) references publications(id, school_id),
    unique (id, school_id),
    check ((kind = 'grant') = (grant_student_id is not null)),
    check ((kind = 'grant') = (granted_by is not null)),
    check (mode <> 'window' or (opens_at is not null and closes_at is not null and closes_at > opens_at)),
    check (mode <> 'live' or join_code is not null)
);
create unique index publication_runs_one_primary
    on publication_runs (publication_id) where kind = 'primary';
create unique index publication_runs_open_join_code
    on publication_runs (join_code) where status = 'open' and join_code is not null;

-- =============================================================================
-- 8. SESSIONS, TURNS, TELEMETRY
-- =============================================================================

-- ST-2, ST-3, ST-4, ST-5. One attempt by default; each grant allows one more.
create table sessions (
    id                uuid primary key default gen_random_uuid(),
    school_id         uuid not null,
    publication_id    uuid not null,
    run_id            uuid not null,
    student_id        uuid not null references student_profiles(user_id),
    attempt_number    smallint not null check (attempt_number >= 1),
    status            session_status not null default 'in_progress',
    end_reason        session_end_reason,
    started_at        timestamptz not null default now(),
    last_activity_at  timestamptz not null default now(),
    ended_at          timestamptz,
    disconnect_count  integer not null default 0,   -- ST-5 resume signal
    foreign key (publication_id, school_id) references publications(id, school_id),
    foreign key (run_id, school_id) references publication_runs(id, school_id),
    unique (publication_id, student_id, attempt_number),
    unique (id, school_id),
    check ((status = 'in_progress') = (ended_at is null)),
    check ((status = 'in_progress') = (end_reason is null))
);
create index sessions_student_idx on sessions (student_id, started_at desc);

-- One row = one prompt shown (anchor or probe) + the student's answer to it.
create table session_turns (
    id                          uuid primary key default gen_random_uuid(),
    school_id                   uuid not null,
    session_id                  uuid not null,
    turn_index                  smallint not null check (turn_index >= 0),
    prompt_kind                 prompt_kind not null,
    prompt_strategy             probe_strategy,      -- null for the anchor
    prompt_text                 text not null,
    prompt_shown_at             timestamptz not null default now(),
    prompt_ai_invocation_id     uuid references ai_invocations(id),
    answer_text                 text,
    answer_submitted_at         timestamptz,
    answer_state                answer_state,        -- classifier output
    detected_misconception_id   uuid references misconceptions(id),
    foreign key (session_id, school_id) references sessions(id, school_id) on delete cascade,
    unique (session_id, turn_index),
    check ((prompt_kind = 'anchor') = (prompt_strategy is null))
);

-- Raw client events, batched to save bandwidth. client_seq makes retries
-- idempotent after a reconnect. Candidate for monthly partitioning at scale.
create table telemetry_batches (
    id              uuid primary key default gen_random_uuid(),
    school_id       uuid not null,
    session_id      uuid not null,
    turn_id         uuid references session_turns(id) on delete cascade,
    client_seq      integer not null,
    client_sent_at  timestamptz,
    received_at     timestamptz not null default now(),
    events          jsonb not null,
    foreign key (session_id, school_id) references sessions(id, school_id) on delete cascade,
    unique (session_id, client_seq)
);

-- Derived per-turn signals used by the authenticity flags.
create table turn_metrics (
    turn_id              uuid primary key references session_turns(id) on delete cascade,
    school_id            uuid not null references schools(id),
    think_time_ms        integer,       -- prompt shown -> first keystroke
    typing_duration_ms   integer,
    chars_typed          integer,
    chars_pasted         integer,
    paste_events         integer,
    revision_count       integer,
    tab_hidden_events    integer,
    tab_hidden_ms        integer,
    disconnect_events    integer,
    computed_at          timestamptz not null default now()
);

-- =============================================================================
-- 9. EVALUATION, EVIDENCE, OVERRIDES, FLAGS, REFLECTIONS
-- =============================================================================

create table session_evaluations (
    id                uuid primary key default gen_random_uuid(),
    school_id         uuid not null,
    session_id        uuid not null unique,
    ai_invocation_id  uuid references ai_invocations(id),
    summary           text,
    evaluated_at      timestamptz not null default now(),
    foreign key (session_id, school_id) references sessions(id, school_id) on delete cascade
);

-- AI level and teacher's final level are stored separately (TC-12).
create table evaluation_scores (
    id             uuid primary key default gen_random_uuid(),
    school_id      uuid not null references schools(id),
    evaluation_id  uuid not null references session_evaluations(id) on delete cascade,
    dimension      rubric_dimension not null,
    ai_level       smallint not null check (ai_level between 0 and 4),
    final_level    smallint not null check (final_level between 0 and 4),
    rationale      text,
    unique (evaluation_id, dimension)
);

-- Every score cites the turns that justify it (quoted evidence).
create table score_evidence (
    id         uuid primary key default gen_random_uuid(),
    school_id  uuid not null references schools(id),
    score_id   uuid not null references evaluation_scores(id) on delete cascade,
    turn_id    uuid not null references session_turns(id),
    quote      text not null
);

-- TC-12 audit trail.
create table score_overrides (
    id              uuid primary key default gen_random_uuid(),
    school_id       uuid not null references schools(id),
    score_id        uuid not null references evaluation_scores(id) on delete cascade,
    overridden_by   uuid not null references profiles(id),
    previous_level  smallint not null check (previous_level between 0 and 4),
    new_level       smallint not null check (new_level between 0 and 4),
    reason          text not null,
    created_at      timestamptz not null default now(),
    check (previous_level <> new_level)
);

-- Per-session concept outcomes. The class map counts these (system counts).
create table session_concept_results (
    id                 uuid primary key default gen_random_uuid(),
    school_id          uuid not null references schools(id),
    session_id         uuid not null references sessions(id) on delete cascade,
    concept_id         uuid not null references concepts(id),
    outcome            concept_outcome not null,
    misconception_id   uuid references misconceptions(id),
    evidence_turn_id   uuid references session_turns(id),
    unique (session_id, concept_id),
    check ((outcome = 'misconception') = (misconception_id is not null))
);

-- TC-11. "Needs verification", never a verdict.
create table authenticity_flags (
    id            uuid primary key default gen_random_uuid(),
    school_id     uuid not null references schools(id),
    session_id    uuid not null references sessions(id) on delete cascade,
    turn_id       uuid references session_turns(id),
    flag_type     flag_type not null,
    severity      flag_severity not null,
    evidence      jsonb not null,     -- e.g. related session ids for similarity
    status        flag_status not null default 'open',
    reviewed_by   uuid references profiles(id),
    reviewed_at   timestamptz,
    review_note   text,
    created_at    timestamptz not null default now(),
    check ((status = 'open') = (reviewed_at is null))
);

-- ST-6. Generated once at session end, stored, timestamped.
create table session_reflections (
    id                uuid primary key default gen_random_uuid(),
    school_id         uuid not null references schools(id),
    session_id        uuid not null unique references sessions(id) on delete cascade,
    content           text not null,
    ai_invocation_id  uuid references ai_invocations(id),
    generated_at      timestamptz not null default now()
);

-- =============================================================================
-- 10. CLASS INSIGHTS and PARENT SUMMARIES
-- =============================================================================

-- TC-9. Counts are computed by SQL (see view); AI stores only interpretation.
-- A new row is written when the map is regenerated; the latest is shown.
create table class_map_insights (
    id                uuid primary key default gen_random_uuid(),
    school_id         uuid not null,
    publication_id    uuid not null,
    counts_snapshot   jsonb not null,    -- exact counts the AI was given
    clusters          jsonb not null,    -- AI grouping and naming
    narrative         text not null,
    ai_invocation_id  uuid references ai_invocations(id),
    generated_at      timestamptz not null default now(),
    foreign key (publication_id, school_id) references publications(id, school_id)
);

-- TC-13
create table follow_up_suggestions (
    id          uuid primary key default gen_random_uuid(),
    school_id   uuid not null references schools(id),
    insight_id  uuid not null references class_map_insights(id) on delete cascade,
    rank        smallint not null,
    content     text not null,
    unique (insight_id, rank)
);

-- TC-18 generates these at release; teacher previews; stored once.
create table parent_summaries (
    id                uuid primary key default gen_random_uuid(),
    school_id         uuid not null,
    publication_id    uuid not null,
    student_id        uuid not null references student_profiles(user_id),
    content           text not null,
    ai_invocation_id  uuid references ai_invocations(id),
    generated_at      timestamptz not null default now(),
    foreign key (publication_id, school_id) references publications(id, school_id),
    unique (publication_id, student_id)
);

-- =============================================================================
-- 11. OPERATIONS: NOTIFICATIONS and AUDIT
-- =============================================================================

-- Invitations, release reminders, periodic parent summaries (Scheduler).
-- dedupe_key makes scheduled jobs safe to re-run.
create table notifications (
    id                 uuid primary key default gen_random_uuid(),
    recipient_id       uuid not null references profiles(id),
    school_id          uuid references schools(id),
    type               notification_type not null,
    payload            jsonb not null default '{}'::jsonb,
    scheduled_for      timestamptz not null default now(),
    sent_at            timestamptz,
    status             notification_status not null default 'pending',
    dedupe_key         text not null unique,
    created_at         timestamptz not null default now()
);

create table audit_logs (
    id             bigint generated always as identity primary key,
    school_id      uuid references schools(id),
    actor_id       uuid references profiles(id),
    action         text not null,          -- 'kb.owner_reassigned', 'score.overridden'
    entity_table   text not null,
    entity_id      uuid,
    changes        jsonb,
    created_at     timestamptz not null default now()
);

-- =============================================================================
-- 12. INDEXES (foreign keys used by RLS and dashboards)
-- =============================================================================
create index school_memberships_user_idx      on school_memberships (user_id);
create index parent_links_student_idx         on parent_student_links (student_id);
create index class_enrollments_class_idx      on class_enrollments (class_id);
create index teaching_assignments_teacher_idx on teaching_assignments (teacher_id);
create index publications_class_idx           on publications (class_id);
create index sessions_publication_idx         on sessions (publication_id);
create index session_turns_session_idx        on session_turns (session_id);
create index concept_results_session_idx      on session_concept_results (session_id);
create index flags_session_idx                on authenticity_flags (session_id) where status = 'open';
create index notifications_due_idx            on notifications (scheduled_for) where status = 'pending';
create index material_chunks_embedding_idx
    on material_chunks using hnsw (embedding vector_cosine_ops);

-- =============================================================================
-- 13. INTEGRITY TRIGGERS
-- =============================================================================
create or replace function set_updated_at() returns trigger
language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end $$;

create trigger schools_updated_at         before update on schools         for each row execute function set_updated_at();
create trigger profiles_updated_at        before update on profiles        for each row execute function set_updated_at();
create trigger knowledge_bases_updated_at before update on knowledge_bases for each row execute function set_updated_at();
create trigger concepts_updated_at        before update on concepts        for each row execute function set_updated_at();
create trigger misconceptions_updated_at  before update on misconceptions  for each row execute function set_updated_at();

-- A published mission version can never change (decision: publications lock
-- the version; edits create a new version).
create or replace function prevent_locked_version_update() returns trigger
language plpgsql as $$
begin
    if old.locked_at is not null then
        raise exception 'mission_version % is locked (published); create a new version instead', old.id;
    end if;
    return new;
end $$;

create trigger mission_versions_immutable
    before update on mission_versions
    for each row execute function prevent_locked_version_update();

create or replace function lock_version_on_publish() returns trigger
language plpgsql as $$
begin
    update mission_versions
       set locked_at = now()
     where id = new.mission_version_id and locked_at is null;
    return new;
end $$;

create trigger publications_lock_version
    after insert on publications
    for each row execute function lock_version_on_publish();

-- Keep final_level honest: overriding writes the log row; the log row
-- updates final_level. The two can never disagree.
create or replace function apply_score_override() returns trigger
language plpgsql as $$
begin
    update evaluation_scores set final_level = new.new_level where id = new.score_id;
    return new;
end $$;

create trigger score_overrides_apply
    after insert on score_overrides
    for each row execute function apply_score_override();

-- =============================================================================
-- 14. VIEWS
-- =============================================================================

-- Original requirement: which students each teacher handles, and vice versa.
-- Derived, so it can never drift out of sync.
create view v_teacher_students with (security_invoker = true) as
select ta.school_id,
       ta.teacher_id,
       ce.student_id,
       ta.class_id,
       ta.school_subject_id,
       ce.academic_year_id
  from teaching_assignments ta
  join class_enrollments ce
    on ce.class_id = ta.class_id and ce.status = 'active';

-- Latest finished attempt per student per publication (it counts in the map).
create view v_latest_sessions with (security_invoker = true) as
select distinct on (s.publication_id, s.student_id) s.*
  from sessions s
 where s.status <> 'in_progress'
 order by s.publication_id, s.student_id, s.attempt_number desc;

-- "System counts": exact numbers for the class misconception map.
create view v_class_concept_counts with (security_invoker = true) as
select ls.school_id,
       ls.publication_id,
       r.concept_id,
       r.outcome,
       r.misconception_id,
       count(*)::integer as student_count
  from v_latest_sessions ls
  join session_concept_results r on r.session_id = ls.id
 group by ls.school_id, ls.publication_id, r.concept_id, r.outcome, r.misconception_id;

-- Cumulative view per student per concept: the most recent observation wins.
create view v_student_concept_mastery with (security_invoker = true) as
select distinct on (s.student_id, r.concept_id)
       s.school_id, s.student_id, r.concept_id, r.outcome, r.misconception_id,
       s.ended_at as observed_at
  from session_concept_results r
  join sessions s on s.id = r.session_id
 where r.outcome <> 'not_observed'
 order by s.student_id, r.concept_id, s.ended_at desc;

-- =============================================================================
-- 15. ROW LEVEL SECURITY
--    Default deny on every table. Backend jobs (AI pipeline, Scheduler) use
--    the service role, which bypasses RLS. The policies below encode the
--    business rules we agreed for direct client reads.
-- =============================================================================
do $$
declare t text;
begin
    for t in select tablename from pg_tables where schemaname = 'public' loop
        execute format('alter table %I enable row level security', t);
    end loop;
end $$;

-- Helper functions (security definer so they can read memberships under RLS).
create or replace function is_platform_admin() returns boolean
language sql stable security definer set search_path = public as $$
    select coalesce((select is_platform_admin from profiles where id = auth.uid()), false);
$$;

create or replace function has_school_role(p_school uuid, p_roles membership_role[])
returns boolean language sql stable security definer set search_path = public as $$
    select exists (
        select 1 from school_memberships
         where school_id = p_school and user_id = auth.uid()
           and status = 'active' and role = any (p_roles));
$$;

create or replace function teaches_publication(p_publication uuid)
returns boolean language sql stable security definer set search_path = public as $$
    select exists (
        select 1
          from publications p
          join teaching_assignments ta on ta.class_id = p.class_id
         where p.id = p_publication and ta.teacher_id = auth.uid());
$$;

-- Parent may read a child's results only after the teacher released them.
create or replace function parent_can_see_session(p_session uuid)
returns boolean language sql stable security definer set search_path = public as $$
    select exists (
        select 1
          from sessions s
          join publications p on p.id = s.publication_id
          join parent_student_links l on l.student_id = s.student_id
         where s.id = p_session
           and l.parent_id = auth.uid()
           and p.released_to_parents_at is not null);
$$;

-- Same release gate, keyed by publication + student (used by parent_summaries).
-- Security definer is required: a policy that queries another RLS-protected
-- table (publications) would otherwise see nothing and silently deny.
create or replace function parent_can_see_publication(p_publication uuid, p_student uuid)
returns boolean language sql stable security definer set search_path = public as $$
    select exists (
        select 1
          from publications p
          join parent_student_links l on l.student_id = p_student
         where p.id = p_publication
           and l.parent_id = auth.uid()
           and p.released_to_parents_at is not null);
$$;

-- Schools: members read their school; Platform Admin reads metadata of all.
create policy schools_read on schools for select to authenticated
    using (is_platform_admin()
           or has_school_role(id, array['school_admin','teacher','student']::membership_role[]));

-- Profiles: everyone reads their own profile.
create policy profiles_self on profiles for select to authenticated
    using (id = auth.uid());

-- Academic structure: readable by members of the school; writable by School Admin.
create policy classes_read on classes for select to authenticated
    using (has_school_role(school_id, array['school_admin','teacher','student']::membership_role[]));
create policy classes_admin_write on classes for all to authenticated
    using (has_school_role(school_id, array['school_admin']::membership_role[]))
    with check (has_school_role(school_id, array['school_admin']::membership_role[]));

-- Sessions: the student's own, the class teacher's, or a parent after release.
-- Platform Admin has no policy here: it can never read student data.
create policy sessions_read on sessions for select to authenticated
    using (student_id = auth.uid()
           or teaches_publication(publication_id)
           or parent_can_see_session(id));

-- Reflections: student (own), teacher, parent after release.
create policy reflections_read on session_reflections for select to authenticated
    using (exists (select 1 from sessions s where s.id = session_id
                   and (s.student_id = auth.uid()
                        or teaches_publication(s.publication_id)
                        or parent_can_see_session(s.id))));

-- Scores, evidence, flags: TEACHERS ONLY. Students never see scores,
-- parents never see scores or authenticity flags. Enforced here.
create policy evaluations_teacher_read on session_evaluations for select to authenticated
    using (exists (select 1 from sessions s where s.id = session_id
                   and teaches_publication(s.publication_id)));
create policy scores_teacher_read on evaluation_scores for select to authenticated
    using (exists (select 1 from session_evaluations e join sessions s on s.id = e.session_id
                   where e.id = evaluation_id and teaches_publication(s.publication_id)));
create policy flags_teacher_read on authenticity_flags for select to authenticated
    using (exists (select 1 from sessions s where s.id = session_id
                   and teaches_publication(s.publication_id)));

-- Parent summaries: parents of the child after release, and teachers (preview).
create policy parent_summaries_read on parent_summaries for select to authenticated
    using (teaches_publication(publication_id)
           or parent_can_see_publication(publication_id, student_id));
