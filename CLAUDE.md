# CLAUDE.md

This file holds the hard rules only; the WHY (full rationale, edge cases, incident history)
lives in `docs/architecture.md`, and operational how-tos live in on-demand skills under
`.claude/skills/`. Read the relevant one before changing code it governs. When a rule here
keeps getting broken, fix it here — don't repeat the correction by hand.
## What this project is

A **market-wide real-estate intelligence platform** for the Czech market. It began as an
hourly sreality.cz scraper and now collects, enriches, and reasons over property data from
**nine portals**. Store of record: Postgres (Supabase, `eu-west-1` Ireland, PostGIS) with full
listing history.

Data layered together:
- **Scraped listings** from nine portals — sreality (JSON v1 API), bazos (HTML crawler), bezrealitky (GraphQL
  API), mmreality (Vue-embedded JSON, proxied), idnes / remax / ceskereality / maxima (structured HTML) and
  realitymix (structured HTML, Centrum.cz aggregator) — landing in one `listings`/`listing_snapshots` contract
  with one canonical vocabulary. Per-portal ingest detail: `docs/architecture.md` § Data sources.
- **Geo data** — coordinates, districts, ČÚZK/RÚIAN admin boundaries, transit geometry, OSM amenities.
- **Operator-supplied** — curated city-quality indexes, collections, building decompositions, estimation inputs.
- **Derived** — condition scores, velocity, statistics, LLM summaries / comparisons / value estimates.

Two goals: (1) **robust, polite scraping** that preserves history (latest-wins current state
+ append-only snapshots; nothing is ever deleted); (2) **let the operator and AI agents work
the data many ways** — filter through a large filter set or on a map, layer map views, see
region/property/type statistics, estimate sale & rental value, browse, run watchdog alerts on
any saved filter, and more (ROADMAP.md is the sequencing source of truth).

Surfaces: an analytical **toolkit + FastAPI service** (Railway), a **React SPA** (Railway,
reads public data directly and routes every write through the API), and a **Chrome extension**
that overlays estimates on portal pages.

A **dark-by-default always-on worker** (`scraper/realtime_worker.py`, a 2nd Railway service from the SAME image, gated by
`REALTIME_WORKER_ENABLED`) runs every lane registered in `_amain`: the latency layer over the GH crons (newest-first probes,
bounded detail drain, images-first downloads, sreality count-probe), plus property/broker maintenance, estimation jobs, location
resolve/intake/refetch, sold comps, text extraction, autodedup and heartbeats (design: `docs/design/realtime-scrapers.md`).
## Territories

Three top-level territories with deliberately different rules — identify which one a task is
in before starting. Deep per-territory rationale: `docs/architecture.md` § Territories.

- **Backend** (`scraper/`, `toolkit/`, `api/`, `migrations/`, `tests/`, `.github/workflows/`)
  — Python 3.12, stdlib-first, `psycopg` direct to Postgres, service-role (reads + writes
  anything). Runs in GitHub Actions + Railway. All architectural rules below apply.
- **Frontend** (`frontend/`) — Vite + React 18 + TypeScript + Tailwind v4 SPA on Railway. **No backend secret in browser code.** It holds the
  anon key, the user's Supabase JWT and `VITE_API_TOKEN`, a static bearer readable from the public bundle: it may gate only "loaded the SPA",
  never identity, admin or per-account data (those take `jwt: true`; routes still breaking this: `roadmap/public-release-track.md` item 11).
  Reads run as `authenticated` (`anon` gets nothing): `*_public` views, RLS'd tables, read models granted with no RLS (`browse_list`,
  `properties_map_mv`, `location_pin_audit_mv`), INVOKER RPCs, DEFINER RPCs only if admin-gated or `ci-allow-ungated`. Writes go via the API by
  convention, not grant (`database` skill). Design tokens (`globals.css` `@theme`) need operator OK. Backend rules don't apply.
- **Chrome extension** (`chrome-extension/`) — Manifest v3, **vanilla TS only** (no React /
  Tailwind), closed shadow-root panel. Every network call goes through the background worker
  (`chrome.runtime.sendMessage`), never a direct `fetch`. Build-time `VITE_API_*` inlined
  (Path 1; ship `dist/` to trusted operators only). Backend + SPA rules don't apply here.

When in doubt which territory a task is in, ask. Don't import frontend deps into the Python tree or vice versa.
## Working with the operator

The owner works locally in **VS Code on WSL2 Ubuntu** with a full terminal, local Git/Python,
and authenticated `gh` — so suggest and run local commands (tests, git, `gh`, debugging). The
operator is **non-technical by training but learns fast** — explain the *why*, define jargon on first
use ("upsert", "JWT", "RLS", "draft PR"), and give click-by-click steps for browser tasks (Supabase
SQL editor, GitHub settings pages).
## Git workflow and pull requests

Short-lived branches, merge via PR. **Never push directly to `main`** — Railway auto-deploys
from `main`, so a merged PR *is* the deploy; PR + branch protection + CI is the production gate.
- **Branch naming:** `feature/<name>`, `fix/<name>`, `cleanup/<name>` or `roadmap/<name>` (docs/hygiene).
- **Start:** `git checkout main && git pull && git checkout -b <branch>`.
- **One PR = one purpose.** Don't mix a feature with an unrelated docs/ROADMAP rewrite (a
  *large* ROADMAP restructure is its own PR; small phase-entry bookkeeping rides with the work).
- **End** by pushing the branch + opening a PR; return the URL.
- **If a PR changes behavior that a skill or `docs/architecture.md` documents, update that
  document in the same PR.** A stale skill is worse than a missing one — sessions trust it
  and load it by default. CI warns (non-blocking) when this is skipped for a mapped path.
## Autonomy and the safety net

Default to **full autopilot**: create the branch, push early, open a **draft PR** so the
operator can watch, and work to completion.
- **Stop and surface** — don't paper over — a merge conflict, a failing test, or genuine ambiguity.
- The safety net: CI (`.github/workflows/test.yml`) runs on every push + branch protection guards
  `main`, so broken code can't reach production. Lean on it; keep tests green.
- **Database changes** have their own gate (the `database` skill: additive migrations are
  autonomous; destructive ones pause for confirmation + a backup).
- **A merge is not a deploy.** After a PR merges to `main`, confirm Railway's rollout with
  `gh api repos/{owner}/{repo}/commits/<sha>/status` — Railway posts a per-service commit
  status (API / vite / realtime-worker), each with `state` + a live URL, no Railway token
  needed. For frontend-visible changes, follow with a real-browser check against the
  production URL — read-only only; never drive a mutating action (merge/unmerge, delete,
  send, …) against production autonomously.
## Fetching live state (fetch, don't ask)

Dynamic state lives outside Git — don't ask, fetch it:
- Migrations on disk → `ls migrations/ | tail -5`; Actions runs → `gh run list --limit 10`.
- **DB reads (counts, freshness, schema, verification SELECTs) → `psql "$SUPABASE_DB_URL" -c "…" | head`**,
  NOT the Supabase MCP (its verbose output persists in context) — but when `psql` or that env
  var is absent (cloud-only sessions) the fallback IS MCP `execute_sql`, one aggregate row per
  question. Both recipes + the reserved-for-migrations MCP policy: the `database` skill.
## Roadmap maintenance

`ROADMAP.md` is a **<120-line index**; phase content lives in `roadmap/<track>.md` and completed
work in `roadmap/archive.md`. **Read the index only; open a track file only to edit it.** After
shipping meaningful work, in the SAME PR update **only** the relevant `roadmap/<track>.md` (move a
bullet to done, add new "next" items) + the index's status cell if the track's status changed.
## Context discipline

- Prefer `grep` / targeted line-range reads over whole-file reads for files >500 lines (this file,
  most `toolkit/` / `api/` / `scraper/` modules, any `roadmap/` track).
- Summarize tool output instead of quoting it back; delegate verbose searches to subagents so their
  output stays out of the main context.
## Architectural rules (do not violate without asking)

**Numbers are cited by code/tests/design-docs — never renumber.** Full rationale, edge cases and incident
history: `docs/architecture.md` § Architectural rules — read it BEFORE modifying anything they touch.

1. **Migrations are append-only.** Never edit an existing numbered file; schema changes go in a new
   `NNN_*.sql`, applied via the Supabase MCP. Additive = autonomous; destructive = pause for OK +
   pg_dump. Prune dead schema with a new forward migration, not by editing history. (see `database` skill)
2. **Snapshots on content change only.** A fetched payload reaches `listings` ONLY via `scraper/listing_write.py`
   `write_listings`, which appends a `listing_snapshots` row iff its content hash differs from the latest (`scraped_at
   DESC, id DESC`). Payload-free writes never snapshot (derived/enrichment/link/lifecycle columns; `scripts/reparse.py` heals of
   a stored page); each outside `scraper/db.py`'s lifecycle writers is ledgered in `tests/scraper/test_listing_write_census.py`.
3. **Never delete; delist via `is_active=false`.** History is sacred. **Since 2026-09-07 index absence NOMINATES,
   the page DECIDES** (`portal_runner._queue_presence_checks`); **since 2026-09-08 the gate is STRUCTURAL, not
   numeric** — a walk that REACHED THE PORTAL'S END (`scraper.portal.walk_reached_end`: every unit walked, each page
   loop out on a portal terminator, no stop of OURS — deadline / page cap / `--limit` / slice subset / error /
   uncorroborated empty page) queues every active row it did not see into `listing_detail_queue` at
   `QUEUE_PRIORITY_VERIFY` (last, 20% reserve); the drain fetches the page and a POSITIVE gone signal (404/410,
   redirect off the listing, the portal's "no longer active" text → `ListingGoneError`) flips that one listing, a
   live page refreshes it, an error waits. `walk_coverage` is a logged `COVERAGE` alarm, NEVER a gate. No staleness
   rail, no absence sweep, no `supports_complete_walk` gate. `delist_flip_cap` (10%, floor 2k) THROTTLES nominations
   per walk (rest deferred; `.overrides` lift it). Budget + gone-rate breaker: `scraper/delist_policy.py`; every
   flip goes through `db.mark_listing_inactive` (guarded, stamps `inactive_at` once, dirties the property).
4. **`last_seen_at` is driven by index sightings + successful detail fetches only; failed fetches never
   touch it** (else repeated failures would falsely delist a live listing). The `unchanged` freshness
   path also doesn't bump it — its signal is `listing_freshness_checks.checked_at`.
5. **Failed detail fetches are tracked, not dropped** — every portal's `listing_detail_queue` row counts `attempts`
   (`given_up` after 5); sreality also keeps `listing_fetch_failures(sreality_id, …)` (re-enqueued at
   `QUEUE_PRIORITY_FAILURE`), deleted inside the successful write's transaction (`listing_write`).
6. **Images download to Cloudflare R2** (bytes, not just URLs). `images` tracks per-image state
   (`storage_path`, `download_attempts`); a separate phase after the scrape, no-op without R2 env vars.
7. **No new dependencies without justification.** Prefer the stdlib; each `pyproject.toml` entry needs a reason.
8. **Latest-wins + snapshot history.** `listings` is current state; every content-hash change (rule #2) appends a
   `listing_snapshots` row. Estimates capture the `snapshot_id` of each comparable (retrospective audit)
   — don't build as-of semantics into live queries.
9. **`listing_freshness_checks` is append-only + ephemeral** (rows >30d safe to delete; no auto-prune).
   It's observability + throttling, not history — the history table is `listing_snapshots`.
10. **`amenities` + `amenity_fetches` are an OSM mirror, not history** — written by `find_anchor_amenities`
    on cache miss; a POI's category is set by the *query*, not OSM tags; taxonomy in `toolkit/amenities.CATEGORY_TAGS`.
11. **`transit_lines` + `transit_line_fetches` are a parallel OSM mirror** for route geometry (migration
    028) — written by `find_comparables_along_axis`; one row per (relation, member way); tram/subway/bus; 30-day TTL.
12. **`estimation_runs` is the single source of truth for every estimation; every run must be created through `create_estimation_run`** (owed forks:
    the Watchdog kickoff, `scripts/smoke_agent.py`). The row is INSERTed at submit (`pending`/`running`; `failed` if setup fails), then the executor
    UPDATEs its terminal `status` in place; failed runs still persist a row (HTTP 200 + `status='failed'`); re-runs INSERT with `parent_run_id`; a
    run's RESULT is immutable (its yield `scenario` and late-bound `input_listing_id` are not). Sources: `ui`/`api`/`clickup`/`extension`.
13. **`building_runs` is the paste-a-building parent.** Children are `estimation_runs` linked via
    `building_run_id` + `building_unit_id`; the unit list is operator-curated JSONB. Status:
    `pending → extracting → awaiting_input → estimating → success|failed`; `awaiting_input` is the
    human-in-the-loop gate; `units_proposal` (agent) vs `units` (confirmed) are kept separate.
14. **Condition scoring is two-axis (building + apartment).** Raw `listings.condition` stays the source;
    the two derived `listings.{building,apartment}_condition_level` (1..5, NULL unscored) + the
    `listing_condition_scores` cache (keyed `(sreality_id, snapshot_id)`) are written together by
    `score_listing_condition` in one latest-wins transaction. Filter on the derived columns, not the
    coarse `condition_assessment`.
15. **Multi-portal listings sit behind a thin `properties` parent (migration 091); grouping is out-of-band, never inline at insert (new rows land
    `property_id` NULL; straggler-attach births a singleton).** Every merge, operator or engine, goes through the **link mechanics**: `toolkit/property_identity.py` is the single merge chokepoint,
    two public writers (`merge_property_set` → a private `_merge_pair` per retired property; `detach_listings`, set-shaped) — it re-points `listings.property_id`,
    soft-retires the loser, logs `property_merge_events` (read by `detach_listings`: each advert back to its origin or a refusal, e.g. `moved_since`; a split is ONE
    call, and so is a group undo (`merge_group_id=`) — no replay), carries every property-anchored operator-state row through ONE ordered list,
    `PROPERTY_CARRIERS` (rule #18), brings every touched property current once per call (`properties_changed`, the dirty drain's after-step), and enforces **category compatibility** (`CategoryClash`:
    sale≠rent, flat≠house — except the sanctioned cross-types, any two of **dům, komerční, pozemek**). `db.presence_candidates` / `active_count` are source-scoped. **Merges are ordered by the
    operator, or by the AUTODEDUP engine (source `autodedup`, through `merge_property_set`, only inside `app_settings.autodedup_apply_scope`, never a split):
    the worker's autodedup lane reconciles its `rt` groups (`autodedup/reconcile.py`); batch `mode=apply`/`unapply` stay until C2 (undo after C2: an open
    operator decision, `roadmap/autodedup.md`); until then the brake is interval 0, then `mode=unapply` — its only detach besides `retire_legacy=1` (until W8:
    undoes the removed engine's `source='auto'` merges in the scope's area, the only code that selects that output by source).** **The old automatic decision
    engine was REMOVED wholesale (2026-08 "NEW DEDUP" cutoff)** — nothing else auto-merges; signal producers (pHash, CLIP, `/labeling`) stay live; the rebuild
    is **simulation-first** (`docs/design/new-dedup/PROGRAM.md` + `CUTOFF.md`). **Never resurrect or consult the removed engine's code or design docs**; the
    operator owns the apply scope (the one rollout control) and every no-merge ruling. Full detail: `docs/architecture.md` § rule 15.
16. **Watchdog + Browse share one definition of "matches"**, rendered per relation: the Watchdog + every cohort compile
    the registry in `toolkit/filter_compiler.compile_filter_where`; Browse's TS + RPCs are pinned per shared predicate only
    (`sql_kind`, place plan, rule-23 measures, served predicate). `notification_dispatches` = the append-only event table,
    **three producers**: `watchdog` + `collection_monitor` (property-grain; `dedupe_key` `:new:` once-ever / `:price_drop:{snapshot_id}`
    per-snapshot; a `monitor_since` anchor so a pre-membership change never fires) + `system_health` (**NOT** property-grain;
    `ops_incidents`, mig 462). **Delivery is separate from detection**: in-app = the row; external = `channel_sends`. A merge re-points them (#18), collapsing a twin (the merge's one delete) once its `channel_sends` move to the kept row.
17. **City-quality indexes are a normalized, operator-curated time series** (`curated_cities` + `city_index_*`
    + `city_population`) — a new index needs no migration; latest revision wins; agenda-gated to **Browse +
    Watchdog only** (the estimation agent never sees them, preserving deterministic estimates).
18. **Operator curation is PROPERTY-grain and dedup-stable** (`collections`, `tags`, `property_notes`, all keyed on `property_id`; migration 202). Every
    property-anchored operator-state row follows a merge through ONE ordered list, `PROPERTY_CARRIERS` (`toolkit/property_carriers.py`: asset link, curation
    tables incl. `notification_dispatches`, pipeline (rule #22), **dismissals** (mig 536: lift, never delete; a LIVE deal wins)), run inside the merge
    transaction, so no such row orphans onto `merged_away`; every other column naming a property sits in `NOT_CARRIED` with its reason, and a census (offline
    over migrations, live over the replayed schema) fails on a column in neither. A SET/APPEND table = one `CurationTable(...)` line; any other shape = one
    adapter. A detach that reactivates a property gives back its pipeline card and asset link; curation, dispatches and dismissals stay on the property left.
    Collections carry monitoring (`monitoring_enabled` + `notify_channels`). Writes go through the API.
19. **The scrape is cadence-split: a fast index-walk feeds an async batched detail-drain via `listing_detail_queue`** (migration 105).
    Index-walk (`--index-only`) walks the full index, `portal_runner.reconcile_sightings` (touch + enqueue) + end-gated nomination (rule #3); detail-drain
    (`--drain-only`) claims a bounded slice (`FOR UPDATE SKIP LOCKED`) and writes each flush in ONE `listing_write.write_listings` call.
    New rows land `property_id` NULL on every path (straggler-attach births the singleton, rule #15). Every portal runs this same split
    through the shared `portal_runner` on the source-generic queue.
20. **Property maintenance is dirty-set incremental, not full-table.** Every child-changing write (content change, revival,
    delist, column heal) enqueues `property_id` into `dirty_properties` (migration 106) in its own transaction (exceptions: the census ledger, and a crawler change confined to unhashed columns, which waits for the daily sweep — architecture § rule 20); `property_maintenance.yml` (`--incremental`, `*/5`)
    attaches new singletons + recomputes only queued properties (O(changes)); the daily full sweep (04:15) is
    the reconcile backstop. Both share the `sreality-property-maintenance` concurrency group. Merge/detach/split recompute inline through the drain's own after-step (`properties_changed`: rollup, Browse row, broker queue) and never enqueue `dirty_properties`.
21. **Every portal runs through ONE shared framework (Phase 4: `portal_base` / `portal` / `portal_runner`, one
    source-generic `listing_detail_queue`); per-portal code is a client (fetch + pacing) + a parser + a `Portal`
    adapter + a config row (`PortalConfig`/`PortalLimits` = its politeness); shared code grows NO new portal-name
    branch.** Its `walk_category` pages + judges its end, then hands what it saw to ONE `portal_runner.reconcile_sightings`
    (touch every sighted known row, enqueue new + repriced; CI rail). The parser emits `source_url` (stored, never rebuilt;
    sreality: `scraper/sreality_url.py`). An unproven walk nominates nothing; its listings close only via gone fetches (rule #3);
    `supports_complete_walk` is posture. A per-portal need is a `Portal` seam, never an `if source ==`; seams + owed: `docs/architecture.md` § rule 21.
22. **The deal pipeline is single-valued, property-grain operator state** (migration 205): `property_pipeline` holds ≤1 card per property at one
    `pipeline_stages` stage (a TABLE, not an enum); "bookmark" == presence of a row at the entry stage. It has its OWN carrier in `PROPERTY_CARRIERS` (over
    `toolkit/pipeline_identity.py`; TERMINAL-AWARE — a live stage always beats a closed one) + a lossless restore when a detach reactivates the merged property.
    Writes go through the JWT-gated API (`tenant_conn`); every SPA card write (Browse cards/rows, listing header, kanban drag + trash) is ONE hook,
    `lib/usePipelineCard` (id per call), over ONE write policy, `lib/useOptimisticWrite` (`lib/pipelineCache`: patches + re-read list). `<PipelineMark>` +
    `<PipelineStageMenu>` serve Browse + the listing header; the kanban (no mark; drag, own trash and confirm) and the extension (glyph, stage `<select>`) keep
    their own shapes; owed: the board's grey `stageColor`, the extension's copied `stageBadge`/`stageAccent` (`roadmap/operator-workflow-track.md`). All MEAN
    one thing: out → a click adds at the entry stage; in → move, or remove behind a two-step confirm. **Never a remove toggle** — close deals into a terminal
    stage. Stages operator-curated (API-enforced). The badge is `pipeline_stages.code` (migration 377), never derived from `position` or the label. Browse's
    pipeline scope (`?pipeline=any|<stage ids>`) is a property-id prefilter mirrored into
    `browse_stats_properties.property_ids_filter` (migration 378) and OUTSIDE preset identity; the chip LOADS
    A VIEW, the sidebar's Curation → Pipeline control modifies. That needs `category_type` nullable
    (`?deal=any`, the "Vše" pill, `FilterDef.nullable`) — NULL has always meant "no constraint" to comparables,
    the watchdog matcher and browse_stats; never an `any` enum member (it would reach the estimation agent).
23. **One measure, one definition, one label — every per-m² figure resolves from
    `public.measure_price_per_m2` / `measure_price_per_m2_basis`** (migration 425; `toolkit/measures.py` +
    `frontend/src/lib/measure.ts` are its Python and SPA faces). No consumer re-derives `price / area` in SQL,
    Python, the SPA or the extension, and **no surface renders the number without the basis that names its
    unit** — sale ~91 535 Kč/m² vs rent ~319 Kč/m²/měs, 300x apart, and a mixed cohort has NO unit and must
    render a gap. The denominator is polymorphic (floor area; PLOT area for `pozemek` — `listings.area_basis`,
    migration 423). Three rails: required-argument signatures, the CI census
    (`tests/test_measure_registry_census.py` + `toolkit.measures.REGISTERED_SITES` — three arms over six
    source trees + every migration statement; it names its own blind spots, so read them before trusting
    a green run) and `FilterDef.basis`. Full rationale: `docs/architecture.md` § rule 23.
24. **Folded into 25** (kept so the citations don't break — rules are never renumbered).
25. **Location: one store, one claim shape, one label, one code predicate; a location PR that adds more than it
    deletes says why in its body and needs the operator's ruling.** `listing_location` (27 columns, migs 501/566) is the ONLY place a listing's
    location is stored — `listings`/`properties` carry none (mig 508), and there is no serving flag and no
    granularity floor — so a place read joins `ll on ll.listing_id = l.id`, casting `ll.geom::geography` for metres
    (uncast = DEGREES). TWO producers write `location_claims`, both fingerprinted in SQL: the `claims_intake` lane
    (payload, page body, the text lane's reading) and `operator_corrections`; ONE four-step resolver writes the
    answer table; TWELVE claim types, ≤ 1 contract entry each, the `obec_name` entry mandatory and live; every
    display is `location_display_label`, every place filter `<level>_id = any(codes)` at four levels. CONSUMERS
    (browse/map/feed/watchdog/dedup) serve a listing only when its location is resolved or determined foreign — ONE
    predicate (`claims_common.SERVED_LOCATION_PREDICATE`, mig 514); detail-by-id surfaces stay reachable. Invariant:
    **every served listing has a row, every active Czech listing has a town** (`location_town_coverage` red until
    zero; foreign is a determination, never a default); a field is added only by operator ruling (katastr_kod
    2026-09, ulice_id 2026-10) or after a measured slowdown, only to `browse_list`. § Location data in `docs/architecture.md`.
## Coding conventions

- Type hints on every signature. `requests` for HTTP, `psycopg` for DB — don't add `httpx` / `aiohttp` / `sqlalchemy` / `supabase-py` lightly (rule #7).
- No comments unless the WHY is non-obvious; no multi-paragraph docstrings (one-liners fine).
- Small single-purpose files: `<portal>_client.py` = HTTP only, `parser.py` / `<portal>_parser.py` = payload→row only;
  `db.py` is for DB I/O — put new policy beside its caller (the policy + dead sweep it still holds are owed, scraper track).
## How to test changes

- **Locally:** one-time `pip install -e ".[dev,api,geo]"`, then `pytest -q` (or `pytest tests/path -q`).
  Interpreter is `python3`. `scripts/test-summary.sh` runs quiet pytest + prints only failures. Mirrors CI.
- **CI:** every push runs `.github/workflows/test.yml` (`gh run watch`, or `scripts/logs.sh <run-id> [pattern]` for pre-filtered logs).
- No-DB end-to-end: `--dry-run`. Single listing: `--detail-only <id>`. Small live run: `--limit 10`.
## Secrets

Never commit secrets (`.env` is gitignored). API keys are **backend-only** — never `VITE_*`-prefix a backend
secret (the frontend build must not see it). **Full env-var / secrets reference:** the `toolkit-api` skill.
## Agent skills

- **Issue tracker:** GitHub Issues on this PUBLIC repo, via `gh` — always with `--json` (plain `gh issue view` / `gh pr view` exit 1 here). See `docs/agents/issue-tracker.md`.
- **Triage labels:** the five default role names (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.
- **Domain docs:** single-context — root `GLOSSARY.md` + `docs/adr/`; the numbered rules above bind as ADRs do. See `docs/agents/domain.md`.
## What is explicitly out of scope right now

- **Team accounts + user-admin surfaces** — per-user auth is LIVE, not out of scope (Supabase
  Auth signup/login, per-user JWTs, `accounts` + RLS, plans + trial; doctrine: `database` skill).
  Out: a 2nd member per account — `account_members.role` is written and read by nothing — and
  any invite / role / remove surface; platform admins are still provisioned by hand in SQL.
- **A public read API** — every data route is gated (`require_token` only as far as § Territories → Frontend allows) and `anon` reads nothing;
  `/health` + the `/images/{key}` photo proxy are open by design, and webhooks + `/u/{token}` verify their own signature/token.

ClickUp is *not* out of scope (a supported API consumer; `'clickup'` is a reserved `estimation_runs.source`);
nor are the email/Telegram channels (rule #16). Don't start out-of-scope work without explicit direction.
## Where the detail lives
| Need | Load |
| --- | --- |
| SQL, migrations, connection modes, Supabase MCP, schema conventions | `.claude/skills/database` |
| Toolkit tools, FastAPI, auth, versioned trace, env-vars & secrets | `.claude/skills/toolkit-api` |
| LLM URL parsing, cached vision/text tools, vision tiers, MF rent map | `.claude/skills/llm-pipelines` |
| Running / debugging scrapers, adding a field, fixtures, reading logs | `.claude/skills/scraper-ops` |
| Dashboards, admin panels, apps, tools — interface/UI design craft | `.claude/skills/interface-design` |
| Full rule rationale, per-portal data sources, territory deep-dives | `docs/architecture.md` |
| Sequencing / what's next | `ROADMAP.md` → `roadmap/<track>.md` |
| Vocabulary: property, ad, canonical ad, merge, split, survivor, curation | `GLOSSARY.md` |
