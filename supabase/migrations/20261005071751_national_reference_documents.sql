-- Keep official PDF sources and reviewed CP provenance separate from school materials.
create table public.national_reference_documents (
    id uuid primary key,
    kind text not null check (kind in ('curriculum', 'guidance')),
    title text not null,
    issuer text not null,
    source_url text not null,
    sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
    storage_path text not null unique,
    uploaded_by uuid not null references public.profiles(id),
    status text not null default 'uploading'
        check (status in ('uploading','extracting','review','indexing','published','failed')),
    revision integer not null default 0,
    review jsonb,
    reviewed_by uuid references public.profiles(id),
    reviewed_at timestamptz,
    published_at timestamptz,
    curriculum_version_id uuid references public.cp_versions(id),
    job_id uuid references public.jobs(id),
    lease_token uuid,
    lease_until timestamptz,
    error_code text,
    created_at timestamptz not null default now(),
    unique (kind, sha256)
);
create table public.national_reference_pages (
    document_id uuid not null references public.national_reference_documents(id),
    page_number integer not null check (page_number > 0),
    text text not null,
    primary key(document_id, page_number)
);
alter table public.national_reference_documents enable row level security;
alter table public.national_reference_pages enable row level security;
revoke all on public.national_reference_documents, public.national_reference_pages
    from public, anon, authenticated;
grant all on public.national_reference_documents, public.national_reference_pages to service_role;

alter table public.cp_learning_outcomes
    add column source_document_id uuid references public.national_reference_documents(id),
    add column source_page_start integer,
    add column source_page_end integer,
    add constraint cp_outcome_source_pages check (
        (source_document_id is null and source_page_start is null and source_page_end is null)
        or (source_document_id is not null and source_page_start is not null
            and source_page_end is not null and source_page_start > 0
            and source_page_end >= source_page_start));
alter table public.teaching_materials
    add column national_reference_id uuid references public.national_reference_documents(id),
    add column national_source_pages integer[],
    add constraint teaching_material_reference_pages check (
        (national_reference_id is null and national_source_pages is null)
        or (national_reference_id is not null and national_source_pages is not null
            and cardinality(national_source_pages) > 0));
create unique index teaching_material_reference_once
    on public.teaching_materials(knowledge_base_id, national_reference_id)
    where national_reference_id is not null;

create function public.keep_published_national_reference() returns trigger
language plpgsql set search_path = public as $$
begin
    if tg_table_name = 'national_reference_pages' then
        if exists (select 1 from national_reference_documents
                   where id = case when tg_op = 'INSERT' then new.document_id else old.document_id end
                   and status = 'published') then
            raise exception 'Published reference pages are immutable';
        end if;
    elsif old.status = 'published' then
        raise exception 'Published references are immutable';
    end if;
    if tg_op = 'DELETE' then return old; end if;
    return new;
end;
$$;
revoke all on function public.keep_published_national_reference() from public, anon, authenticated;
create trigger national_reference_immutable before update or delete
    on public.national_reference_documents for each row
    execute function public.keep_published_national_reference();
create trigger national_reference_pages_immutable before insert or update or delete
    on public.national_reference_pages for each row
    execute function public.keep_published_national_reference();
