-- Separate recovery proofs and durable credential fences preserve onboarding and revoke stale sessions.
alter table public.profiles add column credential_revision bigint not null default 0;

create table public.password_resets (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles(id),
    recipient_email text not null,
    status text not null default 'pending'
        check (status in ('pending', 'submitting', 'sent', 'verifying', 'consumed', 'failed')),
    proof_digest text,
    queue_expires_at timestamptz not null,
    expires_at timestamptz not null,
    created_at timestamptz not null,
    consumed_at timestamptz,
    scheduled_for timestamptz not null,
    payload jsonb not null default '{}'::jsonb
);
create index password_resets_user_created on public.password_resets(user_id, created_at desc);
create unique index password_resets_one_pending on public.password_resets(user_id)
    where status in ('pending', 'submitting', 'sent', 'verifying');
create index password_resets_due on public.password_resets(scheduled_for)
    where status = 'pending';

create table public.password_mutations (
    id uuid primary key,
    user_id uuid not null references public.profiles(id),
    reset_id uuid references public.password_resets(id),
    phase text not null check (phase in (
        'prepared', 'password_submitting', 'password_changed', 'unknown', 'completed', 'failed'
    )),
    prior_password_digest text not null,
    created_at timestamptz not null,
    lease_until timestamptz not null,
    completed_at timestamptz
);
create unique index password_mutations_one_active on public.password_mutations(user_id)
    where phase not in ('completed', 'failed');

alter table public.password_resets enable row level security;
alter table public.password_mutations enable row level security;
revoke all on public.password_resets, public.password_mutations from anon, authenticated;
grant all on public.password_resets, public.password_mutations to service_role;
