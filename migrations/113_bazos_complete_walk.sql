-- 113_bazos_complete_walk.sql
--
-- Promote bazos from partial-walk pilot to a complete-walk portal so it can
-- detect delistings (mark_inactive, architectural rule #3). Two pieces:
--
--   1. A per-portal inactive-sweep throttle on the `portals` registry. The
--      index walk now runs frequently (touch last_seen + enqueue new), but the
--      index-absence delisting sweep is gated to run at most once per
--      `inactive_sweep_min_interval_hours` — a conservative window for an HTML
--      crawl that can be intermittently rate-limited, so a single hourly walk
--      can never mass-delist on a flaky fetch. `last_inactive_sweep_at` records
--      when the sweep last actually ran. These columns are generic (any portal
--      may opt in); only bazos uses them today. NULL interval falls back to a
--      code default (12h).
--
--   2. Flip bazos `supports_complete_walk = true` in the registry (the runtime
--      class attribute in BazosPortal is the robustness floor; this keeps the
--      operator-facing registry consistent and Health-correct). The per-walk
--      completeness guard still gates every flip: a truncated walk (collected
--      << reported total) skips the sweep, so this is safe.

alter table portals add column last_inactive_sweep_at timestamptz;
alter table portals add column inactive_sweep_min_interval_hours integer;

update portals
set supports_complete_walk = true,
    inactive_sweep_min_interval_hours = 12
where source = 'bazos';
