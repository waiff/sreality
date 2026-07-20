-- 326_dual_write_watermark.sql
-- R2 Phase A3 of the listing-identity refactor
-- (docs/design/listing-identity-r2-pk-swap-runbook.md § 2 A3).
-- The anchor that makes the dual-write parity check meaningful.
--
-- Parity has to answer two different questions that look identical in the data:
--   "this old row has no listing_id yet"      -> the backfill has not reached it (fine)
--   "this NEW row has no listing_id"          -> a writer is missing dual-write (bug)
-- A bare `listing_id IS NULL` count cannot tell them apart, and would sit red for
-- days during the backfill while hiding the one condition worth alarming on.
--
-- One row per carrier, capturing where the table's monotonic cursor stood at the
-- moment dual-write went live. Everything above that watermark was written by
-- dual-write code and MUST carry the surrogate. Armed AFTER the deploy, never
-- before: rows written between arming and deploy would otherwise sit above the
-- watermark while still coming from old code, and alarm falsely. Arming late is
-- safe (those rows just look like backfill work), arming early is not.
--
-- Two cursor axes because two carriers have no usable bigint id:
-- notification_dispatches has a uuid PK (not monotonic) and estimation_cohort_entries
-- has none at all — both use a timestamp column instead.
--
-- Service-role only. This project's default privileges auto-GRANT new tables to
-- anon/authenticated, so the revoke is explicit and load-bearing, not decoration.

SET lock_timeout = '3s';

CREATE TABLE IF NOT EXISTS dual_write_watermark (
    child       text PRIMARY KEY,
    legacy_col  text NOT NULL,
    new_col     text NOT NULL,
    cursor_col  text NOT NULL,
    cursor_id   bigint,
    cursor_ts   timestamptz,
    armed_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT dual_write_watermark_one_axis
        CHECK (num_nonnulls(cursor_id, cursor_ts) = 1)
);

COMMENT ON TABLE dual_write_watermark IS
    'R2 dual-write parity anchor: per carrier, where its cursor stood when dual-write '
    'deployed. Rows above it must carry the surrogate; rows below are backfill work.';

REVOKE ALL ON TABLE dual_write_watermark FROM anon, authenticated;
