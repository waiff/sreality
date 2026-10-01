# docs/design/autodedup

Design documents for **AUTODEDUP** — the autonomous cross-portal listing deduplication
engine (package `autodedup/`, schema `autodedup`, lane `.github/workflows/autodedup.yml`,
and the always-on worker's `autodedup` lane in `scraper/realtime_worker.py`).

- **`PROGRAM.md` is the source of truth.** Rules are numbered E1–E305 for the engine
  (through W31, with gaps and lettered sub-rules) and from E900 for the apply track
  (E900–E999 reserved), all cited by code, tests and PRs: never renumber them. Decisions
  are D1–D94 (engine) and D900+ (apply track); a decided ruling is binding and supersedes
  the design text wherever the two differ, and an open one says so in its row. A PR that
  changes behaviour described there updates it in the same PR.
- **It merges in production, inside the operator's scope only.** The worker's lane
  decides, groups and merges in one pass (E911): a group becomes a merge only through the
  chokepoint (`merge_property_set`, `source = 'autodedup'`, E900) and only inside
  `app_settings.autodedup_apply_scope` (E904 — since migration 570 the trial's three
  blocks, sales and rentals), with a ledger row in `autodedup.applied_merges`. The lane
  never splits; undo is the dispatched `unapply` mode. Brake:
  `realtime_autodedup_interval_seconds` = 0, then `unapply`.
- **The operator's rulings** go to `autodedup.verdicts` / `autodedup.must_not_link`: from
  the `/autodedup/verdict*` routes, and from every operator merge, detach or split through
  the chokepoint (`source = 'operator'`, E919, E920). An engine merge or its undo is never
  a ruling.
- **Everything else stays shadow:** decided and stored in `autodedup.*`, shown on the
  review pages, never applied.
- This program is **separate from `docs/design/new-dedup/`** (the operator-guided
  rebuild). Different schema, package, workflow, concurrency group and settings prefix;
  neither reads the other's data, and neither reads the removed legacy engine
  (CLAUDE.md rule 15) — except the temporary `autodedup/legacy_retire.py`
  (`retire_legacy=1`), which reads its `'auto'` merges only to undo them.
- **Refit W7** (the operator's Browse merges + the j2-w30c verdicts on the contested g13
  pairs): `refit_w7_preregistration.json` (written before any fit) and
  `refit_w7_results.json` (the measured outcome — both candidates refused by the
  pre-registered bars; `w6_gold` stays). The multi-export fit directory is built by
  `autodedup/refit_substrate.py`.
- **Sealed cohorts** (`preregistrations/`): cohort 19 is R1's seal and cohort 20 is MF's.
  Each file fixes the blocks, the export line, the seal and read protocol and the bars
  before its export exists. A seal is read once, at its release's freeze.
