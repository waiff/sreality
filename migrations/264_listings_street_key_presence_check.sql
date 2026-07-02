-- 264_listings_street_key_presence_check.sql
-- Construction-level guard for the street_name_key write-path invariant (audit PR-E).
--
-- WHY: listings.street_name_key (migration 256) is a Python-derived column that every
-- listings.street write path must stamp in lockstep. That coverage is enumeration-based
-- (the write-path test lists the known chokepoints + backfills) and the enumerated class
-- already failed once: the coord→street resolver wrote streets without the key until an
-- adversarial review caught it. This CHECK makes the forgot-to-stamp class fail LOUDLY at
-- write time instead of silently under-loading the dedup --dirty lane.
--
-- The alnum gate makes the constraint EXACTLY as strong as the Python function's own
-- guarantee: scraper.street.street_name_key returns a non-NULL key for any street
-- containing an alphanumeric character (the fold/strip loop always falls back to the
-- collapsed form rather than going empty), while a whitespace-only/exotic-whitespace
-- street legitimately keys to NULL — btrim() can't see unicode whitespace the Python
-- .split() collapses, so gating on [[:alnum:]] (not btrim) avoids ever rejecting a write
-- the function itself would produce. Validated against prod pre-apply: 0 violations on
-- ~211k street-bearing rows.
--
-- NOT VALID + VALIDATE: the ADD takes only a brief lock and doesn't scan; the VALIDATE
-- scans without blocking writes (SHARE UPDATE EXCLUSIVE) — the standard low-lock pattern.
--
-- RECORD CORRECTION for migration 256's comments (append-only forbids editing them):
-- (1) 256 claims the normalizer is "not faithfully reproducible in SQL without drift" —
-- the 2026-07 audit DISPROVED that (a plpgsql twin matched 0/210,457 stored keys on this
-- database). Python stays the single source anyway, for lockstep reasons: the engine
-- recomputes the key live at every load, so a SQL twin would be a permanent dual
-- implementation that today's CI (no Postgres) cannot police. (2) 256 claims "a parity
-- test guards stored == recomputed" — that test did not exist; it does NOW, as the weekly
-- sampled-parity job (scripts/check_street_key_parity.py + street_key_parity.yml), which
-- alerts through the workflow-failure monitor when any stored key drifts from the
-- function (the stale-key class this CHECK cannot see).

alter table listings
  add constraint listings_street_key_presence
  check (street is null or street !~ '[[:alnum:]]' or street_name_key is not null)
  not valid;

alter table listings validate constraint listings_street_key_presence;

comment on constraint listings_street_key_presence on listings is
  'A street containing any alphanumeric char must carry its derived street_name_key '
  '(scraper.street.street_name_key stamps it at every write path; migration 256/264). '
  'Fails the forgot-to-stamp write loudly; the weekly street_key_parity job guards the '
  'stale-key class.';
