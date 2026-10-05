-- Fence both sides of page moves and require CP citations to reference extracted pages.
create or replace function public.keep_published_national_reference() returns trigger
language plpgsql set search_path = public as $$
begin
    if tg_table_name = 'national_reference_pages' then
        if tg_op <> 'INSERT' and exists (
            select 1 from national_reference_documents
            where id = old.document_id and status = 'published'
        ) then
            raise exception 'Published reference pages are immutable';
        end if;
        if tg_op <> 'DELETE' and exists (
            select 1 from national_reference_documents
            where id = new.document_id and status = 'published'
        ) then
            raise exception 'Published reference pages are immutable';
        end if;
    elsif old.status = 'published' then
        raise exception 'Published references are immutable';
    end if;
    if tg_op = 'DELETE' then return old; end if;
    return new;
end;
$$;
alter table public.cp_learning_outcomes
    add constraint cp_source_start_exists foreign key (source_document_id, source_page_start)
        references public.national_reference_pages(document_id, page_number),
    add constraint cp_source_end_exists foreign key (source_document_id, source_page_end)
        references public.national_reference_pages(document_id, page_number);
