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
-- THE GATE IS ASCII-ALNUM, DELIBERATELY NOT [[:alnum:]]: the constraint may only demand a
-- key where the Python function GUARANTEES one, or it can reject a legitimate write — and
-- the blast radius of one rejected row is the whole ~100-listing drain batch (sreality) or
-- a claimed-row crash loop (per-item portals). Any ASCII letter/digit survives
-- scraper.street.street_name_key's fold (NFKD keeps it, it is not a combining mark, not
-- whitespace), so `street ~ '[a-zA-Z0-9]'` implies key IS NOT NULL, always. [[:alnum:]]
-- does NOT have that property on this database (PG 17, ICU): Letter-category codepoints
-- that NFKD-decompose to whitespace+combining marks — U+037A GREEK YPOGEGRAMMENI,
-- U+FF9E/FF9F halfwidth katakana voiced marks, Arabic presentation forms U+FE70.. — are
-- alnum to Postgres yet fold to EMPTY in Python (key NULL): a street of only such chars
-- would violate the wider gate. Verified on prod. The narrower gate stays effective for
-- the class it exists to catch: every realistic Czech street contains an ASCII letter or
-- digit, and a pure-diacritic edge case merely goes unenforced (the function still
-- produces its key; the weekly parity job still checks it).
--
-- LOCK NOTE: NOT VALID + VALIDATE in ONE migration (as applied via MCP, one transaction)
-- does NOT get the two-transaction low-lock benefit — the ADD's ACCESS EXCLUSIVE is held
-- through the validation scan. At the current table size that scan is ~1-2s, an accepted
-- brief write-pause; a much larger table should split the VALIDATE into its own
-- transaction.
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
--
-- (Prod history: the constraint was first applied with the [[:alnum:]] gate, then
-- corrected to this ASCII gate before this file merged — the DB and this file agree on
-- the final form.)

alter table listings
  add constraint listings_street_key_presence
  check (street is null or street !~ '[a-zA-Z0-9]' or street_name_key is not null)
  not valid;

alter table listings validate constraint listings_street_key_presence;

comment on constraint listings_street_key_presence on listings is
  'A street containing any ASCII letter/digit must carry its derived street_name_key '
  '(scraper.street.street_name_key stamps it at every write path; migration 256/264). '
  'ASCII gate: the Python fold guarantees a key exactly for these; wider [[:alnum:]] '
  'classes include codepoints that fold to empty (U+037A et al.) and would reject '
  'legitimate writes. Fails the forgot-to-stamp write loudly; the weekly '
  'street_key_parity job guards the stale-key class.';
