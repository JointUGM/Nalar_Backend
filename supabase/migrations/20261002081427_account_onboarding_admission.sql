-- U-1: persist onboarding eligibility and one current email invitation per account.
alter table public.profiles
    add column onboarding_required boolean not null default false;

alter table public.account_activations
    add column superseded_at timestamptz,
    add column recipient_email text;

create unique index account_activations_pending_email_idx
    on public.account_activations (user_id)
    where channel = 'email' and consumed_at is null and superseded_at is null;
