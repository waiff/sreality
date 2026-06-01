-- 127_dedup_eligibility.sql
-- Dedup engine rebuild (rule A): a listing only participates in matching when
-- it has BOTH a street and a disposition — the two key identifiers the new
-- street+disposition engine keys on. Everything else is excluded ("location
-- unclear" = no street; "disposition unclear" = street but no disposition).
--
-- Eligibility is computed live (a pure function of street + disposition) rather
-- than materialised: a STORED generated column would force a full rewrite of
-- the 137k-row table, and the rule is cheap to evaluate inline. The engine
-- filters on the expression and the dashboard counts it with a CASE; this
-- partial index keeps the "eligible" scan tight (only a few hundred rows carry
-- a street today, so the index is tiny and can never drift).

CREATE INDEX IF NOT EXISTS listings_dedup_eligible_idx
  ON listings (street_id, disposition)
  WHERE street IS NOT NULL AND street <> '' AND disposition IS NOT NULL;
