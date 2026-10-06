> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

# MERGE SPRINT — one property, built from its ads

Full plan, the binding decisions MS1–MS23 and the gates:
[`docs/design/merge-sprint/PROGRAM.md`](../docs/design/merge-sprint/PROGRAM.md).

Separate from [AUTODEDUP](autodedup.md), which decides *which* ads are the same unit. This sprint
owns *what a merge and a split do*: the property's facts, its curation, and what the app says.

## State (2026-10-06)
- **Plan approved (PR #1693, merged 2026-10-06).** Decisions come from a four-round design interview; the
  last two questions were answered on 2026-10-03: our condition grades stay for now (Q48), and a
  per-portal "newest on this portal" sort stays, at property grain (Q49). The split rule follows the
  operator's request of the same day: a split by letters, any number of properties in one split.
- **W0 merged (PR #1695, 2026-10-03):** the daily property recompute resumes where the last run
  stopped (it had not completed since 2026-09-29); its gate (a completion stamp) is pending.
- **Hand-over with the AUTODEDUP session agreed in writing** (memory note
  `merge-sprint-handover-to-autodedup`): it keeps `properties.all_sources` / `active_sources`;
  engine-path changes merge after its "C2 CLOSED" line (2026-10-06); its PR #1655 follows the
  destructive window (W6), which waits for a day the operator names.

## Waves
- [x] W0 — daily recompute resumes (PR #1695; gate passed 2026-10-06: one full cycle in one run)
- [x] W1b — asset links and the pipeline note field out (PR #1717, +153 / −1,099)
- [x] W2a — recompute: canonical-ad order, amenity union, portal lists, per-portal newest-ad dates,
      price lineage, city figures follow the canonical ad; realitymix joins the portal filter
      (PR #1719, +755 / −331, migration 588 applied before merge; gate: the first full sweep after the merge)
- [ ] W2b — property page, pipeline board, Browse rows: broker list, lowest price line, chart of
      every ad, marks and note mark in merge mode, honest failed reads
- [ ] W3 — carry record, the count invariant, one merge toast
- [ ] W4 — one split dialog by letters (grown from PR #1699), curation routing per letter with copies
- [ ] W5 — one read-model rewrite, portal and broker filters at property grain, one-portal "Newest first"
- [ ] W6 — the one destructive window

## Later (cut from scope, see PROGRAM.md §9)
- The estimation subject lookup (MS20), with its own design.
- A notice to other accounts after a split.
- Removing our own condition grades (kept for now, Q48).
