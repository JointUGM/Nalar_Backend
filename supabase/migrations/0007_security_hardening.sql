-- Hardening from the production security advisor (2026-09-28). Safe to re-run.
-- Screens read through the backend (decision B4), so browser roles lose the three
-- security-barrier teacher views: v_teacher_mission_versions let any teacher in a school
-- read other teachers' drafts and reference reasoning.

revoke select on public.v_teacher_session_turns,
                 public.v_session_challenge_coverage,
                 public.v_teacher_mission_versions
  from anon, authenticated;

-- RLS policies call these helpers as the signed-in user, so authenticated keeps EXECUTE.
revoke execute on function public.has_school_role(uuid, public.membership_role[]),
                           public.is_platform_admin(),
                           public.parent_can_see_publication(uuid, uuid),
                           public.parent_can_see_session(uuid),
                           public.teaches_publication(uuid)
  from public, anon;
grant execute on function public.has_school_role(uuid, public.membership_role[]),
                          public.is_platform_admin(),
                          public.parent_can_see_publication(uuid, uuid),
                          public.parent_can_see_session(uuid),
                          public.teaches_publication(uuid)
  to authenticated, service_role;

alter function public.set_updated_at() set search_path = public;
alter function public.prevent_locked_version_update() set search_path = public;
alter function public.lock_version_on_publish() set search_path = public;
alter function public.apply_score_override() set search_path = public;
