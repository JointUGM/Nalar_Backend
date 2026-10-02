-- S2 retry checkpoints and one generated draft per durable job.
alter table public.jobs add column if not exists result jsonb;
alter table public.mission_versions
    add column if not exists generation_job_id uuid references public.jobs(id);
create unique index if not exists mission_versions_generation_job_idx
    on public.mission_versions (generation_job_id) where generation_job_id is not null;
