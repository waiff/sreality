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
interpolation).

**The legacy-caller bypass is gone (2026-09-11).** `tenant_conn` and `resolve_account_id`
used to branch on a `claims.get("legacy")` key and route static-`API_TOKEN` callers onto
the unscoped service-role connection (RLS off). `verify_jwt` (`api/dependencies.py`) has
been the SOLE producer of a claims dict since PR #941 (2026-08-04) and returns the decoded
Supabase JWT verbatim — it cannot emit a `legacy` key — so both branches were unreachable
and have been deleted. Consequences worth knowing: `tenant_conn` now has **no fallback
connection at all**, so an unset `TENANT_POOL_DB_URL` raises `RuntimeError` instead of
silently degrading to an RLS-off connection (that silent degradation is exactly how the
2026-07 bad-DSN incident stayed invisible for weeks); and `current_account_ids()` is the
one definition of who the caller is, so a tenant-connection read is scoped by RLS alone.

**`legacy_backfill_claim` the TABLE stays — do NOT propose a DROP.** Only the Python read
of it was dead. The table is the atomic first-signup CAS inside `handle_new_user`
(migrations 294/362, swept by 299): the first user to sign up claims the pre-tenancy
backfill exactly once. That claim is made in SQL, not in `api/tenant_pool.py`.

## The tenancy doctrine — four shapes, and only four

This is the canonical statement. The `database` skill body carries the four-line summary;
`docs/architecture.md` (rules 18/22) points here; nothing else restates it. If you find a
fifth shape in the code, it is a bug or an undocumented exception — make it one of these
four, or add it here with a reason, but do not leave it unnamed.

**1. Reads on a tenant connection are scoped by RLS alone.** `current_account_ids()` — the
SECURITY DEFINER function every per-account policy already calls — is the ONE definition of
who the caller is. It is **plural**, because membership is plural: a user may belong to more
than one account, and any code that picks "the" account re-defines the caller more narrowly
than the database does. A read route on `tenant_conn` therefore takes **no account argument**
and its SQL carries **no account predicate**. An `UPDATE`/`DELETE` *by id* is this same shape:
the policy's `USING` clause is the row set, so supplying an account would be a second
definition of it, not a gate.

**2. Writes carry exactly ONE account, resolved once at the route edge.**
`tenant_pool.require_account_id` (FastAPI dependency) resolves it and raises
`400 "no account for caller"` when there is none. Never a silent `None` (the account columns
are `NOT NULL` since migrations 290/295, so a forwarded NULL is an empty result set or an
opaque 500) and never a `SYSTEM` fallback (migration 290's `WITH CHECK` has no SYSTEM arm to
accept it, so that fallback could only ever 500). The single best shape is the one where the
route names no account at all because a **BEFORE INSERT trigger derives it from the parent
row** (migration 292): `POST /collections/{id}/properties` and `POST /properties/{id}/tags`
cannot be got wrong, because there is nothing at the route to get wrong.

**3. An explicit `account_id = %s` predicate belongs ONLY on a service-role connection.**
There, RLS is off (BYPASSRLS) and the predicate is the SOLE gate rather than a second opinion
about a caller the database has already scoped. The live sites: `toolkit/pipeline_identity.py`
(the merge/unmerge reconcilers, which run inside `merge_properties`' service-role transaction
and must partition every join between the retired and survivor sides), `api/estimation_runs.py`
(service-role child runs from `building_runs`), and the Stripe webhook (no caller identity at
all — the HMAC over the raw body is the auth). On a tenant connection the same predicate is the
#917 bug: `POST /listings/lookup` bound `NULL` into three `account_id IS NOT DISTINCT FROM %s`
predicates for seven weeks and answered "not in pipeline" for every caller.

**4. `deps.account_scope` returns `[account_id, SYSTEM]`** — the fourth shape, named here so
nobody rediscovers it as a divergence and "fixes" it. The `/estimations` read family
(`GET /estimations`, `GET /estimations/latest-by-listing`) runs on the SERVICE-ROLE connection
on purpose: its `LEFT JOIN` onto `listings` + `parsed_url_cache` (RLS-on, zero policies) would
silently NULL `locality_display` on a tenant connection. So it scopes with an explicit list,
and that list mirrors migration 291's three-arm policy (own account OR SYSTEM OR platform
admin) rather than inventing a second tenancy definition. It never widens and never returns
empty: an unresolvable account narrows to `[SYSTEM]`, a missing or bad credential raises.

### The two test rules that would have caught #917

The outage survived seven weeks because three guards looked away at once, and two of them
were in the tests. Both rules generalize:

- **Never stub an identity resolver to the value its absence produces.** The route test
  monkeypatched `resolve_account_id → None` — the exact value a dropped argument yields — so
  every assertion about the account was unfalsifiable. Stub it to a distinctive sentinel and
  assert the sentinel ARRIVES; where the correct posture is "must resolve", stub it to RAISE.
- **Never give a tenant-scope parameter a default.** `account_id: uuid.UUID | None = None`
  turned a three-argument call to a four-parameter function into a silent NULL instead of a
  `TypeError`. Required, keyword-only, no default: then the revert is red at the call site.

A third guard was prose: a fixture COMMENT ("the route now resolves account_id") stood in for
an assertion, and outlived the behaviour it described. A comment is not a rail.

### The standing gates

- `tests/api/test_admin_route_coverage.py` — the ROUTE-SCOPE CENSUS. Walks the live FastAPI
  app; every route on a tenant connection must either reach `require_account_id` or appear in
  `_RLS_ONLY_ALLOWLIST` with a reason naming what scopes it instead. Routes that reach
  `verify_jwt` on the service-role connection (`/brokers/*`, `POST /estimations`) are excluded
  structurally, not enumerated. It has its own non-vacuity sentinel.
- `tests/api/test_account_scope_census.py` — the ACCOUNT-SCOPE CENSUS, one layer down: no
  `account_id`/`account_ids` parameter in `api/` defaults to `None`, and every hand-rolled
  `resolve_account_id` call is enumerated with a reason. Both arms declare their blind spots.

Neither gate can protect `main` on its own: they run in CI, and CI is only a merge gate while
branch protection is ON. It is currently OFF (an operator decision) — so today they protect a
PR, not the branch.
