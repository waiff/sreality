---
name: toolkit-api
description: Use when writing or changing analytical toolkit functions (toolkit/) or the FastAPI service (api/) — the facts-not-opinions rule, the standard tool return envelope, the read-only-with-write-exceptions rule, the auth gates (one credential, a Supabase JWT: require_token = any signed-in caller, require_admin/verify_jwt = identity / admin gating / login), the billing/entitlements skeleton (Stripe webhook, plans, agenda gating), the versioned estimation trace, provider pluggability (Anthropic, Gemini, OpenAI, Qwen, OSS), or the full env-var/secrets reference (Postgres, tenant pool, R2 images, LLM+maps keys, API service, scraper orchestration, frontend/extension build-time). Triggers on: new toolkit tool, /admin route, API_AUTH_OPTIONAL, login, admin gating, identity, account menu, billing, Stripe, entitlement, plan, agenda gating, write exception, estimation_runs.trace, llm_calls, provider, env var, secret, R2/ANTHROPIC/GEMINI/MAPY/STRIPE keys, CORS.
---

# Toolkit & API

Rules for the analytical toolkit (`toolkit/`) and the FastAPI service that exposes it
(`api/`), plus the complete secrets / env-var reference. These do not apply to the
scraper. Rule numbers below are cited by code — never renumber.

## Toolkit and API rules

These rules govern the analytical toolkit (`toolkit/`) and the FastAPI service that exposes
it (`api/`). They do not apply to the scraper.

1. **Tools return facts, not opinions.** No "recommended price", no "this looks like a good
   deal." Tools return data + provenance. Reasoning happens at the agent layer.
2. **Standard envelope on every tool's return value** (hand-built per module, no shared constructor; `property_identity` / `location_quality` still omit `data_freshness`):
   ```python
   {
     "data": ...,
     "metadata": {
       "tool": "tool_name",
       "filters_used": {...},      # echo of actual params after defaults applied
       "result_count": int,
       "queried_at": iso8601,
       "data_freshness": iso8601,  # max(last_seen_at) of considered listings, or null
     }
   }
   ```
3. **Every tool excludes `given_up = true` listings** from `listing_fetch_failures` by
   default. An `include_unreliable: bool = False` parameter overrides. Every cohort (`_shared_filter_where`, the listings adapter over `toolkit/filter_compiler.compile_filter_where`: comparables, velocity, the transit corridor) counts a property ONCE: its FROM is `location_data.claims_common.COHORT_LOCATION_FROM` (ruling R8, migration 636: place → the live property it locates → its canonical advert as the comparable, so price, area and snapshot_id are the canonical advert's and distance, district and place the location advert's), and `_shared_filter_where` leaves out every advert of the subject's property via `TargetSpec.exclude_listing_ids` / `TargetIn.exclude_listing_ids` (the ONE exclusion, decision 13: any advert of the subject names its whole property; the sreality-keyed `exclude_ids` is gone, a frozen spec's copy is history and never re-read).
4. **"Active" filter is `is_active = true AND last_seen_at > now() - interval 'X days'`
   (default 7).** Don't trust `is_active` alone — a listing not seen for 30 days is
   functionally inactive.
5. **No writes from the toolkit, with nine explicit exceptions.** Read-only by default. The
   exceptions are:
   - `verify_listing_freshness` (and `scraper.freshness.freshness_check` that it wraps), so
     an agent can confirm a comparable is still valid before relying on it. Every call logs to
     `listing_freshness_checks` and may write a new `listing_snapshots` row; a gone answer nominates
     a drain page check (`QUEUE_PRIORITY_VERIFY`), never flips (rule #3 hysteresis, 2026-10-09).
   - `find_anchor_amenities`, which writes the OSM-mirror tables `amenities` /
     `amenity_fetches` on a cache miss.
   - `find_comparables_along_axis`, which writes the OSM-mirror tables `transit_lines` /
     `transit_line_fetches` (migration 028) on a cache miss.
   - `summarize_listing`, which writes a structured Claude summary to `listing_summaries`
     (keyed on `(sreality_id, snapshot_id)`) on cache miss.
   - `compare_listing_images`, which writes the pairwise visual comparison to
     `listing_image_comparisons` (canonical-ordered pair) on cache miss.
   - `extract_building_units`, which writes the structural unit decomposition to
     `building_unit_extractions` (keyed on `(sreality_id, snapshot_id)`) on cache miss —
     the vision extractor behind the building-paste flow.
   - `read_floor_plan`, which writes a structured Claude-vision analysis of one
     operator-supplied attachment to `building_attachment_analyses` (keyed on
     `(attachment_id, model)`) on cache miss. Only callable inside the building flow; the
     agent handler in `api/agent.py` enforces that the `attachment_id` belongs to the run's
     `building_run_id`.
   - `discover_condition_markers`, which writes a structured list of Czech "condition
     markers" to `listing_marker_extractions` (keyed on `(sreality_id, snapshot_id)`) on
     cache miss — feeds the condition-scoring marker dictionary.
   - `score_listing_condition` (PARKED, rule #14: dispatch-only; nothing reads its levels), which writes per-listing building/apartment condition levels
     (1..5) + matched marker_ids + per-axis confidence to `listing_condition_scores` (keyed
     on `(sreality_id, snapshot_id)`) AND updates the two derived columns on `listings`
     (`building_condition_level`, `apartment_condition_level`) in one transaction, guarded by
     a latest-wins subquery.
   Every write-allowed exception caches an expensive external/LLM fact locally and
   auto-invalidates (a new snapshot or a model bump yields a fresh key); the LLM/OSM source is the truth, the table is a mirror. No other toolkit
   function may write. The API service should still connect with a read-only role if Postgres
   permits; these nine paths then need a separately-elevated route. For now we ship with one
   role and discipline.
   **Broker merge-review writes are durable decisions, not just status flips** (`api/broker_review.py`,
   migration 401). The nightly sweep re-derives every merge from scratch — name-gated and
   portal-agnostic since 2026-08-20 — so an unmerge used to be re-applied the same night.
   `unmerge_group` derives, inside its own transaction and BEFORE the re-point, every cross-owner
   identity pair it pulled apart (SAME-PORTAL PAIRS INCLUDED: within-source merging is allowed now,
   and the biggest duplicate fans are same-portal) and records it in `broker_merge_suppressions`. Both derivations read a **cohort**, never a remembered id: the
   unmerge anchors on where the restored identities live NOW (the survivor on the event rows may itself
   have been merged away since — that read returns nothing and the unmerge silently writes zero rows),
   and a dismissal anchors on the candidate's BROKER pair, suppressing every cross-owner pair between
   them (the card is keyed `contactbridge:{lo}:{hi}` and the evidence is last-write-wins, so sibling
   identity pairs would otherwise stay live). Pairs already under one broker are skipped — an active
   suppression over co-located identities is an instant `verify_pipeline` violation. A `name_firm`
   candidate has no identity evidence: dismiss only. `merge_brokers` LIFTS (never deletes) every active
   suppression whose two identities sit under DIFFERENT brokers being merged — an explicit operator
   merge always beats the rail, which gates the AUTO path only; `GET /broker-review/suppressions` +
   `POST /broker-review/suppressions/{id}/lift` (409 on a re-lift) are the ledger and the manual
   override. Every mutating `/broker-review/*` route binds `require_admin`'s claims and threads
   `claims.get("email") or claims.get("sub")` into `undone_by` / `resolved_by` / `created_by` /
   `lifted_by`.
   **The AUTODEDUP ruling store is written** (mig 528, same `decided_by` idiom) by `POST /autodedup/verdict` (+ `/split`, `/candidate-split`) and, via `toolkit.property_identity.record_rulings`, by `POST /properties/merge` (a statement of letters over properties, `{letters: {id: "A"}}`, `merge_letters` → one `merge_property_set` per letter in ONE transaction: the oldest record survives, every property-anchored operator-state row follows through `PROPERTY_CARRIERS` (an alert the survivor already holds is marked `duplicate_of`, never deleted, mig 653) and each curation row moved or folded is written to the carry record (`property_merge_carries`), `CategoryClash` refuses rule 15 over the set's ads (a Czech 409), ONE `properties_changed` (rollup, Browse row, broker queue) ends it; "same" on the ticked cards' canonical adverts and every negative pair or set across its members (MS12; `GET /properties/merge` counts it before the click); the route answers the acting account's moved items and `hidden_for_you`) and `POST /properties/{id}/split` (MS18, by letters, `expect` = its `GET` preview's `plan`, a preview the click's `choices` can be read into: ONE `detach_listings` (births in `new`, the acting account's routed items in `curation`) + `merge_property_set` per joined letter, which rules as the merge it is; the split's own rulings "different", across letters only). An engine merge / `unapply` (ONE `detach_listings` per group, routing curation by its carry rows) rules nothing. Each appends
   on change (migration 574); a negative PAIR verdict upserts `autodedup.must_not_link` and reversing it DELETEs that row; cluster verdicts add none; only `/autodedup/*` answers an un-migrated store with a 503. Engine SPLITS are propose-only (decision 9): `GET /autodedup/proposed-splits` (`generation`, keyset `after`/`limit`) and `/proposed-splits/{property_id}` read `autodedup/proposed_splits.py` → `{property_id, canonical_listing_id, proposed, groups: [{cluster_key, adverts: [{listing_id, source, is_active, origin_property_id}]}], unseen, splits: [{listing_lo, listing_hi, reason_source, reason, ruling}], ruled}`; each card links to the property page's split dialog (`?letters=`), no write route of its own.
6. **Spatial queries measure in metres, so they cast.** A listing's point is
   `listing_location.geom`, a `geometry(Point,4326)`: always
   `ST_DWithin(ll.geom::geography, target, radius_m)`. Never compute distance in Python.
7. **psycopg directly, not supabase-py.** Same reasoning as the scraper.
   `prepare_threshold=None` for pgbouncer-mode pooler.
8. **One credential — a Supabase JWT; the gate decides what it needs from it, the client never chooses
   per request.** A person (SPA, extension) sends the session JWT; a machine (ClickUp, a script) signs in as
   its own Supabase user and sends that JWT — no static key exists (`API_TOKEN` deleted 2026-10-13).
   `api/dependencies.py` `_bearer` is the ONE reader of `Authorization`: `require_token` = any verified
   JWT, no identity (401 without one, `503` with no verifier; `API_AUTH_OPTIONAL=1` opens it for local dev
   ONLY), `verify_jwt` = the claims, `account_scope` = a read scope; every 401 is RFC 6750
   (`WWW-Authenticate: Bearer error="invalid_token"` + the reason). `/health` stays open for Railway
   healthchecks. `/admin/*` (Settings-page surface) is gated like every write surface.
   **Phase 1 (increments 1–4, #747/#753/#763/#765) layered identity on top**, not instead
   of the token: `/admin/*` (incl. `POST /admin/health/acknowledge {key}`: stamps `ops_incidents.acknowledged_at` on
   that check's open cell incidents on the service role, which stops their reminders), `/properties/merge*`, `/labeling/*`,
   `/outreach/*`, `/broker-review/*`, `/autodedup/*`, `/new-dedup/*`,
   `/skill-refinements/*`, `/location/*` and dataset-write/dispatch routes on price-stats use `require_admin` (JWT-gated, see below) instead of plain `require_token`;
   `/pipeline/*` (+ `GET`/`POST /pipeline/shares`, `DELETE /pipeline/shares/{account_id}?side=shared_with|shared_with_me`, mig 641), `/collections` (+ `POST`/`DELETE /collections/{id}/shares`, mig 610: the tenant UPDATE (own accounts, `require_account_id`) gates, then the service role writes the share + its notification; all three kinds (collections, pipelines, dashboards) are ONE module, `api/shares.py`), `/tags`, `/filter-presets` (per account, mig 595), `PUT`/`DELETE /push/subscription` + `POST /push/opened` (mig 654: `require_account_id`, never acts, then the service role in the handler; `GET /push/key` is `require_token`, no connection), `GET`/`PUT /stats/dashboard` (per account, migs 626 + 647: the account's working board, a whole-document upsert validated by `toolkit/stats_registry.py`) + `/stats/dashboards` (named dashboards, mig 647: the same saved-set CRUD as `/filter-presets`, one module `api/saved_sets.py`, plus read-only shares through `api/shares.py`), `/estimations` create/detail/scenario/trace payload/feedback (its refiner admin-only), notes, a property's files (`/properties/{id}/attachments[/{aid}]`, mig 660),
   `/listings/lookup` (**RLS-ONLY**: it takes no account argument and its SQL carries no account predicate — `current_account_ids()` must stay the ONE membership definition, the same one the SPA reads; a second, explicitly-bound one is what broke the extension 2026-07-23→09-11), `/brokers/*` and `/properties/{id}/valuo` (tenancy shape 3) use `verify_jwt`/`tenant_conn` for per-account
   identity without the admin claim (`GET /estimations{,/latest-by-listing}` take `account_scope`);
   most other routes are `require_token` (any signed-in caller, no identity — buildings,
   the phone's `GET /properties/{id}/location` + `/static-map.png`, the map-overlay registry + tiles `GET /map-overlays*`). `/brokers/*` moved off `require_token` on 2026-08-12 (D1/D2 of the
   broker E2E review): the leaderboard returned up to 2000 brokers' unmasked email +
   phone behind the bundle-extractable token. Every `/brokers/*` envelope now runs
   through `toolkit.brokers.apply_pii_policy`, which swaps any contact column for
   `has_email` / `has_phone` (matched on the column NAME, so a widened view can't leak)
   and stamps `metadata.pii_masked`, unless the caller is admin; `GET
   /brokers/{id}/contacts` is `require_admin` outright — there is no masked variant.
   **One exception (operator ruling 2026-10-18):** `POST /brokers/by-listings` (property page + board card
   broker list) unmasks for EVERY signed-in caller — `ListingBroker` carries `primary_*`, NULL = no channel;
   `_UNMASKED_CALLS` in `tests/api/test_broker_routes.py` lists it, and every new `/brokers` route lands there or masked.
   Under the name rule sits a **shape rule**: a string value that is an email, or that is
   wholly a phone number, is redacted whichever column it arrives in (live brokers whose
   `display_name` IS their email address). The shape rule also reaches the PREDICATE, not
   just the projection: `GET /brokers/search` passes the caller's `include_pii` into
   `toolkit.brokers.search`, which for a non-admin keeps only rows still matching once
   `_redact_shaped` has been applied — an ILIKE over the raw column was a guess-confirming
   oracle that recovered a redacted address one character at a time. The bound term is also
   LIKE-escaped with backslash, LIKE's default escape (`%` / `_` in a bound value are still
   wildcards, so `@_` walked under the two-character minimum). Firm identifiers (`firm_name`, `firm_domain`) are deliberately
   NOT masked — `pii_masked` promises that contact VALUES are masked, not that the row is
   unattributable. `has_email` / `has_phone` mean "a current
   primary contact is on file" (the `brokers` rollup's `primary_email`/`primary_phone`),
   not "every address ever seen" — that fuller set is `broker_identity_contacts`, admin-only
   via `/contacts`. Both batch reads bound their input at `toolkit.brokers.MAX_BATCH`
   (1000) at the HTTP layer, so an over-cap batch is a 422 rather than a silently
   truncated 200, and `?geo_level=` accepts only `region`/`okres` (`GEO_LEVELS`).
   `tests/api/test_broker_routes.py` holds two standing gates: no `/brokers` route may
   ride `require_token`, and every non-admin one must stamp `pii_masked` — a new route
   must be added to that table. The SPA is the main consumer since 2026-08-12
   (`frontend/src/lib/brokers.ts`, on the session JWT); it previously read the
   underlying views straight off PostgREST and had been dark since migration 299 revoked
   them. Migration 395 revoked the last one A6 missed (`firms_public`, default-ACL drift).
   Because the cap is a 422 on the WHOLE batch, `fetchListingBrokersByIds` chunks
   client-side (supabase-js `.in()` had no such cap, so an oversized board would otherwise
   lose every card's broker, not just the overflow). Its `fetchBrokersByIds` twin is
   **deleted** (hydration sprint W6): migration 419 put `primary_email`/`primary_phone` on
   `listing_broker_public`, so `POST /brokers/by-listings` answers identity AND contact in
   one row and the SPA no longer chains a second, serialized `GET /brokers?ids=` to fetch
   what the first read's join had already touched. Its contact pair is unmasked for every
   signed-in caller (the 2026-10-18 exception above). `GET /brokers?ids=` itself stays, for the agent
   and other non-SPA consumers; and
   the client treats 404 as an answer only when the body carries this module's own
   `broker not found` detail — any other 404 (edge, stale base URL, renamed route) must
   surface as an error, not as "no broker". One per-ad read: `POST /brokers/by-listings`.
   **The static key is gone (2026-10-13)**: `API_TOKEN`, extractable from the shipped SPA bundle via
   devtools, once also satisfied `verify_jwt` as a synthetic `is_admin: True` identity (a live CRITICAL
   finding closed 2026-08-04) and then only `require_token`; today `require_token` delegates to
   `verify_jwt` and nothing reads the variable (`docs/design/api-token-rotation-and-spa-jwt-migration.md`).
   See "Identity, login, and admin gating" for the JWT mechanics.
9. **Trace format on `estimation_runs.trace` is versioned.** `TRACE_SCHEMA_VERSION` lives in
   `api/estimation_runs.py`; every row's `trace.version` matches that constant at write time.
   Shape: `{version, summary, steps: [{n, kind, started_at, duration_ms, output_summary,
   ...}]}`. Step `kind` ∈ `'tool_call' | 'computation' | 'reasoning'`. The reasoning kind is
   emitted per LLM turn by the agent loop. Steps NEVER store full tool outputs — only
   `output_summary`; the full data lives in dedicated columns (`comparables_used` for the
   cohort, etc.). This caps row size at single-digit kilobytes regardless of cohort size.
   Bumping the version is a deliberate change; future readers must handle older versions.
   Full per-step tool outputs that the operator may want to drill into later live in a
   separate side-table `estimation_trace_payloads` (migration 043), keyed on
   `(estimation_run_id, step_n)`. Populated only for `tool_call` steps that opt in via
   `StepHandle.set_full_output(...)`. Reachable through
   `GET /estimations/{id}/trace/{n}/payload`. Same retention discipline as
   `listing_freshness_checks`: rows older than 30 days are safe to delete; no automated
   pruner.
10. **Agent skills live in the `skills` table; the on-disk `skills/<name>/SKILL.md` file is
    the canonical seed.** Each skill is a bundle of (system prompt + allowed tool whitelist +
    per-provider preferred model + loop limits; `preferred_model` names any non-empty subset of the
    registered providers, a run on an unnamed one fails). Migration 029's seed `INSERT` is the importer
    of the markdown file's content; at runtime the DB row is the source of truth. Operators
    edit via `/settings` (UI) or `PUT /admin/skills/{name}` (API). Every update writes a
    `skills_history` row via trigger — same pattern as `app_settings_history` (migration 020).
    When adding a new skill: commit a new `skills/<name>/SKILL.md`, write the corresponding
    seed `INSERT` in a new migration, apply.
11. **LLM provider is pluggable; `llm_calls.provider` records which backend served each call.**
    `api/providers/` defines a `CompletionProvider` Protocol with neutral message / tool /
    completion types; `anthropic`, `gemini`, `openai`, `qwen` (W2-10) and `oss` (`oss:` ids) are wired up
    (default `anthropic`). `provider_for_model` derives the backend from the model id
    prefix, so a lane that only knows its `app_settings` model routes without threading a
    provider argument. Adding a provider is a new file implementing the same Protocol,
    registered in `api/dependencies.py:_build_providers` — AND, separately, in every hand-built provider map (`scraper/main.py`,
    the condition scripts, `scripts/bakeoff_text_extraction.py`, `autodedup/judge_lane.py`, `toolkit/vision_batch.py`,
    `toolkit/description_extraction.py`; the API, the worker and `smoke_agent.py` use `get_providers()`), and a model whose
    provider is unregistered raises in `LLMClient.call` BEFORE the try/except that writes the failure row, so a misroute leaves ZERO `llm_calls` evidence and is
    invisible to the `llm` health check. `LLMClient` is the audit orchestrator — every call
    writes one row to `llm_calls` with provider, model, tokens, USD cost, and a `called_for`
    tag. An unmapped model id records `cost_usd=0` rather than raising — silent, not loud;
    check `api/providers/gemini.py`'s `_PRICES` table after any Gemini model bump.
    **Gemini quirks** (`api/providers/gemini.py`): (1) pricing table needs live maintenance
    across generations — 2.5 closed to new projects, PR #760 moved the default price entries
    to the 3.x generation (`gemini-3.1-pro-preview`, `gemini-3.5-flash`); (2) our
    Anthropic-shaped tool schemas set `additionalProperties: false` and sometimes carry
    `$schema` — Gemini's function-calling API 400s on both, so they're recursively stripped
    before every call (`_GEMINI_UNSUPPORTED_SCHEMA_KEYS`, PR #755) — a new tool schema key
    Gemini rejects needs adding to that frozenset, not a per-call workaround. The
    `CompletionProvider` Protocol also gained `tool_choice` (force-tool-by-name — Anthropic's
    `{"type": "tool", "name": ...}`, Gemini's `FunctionCallingConfig mode=ANY`, PR #768) so a
    caller that needs a guaranteed structured response (no prose fallback) can force it; pass
    it through `LLMClient.call(..., tool_choice=...)` — omitted, providers/fakes without the
    param keep working.
12. **Never spell `price / area`. `toolkit/measures.py` is the only per-m² definition.**
    (Numbered last on purpose — the rules above are cited by code comments; never renumber.)
    `per_m2_sql(alias)` / `per_m2_basis_sql(alias)` render calls to `measure_price_per_m2` /
    `measure_price_per_m2_basis` (migration 425); `alias` is required, so a unit-blind call
    cannot be written. Against the `listings` TABLE the four-argument call is the ONLY legal
    spelling — `listings` has no `price_per_m2` column, so the published-column form fails at
    PREPARE; against `properties_public` / `browse_list`, read the published column, which IS
    the measure. Vocabulary: four values (`sale_capital_czk_m2`, `rent_monthly_czk_m2`,
    `land_capital_czk_m2`, NULL) plus two non-basis COHORT states, `mixed` and `unknown`.
    Floors are on the PRICE (rent < 1000, sale non-land < 100000, land unfloored) and a NULL
    is a visible gap, never a guess. Every envelope carrying a per-m² number carries `basis`
    beside it, in `data` and in `filters_used`; a tool handed caller-supplied rows degrades to
    `'unknown'` rather than defaulting to sale. `require_scalable_basis` gates any
    multiplication of a per-m² percentile by an area (`estimate_yield._scale`), raising
    `MeasureBasisError` → 422, and it is enforced on BOTH paths: `estimate_yield._scale` for
    deterministic runs, `api/agent._require_cohort_scalable_into_rent` for agent runs (the
    model does its own multiplication there, so the gate sits where its number lands). Six
    per-m² statements are invisible to `tests/sql_corpus.discover()`, for TWO different
    reasons: five are assembled by in-function concatenation into a local (comparables,
    velocity, the transit corridor, neighborhoods, the watchdog matcher), and
    `api/portal_lookup._MARKET_SQL` is a module-level constant the sweep DOES discover but
    then skips as a `.format()` template (`_is_format_template` matches its `{values}` slot).
    A `*_SQL` constant is therefore NOT evidence of coverage — one unquoted `{slot}` and the
    sweep walks past. `tests/test_measure_sql_prepare.py` is the gate for all six and must
    gain a line when a seventh appears.

## Error contract and the transport deadline (Broker Unify W2)

`api/main.py`'s global handler answers exactly two shapes, both `{code, message}` details (the
SPA's `detailText` and the extension render `message`): psycopg's busy classes (`QueryCanceled`
57014 — a lock-blocked read dying at the 120 s budget — `LockNotAvailable`, `DeadlockDetected`) →
**503 `db_busy` + `Retry-After`**; everything else → **500 `internal_error` + logged `ref` id**,
never raw exception text. Route-owned refusals (property_split's 409) stay.
`frontend/src/lib/api.ts` is the ONE transport (uploads + blob downloads, no raw `fetch()` beside
it): `REQUEST_DEADLINE_MS` (130 s) sits above the server's 120 s budget and under Railway's ~300 s
close — keep that ordering — a deadline abort (`ApiError.kind === 'timeout'`) is NEVER retried;
react-query retries once only on `isTransientApiError` (network or 502/503/504/520). PostgREST reads
use its twin `lib/pgRead.ts` (20 s, same ApiError, library retry off). The extension duplicates
the deadline by value; its overlay keeps `LOOKUP_TIMEOUT_MS` for MV3 port loss no fetch sees.

## Identity, login, and admin gating (Phase 1, `api/dependencies.py`)

Six auth primitives coexist — four in `api/dependencies.py` over ONE header reader, two in
`api/tenant_pool.py` (`tenant_conn` after this list). `_bearer` (2026-10-13): `API_AUTH_OPTIONAL=1` → anonymous
(local dev only), else `verify_jwt` — a verified JWT's claims or a 401/503. Tests mint a caller with
`tests/api/_jwt.py` `mint` (an autouse fixture serves its key).
- `require_token` — any signed-in caller, nothing returned (rule #8's baseline). Every client sends its
  session JWT to every route, so a route moves between gates without a client change (the deploy-skew
  class behind the 2026-10-13 "Invalid token" preset incident: #1775's stale bundle).
- `account_scope` — `_bearer` plus a READ SCOPE (the tenancy doctrine's FOURTH shape): the local-dev
  opt-out or a JWT with no resolvable account → `[SYSTEM]`, a verified JWT → `[that account, SYSTEM]` — mirroring
  `estimation_runs_tenant_read` (291), never a second tenancy definition. Used by `GET
  /estimations{,/latest-by-listing}`, deliberately SERVICE-ROLE: their LEFT JOIN onto `listings`
  (RLS-on, zero policies) would silently NULL `locality_display` on a tenant conn.
  `account_ids` is a REQUIRED kwarg on those read helpers — omitting it is a `TypeError`, never a silent
  unscoped read.
- `verify_jwt` — verifies a Supabase user JWT and returns its claims over ONE path: asymmetric JWKS
  (`SUPABASE_URL` → `/auth/v1/.well-known/jwks.json`, ES256/RS256, cached via `PyJWKClient`); no shared
  secret anywhere (the HS256 `SUPABASE_JWT_SECRET` fallback was deleted 2026-10-13); `503` with `SUPABASE_URL`
  unset (an unconfigured auth backend must never authenticate anyone). History of the static key it once
  also accepted (the 2026-08-04 god-credential removal, the 2026-10-13 deletion):
  `docs/design/api-token-rotation-and-spa-jwt-migration.md`.
- `require_admin` (`Depends(verify_jwt)`) — gates on `claims["is_admin"]` or
  `claims["app_metadata"]["is_admin"]`; `403` otherwise. Only reachable via a real Supabase JWT
  whose `app_metadata.is_admin` was stamped `true` (the `admins` table is the provisioning
  allowlist, but the live claim is a plain `auth.users.raw_app_meta_data` attribute — Supabase
  includes `app_metadata` in every issued JWT by default, no Custom Access Token Hook needed).
- `require_account_id` — in `api/tenant_pool.py`, NOT `dependencies.py` (tenant_pool imports it, so
  `Depends(tenant_conn)` there is circular). The ONE no-account posture: resolved once at the route edge,
  else `400 "no account for caller"`; `403` under `X-Act-As` (watchdog, presets, `PUT /stats/dashboard`,
  every share-create route never acts; a delegate's collection-monitoring field is a 403). Its twin `require_acting_account_id` (migration 641) is the caller's own
  account without the header, else the owner `acting_account_ids()` grants (`403 "no edit share from that
  account"`): EVERY curation write declares it (`/pipeline/*` writes incl. stages, `POST /collections` `/tags`
  `/properties/{id}/notes` `/attachments` `/dismissals`, `PUT .../artifact-link`), and `tests/api/test_acting_routes_census.py`
  pins that it writes only the eleven curation tables. `tenant_conn` reads `X-Act-As` (400 unless a uuid) and
  sets it + the method as PostgREST's GUCs (an acting GET/HEAD runs READ ONLY, as PostgREST does). Reads take neither dependency, and an UPDATE/DELETE *by id*
  none either (its policy's USING clause is the scope). It replaced three
  postures: notifications' 400, curation's `SYSTEM` fallback (290 has no SYSTEM arm → a 500), pipeline's `None`.

`SYSTEM_ACCOUNT_ID = "00000000-0000-0000-0000-000000000000"` (migration 286) owns a SERVICE-ROLE
write whose caller has no JWT `sub`, ONLY where the table carries a SYSTEM RLS arm
(`estimation_runs`, 291/292) — never a fallback on a tenant conn (290 gave curation none).

For routes that need per-account **data isolation** (not just an admin/non-admin split), use
`api/tenant_pool.py`'s `tenant_conn` instead of the service-role `get_db_conn` — it opens an
RLS-scoped transaction under the `tenant_pool` role; `verify_jwt` is authentication, `tenant_conn`
(via RLS) is authorization, and it has no fallback connection (the static-token bypass was DELETED
2026-09-11, dead since PR #941 — an unset `TENANT_POOL_DB_URL` now raises). **WHICH of the four scoping shapes a
route gets is stated ONCE — the `database` skill's Multi-tenancy section (four-line doctrine) and
its `references/tenancy.md` (full version + the two test rules from the #917 post-mortem). Don't
restate it here or in a module docstring.** Standing gates: `tests/api/test_admin_route_coverage.py`
(every tenant-connection route resolves an account or sits in `_RLS_ONLY_ALLOWLIST` with a reason;
`/brokers/*` + `POST /estimations` are service-role and excluded structurally) and
`tests/api/test_account_scope_census.py` (no nullable tenant scope anywhere in `api/`).

**Billing skeleton** (`api/routes/billing.py`, migration 298, PR #769 — Phase 1 increment 5) adds a
**fourth** auth class: `POST /billing/webhook` verifies the `Stripe-Signature` HMAC over the raw
body with the stdlib (no SDK), rejects payloads outside a 300s replay window, and fails closed with
no `STRIPE_WEBHOOK_SECRET` — never `require_token`/ `verify_jwt`. One DB transaction covers the
`stripe_webhook_events` idempotency INSERT (`ON CONFLICT DO NOTHING` on the event id — atomic,
never check-then-act) AND the handler, so a mid-handler crash lets Stripe's retry reprocess safely.
`checkout.session.completed` anchors the customer id to an account (never re-points a bound one);
`customer.subscription.*` upserts plan/status/period guarded by `last_event_created` (delivery
order isn't guaranteed). `GET /billing/me` rides `tenant_conn` (RLS). `require_entitlement(agenda)`
is a dependency **factory** — `Depends(require_entitlement( "watchdogs"))` 403s unless the plan
carries that agenda; bypass is `is_admin` alone. Wired to no *router* yet — the first real
enforcement is inline in `create_estimation_run` (below).

**Agent-estimation metering** (Wave 1, migration 355) is the first metered path. The paid
`mode:'agent'` submit is gated **inside `api/estimation_runs.py:create_estimation_run`**, at the
single choke point after the subject is resolved (an unknown or unlocated ad is a 422 before any gate)
and before the INSERT (`_prepare_metered_submit`), so a rejected submit spends zero LLM cost. Meter = **per successful agent run, monthly** (operator decision, not USD): free
plan `plans.agent_estimations_monthly_quota` = 3, `trial_*` = 10 (while
`entitlements.status='trialing'` + unexpired: a signup's first 7 days). Only a real, non-admin tenant sending `mode:'agent'`
is metered — admin/SYSTEM and all deterministic runs bypass, mirroring `require_entitlement`.
(ClickUp is named in comments as a `claims is None` bypass — an internal direct-Python path only;
it has zero historical rows, verified 2026-08-04, any HTTP call 401s, and a real credential waits
until the integration activates.) Enforcement is **atomic** (A9 — never check-then-act over the tx
pooler): `INSERT … SELECT WHERE (monthly non-failed count) < quota AND (in-flight count) < cap ON
CONFLICT (account_id, idempotency_key) DO NOTHING` — budget + per-account concurrency + idempotency
in one write, arbiter index `estimation_runs_inflight_idem`. The budget counts `estimation_runs`
directly; `usage_ledger` is the append-only billing/margin record (one row per metered success,
cost = the run's `llm_calls` sum), written at the agent terminal, RLS-scoped like `entitlements`.
Flags (app_settings, service-role read): `estimation_budget_enabled` (absent ⇒ **enforced**, fail-
closed), `agent_estimation_concurrency_cap` (3). Deferred: extension `mode:'agent'`.

## Auth and secrets

All secrets are GitHub Actions secrets and/or Railway env vars in production. Backend code
references them by name; never write a value into a committed file (`.env` is gitignored).
API keys are **backend-only** — never `VITE_*`-prefix one; `frontend/` must not see them.

Database:
- `SUPABASE_DB_URL` — Postgres connection string (Supabase → Database → Connection string →
  Transaction pooler, port 6543; password embedded). **The one the scraper / API / scripts
  actually use.** Required.
- `SUPABASE_DB_SESSION_URL` — Session-mode pooler connection string (Supabase → Database →
  Connection string → Session pooler, port 5432; same host/user as `SUPABASE_DB_URL`, just
  port 5432 not 6543). **Optional**; used only by the scraper's hot detail-write loop
  (`connect_session()`, i.e. the Phase-2 detail-drain's batched writes) so its repeated SQL
  gets prepared statements. Unset → falls back to `SUPABASE_DB_URL`. Set it as an Actions
  secret on `detail_drain.yml` (and the Railway env var only if the API ever calls
  `connect_session()`).
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` — set as Actions secrets for forward
  compatibility; the v1 scraper connects to Postgres directly and does not need them.
  (`SUPABASE_SERVICE_ROLE_KEY` is the 2025 `sb_secret_...` token, **not** a JWT.) **On the
  Railway API service, `SUPABASE_URL` is now load-bearing**, not just forward-compatible:
  `verify_jwt` builds the JWKS URL from it to verify Supabase user JWTs (its only verifier;
  `SUPABASE_JWT_SECRET` is read by nothing since 2026-10-13 and can be deleted from Railway).
- `TENANT_POOL_DB_URL` (Railway API only) — connection string for the `tenant_pool` role
  (migration 293), used by `api/tenant_pool.py`'s `tenant_conn` for RLS-scoped per-account
  writes. Distinct from `SUPABASE_DB_URL` (service-role, unscoped, bypasses RLS).
  **The username MUST carry the project-ref suffix — `tenant_pool.<project-ref>`, not bare
  `tenant_pool`** — because this points at Supabase's *shared* pooler host
  (`aws-0-<region>.pooler.supabase.com:6543`), which routes by that suffix; without it the
  pooler rejects the connection with `FATAL: (ENOIDENTIFIER) no tenant identifier provided`.
  A direct-to-database DSN needs no suffix, which is why `SUPABASE_DB_URL` looks different.
  This was mis-set from migration 293 until 2026-07-21 and stayed invisible the whole time:
  `tenant_conn`'s legacy branch routed static-`API_TOKEN` callers to the service-role
  connection, so until the Chrome extension's own JWT arrived, **no production request had
  ever executed the tenant-pool path**. (That branch was DELETED 2026-09-11; `tenant_conn`
  now raises on an unset DSN rather than absorbing it — the lesson stands.) When moving any
  further route onto `tenant_conn`, exercise it with a real user JWT — a green RLS test
  lane proves nothing about a DSN.

Image storage (Cloudflare R2, S3-compatible):
- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` (usually
  `sreality-images`).
- **TWO runtimes need these, set them on BOTH:** (1) the **scraper** (GitHub Actions secrets)
  to *download* image bytes — optional there, a missing var just logs a skip and exits zero;
  (2) the **FastAPI service** (Railway env vars) to *serve* them, since `GET /images/{key}`
  presigns R2 (the frontend's image path since PR #255) and `GET /properties/{id}/download.zip`
  (JWT) zips them with a PDF of the ad and, under `moje-fotky/`, the acting identity's own photo
  originals (read on its tenant connection) — the bucket sends no CORS header. If the **API** lacks them,
  every listing photo 503s and the UI looks imageless though the DB reports the bytes "stored"
  — the API logs a boot WARNING and `GET /health` reports `image_storage: "unconfigured"`.
- `IMAGE_PRESIGN_ANCHOR_SECONDS` (optional, API service, default `86400`) — the width of the bucket
  `GET /images/{key}` pins its SigV4 signing time to, so a key presigns to a byte-identical URL all
  day (the browser caches on the whole URL; a fresh signature per hourly re-mint re-downloaded every
  photo despite R2's 30-day `max-age`). Read per request: `0` = per-request signing (the kill switch
  if R2 rejects a backdated signature). Capped at half the 7-day presign TTL, or the last URL minted
  in a bucket would already be expired.
- **Phone copy** (#1765): `GET /images/{key}?v=2&w=1080` (any other `w` → 404) redirects to
  `thumb/w1080/{id}/{seq}.webp` (WebP q75), made from the master on first request (≤ 4 at once);
  any failure → a 5-min redirect to the master. SPA: `imageSrc(img, { w: 1080 })`. An attachment's copy
  (`GET /properties/{id}/attachments/{aid}?w=1080`, `api/attachments.py`) is made the same way under
  `thumb/w1080/custom-attachments/…` but streamed through that JWT route, never presigned.

LLM + maps (FastAPI service + scoring jobs):
- `ANTHROPIC_API_KEY` — required for the estimation agent, summarize/vision tools, condition
  scoring, and the agent under `provider='anthropic'`.
- `GEMINI_API_KEY` — Google AI Studio key; required for the agent under `provider='gemini'`.
  A request selecting an unconfigured provider returns 502; missing at boot is not fatal.
- `OPENAI_API_KEY` — any `gpt-*` / `o*` model id (`provider='openai'`): the text lane (attributes
  + a raw location reading, location reader W1); no claim lane runs a model.
- `QWEN_API_KEY` — Alibaba DashScope, INTERNATIONAL (Singapore) endpoint; any `qwen*` model id.
  Read lazily by a lane pointed at qwen: an Actions secret there, Railway only to call it here.
- `OSS_LLM_BASE_URL` / `OSS_LLM_API_KEY` — a self-hosted vLLM pod (`oss:<hf id>` ids, `provider='oss'`):
  an EPHEMERAL RunPod proxy root the autodedup judge lane exports per pass, re-read per call and never stored; the key defaults to `none` (vLLM serves unauthenticated).
- `MAPY_GEOCODE_ENABLED` — **the Mapy kill switch, default OFF.** Mapy.com's terms prohibit
  storing/caching API results, so `scraper.geocoding.geocode()` raises unless this is
  explicitly `1`/`true`/`yes`. No caller stores a result any more: what is left is
  `scripts/seed_curated_cities.py`. Display-only Mapy use
  (`/maps/suggest`, tiles, the phone's static map) is NOT gated — the prohibition is on persistence, not display.
  Do not enable without an operator decision recorded against the Mapy remediation plan.
- `MAPY_CZ_API_KEY` — Mapy.cz REST key; geocodes locality strings, powers `/maps/*` (not `/maps/sreality-price-map`: key-free sreality suggest proxy, no CORS upstream) and the static map.
- `MAPY2_CZ_API_KEY` (optional backup) — a second Mapy.cz key. Geocoding, `/maps/suggest` and the static map fail over
  to it in ONE loop (`scraper.geocoding.with_mapy_key_failover`) **only** on a rejected (401/403) or rate-limited (429)
  primary, never on a 5xx. GitHub Actions secret (`seed_curated_cities.yml`) + the **Railway API env var**. Unset → no-op.
- `MAPY_STATIC_MAPS_ENABLED` (Railway API, default on; `0` → 404 `disabled`, read per request) — the phone's Poloha:
  `GET /properties/{id}/static-map.png?w=1080` streams Mapy's static map (540×405 @2x, **4 credits** a fetch, one
  `STATIC_MAP` line each to count them) on EVERY view, `private, max-age=3600`; Mapy's terms forbid storing it — never R2/disk.
- `VALUO_SANDBOX_API_TOKEN` / `VALUO_API_TOKEN` (Railway API only, never local, never `VITE_*`) — Valuo's paid API v3 at `https://api.valuo.com/v1`
  (`api/valuo_client.py`: `market-value/{type}` + one `advert/detail` for the comparables' photos); production wins, neither = `GET /properties/{id}/valuo` says
  "Valuo zatím není nastaveno." and the POST 503s. A row stamps `token_sha` + the environment that ANSWERED (`ver` `-sandbox` = sandbox); tests delete both (`tests/conftest.py`).
- `LLM_DAILY_COST_WARN_USD` (optional, default `5.0`) — soft cross-provider warning
  threshold; `LLMClient` logs one WARNING per day when the `llm_calls.cost_usd` sum first
  crosses it. Each provider's own console spend cap is the hard guard.

API service:
- `API_AUTH_OPTIONAL=1` — LOCAL DEV ONLY: opens every `require_token` route to anonymous callers. Never on Railway (rule #8).
- `CORS_ALLOW_ORIGINS` — CSV of allowed origins; must include the Chrome extension's
  `chrome-extension://<id>` origin and the SPA origin.
- `STUCK_ROW_SWEEP_DISABLED`, `HEALTH_TELLER_DISABLED` (optional flags) — disable the startup sweep
  of stuck estimation/building runs, and the system-alerts loop (the health teller, the API's only
  background loop), respectively. Default: both enabled. `HEALTH_TELLER_DISABLED` replaced
  `NOTIFICATIONS_MATCHER_DISABLED` in W2b, with no alias (the old name, if still set anywhere, does
  nothing); the watchdog producers run on the realtime worker's `notifications` lane alone.

Notification delivery (rule #16; `docs/design/notifications-push/PROGRAM.md` § 3.4): iPhone push only. W1 (2026-10-21) deleted
the Resend email + Telegram transports, the outbox, `ChannelClient`, `POST /webhooks/resend` and `/u/{token}`, so `RESEND_API_KEY`,
`EMAIL_FROM`, `RESEND_WEBHOOK_SECRET`, `NOTIFICATION_UNSUB_SECRET`, `API_PUBLIC_URL`, `TELEGRAM_BOT_TOKEN`, `SPA_BASE_URL` and
`OUTBOX_DRAIN_DISABLED` are read by nothing (safe to remove from Railway). Push links absolutely from each device's own origin.
- `VAPID_PRIVATE_KEY` (W4) — the P-256 scalar, 32 bytes base64url (no PEM), ONE Railway Shared Variable attached to BOTH the API
  (`GET /push/key` derives the public key; unset/unreadable → 503) and the realtime-worker (the `push` lane signs with it; unset =
  the lane's brake, `unconfigured`). Never `VITE_`; never log it; rotating it ends every subscription (`VapidPkHashMismatch` →
  gone), so rotate only on a leak. Generate it with no output, into a file only you can read, then paste it into Railway and delete
  the file: `python3 -c "import os,base64;from cryptography.hazmat.primitives.asymmetric import ec;os.umask(0o077);k=ec.generate_private_key(ec.SECP256R1()).private_numbers().private_value.to_bytes(32,'big');open(os.path.expanduser('~/vapid_private_key.txt'),'x').write(base64.urlsafe_b64encode(k).rstrip(b'=').decode())"`.
  Transport `api/push.py` (RFC 8291 aes128gcm + RFC 8292 VAPID ES256 on `cryptography` + PyJWT, `*.push.apple.com` only, no
  redirects, a transport error = its class name); devices `api/push_devices.py`; the ledger pass `api/push_delivery.py`.
- The system-alerts loop (`api/system_alerts_loop.py`, the API's only background loop, off under `HEALTH_TELLER_DISABLED`) runs the health ladder `toolkit.system_alerts.reconcile` every 60 s (a constant, no setting): it keeps the `ops_incidents` ladder (onset, reminders by rung, recovery, acknowledgement on Health) and writes no `notification_dispatches` row (ruling R2b, 2026-10-21, superseding Q1's "bell only"); the external alarm is the health canary's GitHub e-mail (scraper-ops skill).
- `STRIPE_WEBHOOK_SECRET` — Stripe webhook signing secret (Dashboard → Developers →
  Webhooks). Railway API env. Unset = `POST /billing/webhook` 503s (fail closed); the
  handler verifies the `Stripe-Signature` HMAC with the stdlib (no stripe SDK).

Scraper orchestration:
- `SREALITY_COUNTRY_ID` (optional, default `112` = Czech Republic).
- `SCRAPE_CHAIN_TOKEN` (optional fine-grained PAT: this repo, Actions read+write) — lets the
  scrape workflow re-dispatch itself for tighter-than-cron cadence; no-op without it.
- `GITHUB_ACTOR` — CI context, used for curated-cities upload attribution.

NEW DEDUP (docs/design/new-dedup/PROGRAM.md, Wave 1):
- `RUNPOD_API_KEY` — Actions secret only (no Railway/frontend use). Auths `scripts/runpod_client.py`'s REST
  (`rest.runpod.io/v1`) + GraphQL (`api.runpod.io/graphql`) calls to launch/poll/terminate on-demand GPU pods.
  Two `workflow_dispatch` lanes: `new_dedup_runpod_smoke_test.yml` (infra proof) and `dinov3_embed_backfill.yml`
  (the DINOv3 corpus embedding pass, never yet run on real data). Every job goes through `RunPodClient.run_job`,
  which terminates the pod in a `finally` however the job ends — the actual cost-safety guarantee (the pod API has no "run
  once and stop" flag). Credentials reach a pod ONLY via the optional `env: dict[str, str]` on
  `launch_pod`/`run_job`/`run_job_with_fallback` — the REST body's `env` **object** (`{"K": "v"}`, not GraphQL's
  `[{key, value}]` list; omitted when empty), never `start_cmd`, which is argv in the pod record. Never logged.
- `HF_TOKEN` — Actions secret only. HuggingFace token for **gated** weights (`facebook/dinov3-*` is `gated: manual`; even `config.json` 401s), read by `scraper/dinov3_tagger.py` and forwarded into the pod.

Frontend / extension (build-time only, inlined into the browser bundle — *not* backend
runtime): `VITE_SUPABASE_URL` + `VITE_SUPABASE_ANON_KEY` (the publishable anon key, safe in
the browser) and `VITE_API_BASE_URL`. Neither bundle carries an API bearer: the SPA dropped
`VITE_API_TOKEN` on 2026-10-13 (every request sends the session JWT; `api/dependencies.py`
`_bearer`), the **extension** (`EXT_API_BASE_URL` / `EXT_SUPABASE_URL` / `EXT_SUPABASE_ANON_KEY`
repo secrets → the same `VITE_*` names at build) dropped `EXT_API_TOKEN` on 2026-07-21 (Wave 1)
and runs its own Supabase session via a hand-rolled PKCE flow (`chrome-extension/src/auth.ts`).
There is no static API key at all since 2026-10-13: a machine that needs the API signs in as its own
Supabase user and sends that JWT, exactly as the smoke-check account does.

