# Geo-dedup false merges from town-level coordinates (+ audit-trail integrity)

> **Status: PROPOSED — for operator review.** Root-caused from a live bug report
> (listing `-238768`, 2026-07-12) and a 12-agent verified re-investigation
> (2026-07-13). Nothing here is applied. An earlier draft of this investigation
> concluded "no property was ever wrongly merged — display bug only"; that
> headline was **wrong** (see *The correction* below) and this document replaces it.

## The correction that reframes everything

The operator reported the merge as **improper**. A first pass concluded it was a
harmless display bug ("no property was ever wrongly merged"). That conclusion was
built on a flawed check: it only proved no property was merged *with itself*
(`survivor == retired`, which is 0 market-wide) — a test that **by construction
cannot see a merge of two *distinct* properties**. When the two listings were
actually compared, they are plainly **two different houses**:

| field | `-238768` | `-235602` |
|---|---|---|
| price | 12,899,000 Kč | 17,289,000 Kč (+34%) |
| area (usable / estate) | 230 m² / 330 m² | 213 m² / — |
| construction | brick (`cihla`), **reconstruction** | mixed (`smíšená`), **new-build** (`novostavba`) |
| village (description + URL slug) | **Horní Bousov** | **Vlčí Pole** |
| realitymix native id | 8554755 | 8468621 |
| shared photos (pHash pairs) | **0** | — |
| coordinate | `POINT(15.12812 50.43825)` | **identical** (0 m apart) |

The operator was right: this was a **genuine false merge** of two distinct
houses. The "two identical IDs" in the audit is a **separate, second** defect
that hid what happened. This document treats the false merge as the **primary**
finding and the audit display bug as a **secondary, orthogonal** one — they are
different bugs (a merge-correctness defect vs. a telemetry-corruption defect)
and, per the verification, must be fixed on independent tracks.

## Two distinct defects

### Defect A (primary) — a distinct-property false merge caused by town-level coordinates

Neither listing has a street address, so both inherited **realitymix's single
town-level coordinate for "Dolní Bousov"** (identical to the metre; the pin sits
~200 m from the cadastral-area centroid — it is a town geocode, not a per-house
GPS fix). `geocode_attempted_at` is NULL on both (coords arrived from the portal;
our coord→street resolver never ran). The geo-dedup lane's blocking key
collapses every street-less same-town listing into **one candidate cell**, so
these two houses became a pair, and — after the engine correctly declined to
auto-merge — an operator approved them from the /dedup review queue.

### Defect B (secondary) — `dedup_pair_audit` records the same listing id on both sides

For the same merge, `dedup_pair_audit` row 61455 has
`left_sreality_id = right_sreality_id = -238768` (the "two identical IDs" the
operator saw), while `left_property_id 374331 / right_property_id 377610` are
correctly distinct. Market-wide: **4,539** audit rows are self-paired on the
listing id, all with distinct property ids. This is a display/telemetry defect,
not a merge-correctness one — but it corrupts the exact decision-history surface
an operator uses to *audit* a merge, which is why it compounded the confusion.

## Root cause of Defect A — a three-part failure (none sufficient alone)

Every claim below is cited to code and was independently verified.

**1. Proximity mistaken for evidence: the coordinate is not a precise anchor,
and nothing knows it.** The geo cell key is
`geo:{obec_id}:{round(lat,4)}:{round(lng,4)}:{bucket}:{type}`
(`toolkit/dedup_engine.py:623`, the migration 276 stored trigger). Two
street-less houses in the same obec sharing a town geocode land on the
**identical literal key** → one candidate cell. There is **no coordinate-precision
signal anywhere the write path or the engine can see** — `listings` has no
accuracy/precision/pin-share column (`scraper/db.py` `_LISTING_COLUMN_PGTYPE`);
`geom` is written raw via `ST_MakePoint` with no metadata; realitymix's parser
stamps `raw['coords']={"source":"page"}` for a town centroid and a real building
fix **identically** (`scraper/realitymix_parser.py:394-400`). The `classify_geo_pair`
reject gates (`toolkit/dedup_engine.py:632-691`) fire only on category mismatch,
coord > 35 m (**useless** — town pins are 0 m apart), house-number contradiction
(both NULL here), area gap > 20% (this pair: 7.4%), and unit-marker contradiction.
Price is used **only positively** (`_price_match`, `_attr_exact_nonbyt` requires
*identical* price to merge) — so the 34% price gap contributes **nothing**.

**2. The contradiction is invisible to the machine.** With no dismiss signal
tripped, the pair becomes a `geo_weak` candidate; pHash = 0; the forensic vision
compare returns neither a High (merge) nor a confident distinctive-room Low
(dismiss) — it returns **`visual_inconclusive`** (`scripts/dedup_engine.py:1357-1360`).
For two generic co-located Czech houses this "inconclusive" outcome is the
*systematically likely* result, not an anomaly. The engine then **correctly
honors rule 15** (proximity never auto-merges): it does **not** merge — it
enqueues the pair as an operator candidate at a **fixed** confidence 0.6
(a sentinel, not a similarity score; `scripts/dedup_engine.py:2115-2116`).

**3. The human is handed the machine's failure with less information than the
machine had.** The operator merge path bypasses `classify_pair` entirely (code
comment, `toolkit/property_identity.py:89-90`); the only backend guards at the
merge chokepoint are survivor≠retired, both-active, `category_type` equality, and
`category_main_compatible` (`toolkit/property_identity.py:63-100`) — a
`dum/prodej ↔ dum/prodej` pair passes every one. And the /dedup review card
**omits the disambiguating fields**: it shows price, area, disposition, district,
street, floor + photos, but **not** description, portal URL/slug, `building_type`,
`condition`, or `estate_area` (`api/property_dedup.py:156-173`,
`frontend/.../dedupDiff.ts:325-374`) — i.e. exactly the "different village /
new-build vs reconstruction" content that reveals the non-match. It even frames
the two as same-location (identical lat/lng), because the coordinate is a town
pin. Price (34%) and area (17 m²) do exceed the card's ±2% / ±2 m² tolerances and
would render a red ✗ mismatch mark — a warning, never a block. So the operator is
asked to succeed at exactly the task the calibrated model just failed, with less
structured information.

**Rule 15 held.** The engine did not auto-merge on proximity; it deferred to a
human. The gap is that the **operator surface has no equivalent of the engine's
own guards** — no dissimilarity signal, no precision awareness, no disambiguating
fields — so the human is a weaker gate than the machine that escalated to them.

## Root cause of Defect B — post-commit read of a field the merge just mutated

`merge_cluster` (and `merge_candidate` / `merge_property_set`) call
`_record_operator_decision` **after** `merge_properties` has committed
(`api/property_dedup.py:970-976`, etc.). That recorder runs
`SELECT ... repr_listing_id ... WHERE id IN (survivor, retired)`
(`api/property_dedup.py:114-152`) to build the audit label — but `merge_properties`
just re-pointed the retired listings onto the survivor and ran `recompute_one`
**on the survivor only** (`toolkit/property_identity.py:172`), which can change the
survivor's `repr_listing_id` to the just-absorbed listing, while the retired
property's `repr_listing_id` is never cleared. When the freshly-scraped
`-238768` won the survivor's recompute tie-break, both sides read back the same
id. Classic read-after-write-invalidates-it (TOCTOU). The same file already names
this exact hazard elsewhere — a feedback join deliberately keys on the immutable
property pair "unlike the drifting repr listing" (`api/property_dedup.py:404-406`).
The engine's own self-paired rows (7.7% of phash merges etc.) are a *different,
largely-legacy* writer but share the same underlying cause (survivor-only
recompute leaving a stale `repr_listing_id`).

## Scale — honest bounds (exposure ≠ realized harm)

The raw numbers look alarming but are the wrong denominators; the verification
drill-down (its own prod queries) corrected an initial over-statement:

- **Exposure, not harm:** 51,638 coordinates each carry ≥2 distinct active
  houses/land/commercial properties (worst pin: 271); 30,835 geo candidates
  generated all-time. These measure the *fuel* the candidate factory draws on —
  the properties are still correctly **separate**.
- **The funnel attenuates ~4 orders of magnitude:** 51,638 pins → 30,835
  candidates → 1,061 geo merges → **663 auto** (all required a real
  image/attr/visual-High signal per rule 15 — a stale `engine_decision` column
  made these *look* like inconclusive auto-merges, but their actual
  `property_merge_events.reason` is `visual_match` 153 / `attr_exact` 57 /
  `phash` 50 / `cosine` 1 — **not** rule-15 violations) → **~398 operator-approved
  `visual_inconclusive` geo merges** = the *entire* population where
  town-pin-collapse can occur.
- **Realized false merges are low single digits.** Of 81 active
  geo-operator-`visual_inconclusive` **house** survivors, **77 are legitimate
  cross-portal merges** (same street-less rural house genuinely listed on 2–4
  portals, sharing a town pin, agreeing on price/area) — the geo lane doing its
  intended job. Running the tightening query (per survivor: distinct source-URL
  slug tails, sources, price/area spread) leaves **exactly one** house with the
  true false-merge signature — **374331, the reported case** — plus a handful of
  land/`pozemek` suspects that are mostly per-m²-vs-total price-unit contamination
  or multi-parcel developments, not house-centroid-collapse. Confirmed floor
  ~1–3; credible central estimate low-single-digits to ~10–15 once
  metadata-invisible "same-price twins" and retired survivors are allowed for.
  **"Dozens-to-hundreds" is refuted.**

**Data-quality framing:** this is therefore a **prevention + UX + integrity**
fix, not a cleanup. The realized damage is tiny; but the mechanism is **live**
(the candidate factory runs every cycle, the operator card still omits the
disambiguating fields, no precision signal exists), so the forward rate of new
false merges is nonzero and grows with inventory — and each one directly
contradicts the very metric the dedup program exists to improve (perceived
duplicate rate). The metadata-invisible twin (two different houses on one town
pin with coincidentally similar price/area) is the mechanism's easiest-to-produce
and hardest-to-detect output and is fundamentally un-catchable by metadata — only
a fixed human surface (Defect-A Layer 2) can catch it.

## Proposed architecture

The verification's decisive finding constrains the whole design: **the 77 good
pairs and the 1 bad pair are all low-precision town-pin pairs, so coordinate
precision cannot be a discriminator** — it only tells you proximity carries zero
positive evidence, shifting the burden onto *corroboration* (for a merge) or
*contradiction* (for a defer). Any change that gates on precision *alone* either
kills the 77 legitimate merges or does nothing. Concretely:

### Defect A — fix the false-merge mechanism WITHOUT harming cross-portal recall

**A2 (UX) — fix the operator review card. Ship first; zero recall cost.**
Surface the omitted disambiguating fields (description snippet, portal URL/slug,
`building_type`, `condition`, `estate_area`) and warn when the shared coordinate
is a **town pin** ("⚠ town-level location, not a precise address — proximity here
is not evidence"). This is the *only* layer that can catch the metadata-invisible
twin, it cannot lose a merge (it only adds information), and it directly addresses
the realized failure (a blind approval). Highest leverage, lowest risk.

**A0 (substrate, not a gate) — a first-class `listings.coord_precision` signal.**
Add a refetch-surviving, SQL-visible column (sibling to `street_source`
mig 263 / `geo_cell_key` mig 276; derived portal-uniformly at the single `geom`
write seam). It reuses **already-calibrated** inputs: the distinct-property
pin-share count (the `HAVING count(*) >= 4` town-centroid detector already in
`scripts/backfill_address_point_streets.py:74,81-89`), street/house-number
presence, distance from `geom` to obec/ku centroid, plus portal-native signals
where present (sreality `raw_json` `locality.accuracy = 'not_address'`; the
geocoder confidence tier). **Do not use it as an eligibility filter**
("make low-precision coords ineligible to form geo candidates" is a **recall
catastrophe** — the 77 legitimate merges *are* low-precision town-pin pairs).
Its two jobs are: (a) drive the A2 town-pin warning, and (b) be the precondition
for A1.

**A1 (machine guard) — defer, never dismiss, on a low-precision + contradiction
conjunction.** This is what separates the agreeing-77 from the contradicting-1.
When a geo pair is **low-precision** AND carries a **genuine contradiction** —
two *positively different* village tokens parsed from source-URL slug/description
(fire only on two-present-and-different, never on absence, and never dismiss:
"Vlčí Pole within Dolní Bousov" could be granularity, not conflict), or a
**conjunction** of large area gap AND price gap well above cross-portal drift
(≈ ≥25–30%) — route it to a reversible, **observable DEFER** (a counter/bucket
the /dedup dashboard shows), not the operator queue and not an auto-dismiss.
Rationale: the codebase deliberately never dismisses on price (cross-portal drift,
stale/reduced prices are legitimate), so this must be a conjunction and a defer,
preserving recall on the 77. This is a *mitigation* of the mechanism, correctly
placed downstream of A0.

Explicitly **rejected:** making low-precision coordinates ineligible for geo
blocking (kills the 77); deferring *all* low-precision inconclusive pairs (also
kills the 77 — most of them reach forensic, return inconclusive, and are
correctly approved). Only *contradiction* discriminates, which is why A1 is
narrow.

### Defect B — fix the audit self-pairing at the chokepoint, then constrain

**B1 — write the audit "merged" row from inside `merge_properties`, sourced from
the `FOR UPDATE` lock it already holds before mutating** (`toolkit/property_identity.py:69-74`),
instead of re-querying `repr_listing_id` post-commit in three separate callers.
One tested chokepoint, immune to the recompute drift, fixes both the operator
path and the engine path. Delete the now-redundant `_record_operator_decision(...,
"merged", ...)` calls and the engine's pre-merge `_audit(..., "merged", ...)`
calls; keep the *dismissed*-outcome writers untouched (no mutation, no bug, 0%
corrupted historically).

**B2 — distinctness CHECK constraints (defense in depth; exact DDL verified
against live data).** Ordering (`left < right`) is **wrong** for these tables
(20 `property_merge_events` and 43,615 `dedup_pair_audit` rows legitimately have
left/survivor > right/retired) — the correct minimal invariant is *inequality*:

```sql
-- 0 live violations → validate immediately:
ALTER TABLE property_merge_events
  ADD CONSTRAINT property_merge_events_distinct
  CHECK (survivor_property_id <> retired_property_id) NOT VALID;
ALTER TABLE property_merge_events VALIDATE CONSTRAINT property_merge_events_distinct;

ALTER TABLE dedup_pair_audit
  ADD CONSTRAINT dedup_pair_audit_distinct_property
  CHECK (left_property_id <> right_property_id) NOT VALID;
ALTER TABLE dedup_pair_audit VALIDATE CONSTRAINT dedup_pair_audit_distinct_property;

-- 4,539 pre-existing legacy self-paired listing rows → NOT VALID only
-- (still enforced on every FUTURE write); ship AFTER B1 or new inserts break:
ALTER TABLE dedup_pair_audit
  ADD CONSTRAINT dedup_pair_audit_distinct_listing
  CHECK (left_sreality_id <> right_sreality_id) NOT VALID;
```
(`<>` yields UNKNOWN on a NULL side and a CHECK passes on UNKNOWN, so future
property-only audit rows with NULL listing ids remain legal — correct.)

### Remediation

**Unmerge the confirmed false merge** (`374331` / group `3badb2e5-…`) via
`unmerge_group` (reversible; verify operator-state re-split). The small tail of
other suspects (the `pozemek`/land price-unit cases) should be operator-reviewed
individually, not bulk-unmerged — several are legitimate.

## Assumptions validated / corrected (do not rebuild on the wrong ones)

- **"No property was ever wrongly merged" (my own first-pass headline) — REFUTED.**
  It only tested self-merge (`survivor==retired`, 0), which cannot see a merge of
  two distinct properties. The reported merge *is* a genuine false merge.
- **"The coordinate is the trustworthy anchor (~95% coverage, straight from each
  portal's GPS)" (`.claude/skills/database`) — FALSE for street-less HTML-portal
  listings.** For these the coordinate is a **town-level geocode shared by many
  distinct properties**, and nothing downstream can tell it from a precise fix.
  This is the root of Defect A and should be qualified in that skill.
- **"261 inconclusive pairs were auto-merged (a rule-15 violation)" — REFUTED.**
  `engine_decision` is a stale last-decision marker; the actual
  `property_merge_events.reason` was a positive signal every time. Rule 15 holds.
- **"Dozens-to-hundreds of false merges" — REFUTED / overstated ~10×.** The
  312/43/51,638 figures are exposure and contamination pools (cross-portal
  `building_type` taxonomy noise, per-m²-vs-total price-unit contamination on
  land, price drops over time); realized house false merges are low single digits.
- **Rule 15 (proximity never auto-merges) — CONFIRMED honored by the engine.**
  The false merge entered through the **operator** surface, which has no
  equivalent guard — that is the gap, not a rule-15 breach.
- **Coordinate precision as a *discriminator* — INVALID.** Good and bad pairs are
  both low-precision; precision is substrate, only *contradiction* discriminates.

## Non-goals

- **Not** making low-precision coordinates ineligible for geo candidacy (recall
  catastrophe — see A0/A1).
- **Not** dismissing on price or area alone (legitimate cross-portal drift; the
  engine deliberately never does).
- **Not** collapsing `property_merge_events` and `dedup_pair_audit` into one
  table (different purposes: reversibility ledger vs. decision/evidence log with
  dismissals; no necessity).
- **Not** bundling Defect A and Defect B into one PR — they are orthogonal
  (merge-correctness vs. telemetry) and independently shippable.

## Rollout (one PR = one purpose, sequenced by risk/leverage)

1. **A2 (operator card) + unmerge `374331`.** Immediate, zero recall risk,
   addresses the realized failure. Ship first.
2. **B1 (chokepoint audit writer) then B2 (constraints).** Separate track;
   writer before the `NOT VALID` listing constraint. Add the
   `survivor==retired` test (`tests/test_property_identity.py` has none today) and
   a guardrail test pinning the chokepoint audit INSERT (mirror
   `tests/test_browse_read_path_guardrail.py`).
3. **A0 (`coord_precision` substrate) + backfill** from already-stored data
   (no re-fetch — mirror `backfill_address_point_streets.py`), wired into the A2
   warning.
4. **A1 (defer-on-contradiction)**, narrowed and observable, once A0 exists.

## Testing / verification

- **A1/A0:** unit-test that a low-precision + contradiction geo pair DEFERS
  (lands in the observable bucket, not the operator queue) while a low-precision
  *agreeing* cross-portal pair still reaches the queue/merge (the 77-recall guard);
  regression-fixture the `-238768` pair.
- **B1:** integration test — seed two active properties with distinct
  `repr_listing_id`s, call `merge_properties`, assert the `dedup_pair_audit` row
  has `left_sreality_id <> right_sreality_id` matching the pre-merge repr ids
  captured *before* the call.
- **A2:** manual — the reported pair's review card now shows the two village names
  / slugs and a town-pin warning; verify a second operator would catch it.

## Side finding — unrelated, likely urgent

`migrations/` has **two files numbered 285**: `285_enable_nonbyt_free_arms.sql`
(committed, applied — commit `8513540`) and `285_phase0_anon_hardening.sql`
(untracked in this working tree). Per CLAUDE.md rule #1 migration numbers are
never reused — the anon-hardening file needs renumbering to the next free number
(reconcile against prod's applied list — memory notes migs 286–295 and 298 live —
before picking) before it is committed or applied. Not touching it here (it is a
concurrent session's in-progress public-release work), flagging so it isn't
applied collision-first.
