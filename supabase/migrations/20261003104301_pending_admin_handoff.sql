-- Keep the incumbent school admin until the invited replacement activates.
alter table public.schools add column pending_admin_id uuid references public.profiles(id);
