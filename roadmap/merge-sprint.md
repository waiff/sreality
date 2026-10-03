> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

# MERGE SPRINT — one property, built from its ads

Full plan, the binding decisions MS1–MS23 and the gates:
[`docs/design/merge-sprint/PROGRAM.md`](../docs/design/merge-sprint/PROGRAM.md).

Separate from [AUTODEDUP](autodedup.md), which decides *which* ads are the same unit. This sprint
owns *what a merge and a split do*: the property's facts, its curation, and what the app says.

## State (2026-10-03)
- **Plan awaiting the operator's approval.** Decisions come from a four-round design interview; two
  questions are open (Q48 condition grades and the estimator, Q49 the per-portal sort order).
- **W0 approved and built:** the daily property recompute resumes where the last run stopped (it had
  not completed since 2026-09-29). Draft PR #1695; merges with the AUTODEDUP session's OK.
- **Hand-over with the AUTODEDUP session agreed in writing** (memory note
  `merge-sprint-handover-to-autodedup`): it keeps `properties.all_sources` / `active_sources`;
  engine-path changes merge after its "C2 CLOSED" line (2026-10-06) and after its PR #1655.

## Waves
- [ ] W0 — daily recompute resumes
- [ ] W1a — condition grades out (pending Q48)
- [ ] W1b — asset links and the pipeline note field out
- [ ] W2a — recompute: speaking-ad order, amenity union, portal lists, price lineage
- [ ] W2b — property page, pipeline board, Browse rows: broker list, lowest price line, chart of
      every ad, marks and note mark in merge mode, honest failed reads
- [ ] W3 — carry record, the count invariant, one merge toast
- [ ] W4 — one split dialog (stays / goes / both), curation routing
- [ ] W5 — one read-model rewrite, portal and broker filters at property grain (pending Q49)
- [ ] W6 — the one destructive window

## Later (cut from scope, see PROGRAM.md §9)
- The estimation subject lookup (MS20), with its own design.
- A notice to other accounts after a split.
