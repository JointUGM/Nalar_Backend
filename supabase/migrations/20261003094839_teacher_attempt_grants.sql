-- Fence retried grant requests and allow one outstanding grant per student/publication.
alter table public.publication_runs
    add column grant_request_key uuid,
    add column grant_request_digest text,
    add constraint grant_request_fields check (
        (grant_request_key is null) = (grant_request_digest is null)
        and (grant_request_key is null or (kind = 'grant' and mode = 'window'))
    );
create unique index publication_runs_grant_request_key
    on public.publication_runs (publication_id, granted_by, grant_request_key)
    where grant_request_key is not null;
create unique index publication_runs_one_outstanding_grant
    on public.publication_runs (publication_id, grant_student_id)
    where kind = 'grant' and status in ('scheduled', 'lobby', 'open');
