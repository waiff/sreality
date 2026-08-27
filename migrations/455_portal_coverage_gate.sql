-- 455: the coverage gate's verdict log — the un-parking decision, made from evidence, on a timer.
--
-- `supports_complete_walk` gates delisting (architectural rule #3). Two portals are parked on it
-- today: ceskereality (migration 449) and idnes (453). Both were parked for the same reason --
-- the flag was a standing claim someone typed once, and the walks stopped matching it -- and
-- both need the same thing to come back: not an opinion, but repeated evidence.
--
-- Un-parking by hand would put us straight back where we started. A human looks once, flips the
-- flag, and the flag then keeps asserting something nobody re-checks. So the gate runs on a
-- schedule and writes down what it saw, every time, whether it passes or not. The verdict is a
-- ROW, not a log line: an Actions log expires, and the lesson this sprint keeps re-teaching is
-- that a signal nothing can query is a signal nobody receives.
--
-- THE THREE QUESTIONS, all answered from portal_index_slices (migration 454) and listings:
--
--   1. COVERED  — did every slice of every category finish (outcome='exhausted') inside one
--                 freshness window? One hole and the answer is no; 14 of 15 slices is not 93%
--                 coverage for delisting purposes, it is a walk with a hole in it.
--   2. STABLE   — has that been true for N consecutive evaluations, with the delist-candidate
--                 count holding steady between them? One lucky run proves nothing. A candidate
--                 count that swings between evaluations means the walk is flaky, not that the
--                 market moved.
--   3. SAFE     — the answer to "what if the gate is wrong anyway", which is not this table's
--                 job at all: the flip cap (migrations 451/452) refuses any sweep over 10% of a
--                 category and latches. idnes's backlog is ~37% of its rows, so even a wrongly
--                 opened gate cannot execute the mass flip -- it gets refused and alarms.
--
-- That last point is why this can be autonomous. The gate does not need to be right, it needs to
-- be RIGHT-OR-CAUGHT, and the layer underneath it catches it.

CREATE TABLE IF NOT EXISTS public.portal_coverage_gate (
    id             bigserial   PRIMARY KEY,
    evaluated_at   timestamptz NOT NULL DEFAULT now(),
    source         text        NOT NULL,
    covered        boolean     NOT NULL,   -- every slice of every category exhausted, in window
    categories     integer     NOT NULL,   -- how many the portal declares
    categories_ok  integer     NOT NULL,   -- how many were fully covered
    slices_ok      integer     NOT NULL,
    slices_total   integer     NOT NULL,
    candidates     integer,                -- rows a sweep would delist right now
    consecutive    integer     NOT NULL DEFAULT 0,  -- consecutive covered evaluations incl. this
    verdict        text        NOT NULL,   -- pass | hold | unparked
    note           text
);

COMMENT ON TABLE public.portal_coverage_gate IS
    'Coverage-gate verdict log (migration 455): one append-only row per evaluation per portal, '
    'recording whether every index slice finished inside the freshness window, how many '
    'consecutive evaluations that has held for, and how many rows a delisting sweep would flip. '
    'verdict=''unparked'' is the row where supports_complete_walk was set back to true. Read this '
    'rather than asking whether a walk "looks complete" -- the flag is only ever as true as the '
    'last evaluation here.';

CREATE INDEX IF NOT EXISTS portal_coverage_gate_recent_idx
    ON public.portal_coverage_gate (source, evaluated_at DESC);

ALTER TABLE public.portal_coverage_gate ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.portal_coverage_gate FROM anon, authenticated;
