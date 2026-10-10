-- Revision requests are private durable intent; published packets are never backfilled.
alter table public.mission_versions
  add column learning_objective_snapshot text,
  add column title_snapshot text,
  add column base_version_id uuid references public.mission_versions(id);

create table public.mission_legacy_metadata (
  version_id uuid primary key references public.mission_versions(id),
  learning_objective text not null,
  title text not null
);
alter table public.mission_legacy_metadata enable row level security;
revoke all on public.mission_legacy_metadata from anon, authenticated;
grant all on public.mission_legacy_metadata to service_role;
insert into public.mission_legacy_metadata(version_id, learning_objective, title)
select mv.id, mi.learning_objective, mi.title
from public.mission_versions mv join public.missions mi on mi.id=mv.mission_id;

create table public.mission_revision_requests (
  job_id uuid primary key references public.jobs(id),
  school_id uuid not null,
  mission_id uuid not null,
  requested_by uuid not null references public.profiles(id),
  idempotency_key uuid not null,
  request_hash text not null,
  request jsonb not null,
  created_at timestamptz not null default now(),
  foreign key (mission_id, school_id) references public.missions(id, school_id),
  unique (requested_by, mission_id, idempotency_key)
);
create index mission_revision_requests_mission_idx on public.mission_revision_requests(mission_id);
create index mission_versions_base_idx on public.mission_versions(base_version_id);
alter table public.mission_revision_requests enable row level security;
revoke all on public.mission_revision_requests from anon, authenticated;
grant all on public.mission_revision_requests to service_role;

create function public.freeze_mission_revision_request() returns trigger
language plpgsql set search_path = '' as $$
begin
  raise exception 'mission revision requests are immutable';
end $$;
create trigger mission_revision_request_immutable before update or delete
  on public.mission_revision_requests for each row execute function public.freeze_mission_revision_request();
revoke all on function public.freeze_mission_revision_request() from public, anon, authenticated;

create function public.snapshot_mission_version() returns trigger
language plpgsql set search_path = '' as $$
declare parent public.missions; base public.mission_versions;
begin
  select * into strict parent from public.missions where id = new.mission_id;
  if new.base_version_id is not null then
    select * into strict base from public.mission_versions where id = new.base_version_id;
    if base.mission_id <> new.mission_id or base.school_id <> new.school_id then
      raise exception 'base version must belong to the same mission and school';
    end if;
  end if;
  new.learning_objective_snapshot := coalesce(new.learning_objective_snapshot, base.learning_objective_snapshot, parent.learning_objective);
  new.title_snapshot := coalesce(new.title_snapshot, base.title_snapshot, parent.title);
  if length(trim(new.learning_objective_snapshot)) = 0 or length(trim(new.title_snapshot)) = 0 then
    raise exception 'mission snapshots must not be blank';
  end if;
  return new;
end $$;
create trigger mission_version_snapshot before insert on public.mission_versions
  for each row execute function public.snapshot_mission_version();
revoke all on function public.snapshot_mission_version() from public, anon, authenticated;

create function public.keep_mission_identity() returns trigger
language plpgsql set search_path = '' as $$
begin
  if (old.learning_objective is distinct from new.learning_objective)
     and exists(select 1 from public.mission_versions where mission_id = old.id) then
    raise exception 'change mission metadata through a new version';
  end if;
  return new;
end $$;
create trigger mission_identity_immutable before update on public.missions
  for each row execute function public.keep_mission_identity();
revoke all on function public.keep_mission_identity() from public, anon, authenticated;

create trigger mission_legacy_metadata_immutable before update or delete
  on public.mission_legacy_metadata for each row execute function public.freeze_mission_revision_request();
