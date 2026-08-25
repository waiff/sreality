# Roadmap

The long-term plan for this project. Each phase builds on the previous; tools within a
phase are independent. **CLAUDE.md is the authoritative source for active rules; ROADMAP
is for sequencing.** This file is an index — the actual phase content lives in one file
per track under `roadmap/`, with completed work in `roadmap/archive.md`.

## How this roadmap is maintained

- One file per track under `roadmap/`; `roadmap/archive.md` holds completed work (the old
  `## Done` block).
- After shipping meaningful work, in the SAME PR update **only** the relevant
  `roadmap/<track>.md` (move a bullet to done, add new "next" items) **and** this index's
  status cell if the track's status changed. Don't defer roadmap updates to a follow-up.
- **Never open all track files to make one edit** — go straight to the track you shipped in.
- A large ROADMAP restructure is its own PR (see CLAUDE.md § Git workflow).

## Sequencing

The analytical backbone runs scraper → real-time properties → dedup → estimation; the UI,
Map, Operator-workflow, Building-decomposition, Skill-refinement, and Summarize tracks run
in parallel and are independent within a track. The current front line is in
`roadmap/next.md` — the real-time hot lane (Wave C, greenlit 2026-07-02), building
decomposition (Phase B1), and Phase QUAL (qualitative city data) are the live items.
Multi-portal ingestion (Scraper Phase 2) is the larger not-yet-started body of work. Dedup is
**rebuilding**: the automatic decision engine was removed wholesale in the 2026-08 "NEW DEDUP"
cutoff and is being rebuilt simulation-first (`docs/design/new-dedup/PROGRAM.md`); only the
operator-ordered merge mechanics are live (see the table below).

## Tracks

| Track | Status | Scope | File |
| --- | --- | --- | --- |
| Next | 🟡 in progress | Live front line: real-time Wave C, building B1, Phase QUAL, async agent (7 slice 2 / 7d) | [roadmap/next.md](roadmap/next.md) |
| UI | 🟢 mostly done | Browse / estimation / detail SPA; U-Nav next, U3 later | [roadmap/ui-track.md](roadmap/ui-track.md) |
| Map | 🟡 mixed | Typed locality IDs + map layers (map-1) | [roadmap/map-track.md](roadmap/map-track.md) |
| Scraper | 🟢 mostly done | Cadence-split, portal framework, prepared stmts; Phase 2 multi-portal (larger, later) | [roadmap/scraper-track.md](roadmap/scraper-track.md) |
| Dedup + canonical listing (legacy, superseded) | ⚪ superseded | Legacy decision engine REMOVED wholesale (2026-08 "NEW DEDUP" cutoff); history only — see NEW DEDUP below | [roadmap/dedup-track.md](roadmap/dedup-track.md) |
| NEW DEDUP | 🟡 active (W0 + W1) | Legacy decision engine removed (PR-1/PR-2 merged, M-0 done); PR-3 (table drops) + W0 verification still pending; W1 shared prerequisites running in parallel — see `docs/design/new-dedup/PROGRAM.md` + `CUTOFF.md` | [roadmap/new-dedup.md](roadmap/new-dedup.md) |
| Location data | 🟡 active (W1/W1v/W3 shipped, W2a closing) | Greenfield location SSOT (claims → resolutions → projections on RÚIAN, four-axis precision, licence enforcement); spine live in **shadow** (migs 380–389, 399–400, 402–408), nothing reads it until W6; **W3 history backfill SHIPPED 2026-08-19 — 1.63 M snapshots mined, 92,312 historical claims, ~10.85 M observations, all four gate arms pass; measured oscillation is small (804 of 327,113 listings ever changed a location value), which should temper any wave resting on location churn**; W2a archive measured + bounded, dual-write ON, backfill terminal; W2 infra shipped but **inert** — the per-portal contracts (W2-6…W2-12) are the next real work | [roadmap/location-data.md](roadmap/location-data.md) |
| Hydration sprint | 🟡 active (Lane 0 + W0 done) | One way to load a surface: one read proportional to what renders, decorations streamed, never blocking. Hotfixes shipped — collection/tag CRUD was cross-tenant readable (#1119), Browse's default view was answering HTTP 500 on a single-value `in.()` (#1120, 15,877→6 blocks), browse_list rebuild was at ~83% duty cycle (mig 413). W1 (pipeline progressive hydration) next | [roadmap/hydration-sprint.md](roadmap/hydration-sprint.md) |
| Measure unification | 🟡 active (W4 keystone + W5 Python/API shipped) | One measure, one definition, one label — 64 per-m² sites across 4 territories collapsed onto one named SQL measure + basis label; two live input defects (mmreality plot-as-floor-area, three portals storing unit prices as absolute) fixed at the source. Charter: `docs/design/ppm2-measure-unification.md` | [roadmap/measure-track.md](roadmap/measure-track.md) |
| Measure unification | 🟡 active (W4 keystone shipped) | One measure, one definition, one label — 64 per-m² sites across 4 territories collapsed onto one named SQL measure + basis label; two live input defects (mmreality plot-as-floor-area, three portals storing unit prices as absolute) fixed at the source. Charter: `docs/design/ppm2-measure-unification.md` | [roadmap/measure-track.md](roadmap/measure-track.md) |
| Operator workflow | 🟢 mostly done | Collections / tags / notes, deal pipeline; U-ME (manual rental estimates) next | [roadmap/operator-workflow-track.md](roadmap/operator-workflow-track.md) |
| Building decomposition | 🟢 mostly done | Paste-a-building unit extraction + fan-out; B3 business-case tab proposed | [roadmap/building-decomposition-track.md](roadmap/building-decomposition-track.md) |
| Skill refinement | 🟡 active | Phase AI — feedback-driven estimation-skill refinement | [roadmap/skill-refinement-track.md](roadmap/skill-refinement-track.md) |
| Summarize | ✅ done | Annotated distribution charts (summarize-1) | [roadmap/summarize-track.md](roadmap/summarize-track.md) |
| Public release | 🟡 active | Accounts, multi-tenancy (RLS), admin gating, Stripe — see `docs/design/public-release-program.md` | [roadmap/public-release-track.md](roadmap/public-release-track.md) |
| Listing identity | 🟡 active | Retire the `sreality_id` smart key; Phase 0/R1/natural-key/URL cutover shipped, R2→PK-swap in progress per `docs/design/listing-identity-r2-pk-swap-runbook.md` | [roadmap/listing-identity-track.md](roadmap/listing-identity-track.md) |
| Archive | ✅ 80 entries | All completed phases (dated) | [roadmap/archive.md](roadmap/archive.md) |

## Out of scope until explicitly opened
- ClickUp integration.
- MCP server wrapping the toolkit (for ad-hoc chat with the data).
- Public read API beyond the bearer-token gate.

Per-user identity / accounts is no longer out of scope — see the Public release track above.

## Data preconditions
- Velocity tools (Phase 3b) work today (1 snapshot per listing is enough
  for TOM math).
- Outlier history-pattern detection (Phase 3a) becomes more useful as
  snapshot density grows past ~1.5/listing average.
- Cluster detection (Phase 5) needs neighborhoods with 30+ comparables
  to be meaningful; sparse rural areas will return single-cluster
  results.
