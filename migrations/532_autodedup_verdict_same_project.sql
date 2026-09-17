-- 532: admit `same_project_different_unit` on autodedup.verdicts.
--
-- WHY. The operator reviewing a proposed group found a shape the four stored values cannot
-- say: one advert is a DIFFERENT BUILDING of the SAME DEVELOPMENT PROJECT, two others are the
-- same unit, and two more are different units of that same project. `different` throws the
-- project away and `same_building_different_unit` claims a building the adverts do not share —
-- so the fifth value records the relation the operator actually saw. Like the other two
-- negatives it is a permanent must-not-link at the API (E49): a unit the operator says is a
-- different unit must never come back as a merge proposal.
--
-- Migration 528 spelled the verdict domain as an INLINE check, which Postgres names for you
-- (`verdicts_verdict_check`). The name is generated, so this file LOOKS IT UP in pg_constraint
-- — every check constraint of the table that covers the `verdict` column — instead of guessing
-- it, and re-adds a NAMED one so the next widening does not have to look anything up. Applying
-- the file twice is a no-op: the loop drops whatever is there, named or generated.
--
-- ADDITIVE in effect: the accepted set only grows, so no stored row can fail the re-add and the
-- validation scan reads rows it is already allowed to keep. `lock_timeout` is still set — the
-- swap takes an ACCESS EXCLUSIVE lock and must not queue behind a long reader.

set lock_timeout = '5s';

do $$
declare
  constraint_name text;
begin
  for constraint_name in
    select c.conname
      from pg_constraint c
      join pg_attribute a
        on a.attrelid = c.conrelid
       and a.attname = 'verdict'
       and a.attnum = any (c.conkey)
     where c.conrelid = 'autodedup.verdicts'::regclass
       and c.contype = 'c'
  loop
    execute format('alter table autodedup.verdicts drop constraint %I', constraint_name);
  end loop;
end
$$;

alter table autodedup.verdicts
  add constraint autodedup_verdicts_verdict_ck
  check (verdict in ('same', 'different', 'same_building_different_unit',
                     'same_project_different_unit', 'unsure'));
