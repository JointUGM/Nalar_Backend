-- PA-4: a parent can turn the weekly digest off (on by default).
alter table public.profiles
  add column if not exists weekly_digest_enabled boolean not null default true;
