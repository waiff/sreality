-- 454: the index-slice ledger — what each portal's walk actually reached, and when.
--
-- THE PROBLEM IT SOLVES IS NOT SPEED, IT IS AMNESIA.
--
-- An index walk starts at the first category's first page every time it runs. When the budget
-- runs out it stops, and the next run starts from the same place. So a portal whose catalogue
-- is bigger than one run's budget does not get walked slowly -- it gets the SAME HEAD walked
-- over and over while the tail is never reached at all. idnes is the proof: 11 of its last 14
-- runs were killed by the clock, and the one that finished covered 2 of the 10 category pairs
-- we hold, reaching 13% of the biggest. The other 8 categories were not walked slowly. They
-- were not walked.
--
-- More requests do not fix that, and neither does a bigger budget: it just moves where the
-- restart happens. What fixes it is remembering. This table is that memory -- one row per
-- (source, category, slice), latest-wins, holding when the slice was last walked and how it
-- ended. A run orders its work by `walked_at NULLS FIRST`, so a slice never walked outranks
-- one walked an hour ago, and coverage becomes monotonic across runs instead of restarting.
--
-- WHY SLICES AND NOT CATEGORIES. A category is the wrong unit to prove anything about: idnes
-- `prodej/byty` is 27,372 rows over 1,053 pages, so "did we reach the end" is one question with
-- one answer for a fifth of the portal. Cut by the 14 kraje plus the abroad bucket, the same
-- category is 15 questions, each with its own declared total to check against -- and each small
-- enough to finish inside any plausible budget. Verified a true row-level partition on idnes by
-- ID enumeration (755 rows, 14 slices, zero overlap and zero gap), and kraj_sum + abroad equals
-- the national declared total on every one of the 10 categories.
--
-- WHAT `outcome` MEANS. Only 'exhausted' -- walked to the slice's own declared tail -- is
-- positive. 'deadline', 'error', 'degraded' and 'ceiling' are all MISSING EVIDENCE, and a
-- category is complete only when every one of its slices is 'exhausted' inside one freshness
-- window. That is the ledger's real job: `supports_complete_walk` stops being a standing claim
-- someone typed once and becomes a fact this table can be asked about (architectural rule #3).

CREATE TABLE IF NOT EXISTS public.portal_index_slices (
    source          text        NOT NULL,
    category_main   text        NOT NULL,
    category_type   text        NOT NULL,
    slice_key       text        NOT NULL,   -- a kraj slug, or '__abroad__'
    walked_at       timestamptz NOT NULL DEFAULT now(),
    outcome         text        NOT NULL,   -- exhausted | deadline | error | degraded | ceiling
    declared_total  integer,                -- what the slice said it held
    collected       integer,                -- what we actually enumerated
    pages           integer,
    first_walked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, category_main, category_type, slice_key)
);

COMMENT ON TABLE public.portal_index_slices IS
    'Index-walk coverage ledger (migration 454): one latest-wins row per portal slice, recording '
    'when it was last walked and whether it reached its own declared tail. Read to order a run '
    'least-recently-walked first (so coverage is monotonic across runs instead of restarting at '
    'page 1), and to answer whether a category was FULLY covered inside one freshness window -- '
    'the evidence behind supports_complete_walk / mark_inactive (architectural rule #3). Only '
    'outcome=''exhausted'' is positive; every other value is missing evidence.';

CREATE INDEX IF NOT EXISTS portal_index_slices_staleness_idx
    ON public.portal_index_slices (source, walked_at);

ALTER TABLE public.portal_index_slices ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.portal_index_slices FROM anon, authenticated;
