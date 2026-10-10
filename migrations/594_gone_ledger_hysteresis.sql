-- 594: the ledger behind rule #3's hysteresis (2026-10-09).
--
-- A page check's "gone" verdict no longer closes an ad by itself. The drain records the verdict
-- and flips the listing only when the ledger already holds an EARLIER gone verdict for it that
-- is at least delist_policy.GONE_DWELL (12 h) old and newer than the row's last sighting
-- (db.gone_evidence; a sighting refutes the earlier verdict). Two changes to the ledger:
--
-- 1. `outcome` says what the drain DID. 'gone' now means "verdict recorded, ad left active" --
--    the evidence a later verdict confirms -- and the new 'flipped' means "a confirming verdict
--    closed the ad". Without the split a false flip is unmeasurable once the ad is revived
--    (reactivation clears listings.inactive_at), and the pipeline check `false_delist_share`
--    reads exactly the 'flipped' rows that are active again.
-- 2. One point lookup per gone verdict (db.gone_evidence): (source, native_id) over the gone
--    rows. The existing (source, completed_at) index serves the latency view and the retention
--    delete, not this; the partial index keeps the new one to the gone rows.
--
-- Why: measured over 2026-10-02..09, 35% of sreality's 18,105 gone verdicts were active again
-- within the week, and a live re-probe put 62% of one night's batch back on the portal nine
-- hours later -- sreality takes an ad down (index AND detail) for hours around its nightly
-- expiry/renewal and brings it back under the same id. One verdict is a point observation.
alter table detail_queue_completions
  drop constraint detail_queue_completions_outcome_check;
alter table detail_queue_completions
  add constraint detail_queue_completions_outcome_check
  check (outcome in ('written', 'gone', 'flipped', 'given_up'));

create index if not exists detail_queue_completions_gone_native_idx
  on detail_queue_completions (source, native_id, completed_at)
  where outcome = 'gone';
