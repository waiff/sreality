-- 452: calibrate the delisting flip cap against measured sweep history, and give it a release valve.
--
-- Migration 451 shipped the cap at 2% of a category with a 500-row size floor. Those numbers were
-- reasoned, not measured, and measurement says they are wrong in a way that would have caused an
-- outage rather than prevented one.
--
-- Sixty days of real sweeps (11,763 that flipped at least one row, on categories of 500+ live rows)
-- put the per-sweep share of a category at p95 = 1.8% and p99 = 3.4%, and then the tail jumps
-- straight to 86%. Routine churn and genuine incidents are two separate populations with a wide
-- empty gap between them, and the ceiling belongs in that gap.
--
--   at  2% the breaker trips 446 times / 60 days -- ordinary sreality and idnes rental churn
--   at  5% it trips  63 times
--   at 10% it trips  43 times, and every distinct event is real:
--            realitymix   dum/prodej        86.3%   (9,557 of 11,198 -- the June incident)
--            ceskereality komercni/prodej   30.1%   (734 of 2,436)
--            sreality     pozemek/podil     18.7%   (1,175 of 6,272)
--            ceskereality komercni/prodej   13.7%   (330 of 2,414)
--
-- A breaker that trips 7x a day on healthy portals is not a breaker; it is an outage generator,
-- and worse, it LATCHES (see below), so it would have permanently stalled delisting on sreality
-- and idnes rentals -- our two largest sources.
--
-- min_rows moves 500 -> 2000 for the same reason, and it is a floor on CATEGORY SIZE, not on the
-- ceiling. The small categories are the churny ones: sreality pozemek/drazba holds ~600 live rows
-- and legitimately turns over 6-39% of them in a single sweep because auctions end on a date; idnes
-- dum/pronajem does the same at ~630 rows. Policing those is pure noise. Above the floor, a 10% cap
-- cannot refuse a flip smaller than 200 rows.
--
-- THE RELEASE VALVE. A refusal does not clear itself: the unswept rows keep aging, so the next sweep
-- proposes more and is refused again. That is correct breaker behaviour -- an auto-reclosing breaker
-- defeats the purpose -- but a breaker with no reset is a permanent stall, and the only reset 451
-- offered was raising the global ceiling, which disarms the guard for every portal at once.
--
-- `overrides` is the per-scope reset. Each entry is:
--
--   {"source": "ceskereality",          -- required, must match exactly
--    "category_main": "byt",            -- optional; omitted or null = any
--    "category_type": "prodej",         -- optional; omitted or null = any
--    "subtype": null,                   -- optional; omitted or null = any
--    "max_rows": 30000,                 -- required, a hard row count for one sweep
--    "until": "2026-09-05T00:00:00Z",   -- required, ISO-8601; ignored once past
--    "reason": "verified 29,400 by per-listing fetch, see PR #1210"}
--
-- It is SCOPED (names its source), BOUNDED (max_rows caps even a wildcard entry), and EXPIRING
-- (an override that outlives its investigation stops being one). Anything missing, unparseable or
-- already expired is ignored: the valve fails shut, exactly like the cap it releases.

UPDATE public.app_settings
   SET value = jsonb_build_object('fraction', 0.10, 'min_rows', 2000, 'overrides', '[]'::jsonb)
 WHERE key = 'delist_flip_cap';

INSERT INTO public.app_settings (key, value)
VALUES ('delist_flip_cap',
        jsonb_build_object('fraction', 0.10, 'min_rows', 2000, 'overrides', '[]'::jsonb))
ON CONFLICT (key) DO NOTHING;

COMMENT ON TABLE public.delist_flip_refusals IS
    'One row each time a delisting sweep was REFUSED for exceeding the per-category flip cap '
    '(migrations 451, 452). Append-only, operator-facing: a row here means a walk believed a large '
    'share of a category had vanished and was stopped. The cap latches on purpose -- it does not '
    'clear itself -- so investigate by FETCHING the listings, then release that one scope with a '
    'bounded, expiring app_settings.delist_flip_cap.overrides entry. Raising the global ceiling '
    'instead disarms the guard for every portal.';
