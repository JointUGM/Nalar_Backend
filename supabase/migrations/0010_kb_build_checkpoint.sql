-- Commit concept extraction with its checkpoint so retries never extract twice.
alter table public.material_sections add column concepts_built_at timestamptz;
