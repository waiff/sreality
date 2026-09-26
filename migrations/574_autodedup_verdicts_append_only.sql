-- 574_autodedup_verdicts_append_only.sql
--
-- AUTODEDUP: the operator's rulings become a ledger (Decision 8: "each correction keeps the
-- evidence the engine saw at that moment"). Until now `autodedup.verdicts` held ONE row per
-- (pair, decider) and per (group key, pass, decider): migration 528's and 538's partial unique
-- indexes made every re-ruling by the same login an in-place UPDATE, so a flip or a withdrawal
-- erased the ruling it replaced. With the two indexes gone, every change is a NEW row and the
-- newest row per pair / per (group key, pass) is the ruling -- which is how every reader has
-- always read the table (`incremental_sql.RT_MUST_LINK_SQL`, `apply_sql.PAIR_VERDICTS_SQL`,
-- `labels_sql.PAIR_VERDICTS_SQL`, `ui_sql.OPERATOR_PAIR_VERDICTS_SQL`: DISTINCT ON the key,
-- `decided_at desc, id desc`). The rulings page (/autodedup/rulings, E919) writes flips and
-- withdrawals this way.
--
-- No row moves and no column changes. It DROPS two uniqueness rules, so it goes through the
-- database gate as a destructive change: operator OK before it is applied.
--
-- ORDER: merge the code first, then apply this. The code's writes
-- (`ui_sql.VERDICT_PAIR_APPEND_SQL` / `VERDICT_CLUSTER_APPEND_SQL`) work on either side of it;
-- the code on main before that PR spells `ON CONFLICT (kind, listing_lo, listing_hi,
-- decided_by)`, which raises 42P10 on every operator write the moment these indexes are gone.
--
-- The two new indexes serve the newest-per-key reads (the DISTINCT ON of every reader and the
-- write's own "what is the newest row for this key" probe); they are created before the unique
-- indexes they replace are dropped.

set lock_timeout = '5s';

create index if not exists autodedup_verdicts_pair_newest_idx
  on autodedup.verdicts (listing_lo, listing_hi, decided_at desc, id desc)
  where kind = 'pair';

create index if not exists autodedup_verdicts_cluster_newest_idx
  on autodedup.verdicts (cluster_key, (coalesce(generation, ''::text)), decided_at desc, id desc)
  where kind = 'cluster';

drop index if exists autodedup.autodedup_verdicts_pair_uidx;
drop index if exists autodedup.autodedup_verdicts_cluster_gen_uidx;

comment on table autodedup.verdicts is
  'The operator''s rulings, append-only since migration 574: a flip or a withdrawal (verdict '
  '''unsure'') is a NEW row, and per pair / per (cluster_key, generation) the newest row '
  '(decided_at desc, id desc) is the ruling every reader obeys.';

reset lock_timeout;
