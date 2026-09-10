# The candidate generation lane (NEW DEDUP Level 0, path C)

`new_dedup_candidates.yml` — **manual `workflow_dispatch` only, no schedule, CPU only, no
external service, no money.** Level 0 of the rebuilt dedup engine finds the listing pairs the
later levels (pHash, embeddings, vision) will compare. It decides nothing. Design and the
operator's ruling: `docs/design/new-dedup/PROGRAM.md` — the decisions-ledger row "Candidate
path C" and the `2026-09-10 (a)` ledger entry.

Three words do the work below:

- a **path** is one way of looking for pairs. **Path C**, the first and so far only one built,
  blocks on the **town** — `listing_location_current.obec_kod`, the new location engine's
  projection (CLAUDE.md rule 24), never the legacy `listings.obec_id` / `geom` / `street` —
  and compares attributes. Path A (street / geo / radius) is a second `PathDef` in
  `toolkit/dedup_candidates.py`, not built; path B (image similarity) is Wave 3.
- a **rung** is which attributes were compared: **C1** = town + disposition + area (both sides
  carry a disposition; the areas, when both are stated, must be within the wide C1 tolerance),
  **C3** = town + area (taken when a disposition is missing on either side — the fallback is on
  ABSENCE, never on a mismatch). C2 does not exist.
- a **parameter set** (`dedup_sim.candidate_inputs`) is the settings a run used, identified by
  a **fingerprint**; a **generation** (`dedup_sim.candidate_generations`) is one run of one
  parameter set, linked to its `dedup_sim.simulation_runs` row.

## The rule, in one place

`toolkit/dedup_candidates.evaluate_pair` is the rule as Python and the **oracle** the SQL
(`toolkit/dedup_candidates_sql.py`) is held to. What it says, and what `verify` checks:

- same town (`obec_kod`), granularity at least `obec` by RANK (`dedup_path_c` floor);
- in a split town (`l0_path_c_district_split_towns`: Praha, Brno, Ostrava) the two listings must
  name the same city district (`cast_obce_kod`) **when both name one**; an unknown district
  reaches the whole town and never vetoes;
- sale ≠ rent, `category_type` NULL = unknown = not a conflict; `category_main` equal, NULL, or
  the one sanctioned dům ↔ komerční cross-type — the merge chokepoint's guards, verbatim;
- byt floor rule ±`l0_floor_tolerance`, checked only when BOTH sides are byt with a floor,
  otherwise the pair is kept and marked `floor_checked = false`;
- C1: equal disposition (trimmed; NULL/blank = not available), plus the areas within
  `l0_c1_area_tolerance_pct` when both sides state one — skipped, pair kept, when either does not;
- C3: both areas present and > 0 (`estate_area` for pozemek, else `usable_area`), the gap as a
  percent of the larger side ≤ `l0_area_tolerance_pct_general` (or `_pozemek` when either side
  is land);
- scope `l0_candidate_scope`: `all` (every listing ever) or `active` (both sides active).

Every knob is in the NEW DEDUP Settings page; all of path C's knobs are in the fingerprint, so
changing one and re-running writes a NEW parameter set and leaves the old rows alone.

## Modes

| mode | writes? | what it does |
| --- | --- | --- |
| `estimate` | no | counts the pairs a generation WOULD write, per rung and per town, for every scope in `scopes`; prints the funnel per portal × type, the largest (town, disposition) buckets and the town-assignment breakdown into the job summary. Needs no migration-492 table. |
| `verify` | no | for the named `blocks` (small towns, ≤ `max_listings`), runs the rung statements as plain SELECTs and compares every pair, rung and evidence value with the oracle on the same listings. **Fails the job on any disagreement.** Run it on a few towns after any SQL change. |
| `generate` | **yes** | `dry_run=true` (default) prints the plan. `dry_run=false` opens a generation and upserts pairs town by town in id-range chunks (`chunk` listings per statement, each paired against the whole town), records a resume cursor after every chunk, then the audit statistics onto the generation row and — only when `blocks` was empty — the stale sweep. |

## Order of operations, and what to look at

1. **`estimate` first, whole corpus, `scopes=all,active`.** The job summary is the number the
   operator decides on: Praha at obec grain is on the order of 10⁸ pairs all-time. Nothing is
   written until that is accepted (and migration 492 applied).
2. **`verify` on two or three small towns** (`blocks=<obec_kod,…>`). AGREE is the only
   acceptable outcome; a DISAGREE lists the first pairs on each side.
3. **`generate` with `dry_run=true`**, then on a **pilot** (`blocks=582786` for Brno) with
   `dry_run=false`, then the whole corpus. A partial run never sweeps stale rows.
4. A generation that stopped (job timeout at 350 min, a network error — `running` or
   `failed`) resumes with `resume=true` — same settings, same parameter set; it continues from
   the cursor in `candidate_generations.progress` (`last_block_key` + `last_id_to`) and a failed
   one is reopened first. **A resume keeps the scope the generation was opened over**: a pilot
   stays a pilot (no stale sweep) whatever `blocks` says now, and a `blocks` value that differs
   from the stored one is refused. Changing a setting between the two runs makes it a NEW
   parameter set, and `resume` will find nothing to resume.

The `INFO` lines name the town, its listing count and the running pair total on every large
town (≥ 5,000 listings) and every 200th town; the job summary carries the final matrix.

## Reading the store afterwards

- `dedup_sim.candidate_generations.stats` is what the Candidate audit page reads: the rung ×
  type matrix, listings with ≥ 1 candidate per type, the largest towns, the town-size
  distribution, the funnel per portal × type, the largest buckets, the town-assignment
  breakdown. Never scan `candidate_pairs` from the API.
- One row per pair per parameter set: `(inputs_id, listing_id_lo, listing_id_hi)`, `lo < hi`.
  `generation_id` says which run last produced it; `first_seen_at` / `last_seen_at` bracket it.
- A failed run keeps its row (`status='failed'`, `error_message`, the cursor in `progress`).

## What it will not do

- Merge anything, or write outside `dedup_sim`. The operator owns every merge decision.
- Invent a threshold: every number is a setting the operator can see and change.
- Read legacy location columns, even where they are better populated today.
