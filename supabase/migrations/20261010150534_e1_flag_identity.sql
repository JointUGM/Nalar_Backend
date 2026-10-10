-- One E1 identity across live/final jobs; preserve existing review history.
do $$
declare
    duplicate_identity text;
begin
    select jsonb_build_object(
        'session_id', session_id,
        'flag_type', flag_type,
        'turn_id', turn_id,
        'flag_ids', array_agg(id order by created_at, id)
    )::text
    into duplicate_identity
    from authenticity_flags
    group by session_id, flag_type, turn_id
    having count(*) > 1
    order by session_id, flag_type, turn_id
    limit 1;

    if duplicate_identity is not null then
        raise exception 'Duplicate E1 flag identity prevents migration'
            using errcode = '23505',
                  detail = duplicate_identity,
                  hint = 'Review and reconcile duplicate records before retrying; preserve teacher decisions.';
    end if;
end;
$$;

alter table authenticity_flags
    add constraint authenticity_flags_identity
    unique nulls not distinct (session_id, flag_type, turn_id);
