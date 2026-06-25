-- 237_lock_down_image_tag_tables.sql
--
-- The CLIP / room-classifier per-image tag tables are BACKEND-ONLY: the dedup
-- engine, the API, and the backfill read/write them as the table owner /
-- service_role (both bypass RLS); the browser reaches the tags ONLY through
-- images_public (migration 236). But all three shipped RLS-DISABLED while
-- carrying Supabase's default anon/authenticated DML grant — so the publishable
-- anon key inlined in the SPA + Chrome-extension bundle could read AND
-- INSERT/UPDATE/DELETE/TRUNCATE them via PostgREST. (Migration 225's
-- "no anon grant" comment was aspirational; the schema-wide default grant +
-- RLS-off meant it was never actually enforced. image_clip_embeddings — 512-d
-- vectors — being anon-truncatable is the worst of the three.)
--
-- Fix: ENABLE RLS (no anon policy ⇒ deny) AND REVOKE the stray anon/authenticated
-- grants, so images_public stays the sole, column-controlled anon path. The
-- owner role + service_role bypass RLS, so the dedup engine / API / clip_tag
-- backfill are unaffected (verified: every reader connects as the DB owner, not
-- anon). service_role grants are deliberately left intact.
--
-- image_clip_embeddings is created conditionally behind pgvector (migration 226),
-- so it may be absent in the CI migration-replay image (postgis/postgis, no
-- pgvector). Guard its statements on table existence; the table is present in
-- production. The other two tables exist unconditionally (migrations 225 / 128).

alter table image_clip_tags            enable row level security;
alter table image_room_classifications enable row level security;

revoke all on image_clip_tags            from anon, authenticated;
revoke all on image_room_classifications from anon, authenticated;

do $$
begin
  if exists (
    select 1 from information_schema.tables
    where table_schema = 'public' and table_name = 'image_clip_embeddings'
  ) then
    execute 'alter table image_clip_embeddings enable row level security';
    execute 'revoke all on image_clip_embeddings from anon, authenticated';
  end if;
end $$;
