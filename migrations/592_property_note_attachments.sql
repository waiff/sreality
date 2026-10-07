-- 592_property_note_attachments.sql -- files attached to an operator note (the property page's
-- Notes section). ADDITIVE: one table, its account trigger, RLS + grants; nothing existing changes.
--
-- NOTE-GRAIN, not property-grain: a row names its note, never a property, so it rides the note
-- through every merge (the note carrier keeps the note id, rule #18) and a split's MOVE; a split's
-- COPY makes a new note, and toolkit.property_carriers copies that note's attachment rows onto it
-- (same storage_key: the bytes are shared, never deleted). Deleting the note deletes its rows.
-- No property_id column and no FK to properties, so the carrier census has nothing to classify.
--
-- The bytes live in R2 under custom-attachments/note/<note_id>/<uuid><ext> (the private prefix
-- the public /images/{key} route can never presign); the API proxies them to the SPA. R2 objects
-- are never deleted, so storage_key is not unique: a split's copy points at the same object.
--
-- Tenancy: the 292 child-grain shape. account_id is derived from the parent note by a BEFORE
-- trigger running as the invoker, so a tenant attaching to another account's note reads NULL
-- through the note's RLS and fails WITH CHECK (fails closed); the route names no account.
-- Verify:
--   select relrowsecurity from pg_class where oid = 'public.property_note_attachments'::regclass;  -- t
--   select has_table_privilege('authenticated', 'public.property_note_attachments', 'INSERT');  -- t

begin;

create table if not exists property_note_attachments (
  id          bigserial primary key,
  note_id     bigint not null references property_notes(id) on delete cascade,
  account_id  uuid references accounts(id) on delete cascade,
  storage_key text not null,
  filename    text not null check (char_length(filename) between 1 and 255),
  mime_type   text not null,
  byte_size   integer not null check (byte_size > 0),
  sha256_hex  text not null check (sha256_hex ~ '^[0-9a-f]{64}$'),
  created_at  timestamptz not null default now(),
  unique (note_id, sha256_hex)
);
create index if not exists property_note_attachments_note_idx
  on property_note_attachments (note_id, created_at);
create index if not exists property_note_attachments_account_id_idx
  on property_note_attachments (account_id);

alter table property_note_attachments enable row level security;

create or replace function sync_property_note_attachments_account_id()
returns trigger language plpgsql as $$
begin
  select account_id into new.account_id from property_notes where id = new.note_id;
  return new;
end;
$$;
revoke execute on function sync_property_note_attachments_account_id() from public, anon;
drop trigger if exists property_note_attachments_account_id_biu on property_note_attachments;
create trigger property_note_attachments_account_id_biu
  before insert or update of note_id on property_note_attachments
  for each row execute function sync_property_note_attachments_account_id();

revoke all on property_note_attachments from anon, authenticated;
grant select, insert, delete on property_note_attachments to authenticated;
grant usage on sequence property_note_attachments_id_seq to authenticated;

drop policy if exists property_note_attachments_tenant_rw on property_note_attachments;
create policy property_note_attachments_tenant_rw on property_note_attachments
  for all to authenticated
  using (account_id in (select current_account_ids()))
  with check (account_id in (select current_account_ids()));

comment on table property_note_attachments is
  'Files attached to an operator note (migration 592). Note-grain: follows its note through '
  'merges; a split''s note copy copies these rows. Bytes in R2 (custom-attachments/note/...), '
  'never deleted; account_id derived from the note by trigger.';

commit;
