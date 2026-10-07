-- AI-drafted CP review, stored beside the human review and never inside it.
-- draft_job_id fences stale or duplicate draft jobs (only the latest job may write).
alter table public.national_reference_documents
    add column draft jsonb,
    add column draft_report jsonb,
    add column draft_status text check (draft_status in ('pending','ready','failed','skipped')),
    add column draft_job_id uuid references public.jobs(id),
    add column draft_error text,
    add constraint reference_draft_state check (
        (draft_status is null and draft_job_id is null)
        or (draft_status is not null and draft_job_id is not null));
