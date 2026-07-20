-- 328_dual_write_watermark_rls.sql
-- Follow-up to 326: turn RLS on for dual_write_watermark.
--
-- 326 revoked anon/authenticated grants but left RLS off, which the migration-RLS
-- CI gate correctly rejects: this project's default privileges auto-GRANT new
-- tables, so an RLS-off public table is one stray future GRANT away from being
-- readable. Defence in depth — revoking today does not protect the table from
-- tomorrow's default ACL.
--
-- No policy is added on purpose. With RLS enabled and no policy, every non-superuser
-- role sees nothing; the service role (BYPASSRLS) still reads and writes it, which
-- is exactly the access this table needs — verify_pipeline and the backfill script
-- both connect as service role.
--
-- A forward migration rather than an edit to 326: migrations are append-only, and
-- 326 is already applied in production.

ALTER TABLE dual_write_watermark ENABLE ROW LEVEL SECURITY;
