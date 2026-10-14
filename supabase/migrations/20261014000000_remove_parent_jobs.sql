-- Retire parent access and recurring jobs while preserving historical records.
select cron.unschedule(jobid) from cron.job
 where jobname in ('nalar-weekly-digest', 'nalar-release-reminders');

-- Shared RLS policies retain their student/teacher branches; parent branches deny access.
create or replace function public.parent_can_see_session(p_session uuid)
returns boolean language sql stable security definer set search_path = public
as $$ select false $$;

create or replace function public.parent_can_see_publication(p_publication uuid, p_student uuid)
returns boolean language sql stable security definer set search_path = public
as $$ select false $$;
