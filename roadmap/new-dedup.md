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

🟡 **W0 nearly done** (Wave 0 — backup + teardown + scaffolding: legacy engine removed, only the
teardown migration + verification checklist left), **W1 in progress in parallel** (Wave 1 —
shared prerequisites). See PROGRAM.md's progress ledger for session-by-session detail; this file
tracks only the phase-to-phase status.

- [x] PR-0: design docs landed (#960).
- [x] Backup branch `backup/pre-new-dedup-2026-08` + tag `backup-pre-new-dedup` cut.
- [x] Day-0 freeze (the 6 legacy decision workflow files were deleted outright by PR-1, which
      supersedes "disabled").
- [x] M-0 (`dedup_publication_gate_enabled=false`, `realtime_dedup_interval_seconds=0` — both
      confirmed live).
- [x] pg_dump backup of to-be-dropped tables to R2.
- [x] PR-1 (backend decision-layer removal) — merged (#966), including the CUTOFF §6 doc pass.
- [x] PR-2 (frontend decision-layer removal) — merged (#967), shipped a minimal NEW DEDUP nav
      placeholder (Dashboard + Settings stub) that W1 is now filling in.
- [ ] PR-3 (migration: table drops + view redefinition + legacy stamp) — not started.
- [ ] W0 verification checklist green → Gate 0 closes (blocked only on PR-3 now).

W1 (shared prerequisites + labeling program):
- [x] `dedup_sim` schema + `settings`/`settings_history`/`simulation_runs` (migration 372, #965).
- [x] Settings registry (`toolkit/dedup_sim_settings.py`), 12 knobs seeded from the decisions
      ledger, each with a plain-language blurb (#965).
- [x] Settings API (`api/routes/new_dedup.py`, admin-gated) + the real NEW DEDUP Settings page
      (`frontend/src/pages/NewDedupSettings.tsx`, replaces PR-2's placeholder) — operator can now
      review/tune every decided default ahead of any wave consuming it.
- [ ] Dashboard skeleton (funnel + cost table) — still just PR-2's placeholder; genuinely no data
      to show until W2+ produces candidates/decisions. Revisit once W2 lands.
- [x] Labeling page — `dedup_sim.taxonomy_labels`/`labeling_sample`/`label_proposals` (migration
      373), `toolkit/dedup_sim_labeling.py`, `/new-dedup/labeling/*` API, and
      `frontend/src/pages/NewDedupLabeling.tsx`: Taxonomy v1 add/rename/remove, sample grow,
      proposal review (single + batch confirm/dismiss) with a new-vs-original tag toggle.
      Secondary-CLIP scoring (`scraper/label_proposal_tagger.py` +
      `scripts/label_proposal_backfill.py`, dispatch-only GH Actions workflow) is separate infra
      from the DINOv2/RunPod embeddings path below — a stronger CLIP checkpoint, CPU, no RunPod
      dependency. Operator still needs to run several labeling rounds to reach Gate 1 (150
      confirmed images/tag) — the tool is built, the labeling itself is ongoing curation work.
- [ ] RunPod serverless workflow (DINOv2 embeddings, Wave 5) — blocked on the operator creating
      the RunPod account (no `RUNPOD_API_KEY` secret exists yet); reuse target is PR #804's
      pod-side harness.

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
