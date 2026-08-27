-- 451: a hard ceiling on how much of a category one sweep may delist, and a record when it refuses.
--
-- `mark_inactive` has never had a cap. It flips every active row of a (source, category_main,
-- category_type) that the walk did not see, however many that is, in one statement. That was
-- survivable only because the completeness gate kept the dangerous cases from ever running --
-- which is not a safety property, it is a coincidence.
--
-- The coincidence is now ending. ceskereality's walk has been rebuilt onto a proven 14-kraj
-- partition, and it will report categories complete for the first time in months. The moment
-- its delisting flag is un-parked, ~29,400 rows become eligible in a single pass: the portal
-- declares 48,235 and we hold 78,718 active. idnes has the identical exposure the first time
-- its walk ever completes -- 9 of its last 12 runs covered ZERO categories.
--
--   The event that FIXES coverage is the same event that authorises the mass flip.
--
-- So the cap is not about this portal. It is the standing answer to "a gate that has been shut
-- for months just opened", which will happen again on every portal we repair. A sweep larger
-- than `fraction` of a category's live rows is not routine churn; it is a claim that the market
-- moved by that much overnight, and that claim should interrupt a human rather than execute.
--
-- Refusing is safe in the direction that matters. An unswept stale row is visible, queryable,
-- and self-heals the moment the listing is seen again (touch_listings). A wrongly-delisted live
-- listing is invisible to Browse, the watchdog and every estimate, and nothing re-surfaces it.
--
-- The floor exists so the cap polices catastrophes, not small categories: 2% of a 200-row
-- category is 4, which ordinary churn would trip weekly. Below `min_rows` the cap never fires.
--
-- This table is the alarm surface. `mark_inactive` writing a log line would leave the refusal in
-- an Actions log that expires, and the whole lesson of this sprint is that a signal nothing can
-- query is a signal nobody receives.

CREATE TABLE IF NOT EXISTS public.delist_flip_refusals (
    id             bigserial PRIMARY KEY,
    refused_at     timestamptz NOT NULL DEFAULT now(),
    source         text        NOT NULL,
    category_main  text,
    category_type  text,
    subtype        text,
    candidates     integer     NOT NULL,   -- rows the sweep would have flipped
    active_rows    integer     NOT NULL,   -- live rows in that scope at refusal time
    cap            integer     NOT NULL    -- the ceiling that was exceeded
);

COMMENT ON TABLE public.delist_flip_refusals IS
    'One row each time a delisting sweep was REFUSED for exceeding the per-category flip cap '
    '(migration 451). Append-only, operator-facing: a row here means a walk believed a large '
    'share of a category had vanished and was stopped. Investigate before raising the cap -- '
    'the expected cause is a completeness gate re-opening after a long block, which needs '
    'per-listing verification by fetch, not a bigger ceiling.';

CREATE INDEX IF NOT EXISTS delist_flip_refusals_recent_idx
    ON public.delist_flip_refusals (refused_at DESC);

-- Operator-tunable without a deploy, same shape as the other scraper knobs.
INSERT INTO public.app_settings (key, value)
VALUES ('delist_flip_cap', '{"fraction": 0.02, "min_rows": 500}'::jsonb)
ON CONFLICT (key) DO NOTHING;

ALTER TABLE public.delist_flip_refusals ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.delist_flip_refusals FROM anon, authenticated;
