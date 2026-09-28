-- New enum labels can't be used in the transaction that adds them, so they ship alone before 0005.
alter type public.run_status     add value if not exists 'lobby' before 'open';
alter type public.session_status add value if not exists 'paused_safety';
alter type public.session_status add value if not exists 'ended_safety';
