-- 453: park idnes delisting. The portal cannot prove it saw everything, because WE have not.
--
-- `supports_complete_walk` is the operator's claim that a portal's index walk can be trusted to
-- have seen the whole catalogue, and it is what gates `mark_inactive` (architectural rule #3). For
-- idnes that claim has been true in the registry and false in reality for months.
--
-- MEASURED 2026-08-27:
--   * We hold 109,908 active idnes rows -- MORE than sreality (95,401). It is our largest source.
--   * 70,130 of them (64%) have not been seen in over 7 days. Not "probably sold": unknown.
--   * Of the last 14 index-walk runs, ELEVEN were killed by the job clock and one crashed.
--   * The one run that finished (08-26 13:17) covered 2 of the 10 category pairs we hold, and
--     reached 3,576 of the portal's declared 27,274 flats for sale -- 13%.
--   * It still delisted 768 rental flats off that walk, because the flag said it could.
--
-- The walk is not slow because the portal is large. It is slow because idnes soft-throttles our
-- egress: pages arrive in ~2.3s each for exactly 20 requests, then one request stalls for ~390
-- SECONDS with no 429, no error and no retry -- 24 such stalls consumed 143 of that run's 160
-- minutes. The same walk from a residential IP shows no stall at all. Migration 453 does not fix
-- that (the fix is the residential proxy the sister portals already ride, and a walk sliced into
-- units small enough to finish); it fixes the LIE, which is the part that costs data.
--
-- The rule is simply stated: a portal cannot prove it saw everything if we have not. Un-park only
-- when the slice ledger shows every slice of every category walked to its own declared tail inside
-- one freshness window, repeatedly -- not because a single run happened to finish.

UPDATE public.portals
   SET supports_complete_walk = false
 WHERE source = 'idnes';

COMMENT ON COLUMN public.portals.supports_complete_walk IS
    'Can this portal prove a near-complete index walk? Gates mark_inactive (architectural '
    'rule #3): a portal that cannot prove completeness never delists from index absence. '
    'This is a claim about OUR walk, not about the portal: ceskereality was parked in migration '
    '449 while its walk was rebuilt onto the 14-kraj partition, and idnes in migration 453 '
    'because 64% of the 109,908 rows we hold had not been seen in a week while the flag still '
    'authorised delisting. Un-park only on repeated, ledger-proven full coverage.';
