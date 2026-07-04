# CLAUDE.md

**The project brain** — standing context for any session that touches this repo. This file
holds the hard rules only; the WHY (full rationale, edge cases, incident history) lives in
`docs/architecture.md`, and operational how-tos live in on-demand skills under
`.claude/skills/`. Read the relevant one before changing code it governs. When a rule here
keeps getting broken, fix it here — don't repeat the correction by hand.

## What this project is

A **market-wide real-estate intelligence platform** for the Czech market. It began as an
hourly sreality.cz scraper and now collects, enriches, and reasons over property data from
**seven portals**. Store of record: Postgres (Supabase, Frankfurt, PostGIS) with full
listing history.

Data layered together:
- **Scraped listings** from seven portals — **sreality** (JSON v1 API, the steady hourly
  ingest), **bazos** (HTML crawler), **bezrealitky** (GraphQL API), **idnes** (structured
  HTML), **mmreality** (Vue-embedded JSON, proxied), **remax** (structured HTML), and
  **ceskereality** (structured HTML) — landing in one `listings`/`listing_snapshots`
  contract with one canonical vocabulary. Per-portal ingest detail: `docs/architecture.md`
  § Data sources.
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
that overlays estimates on portal pages. Multi-portal rows sit behind a thin `properties`
parent (migration 091) so one real-world property seen on several portals can be grouped.

## Territories

Three top-level territories with deliberately different rules — identify which one a task is
in before starting. Deep per-territory rationale: `docs/architecture.md` § Territories.

- **Backend** (`scraper/`, `toolkit/`, `api/`, `migrations/`, `tests/`, `.github/workflows/`)
  — Python 3.12, stdlib-first, `psycopg` direct to Postgres, service-role (reads + writes
  anything). Runs in GitHub Actions + Railway. All architectural rules below apply.
- **Frontend** (`frontend/`) — Vite + React 18 + TypeScript + Tailwind v4 SPA on Railway.
  **anon key only** (never a secret in browser code); reads `*_public` views + `SECURITY
  INVOKER` RPCs; **no write path from the browser** — writes go through the bearer-gated API.
  Design tokens in `globals.css` `@theme` — never change without operator approval. Backend
  rules below don't apply here.
- **Chrome extension** (`chrome-extension/`) — Manifest v3, **vanilla TS only** (no React /
  Tailwind), closed shadow-root panel. Every network call goes through the background worker
  (`chrome.runtime.sendMessage`), never a direct `fetch`. Build-time `VITE_API_*` inlined
  (Path 1; ship `dist/` to trusted operators only). Backend + SPA rules don't apply here.

When in doubt which territory a task is in, ask. Don't import frontend deps into the Python tree or vice versa.

## Working with the operator

The owner works locally in **VS Code on WSL2 Ubuntu** with a full terminal, local Git/Python,
and authenticated `gh` — so suggest and run local commands (tests, git, `gh`, debugging).
Production still runs in the cloud (Actions + Railway); local is for dev/test/debug. The
operator is **non-technical by training but learns fast** — explain the *why*, define jargon
on first use ("upsert", "JWT", "RLS", "draft PR"), and give click-by-click steps for browser
tasks (Supabase SQL editor, GitHub settings pages).

## Git workflow and pull requests

Short-lived branches, merge via PR. **Never push directly to `main`** — Railway auto-deploys
from `main`, so a merged PR *is* the deploy; PR + branch protection + CI is the production gate.
- **Branch naming:** `feature/<name>`, `fix/<name>`, `cleanup/<name>` or `roadmap/<name>` (docs/hygiene).
- **Start:** `git checkout main && git pull && git checkout -b <branch>`.
- **One PR = one purpose.** Don't mix a feature with an unrelated docs/ROADMAP rewrite (a
  *large* ROADMAP restructure is its own PR; small phase-entry bookkeeping rides with the work).
- **End** by pushing the branch + opening a PR; return the URL. Commit messages / PR bodies
  follow the harness footer convention.

## Autonomy and the safety net

Default to **full autopilot**: create the branch, push early, open a **draft PR** so the
operator can watch, and work to completion.
- **Stop and surface** — don't paper over — a merge conflict, a failing test, or genuine ambiguity.
- The safety net: CI (`.github/workflows/test.yml`) runs on every push + branch protection guards
  `main`, so broken code can't reach production. Lean on it; keep tests green.
- **Database changes** have their own gate (the `database` skill: additive migrations are
  autonomous; destructive ones pause for confirmation + a backup).

## Fetching live state (fetch, don't ask)

Dynamic state lives outside Git — don't ask, fetch it:
- Recent activity → `git log --oneline -10`; branch / tree → `git status`, `git branch --show-current`.
- Migrations on disk → `ls migrations/ | tail -5`; Actions runs → `gh run list --limit 10`.
- **DB reads (counts, freshness, schema, verification SELECTs) → `psql "$SUPABASE_DB_URL" -c "…" | head`**,
  NOT the Supabase MCP (its verbose output persists in context). Ready-made commands + the
  reserved-for-migrations MCP policy are in the `database` skill: after a heavy MCP phase run
  `/compact`; disable the server with `/mcp` in sessions that don't touch the DB.

## Roadmap maintenance

`ROADMAP.md` is a **<120-line index**; phase content lives in `roadmap/<track>.md` and completed
work in `roadmap/archive.md`. After shipping meaningful work, in the SAME PR update **only** the
relevant `roadmap/<track>.md` (move a bullet to done, add new "next" items) + the index's status
cell if the track's status changed — **never open all track files to make one edit**. A large
restructure is its own PR.

## Context discipline

- Prefer `grep` / targeted line-range reads over whole-file reads for files >500 lines (this file,
  most `toolkit/` / `api/` / `scraper/` modules, any `roadmap/` track).
- Read the `ROADMAP.md` index only; open a `roadmap/<track>.md` only when editing that track.
- Summarize tool output instead of quoting it back; delegate verbose searches to subagents so their
  output stays out of the main context.
- Load a skill (`database`, `toolkit-api`, `llm-pipelines`, `scraper-ops`) when its trigger fits,
  rather than re-deriving from memory.

## Architectural rules (do not violate without asking)

**Numbers are cited by code/tests/design-docs — never renumber.** Full rationale, edge cases, and
incident history: `docs/architecture.md` § Architectural rules.

1. **Migrations are append-only.** Never edit an existing numbered file; schema changes go in a new
   `NNN_*.sql`, applied via the Supabase MCP. Additive = autonomous; destructive = pause for OK +
   pg_dump. Prune dead schema with a new forward migration, not by editing history. (see `database` skill)
2. **Snapshots on content change only.** Never write `listings` without computing the content hash and
   appending a `listing_snapshots` row when it differs from that listing's latest snapshot. Every write
   path into `listings`.
3. **Never delete; delist via `is_active=false`.** History is sacred. Infer inactive ONLY after a
   ~complete index walk (≥99.5%, `INDEX_MIN_COMPLETENESS`) AND only for rows additionally unseen 24h+
   (`min_unseen_hours`); partial walks (`--limit` / `--detail-only` / `--max-pages`) must never flip
   rows; a false flip self-heals on next sighting (`touch_listings`); a gone detail fetch (404/410 →
   `ListingGoneError`) flips that one listing immediately. Every flip stamps `inactive_at`.
4. **`last_seen_at` is driven by index sightings + successful detail fetches only; failed fetches never
   touch it** (else repeated failures would falsely delist a live listing). The `unchanged` freshness
   path also doesn't bump it — its signal is `listing_freshness_checks.checked_at`.
5. **Failed detail fetches are tracked, not dropped** — `listing_fetch_failures(sreality_id, attempts,
   last_error, given_up)`; failures jump to the front of the refetch queue; `given_up` after 5 attempts;
   the row is deleted on success.
6. **Images download to Cloudflare R2** (bytes, not just URLs). `images` tracks per-image state
   (`storage_path`, `download_attempts`); a separate phase after the scrape, no-op without R2 env vars.
7. **No new dependencies without justification.** Prefer the stdlib; each `pyproject.toml` entry needs a reason.
8. **Latest-wins + snapshot history.** `listings` is current state; every meaningful change appends a
   `listing_snapshots` row. Estimates capture the `snapshot_id` of each comparable (retrospective audit)
   — don't build as-of semantics into live queries.
9. **`listing_freshness_checks` is append-only + ephemeral** (rows >30d safe to delete; no auto-prune).
   It's observability + throttling, not history — the history table is `listing_snapshots`.
10. **`amenities` + `amenity_fetches` are an OSM mirror, not history** — written by `find_anchor_amenities`
    on cache miss; a POI's category is set by the *query*, not OSM tags; taxonomy in `toolkit/amenities.CATEGORY_TAGS`.
11. **`transit_lines` + `transit_line_fetches` are a parallel OSM mirror** for route geometry (migration
    028) — written by `find_comparables_along_axis`; one row per (relation, member way); tram/subway/bus; 30-day TTL.
12. **`estimation_runs` is the single source of truth for every estimation** (UI / API / ClickUp / agent).
    Sync mode INSERTs once with a terminal `status`; failed runs still persist a row (HTTP 200 +
    `status='failed'`); re-runs INSERT with `parent_run_id`; originals are immutable. Sources: `ui`/`api`/`clickup`.
13. **`building_runs` is the paste-a-building parent.** Children are `estimation_runs` linked via
    `building_run_id` + `building_unit_id`; the unit list is operator-curated JSONB. Status:
    `pending → extracting → awaiting_input → estimating → success|failed`; `awaiting_input` is the
    human-in-the-loop gate; `units_proposal` (agent) vs `units` (confirmed) are kept separate.
14. **Condition scoring is two-axis (building + apartment).** Raw `listings.condition` stays the source;
    the two derived `listings.{building,apartment}_condition_level` (1..5, NULL unscored) + the
    `listing_condition_scores` cache (keyed `(sreality_id, snapshot_id)`) are written together by
    `score_listing_condition` in one latest-wins transaction. Filter on the derived columns, not the
    coarse `condition_assessment`.
15. **Multi-portal listings sit behind a thin `properties` parent (migration 091); grouping is out-of-band,
    never inline at insert (new rows get a singleton property).** Two eligibility paths feed **one**
    `resolve_pair` decision tree (pHash fast-path → CLIP cosine tier → forensic visual compare →
    floor/site-plan gate): **apartments** key on **street + disposition**; **single-dwelling house / land /
    commercial** key on **geo-proximity** (its OWN scheduled run, `dedup_geo_enabled`). A geo/visual signal
    never auto-merges on proximity alone — the forensic **High** verdict is the sole auto-merge gate; the
    floor-plan gate only ever adds conservatism (`different_layout` dismiss; both-2D `inconclusive` queues;
    `no_2d_plan` / one-sided → proceed). `db.mark_inactive` / `active_count` are source-scoped; category
    compatibility is enforced at merge (sale≠rent, flat≠house — except the one sanctioned **dům↔komerční**
    cross-type); merges are reversible (`property_merge_events` / `unmerge_group`). Applies to anything
    touching `listings.property_id`, the dedup engine, or `properties` rollups. Full engine spec:
    `docs/architecture.md` § rule 15 (+ `docs/design/multi-portal-dedup.md`, `dedup-byt-precision.md`, `clip-visual-embeddings.md`).
16. **Watchdog + Browse share one definition of "matches"** (`_shared_filter_where` + `_city_quality_clauses`).
    `notification_dispatches` is the unified, property-grain, append-only event table with **two producers**
    (`watchdog` + `collection_monitor`), a per-event `dedupe_key` (`:new:` once-ever, `:price_drop:{snapshot_id}`
    per-snapshot), producer-stamped `target_channels`, and a `monitor_since` anchor so a change predating
    membership never fires. **Delivery is separate from detection**: in-app = the row itself; external channels
    drain via a `channel_sends` ledger, not a `channel`-column widen. Merges re-point rows (rule #18).
17. **City-quality indexes are a normalized, operator-curated time series** (`curated_cities` + `city_index_*`
    + `city_population`) — a new index needs no migration; latest revision wins; agenda-gated to **Browse +
    Watchdog only** (the estimation agent never sees them, preserving deterministic estimates).
18. **Operator curation is PROPERTY-grain and dedup-stable** (`collections`, `tags`, `property_notes`, all
    keyed on `property_id`; migration 202). `toolkit/operator_state.py` (`OPERATOR_STATE_TABLES` registry —
    including `notification_dispatches`) re-points state onto the survivor inside the `merge_properties`
    transaction, so no row orphans onto a `merged_away` property; unmerge/split are best-effort. Collections
    carry monitoring (`monitoring_enabled` + `notify_channels`). Writes go through the API; a new
    property-anchored table = one registry line.
19. **The scrape is cadence-split: a fast index-walk feeds an async batched detail-drain via
    `listing_detail_queue`** (migration 105). Index-walk (`--index-only`) walks the full index,
    `touch_listings` + completeness-gated `mark_inactive`, and enqueues; detail-drain (`--drain-only`) claims
    a bounded slice (`FOR UPDATE SKIP LOCKED`) and writes batched via `write_detail_batch`. New rows land
    `property_id` NULL (grouping deferred, rule #15). Every portal runs this same split through the shared
    `portal_runner` on the source-generic queue.
20. **Property maintenance is dirty-set incremental, not full-table.** Child-changing writers enqueue
    `property_id` into `dirty_properties` (migration 106); `property_maintenance.yml` (`--incremental`, `*/5`)
    attaches new singletons + recomputes only queued properties (O(changes)); the daily full sweep (04:15) is
    the reconcile backstop. Both share the `sreality-property-maintenance` concurrency group.
21. **Every portal runs through ONE shared framework (Phase 4); per-portal code is a fetcher + parser +
    config row — no per-portal branches in shared code.** `portal_base` / `portal` / `portal_runner`; one
    source-generic `listing_detail_queue`. A portal that can't prove a near-complete walk sets
    `supports_complete_walk=false` and is never marked inactive from index absence (rule #3). The one
    sanctioned per-portal hook is sreality's district-split.
22. **The deal pipeline is single-valued, property-grain operator state** (migration 205): `property_pipeline`
    holds ≤1 card per property at one `pipeline_stages` stage (a TABLE, not an enum); "bookmark" == presence of
    a row at the entry stage. It has its OWN merge reconciler (`reconcile_pipeline_on_merge`, TERMINAL-AWARE — a
    live stage always beats a closed one; snapshots to `property_pipeline_events`) + lossless unmerge. Writes
    through the bearer-gated API. The affordance is the shared `<FunnelIcon>` on EVERY surface (Browse card,
    listing header, kanban, Chrome extension); kanban moves are drag-and-drop only; stages are operator-curated
    (entry/terminal invariants API-enforced).

Full rationale, edge cases, and incident history: read `docs/architecture.md` before modifying anything
these rules touch.

## Coding conventions

- Python 3.12, type hints on every signature. Prefer the stdlib; justify each dependency.
- No comments unless the WHY is non-obvious; no multi-paragraph docstrings (one-liners fine).
- `requests` for HTTP, `psycopg` for DB — don't add `httpx` / `aiohttp` / `sqlalchemy` / `supabase-py` lightly.
- Small single-purpose files: `sreality_client.py` = HTTP only, `parser.py` = JSON→row only, `db.py` = DB I/O only.

## How to test changes

- **Locally:** one-time `pip install -e ".[dev,api,geo]"`, then `pytest -q` (or `pytest tests/path -q`).
  Interpreter is `python3`. `scripts/test-summary.sh` runs quiet pytest + prints only failures. Mirrors CI.
- **CI:** every push runs `.github/workflows/test.yml` (`gh run watch`, or `scripts/logs.sh <run-id> [pattern]`
  to fetch pre-filtered logs) — CI + branch protection is the autopilot safety net.
- No-DB end-to-end: `--dry-run`. Single listing: `--detail-only <id>`. Small live run: `--limit 10`.

## Secrets

Never commit secrets (`.env` is gitignored). API keys are **backend-only** — never `VITE_*`-prefix a backend
secret (the frontend build must not see it). **Full env-var / secrets reference** (DB, R2, LLM, maps, API,
notifications, scraper orchestration, frontend build-time): the `toolkit-api` skill.

## What is explicitly out of scope right now

- **Auth / user management** — single-operator platform, one shared API token, no per-user identity.
- **A public read API** — the bearer-gated FastAPI service is private (the Railway URL is the perimeter).

ClickUp is *not* out of scope (a supported API consumer; `'clickup'` is a reserved `estimation_runs.source`).
A free email/Telegram notification channel is planned (tracked in ROADMAP, not here). Don't start anything
out of scope without explicit direction in a new session.

## Where the detail lives

| Need | Load |
| --- | --- |
| SQL, migrations, connection modes, Supabase MCP, schema conventions | `.claude/skills/database` |
| Toolkit tools, FastAPI, auth, versioned trace, env-vars & secrets | `.claude/skills/toolkit-api` |
| LLM URL parsing, cached vision/text tools, vision tiers, MF rent map | `.claude/skills/llm-pipelines` |
| Running / debugging scrapers, adding a field, fixtures, reading logs | `.claude/skills/scraper-ops` |
| Full rule rationale, per-portal data sources, territory deep-dives | `docs/architecture.md` |
| Sequencing / what's next | `ROADMAP.md` → `roadmap/<track>.md` |
