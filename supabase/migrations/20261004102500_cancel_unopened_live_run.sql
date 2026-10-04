-- Cancelling before lobby admission must not invent a join code.
alter table public.publication_runs drop constraint publication_runs_live_code_after_scheduled;
alter table public.publication_runs add constraint publication_runs_live_code_after_scheduled
    check (mode <> 'live' or status in ('scheduled', 'closed') or join_code is not null);
