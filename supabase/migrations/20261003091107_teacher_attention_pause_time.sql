-- Record actual safety pauses for the teacher attention queue; legacy times remain unknown.
alter table public.sessions add column safety_paused_at timestamptz;
