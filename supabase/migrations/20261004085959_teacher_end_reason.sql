-- Distinguish an incomplete teacher-ended session from a time limit.
alter type public.session_end_reason add value if not exists 'teacher_ended';
