# Public-release remediation, round 2 — fixes for the #856 exit-gate audit

**Date:** 2026-07-21. **Status: in execution — PR-A shipped (migration 340). B-G pending.**

The `/code-review ultra` audit of PR #856 (the migrations 329–332 batch, live in
production) returned 13 findings. Before planning, every finding was re-verified by a
6-agent workflow against the repo (`origin/main`) and the **live DB** — adversarially,
with empirical reproduction of each claimed evasion. Verdicts: **11 confirmed, 1 refuted,
1 partial**, plus **one escalation the audit itself missed** (the MAINTAIN revoke has
already drifted back — see G7).

Companion to `public-release-remediation-2026-07.md` (round 1). Work = **7 PRs (A–G), in
order**. A is the only live exposure; B/C are latent multi-tenant blockers; D–G are the
lanes that keep it all honest.

## Verdict table

| # | Audit finding | Verdict | Disposition |
| --- | --- | --- | --- |
| G1 | `property_estimates_public` cross-tenant leak (owner rights bypass `estimation_runs` RLS) | CONFIRMED — but **latent/inert**: all 98 runs sit on the shared sentinel account `0000…0000`, which is `estimation_runs.account_id`'s DEFAULT and world-readable **by the RLS policy itself**; activates only when the write path stamps real accounts AND a 2nd tenant exists | PR-B |
| G2 | Non-admin deny check vacuous (empty CI DB) | CONFIRMED for **21/23** views + the 3 set-returning functions; `dedup_engine_flow_public` and `publication_gate_health_public` are bare aggregates (1 row on empty input) and already non-vacuous | PR-D |
| G2b | (extension) scalar-RPC NULL checks also vacuous? | **REFUTED** — all 5 return non-NULL on an empty DB if the gate is dropped (bare aggregates / coalesce defaults / migration-seeded `portals` rows), so `is None` is a real signal. One WRONG inline comment to fix ("admin legitimately reads NULL here" — false, admin reads non-NULL) | PR-D |
| G3 | `scrape_runs_public` / `recent_scrape_runs()` ungated | CONFIRMED **live-exploitable**: as `authenticated`, 7 945 rows via the view, 2 166 via the RPC (per-run counts, error blobs). Missed because `scrape_runs` was absent from `_ADMIN_ONLY_RELATIONS` (list was seeded from 318's objects, not a table inventory). Full first-principles sweep found exactly one more sibling: `worker_liveness` (ungated but no browser grant — latent) | PR-A |
| G4 | Live gate accepts OR'd-in gate | CONFIRMED — `_GATE_IN_PREDICATE` matches `WHERE x=y OR is_platform_admin()`; the generalized sweep is a bare substring (`NOT LIKE`). Offline lane has **no behavioral backstop**, so an OR'd gate in a new migration merges uncaught | PR-D |
| G5 | Offline gate evasions ×3 + coverage floor | CONFIRMED, all four empirically reproduced — incl. that `_strip_comments`' literal-blindness is triggered by **real** SQL shapes, and mig 299 genuinely creates relations via dynamic `EXECUTE` (invisible to the scanner) | PR-D |
| G6 | anon matview-write check tests INSERT only | CONFIRMED (mig 331's own post-condition has the same asymmetry) | PR-D |
| G7 | MAINTAIN untested (PG17-only, CI is PG15) | CONFIRMED + **ESCALATED**: the postgres **default ACL** grants `authenticated` SELECT+MAINTAIN (`rm`) on every new relation → `properties_map_mv`'s blue-green rebuild re-granted MAINTAIN within ~30 min of mig 331 stripping it (verified: gone 07-20, back 07-21); 84 base tables carry it too. One-time revokes cannot hold; the fix is a default-ACL revoke | PR-C (+ test in PR-D, CI bump PR-G) |
| G8 | Doc inconsistency on bare-gate perf | CONFIRMED — the `database` skill states the per-row rule as universal; it only holds when the gate is OR-ed with a column Var (the 275 case). Live EXPLAIN re-confirmed One-Time Filter for the standalone form | PR-F |
| G9 | Two new multi-paragraph docstrings | CONFIRMED — both added by the very PR that fixed F13 (`test_tenant_view_scopes_both_ways`, `test_no_ungated_relation_reads_admin_only_data`) | PR-D |
| G10 | Migration-number reuse trap | CONFIRMED — the round-1 doc uses "332" for both the shipped health-RPC gate and the deferred matview repoint; roadmap is clean | PR-F |
| G11 | Lock-contention risk (revoke-all-matviews in one tx) | CONFIRMED as real-but-low-probability, retrospective; standing guidance needed (GRANT/REVOKE takes ACCESS EXCLUSIVE; a stall pins every already-revoked relation for the tx) | PR-F |
| G12 | No standing test for API `require_admin` coverage | CONFIRMED — no test walks `app.routes`. **Adversarial scan REFUTED any actual gap**: all 190 route-method pairs classified (88 admin / 89 token / 10 tenant / 3 public); every admin-class route is gated. Observation, no action: `GET /brokers/{id}/contacts` (PII) is token-gated while broker mutations are admin-gated — documented deliberate, strictly narrower than admin for JWTs | PR-E |

## Ground rules

- Migration numbers below are **next-free at apply time** — the listing-identity track is
  active and took 333–339 during round 1's execution. As of this writing the next free is
  **340**; re-check `ls migrations/ | tail -3` before every apply and renumber forward.
- Same conventions as round 1: `database` skill loaded, additive → autonomous, apply via
  MCP, migration file on the branch byte-matches what was applied, live-verify both
  directions before pushing, run the live suite, one branch + PR per item, roadmap
  bookkeeping rides each PR.
- New rule from G11 (adopt immediately, PR-F codifies it): any migration that
  GRANT/REVOKE/ALTERs a hot or cron-refreshed relation opens with
  `set local lock_timeout = '5s';` so it fails fast instead of queuing behind — or ahead
  of, thereby blocking — a pg_cron refresh.

---

## PR-A — gate `scrape_runs_public` + `recent_scrape_runs()` + `worker_liveness` (G3)

**✅ SHIPPED 2026-07-21 — migration 340.** Live-verified: as a non-admin `authenticated`
session the view went **7 945 → 0 rows** and the RPC **2 166 → 0**; the admin/service path
and the Health dashboard are unaffected.

**Branch `fix/scrape-runs-admin-gate` · one migration · the only live exposure.**

Live proof: `SET LOCAL ROLE authenticated` reads 7 945 rows from `scrape_runs_public` and
2 166 from `recent_scrape_runs(14)` — scraper run bookkeeping (new/updated/inactive counts,
image volumes, error blobs, per-category breakdowns). Sole reader is the admin Health page
(`queries.ts:1485 → Health.tsx`), route-gated only. `scrape_runs_public` itself has **no**
code reader — it exists only as the RPC's helper.

Migration (agent-drafted, live-validated — key decisions):
- `scrape_runs_public` → 318 wrapper pattern (`select * from (…14 cols…) __admin_gate
  where is_platform_admin()`), body reproduced verbatim from the live def.
- `recent_scrape_runs(p_days integer default 14)` → 332 pattern: SECURITY DEFINER +
  `where … and is_platform_admin()`, **repointed at the base table** — reading the
  `_public` view would leave the function a transitive blind spot the standing
  function-sweep can never see. `RETURNS SETOF scrape_runs` column-compat verified.
- `worker_liveness` (over `worker_heartbeats`) → same wrapper. Latent (no browser grant)
  but gate-the-class; its only non-admin reader is `scripts/verify_pipeline.py` over a
  claims-less bypassrls connection, which passes the 330 fallback — behaviour-preserving.
- Function EXECUTE: `revoke … from public, anon; grant … to authenticated;` (the 287
  default-ACL re-grant lesson). Views keep their ACL across CREATE OR REPLACE — no re-grant.
- Post-conditions: data-independent only (gate present in defs, function DEFINER+gated,
  anon dark, `perform 1 … limit 1` queryability probes).

Test-list edits in the same PR (exact placements verified against `origin/main`):
- `_ADMIN_ONLY_RELATIONS` (**both** files): add `"scrape_runs", "worker_heartbeats"`.
  These move together with the gates — adding `worker_heartbeats` without gating
  `worker_liveness` fails the live sweep. Verified: no migration in 333–339 references
  either table, so the offline gate stays green retroactively.
- `_ADMIN_GATED_VIEWS`: add `scrape_runs_public`. Do **NOT** add `worker_liveness` — it
  has no authenticated SELECT grant, so the deny test's `count(*)` would hit
  permission-denied instead of 0; the generic sweep covers it.
- `_ADMIN_GATED_FUNCTIONS`: add `recent_scrape_runs`.
- Do NOT add `image_clip_tags` / `*_record_history` / `broker_resolution_runs` to the
  sensitive list (would false-flag the intentional `images_public`; trigger-returning
  functions are uncallable; broker surfaces are covered by `_BROKER_PII_RELATIONS`).
- Sweep re-confirmed the three deliberately-open views (`listing_freshness_checks_public`,
  `browse_read_model_state_public`, `portal_listing_counts`) stay open.

Live verification: authenticated+foreign-JWT → 0 rows/all three; admin/service → data;
Health page still renders for the operator.

## PR-B — per-account scoping for `property_estimates_public` (G1)

**✅ SHIPPED 2026-07-21 — migration 341.** Live-verified: a tenant still sees **58 rows**
(the shared-SYSTEM arm preserves Browse's "with estimates" filter, where the naive
two-arm predicate returned **0**).

**Branch `fix/estimates-per-account` · one migration.**

**Fix-path landmine found during verification:** the "obvious" predicate
(`er.account_id in (select current_account_ids()) or is_platform_admin()`) was probed live
under `SET LOCAL ROLE authenticated` + fake sub → **0 rows** — it would re-empty Browse's
"with estimates" filter, reproducing the exact 316 regression this batch started from.
All 98 live runs sit on the shared sentinel account, whose visibility comes from the RLS
policy's *unconditional* zero-UUID arm. The view predicate must mirror **all three arms**
of `estimation_runs_tenant_read` verbatim:

```sql
and ( er.account_id in (select current_account_ids())
   or er.account_id = '00000000-0000-0000-0000-000000000000'::uuid
   or (er.account_id is null and is_platform_admin()) )
```

- Option A (flip `security_invoker` back on, join `listings_public`) was **rejected on
  live evidence**: `listings_public` exposes neither `source_url` nor `property_id`, so it
  cannot serve the view's two join arms without widening it — and invoker rights
  re-introduce the 316 fragility. Option B (owner rights + in-body mirror of the RLS
  policy, both arms of the UNION) touches exactly one view; the three-arm predicate was
  probed live: tenant sees 74 rows (all sentinel), naive form sees 0.
- `CREATE OR REPLACE VIEW` preserves reloptions (`security_invoker=false` from 329), the
  authenticated grant (319) and the anon revoke (331) — re-issue nothing.
- Post-conditions: reloption still `false`; `current_account_ids` present in the def;
  queryability probe. No row counts.
- **Recorded semantics:** runs on the shared sentinel stay market-wide (that IS today's
  Browse behaviour, and it matches the RLS policy); a run stamped with a real account
  becomes visible only to that account + admins. `browse_stats_properties` (SECURITY
  INVOKER, does an EXISTS over this view) inherits per-account semantics automatically.
  Known drift risk: the predicate is a hand-copy of the RLS policy — same tradeoff as
  every 318 inline gate; the new test pins it.

Test rework in the same PR (all in `tests/test_tenant_isolation_live.py`):
- **Delete** `test_market_view_readable_by_authenticated` — its scoped == service-role
  read-parity assertion is now wrong by design (passes only vacuously on empty CI).
- **Keep** `_MARKET_VIEWS` + `test_market_view_not_security_invoker` (the view must stay
  owner-rights; that guard is still correct); update the surrounding comments ("owner
  rights so the zero-policy listings join returns the market; scoping lives in the body").
- **Add** `seeded_estimate_rows` fixture (one success run per account A/B + one sentinel,
  each on its own property via a seeded sreality listing — constraints verified live:
  `estimation_runs` requires source/mode/status/input_spec with enum CHECKs; `listings`
  requires `sreality_id>0 ∧ source='sreality'` (sign check), `source_id_native`,
  `raw_json`) and `test_estimates_view_scopes_per_account` asserting all six directions
  (A sees own+sentinel, not B's; B symmetric).

## PR-C — revoke MAINTAIN durably (G7, escalated)

**✅ SHIPPED 2026-07-21 — migration 342.** Live-verified: MAINTAIN holders **85 → 0**, and
the postgres default ACL for tables went `authenticated=rm` → **`authenticated=r`**, which
is what stops the drift-back. `authenticated` still reads shared-market tables.

**Branch `fix/maintain-default-acl` · one migration · PG17-guarded.**

Root cause (live): `pg_default_acl` for grantor postgres grants `authenticated`
SELECT+MAINTAIN on **every new relation** — so mig 331's one-time revoke was undone for
`properties_map_mv` by the next blue-green rebuild (~30 min), and 84 base tables carry
MAINTAIN besides. MAINTAIN permits REFRESH/VACUUM/ANALYZE/CLUSTER/REINDEX/LOCK. Not
reachable via PostgREST/tenant-pool (no utility statements) → latent posture hole, frame
as hardening, not incident.

Migration (single PG17-guarded DO block; returns early on the PG15 CI replay):
1. `alter default privileges for role postgres in schema public revoke maintain on tables
   from authenticated;` — kills the drift-back vector.
2. Loop-revoke MAINTAIN from anon+authenticated on all existing `relkind in ('r','m','p')`
   holders (85 today). **Exclude plain views** (`'v'` — MAINTAIN is meaningless there and
   the revoke can error).
3. Post-condition: zero browser-role MAINTAIN across tables/matviews.
4. Per G11: open with `set local lock_timeout = '5s'`.

Live verification: holder count 85 → 0; wait one `properties_map_mv` rebuild cycle
(~30 min) and re-check it did **not** come back (the default-ACL change is what makes this
pass where 331 failed).

## PR-D — make the standing gates honest (G2, G2b, G4, G5, G6, G7-test, G9)

**✅ SHIPPED 2026-07-21.** Every claim empirically validated rather than asserted:
`gate_is_sound` accepts all **35** live gated objects and rejects all **8** adversarial
forms; the historical-exemption lists are **exact** (8 hits / 8 entries, no dead weight,
no gaps); the seed's view coverage was measured by seeding production inside a
rolled-back transaction and diffing per-view counts (**17 of 19** reached).

**Branch `fix/gate-lane-hardening` · no migration · the biggest PR.**

1. **Shared gate-shape module** `tests/_admin_gate_shape.py` (G4): `gate_is_sound(defn,
   kind)` = reject OR-adjacency/tautology first (`\bor\s+\(?\s*(select\s+)?is_platform_admin`
   or `is_platform_admin\(\)\s*\)?\s*or\b`), then require the gate in a restricting
   position per kind — views: the `__admin_gate … where is_platform_admin()$` wrapper tail
   or a WHERE-clause gate; functions: WHERE gate or the `case when is_platform_admin()
   then` head. **Empirically validated** against all 23 gated views + 3 gated functions +
   5 scalar RPCs (all pass) and 8 adversarial forms (all fail) — in both Python `re` and
   Postgres `~*`. Both lanes switch to it: `_GATE_IN_PREDICATE` and the two
   `NOT LIKE '%is_platform_admin()%'` guards are replaced.
2. **Offline-lane parser hardening** (G5): string/dollar-quote-aware `_strip_comments`
   (char scanner: `--`, nesting `/* */`, `'…''…'`, `$tag$…$tag$`) + matching
   `_split_statements` (`;` outside strings/dollar-quotes, so a whole DO block or function
   body is ONE statement). These feed `_statements()`, hardening **all five** offline
   rules at once. Validated: created-base-tables set unchanged (10==10) on the real
   migration corpus.
3. **Coverage floor** (G5): delete `MIN_VIEW_GATE=333`; rule 5 scans from `MIN_ENFORCED`
   (299) with an 8-entry `_HISTORICAL_UNGATED` frozenset (`"<file>:<object>"` — objects
   created ungated in 300–311 and re-gated live by 318; enumerated by running the
   tightened scanner over 299–332, each verified sound in prod today). Migrations stay
   append-only — exemptions live in the test, never in old files.
4. **Dynamic-DDL rule** (G5): flag `execute format('create …view/function…')` /
   `execute <variable>` in enforced migrations unless the file carries
   `-- ci-allow-dynamic: <name> <reason>`; `_DYNAMIC_DDL_HISTORICAL = {299}` (real dynamic
   creates: `browse_list_next`, `properties_map_mv_next`). Validated: trips only 299 in
   the historical corpus.
5. **Deny-test positive controls** (G2): new module-scoped `seeded_admin_rows` fixture —
   one service-role row per *seedable* gated view (coverage map drafted and
   constraint-checked live: 2 shared properties + 2 images + 1 active listing feed the
   pair/image/quality views; single-row seeds for dedup_engine_runs, dedup_scan_state,
   bakeoff, queue/completions, fetch_failures, llm_calls, parsed_url_cache,
   pipeline_check_results, phash/border/annotations/training, identity_candidates,
   decision_feedback). The deny test then asserts: service-role sees the seed (positive
   control — proves the seed reaches the view), non-admin sees 0 (now meaningful). The 2
   bare-aggregate views need no seed; the 2 matview-backed views + 3 set-returning
   functions stay structural-only with an honest comment (refresh-from-empty is empty).
   Implementation notes: capture `dedup_engine_runs.id` via `RETURNING` for cleanup;
   `listings` seed may fire snapshot/dirty triggers — clean `listing_snapshots`
   defensively first; teardown children-first.
6. **G2b comment fix**: the admin-direction comment "a fresh schema replay never refreshes
   these matviews, so an admin legitimately reads NULL" is factually wrong (all 5 RPCs
   return non-NULL on empty CI); correct it and strengthen the two unconditional cases
   (`image_storage_overview`, `category_trends('sreality')`) to assert non-NULL for the
   admin.
7. **G6 symmetry** + **G7 test**: extend `test_matviews_not_writable_by_browser_roles` to
   anon UPDATE/DELETE, and add a `server_version_num >= 170000`-guarded MAINTAIN probe
   (no-op on PG15 CI today; bites after PR-G).
8. **G9**: collapse the two multi-paragraph docstrings (lines 434-441 and 529-541 of the
   live test file). Module docstrings are treated leniently in this repo — leave those.

## PR-E — standing API `require_admin` coverage test (G12)

**Branch `test/api-admin-route-coverage` · no migration.**

`tests/api/test_admin_route_coverage.py`: imports `api.main:app` (verified: imports clean
with no env/DB — providers construct key-free; lifespan doesn't run on import),
DFS-walks each route's `dependant.dependencies` collecting `.call`s, buckets each
route-method pair (`admin` if `deps.require_admin` reachable; else `tenant` / `token` /
`public`), and asserts every route is `admin` **or** on an explicit allowlist
(3 public / 10 tenant / 89 token — full tables enumerated during verification; includes a
sentinel check that the admin routers actually mounted, and a stale-allowlist check).
Draft file parked at `scratchpad/drafts/test_admin_route_coverage.py`.

⚠ **Validate in CI only.** The local box has Python 3.14 + fastapi 0.139 where
`include_router` silently mounts 0 routes — the test *correctly* fails there. CI runs
Python 3.12 + fastapi ≥0.115 (verified in `test.yml` / pyproject). Do not "fix" the test
to pass locally.

Operator note, no action taken: `GET /brokers/{id}/contacts` (broker PII) is
`require_token`-gated while broker mutations are `require_admin`. Deliberate per the
router header and not an escalation path (the static token is strictly narrower than admin
for JWTs) — the allowlist records it explicitly so it stays a visible decision.

## PR-F — docs + skill corrections (G8, G10, G11)

**✅ SHIPPED 2026-07-21.**

**Branch `docs/gate-perf-and-numbering` · docs only.**

- **G8**: rewrite the `database` skill's per-row paragraph into the three cases —
  (a) bare **standalone** gate ⇒ One-Time Filter, O(1), safe (the 318/332 pattern);
  (b) gate **OR-ed with a column Var** ⇒ per-row (the actual 275 incident);
  (c) `(SELECT gate())` ⇒ InitPlan, the rescue for (b). Net-neutral length
  (docs-budget CI). Mig 318's imprecise comment is append-only — the skill becomes the
  authority; the round-1 doc already flags it.
- **G10**: retitle the round-1 doc's two "§ 332 repoint" headings (the deferral never
  shipped as any number; the real 332 is the health-RPC gate) + one cross-reference line.
- **G11**: ~3-line lock-timeout rule in the `database` skill's migration-safety section
  (fall back to `docs/architecture.md` if the budget trips).

## PR-G — bump the CI schema-replay to PG17 (G7 part C)

**Branch `ci/replay-pg17` · LAST, and only after PR-C is applied live** (else the PG17
replay goes red on the 85 MAINTAIN holders the moment the version-guarded checks activate).

`migrations.yml` heredoc: `FROM postgis/postgis:15-3.4` → `17-3.4`, packages
`postgresql-17{,-postgis-3,-pgvector}` (image + PGDG package existence verified;
`ci_db_bootstrap.sql` is version-agnostic). Expected value: the replay finally runs at
prod parity (17.6), activating the MAINTAIN/anon-DML branches. Expected cost: PG17 may
surface latent replay breaks PG15 masked — that is the point; budget an iteration loop on
this PR and don't let it block A–F.

## Sequencing

| Order | PR | Why here |
| --- | --- | --- |
| 1 | A | live exposure (7 945 rows readable today) |
| 2 | B | latent leak; blocks multi-tenant onboarding |
| 3 | C | durable MAINTAIN fix; prerequisite of G |
| 4 | D | rebases on A's list edits; makes every gate above provable |
| 5 | E | independent; CI-validated only |
| 6 | F | docs; codifies G11 for future migrations |
| 7 | G | after C is live; expect iteration |

## Explicitly not doing

- No change for the brokers-contacts token-vs-admin observation (recorded above).
- No renumbering/edits of any existing migration (append-only); all historical-corpus
  exemptions live in test-file frozensets.
- The round-1 matview-repoint deferral stands — nothing in this audit weakened that call.
