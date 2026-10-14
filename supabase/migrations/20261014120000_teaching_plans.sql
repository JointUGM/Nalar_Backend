-- TC-23: one AI-drafted, teacher-reviewed next-lesson activity per publication and misconception.
-- Generated once on the teacher's request (never on view); the plan holds no names or counts.
alter type public.ai_purpose add value if not exists 'teaching_plan';

create table if not exists public.teaching_plans (
    id                uuid primary key default gen_random_uuid(),
    school_id         uuid not null,
    publication_id    uuid not null,
    misconception_id  uuid not null references public.misconceptions(id),
    plan              jsonb not null check (jsonb_typeof(plan) = 'object'),
    ai_invocation_id  uuid references public.ai_invocations(id),
    generated_at      timestamptz not null default now(),
    unique (publication_id, misconception_id),
    foreign key (publication_id, school_id) references public.publications(id, school_id)
);
alter table public.teaching_plans enable row level security;  -- no policy on purpose: read through the backend
