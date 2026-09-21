-- 542_sold_transactions.sql — the sold-transactions store (sold-comps W1).
--
-- NORTH STAR. A registered sale is an account-less external FACT, not a listing: one row
-- per sale under the sale's own cadastral identity (`source_record_id` = reas's
-- `mapPointerId`, the cadastre transfer key), fetched per municipality cell and read
-- through ONE SQL definition. Never a listing row, never linked to a property, never
-- mixed with asking prices, never adjusted, never shown without saying where it came
-- from and when we last looked.
--
-- SECURITY POSTURE. Both tables are backend/service-role only: `enable row level
-- security` with ZERO policies (deny-all to anon + authenticated) AND the explicit
-- `revoke all ... from anon, authenticated` at the foot, because Supabase's postgres
-- default ACL grants `authenticated` SELECT on every new table in schema `public`
-- (migration 299's model; the same pair migration 501 ships for `listing_location`).
-- The browser-readable view and the radius function are W3, not this file.
--
-- WHY THE ATTRIBUTE COLUMNS ARE SPELLED LIKE `listings`. `price_czk`, `category_main`,
-- `category_type`, `subtype`, `disposition`, `area_m2`, `area_basis`, `usable_area` and
-- `estate_area` carry the same names AND the same types as the identically-named
-- `listings` columns, so `measure_price_per_m2` / `plot_area_m2` (migrations 425/535) and
-- their Python faces in `toolkit/measures.py` apply to a sold row with zero new code.
-- The RÚIAN codes match `listing_location`'s `bigint`. A second area grammar, a second
-- per-m² definition or a second vocabulary would each be the thing rule 21/23 forbids.
--
-- ABSENT BY DESIGN: `account_id` (market data, not user state), `property_id` /
-- `listing_id` / any link table (a sale is not one of our listings; grouping is rule 15's
-- business and stays out), snapshots (a transfer does not change), `is_active` (a sale
-- cannot be delisted), a queue, a failures table, a runs table, `images` rows (photos are
-- hot-linked, not copied), a feature flag, and a `price_kind` column — asking vs realized
-- is PROVENANCE, and the table identity is the discriminator. `histogramPrice`
-- (reas's soldPrice indexed to today, ×1.10–1.28 beyond 12 months) and `originalPrice`
-- (corrupt: one observed record carries 1 Kč) have no column and are not in `raw`.
--
-- Additive and idempotent; no `--single-transaction`, so every statement is re-runnable
-- from statement 1 (.github/workflows/apply_migration.yml).

set lock_timeout = '5s';

------------------------------------------------------------------
-- the facts
------------------------------------------------------------------

create table if not exists sold_transactions (
  -- identity: the SALE's own key, not the advertisement's. reas re-creates ad records;
  -- a cadastre transfer id survives that.
  source            text not null,
  source_record_id  text not null,

  -- the transaction. `sold_at` is a DATE because the source's time component is its
  -- batch-ingest clock, not a legal-effects time. `price_czk` is NOT NULL: a sale
  -- without a price is not a fact this table is for.
  sold_at           date not null,
  price_czk         integer not null,

  -- the advertisement around the sale. `asking_last_czk` is the last ASKING price and is
  -- only ever rendered as a discount against `price_czk` — it is never a comparable.
  asking_last_czk   integer,
  listed_at         timestamptz,
  published_at      timestamptz,

  -- attributes, spelled and typed exactly as `listings` spells them (see the header).
  category_main     text not null,
  category_type     text not null,
  subtype           text,
  disposition       text,
  area_m2           numeric(7,1),
  area_basis        text,
  usable_area       numeric(9,1),
  estate_area       numeric(9,1),

  -- where. The point is BUILDING-grain and shared by units in one building, so two rows
  -- at identical coordinates are a normal, expected, non-duplicate pair.
  geom              geometry(Point, 4326) not null,
  address_text      text,
  obec_kod          bigint,
  ku_kod            bigint,
  ulice_kod         bigint,

  -- provenance. `photo_urls` are the source's own public URLs, hot-linked; nothing is
  -- copied to R2 and no `images` row exists. `raw` is the list-grain record minus the
  -- seller/broker identity and the three modelled prices/areas, so a re-parse never
  -- needs a re-fetch and can never resurrect a field this table refuses.
  photo_urls        text[],
  source_url        text,
  fetched_at        timestamptz not null default now(),
  raw               jsonb not null,

  primary key (source, source_record_id)
);

-- The one read: "sales within N metres of this subject". CAST AT EVERY CALL SITE —
-- `geom` is geometry, so an uncast ST_DWithin measures DEGREES and silently answers a
-- different question (migration 507's rail for `listing_location`).
create index if not exists sold_transactions_geog_gist
  on sold_transactions using gist ((geom::geography));

------------------------------------------------------------------
-- the fetch ledger — append-only, and its own run record
------------------------------------------------------------------

-- One row per (cell, attempt). A ZERO-YIELD fetch writes a row too: "we asked this cell
-- and it held nothing" is a fact, and it is the only thing that makes an empty result
-- distinguishable from a lane that never ran. `source_total` is what the source declared
-- the cell holds WITHOUT our date window (reas's `possibleCount`), against `record_count`
-- for what we took — the source's own `count` equals what a completed walk took, so only
-- the wider number can say how much the table is NOT seeing (Olomouc 89 of 625).
create table if not exists sold_transaction_fetches (
  id           bigserial   primary key,
  source       text        not null,
  obec_kod     bigint      not null,
  bbox         geometry(Polygon, 4326) not null,
  fetched_at   timestamptz not null default now(),
  status       text        not null check (status in ('ok', 'failed')),
  record_count integer     not null default 0,
  source_total integer,
  pages        integer,
  error        text
);

create index if not exists sold_transaction_fetches_cell_idx
  on sold_transaction_fetches (source, obec_kod, fetched_at desc);

------------------------------------------------------------------
-- RLS posture (tests/test_migration_rls_grants.py rule 3)
------------------------------------------------------------------

alter table sold_transactions        enable row level security;
alter table sold_transaction_fetches enable row level security;

revoke all on sold_transactions        from anon, authenticated;
revoke all on sold_transaction_fetches from anon, authenticated;
revoke all on sequence sold_transaction_fetches_id_seq from anon, authenticated;

reset lock_timeout;
