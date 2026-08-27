-- 449: park ceskereality's delisting authority while its walk is rebuilt.
--
-- ceskereality's index walk has just been rebuilt onto a declared 14-kraj partition with
-- real deep pagination (the old walk scraped whatever filter links the page rendered -- a
-- top-10-by-popularity list, not a partition -- and refused to request page 13 of anything).
-- The rebuild is expected to report categories COMPLETE for the first time in months.
--
-- That is exactly the danger. `mark_inactive` fires when a walk reports complete, and it has
-- no cap on how many rows one sweep may flip. ceskereality currently holds 77,833 active rows
-- against 48,235 the portal declares -- roughly 29,400 adverts that have not been seen by any
-- walk in over 30 days. The instant the rebuilt walk says "complete", every one of those
-- becomes eligible in a single pass.
--
--   The fix and the hazard are THE SAME EVENT.
--
-- Absence from a first run of brand-new code is the weakest evidence there is, and the
-- portal's degraded response is a 200 with zero cards -- indistinguishable, page-shape-wise,
-- from a genuinely empty region. An adversarial review reproduced complete=true with 5,200 of
-- 5,600 rows collected and a whole kraj silently missing. Those specific holes are now closed
-- (the national cross-check fails closed; an empty slice must confirm on a second read), but
-- "we closed the holes we found" is not the same as "the walk is proven", and the operator has
-- explicitly chosen the careful path: verify each candidate by FETCHING it and delist only on
-- a real 404/410 from the portal, never on absence from our own walk.
--
-- So the flag comes down first and the rebuild ships dark. Un-parking is a separate, deliberate
-- act gated on three things:
--   (a) 3 consecutive full walks with every category reporting complete and zero negative slice
--       outcomes, run through the production proxy;
--   (b) the delist-candidate set stable across all three (a row that appears in one walk and not
--       another is a throttle artifact, not a dead listing);
--   (c) a per-listing verification fetch confirming gone for each row actually flipped.
--
-- Reversible in one statement; the walk keeps collecting throughout. Over-retention is the safe
-- failure direction -- a stale row is visible and self-heals on next sighting (touch_listings),
-- a wrongly-deleted live listing is not.

UPDATE public.portals
   SET supports_complete_walk = false
 WHERE source = 'ceskereality';

COMMENT ON COLUMN public.portals.supports_complete_walk IS
    'Can this portal prove a near-complete index walk? Gates mark_inactive (architectural '
    'rule #3): a portal that cannot prove completeness never delists from index absence. '
    'ceskereality was parked false in migration 449 while its walk was rebuilt onto the '
    '14-kraj partition -- not because the portal cannot prove a walk, but because the rebuild '
    'makes ~29,400 rows delist-eligible in one pass and that correction must be verified '
    'per-listing by fetch, not inferred from one run of new code.';
