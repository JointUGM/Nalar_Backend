-- S1 detection output the contract exposes (GET /knowledge-bases/{id}, .../sections):
-- which sections detection pre-ticks, and which pages had no extractable text.
alter table public.material_sections
  add column if not exists suggested boolean not null default false;
alter table public.teaching_materials
  add column if not exists pages_without_text integer[] not null default '{}';
