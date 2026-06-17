-- 188_brokers_seed_identity.sql
--
-- Provenance column on brokers: the broker_identity that first created this
-- canonical broker. Beyond audit, it is the correlation key that makes the
-- resolver's singleton-attach fully set-based and order-independent — INSERT ...
-- SELECT ... RETURNING can return this column, so new brokers link back to their
-- seeding identity without relying on RETURNING row order (which SQL does not
-- guarantee). Purely additive.

alter table brokers add column seed_identity_id bigint references broker_identities(id);
create index brokers_seed_identity_idx on brokers (seed_identity_id);

comment on column brokers.seed_identity_id is
  'The broker_identity that first created this canonical broker (singleton attach). '
  'Provenance only — canonical brokers can hold many identities after cross-source merges.';
