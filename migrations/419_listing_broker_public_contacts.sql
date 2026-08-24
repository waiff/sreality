-- 419: listing_broker_public carries the broker's primary contact.
--
-- Hydration sprint W6 — "one broker call". Both the pipeline board's broker
-- decoration and the listing page's vizitka spent TWO serialized Railway round
-- trips on one broker line: /brokers/by-listings (identity, off
-- listing_broker_public) and then /brokers?ids= (contact, off brokers_public),
-- the second unable to start until the first's broker_ids landed. The contact
-- pair lives on `brokers` — the very row this view already joins, and already
-- filters to status='active' — so the second hop re-read heap pages the first
-- read had just had in hand.
--
-- Measured live, EXPLAIN (ANALYZE, BUFFERS), the 48 real board listing ids:
--   step 1  listing_broker_public   35 rows,  518 buffers (331 hit / 187 read)
--   step 2  brokers_public          35 rows,  207 buffers (196 hit /  11 read)
--                                            + 436 planning buffers
-- Of step 2's 207, 108 are the same brokers_pkey scan step 1 already did and 99
-- are the same firms_pkey lookups. Widening the view projects both columns off a
-- tuple the join has already fetched: zero added blocks, one fewer round trip.
--
-- NOT a PII widening. listing_broker_public is API-ONLY under Amendment A6 — no
-- anon and no authenticated grant (live ACL: postgres + service_role only),
-- registered in BOTH tests/test_migration_rls_grants.py::_BROKER_A6_SURFACES and
-- tests/test_tenant_isolation_live.py::_BROKER_PII_RELATIONS. The SPA cannot
-- read it; it reads /brokers/*, where toolkit.brokers.apply_pii_policy swaps
-- every *email* / *phone* column for a has_* flag unless the caller is an admin.
-- That mask matches on the column NAME, not a fixed list, precisely so a widened
-- view is masked the day it lands — these two columns need no route change to be
-- covered, and _mask's own docstring says so.
--
-- The values are the same `brokers.primary_email` / `brokers.primary_phone`
-- columns brokers_public projects, so a card shows exactly what the deleted
-- second call returned. Written against the LIVE viewdef (pg_get_viewdef), not a
-- migration file — 190 created this view and 343 last replaced it.
--
-- Definer view, no security_invoker: unchanged from live (reloptions is NULL on
-- both this view and brokers_public). It is deliberately definer — `listings` is
-- RLS-on-with-zero-policies, so only the owner-bypass makes the read work at all,
-- and the browser never holds a grant to reach it.
--
-- Additive: CREATE OR REPLACE VIEW can only append, so both columns go on the end
-- and every existing `select *` consumer keeps its column order.

set local lock_timeout = '5s';

create or replace view listing_broker_public as
select
  l.sreality_id,
  bi.broker_id,
  b.display_name as broker_display_name,
  coalesce(f.display_name, f.canonical_domain) as broker_firm_label,
  l.id as listing_id,
  b.primary_email,
  b.primary_phone
from listings l
join broker_identities bi on bi.id = l.broker_identity_id
join brokers b on b.id = bi.broker_id and b.status = 'active'
left join firms f on f.id = b.primary_firm_id;

-- Re-assert the A6 posture explicitly rather than trusting that CREATE OR REPLACE
-- preserves the ACL. This project's default privileges auto-GRANT on CREATE, and
-- firms_public reached production browser-readable by exactly that route — its
-- `authenticated` SELECT came from the default ACL, not from any grant statement,
-- so 299's sweep never saw it and it stayed live until 395.
revoke all on listing_broker_public from anon, authenticated;

comment on view listing_broker_public is
  'The active broker behind each listing, with the primary contact pair. API-only '
  '(Amendment A6): no browser-role grant — the SPA reads it through /brokers/*, '
  'which masks primary_email/primary_phone to has_email/has_phone for a non-admin.';
