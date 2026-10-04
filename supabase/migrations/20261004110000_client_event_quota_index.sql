-- Keep per-actor admission checks bounded as event history grows.
create index client_events_actor_received on public.client_events(actor_id, received_at desc);
