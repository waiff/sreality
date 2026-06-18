-- Phase 5: operator broker merge-review queue.
--
-- The auto-merge engine only unifies brokers that share a PERSONAL contact
-- (a phone/email used by exactly one broker per source). Corporate/developer
-- accounts hide behind per-portal role inboxes (sreality@yit.cz, ridnes@yit.cz)
-- and a switchboard number, so they have no personal bridge and are deliberately
-- NOT auto-merged (name-alone is never enough). This queue surfaces those
-- "same name + same firm, no bridge" groups for one-click operator review.
--
-- Reversibility already exists in broker_merge_events (undone_at/undone_by); this
-- table is just the proposal ledger. group_key is the stable identity of a group
-- so daily regeneration is idempotent and a resolved (merged/dismissed) group is
-- never re-proposed.

CREATE TABLE broker_merge_candidates (
  id          bigserial PRIMARY KEY,
  group_key   text NOT NULL UNIQUE,
  broker_ids  bigint[] NOT NULL,
  reason      text NOT NULL DEFAULT 'name_firm',
  evidence    jsonb NOT NULL DEFAULT '{}'::jsonb,
  status      text NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed', 'merged', 'dismissed')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz,
  resolved_by text
);

CREATE INDEX broker_merge_candidates_status_idx ON broker_merge_candidates (status);
