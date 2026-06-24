-- Dedup v2: make dedup_pair_audit the SINGLE terminal-decision log (merged /
-- dismissed), written by BOTH the engine and the operator review actions, and
-- carrying the undo handle. Queued pairs are NOT audited — a queued pair IS a
-- property_identity_candidate (the review queue), so logging it here every run
-- only produced duplicate rows. Decision history reads merged/dismissed; Needs
-- review reads the candidates.
--
--   source         — 'engine' (autonomous run) | 'operator' (a /dedup action)
--   merge_group_id  — for a merged row, the property_merge_events group id, so the
--                     UI can undo inline (text: the SPA + unmerge endpoint use the
--                     string form; the ledger stays the reversible source of truth)
-- The `detail` jsonb gains the decision FACTORS (stage, phash_pairs, cosine,
-- verdict, room_type, the driving image ids) — no schema change, just richer rows.

alter table dedup_pair_audit
  add column if not exists source         text,
  add column if not exists merge_group_id text;

create index if not exists dedup_pair_audit_merge_group_idx
  on dedup_pair_audit (merge_group_id);

-- Drop the historical 'queued' rows: that state now lives only in the review queue.
delete from dedup_pair_audit where outcome = 'queued';
