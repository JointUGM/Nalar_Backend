-- Backend v1: live lobby, safety session states, mission review marker and frozen pack
-- inputs, topic knowledge bases, evaluation status, jobs, realtime. Safe to re-run.

do $$ begin
  if not exists (select 1 from pg_type where typname = 'participant_status') then
    create type public.participant_status as enum ('waiting', 'started', 'cancelled');
  end if;
  if not exists (select 1 from pg_type where typname = 'evaluation_status') then
    create type public.evaluation_status as enum ('completed', 'no_answer', 'failed');
  end if;
  if not exists (select 1 from pg_type where typname = 'job_status') then
    create type public.job_status as enum ('queued', 'running', 'succeeded', 'failed');
  end if;
end $$;

alter table public.publication_runs
  add column if not exists lobby_opened_at timestamptz;
alter table public.publication_runs alter column planner_mode set default 'table';

do $$
declare c record;
begin
  for c in select conname from pg_constraint
            where conrelid = 'public.publication_runs'::regclass and contype = 'c'
              and pg_get_constraintdef(oid) ilike '%join_code IS NOT NULL%'
              and conname <> 'publication_runs_live_code_after_scheduled'
  loop
    execute format('alter table public.publication_runs drop constraint %I', c.conname);
  end loop;
  if not exists (select 1 from pg_constraint where conname = 'publication_runs_live_code_after_scheduled') then
    alter table public.publication_runs add constraint publication_runs_live_code_after_scheduled
      check (mode <> 'live' or status = 'scheduled' or join_code is not null);
  end if;
end $$;

drop index if exists public.publication_runs_open_join_code;
create unique index publication_runs_open_join_code
  on public.publication_runs (join_code)
  where status in ('lobby', 'open') and join_code is not null;

do $$
declare c record;
begin
  for c in select conname from pg_constraint
            where conrelid = 'public.sessions'::regclass and contype = 'c'
              and pg_get_constraintdef(oid) ilike '%in_progress%'
              and conname not in ('sessions_open_has_no_end', 'sessions_open_has_no_reason')
  loop
    execute format('alter table public.sessions drop constraint %I', c.conname);
  end loop;
  if not exists (select 1 from pg_constraint where conname = 'sessions_open_has_no_end') then
    alter table public.sessions add constraint sessions_open_has_no_end
      check ((status in ('in_progress', 'paused_safety')) = (ended_at is null));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'sessions_open_has_no_reason') then
    alter table public.sessions add constraint sessions_open_has_no_reason
      check ((status in ('in_progress', 'paused_safety')) = (end_reason is null));
  end if;
end $$;

alter table public.sessions add column if not exists deadline_at timestamptz;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'sessions_deadline_after_start') then
    alter table public.sessions add constraint sessions_deadline_after_start
      check (deadline_at is null or deadline_at > started_at);
  end if;
end $$;
create index if not exists sessions_open_deadline_idx
  on public.sessions (deadline_at) where status in ('in_progress', 'paused_safety');

-- A paused session is not finished, so it must not count as the latest attempt.
create or replace view public.v_latest_sessions with (security_invoker = true) as
select distinct on (s.publication_id, s.student_id) s.*
  from public.sessions s
 where s.status not in ('in_progress', 'paused_safety')
 order by s.publication_id, s.student_id, s.attempt_number desc;

alter table public.session_turns add column if not exists client_submission_id uuid;
create unique index if not exists session_turns_submission_key
  on public.session_turns (session_id, client_submission_id)
  where client_submission_id is not null;

create table if not exists public.run_participants (
    id                   uuid primary key default gen_random_uuid(),
    school_id            uuid not null,
    run_id               uuid not null,
    student_id           uuid not null references public.student_profiles(user_id),
    status               public.participant_status not null default 'waiting',
    warmup_choice_id     text,
    warmup_submitted_at  timestamptz,
    session_id           uuid,
    joined_at            timestamptz not null default now(),
    foreign key (run_id, school_id)     references public.publication_runs(id, school_id),
    foreign key (session_id, school_id) references public.sessions(id, school_id),
    unique (run_id, student_id),
    check ((warmup_choice_id is null) = (warmup_submitted_at is null)),
    check ((status = 'started') = (session_id is not null))
);
alter table public.run_participants enable row level security;
create index if not exists run_participants_run_idx on public.run_participants (run_id, status);

create or replace function public.teaches_run(p_run uuid)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (select 1 from publication_runs r
                  where r.id = p_run and public.teaches_publication(r.publication_id));
$$;
revoke all on function public.teaches_run(uuid) from public, anon;
grant execute on function public.teaches_run(uuid) to authenticated, service_role;

do $$ begin
  if not exists (select 1 from pg_policies where tablename = 'run_participants' and policyname = 'participants_read') then
    create policy participants_read on public.run_participants for select to authenticated
      using (student_id = auth.uid() or public.teaches_run(run_id));
  end if;
end $$;

alter table public.mission_versions
  add column if not exists question_bank jsonb  not null default '[]'::jsonb,
  add column if not exists answer_terms  text[] not null default '{}',
  add column if not exists live_warmup   jsonb,
  add column if not exists reviewed_at   timestamptz,
  add column if not exists reviewed_by   uuid references public.profiles(id);
alter table public.mission_versions alter column max_duration_minutes set default 20;

do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'mission_versions_bank_is_array') then
    alter table public.mission_versions add constraint mission_versions_bank_is_array
      check (jsonb_typeof(question_bank) = 'array');
  end if;
  if not exists (select 1 from pg_constraint where conname = 'mission_versions_reviewed_has_pack') then
    alter table public.mission_versions add constraint mission_versions_reviewed_has_pack
      check (reviewed_at is null or (context_pack is not null and reference_reasoning is not null));
  end if;
end $$;
-- Students can't read these columns: migration 002 grants them an explicit column list.

create or replace function public.require_reviewed_version() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if not exists (select 1 from mission_versions
                  where id = new.mission_version_id and reviewed_at is not null) then
    raise exception using errcode = '23514',
      message = 'Only a reviewed mission version (with a frozen context pack) can be published.';
  end if;
  return new;
end $$;
revoke all on function public.require_reviewed_version() from public, anon, authenticated;
drop trigger if exists publications_require_reviewed on public.publications;
create trigger publications_require_reviewed before insert on public.publications
  for each row execute function public.require_reviewed_version();

alter table public.session_evaluations
  add column if not exists status public.evaluation_status not null default 'completed';

alter table public.knowledge_bases
  add column if not exists topic_key   text,
  add column if not exists topic_title text;
update public.knowledge_bases set topic_key = 'umum' where topic_key is null;
update public.knowledge_bases set topic_title = 'Umum' where topic_title is null;
alter table public.knowledge_bases
  alter column topic_key set not null,
  alter column topic_title set not null;
alter table public.knowledge_bases drop constraint if exists knowledge_bases_school_subject_id_key;
create unique index if not exists knowledge_bases_subject_topic_key
  on public.knowledge_bases (school_subject_id, topic_key);

create table if not exists public.jobs (
    id             uuid primary key default gen_random_uuid(),
    school_id      uuid references public.schools(id),
    kind           text not null,
    status         public.job_status not null default 'queued',
    entity_type    text not null,
    entity_id      uuid not null,
    requested_by   uuid references public.profiles(id),
    attempts       integer not null default 0,
    error_code     text,
    error_message  text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);
alter table public.jobs enable row level security;   -- no policy on purpose: read through the backend
create index if not exists jobs_entity_idx on public.jobs (entity_type, entity_id, created_at desc);
drop trigger if exists jobs_updated_at on public.jobs;
create trigger jobs_updated_at before update on public.jobs
  for each row execute function set_updated_at();

do $$ begin
  if not exists (select 1 from pg_policies where tablename = 'notifications' and policyname = 'notifications_own') then
    create policy notifications_own on public.notifications for select to authenticated
      using (recipient_id = auth.uid());
  end if;
end $$;

do $$
declare t text;
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    foreach t in array array['sessions', 'run_participants', 'notifications'] loop
      if not exists (select 1 from pg_publication_tables
                      where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = t) then
        execute format('alter publication supabase_realtime add table public.%I', t);
      end if;
    end loop;
  end if;
end $$;
