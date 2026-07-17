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
Multi-portal ingestion (Scraper Phase 2) is the larger not-yet-started body of work (design in
`docs/design/multi-portal-dedup.md`); Dedup is active (see the table below).

## Tracks

| Track | Status | Scope | File |
| --- | --- | --- | --- |
| Next | 🟡 in progress | Live front line: real-time Wave C, building B1, Phase QUAL, async agent (7 slice 2 / 7d) | [roadmap/next.md](roadmap/next.md) |
| UI | 🟢 mostly done | Browse / estimation / detail SPA; U-Prop (property detail page) shipped 2026-07-17; U-Nav next, U3 later | [roadmap/ui-track.md](roadmap/ui-track.md) |
| Map | 🟡 mixed | Typed locality IDs + map layers (map-1) | [roadmap/map-track.md](roadmap/map-track.md) |
| Scraper | 🟢 mostly done | Cadence-split, portal framework, prepared stmts; Phase 2 multi-portal (larger, later) | [roadmap/scraper-track.md](roadmap/scraper-track.md) |
| Dedup + canonical listing | 🟡 active | LLM-cost program; funnel now **Anthropic-free** (gpt-5-mini); Session 4 batch deferral + Session 5a recency ordering shipped 2026-07-14; Session 5b image-role registry shipped, pozemek dismissal BLOCKED on a site-plan model fix (gpt-5-mini scores 50% on pozemek per the bake-off); see `docs/design/dedup-vision-and-backlog-overhaul.md` | [roadmap/dedup-track.md](roadmap/dedup-track.md) |
| Operator workflow | 🟢 mostly done | Collections / tags / notes, deal pipeline; U-ME (manual rental estimates) next | [roadmap/operator-workflow-track.md](roadmap/operator-workflow-track.md) |
| Building decomposition | 🟢 mostly done | Paste-a-building unit extraction + fan-out; B3 business-case tab proposed | [roadmap/building-decomposition-track.md](roadmap/building-decomposition-track.md) |
| Skill refinement | 🟡 active | Phase AI — feedback-driven estimation-skill refinement | [roadmap/skill-refinement-track.md](roadmap/skill-refinement-track.md) |
| Summarize | ✅ done | Annotated distribution charts (summarize-1) | [roadmap/summarize-track.md](roadmap/summarize-track.md) |
| Public release | 🟡 active | Accounts, multi-tenancy (RLS), admin gating, Stripe — see `docs/design/public-release-program.md` | [roadmap/public-release-track.md](roadmap/public-release-track.md) |
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
