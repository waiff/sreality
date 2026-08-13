-- 399_location_w1v_labelled_samples.sql
--
-- Location-data program, Wave W1v: the frozen random labelled samples of
-- design/final/06-migration-backfill.md section 6.4.0.
--
-- Every portal contract ships with a frozen random labelled sample (n >= 100,
-- drawn BEFORE the extraction sweep, random - never pathology-stratified),
-- hand-labelled by the operator against the portal page/payload. The gate on a
-- contract is PRECISION on that sample, not yield: street >= 95 %, obec/okres
-- >= 98 %, precision-class assignment >= 95 %. The label set is frozen and
-- reused across portal_contracts.version bumps so v2-vs-v1 rates are
-- comparable, and the same sample scores the OLD system's columns (which are
-- snapshotted here at draw time, because the refetch/re-parse that follows the
-- draw may itself rewrite listings.street - scoring "the old system as it
-- stood" needs the values as they stood).
--
-- Storage classes (01-schema.md section 0.1):
--   location_labelled_samples         MUTABLE (operator/config) - header, one
--                                     is_current row per source
--   location_labelled_sample_members  MUTABLE (operator/config) - membership
--                                     is frozen by convention at draw time
--                                     (the draw script inserts once; nothing
--                                     else writes these columns); the label_*
--                                     columns are operator-curated and
--                                     mutable until labelled.
--
-- Licence note: NO coordinate is snapshotted here. legacy street/obec/okres
-- text is first-party portal/parser output (class B/D at worst); a legacy
-- geom copy could be class E (Mapy-derived) on some rows and a quarantine
-- table holding such a value would recreate the exposure under a new name
-- (06 section 6.1.5). Coordinate-precision scoring of the NEW system reads
-- the projection live; the old system's coordinate is not scored from here.

begin;

create table location_labelled_samples (
  id          bigserial primary key,
  source      text not null,
  drawn_at    timestamptz not null default now(),
  method      text not null,
  n           integer not null,
  is_current  boolean not null default true,
  note        text
);

create unique index location_labelled_samples_one_current
  on location_labelled_samples (source) where is_current;

create table location_labelled_sample_members (
  sample_id             bigint not null references location_labelled_samples(id),
  listing_id            bigint not null,
  source_id_native      text not null,
  position              integer not null,

  -- the old system's serving values, frozen at draw time (section 6.4.0 #4)
  legacy_street         text,
  legacy_street_source  text,
  legacy_house_number   text,
  legacy_obec           text,
  legacy_okres          text,
  legacy_zip            text,

  -- operator ground truth, labelled against the portal page/payload.
  -- Each field is either a value, or explicitly "not determinable from this
  -- surface" (the _nd flag) - an unlabelled field is neither.
  label_street          text,
  label_street_nd       boolean not null default false,
  label_house_number    text,
  label_house_number_nd boolean not null default false,
  label_obec            text,
  label_obec_nd         boolean not null default false,
  label_okres           text,
  label_okres_nd        boolean not null default false,
  label_precision_class location_granularity,
  label_precision_nd    boolean not null default false,
  label_note            text,
  labelled_at           timestamptz,

  primary key (sample_id, listing_id)
);

create index location_labelled_sample_members_listing
  on location_labelled_sample_members (listing_id);

alter table location_labelled_samples        enable row level security;
alter table location_labelled_sample_members enable row level security;

------------------------------------------------------------------
-- Backend/service-role only (operator labels arrive through the
-- admin-gated API, never a direct client write).
------------------------------------------------------------------

revoke all on location_labelled_samples        from anon, authenticated;
revoke all on location_labelled_sample_members from anon, authenticated;
revoke all on sequence location_labelled_samples_id_seq from anon, authenticated;

commit;
