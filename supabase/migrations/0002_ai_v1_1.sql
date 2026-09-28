-- =====================================================================
-- NALAR migration 002: AI design v1.1
-- Apply AFTER NALAR-Schema.sql (v1.0). Safe to re-run.
--
-- What it does
--   D4  misconceptions: detection cues, counter-examples, source chunks; concepts: source chunks
--   D5  mission_versions: hidden reference reasoning + frozen context pack; publication_runs: planner mode
--   D6  session_turns: bounded-prober trace (target concept, allowed moves, chosen move, reason,
--       guard result, second misconception, frustration, safety pause, analyzer call);
--       sessions: current turn counter for the live monitor; idempotency unique keys
--   D7  session_evaluations: per-turn quality; session_concept_results: misconception at start + resolved
--   D9  ai_invocations: provider, cache tokens, request id; new enum labels
--   Access: students can no longer read answer states, moves, rubric, question bank or reference
--           reasoning; teachers read them through views; teacher-only coverage view
--
-- Enum labels: this migration ADDS labels with IF NOT EXISTS and CHECKS the labels it relies on.
-- If a check fails it stops with a message naming the label to align.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. Preconditions
-- ---------------------------------------------------------------------
do $$
declare
  missing text := '';
  pair text[];
begin
  foreach pair slice 1 in array array[
    ['membership_role','teacher'],
    ['membership_status','active'],
    ['probe_strategy','transfer'],
    ['probe_strategy','request_justification'],
    ['probe_strategy','counter_example'],
    ['probe_strategy','decompose'],
    ['probe_strategy','refuse_and_redirect']
  ] loop
    if not exists (
      select 1 from pg_enum e join pg_type t on t.oid = e.enumtypid
      where t.typname = pair[1] and e.enumlabel = pair[2]
    ) then
      missing := missing || format(' %s.%s', pair[1], pair[2]);
    end if;
  end loop;
  if missing <> '' then
    raise exception 'Migration 002 expects these enum labels, which were not found:%. Rename them in this file to match NALAR-Schema.sql, then re-run.', missing;
  end if;
end $$;

-- ---------------------------------------------------------------------
-- 1. Enum labels (additive)
-- ---------------------------------------------------------------------
alter type probe_strategy     add value if not exists 'deeper_reason';
alter type probe_strategy     add value if not exists 'explain_mechanism';
alter type probe_strategy     add value if not exists 'simpler_reason';
alter type answer_state       add value if not exists 'mixed';
alter type session_end_reason add value if not exists 'safety_pause';
alter type notification_type  add value if not exists 'wellbeing_alert';
alter type ai_purpose         add value if not exists 'turn_analyze';
alter type ai_purpose         add value if not exists 'probe_plan';
alter type ai_purpose         add value if not exists 'kb_extract';
alter type ai_purpose         add value if not exists 'kb_misconceptions';
alter type ai_purpose         add value if not exists 'cp_align';
alter type ai_purpose         add value if not exists 'mission_critic';
alter type ai_purpose         add value if not exists 'embedding';

do $$ begin
  if not exists (select 1 from pg_type where typname = 'planner_mode') then
    create type planner_mode as enum ('table', 'hybrid');
  end if;
  if not exists (select 1 from pg_type where typname = 'move_source') then
    create type move_source as enum ('planner', 'default', 'fixed_rule', 'prefilter', 'fallback_invalid', 'fallback_error');
  end if;
  if not exists (select 1 from pg_type where typname = 'move_reason_code') then
    create type move_reason_code as enum ('default', 'mixed_answer', 'second_wrong_idea', 'no_change_after_example',
                                          'frustration', 'already_covered', 'check_deeper');
  end if;
  if not exists (select 1 from pg_type where typname = 'guard_result') then
    create type guard_result as enum ('passed', 'blocked_shape', 'blocked_verdict', 'blocked_new_terms',
                                      'blocked_similarity', 'blocked_drift', 'not_run');
  end if;
end $$;

-- ---------------------------------------------------------------------
-- 2. D4 Knowledge base
-- ---------------------------------------------------------------------
alter table public.misconceptions
  add column if not exists detection_cues   text[] not null default '{}',
  add column if not exists counter_examples text[] not null default '{}',
  add column if not exists source_chunk_ids uuid[] not null default '{}';

alter table public.concepts
  add column if not exists source_chunk_ids uuid[] not null default '{}';

-- ---------------------------------------------------------------------
-- 3. D5 Missions and publications
--    context_pack is written by the backend BEFORE inserting the publication,
--    because the existing lock trigger rejects any edit once locked_at is set.
-- ---------------------------------------------------------------------
alter table public.mission_versions
  add column if not exists reference_reasoning text,
  add column if not exists context_pack        jsonb;

alter table public.publication_runs
  add column if not exists planner_mode planner_mode not null default 'hybrid';

-- ---------------------------------------------------------------------
-- 4. D6 Sessions and turns
-- ---------------------------------------------------------------------
alter table public.sessions
  add column if not exists current_turn_index smallint not null default 0;

alter table public.session_turns
  add column if not exists target_concept_id          uuid references public.concepts(id),
  add column if not exists allowed_moves              probe_strategy[],
  add column if not exists move_source                move_source,
  add column if not exists move_reason_code           move_reason_code,
  add column if not exists move_reason                text,
  add column if not exists question_bank_id           text,
  add column if not exists guard_result               guard_result,
  add column if not exists secondary_misconception_id uuid references public.misconceptions(id),
  add column if not exists frustration_signal         boolean not null default false,
  add column if not exists safety_paused              boolean not null default false,
  add column if not exists analysis_ai_invocation_id  uuid references public.ai_invocations(id);

comment on column public.session_turns.prompt_ai_invocation_id   is 'The Sonnet call that chose the move and wrote the question (v1.1)';
comment on column public.session_turns.analysis_ai_invocation_id is 'The Haiku call that labeled the answer (v1.1)';

do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'session_turns_reason_required') then
    alter table public.session_turns add constraint session_turns_reason_required
      check (move_reason_code is null or move_reason_code = 'default' or move_reason is not null);
  end if;
  if not exists (select 1 from pg_constraint where conname = 'session_turns_chosen_move_allowed') then
    alter table public.session_turns add constraint session_turns_chosen_move_allowed
      check (allowed_moves is null or prompt_strategy is null or prompt_strategy = any (allowed_moves));
  end if;
end $$;

-- Idempotency keys (NFR-R1): create only if no unique index already covers the same columns
do $$
declare
  spec record;
begin
  for spec in
    select * from (values
      ('session_turns',     array['session_id','turn_index'], 'session_turns_session_id_turn_index_key'),
      ('telemetry_batches', array['client_seq','session_id'], 'telemetry_batches_session_id_client_seq_key')
    ) as v(tbl, cols, idx)
  loop
    if not exists (
      select 1
      from pg_index i
      join pg_class c on c.oid = i.indrelid
      join pg_namespace n on n.oid = c.relnamespace
      where n.nspname = 'public' and c.relname = spec.tbl and i.indisunique
        and (select array_agg(a.attname::text order by a.attname)
             from pg_attribute a
             where a.attrelid = c.oid and a.attnum = any (i.indkey)) = spec.cols
    ) then
      execute format('create unique index %I on public.%I (%s)',
                     spec.idx, spec.tbl,
                     case spec.tbl when 'session_turns' then 'session_id, turn_index'
                                   else 'session_id, client_seq' end);
    end if;
  end loop;
end $$;

create index if not exists session_turns_target_concept_idx on public.session_turns (session_id, target_concept_id);

-- ---------------------------------------------------------------------
-- 5. D7 Evaluation
-- ---------------------------------------------------------------------
alter table public.session_evaluations
  add column if not exists turn_quality jsonb not null default '[]'::jsonb;

alter table public.session_concept_results
  add column if not exists initial_misconception_id uuid references public.misconceptions(id),
  add column if not exists resolved_in_session      boolean not null default false;

do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'session_evaluations_turn_quality_is_array') then
    alter table public.session_evaluations add constraint session_evaluations_turn_quality_is_array
      check (jsonb_typeof(turn_quality) = 'array');
  end if;
  if not exists (select 1 from pg_constraint where conname = 'scr_resolved_needs_initial') then
    alter table public.session_concept_results add constraint scr_resolved_needs_initial
      check (not resolved_in_session or initial_misconception_id is not null);
  end if;
end $$;

-- ---------------------------------------------------------------------
-- 6. D9 Operations
-- ---------------------------------------------------------------------
alter table public.ai_invocations
  add column if not exists provider           text not null default 'sumopod',
  add column if not exists cache_read_tokens  integer,
  add column if not exists cache_write_tokens integer,
  add column if not exists request_id         text;

-- ---------------------------------------------------------------------
-- 7. Access helpers (security definer, per the team rule: policies and views
--    never query another RLS-protected table directly)
-- ---------------------------------------------------------------------
create or replace function public.is_active_teacher_in_school(p_school uuid)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from school_memberships m
    where m.school_id = p_school and m.user_id = auth.uid()
      and m.role = 'teacher' and m.status = 'active'
  );
$$;

create or replace function public.teacher_can_see_session_detail(p_session uuid)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (
    select 1
    from sessions s
    join publications p          on p.id = s.publication_id and p.school_id = s.school_id
    join teaching_assignments ta on ta.class_id = p.class_id and ta.school_id = s.school_id
    where s.id = p_session and ta.teacher_id = auth.uid()
  ) and exists (
    select 1 from sessions s where s.id = p_session and public.is_active_teacher_in_school(s.school_id)
  );
$$;

revoke all on function public.is_active_teacher_in_school(uuid)     from public, anon;
revoke all on function public.teacher_can_see_session_detail(uuid) from public, anon;
grant execute on function public.is_active_teacher_in_school(uuid)     to authenticated, service_role;
grant execute on function public.teacher_can_see_session_detail(uuid) to authenticated, service_role;

-- ---------------------------------------------------------------------
-- 8. Column privileges: what students (and any browser client) may read
--    RLS still decides WHICH ROWS; these grants decide WHICH COLUMNS.
--    The backend uses the service role and is unaffected.
-- ---------------------------------------------------------------------
revoke select on public.session_turns from anon, authenticated;
grant select (id, school_id, session_id, turn_index, prompt_kind, prompt_text,
              prompt_shown_at, answer_text, answer_submitted_at)
  on public.session_turns to authenticated;

revoke select on public.mission_versions from anon, authenticated;
grant select (id, school_id, mission_id, version_number, anchor_problem,
              max_turns, max_duration_minutes, created_by, locked_at, created_at)
  on public.mission_versions to authenticated;

-- ---------------------------------------------------------------------
-- 9. Views
-- ---------------------------------------------------------------------
-- Students: safe columns only, row access by the table's own RLS
create or replace view public.v_student_session_turns
with (security_invoker = true) as
select id, school_id, session_id, turn_index, prompt_kind, prompt_text,
       prompt_shown_at, answer_text, answer_submitted_at
from public.session_turns;

create or replace view public.v_student_mission_versions
with (security_invoker = true) as
select id, school_id, mission_id, version_number, anchor_problem,
       max_turns, max_duration_minutes, locked_at
from public.mission_versions;

-- Teachers: every column, only for sessions of classes they teach
create or replace view public.v_teacher_session_turns
with (security_barrier = true) as
select t.*
from public.session_turns t
where public.teacher_can_see_session_detail(t.session_id);

create or replace view public.v_teacher_mission_versions
with (security_barrier = true) as
select mv.*
from public.mission_versions mv
where public.is_active_teacher_in_school(mv.school_id);

-- Teachers: did every target concept get at least one challenge probe?
create or replace view public.v_session_challenge_coverage
with (security_barrier = true) as
select s.id  as session_id,
       s.school_id,
       mvc.concept_id,
       exists (
         select 1 from public.session_turns t
         where t.session_id = s.id
           and t.target_concept_id = mvc.concept_id
           and t.prompt_strategy::text in ('counter_example', 'transfer')
       ) as challenged
from public.sessions s
join public.publications p              on p.id = s.publication_id and p.school_id = s.school_id
join public.mission_version_concepts mvc on mvc.mission_version_id = p.mission_version_id
where public.teacher_can_see_session_detail(s.id);

revoke all on public.v_student_session_turns, public.v_student_mission_versions,
              public.v_teacher_session_turns, public.v_teacher_mission_versions,
              public.v_session_challenge_coverage from anon;
grant select on public.v_student_session_turns, public.v_student_mission_versions,
                public.v_teacher_session_turns, public.v_teacher_mission_versions,
                public.v_session_challenge_coverage to authenticated, service_role;

-- End of migration 002
