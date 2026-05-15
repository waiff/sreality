-- 052_skill_versioning.sql
-- Add a monotonically increasing `version` int to `skills` and have
-- the skills_record_history trigger snapshot it into skills_history.
-- Also link estimation_runs back to the (skill_name, skill_version)
-- pair that produced each run so a future change to a skill prompt
-- doesn't invisibly muddy historical traces.
--
-- Captured retroactively as a documentation file: the live production
-- DB had this applied (supabase migration version 20260514081613) but
-- the migrations/ folder was missing it. Recovered verbatim from
-- supabase_migrations.schema_migrations. The backfill UPDATE below
-- runs again on a fresh rebuild — it's idempotent because the WHERE
-- clause checks `skill_name is null`.

alter table skills
  add column if not exists version int not null default 1;

alter table skills_history
  add column if not exists version int;

create or replace function skills_record_history()
returns trigger
language plpgsql
as $$
begin
  insert into skills_history (
    name, description, system_prompt, allowed_tools,
    preferred_model, limits, replaced_at, replaced_by,
    archived_at, version
  )
  values (
    old.name, old.description, old.system_prompt, old.allowed_tools,
    old.preferred_model, old.limits, now(), old.updated_by,
    old.archived_at, old.version
  );
  new.updated_at := now();
  new.version    := old.version + 1;
  return new;
end;
$$;

drop trigger if exists skills_history_trigger on skills;
create trigger skills_history_trigger
  before update on skills
  for each row
  execute function skills_record_history();

alter table estimation_runs
  add column if not exists skill_name    text default null,
  add column if not exists skill_version int  default null;

update estimation_runs
   set skill_name = (
     select s->'output_summary'->>'skill_name'
       from jsonb_array_elements(trace->'steps') s
      where s->>'kind'  = 'computation'
        and s->>'label' = 'skill_choice'
      limit 1
   )
 where mode = 'agent'
   and skill_name is null
   and trace is not null;
