# docs/design/autodedup

Design documents for **AUTODEDUP** — the autonomous cross-portal listing deduplication
engine (package `autodedup/`, schema `autodedup`, lane `.github/workflows/autodedup.yml`).

- **`PROGRAM.md` is the source of truth.** Rules are numbered E1–E44 and cited by code,
  tests and PRs: never renumber them. Rulings D1–D10 are binding and supersede the design
  text wherever the two differ. A PR that changes behaviour described there updates it in
  the same PR.
- The whole program runs in **shadow mode** — decide, store, write nothing.
- This program is **separate from `docs/design/new-dedup/`** (the operator-guided
  rebuild). Different schema, package, workflow, concurrency group and settings prefix;
  neither reads the other's data, and neither reads the removed legacy engine
  (CLAUDE.md rule 15).
- **Refit W7** (the operator's Browse merges + the j2-w30c verdicts on the contested g13
  pairs): `refit_w7_preregistration.json` (written before any fit) and
  `refit_w7_results.json` (the measured outcome — both candidates refused by the
  pre-registered bars; `w6_gold` stays). The multi-export fit directory is built by
  `autodedup/refit_substrate.py`.
