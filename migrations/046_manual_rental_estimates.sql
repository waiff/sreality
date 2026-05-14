-- 046_manual_rental_estimates.sql
--
-- (Originally drafted as 043; renumbered because slot 043 was claimed
-- by 043_estimation_trace_payloads.sql on main's Phase AI slice A
-- before this branch landed. Renumbered before merge — the live DB
-- already has both migrations applied; this rename is filename-only.)
--
-- Operator-recorded manual rental estimates attached to listings.
--
-- One-to-many on sreality_id. A listing can carry estimates from
-- several signal sources (broker quote, portfolio benchmark, gut
-- number) and the operator may revise them. Mutable rows with full
-- audit history on UPDATE and DELETE — same shape as
-- app_settings + app_settings_history (migration 020), extended with
-- a delete trigger so hard-deletes still leave a trail.
--
-- Read path: manual_rental_estimates_public (anon select grant) for
-- the SPA. Write path: bearer-gated FastAPI routes in
-- api/manual_estimates.py.

create table manual_rental_estimates (
  id          bigserial   primary key,
  sreality_id bigint      not null references listings(sreality_id) on delete cascade,
  rent_czk    integer     not null check (rent_czk between 1000 and 1000000),
  author      text        not null check (length(author) between 1 and 120),
  source_kind text        not null
                check (source_kind in ('broker','gut','external_comp','portfolio','other')),
  notes       text        check (notes is null or length(notes) between 1 and 4000),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  updated_by  text
);

create index manual_rental_estimates_by_listing
  on manual_rental_estimates (sreality_id, created_at desc);

alter table manual_rental_estimates enable row level security;

create table manual_rental_estimates_history (
  id           bigserial   primary key,
  estimate_id  bigint      not null,
  sreality_id  bigint      not null,
  rent_czk     integer     not null,
  author       text        not null,
  source_kind  text        not null,
  notes        text,
  change_kind  text        not null check (change_kind in ('update','delete')),
  replaced_at  timestamptz not null default now(),
  replaced_by  text
);

create index on manual_rental_estimates_history (estimate_id, replaced_at desc);

alter table manual_rental_estimates_history enable row level security;

create or replace function manual_rental_estimates_record_history()
returns trigger
language plpgsql
as $$
begin
  insert into manual_rental_estimates_history (
    estimate_id, sreality_id, rent_czk, author, source_kind, notes,
    change_kind, replaced_at, replaced_by
  ) values (
    old.id, old.sreality_id, old.rent_czk, old.author, old.source_kind, old.notes,
    case when TG_OP = 'DELETE' then 'delete' else 'update' end,
    now(), old.updated_by
  );
  if TG_OP = 'DELETE' then
    return old;
  else
    return new;
  end if;
end;
$$;

create trigger manual_rental_estimates_history_update
  before update on manual_rental_estimates
  for each row
  when (
    old.rent_czk    is distinct from new.rent_czk    or
    old.author      is distinct from new.author      or
    old.source_kind is distinct from new.source_kind or
    old.notes       is distinct from new.notes
  )
  execute function manual_rental_estimates_record_history();

create trigger manual_rental_estimates_history_delete
  before delete on manual_rental_estimates
  for each row
  execute function manual_rental_estimates_record_history();

create view manual_rental_estimates_public as
  select id, sreality_id, rent_czk, author, source_kind, notes,
         created_at, updated_at
  from manual_rental_estimates;

grant select on manual_rental_estimates_public to anon;
