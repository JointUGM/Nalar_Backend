create extension if not exists pgmq;
create extension if not exists pg_cron;

do $$
declare q text;
begin
  foreach q in array array['nalar_kb', 'nalar_eval', 'nalar_default'] loop
    if not exists (select 1 from pgmq.list_queues() where queue_name = q) then
      perform pgmq.create(q);
    end if;
  end loop;
end $$;

-- Ticks only; all logic is in the Python worker (B7). cron.schedule upserts by name.
select cron.schedule('nalar-tick-minute', '* * * * *',
  $$select pgmq.send('nalar_default', jsonb_build_object('kind', 'scheduler_tick'))$$);
select cron.schedule('nalar-weekly-digest', '0 0 * * 1',        -- Monday 07:00 WIB
  $$select pgmq.send('nalar_default', jsonb_build_object('kind', 'weekly_digest'))$$);
select cron.schedule('nalar-release-reminders', '0 1 * * *',    -- daily 08:00 WIB
  $$select pgmq.send('nalar_default', jsonb_build_object('kind', 'release_reminders'))$$);
