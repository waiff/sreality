-- 389_location_w1_registry_lookup_indexes.sql
--
-- Location-data W1, resolve-drain latency. Three additive btree indexes on the
-- RUIAN mirror. NOT YET APPLIED to production -- this file ships with the query
-- rewrites in location_data/resolver/resolve_db.py and is applied separately,
-- when the instance is quiet, because two of the three build over the
-- 3,020,222-row ruian_address_points table.
--
-- WHY. The 2026-08-10 resolve drain (run 31439340945) measured itself:
-- registry_q=1887 misses costing registry_wait=228.1 s of a 416.3 s batch, i.e.
-- ~121 ms per registry lookup, with ~97 % of the batch's "core" bucket being
-- round-trip wait rather than CPU. EXPLAIN (ANALYZE, BUFFERS) against the live
-- mirror found the cost is not the round trip -- it is the queries.
--
-- The two worst were QUERY SHAPE faults, fixed in resolve_db.py rather than
-- here, because the right index already existed and the query was addressing the
-- wrong column (obec_kod / cast_obce_kod instead of the indexed obec_unit_id /
-- cast_obce_unit_id):
--   address_points_by_number   21,494 ms -> 25 ms  (2,954,453 -> 424 buffers)
--   cast_obce_extent_m          5,059 ms -> 35 ms  (   79,905 -> 167 buffers)
--   containing_obec               194 ms -> 19 ms  (    1,225 ->  25 buffers)
-- An index is only added where no rewrite can help, which is the case below.
--
-- All three are additive: CREATE INDEX only, no table or column touched, nothing
-- dropped. The mirror is written by the monthly registry load and by nothing
-- else, so a plain (non-CONCURRENT) build inside a transaction cannot block live
-- ingest the way it would on `listings`; lock_timeout still fails fast rather
-- than queueing behind the loader if one happens to be running.

begin;

set local lock_timeout = '5s';
-- Two of these build over 3.02 M rows. Bounded, not 0: a build that has not
-- finished in 30 minutes is contending with something and should say so.
set local statement_timeout = '30min';

------------------------------------------------------------------
-- 1. Gazetteer name lookup (resolve_db._ADMIN_BY_NAME_SQL).
------------------------------------------------------------------
-- `n.name_norm = %s AND n.registry_version_id = %s` had no btree to use: the
-- unique index is (entity_kind, entity_id, name_norm, name_kind,
-- registry_version_id), whose leading columns are not the ones bound, and
-- ruian_name_homonym is partial on homonym_count > 1. So the planner fell back
-- to the GIN TRIGRAM index for an EQUALITY -- `Bitmap Index Scan on
-- ruian_name_norm_trgm ... Rows Removed by Index Recheck: 52` for one matching
-- row, 12.7 ms warm on a rare name and worse the shorter and commoner the name
-- gets, because a trigram scan's cost tracks the posting lists of the name's
-- trigrams and not the number of answers. The trigram index stays -- fuzzy name
-- search is what it is for; this one just serves the exact lookup.
create index if not exists ruian_name_index_norm_version
  on ruian_name_index (name_norm, registry_version_id);

------------------------------------------------------------------
-- 2. PSC -> obec fan-out (resolve_db._PSC_OBEC_SQL).
------------------------------------------------------------------
-- `SELECT DISTINCT obec_kod ... WHERE psc = %s AND valid_to IS NULL` used
-- ruian_ap_psc (psc) and then went to the heap for obec_kod on every matching
-- row: 2,860 heap rows and 260 buffers for ONE answer on PSC 11000, 101.8 ms.
-- Carrying obec_kod in the index makes it index-only, and the partial predicate
-- keeps it to the live rows the query asks for. Dense PSCs are several times
-- larger than this sample, and this lookup runs per listing that carries a
-- postcode claim.
create index if not exists ruian_ap_psc_obec
  on ruian_address_points (psc, obec_kod)
  where valid_to is null;

------------------------------------------------------------------
-- 3. Street name equality (resolve_db._ADDRESS_POINTS_BY_NUMBER_SQL).
------------------------------------------------------------------
-- Same trigram-for-equality fault as #1: `s.name_norm = %s` with no obec bound
-- has only ruian_streets_norm_trgm to use (ruian_streets_obec_norm leads on
-- obec_unit_id, which this arm does not constrain -- deliberately, because
-- narrowing the street to the listing's obec would be a semantic change this
-- migration has no business making). Measured: 41 buffers and a 32-row
-- nationwide match for 'vinohradska'. A plain btree answers the same question
-- without the recheck.
create index if not exists ruian_streets_name_norm
  on ruian_streets (name_norm);

commit;
