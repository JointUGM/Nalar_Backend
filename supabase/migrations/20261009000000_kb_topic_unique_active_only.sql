-- A deleted (archived) topic frees its name so the teacher can upload it again.
drop index if exists public.knowledge_bases_subject_topic_key;
create unique index knowledge_bases_subject_topic_key
  on public.knowledge_bases (school_subject_id, topic_key)
  where archived_at is null;
