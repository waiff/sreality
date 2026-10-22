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
- Grants follow the skill body's "Default privileges" recipe: `handle_new_user()`
  (trigger-only) is executable by no client role; the other two grant `authenticated`
  EXECUTE, since RLS policies invoke them under the querying role.

## Per-table RLS, by migration

Every table below follows: `revoke all on <table> from anon, authenticated` →
`grant select, insert, update, delete on <table> to authenticated` (or a narrower verb
set where the table is genuinely add/remove-only) → a `for all using (...) with check
(...)` policy testing `account_id in (select current_account_ids())`. An integer surrogate id is `bigint
generated always as identity` (611), never `bigserial`: ALWAYS refuses, with no USAGE grant, the caller-chosen id
that would pre-claim the sequence; a uuid id (`gen_random_uuid()`) cannot be pre-claimed and stays as is. Every
such table is listed once, in `tests/_tenant_registry.py`, which both tenancy gates read (the offline grant scan
`tests/test_migration_rls_grants.py` and the live lane `tests/test_tenant_isolation_live.py`): a new tenant table
is added there.

**Migration 290 — curation tables**, `account_id` on the row directly (not
trigger-derived): `collections`, `tags`, `property_notes`, `filter_presets`,
`notification_subscriptions` (and `manual_rental_estimates`, dropped by 639). Later siblings: `property_dismissals`
(536), `property_artifact_links` (599, one URL per `(account_id, property_id)` composite key,
caller-supplied account, API-only reads and writes) and `stats_dashboards` (626; 647 re-keyed it on
`id`: named dashboards, the saved-set CRUD of `/stats/dashboards`, plus ONE unnamed working board per account
by partial unique index: `PUT /stats/dashboard` upserts it under the account `require_account_id` resolves, the
row's KEY; `GET /stats/dashboard` reads it by RLS alone, shape 1, and under `X-Act-As` that is the caller's own,
the table being identity-scoped). The column was added nullable (the tables
already held rows); it is NOT NULL on four of the five since `notification_subscriptions` 364,
`filter_presets` 595 and `collections` / `tags` 608. `property_notes` waits on the operator's ruling
about the split preview's bare-note case (PR #1829 attributes its one pre-tenancy NULL row).

**Migration 291 — estimation/building tables**: `estimation_runs`, `building_runs`.
`account_id` is **NULLABLE**, `DEFAULT` the SYSTEM account — these can be written by
service-role paths (the estimation agent, ClickUp) that don't carry a tenant JWT.

**Migration 292 — child-grain tables**, `account_id` **derived by a `BEFORE INSERT OR
UPDATE` trigger** from the owning parent row (a plain `DEFAULT` can't see a sibling
column on `NEW`, and a trigger also covers ad-hoc SQL / forgotten code paths that a
column default would miss): `collection_properties` (parent `collections`),
`property_tags` (parent `tags`), `notification_dispatches`, `estimation_cohort_entries`,
`estimation_trace_payloads`, `estimation_feedback`, `building_run_attachments` (and, later,
`property_note_attachments`, parent `property_notes`, migration 592). Because
Postgres evaluates `WITH CHECK` **after** `BEFORE` triggers run, a tenant inserting a
child row that points at another tenant's parent gets `account_id = NULL` back from the
RLS-filtered parent lookup (the trigger runs as the invoking role) — or the OWNER's account when
the parent is merely shared with it (migration 610) — and `WITH CHECK` refuses either, so the
insert **fails closed** — a `with check (true)` here would instead silently write invisible orphan rows.
Service-role writers bypass RLS entirely but still get correctly-stamped rows because
their parent lookups see every row. Since migration 651 (notifications W1), `authenticated` may UPDATE only `seen_at`
on `notification_dispatches`, and no admin arm reads or updates another account's rows (651 dropped 619's two). Since
656 the dispatch trigger stamps no SYSTEM account: it raises for any source but `watchdog` / `collection_monitor` /
`share` (656 deleted the system_health history, ruling R2 (b)), so every dispatch is one account's own.

**Migration 294 — pipeline tables**: `property_pipeline`, `pipeline_stages`,
`property_pipeline_events`, plus the signup trigger:
- **Signup** (`handle_new_user` on `auth.users`, one path since migration 612): it creates the
  account, the owner membership, the default board (`seed_default_pipeline`: 10 stages with
  codes from the label numbers, a dated snapshot of the operator's board), the `monitoring` collection and the 7-day
  free trial. A new default is a new migration replacing that function's literal; this prints
  one from a live board (keys as `api/pipeline.py` `_slugify` mints them from an ASCII label; the code
  from the label number, the ruled default; drop the comma on the last row):
  ```sql
  select format('(target_account_id, %L, %L, %s, %L, %L, %s, %s),',
           trim(both '_' from lower(regexp_replace(label, '[^A-Za-z0-9]+', '_', 'g'))), label,
           row_number() over (order by position, id), color,
           coalesce(substring(label from '^\s*([0-9]{1,3})'), code), is_entry::text, is_terminal::text)
    from pipeline_stages
   where account_id = '<account uuid>' and archived_at is null
   order by position, id;
  ```

**Migration 610 — `collection_shares`**, one row per (collection, recipient account):
`account_id` is the OWNER, stamped by a `BEFORE INSERT` trigger from the collection (292's
function, renamed `sync_account_id_from_collection`). ONE `FOR ALL` party policy (`USING` owner
or recipient, `WITH CHECK` owner); `grant select, delete` and **no INSERT** — account uuids are
learnable, so a share is created only by the API (`POST /collections/{id}/shares`, service role,
after resolving a confirmed email); either party deletes (revoke / leave). Reads widen through
four `FOR SELECT` policies (`*_shared_read` on `collections`, `collection_properties`,
`property_notes` — only rows the author marked `shared`, through any share between the author and
the reader that holds the property, made before or after the mark (migration 613 dropped 610's `shared_at`
consent time by ruling) — and `property_note_attachments`);
the owner's `FOR ALL` policies decide every write, unchanged. Visibility rule:
`docs/design/collection-sharing.md` §3.2.

**Migration 536 — `property_dismissals`**, `account_id` on the row directly (the route
names it, shape 2 below). Append-only: `grant select, insert` plus a COLUMN-scoped
`grant update (lifted_at, lift_reason)` and no DELETE, so a tenant can lift a dismissal but
never rewrite or remove one. One active row per (property, account) is a PARTIAL unique
index (`where lifted_at is null`), so an idempotent insert spells its conflict target with
the predicate: `ON CONFLICT (property_id, account_id) WHERE lifted_at IS NULL`.

**Migration 641 — `pipeline_shares` + `delegated_edits`**: the share is one row per (owner `account_id`,
`recipient_account_id`) with `mode ∈ {read, edit}`, the same party-policy / no-INSERT / either-party-deletes
shape as `collection_shares`, written by the API on the service role; the ledger is the delegation section below.

**Migration 647 — `stats_dashboard_shares`**, the third kind, 610's shape over one named Stats dashboard:
owner-stamped by `sync_account_id_from_dashboard()` (insert or a re-pointed `dashboard_id`; it also refuses the
unnamed working board), one `stats_dashboards_shared_read` `FOR SELECT` arm (named rows only), the party policy with
`not acting()`, created by `POST /stats/dashboards/{id}/shares` (tenant gate, then the service role);
`share_party_emails()` reads all three. A dashboard's name never crosses NULL (`stats_dashboards_name_guard` /
`guard_stats_dashboard_name()`), so a direct PostgREST UPDATE cannot turn a shared dashboard into a working board.

## Delegation (migration 641)

Two questions, two functions. `current_account_ids()` answers **who the caller IS** and stays
untouched; `acting_account_ids(for_board boolean default false)` (`SECURITY DEFINER`, `RETURNS TABLE (account_id
uuid)`) answers **whom the caller acts FOR on curation**. Exactly eleven policies read the second, in
`USING` and `WITH CHECK` (switched by `ALTER POLICY`; the three pipeline tables pass `true`): `collections`, `tags`, `property_notes`,
`collection_properties`, `property_tags`, `property_note_attachments`, `property_artifact_links`,
`property_dismissals`, `property_pipeline`, `pipeline_stages`, `property_pipeline_events`. **Every
other table keeps `current_account_ids()`** — billing, estimations, watchdog, presets, notifications (their routes never act
as another account; the push tables, `push_subscriptions` and `notification_deliveries`, are admin-only, migration 654),
the three share tables, `share_party_emails()` — so a delegate never reads, writes or spends as the owner
there (`docs/design/pipeline-sharing.md` §3.7; pinned offline by
`test_exactly_the_eleven_curation_policies_act`).

- **The header and the method.** `x-act-as: <owner account>` rides the request; PostgREST exposes it as
  the transaction-local GUC `request.headers` (JSON, lower-case keys) and the method as
  `request.method`, and the API's tenant transaction sets the same two next to `request.jwt.claims`
  (bind parameters). `acting()` is true when the header is non-empty. Without it,
  `acting_account_ids()` returns exactly `current_account_ids()`'s rows (641's DO block checks up to
  50 members, all of them today). With it, the function returns the owner when a share from that
  owner to the caller exists — `edit` on any method and table; `read` on `GET`/`HEAD` only and on the
  three pipeline tables only (`for_board`; ruling 2: a read share sees the board, never the owner's
  notes, files, tags, collections, dismissals or links; a missing method counts as a write) — and
  **nothing** otherwise. The API runs a GET/HEAD that acts `READ ONLY`, as PostgREST does.
- **Replace, never union.** While acting, the eleven tables show the owner's rows and none of the
  caller's own, so the card map, the entry stage, the dismissal rule and Browse's curated scope stay
  single-account. The child triggers (292's shape) read the owner's parent under the acting identity
  and stamp the owner's account, which `WITH CHECK` accepts in edit mode.
- **Fail closed.** A stale, revoked or forged header yields an EMPTY identity, never the caller's own
  rows; a header that is not a uuid is an error. A read grant's write sees no rows (UPDATE/DELETE
  touch 0, INSERT is 42501).
- **The share rows are the caller's own consent**, so every party policy (`collection_shares_party`,
  `pipeline_shares_party`, `stats_dashboard_shares_party`) carries `and not acting()`: while acting the five
  `*_shared_read` arms add nothing, and a delegate neither sees nor revokes the owner's shares.
- **The ledger.** `delegated_edits` (append-only; `actor_user_id` = the JWT `sub`, `actor_account_id`
  = the share's recipient, `owner_account_id`, `relation`, `row_key` = the PK values joined by `:`,
  `op`, `at`) is written only by `stamp_delegated_edit()`, ONE `SECURITY DEFINER` trigger function on
  the eleven tables (`AFTER … FOR EACH ROW WHEN (acting())`, the PK column names as trigger
  arguments). `authenticated` holds SELECT only; the owner reads it (`delegated_edits_owner_read`),
  nobody else. A row's stamp is defined once, `latest_delegated_edit(relation, row_key, changed_at)`
  (invoker: its latest delegated edit, NULL when the row changed after it, so the owner's own later
  write clears it); `pipeline_stages_public`, `property_pipeline_public` and `pipeline_board_public`
  append it as `edited_by_account_id` + `edited_at`, and the API's note and card projections call it.
  `share_party_emails()` (every share kind, plus every actor in the caller's own ledger, so a revoked
  delegate stays named) is the label door; deleting the actor's account nulls the actor, not the row.
- **One notification kind.** `notification_dispatches.source_kind = 'share'` for every kind
  (`collection_id` set for a collection share, NULL for a pipeline or dashboard share; 641 renamed
  `collection_share`); `api/shares.py` writes all three with `change_kind` `shared_<collection|pipeline|dashboard>`
  (652), the sharer as `actor_account_id` and the dashboard id or the mode as `event_ref` and NO text (656 dropped
  `message`: the bell names the sharer through `share_party_emails()`, so the address drops out once no share joins the
  two, unless the sharer is an actor in the recipient's own `delegated_edits` ledger, whose stamp outlives a revoke; and
  the thing by its live name), conflicting on the
  one event identity (`EVENT_IDENTITY`, migration 653; 656 dropped `dedupe_key`), so a share notifies once per
  identity: (recipient, collection or dashboard, actor), and for a pipeline (recipient, owner, mode) — a
  widening to edit is a second notification. In-app and pushed (W4; collection-sharing ruling 5(e) superseded
  2026-10-21): at most one push per (sharer, recipient) a day, the rest `skipped/capped` in the ledger, bell only (R11).
- **The API declares, per write route, which account it writes.** `tenant_conn` carries the header
  and the method into the transaction (`tenant_pool.tenant_transaction(claims, act_as=, method=)`,
  both required); `require_acting_account_id` (curation writes: own account, or the owner the
  function grants, else 403) or `require_account_id` (everything else: own account, 403 under the
  header). Sharing never delegates: every share-create route declares `require_account_id`, and a
  delegate may not switch on the owner's collection monitoring (403). `tests/api/test_acting_routes_census.py`
  pins that the first writes only the eleven tables, the second none of them (or says why), and that an
  RLS-only route that reaches the service role or mixes curation with other writes declares one.
- **Tests** scope through ONE helper, `tests/_tenant_scope.scoped(sub, act_as=, method=, cur=)`; the
  four-tenant lane is `tests/test_pipeline_shares_live.py`.

## Composite PK: `property_pipeline` (migration 295)

The **only** table where the primary key itself changed, not just an added/scoped
column. Explicitly **gated** — its header states it must apply in the same deploy window
as, and strictly after, the matching Python rewrite (`api/pipeline.py`'s
`ON CONFLICT (account_id, property_id)` in add/move, `toolkit/pipeline_identity.py`'s
account-partitioned merge/detach carriers). Applying it before the Python ships is a hard
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
drop the retired card — a correctness bug, not just an isolation one; since migration 612 the
event ledger's `from_stage_id` / `to_stage_id` are account-qualified the same way.

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

**`legacy_backfill_claim` is gone (613).** It was the first-signup CAS that handed the
pre-tenancy rows to the operator's account (claimed 2026-07-11); 612 removed the branch that
read it, 613 dropped the table (operator ruling 2026-10-17; the row is in 613's header).

## The tenancy doctrine — four shapes, and only four

This is the canonical statement. The `database` skill body carries the four-line summary;
`docs/architecture.md` (rules 18/22) points here; nothing else restates it. If you find a
fifth shape in the code, it is a bug or an undocumented exception — make it one of these
four, or add it here with a reason, but do not leave it unnamed.

**1. Reads on a tenant connection are scoped by RLS alone.** `current_account_ids()` — the
SECURITY DEFINER function every per-account policy already calls — is the ONE definition of
who the caller is (on the eleven curation tables the policies read `acting_account_ids()`, which
equals it without the `x-act-as` header; § Delegation). It is **plural**, because membership is plural: a user may belong to more
than one account, and any code that picks "the" account re-defines the caller more narrowly
than the database does. A read route on `tenant_conn` therefore takes **no account argument**
and its SQL carries **no account predicate**. An `UPDATE`/`DELETE` *by id* is this same shape:
the policy's `USING` clause is the row set, so supplying an account would be a second
definition of it, not a gate.

**The write is the gate.** On a tenant connection, ownership is proven by the `UPDATE`/`DELETE`
itself — rowcount 0 (or no `RETURNING` row) → 404, before any side effect — never by a `SELECT`
probe first: a `SELECT` proves only visibility, and once collections can be shared
(`docs/design/collection-sharing.md`, W4) visibility is wider than ownership, so a probe would pass
and the write would 500 on `WITH CHECK` or report a false success. A refusal that depends on the row (the system
collection's 409) is raised inside the transaction so it rolls back; a pre-write check that must
see the row takes `SELECT … FOR UPDATE`, which applies the UPDATE policy. Example:
`api/curation.py` (`update_collection`, `delete_collection`, `add_properties_to_collection`,
`update_note`) and `api/note_attachments.add_attachment`.

**2. Writes carry exactly ONE account, resolved once at the route edge.**
`tenant_pool.require_account_id` (FastAPI dependency; a curation write declares its twin
`require_acting_account_id`, § Delegation) resolves it and raises
`400 "no account for caller"` when there is none. Never a silent `None` (the account columns
are `NOT NULL`, see the per-table section, so a forwarded NULL is an empty result set or an
opaque 500) and never a `SYSTEM` fallback (migration 290's `WITH CHECK` has no SYSTEM arm to
accept it, so that fallback could only ever 500). The single best shape is the one where the
route names no account at all because a **BEFORE INSERT trigger derives it from the parent
row** (migration 292): `POST /collections/{id}/properties` and `POST /properties/{id}/tags`
cannot be got wrong, because there is nothing at the route to get wrong.

**3. An explicit `account_id = %s` predicate belongs ONLY on a service-role connection.**
There, RLS is off (BYPASSRLS) and the predicate is the SOLE gate rather than a second opinion
about a caller the database has already scoped. The live sites: `toolkit/property_carriers.py` and `toolkit/pipeline_identity.py`
(the carriers, which run inside `merge_property_set`'s service-role transaction
and must partition every join between the retired and survivor sides: by an explicit `account_id`,
or through a key one account owns, as a `CurationTable`'s collection/tag/subscription id does; and the
routing inside `detach_listings`, whose carry-row identity, twins and conflicts all name the account), the
merge's receipt (`api/property_merge.merge_receipt`: the caller's account's own carry rows), the split's preview
and receipt (`toolkit/property_split`: the caller's account's own items only, nothing of another's), `api/estimation_runs.py`
(service-role child runs from `building_runs`), the Stripe webhook (no caller identity at
all — the HMAC over the raw body is the auth), and both Valuo routes (`api/routes/valuo.py` →
`api/valuo.py`; `valuo_valuations` is RLS-on with zero policies): the account is
`require_account_id(conn, claims, None)` called on the service-role connection — the one no-account 400,
with no tenant transaction left open across the POST's two paid calls (`market-value`, then
`advert/detail`), each bounded by `valuo_client.TIMEOUT` (5 s connect + 20 s read), ~50 s in all.
On a tenant connection the same predicate is the #917 bug: `POST /listings/lookup` bound `NULL`
into three `account_id IS NOT DISTINCT FROM %s` predicates for seven weeks and answered "not in
pipeline" for every caller.

**4. `deps.account_scope` returns `[account_id, SYSTEM]`** — the fourth shape, named here so
nobody rediscovers it as a divergence and "fixes" it. The `/estimations` read family
(`GET /estimations`, `GET /estimations/latest-by-listing`) runs on the SERVICE-ROLE connection
on purpose: its `LEFT JOIN` onto `listings` (RLS-on, zero policies) would
silently NULL `locality_display` on a tenant connection. So it scopes with an explicit list,
and that list mirrors migration 291's three-arm policy (own account OR SYSTEM OR platform
admin) rather than inventing a second tenancy definition. It never widens and never returns
empty: an unresolvable account narrows to `[SYSTEM]`, a missing or bad credential raises.

**Named exception — `POST /estimations/{run_id}/feedback`: a short tenant transaction, then
admin-only service-role work.** The route does not take `tenant_conn`; it opens
`tenant_pool.tenant_transaction(claims, act_as=None, method="POST")` in the handler, for three reasons:

- *The write gate is narrower than the read policy.* `estimation_runs`' three-arm read policy
  (migration 291) passes every SYSTEM run, and migration 292's trigger stamps a child row with
  its parent's account, so a non-admin's note on a SYSTEM run would be readable by every
  account. A non-admin's gate is therefore `estimation_run_owned`
  (`account_id IN (SELECT current_account_ids())`: the same one definition, minus the SYSTEM
  arm); an admin's is `estimation_run_visible` (plain RLS). Both probes are strict: an error
  raises. `_fetch_run`'s stub-on-error would read as "visible" and must never be a gate.
- *The note is a shape-2 write.* The gate and the `INSERT` share the short tenant transaction.
  The 292 trigger derives the account, and `WITH CHECK` backs the gate. The route names no
  account.
- *The refiner rides the service role, after the commit, and only for an admin.* It reads
  `skills` / `app_settings` and inserts `skill_refinements` (none granted to `authenticated`),
  and that insert's FK onto `estimation_feedback` needs the note committed. Running it after
  the block also means no pooler backend sits idle in a tenant transaction across the LLM call
  (the W1-1 boundary `POST /estimations` keeps too). The service connection opens only after
  the gate passes. Admin-only, because the refiner is unmetered LLM spend and its row carries
  the platform skill prompt; a non-admin's `kick_off_refinement` stores the note as
  `submitted`.

The route-scope census excludes it structurally (it reaches `verify_jwt`, not `tenant_conn`).
`test_estimation_child_routes_ride_the_tenant_connection` holds its shape instead, and
`tests/api/test_estimation_child_routes_tenancy.py` holds its behaviour.

**Second instance — `POST /collections/{collection_id}/shares`: the tenant gate, then a
service-role write.** Browser roles hold no INSERT on `collection_shares` (uuids are learnable)
or on `notification_dispatches`, so the share row and the recipient's notification are written
on the service-role connection. The order is fixed: the tenant `UPDATE collections SET
updated_at = now()` is the ownership gate (shape 1, rowcount 0 → 404; `shares.owner_gate`, which the
dashboard share route runs too) BEFORE the service-role
connection even opens (`deps.open_background_conn` in the handler, never a dependency; pinned by
`test_the_share_route_opens_the_service_role_only_after_its_gate`); the email resolves on the
service role (`tenant_pool.account_for_email`); the share and its dispatch are one service-role
transaction; the response is re-read on the tenant connection. Unlike the feedback route it keeps
the request-long `tenant_conn`: its service-role work is two fast INSERTs, not an LLM call. The gate's row lock (`FOR NO KEY UPDATE`) does not block the share's FK check
(`FOR KEY SHARE`), so holding it across the service-role write cannot self-deadlock.

**Third instance — the push device routes (`api/routes/push.py`, migration 654): the caller's own account, then the
service role.** `push_subscriptions` and `notification_deliveries` are RLS-on with NO policy and no browser grant, registered in
both `_ADMIN_ONLY_RELATIONS` lists (not tenant tables: no browser DML). `PUT`/`DELETE /push/subscription` and `POST
/push/opened` declare `require_account_id` (in `_NEVER_ACTS`: a device belongs to the signed-in user, so a delegate's phone gets
only the delegate's alerts and nobody registers a device for an account they act for), then `deps.open_background_conn` in the
handler (pinned beside the share routes by `test_the_share_routes_open_the_service_role_only_after_their_gate`); every
statement names that account (shape 3), and `opened` reaches the account through the delivery's event. `GET /push/key` is
`require_token` with no connection (the public key derives from `VAPID_PRIVATE_KEY`). The worker's push lane routes by ONE
join, `ps.account_id = d.account_id AND ps.ended_at IS NULL`: recipients come from the event's account at send time.

**The only `auth.users` touchpoints:** `account_email(uuid)` (migration 610; EXECUTE revoked
from every browser role), called by `share_party_emails()` (SECURITY DEFINER, the caller's
counterparties only) and by the admin entitlements route; and the share route's email lookup,
`tenant_pool.account_for_email` (service role, confirmed emails only). A share is never membership:
`current_account_ids()` is untouched — a collection share widens what the caller reads as themselves; a
pipeline share lets the caller ACT AS the owner on curation (`acting_account_ids()`, § Delegation).

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
  structurally, not enumerated, and so is the named exception above. It has its own
  non-vacuity sentinel.
- `tests/api/test_account_scope_census.py` — the ACCOUNT-SCOPE CENSUS, one layer down: no
  `account_id`/`account_ids` parameter in `api/` defaults to `None`, and every hand-rolled
  `resolve_account_id` call is enumerated with a reason. Both arms declare their blind spots.
