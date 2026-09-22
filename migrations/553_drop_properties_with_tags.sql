-- 553_drop_properties_with_tags.sql
--
-- The `properties_with_tags(tag_ids)` RPC (migration 202) has no caller. It
-- powered Browse's tags prefilter until PR #1576 moved that resolver onto the
-- same complete-or-throw membership read the `collections` filter uses
-- (property_tags_public, AND computed in the client): the function body carried
-- `limit 5000` under a client comment asserting the allowlist was exhaustive, so
-- past 5,000 tagged properties it silently bled excluded listings back into the
-- cohort. Nothing in api/, toolkit/, tests/ or the SPA references it now.
--
-- Removal, so it applies AFTER the #1576 deploy (a removal applied before the
-- SPA stops calling it would 404 the tags facet). No data is touched: the
-- function reads property_tags, which stays. Grant to anon was already revoked
-- wholesale by migration 299; `drop function` removes the authenticated EXECUTE
-- with it.

drop function if exists public.properties_with_tags(bigint[]);
