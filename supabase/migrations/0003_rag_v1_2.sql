-- =============================================================================
-- NALAR — Migration 003: retrieval (RAG) hardening, schema v1.2
-- Run after NALAR-Schema.sql and NALAR-Migration-002-ai-v1_1.sql.
-- Safe to re-run: every step checks before it changes anything.
--
-- What it does (AI System Design v2.4, section 6):
--   D3  cp_learning_outcomes: statement-level rows (grain, parent_id), embedding_model
--   D4  material_sections (new): chapters found in an upload; S1 builds chapter by chapter
--   D4  material_chunks: page range, heading path, chunk kind, section, embedding_model;
--       composite tenant keys; exact per-KB search index; chunks can't be deleted
--       while an idea or a mission points at them; materials are archived, not deleted
--   D4  concepts, misconceptions: per-item review (AI-3 at item level), embeddings for
--       duplicate checks, composite tenant keys, source_chunk_ids validated by the database
--   D4  misconception_library (new): platform-level, research-sourced wrong ideas (cabinet 3)
--   D5  mission_versions.source_chunk_ids: which teacher paragraphs grounded a mission;
--       mission items must be approved and from the mission's own knowledge base
--   D9  ai_invocations.retrieval: what each AI call was given, for tracing and tests
--   Retrieval functions (service role only): match_kb_chunks, get_kb_chunks,
--       match_kb_concepts, match_cp_outcomes, match_misconception_library
--   Teacher read policies on the knowledge base tables (owner + teachers of the subject)
-- =============================================================================

-- ---------------------------------------------------------------------
-- 0. Preconditions: migration 002 must already be applied
-- ---------------------------------------------------------------------
do $$
begin
  if not exists (select 1 from information_schema.columns
                 where table_schema = 'public' and table_name = 'misconceptions'
                   and column_name = 'source_chunk_ids') then
    raise exception 'Migration 003 needs migration 002 (misconceptions.source_chunk_ids is missing).';
  end if;
  if not exists (select 1 from pg_proc where proname = 'is_active_teacher_in_school') then
    raise exception 'Migration 003 needs migration 002 (is_active_teacher_in_school is missing).';
  end if;
end $$;

-- ---------------------------------------------------------------------
-- 1. Enums
-- ---------------------------------------------------------------------
do $$ begin
  if not exists (select 1 from pg_type where typname = 'review_status') then
    create type review_status as enum ('pending', 'approved', 'rejected');
  end if;
  if not exists (select 1 from pg_type where typname = 'chunk_kind') then
    create type chunk_kind as enum ('explanation', 'example', 'activity', 'exercise',
                                    'answer_key', 'summary', 'sidebar', 'other');
  end if;
  if not exists (select 1 from pg_type where typname = 'section_build_status') then
    create type section_build_status as enum ('not_selected', 'queued', 'building', 'built', 'failed');
  end if;
  if not exists (select 1 from pg_type where typname = 'library_status') then
    create type library_status as enum ('draft', 'published', 'retired');
  end if;
end $$;

alter type ai_purpose add value if not exists 'kb_dedup';

-- ---------------------------------------------------------------------
-- 2. National curriculum: statement-level outcomes (cabinet 1)
--    One CP element paragraph = grain 'element'. Each sentence or clause split
--    from it = grain 'statement' with parent_id. Only statements are matched.
-- ---------------------------------------------------------------------
alter table public.cp_learning_outcomes
  add column if not exists grain           text not null default 'statement',
  add column if not exists parent_id       uuid references public.cp_learning_outcomes(id),
  add column if not exists embedding_model text;

do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'cplo_grain_check') then
    alter table public.cp_learning_outcomes add constraint cplo_grain_check
      check (grain in ('element', 'statement'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'cplo_embedding_model_check') then
    alter table public.cp_learning_outcomes add constraint cplo_embedding_model_check
      check (embedding is null or embedding_model is not null);
  end if;
end $$;

create index if not exists cplo_subject_idx on public.cp_learning_outcomes (cp_subject_id);

-- ---------------------------------------------------------------------
-- 3. Teaching materials: archive instead of delete; key for composite links
-- ---------------------------------------------------------------------
alter table public.teaching_materials
  add column if not exists archived_at timestamptz,
  add column if not exists page_count  integer;

do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'teaching_materials_id_kb_key') then
    alter table public.teaching_materials
      add constraint teaching_materials_id_kb_key unique (id, knowledge_base_id);
  end if;
end $$;

-- ---------------------------------------------------------------------
-- 4. Material sections (new): chapters detected in an upload.
--    A whole-book PDF becomes a list of sections; the teacher picks which to
--    build. S1 runs per section, so a knowledge base grows chapter by chapter.
-- ---------------------------------------------------------------------
create table if not exists public.material_sections (
    id                 uuid primary key default gen_random_uuid(),
    school_id          uuid not null,
    knowledge_base_id  uuid not null,
    material_id        uuid not null,
    parent_section_id  uuid references public.material_sections(id),
    ordinal            integer not null,
    level              smallint not null default 1 check (level between 1 and 4),
    title              text not null,
    page_start         integer not null check (page_start >= 1),
    page_end           integer not null,
    build_status       section_build_status not null default 'not_selected',
    built_at           timestamptz,
    created_at         timestamptz not null default now(),
    check (page_end >= page_start),
    check (build_status <> 'built' or built_at is not null),
    unique (material_id, ordinal),
    unique (id, material_id),
    foreign key (material_id, knowledge_base_id)
        references public.teaching_materials(id, knowledge_base_id),
    foreign key (knowledge_base_id, school_id)
        references public.knowledge_bases(id, school_id)
);
alter table public.material_sections enable row level security;

-- ---------------------------------------------------------------------
-- 5. Material chunks (cabinet 2): metadata, tenant keys, safe deletion
-- ---------------------------------------------------------------------
alter table public.material_chunks
  add column if not exists section_id      uuid,
  add column if not exists page_start      integer,
  add column if not exists page_end        integer,
  add column if not exists heading_path    text,
  add column if not exists chunk_kind      chunk_kind not null default 'other',
  add column if not exists embedding_model text;

do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'material_chunks_pages_check') then
    alter table public.material_chunks add constraint material_chunks_pages_check
      check (page_start is null or (page_start >= 1 and page_end >= page_start));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'material_chunks_embedding_model_check') then
    alter table public.material_chunks add constraint material_chunks_embedding_model_check
      check (embedding is null or embedding_model is not null);
  end if;
  -- the chunk's knowledge base must belong to the chunk's school
  if not exists (select 1 from pg_constraint where conname = 'material_chunks_kb_school_fkey') then
    alter table public.material_chunks add constraint material_chunks_kb_school_fkey
      foreign key (knowledge_base_id, school_id) references public.knowledge_bases(id, school_id);
  end if;
  -- the chunk's material must belong to the same knowledge base, and deleting a
  -- material with chunks is refused (was: on delete cascade). Archive it instead.
  if exists (select 1 from pg_constraint where conname = 'material_chunks_material_id_fkey') then
    alter table public.material_chunks drop constraint material_chunks_material_id_fkey;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'material_chunks_material_kb_fkey') then
    alter table public.material_chunks add constraint material_chunks_material_kb_fkey
      foreign key (material_id, knowledge_base_id)
      references public.teaching_materials(id, knowledge_base_id) on delete restrict;
  end if;
  -- the chunk's section must belong to the same material
  if not exists (select 1 from pg_constraint where conname = 'material_chunks_section_fkey') then
    alter table public.material_chunks add constraint material_chunks_section_fkey
      foreign key (section_id, material_id) references public.material_sections(id, material_id);
  end if;
end $$;

-- Exact search inside one knowledge base (see match_kb_chunks). The global HNSW
-- index from the base schema stays for future cross-KB use but is never used by
-- the per-KB functions, because filtering after an HNSW scan can silently drop results.
create index if not exists material_chunks_kb_idx
  on public.material_chunks (knowledge_base_id, school_id);

-- ---------------------------------------------------------------------
-- 6. Misconception library (cabinet 3, new). Platform-level, like the CP tables.
--    Paraphrased statements with citations; never copied text from the source.
-- ---------------------------------------------------------------------
create table if not exists public.misconception_library (
    id                     uuid primary key default gen_random_uuid(),
    subject                text not null,              -- 'Ilmu Pengetahuan Alam'
    phase                  text not null,              -- 'D'
    topic                  text not null,              -- 'Gaya dan Gerak'
    statement              text not null,              -- the wrong idea
    correct_understanding  text not null,
    student_phrasings      text[] not null default '{}',
    counter_examples       text[] not null default '{}',
    source_citations       text[] not null,
    status                 library_status not null default 'draft',
    embedding              vector(1536),
    embedding_model        text,
    published_at           timestamptz,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    check (cardinality(source_citations) >= 1),
    check (status <> 'published' or published_at is not null),
    check (embedding is null or embedding_model is not null),
    unique (subject, phase, topic, statement)
);
alter table public.misconception_library enable row level security;
drop trigger if exists misconception_library_updated_at on public.misconception_library;
create trigger misconception_library_updated_at before update on public.misconception_library
  for each row execute function set_updated_at();

-- ---------------------------------------------------------------------
-- 7. Concepts and misconceptions: per-item review, embeddings, tenant keys
--    Backfill runs only the first time the column is added, so re-running this
--    migration can never auto-approve items a teacher left pending.
-- ---------------------------------------------------------------------
do $$ begin
  if not exists (select 1 from information_schema.columns
                 where table_schema = 'public' and table_name = 'concepts'
                   and column_name = 'review_status') then
    alter table public.concepts
      add column review_status review_status not null default 'pending',
      add column reviewed_by   uuid references public.profiles(id),
      add column reviewed_at   timestamptz;
    update public.concepts c
       set review_status = 'approved',
           reviewed_at   = coalesce(kb.approved_at, now()),
           reviewed_by   = kb.approved_by
      from public.knowledge_bases kb
     where kb.id = c.knowledge_base_id and kb.status = 'approved';
  end if;

  if not exists (select 1 from information_schema.columns
                 where table_schema = 'public' and table_name = 'misconceptions'
                   and column_name = 'review_status') then
    alter table public.misconceptions
      add column review_status review_status not null default 'pending',
      add column reviewed_by   uuid references public.profiles(id),
      add column reviewed_at   timestamptz;
    update public.misconceptions m
       set review_status = 'approved',
           reviewed_at   = coalesce(kb.approved_at, now()),
           reviewed_by   = kb.approved_by
      from public.knowledge_bases kb
     where kb.id = m.knowledge_base_id and kb.status = 'approved';
  end if;
end $$;

alter table public.concepts
  add column if not exists embedding       vector(1536),
  add column if not exists embedding_model text;

alter table public.misconceptions
  add column if not exists library_id      uuid references public.misconception_library(id),
  add column if not exists embedding       vector(1536),
  add column if not exists embedding_model text;

do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'concepts_reviewed_check') then
    alter table public.concepts add constraint concepts_reviewed_check
      check (review_status = 'pending' or reviewed_at is not null);
  end if;
  if not exists (select 1 from pg_constraint where conname = 'misconceptions_reviewed_check') then
    alter table public.misconceptions add constraint misconceptions_reviewed_check
      check (review_status = 'pending' or reviewed_at is not null);
  end if;
  if not exists (select 1 from pg_constraint where conname = 'concepts_embedding_model_check') then
    alter table public.concepts add constraint concepts_embedding_model_check
      check (embedding is null or embedding_model is not null);
  end if;
  if not exists (select 1 from pg_constraint where conname = 'misconceptions_embedding_model_check') then
    alter table public.misconceptions add constraint misconceptions_embedding_model_check
      check (embedding is null or embedding_model is not null);
  end if;
  if not exists (select 1 from pg_constraint where conname = 'concepts_kb_school_fkey') then
    alter table public.concepts add constraint concepts_kb_school_fkey
      foreign key (knowledge_base_id, school_id) references public.knowledge_bases(id, school_id);
  end if;
  if not exists (select 1 from pg_constraint where conname = 'misconceptions_kb_school_fkey') then
    alter table public.misconceptions add constraint misconceptions_kb_school_fkey
      foreign key (knowledge_base_id, school_id) references public.knowledge_bases(id, school_id);
  end if;
end $$;

create index if not exists concepts_kb_idx              on public.concepts (knowledge_base_id, school_id);
create index if not exists misconceptions_kb_idx        on public.misconceptions (knowledge_base_id, school_id);
create index if not exists concepts_source_chunks_gin   on public.concepts using gin (source_chunk_ids);
create index if not exists misconceptions_source_chunks_gin on public.misconceptions using gin (source_chunk_ids);

-- A wrong idea can only be approved once its key idea is approved.
create or replace function public.check_misconception_review() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if new.review_status = 'approved' and not exists (
       select 1 from concepts c where c.id = new.concept_id and c.review_status = 'approved') then
    raise exception using errcode = '23514',
      message = 'A misconception can only be approved after its concept is approved.';
  end if;
  return new;
end $$;
drop trigger if exists misconceptions_review_check on public.misconceptions;
create trigger misconceptions_review_check before insert or update of review_status, concept_id
  on public.misconceptions for each row execute function public.check_misconception_review();

-- ---------------------------------------------------------------------
-- 8. Missions: record the grounding paragraphs; only approved items allowed
-- ---------------------------------------------------------------------
alter table public.mission_versions
  add column if not exists source_chunk_ids uuid[] not null default '{}';
create index if not exists mission_versions_source_chunks_gin
  on public.mission_versions using gin (source_chunk_ids);

-- Every id in source_chunk_ids must be a chunk of the same knowledge base and school.
-- Security definer: this check must see chunks even when called under RLS.
create or replace function public.check_source_chunks() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  v_kb  uuid;
  v_bad integer;
begin
  if new.source_chunk_ids is null or cardinality(new.source_chunk_ids) = 0 then
    return new;
  end if;
  if tg_table_name = 'mission_versions' then
    select m.knowledge_base_id into v_kb from missions m where m.id = new.mission_id;
  else
    v_kb := new.knowledge_base_id;
  end if;
  select count(*) into v_bad
    from unnest(new.source_chunk_ids) as s(id)
   where not exists (select 1 from material_chunks c
                     where c.id = s.id and c.knowledge_base_id = v_kb and c.school_id = new.school_id);
  if v_bad > 0 then
    raise exception using errcode = '23503',
      message = format('%s source chunk id(s) on %s are not chunks of knowledge base %s',
                       v_bad, tg_table_name, v_kb);
  end if;
  return new;
end $$;

drop trigger if exists concepts_source_chunks_check on public.concepts;
create trigger concepts_source_chunks_check before insert or update of source_chunk_ids
  on public.concepts for each row execute function public.check_source_chunks();
drop trigger if exists misconceptions_source_chunks_check on public.misconceptions;
create trigger misconceptions_source_chunks_check before insert or update of source_chunk_ids
  on public.misconceptions for each row execute function public.check_source_chunks();
drop trigger if exists mission_versions_source_chunks_check on public.mission_versions;
create trigger mission_versions_source_chunks_check before insert or update of source_chunk_ids
  on public.mission_versions for each row execute function public.check_source_chunks();

-- A chunk that an idea or a mission points at can't be deleted (history stays traceable).
create or replace function public.prevent_referenced_chunk_delete() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if exists (select 1 from concepts         where source_chunk_ids @> array[old.id])
  or exists (select 1 from misconceptions   where source_chunk_ids @> array[old.id])
  or exists (select 1 from mission_versions where source_chunk_ids @> array[old.id]) then
    raise exception using errcode = '23503',
      message = format('Chunk %s is referenced by a concept, misconception or mission version; archive its material instead.', old.id);
  end if;
  return old;
end $$;
drop trigger if exists material_chunks_delete_guard on public.material_chunks;
create trigger material_chunks_delete_guard before delete on public.material_chunks
  for each row execute function public.prevent_referenced_chunk_delete();

-- Items attached to a mission version must be approved, active, and from the
-- mission's own knowledge base (AI-3 enforced in the database, per item).
create or replace function public.check_mission_item() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  v_kb uuid;
  v_ok boolean;
begin
  select m.knowledge_base_id into v_kb
    from mission_versions mv join missions m on m.id = mv.mission_id
   where mv.id = new.mission_version_id;
  if tg_table_name = 'mission_version_concepts' then
    select (c.knowledge_base_id = v_kb and c.review_status = 'approved' and c.archived_at is null)
      into v_ok from concepts c where c.id = new.concept_id;
  else
    select (x.knowledge_base_id = v_kb and x.review_status = 'approved' and x.archived_at is null)
      into v_ok from misconceptions x where x.id = new.misconception_id;
  end if;
  if not coalesce(v_ok, false) then
    raise exception using errcode = '23514',
      message = format('%s: only approved, active items from the mission''s own knowledge base can be attached', tg_table_name);
  end if;
  return new;
end $$;
drop trigger if exists mvc_item_check on public.mission_version_concepts;
create trigger mvc_item_check before insert or update on public.mission_version_concepts
  for each row execute function public.check_mission_item();
drop trigger if exists mvm_item_check on public.mission_version_misconceptions;
create trigger mvm_item_check before insert or update on public.mission_version_misconceptions
  for each row execute function public.check_mission_item();

-- Teachers see source_chunk_ids through the teacher view (re-expand mv.*).
-- Students do not: the column is not in the student column grant from migration 002.
create or replace view public.v_teacher_mission_versions
with (security_barrier = true) as
select mv.*
from public.mission_versions mv
where public.is_active_teacher_in_school(mv.school_id);

-- ---------------------------------------------------------------------
-- 9. AI provenance: what was retrieved for each call
--    retrieval = [{"source": "material_chunk" | "cp_outcome" | "library" | "concept",
--                  "id": uuid, "path": "link" | "search" | "teacher_confirmed",
--                  "rank": int, "score": float}]
-- ---------------------------------------------------------------------
alter table public.ai_invocations add column if not exists retrieval jsonb;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'ai_invocations_retrieval_array') then
    alter table public.ai_invocations add constraint ai_invocations_retrieval_array
      check (retrieval is null or jsonb_typeof(retrieval) = 'array');
  end if;
end $$;

-- ---------------------------------------------------------------------
-- 10. Retrieval functions. Tenant filters live HERE, not in backend code,
--     because the backend uses the service role, which ignores RLS.
--     The MATERIALIZED CTE forces an exact scan inside one knowledge base.
--     Only the service role may call them.
-- ---------------------------------------------------------------------

-- Search the teacher's paragraphs in one knowledge base.
create or replace function public.match_kb_chunks(
    p_school_id        uuid,
    p_kb_id            uuid,
    p_query            vector(1536),
    p_embedding_model  text,
    p_k                integer default 3,
    p_min_similarity   double precision default 0,
    p_kinds            chunk_kind[] default null,
    p_exclude_ids      uuid[] default '{}')
returns table (chunk_id uuid, material_id uuid, section_id uuid, chunk_index integer,
               page_start integer, page_end integer, heading_path text, kind chunk_kind,
               content text, token_count integer, similarity double precision)
language sql stable set search_path = public as $$
  with kb as materialized (
    select c.id, c.material_id, c.section_id, c.chunk_index, c.page_start, c.page_end,
           c.heading_path, c.chunk_kind, c.content, c.token_count, c.embedding
      from material_chunks c
      join teaching_materials m on m.id = c.material_id
     where c.school_id = p_school_id
       and c.knowledge_base_id = p_kb_id
       and m.archived_at is null
       and c.embedding is not null
       and c.embedding_model = p_embedding_model
       and (p_kinds is null or c.chunk_kind = any (p_kinds))
       and not (c.id = any (coalesce(p_exclude_ids, '{}')))
  ), scored as (
    select kb.*, 1 - (kb.embedding <=> p_query) as sim from kb
  )
  select s.id, s.material_id, s.section_id, s.chunk_index, s.page_start, s.page_end,
         s.heading_path, s.chunk_kind, s.content, s.token_count, s.sim
    from scored s
   where s.sim >= p_min_similarity
   order by s.sim desc, s.id
   limit least(greatest(p_k, 0), 50);
$$;

-- Read linked paragraphs by id (S2 path 1), limited to one knowledge base, in reading order.
create or replace function public.get_kb_chunks(
    p_school_id uuid, p_kb_id uuid, p_chunk_ids uuid[])
returns table (chunk_id uuid, material_id uuid, section_id uuid, chunk_index integer,
               page_start integer, page_end integer, heading_path text, kind chunk_kind,
               content text, token_count integer)
language sql stable set search_path = public as $$
  select c.id, c.material_id, c.section_id, c.chunk_index, c.page_start, c.page_end,
         c.heading_path, c.chunk_kind, c.content, c.token_count
    from material_chunks c
    join teaching_materials m on m.id = c.material_id
   where c.school_id = p_school_id
     and c.knowledge_base_id = p_kb_id
     and m.archived_at is null
     and c.id = any (p_chunk_ids)
   order by m.created_at, c.material_id, c.page_start nulls last, c.chunk_index;
$$;

-- Find existing ideas close to a new one (S1 incremental duplicate check).
create or replace function public.match_kb_concepts(
    p_school_id uuid, p_kb_id uuid, p_query vector(1536), p_embedding_model text,
    p_k integer default 5)
returns table (concept_id uuid, name text, description text, status review_status,
               similarity double precision)
language sql stable set search_path = public as $$
  with kb as materialized (
    select c.id, c.name, c.description, c.review_status, c.embedding
      from concepts c
     where c.school_id = p_school_id and c.knowledge_base_id = p_kb_id
       and c.archived_at is null and c.review_status <> 'rejected'
       and c.embedding is not null and c.embedding_model = p_embedding_model
  )
  select kb.id, kb.name, kb.description, kb.review_status, 1 - (kb.embedding <=> p_query) as sim
    from kb order by sim desc, kb.id
   limit least(greatest(p_k, 0), 50);
$$;

-- Candidate CP statements for one CP subject (S1 step 6). Statements only, never element paragraphs.
create or replace function public.match_cp_outcomes(
    p_cp_subject_id uuid, p_query vector(1536), p_embedding_model text, p_k integer default 5)
returns table (outcome_id uuid, element text, description text, parent_id uuid,
               similarity double precision)
language sql stable set search_path = public as $$
  with s as materialized (
    select o.id, o.element, o.description, o.parent_id, o.embedding
      from cp_learning_outcomes o
     where o.cp_subject_id = p_cp_subject_id and o.grain = 'statement'
       and o.embedding is not null and o.embedding_model = p_embedding_model
  )
  select s.id, s.element, s.description, s.parent_id, 1 - (s.embedding <=> p_query) as sim
    from s order by sim desc, s.id
   limit least(greatest(p_k, 0), 50);
$$;

-- Research-sourced wrong ideas for a concept (S1 step 7). Published entries only.
create or replace function public.match_misconception_library(
    p_subject text, p_phase text, p_query vector(1536), p_embedding_model text,
    p_k integer default 5, p_topic text default null)
returns table (library_id uuid, topic text, statement text, correct_understanding text,
               student_phrasings text[], counter_examples text[], source_citations text[],
               similarity double precision)
language sql stable set search_path = public as $$
  with lib as materialized (
    select l.* from misconception_library l
     where l.subject = p_subject and l.phase = p_phase and l.status = 'published'
       and (p_topic is null or l.topic = p_topic)
       and l.embedding is not null and l.embedding_model = p_embedding_model
  )
  select lib.id, lib.topic, lib.statement, lib.correct_understanding, lib.student_phrasings,
         lib.counter_examples, lib.source_citations, 1 - (lib.embedding <=> p_query) as sim
    from lib order by sim desc, lib.id
   limit least(greatest(p_k, 0), 50);
$$;

revoke all on function public.match_kb_chunks(uuid, uuid, vector, text, integer, double precision, chunk_kind[], uuid[]) from public, anon, authenticated;
revoke all on function public.get_kb_chunks(uuid, uuid, uuid[])                         from public, anon, authenticated;
revoke all on function public.match_kb_concepts(uuid, uuid, vector, text, integer)      from public, anon, authenticated;
revoke all on function public.match_cp_outcomes(uuid, vector, text, integer)            from public, anon, authenticated;
revoke all on function public.match_misconception_library(text, text, vector, text, integer, text) from public, anon, authenticated;
grant execute on function public.match_kb_chunks(uuid, uuid, vector, text, integer, double precision, chunk_kind[], uuid[]) to service_role;
grant execute on function public.get_kb_chunks(uuid, uuid, uuid[])                         to service_role;
grant execute on function public.match_kb_concepts(uuid, uuid, vector, text, integer)      to service_role;
grant execute on function public.match_cp_outcomes(uuid, vector, text, integer)            to service_role;
grant execute on function public.match_misconception_library(text, text, vector, text, integer, text) to service_role;

-- Trigger functions are not callable as RPC by browser clients.
revoke all on function public.check_source_chunks()             from public, anon, authenticated;
revoke all on function public.prevent_referenced_chunk_delete() from public, anon, authenticated;
revoke all on function public.check_mission_item()              from public, anon, authenticated;
revoke all on function public.check_misconception_review()      from public, anon, authenticated;

-- ---------------------------------------------------------------------
-- 11. Teacher read access to the knowledge base (TC-2 review screen, sources)
--     Owner, plus teachers assigned to the knowledge base's subject.
--     Students and parents: no policy, so no rows. Writes stay backend-only.
-- ---------------------------------------------------------------------
create or replace function public.teacher_can_read_kb(p_kb uuid)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from knowledge_bases kb
     where kb.id = p_kb
       and public.is_active_teacher_in_school(kb.school_id)
       and (kb.owner_teacher_id = auth.uid()
            or exists (select 1 from teaching_assignments ta
                        where ta.school_id = kb.school_id
                          and ta.school_subject_id = kb.school_subject_id
                          and ta.teacher_id = auth.uid())));
$$;
revoke all on function public.teacher_can_read_kb(uuid) from public, anon;
grant execute on function public.teacher_can_read_kb(uuid) to authenticated, service_role;

do $$
declare
  spec text[];
begin
  foreach spec slice 1 in array array[
    ['knowledge_bases',       'kb_teacher_read',        'id'],
    ['teaching_materials',    'materials_teacher_read', 'knowledge_base_id'],
    ['material_sections',     'sections_teacher_read',  'knowledge_base_id'],
    ['material_chunks',       'chunks_teacher_read',    'knowledge_base_id'],
    ['concepts',              'concepts_teacher_read',  'knowledge_base_id'],
    ['concept_prerequisites', 'prereqs_teacher_read',   'knowledge_base_id'],
    ['misconceptions',        'misconc_teacher_read',   'knowledge_base_id']
  ] loop
    if not exists (select 1 from pg_policies
                   where schemaname = 'public' and tablename = spec[1] and policyname = spec[2]) then
      execute format('create policy %I on public.%I for select to authenticated using (public.teacher_can_read_kb(%I))',
                     spec[2], spec[1], spec[3]);
    end if;
  end loop;
end $$;

-- Browser clients never need raw vectors.
revoke select on public.material_chunks from anon, authenticated;
grant select (id, school_id, material_id, knowledge_base_id, section_id, chunk_index, content,
              token_count, page_start, page_end, heading_path, chunk_kind)
  on public.material_chunks to authenticated;

-- ---------------------------------------------------------------------
-- 12. Views
-- ---------------------------------------------------------------------
-- Items waiting for the owner's review, per knowledge base.
create or replace view public.v_kb_review_queue
with (security_invoker = true) as
select kb.id as knowledge_base_id, kb.school_id,
       (select count(*) from public.concepts c
         where c.knowledge_base_id = kb.id and c.review_status = 'pending' and c.archived_at is null) as pending_concepts,
       (select count(*) from public.misconceptions m
         where m.knowledge_base_id = kb.id and m.review_status = 'pending' and m.archived_at is null) as pending_misconceptions
from public.knowledge_bases kb;

-- Concepts whose CP label is missing or points at a CP subject the school no longer
-- maps to (SA-7 remap or a new CP version). The backend re-runs CP alignment on these.
create or replace view public.v_concepts_needing_cp_realign
with (security_invoker = true) as
select c.id as concept_id, c.school_id, c.knowledge_base_id,
       ss.cp_subject_id as mapped_cp_subject_id,
       o.cp_subject_id  as labelled_cp_subject_id
from public.concepts c
join public.knowledge_bases kb on kb.id = c.knowledge_base_id
join public.school_subjects ss on ss.id = kb.school_subject_id
left join public.cp_learning_outcomes o on o.id = c.cp_learning_outcome_id
where c.archived_at is null
  and c.review_status <> 'rejected'
  and ss.cp_subject_id is not null
  and (o.id is null or o.cp_subject_id is distinct from ss.cp_subject_id);

revoke all on public.v_kb_review_queue, public.v_concepts_needing_cp_realign from anon;
revoke all on public.v_concepts_needing_cp_realign from authenticated;
grant select on public.v_kb_review_queue to authenticated, service_role;
grant select on public.v_concepts_needing_cp_realign to service_role;

-- End of migration 003
