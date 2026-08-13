-- 399: the operator's NO on a cross-source broker auto-merge, made durable.
--
-- The gap (2026-08-12 brokers E2E review, decision D5). The nightly sweep's
-- cross-source step (scripts/resolve_brokers.py::_cross_source_merge) re-derives
-- its ENTIRE candidate set from broker_identity_contacts on every run and consults
-- no record of any past decision. So:
--
--   * An UNMERGE (POST /broker-review/merges/{group}/unmerge) restores the
--     identities and writes nothing else. The next sweep sees byte-identical
--     bridges, re-derives the same corroborated edge, and silently re-applies the
--     merge — forever, every night, with no way for the operator to make it stop.
--   * A DISMISSED review candidate only blocks RE-PROPOSAL of the review row
--     (broker_merge_candidates.group_key + the status='proposed' upsert guard).
--     Nothing consults it before auto-merging, so the moment the evidence
--     strengthens — a second bridge value appears, display names converge so
--     names_match() passes, or a source is later added to broker_auto_merge_sources
--     — the pair auto-merges despite the operator having said no.
--
-- 7,689 auto-merges are live and zero have ever been undone, so the gap has never
-- been hit in anger; D5 is to build the rail BEFORE it can be. It is scoped for the
-- single-rung evidence class the next portals bring (remax = email-only contacts,
-- ceskereality = phone-only), where one bridge value plus a name match is the whole
-- case for a merge and a wrong one is therefore cheap to produce.
--
-- Semantics:
--   * Keyed on the IDENTITY pair, not the broker pair. broker_identities.id is
--     durable (upserted ON CONFLICT (source, source_broker_id_native), never
--     deleted); a broker id survives an unmerge but stops describing the same
--     cohort after any later unrelated merge, so a broker-pair key is evadable.
--   * ACTIVE suppression = lifted_at IS NULL. The partial unique index is both the
--     one-active-row-per-pair constraint and the sweep's lookup index.
--   * Lifting NEVER deletes (history is sacred, rule #3): an explicit operator
--     merge of the two sides lifts the suppression and stamps who/why. The rail
--     gates the AUTO path only — the operator always wins.
--   * The natural-key + display columns are denormalized on purpose: a pair of
--     bare identity ids is unreadable in an audit six months later.
--
-- Additive only. Nothing reads the table until the resolver ships with it.

CREATE TABLE broker_merge_suppressions (
  id            bigserial PRIMARY KEY,
  identity_lo   bigint      NOT NULL REFERENCES broker_identities(id),
  identity_hi   bigint      NOT NULL REFERENCES broker_identities(id),
  source_lo     text        NOT NULL,
  native_lo     text        NOT NULL,
  source_hi     text        NOT NULL,
  native_hi     text        NOT NULL,
  display_lo    text,
  display_hi    text,
  origin        text        NOT NULL CHECK (origin IN ('unmerge', 'dismiss')),
  merge_group_id uuid,                          -- provenance when origin='unmerge'
  candidate_id  bigint,                         -- provenance when origin='dismiss'
  created_by    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  lifted_at     timestamptz,
  lifted_by     text,
  lift_reason   text,
  CONSTRAINT broker_merge_suppressions_ordered CHECK (identity_lo < identity_hi)
);

CREATE UNIQUE INDEX broker_merge_suppressions_active_pair_idx
  ON broker_merge_suppressions (identity_lo, identity_hi) WHERE lifted_at IS NULL;

-- Observability parity with auto_merges / queued_for_review: a sweep that
-- suppressed nothing and a sweep whose rail is broken must not look identical.
ALTER TABLE broker_resolution_runs ADD COLUMN suppressed_merges integer;

-- Service-role only, exactly like broker_merge_events: RLS on with zero policies
-- already denies anon/authenticated, and the REVOKE closes Supabase's default ACL
-- (which grants SELECT on a newly created table to both browser roles).
ALTER TABLE broker_merge_suppressions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON broker_merge_suppressions FROM anon, authenticated;
