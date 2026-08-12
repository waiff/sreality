-- 394: pin PostgREST's db-max-rows in git (50,000 = the SPA's MAP_CAP).
--
-- Why this exists: db-max-rows silently clamps EVERY PostgREST response.
-- This project ran on Supabase's 1,000-row default long enough to ship two
-- real silent-truncation bugs (the city-index "~32 cities" popup, the
-- rent-map choropleth), then the cap was lifted OUT-OF-BAND in the dashboard
-- so the 50k-point Browse map could do single-shot reads — leaving frontend
-- correctness resting on an invisible, unversioned setting that a project
-- restore or a Supabase preview branch would quietly revert to 1,000.
--
-- This migration makes the value versioned config. 50,000 = frontend
-- MAP_CAP (frontend/src/lib/queries.ts), the largest sanctioned single
-- request (the map communicates overflow via its `capped` flag). Everything
-- larger pages through frontend/src/lib/fetchAllRows.ts, whose termination
-- is deliberately correct under ANY cap value — the two layers never have to
-- move together. If the dashboard's API "Max Rows" field is ever edited it
-- writes this same role setting; keep the two at 50,000 (this statement is
-- idempotent and simply restates it on replay).
alter role authenticator set pgrst.db_max_rows = '50000';

-- PostgREST reloads role-based config on this notification (no restart).
notify pgrst, 'reload config';
