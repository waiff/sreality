-- 185_broker_identity_foundation.sql
--
-- Broker intelligence, phase 1: the canonical identity model. Mirrors the proven
-- listings -> properties two-layer architecture (rule #15) one tier wider:
--
--   broker_identities  ~ listings    (per-source, authoritative, NEVER merged within a source)
--   brokers            ~ properties  (canonical human; cross-source grouping, resolver-maintained)
--   firm_identities    ~ listings    (per-source agency observation)
--   firms              ~ properties  (canonical agency, keyed on email domain)
--
-- Identity keystone (validated against live data): a contact (email/phone) is a
-- CROSS-source identity bridge ONLY if it is personal on both sides (frequency==1
-- within each source). Shared/role inboxes (info@mmreality.cz -> 353 brokers) and
-- toll-free/switchboard numbers (+420731404040 -> 464 brokers) are demoted to
-- firm/display attributes, never used to merge humans. Within-source identity is
-- the portal-native id (sreality user_id), never contact-merged. broker_identity_
-- contacts is the normalized per-identity contact ledger that feeds the frequency
-- guard (over accumulated history, not just the latest rollup).
--
-- Purely additive: new tables only. Hot public views (listings_public /
-- properties_public) are intentionally NOT touched in this migration — broker
-- fields there stay NULL exactly as today until a later, measured migration wires
-- them from the FK join.

-- Canonical agency. canonical_domain NULL = independent / free-provider broker.
create table firms (
  id                   bigserial primary key,
  canonical_domain     text unique,
  display_name         text,
  is_franchise         boolean     not null default false,
  broker_count         integer     not null default 0,
  listing_count        integer     not null default 0,
  active_listing_count integer     not null default 0,
  first_seen_at        timestamptz not null default now(),
  last_seen_at         timestamptz not null default now(),
  stats_computed_at    timestamptz
);
create index firms_domain_idx on firms (canonical_domain) where canonical_domain is not null;
alter table firms enable row level security;

-- Per-source agency observation. sreality: source_firm_native = email domain;
-- idnes (phase 2): account.<mongoOid>. email_domain is the cross-source firm key.
create table firm_identities (
  id                 bigserial primary key,
  source             text        not null,
  source_firm_native text        not null,
  firm_id            bigint references firms(id) on delete set null,
  email_domain       text,
  display_name       text,
  logo_url           text,
  first_seen_at      timestamptz not null default now(),
  last_seen_at       timestamptz not null default now(),
  constraint firm_identities_src_native_uniq unique (source, source_firm_native)
);
create index firm_identities_firm_id_idx on firm_identities (firm_id);
create index firm_identities_domain_idx  on firm_identities (email_domain) where email_domain is not null;
alter table firm_identities enable row level security;

-- Canonical human (the properties analog). Rollups are resolver-maintained.
-- status='merged_away' + merged_into make cross-source merges reversible
-- (broker_merge_events, migration 186) exactly like properties.
create table brokers (
  id                    bigserial primary key,
  status                text not null default 'active'
                          check (status in ('active', 'merged_away')),
  merged_into           bigint references brokers(id),
  merged_at             timestamptz,
  display_name          text,
  primary_email         text,
  primary_phone         text,
  primary_firm_id       bigint references firms(id) on delete set null,
  source_count          integer     not null default 0,
  distinct_source_count integer     not null default 0,
  listing_count         integer     not null default 0,
  property_count        integer     not null default 0,
  active_listing_count  integer     not null default 0,
  active_property_count integer     not null default 0,
  first_seen_at         timestamptz not null default now(),
  last_seen_at          timestamptz not null default now(),
  stats_computed_at     timestamptz
);
create index brokers_active_idx       on brokers (status) where status = 'active';
create index brokers_primary_firm_idx on brokers (primary_firm_id);
create index brokers_merged_into_idx  on brokers (merged_into);
alter table brokers enable row level security;

-- Per-source broker identity (the listings analog). source_broker_id_native is
-- text so sreality int user_id and idnes account/makler ids share one column
-- (source-generic, rule #21). email/display_name are latest-wins rollups over the
-- identity's listings (absorbs the ~0.2% user_id name/email drift). email_domain
-- is GENERATED so the firm-domain key can never drift from the email.
create table broker_identities (
  id                      bigserial primary key,
  source                  text not null,
  source_broker_id_native text not null,
  broker_id               bigint references brokers(id) on delete set null,
  display_name            text,
  email                   text,
  email_domain            text generated always as (lower(split_part(email, '@', 2))) stored,
  rating                  numeric(3, 2),
  review_count            integer,
  firm_identity_id        bigint references firm_identities(id) on delete set null,
  listing_count           integer     not null default 0,
  active_listing_count    integer     not null default 0,
  first_seen_at           timestamptz not null default now(),
  last_seen_at            timestamptz not null default now(),
  attrs_computed_at       timestamptz,
  constraint broker_identities_src_native_uniq unique (source, source_broker_id_native)
);
create index broker_identities_broker_id_idx  on broker_identities (broker_id);
create index broker_identities_email_idx       on broker_identities (email)        where email is not null;
create index broker_identities_domain_idx      on broker_identities (email_domain) where email_domain is not null;
create index broker_identities_firm_ident_idx  on broker_identities (firm_identity_id);
create index broker_identities_unresolved_idx  on broker_identities (id) where broker_id is null;
alter table broker_identities enable row level security;

-- Normalized per-identity contact ledger. The frequency guard (the keystone) is
-- computed over this table (accumulated history), NOT the latest-wins rollup on
-- broker_identities, so a contact that was shared at any point is correctly
-- classed shared. value is normalized (lowercased email / digits-only phone).
-- source is denormalized so the frequency group-by needs no join.
create table broker_identity_contacts (
  id                  bigserial primary key,
  broker_identity_id  bigint not null references broker_identities(id) on delete cascade,
  source              text   not null,
  kind                text   not null check (kind in ('email', 'phone')),
  value               text   not null,
  first_seen_at       timestamptz not null default now(),
  last_seen_at        timestamptz not null default now(),
  constraint broker_identity_contacts_uniq unique (broker_identity_id, kind, value)
);
create index broker_identity_contacts_freq_idx on broker_identity_contacts (source, kind, value);
alter table broker_identity_contacts enable row level security;

-- Time-bounded canonical broker<->firm memberships (a broker can belong to many
-- firms over time / concurrently). Pure rollup the resolver recomputes from the
-- broker's listings. "is_current" is derived at READ time from last_seen_at in the
-- public view (never a stored now()-relative boolean that goes stale, per review).
create table broker_firm_memberships (
  id            bigserial primary key,
  broker_id     bigint not null references brokers(id) on delete cascade,
  firm_id       bigint not null references firms(id)   on delete cascade,
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  listing_count integer     not null default 0,
  constraint broker_firm_memberships_uniq unique (broker_id, firm_id)
);
create index bfm_broker_idx on broker_firm_memberships (broker_id);
create index bfm_firm_idx    on broker_firm_memberships (firm_id);
alter table broker_firm_memberships enable row level security;

comment on table broker_identities is
  'Per-source broker identity (listings analog). Authoritative within its source '
  '(portal-native id); never contact-merged within a source. broker_id is the '
  'resolver-maintained canonical parent.';
comment on table brokers is
  'Canonical broker (properties analog). Cross-source grouping done out-of-band by '
  'scripts.resolve_brokers; merges reversible via broker_merge_events.';
comment on table broker_identity_contacts is
  'Normalized per-identity contact ledger feeding the frequency guard: a contact '
  'is a cross-source identity bridge only if frequency==1 within each source.';
