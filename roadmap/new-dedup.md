> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

# NEW DEDUP — ground-up decision-layer rebuild

Full plan: [`docs/design/new-dedup/PROGRAM.md`](../docs/design/new-dedup/PROGRAM.md) (mission,
decisions ledger, simulation architecture, Wave 0-8 plan — the standing home for wave status).
Surgical removal spec: [`docs/design/new-dedup/CUTOFF.md`](../docs/design/new-dedup/CUTOFF.md).

This track **supersedes** [`roadmap/dedup-track.md`](dedup-track.md) — that file documents the
legacy engine's incremental development and is retained only as history; do not resume work
there.

## Why a rebuild instead of another iteration

The legacy dedup decision engine (candidate discovery → pHash/CLIP cosine → forensic vision
compare, `toolkit/dedup_engine.py` + friends) accumulated years of incremental patches and is
being torn out entirely (2026-08-05 operator directive) rather than patched further. The
replacement is built as a **simulation engine first** — every level computes merge/dismiss
outcomes "as if," over the whole database, into a droppable schema — with nothing writing to
production tables until the full stack is approved end-to-end. See PROGRAM.md's "Mission and
non-negotiables" for the full rationale.

## Status

🟡 **W0 in progress** (Wave 0 — backup + teardown + scaffolding), **W1 started in parallel**
(Wave 1 — shared prerequisites; its additive-only pieces don't depend on W0 landing). See
PROGRAM.md's progress ledger for session-by-session detail; this file tracks only the
phase-to-phase status.

- [x] PR-0: design docs landed (#960).
- [x] Backup branch `backup/pre-new-dedup-2026-08` + tag `backup-pre-new-dedup` cut.
- [ ] Day-0 freeze (disable the 6 legacy decision workflows + confirm the worker dedup lane dark)
      — blocked on operator permission (classifier denies both `gh workflow disable` and the DB
      flip from an agent session).
- [ ] M-0 (`app_settings.dedup_publication_gate_enabled = false`) — same blocker as above.
- [x] pg_dump backup of to-be-dropped tables to R2.
- [ ] PR-1 (backend decision-layer removal) — code + tests done and staged in the worktree; the
      CUTOFF §6 doc pass (CLAUDE.md rule 15, architecture.md §15, 2 skills, legacy design doc
      deletes) is still outstanding before it opens.
- [ ] PR-2 (frontend decision-layer removal).
- [ ] PR-3 (migration: table drops + view redefinition + legacy stamp).
- [ ] W0 verification checklist green → Gate 0 closes.

W1 (shared prerequisites + labeling program) — backend/infra half started:
- [x] `dedup_sim` schema + `settings`/`settings_history`/`simulation_runs` (migration 372).
- [x] Settings registry (`toolkit/dedup_sim_settings.py`), 12 knobs seeded from the decisions
      ledger, each with a plain-language blurb.
- [ ] RunPod serverless workflow — blocked on the operator creating the RunPod account (no
      `RUNPOD_API_KEY` secret exists yet); reuse target is PR #804's pod-side harness.
- [ ] Dashboard skeleton + Labeling page — deliberately held until PR-2 lands (same nav
      territory PR-2 restructures).

Waves W2-W8 (candidate selection through production wiring) are not started; see PROGRAM.md.

## Data-quality prerequisite (operator-run, parallel to the code work)

Flagged during W0 recon (2026-08-05): **122,920 listings sit on 4,206 coordinate pins shared by
>10 listings each** (town/municipality centroids used as a geocoding fallback), with the biggest
single pin holding 1,029 listings. This is a candidate-storm risk for any future geo-proximity
fallback rung (L0's third path) and degrades geo-based candidate generation generally. The
operator is investigating this in separate data-quality sessions (per-portal geocoding /
enrichment fix, cost-aware) — not blocked on or blocking the code removal above, but a
precondition for trusting L0's geo-fallback path once built. The clique guard question (whether
candidate generation needs an explicit "too many pins share this exact point" veto) is **parked**
pending that investigation; see PROGRAM.md's decisions ledger.
