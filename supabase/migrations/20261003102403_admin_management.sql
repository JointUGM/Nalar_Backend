-- Add admin metadata and durable retry identities without changing historical assessments.
alter table public.schools add column city text;
alter table public.classes add column homeroom_teacher_id uuid references public.profiles(id);
alter table public.parent_student_links add column deactivated_at timestamptz;
alter table public.cp_versions add column is_current boolean not null default false;
update public.cp_versions set is_current = true where id = (
    select id from public.cp_versions where status = 'published'
    order by effective_on desc, published_at desc, id limit 1
);
create unique index cp_versions_one_current on public.cp_versions (is_current) where is_current;
alter table public.cp_versions add constraint cp_current_published
    check (not is_current or status = 'published');
create table public.admin_requests (
    id uuid primary key default gen_random_uuid(),
    actor_id uuid not null references public.profiles(id),
    operation text not null,
    scope_id uuid not null,
    request_key uuid not null,
    request_digest text not null,
    result_id uuid,
    created_at timestamptz not null default now(),
    unique (actor_id, operation, scope_id, request_key)
);
alter table public.admin_requests enable row level security;
revoke all on public.admin_requests from anon, authenticated;
grant all on public.admin_requests to service_role;
