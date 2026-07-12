# Tenancy — full reference

Detail supporting the "Multi-tenancy and RLS" section of the `database` skill. Read the
skill body first; this fills in the table-by-table migration list and the exact policy
shape for each grain.

## Foundation (migration 286)

- `accounts(id uuid, kind ∈ {personal,team,system}, name, created_at)` — one fixed SYSTEM
  row, `00000000-0000-0000-0000-000000000000`, is the default owner of everything created
  before Phase 1.
- `account_members(account_id, user_id, role)`, PK `(account_id, user_id)`.
- `admins(user_id)` — platform-admin allowlist, **separate** from account membership; a
  platform admin doesn't need to be a member of the account they're inspecting.
- `current_account_ids()` — `LANGUAGE sql STABLE SECURITY DEFINER`, returns the caller's
  `account_members.account_id` set keyed off `auth.jwt() ->> 'sub'`.
- `is_platform_admin()` — same shape against `admins`.
- Migration 287 had to explicitly `revoke execute ... from anon, authenticated` on all
  three functions — this project's default privileges auto-grant EXECUTE on new
  functions, the same default-ACL issue the Phase-0 table audit found (see the skill
  body's "default privileges" callout). `handle_new_user()` (trigger-only) gets no grant
  at all; the other two get `authenticated` back since RLS policies invoke them under the
  querying role.

## Per-table RLS, by migration

Every table below follows: `revoke all on <table> from anon, authenticated` →
`grant select, insert, update, delete on <table> to authenticated` (or a narrower verb
set where the table is genuinely add/remove-only) → a `for all using (...) with check
(...)` policy testing `account_id in (select current_account_ids())` → (if the table has
a `bigserial`/`serial` PK) an explicit `grant usage on sequence <seq> to authenticated`,
since `grant insert` on the table does not cover the sequence.

**Migration 290 — curation tables**, `account_id` on the row directly (not
trigger-derived): `collections`, `tags`, `property_notes`, `filter_presets`,
`notification_subscriptions`, `manual_rental_estimates`.

**Migration 291 — estimation/building tables**: `estimation_runs`, `building_runs`.
`account_id` is **NULLABLE**, `DEFAULT` the SYSTEM account — these can be written by
service-role paths (the estimation agent, ClickUp) that don't carry a tenant JWT.

**Migration 292 — child-grain tables**, `account_id` **derived by a `BEFORE INSERT OR
UPDATE` trigger** from the owning parent row (a plain `DEFAULT` can't see a sibling
column on `NEW`, and a trigger also covers ad-hoc SQL / forgotten code paths that a
column default would miss): `collection_properties` (parent `collections`),
`property_tags` (parent `tags`), `notification_dispatches`, `estimation_cohort_entries`,
`estimation_trace_payloads`, `estimation_feedback`, `building_run_attachments`. Because
Postgres evaluates `WITH CHECK` **after** `BEFORE` triggers run, a tenant inserting a
child row that points at another tenant's parent gets `account_id = NULL` back from the
RLS-filtered parent lookup (the trigger runs as the invoking role) and the insert **fails
closed** — a `with check (true)` here would instead silently write invisible orphan rows.
Service-role writers bypass RLS entirely but still get correctly-stamped rows because
their parent lookups see every row.

**Migration 294 — pipeline tables**: `property_pipeline`, `pipeline_stages`,
`property_pipeline_events`, plus:
- `seed_default_pipeline` / `seed_default_collections` — run on every signup **after**
  the first, giving each new account its own starter stages/collections.
- `backfill_legacy_account_id` — claims every pre-tenancy NULL-`account_id` row for
  whichever account wins the first-signup race.
- `legacy_backfill_claim` — a one-row table claimed by an atomic
  `INSERT ... ON CONFLICT DO NOTHING`-style CAS (same shape as the lease-row pattern in
  migration 279), so a signup race can't double-claim the legacy data.
- The signup trigger branches on whether `legacy_backfill_claim` is already claimed: the
  winner runs the backfill, everyone else gets fresh seed data. The migration comment
  flags this as unsafe once signup is public (non-operator) — the "first signup wins"
  assumption only holds while signup is effectively single-operator.

## Composite PK: `property_pipeline` (migration 295)

The **only** table where the primary key itself changed, not just an added/scoped
column. Explicitly **gated** — its header states it must apply in the same deploy window
as, and strictly after, the matching Python rewrite (`api/pipeline.py`'s
`ON CONFLICT (account_id, property_id)` in add/move, `toolkit/pipeline_identity.py`'s
account-partitioned reconcile/unmerge). Applying it before the Python ships is a hard
outage: old code's `ON CONFLICT (property_id)` can no longer infer against the new
composite unique index (`42P10`), and every `property_pipeline_events` INSERT that
doesn't stamp `account_id` hits a `NOT NULL` violation.

The migration self-guards: it `raise exception`s and refuses to apply if any NULL
`account_id` row remains on `property_pipeline`, `pipeline_stages`, or
`property_pipeline_events` — a hard stop instead of silent corruption if the legacy
backfill hasn't run yet (i.e., the operator hasn't signed up).

Mechanics:
```sql
alter table property_pipeline drop constraint property_pipeline_pkey;
alter table property_pipeline add constraint property_pipeline_pkey
  primary key (account_id, property_id);
```
Plus a **composite FK** so a card's stage can't cross accounts even though FK checks
bypass RLS: `pipeline_stages` gets a `unique (account_id, id)` index, and
`property_pipeline.stage_id` becomes a composite FK `(account_id, stage_id) references
pipeline_stages (account_id, id)`. Without this, a cross-account `stage_id` write would
make the account-partitioned reconciler's keep-most-advanced step silently no-op and then
drop the retired card — a correctness bug, not just an isolation one.

## Tenant role and pool

`tenant_pool` (migration 293): `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
NOINHERIT PASSWORD NULL CONNECTION LIMIT 50`, granted membership in `authenticated`.
`NOINHERIT` means membership grants nothing until an explicit `SET LOCAL ROLE
authenticated` runs inside a transaction — the role has zero data access on its own, so a
code path that forgets the switch fails closed rather than leaking. No password is set by
the migration (secrets discipline); the operator sets one via the Supabase SQL editor and
stores it only in Railway as part of `TENANT_POOL_DB_URL`.

`api/tenant_pool.py`'s `tenant_conn` FastAPI dependency is the runtime side — see the
`database` skill body's connection-modes section for the request-transaction mechanics
(`SET LOCAL ROLE` + `set_config('request.jwt.claims', ...)`, bind param not string
interpolation, legacy-caller bypass).
