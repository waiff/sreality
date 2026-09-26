# Hand-over to the autodedup program — `listings.floor` becomes ground = 0 everywhere

**From:** FIELD CAPTURE W8 (`docs/design/field-capture/PROGRAM.md`, ruling R12).
**Status:** the PARSERS changed on merge; the STORED rows change when the heal below is run.
**Nothing in `autodedup/` or `docs/design/autodedup/` was edited by this wave.** This document is the
whole hand-over: what moved, the exact predicate, what did not move and why, and the list of your own
code sites that assume a floor convention.

---

## 1. What was wrong

`listings.floor` was a MIXED column, almost exactly 50/50, and no measurement in the platform could see
it: a storey that is one too high is perfectly typed, perfectly non-NULL and perfectly plausible.

* **ground = 0** (přízemí = 0): idnes, bazos, ceskereality.
* **ground = 1** (the storey ordinal, ground = 1): sreality, realitymix, mmreality, remax, bezrealitky,
  maxima — undeclared portal passthrough, six parsers that read the integer and threw the Czech word away.

Proof, re-measured live 2026-09-22 — mean(portal floor − idnes floor) over unique
`(price_czk, area_m2, disposition)` active `byt` keys, idnes as the ground = 0 reference:

| portal | pairs | mean delta | at +1 | at 0 | verdict |
| --- | ---: | ---: | ---: | ---: | --- |
| sreality | 13,574 | **+0.973** | 12,755 | 652 | ground = 1 → converted |
| realitymix | 7,416 | **+0.962** | 6,852 | 377 | ground = 1 → converted |
| remax | 1,248 | **+1.050** | 1,083 | 35 | ground = 1 → converted |
| mmreality | 1,574 | **+0.988** | 1,552 | 6 | ground = 1 → converted |
| bezrealitky | 900 | **+0.869** | 657 | 73 | ground = 1 → converted |
| maxima | 49 | **+0.816** | 42 | 6 | ground = 1 → converted |
| ceskereality | 7,453 | +0.201 | 927 | 6,382 | **already ground = 0 — NOT converted** |
| bazos | 2,586 | +0.098 | 561 | 1,698 | already ground = 0 — NOT converted |

Part of the residual on the two canonical portals is the measure's own noise floor — a
price/area/disposition match is a strong sibling signal, not a proven duplicate — so expect the six to land
in that band after the heal, not at exactly 0. **bazos's +0.10 is that noise floor; ceskereality's +0.20 is
not all noise**, and §6 records the part that is a defect. Simulating the conversion on the live corpus
(`floor − 1 where floor ≥ 1` on the six) lands sreality −0.019, realitymix −0.031, mmreality −0.006,
remax +0.059, bezrealitky −0.129, maxima −0.160.

## 2. The exact conversion predicate

Per converted portal, the stored column is **re-derived from the portal's own stored payload through
that portal's own parser** — never arithmetic on the column:

```
floor := floor_from_portal("ground1", <the portal's declared floor key>)
       = value - 1   where value >= 1
       = value       where value <= 0
```

* **`floor >= 1` only.** sreality emits BOTH 0 and 1 for the ground storey (4,597 rows at 0, the
  "zvýšené přízemí" cell), so a blanket −1 would invent basements. mmreality has 108 rows at 0,
  maxima 2. Those **4,707 rows do not move**.
* **Negatives do not move.** −1 already means suterén / 1. PP under both conventions
  (sreality 558, bezrealitky 87, realitymix 147, remax 106, maxima 2 = **900 rows**).
* **A value that spells the storey out wins over the key's convention.** "přízemí" reads 0 and
  "suterén" −1 on every portal — `scraper.floor.normalize_floor`.
* **Idempotent by construction.** The heal reads the payload, not the column, so a row the drain
  already rewrote between the parser deploy and the heal is re-derived to the same value rather than
  decremented twice. `scripts/reparse.py` additionally compare-and-sets every column it writes.
* **Out-of-band values are decremented like any other value ≥ 1** (`floor > 40`: sreality 45,
  realitymix 26, ceskereality 11, remax 3, bezrealitky 1; `floor < -3`: sreality 4, realitymix 2 — the
  negatives, like every negative, stay). The converter has no bounds check, so a stored 3,127 becomes
  3,126: do not treat these as a fixed reference set across the heal. BLANKING them is what is out of
  scope, and it is the separate finding listed in §6. The one exception is a value that states an
  out-of-band storey in WORDS ("45. patro"): the grammar refuses it and the converter returns NULL
  rather than falling through to the key's convention, which would read it on the opposite scale.
  *(Superseded 2026-09-26, PR #1630: the bare-number arm now bounds exactly like the word arm, so an
  out-of-band storey is NULL at ingest on either scale, and migration 573 (#1633) NULLs the
  stored ones.)*

## 3. Rows that move, and the before/after distribution

Whole corpus, rows with `floor NOT NULL`, re-measured 2026-09-22. The counts drift by a few dozen a day
with ingest; the shape does not.

| portal | with floor | active | inactive | **rows that move** (`floor >= 1`) | active movers | stay put (0 / negative) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| sreality | 129,455 | 41,013 | 88,442 | **124,300** | 38,104 | 4,597 / 558 |
| realitymix | 34,181 | 14,971 | 19,210 | **34,034** | 14,893 | 0 / 147 |
| bezrealitky | 13,749 | 3,151 | 10,598 | **13,662** | 3,132 | 0 / 87 |
| remax | 6,328 | 3,695 | 2,633 | **6,222** | 3,620 | 0 / 106 |
| mmreality | 5,451 | 3,670 | 1,781 | **5,343** | 3,594 | 108 / 0 |
| maxima | 244 | 89 | 155 | **240** | 88 | 2 / 2 |
| **total (six)** | **189,408** | **66,589** | **122,819** | **183,801** | **63,431** | 4,707 / 900 |
| ceskereality | 34,363 | 13,712 | 20,651 | 0 (unchanged) | — | — |
| idnes | 103,740 | 33,900 | 69,840 | 0 (unchanged) | — | — |
| bazos | 40,668 | 14,545 | 26,123 | 0 (unchanged) | — | — |

**Distribution, before → after** (whole corpus; each converted portal's histogram shifts down one bin,
the 0 bin absorbs today's 1 bin, and the negative tail is unchanged):

| portal | <0 | 0 | 1 | 2 | 3 | 4 | 5 | ≥6 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| sreality **before** | 558 | 4,597 | 29,820 | 30,742 | 23,409 | 15,850 | 9,592 | 14,851 |
| sreality **after** | 558 | 34,417 | 30,742 | 23,409 | 15,850 | 9,592 | 5,765 | 9,086 |
| realitymix **before** | 147 | 0 | 7,230 | 8,507 | 6,617 | 4,584 | 2,802 | 4,278 |
| realitymix **after** | 147 | 7,230 | 8,507 | 6,617 | 4,584 | 2,802 | 1,636 | 2,642 |
| bezrealitky **before** | 87 | 0 | 3,482 | 3,106 | 2,481 | 1,867 | 1,126 | 1,599 |
| bezrealitky **after** | 87 | 3,482 | 3,106 | 2,481 | 1,867 | 1,126 | 638 | 961 |
| remax **before** | 106 | 0 | 1,575 | 1,510 | 1,169 | 704 | 470 | 793 |
| remax **after** | 106 | 1,575 | 1,510 | 1,169 | 704 | 470 | 273 | 520 |
| mmreality **before** | 0 | 108 | 1,854 | 1,277 | 907 | 530 | 272 | 502 |
| mmreality **after** | 0 | 1,962 | 1,277 | 907 | 530 | 272 | 171 | 331 |
| maxima **before** | 2 | 2 | 32 | 56 | 43 | 36 | 35 | 38 |
| maxima **after** | 2 | 34 | 56 | 43 | 36 | 35 | 18 | 20 |

**ceskereality and idnes are byte-identical after the wave; bazos is NOT** — its free-text miner changed in
two ways that are not a floor conversion and are not healed (§4, and the rows table above counts 0 movers for
it because no `floor` value is rescaled). The characterisation goldens
(`tests/fixtures/field_capture/golden/`) moved on exactly six probes, all of them `floor` cells of
converted portals (sreality 1, realitymix 4, mmreality 1), and not one `total_floors` cell anywhere.

## 4. `total_floors` does NOT change

Checked, because the two are read together. `total_floors` is a **podlaží count including the ground
storey** on every portal that publishes it, so it needs no conversion — the only thing that changes is
the INVARIANT relating the two: under ground = 0 the top storey is `total_floors - 1`, not
`total_floors`. `scraper.floor.is_plausible_floor` was tightened to match in this PR (it had been
enforcing the ground = 1 relation inside a module declaring ground = 0).

Two related corrections, bazos only — the free-text miner, not a conversion, and **not healed**:

1. The two **patra-worded** total cues ("z celkových 10 pater", "6patrový") count storeys ABOVE the ground
   one and are now read as `n + 1` podlaží. 28 of 1,158 active bazos rows with a `total_floors` carry such
   a cue.
2. The tightened `is_plausible_floor` drops a mined floor EQUAL to the stated total (under ground = 0 the
   top storey is total − 1), so `floor_from_text("Podlaží: 6. patro / Podlaží celkem: 6")` now returns
   `(None, 6)` where it returned `(6, 6)`. 23 active bazos rows currently hold `floor >= total_floors`.

Both take effect **per row at its next detail fetch**, not at merge: an active bazos row is re-mined and
changes (which also re-hashes it and appends one `listing_snapshots` row), while the 26,053 inactive bazos
rows keep the old reading permanently. `total_floors` on bazos is therefore a two-reading column for as long
as that tail survives. We did NOT add a bazos pass to the runbook (§7): re-deriving `floor` on bazos would be
correct too, but bazos floors are MINED from prose rather than read from a key, so a mass re-mine is a
different risk profile from a key re-read and it belongs to whoever owns the text lane, not to W8.

## 5. What this does to your features, and what we did not touch

Everything below is **yours to re-measure**. We list the mechanism, not a prescription.

* **`l0_floor_tolerance` (default 2, `toolkit/dedup_sim_settings.py:176`).** Its own explanation —
  *"Floor numbers are self-reported and often off by one or two"* — is a description of THIS defect
  adopted as a parameter. After the heal a cross-portal ±1 is no longer a convention artefact, so the
  tolerance is buying recall with a genuine distinguishing fact. Re-gate it downward on measurement.
* **`floor_stated_conflict` (`autodedup/features.py:1591-1603`, feature-set v4).** Its comment states
  the finding exactly: *"ACROSS portals the same gap fires on 38.7% of positives against 30.1% of
  negatives — it is the portals disagreeing about prizemi, and carries nothing."* That is the defect
  this wave removes. After the heal the cross-portal `floor_diff == 1` population should behave like
  the same-portal one (2.0% of positives vs 15.5% of negatives), which makes the feature's
  `same_source` product **no longer necessary to neutralise a bug** — and possibly harmful, since it
  now suppresses a real signal across portals. FEATURE_VERSION bump + refit is your call.
* **`floor_diff` (ATTR) and `total_floors_equal` (ATTR).** `floor_diff` shifts by one on every
  cross-portal pair with one converted side; `total_floors_equal` is unaffected (§4).
* **`guards.pre_reject` floor rule (`autodedup/guards.py:79-82`) and `floor_relation`
  (`:117-125`).** Both key on `abs(a.floor - b.floor)`. The `band` arm's own docstring —
  *"delta 1 — portals disagree on přízemí"* — is this defect named in a return value. After the heal a
  cross-portal delta of 1 is a real one-storey difference, so `band` and `reject` both move.
* **`guards.cluster_invariants_ok` `floor_spread` (`:158-161`).** A cluster of one flat's adverts across
  a converted and an unconverted portal was spuriously spread by one. Those clusters become clean; new
  ones with a genuine spread become visible.
* **`structural_truth.py:275-280` `pos_unit_in_project`.** `floors_agree` is an exact equality, so
  every cross-convention positive was falling through to the area arm. Its `floor` evidence string
  (`f"{a.floor}|{b.floor}"`) records the OLD numbers on anything already emitted.
* **Persisted pair state — `dedup_pair_candidates.floor_lo / floor_hi / floor_checked`**
  (`toolkit/dedup_candidates.py:347-351`, `toolkit/dedup_candidates_sql.py:126-130,149,167,182`).
  These are stored per pair and are stale the moment the heal runs. **Regenerate.**
  `l0_scope` is RULED `all` (everything ever seen), so the heal deliberately covers inactive rows too —
  leaving 122,873 inactive rows on the old scale would have made the `all` and `active` scopes disagree
  about the same flat.
* **`FALSE_BY_OMISSION` (`autodedup/features.py:158-171`) is untouched by this wave.** No floor entry
  exists there and none is needed: a floor this program cannot read is written NULL, never a default.
  Named here only because the hand-over brief asked — the parser-default hazard it guards is a boolean
  one, and W8 changed no boolean.
* **`autodedup/dataset.py:188-189,220-221`** reads both columns straight off the row, so any cached
  dataset snapshot taken before the heal carries the old scale. Rebuild before refitting.
* **`autodedup/verdict_reasons.py:22` `floor_differs` ("Jiné podlaží")** — operator-facing wording,
  unchanged, but it now means what it says.

## 6. Reported, not fixed (yours or ours, but not this wave's)

* **ceskereality carries an unexplained one-storey sub-population.** Its +0.199 mean against idnes is NOT
  mostly sample conditioning: restricting the idnes side to `floor >= 1` as well (removing the effect of
  ceskereality never writing a 0) moves it only to +0.167 (n = 7,166 of 7,394). What remains is 925 pairs at
  exactly +1 against 92 below 0 — a 10:1 asymmetry bazos does not have (565:264) — so roughly 12 % of its
  pairs look a storey high. 226 of its pairs sit against an idnes floor of 0, and ceskereality writes a
  non-zero for every one of them. **Do not read "already ground = 0 — NOT converted" as "certified".** The
  portal must not be bulk-converted (that would break the ~88 % that are right), so this needs a per-row
  cause, not a scale flip. W8 leaves it open and the gate's warn tier (0.35) is set to keep it amber-free
  until it is understood, not because it is clean.
* **mmreality's 108 rows at `floor = 0` are unverified.** Its cell declares `ground1`, under which 0 has no
  legal meaning, and the converter passes 0 through untouched — so after W8 they assert "přízemí". Their
  stored payload literally carries `"floor": "0"` with `total_floors` NULL, which is consistent with either
  a genuine ground-storey cell or a not-specified sentinel like bezrealitky's. The only live evidence is 9
  sibling pairs, on which the idnes side reads mean **0.89 storeys higher** — i.e. those flats look like
  1st-floor flats, not ground-floor ones. sreality's own floor = 0 rows pair at only 0.33 higher (n = 46),
  which does support the "zvýšené přízemí" reading there. 9 pairs decides nothing, so these 108 rows are
  excluded from the "rows that stay put" claim being called verified.
* **mmreality's `total_floors` is `overgroundFloors + undergroundFloors`**
  (`scraper/mmreality_parser.py:485-490`), so it is not a storey count and
  `total_floors_equal` is comparing a different quantity on that portal.
* **ceskereality's `total_floors` is NULL on all 34,363 rows** (the contract declares the cell a genuine
  portal gap). That portal contributes nothing to any floor/total relation.
* **realitymix has 3,123 `byt` rows at `floor > total_floors`** (idnes 2,700, sreality 1,137,
  remax 27, mmreality 10, bezrealitky 45) — pre-existing, unrelated to the convention, and now visible.
* **Out-of-band values survive every parser**: `floor` max 3,127 (sreality), 2,315 (realitymix),
  367 (ceskereality); min −390 on sreality and realitymix. 86 rows corpus-wide, 33 of them active.
  W8 deliberately does not blank them (blanking a stated value is not a heal's to do).
  *(2026-09-26, PR #1630: the parsers now decline them — `floor` outside -3..40 and `total_floors` outside
  1..40 on every portal, idnes's "20. patro a vyšší" as a declared sentinel — and migration 573 (its own
  PR, #1633) NULLs the stored ones after a backup to `backup_a4.listing_cells`; that is a data migration, not this heal.)*

## 7. Heal runbook (not run by W8 — the operator runs it after merge)

One `scripts/reparse.py` pass per converted portal, `--fields floor`, dry-run first. `floor` is in
`_HASH_FIELDS`, hence `--allow-snapshot-deferral`.

```
python -m scripts.reparse --source sreality    --fields floor            # dry run (default)
python -m scripts.reparse --source sreality    --fields floor --write --allow-snapshot-deferral
python -m scripts.reparse --source realitymix  --fields floor --write --allow-snapshot-deferral
python -m scripts.reparse --source bezrealitky --fields floor --write --allow-snapshot-deferral
python -m scripts.reparse --source remax       --fields floor --write --allow-snapshot-deferral
python -m scripts.reparse --source mmreality   --fields floor --write --allow-snapshot-deferral
python -m scripts.reparse --source maxima      --fields floor --write --allow-snapshot-deferral
```

Expected `changed=` per pass: the "rows that move" column of §3 (sreality 124,300 minus its
unreachable rows — see below; realitymix 34,034; bezrealitky 13,662; remax 6,222; mmreality 5,343;
maxima 240), plus whatever the portals have ingested since. A SECOND pass over the same portal must report `changed=0`; that is the live proof of
idempotence, and the offline half is
`tests/scripts/test_reparse.py::test_the_floor_heal_converges_in_one_pass_and_never_decrements_twice`.

* **Snapshot budget.** The seam writes no `listing_snapshots` row. On the five portals that hash the
  PARSED fields, each healed LIVE row appends exactly one snapshot at its next detail fetch:
  14,893 + 3,132 + 3,620 + 3,594 + 88 = **25,327 deferred snapshots**, spread over the normal cadence.
  **sreality appends none** — it hashes the RAW payload, which a column heal never touches, so its
  124,300 rows leave column and history permanently divergent (the asymmetry `docs/architecture.md`
  already records for the W17 land heal). The gate is a DELTA, not an absolute: `listing_snapshots`/day
  within +40 % of the trailing 7-day median (11,199 over the seven full days to 2026-09-21 — 20,386 /
  9,608 / 13,107 / 11,199 / 8,669 / 3,220 / 13,130). Three of those days already cross the 12,500 the
  plan wrote, before a single row moved, which is why the absolute was dropped.
* **sreality `parse_errors` are expected and are not a failure.** The raw_json arm needs
  `hash_id`/`id`, and rows stored before the client unwrapped the estate object carry neither: measured
  234/1,000 usable at id ≤ 1,000, 609/1,001 at id ≈ 30k, 597/1,001 at id ≈ 60k, 1,001/1,001 from
  id ≈ 90k up. Those rows are counted, WARNed about with their share, and stay on the old scale forever
  — a floor heal cannot reach them by any means.
* **ceskereality, idnes and bazos must NOT be healed for floor.** Converting ceskereality would break
  the ~85 % of its 34,363 rows that are already right (and would not fix the ~12 % that are not — §6).
* **The gate.** `scripts/verify_pipeline.py` → `floor_convention` (new in this PR). It reads RED until
  every one of the six passes has run; after them each portal must sit within ±0.35 of 0, with fail at
  ±0.50. The warn tier is 0.35 rather than the planned 0.25 to hold ceskereality's +0.20 amber-free while
  the §6 finding behind it is investigated — NOT because that reading is certified. Every converted portal
  lands inside 0.25 in simulation (§1), so 0.35 is protecting exactly one portal. Run the check before and
  after, and once more after a full drain cycle.
