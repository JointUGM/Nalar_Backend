-- Durable retry receipts and private UI state preserve historical learning records.
alter table public.admin_requests add column response_payload jsonb;
alter table public.knowledge_bases add column archived_at timestamptz;
alter table public.notifications add column read_at timestamptz;
create index notifications_recipient_created on public.notifications(recipient_id, created_at desc, id desc);
create table public.parent_child_seen (
    parent_id uuid not null references public.profiles(id),
    student_id uuid not null references public.profiles(id),
    last_seen_at timestamptz not null,
    primary key(parent_id, student_id)
);
alter table public.parent_child_seen enable row level security;
revoke all on public.parent_child_seen from anon, authenticated;
grant all on public.parent_child_seen to service_role;
create table public.client_events (
    actor_id uuid not null references public.profiles(id),
    event_id uuid not null,
    kind text not null check(kind in ('error','vital')),
    code text not null,
    route text not null,
    value double precision,
    occurred_at timestamptz not null,
    received_at timestamptz not null default now(),
    primary key(actor_id, event_id)
);
alter table public.client_events enable row level security;
revoke all on public.client_events from anon, authenticated;
grant all on public.client_events to service_role;
create index client_events_received on public.client_events(received_at);
