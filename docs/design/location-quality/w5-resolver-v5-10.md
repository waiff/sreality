# W5: resolver v5.10. One ladder, one matcher, one grain, one truth (as built, 2026-10-31)

**Status: built 2026-10-31 (#2251) on W3's branch; merges after W3.** The design below is what was built, except where "As built" (end of file) says otherwise; §§ 0, 4.1 and 8 hold the measurements the acceptance reads against. Line numbers marked "W3" are on W3's branch before the build.

## Summary

I read production between 16:15Z and 16:45Z through `execute_sql` on erlvtprrmrylhznfyaih (SELECT only, aggregates). The code was read at `main` 51bfa8196 and at W3's branch `origin/feature/location-w3-one-claim-per-fact`.

**The rule (ruling Q3, one truth).** In `bind.place` one boolean replaces v5.9's declared-blurred exception:

`may_place = pin is not None and (not words_bound or (pin_inside_bound_obec is True and not declared.area))`

- `words_bound` means the binding is bound and not pin-derived.
- It is read at the pin rung and in the street branch.
- A pin outside the town the words bound is never the position, whatever its label. FILL stands the row at the bound entity's own point, with that entity's reach as the radius.
- bazos's `approximate_location` (the "Přibližná lokalita" label every one of its 52,185 active pins carries) becomes the one area-marker label, so a bazos pin never places a row the words bound.
- With no words (R7/R8) the pin still names the town and stays the position, as today.
- CHECK keeps the `pin_outside_obec` flag and loses its coarsening. The grain becomes the row's finest key (`grain_of`), with one writer. CHECK no longer writes grain, and `grade()` / `Grade` go.
- Ruling Q3 removes the 250 m tolerance, so the planned registry distance read is **not** built.

**Quarter = část obce only (Q1/Q2).** One constant, `MATCH_LEVELS = ("cast_obce", "momc")`, replaces `PART_LEVELS`, the `"zsj"` literals and `_PART_OF_TOWN_LEVELS`. One function, `part_of`, decides what can be bound: a část is bound as itself, a KÚ as the část its name spells, and a MOMC as nothing. Part claims are matched like town names. A street keeps the claimed část where it has doors.

**Measured today.** Pins that are the position and lie outside the bound town: **1,353**.

| Portal | Pins outside |
|---|---|
| realitymix (undeclared) | 709 (≤ 110 after W4's re-mine) |
| idnes (undeclared) | 291 |
| sreality `gps` | 209 |
| ceskereality (undeclared) | 137 |
| mmreality (`accurate`) | 3 |
| bezrealitky | 1 |
| remax | 1 |
| maxima (`Point`) | 1 |

Other measures:
- **bazos** rows placed on the pin despite having words: **23,297** (18,071 at obec grain, 5,226 at quarter grain). Their radius goes from 1 km to the town's reach (median 5.3 km).
- **MOMC codes** in `cast_obce_kod`: **2,834**. Of these, 1,899 gain the část they claim; about 935 lose the quarter label.
- **Správní obvod (SO) ties:** 200 (bazos 41, sreality 159). **ZSJ:** 0.
- **remax "Praha N" rows:** **1,607**, all bound to Praha; 1,604 also claim a část.
- **Grain differs from the keys:** 736.
- **Praha rows with no quarter:** 9,308.

**After W5:**
- `{p}/outside` reaches 0 on every portal except about 10 register-point slivers (bazos 6, ceskereality 3, bezrealitky 1). These are door centroids, not pins.
- The words cannot judge about 300 ceskereality ads that have only a pin; their pin names their town.

**Autodedup gate.** Run W5's own `ids_sql` (§4), sliced to blocks that hold rulings and Praha cut by KÚ. The id-grain export block is **needed and is built inside this PR**: without it the about 5.4k Praha movers that gain a quarter (the rows whose candidate set shrinks) cannot be judged.

**Release.** `resolver:v5.10`. W2's reconcile sweeps the corpus on deploy. There is no migration and no hand step.

**Ledger** (measured, § 9): −715 / +712 lines including tests, docs and the id block, this file aside.

**Defaults taken, no ruling needed:**
- bazos keeps its existing label; no contract bump.
- Radii of pins kept inside their town stay until W6.
- The determinism fix (`ORDER BY` in the containing-obec query) is deferred: 0 of 6,553 sampled pins sit on a shared border, and the fix would force a 247-line re-render of the scorecard view.
- The register-point residual is named, not fixed.
- A MOMC name never maps to the část of the same name.

---

## 0. Baseline (production, 2026-10-31 16:15–16:45Z)

Context: the v5.9 sweep is still running (230,756 active rows at v5.8, 166,094 at v5.9). Every disputed row is already at v5.9. W4 has not been re-mined yet (realitymix@7 is the active contract).

| Question (active listings) | Measured |
|---|---|
| W1 `listing/{p}/outside` | realitymix 709, idnes 288, sreality 209, ceskereality 140, bazos 6, mmreality 3, bezrealitky 2, remax 1, maxima 1 (1,359) |
| W1 `served/{p}/outside` | realitymix 533, idnes 268, sreality 182, ceskereality 113, bazos 5, bezrealitky 2, remax 1, mmreality 1 (1,105) |
| W1 `listing/{p}/disputed/pin_outside_obec` | bazos 14,459, realitymix 1,005, idnes 344, sreality 270, ceskereality 158, mmreality 10, bezrealitky 4, maxima 2, remax 1 |
| Position outside the bound obec (strict pip test), by label and by what placed it | **the pin** (Q3's movers): sreality `gps` 209 (189 in another obec, 20 outside Czechia); idnes undeclared 291 (288 + 3 outside Czechia / country conflict); realitymix undeclared 710 (707 + 3 outside Czechia); ceskereality undeclared 137; mmreality `accurate` 3; bezrealitky 1; remax 1; maxima `Point` 1. **A register point**: bazos 6 (5 část door centroids, 1 street centroid), ceskereality 3, bezrealitky 1. No declared-blurred pin is the position outside its town (v5.9). |
| bazos rows placed on the pin, all with words | obec grain 18,071, quarter grain 5,226. Register-placed: obec 9,615, street 13,997, address 2,451, quarter 2,825. bazos rows with a pin and no words: **0**. Pins carrying `approximate_location`: 52,185 of 52,185. |
| Radius of those 18,075 bazos obec rows if FILL places them | town reach p10 2.7 km, p50 5.3 km, p90 10.9 km; all above 1 km; 658 in Praha (17.6 km) |
| `cast_obce_kod` holding a MOMC code | **2,834**: remax 2,045, sreality 467, bazos 148, idnes 68, realitymix 62, mmreality 26, bezrealitky 18. 2,829 at quarter grain, 5 at obec grain. 0 code collisions between část and MOMC codes. |
| Of those, the part claim names a část or KÚ in the town | 1,899 (remax 1,510, sreality 149, bazos 143, idnes 68, mmreality 26, bezrealitky 3). It names something else (a MOMC name) for 871; 64 rows have no part claim. |
| Quarter grain with no část code (správní obvod ties) | **200** (sreality 159, bazos 41) |
| ZSJ | 23,604 units in the mirror, 0 name-index entries: can never bind |
| remax with town "Praha N" | **1,607** of 9,675 active remax. All bound to Praha; 1,418 at quarter grain with a MOMC code; 1,604 also claim a část (Žižkov, Smíchov, Stodůlky …) |
| Grain differs from the finest key | **736**: 505 + 31 coarsened by CHECK (pin outside and pin is the position), 200 SO ties |
| Praha / Brno / Ostrava rows with no quarter | Praha 9,308 (street rows with a part claim 5,052; obec rows with a part claim 141; SO 200); Brno 3,463; Ostrava 2,187 |
| Street/address rows with no část | 18,724 |
| Quarter-grain rows bound to the eponymous main část | 38,095 of 79,354 (a guard: see §2) |
| Address-grain rows with no KÚ | 1 (19405830; its address point has no coordinates) |
| Active pins with no word claims (1/20 sample) | 15 of 19,926, all ceskereality (about 300 active) |
| Pins covered by two obce (1/50 sample) | 0 of 6,553 |

---

## 1. The rule change in `bind.place` and `check`

### 1.1 One truth (W3 bind.py:1037–1143; main bind.py:1101–1207)

```python
def place(binding, pin_claim, *, declared, pin_inside_bound_obec):
    pin = pin_claim.point if pin_claim is not None else None
    words = binding.bound and not binding.pin_derived
    may_place = pin is not None and (not words or (pin_inside_bound_obec is True and not declared.area))
    # address-point branch: UNCHANGED (the pin is still evidence: distance -> GRADE)
    # street branch:
    if pin is None or declared.blurred or off_street or not may_place:   # + `or not may_place`
        return Position(..., origin="street_point", pin_overridden=bool(off_street and not declared.blurred), ...)
    ...
    if may_place:                      # replaces `if declared.blurred and pin_inside_bound_obec is False`
        return Position(lat=pin[0], lon=pin[1], origin="portal_pin", blurred=declared.blurred, ...)
    return Position(lat=None, lon=None, origin="none")   # FILL places it (fill.position)
```

How each case comes out:

- **Words bound a town; the pin is inside it.** The pin refines, for every portal and every label. bazos is the exception: its area marker never places the row.
- **Words bound a town; the pin is outside it, outside Czechia, or the bind was region-only (R9) so there is no town.** The pin is never the position. FILL stands the row at the bound entity's point: the address point, else the street, else the část doors, else the obec, else the okres. The radius is the reach (v5.8).
- **No words (R7/R8), or nothing bound (foreign or undetermined).** The pin stays the position, which is the R7 fallback the ruling keeps.
- **Evidence uses stay as they are.** The address-point and street branches still compute `registry_pin_distance_m` and `pin_overridden` from the real pin. Nulling the pin there would let an address point bound from words grade `exact` against a pin lying outside the town.

`pin_inside_bound_obec` (W3 bind.py:1037) stays the one containment read that `place` and CHECK share, with no new registry read. It answers `None` for R7/R8 and for region-only binds, which is why `is True` is the test.

### 1.2 bazos: an area marker, not a portal name

- Add `AREA_MARKER_LABELS = frozenset({"approximate_location"})` next to `BLURRED_DECLARED_LABELS` (W3 bind.py:106). It is a subset of the blurred labels, so confidence caps and street precedence are unchanged.
- Add a property on `DeclaredPrecision` (W3 bind.py:1001): `area = self.label in AREA_MARKER_LABELS`.
- This is the label the bazos contract already emits (`contracts/portals/bazos.yaml:76-84`, `bzs.det.blur_hint`). bazos is its only emitter, and all 52,185 active bazos pins carry it.
- Default taken: Q3's "the bazos contract declares its pin an area marker" is read as the existing declaration, so there is no bazos@10 bump and no re-mine. Relabelling in the contract would cost a re-mine of every bazos page and a second shadow mode (`remine`), and would change no answer.
- Other blurred labels (sreality `street`/`ward`/`quarter`/`municipality`/`not_address`, idnes `no_exact_address`, mmreality `regional`, realitymix `estimated`, maxima `Circle`/`LineString`) still refine inside their town (Q3: "a pin inside it refines").

### 1.3 CHECK (W3 check.py)

- `pin_outside_obec` / `pin_outside_cz` / `country_conflict` are still recorded off the claim pin (W18). The flag is now always a statement about evidence that lost.
- Delete the coarsening at check.py:267-277 (`granularity=(rank.coarser_of(granularity, "obec") if pin_is_position else granularity)`). The pin can no longer be the position there.
- Also delete `pin_is_position` (:239), the `granularity` and `rank` parameters (:209, :212), `Verdict.granularity`, and docstring :10-22 (rewritten in 5 lines).

### 1.4 Grain = grain_of(keys) (§ W5 "CHECK stops writing grain")

New in grade.py, about 10 lines, the only grain writer:

```python
_GRAIN_KEYS = (("address_point","ruian_adm_kod"),("street","ulice_kod"),
               ("cast_obce_or_quarter","cast_obce_kod"),("obec","obec_kod"),("okres","okres_kod"),("kraj","kraj_kod"))
def grain_of(filled: Fill, verdict: Verdict) -> str:
    """The row's grain is its finest key (W5): one writer."""
    if verdict.country_status != "cz":
        return "country" if verdict.country_status == "foreign" and verdict.country_code else "unknown"
    return next((g for g, k in _GRAIN_KEYS if getattr(filled, k) is not None), "unknown")
```

core.py:

```python
granularity = step_grade.grain_of(filled, verdict)
match_confidence = step_grade.confidence(binding, position, blurred=declared.blurred)
uncertainty_radius_m = step_grade.radius_m(granularity, floor_m=position.extent_m)
```

Deleted along with it:
- `grade.grade()` and `types.Grade`. Their only callers are core.py:103 and one test helper.
- grade.py's 19-line "THE GRANULARITY IS THE BIND'S" docstring, :61-79.
- `Binding.granularity`. Its only downstream reader is grade.py:75; `_Candidate.granularity` stays for ranking.
- `GranularityRank.coarser_of`, whose only caller is check.py:275.

This agrees with today on every bind except the 736 rows of §0.

### 1.5 The 1 km constant on a kept pin

- v5.9 kept exact and undeclared pins outside the town at obec grain. CHECK coarsened them, and the radius fell to `RADIUS_M["obec"] = 1 km` with no floor, even 5–20 km from the town. That case no longer exists:
  - those 1,353 rows go to FILL's point with the entity's reach;
  - the 23,297 bazos rows on postal pins go from 1 km or 750 m to reach (median 5.3 km).
- Default taken, no ruling: a pin kept **inside** its town keeps the grain constant until W6's CE95 by class (principle 4). An interim floor would be a band that W6 deletes.

### 1.6 What is deleted (rule and special cases)

| Deleted | Where (W3 / main) |
|---|---|
| v5.9's declared-blurred exception | bind.py W3:1137-1138 / main:1201-1202 |
| Its ladder note and docstring paragraph | W3:1062-1063, 1083-1090 / main:1125-1127, 1147-1154 |
| Its prose | core.py:82-83; fill.py:18-28, 136; check.py:13-22; version.py note; architecture.md:5188-5225 |
| CHECK's coarsening, `pin_is_position`, `rank`, `granularity` params, `Verdict.granularity` | check.py:209, 212, 239, 267-277 |
| Second grain writer: `grade.grade`, `Grade`, `Binding.granularity`, `coarser_of`, `verdict.granularity or graded.granularity` | core.py:106 |
| The 1 km pin-outside-town case | via the above |
| The planned 250 m tolerance and its registry distance read (view, SQL, CI mini mirror, drain pre-answer) | never built (Q3) |
| MOMC / SO / ZSJ binding | §2 |

---

## 2. The level lists: one constant

```python
# composite.py:55 (replaces PART_LEVELS = ("cast_obce", "momc", "spravni_obvod"))
# W5 (ruling Q1): the levels BELOW the town a place name is matched at, finest first. Every match
# anchors its town; only a část obce is ever bound (`part_of`). MOMC stays a key: 1,607 remax ads write
# their town "Praha N". Správní obvod and ZSJ are no level (W7 retires them from the mirror).
MATCH_LEVELS: tuple[str, ...] = ("cast_obce", "momc")

def part_of(unit, town_code, registry) -> AdminUnit | None:
    """The část a match binds: a část itself; a KÚ the část of the same obec its qualifier-free name
    spells (Q2); a MOMC or the obec nothing."""
```

There is no second list of bindable levels: `part_of` is the one predicate. Its KÚ arm moves out of `bind.climbed_part`'s :846-853. `WHOLE_LEVELS = ("obec", *MATCH_LEVELS)`. `TOWN_NAME_LEVELS` (bind.py:78) is unchanged.

| Reader | Today | After |
|---|---|---|
| Part claim → R4 part (W3 bind.py:581-586) | `admin_units_by_name(key, levels=(*PART_LEVELS, "zsj"))`; the unit itself, MOMC/SO included | Match at `(*MATCH_LEVELS, "katastralni_uzemi")` (the town ladder's KÚ arm, Q2); keep units whose town (`obec_of`) is a constraining town; part = `part_of(unit, town)` |
| Part claim anchors the town (W3 :474) | `bind_towns(cast_obce_keys, PART_LEVELS)` | `bind_towns(cast_obce_keys, (*MATCH_LEVELS, "katastralni_uzemi"))`: a MOMC still anchors through PSČ or reach; SO goes (all 22 have MOMC twins) |
| `climbed_part` (W3 :840-858) | Accepts `("cast_obce", "momc")` | `part_of` plus the 1a rules (one part only; not the town's eponymous main part) |
| Composite whole string (composite.py:117-121) | Part = any non-obec unit | `part=part_of(unit, …)`: a MOMC line ("Praha-Řeporyje", "Brno-střed") gives the town only |
| Composite token anchor (composite.py:106-107) | `part = unit` if not obec | `part = unit` if it is a část |
| `_part_inside` (composite.py:158-178) | Loops all three levels | One level (`cast_obce`); the loop is deleted |
| `places_named` / `_parts_of_town` / `_one_part` (composite.py:248, 448-462) | `_PART_OF_TOWN_LEVELS`, level loop | `("obec", *MATCH_LEVELS)`; parts = části only; `_one_part` = "one of them"; `_PART_OF_TOWN_LEVELS` deleted |
| `core._is_place_name` (W3 core.py:151) | `("obec","cast_obce","momc","zsj")` | `("obec", *MATCH_LEVELS)` |
| bind.py :608-609 `cast_parts` level filter | — | Deleted (every part is a část) |
| bind.py :871-885 `_reconcile_parts` | "different levels not comparable" (the MOMC-over-část rule) | One peer test |
| fill.py:70 `"momc": ("cast_obce_kod", …)`, :41 "or a ZSJ inside one", :203-207 MOMC prose | — | Deleted |

Three traps the implementer must keep:

1. **Eponymous parts.** The part-claim path must **not** inherit `climbed_part`'s eponymous rule. 38,095 active rows are bound to the main část that shares the town's name, and the rule would drop them to obec grain.
2. **R3 on a MOMC line.** In `resolve_street` (W3 composite.py:402), a line that names any place of the town (a MOMC included) must still return reason `street_names_part`. Otherwise R3 would fuzzy-match a MOMC name to a street (bind.py's R3 guard).
3. **Ties.** The R4 tie between a MOMC and a část, broken by unit id (the plan's "unit-id tie between levels"), disappears. remax "Praha 3 / Žižkov" now has one candidate and stops grading `low`.

**Q2, a street keeps the claimed část** (W3 bind.py:789-792):

```python
if top.rung == "R2" and street_point:
    doors = street_point.part_unit_ids
    if towns.named and len(doors) == 1:
        part_unit_id = doors[0]                      # Q3 2026-09-30, unchanged
    elif part is not None and part.unit_id in doors:
        part_unit_id = part.unit_id                  # Q2: the claimed část chooses among the door-parts
```

**KÚ fix** (W3 fill.py:186-187): an address point whose own KÚ is NULL (no coordinates) falls through to its část's door rule, `registry.part_point(point.cast_obce_unit_id).katastr_kod`. That is 2 lines and fixes 19405830.

---

## 3. Rows bound at MOMC / SO / ZSJ today

All of them re-bind through the ordinary sweep. FILL's chain then starts at the část, or at the obec when no část binds.

| Today | Count | After |
|---|---|---|
| MOMC code; the part claim names a část or KÚ of the town | 1,899 | That část, at quarter grain or finer. remax "Praha N" rows read "Žižkov, Praha" instead of "Praha 3, Praha" |
| MOMC code; the part claim is a MOMC name, or there is none | 935 | The street-door rule (Q2) where a street binds, else obec grain. Losing the label is the ruled cost (Q1: "about 960") |
| SO tie (quarter grain, no code) | 200 | Obec Praha grain, unless a street or část binds. Same point and radius (Praha's reach); only the grain value changes |
| ZSJ | 0 | — |
| remax "Praha N" town | 1,607 bound to Praha | 1,607 bound to Praha. The MOMC still anchors through composite (or the ladder when a PSČ is present); the town cell is no redder |

Side effects:
- Quarter chips (`api/maps.py:266-268` offers `cast_obce` only) could never reach the 2,834 MOMC-coded rows. 1,899 become reachable.
- Expected MOMC codes in `cast_obce_kod`: 0. Expected "Wrong-MČ" rows: 0 (plan: 518).

---

## 4. Effect, the autodedup gate and the shadow tool

### 4.1 Rows that move, by portal (active)

| Portal | Pin → register point (pin outside the town) | Other movers | `{p}/outside` after |
|---|---|---|---|
| bazos | 0 (v5.9 already did this) | **23,297** area-marker pins move to the town or část point (radius from 1 km / 750 m to reach, median 5.3 km); 148 MOMC, 41 SO | 6 (register slivers) |
| realitymix | 710 today; after W4's re-mine ≤ 110 (W4's own figure: 68 undeclared agency pins + 6 placeholders + border pins) | 62 MOMC | 0 |
| idnes | 291 | 68 MOMC | 0 |
| ceskereality | 137 | — | 3 (register slivers) |
| sreality | 209, all `gps` (189 in another obec, 20 abroad) | 467 MOMC, 159 SO | 0 |
| mmreality | 3 (`accurate`) | 26 MOMC | 0 |
| bezrealitky | 1 | 18 MOMC | 1 (register sliver) |
| remax | 1 | 2,045 MOMC (1,418 "Praha N") | 0 |
| maxima | 1 (`Point`) | — | 0 |

Further movers:
- **Grain.** 536 coarsened rows regain their bind's grain: 505 quarter, 31 street.
- **Quarters (Q2).** About 12,441 rows gain a quarter, about 4.5k of them in Praha. Plan: Praha with no quarter falls to ≤ 4,800; street rows with no část fall to ≤ 6,400.
- **Q2 KÚ climbs.** About 1,460 (plan).
- **KÚ fix.** 1 row.

Kept as evidence: the `disputed/pin_outside_obec` counts stay. They become the scorecard's count of rows where the words and the pin disagree, the cases Q3 accepts as a cost: a homonym town bound without a PSČ now places an exact pin's row at the wrong town's point. The review's n = 36 favoured the pin; W5b refines pins only inside the words' town.

Consumers:
- **Labels.** `location_display_label`, unchanged: about 935 lose "Praha 5, ", 1,899 gain a část. A street row's label is unchanged (street + number + obec).
- **Location ad.** It can change where an ad's grain changes (rule 15).
- **Map.** The popup "±" grows for bazos; the drawn circle stays capped at 2 km.

### 4.2 Autodedup impact

These are the inputs the engine reads through `COHORT_LOCATION_SQL`:

- **`same_exact_pin` / `pin_pop`.** These use `pin_key` at any grain. bazos rows leave their postal pins and stack on town and část points, which is v5.9's precedent at twice the volume.
- **`dist_norm`.** Only at street grain or finer; the moved bazos rows are coarser and unaffected.
- **Blocks (E300).** In Praha, Brno and Ostrava:
  - 1,418 + 627 remax ads move from MOMC-coded "quarter" blocks to část blocks;
  - about 5.4k Praha no-quarter rows gain a quarter. They **stop probing the town key** (`blocking.probes_town_key`), so their candidates shrink from the whole city to one quarter. A ruled `same` pair whose other ad sits in another quarter would break. That is exactly what the gate must judge.

### 4.3 The id-grain export block: needed, built in this PR

`autodedup/shadow.py:94-109 blocks_for` counts a split-city side with no quarter as `left_out`, so the mover is in neither arm and the verdict is PARTIAL (wave-shadow.md:122-126). For W5 that is the riskiest population.

Build:
- `cohort.GRAINS` gets `"ids"`, with `Block.ids`.
- `export.fetch_block_ids` (export.py:537): `if block.grain == "ids": return list(block.ids), {}`.
- `blocks_for` emits one `ids` block in place of `left_out += 1`. It holds the mover, every ad an `autodedup.verdicts` pair ruling ties it to (`PAIR_VERDICTS_SQL`), the `member_ids` of any cluster verdict containing it, and its property's sibling ads.

Both arms then hold the mover and every ad a ruling ties it to:
- in the base arm the mover has no quarter, probes the town key and meets them;
- in the candidate arm it meets only its new quarter and the ads that probe the town key.

A ruled pair split across quarters shows as broken, and the gate FAILs as it should. Deleted: the `left_out` counter and print (shadow.py:96-108, 232-233) and the PARTIAL caveat (wave-shadow.md:124-126). Cost about +60 code / +40 tests, carried inside W5's ledger (rule 25) rather than as a separate PR that adds.

### 4.4 W5's `ids_sql` (claims=none, from the W5 branch)

```sql
SELECT l.id
  FROM listings l
  JOIN listing_location ll ON ll.listing_id = l.id
 WHERE l.is_active
   AND (   ll.disputed IN ('pin_outside_obec', 'pin_outside_cz', 'country_conflict')      -- Q3, every portal
        OR (l.source = 'bazos' AND ll.granularity IN ('obec', 'cast_obce_or_quarter'))    -- Q3, the area marker
        OR ll.cast_obce_kod IN (SELECT u.code FROM ruian_admin_units u
                                 WHERE u.level = 'momc' AND u.valid_to IS NULL)            -- Q1, MOMC-bound
        OR (ll.granularity = 'cast_obce_or_quarter' AND ll.cast_obce_kod IS NULL)          -- Q1, SO-bound
        OR (ll.obec_kod IN (554782, 582786, 554821) AND ll.cast_obce_kod IS NULL))         -- Q1/Q2, split city gains a quarter
   AND <slice>
```

Size before scope: about 16.4k + 35.2k + 2.8k + 0.2k + 15k ≈ 65k (overlapping); `what_if` keeps only the real movers. `-f scope=apply` keeps the live apply scope. The caps (40 blocks, 60k ads) need slices, and a block with no ruling cannot fail (PASS = E111 clean and no ruled pair moved against its ruling; E111 is about operator negatives), so:

- **Non-Praha slices:**
  ```sql
  AND ll.obec_kod <> 554782
  AND ll.okres_kod = ANY('{…}')
  AND ll.obec_kod IN (SELECT r.obec_kod FROM autodedup.verdicts v
                      JOIN listing_location r ON r.listing_id IN (v.listing_lo, v.listing_hi)
                      WHERE v.kind = 'pair')
  ```
  Use groups of okresy, each at most 40 town blocks.
- **Praha in 6 slices** by the KÚ of the position (Praha's 112 KÚ match its části almost one to one, so a slice's new quarter blocks stay inside it):
  ```sql
  AND ll.obec_kod = 554782
  AND (SELECT u.code FROM ruian_admin_unit_geometries g JOIN ruian_admin_units u ON u.id = g.unit_id
        WHERE g.registry_version_id = (SELECT id FROM registry_versions WHERE is_current)
          AND g.purpose = 'pip' AND u.level = 'katastralni_uzemi' AND ST_Covers(g.geom, ll.geom)
        LIMIT 1) % 6 = k
  ```

Dispatch:

```
gh workflow run location_wave_shadow.yml --ref feature/location-w5-resolver-v5-10 \
  -f wave=w5-v5-10-<slice> -f claims=none -f scope=apply -f ids_sql="<above>"
```

Judge on the rulings ledger in the affected blocks; the 413 `same` pairs recorded since 2026-10-27 must hold. Read `drain.tally` in the artifact (`town`, `part`, `moved_300m`, `obec>cast_obce_or_quarter` …) against §4.1.

---

## 5. Version, sweep and rollout

`version.py`: `RESOLVER_VERSION = "resolver:v5.10"`, with this entry:

> v5.10 = location W5 (rulings Q1, Q2, Q3). ONE TRUTH: where the words bound the row, a pin is its position only inside the bound town's polygon and never when the portal declares it an area marker (bazos `approximate_location`); without words it names its town (R7/R8) as before. v5.9's declared-blurred exception and CHECK's coarsening go: 1,353 pins outside their town and 23,297 bazos postal pins move to the bound entity's point at its reach. The quarter is a část only: MOMC is a match key that anchors its town; správní obvod and ZSJ are no level (2,834 MOMC codes and 200 SO ties re-bind). A part claim matches like a town name (a KÚ binds the část it spells), and a street keeps the claimed část where it has doors. The grain is the row's finest key. An address point without coordinates takes its část's KÚ. The bump re-resolves the corpus through W2's reconcile.

Rollout:

1. **Prerequisites.**
   - W3 merged; v5.9.1 live and its sweep complete. W5 is cut from W3's `bind.py`: no `elect_pin`, one claim per type.
   - W4 re-mined (realitymix@8).
   - W7 is not required. The code stops naming SO and ZSJ either way; SO has 12 name-index entries, all MOMC twins.
2. **Branch** `feature/location-w5-resolver-v5-10` from `main` after W3. One PR: the resolver change plus the `ids` export block.
3. **Shadow gate** (§4.4), every slice PASS.
4. **Merge.** Railway deploys; the worker's `reconcile` sees (v5.10, label) and enqueues active rows first. Evidence beats the sweep; W2's bound is active rows within 90 min and all rows within 4 h. There is no migration and no hand dispatch.
5. **Read acceptance** (§8) after `freshness/version` clears.

---

## 6. Tests

No contract changes, so no claim goldens are re-blessed (`tests/fixtures/location_w2/golden/` stays). The resolver's expectation tests act as the goldens and are rewritten in place.

**Delete or invert:**
- `test_resolver_bind.py:682` `test_place_sets_aside_only_a_declared_blurred_pin…` becomes the one-truth table below.
- `test_resolver_check.py:163` (keeps the pin and drops to the admin level) is inverted: FILL's point, the bind's grain, still flagged.
- `test_resolver_check.py:252` (a precise pin outside is kept) is inverted.
- `test_resolver_check.py:193, 222, 238` fold into one parametrized test.
- `test_resolver_bind.py:462` (a claimed part of another level stands) is deleted with the levels.
- `test_resolver_composite.py:59-77`: MOMC and numbered-obvod lines give the town with part None. In `:97-98` the Liberec and Ústí rows expect part None.
- `test_resolver_katastr.py:92`: the ZSJ case goes.
- `test_resolver_grade.py`: `_grade` and `graded.granularity` tests (:44, :152, :217-236) become `confidence` + `grain_of`.
- `mini_mirror.py`: MOMC units stay (match keys); drop SO units if no test reads them.

**Add (parametrized, compact):**
- One-truth table over labels `gps`, `accurate`, undeclared, `Point`, `no_exact_address`, `approximate_location` × inside/outside × words/no words. Expected positions: pin / FILL / R7 pin.
- R9 with a pin abroad: okres point.
- A pin outside Czechia with a Czech town: town point and `pin_outside_cz`.
- MOMC is a key: town "Praha 3" + part "Žižkov" gives Praha/Žižkov, `cast_obce_kod` = Žižkov, not ambiguous. "Praha 5" alone gives obec grain. "Brno-střed" gives Brno with no part. A street line naming a MOMC of the town is no street and R3 does not run.
- A claimed eponymous main část still binds (guard against the 38,095-row trap).
- Q2: a street across parts keeps the claimed část only where it has doors. A KÚ-named part claim ("Nové Sady u Olomouce") climbs to its část.
- `grain_of(keys) == granularity` over every fixture resolution (a property rail; it replaces the grain branches).
- KÚ fix: an address point without coordinates takes its část's door KÚ.
- Shadow: `blocks_for` emits an `ids` block (mover + ruled counterparts + siblings); `fetch_block_ids` returns it; the import and AST rails are unchanged.
- Purity and replay unchanged. The view contract test is unchanged (`CONTAINING_OBEC_SQL` untouched).

---

## 7. Docs

**`docs/architecture.md`**

- **:5186-5225, the ladder.** The two paragraphs become one:

  > **WHERE THE ROW IS PLACED IS ONE LADDER, AND THE POSITION LIES INSIDE THE TOWN THE WORDS BIND** (`bind.place`, v5.10, ruling Q3): registry ADDRESS POINT > bound STREET point > portal pin *inside the bound town* > FILL's entity point. The words bind the town (stated, or derived from finer words through RÚIAN). A pin outside it, by the pip polygon and with no tolerance, is never the position, whatever the portal declares. A pin inside refines. A pin the portal declares an AREA marker (bazos "Přibližná lokalita", a postal-area point) never places a row the words bound. Without words the pin names its town (R7/R8) and stays. The pin stays evidence: CHECK records `pin_outside_obec`, the scorecard's count of word/pin disagreements.

  Keep the street-extent and `pin_off_street` sentences; delete the v5.9 exception paragraph and its cost list (it becomes one line of costs).
- **:5242-5255.** The heading becomes **"THE GRAIN IS THE ROW'S FINEST KEY; A DECLARATION REACHES ONLY THE CONFIDENCE"**: one writer (`grain_of`), CHECK writes none; the W18 history shrinks to two sentences.
- **:5115-5119, the composite paragraph.** "obec / část obce / MOMC" (SO deleted). Add: "a MOMC anchors its town and is never the quarter; the quarter is a část obce (ruling Q1)."

**`CLAUDE.md` rule 25.** After "ONE resolver writes `listing_location`" add: "— its quarter a část obce, its grain the row's finest key, its position inside the town the words bind;".

**`PROGRAM.md` § W5 "As built".** Record:
- strict containment (Q3), with no tolerance and no distance read;
- the bazos area label;
- determinism deferred, with the measurement;
- the `ids` block built;
- §8's before and after;
- in the acceptance table, "Outside cell (tolerant test)" becomes "outside (strict, Q3)", and the remax row 1,432 becomes 1,607.

**Other files.**
- `wave-shadow.md`: the `ids` grain and W5's dispatch.
- `version.py` (§5), plus the docstrings of `place`, `fill`, `check` and `core`.

---

## 8. Acceptance (the W1 cells and SQL)

| Measure | Before (10-31 16Z) | After |
|---|---|---|
| `listing/{p}/outside` | 1,359 (realitymix 709 → ≤ 110 after W4) | Register slivers only: bazos ≤ 6, ceskereality ≤ 3, bezrealitky ≤ 1, all others 0 |
| `served/{p}/outside` | 1,105 | The same residual |
| Position is a pin outside the bound obec | 1,353 | **0** |
| bazos words-bound rows on their pin | 23,297 | **0** |
| Grain differs from the finest key | 736 | **0** |
| MOMC codes in `cast_obce_kod` | 2,834 | **0** |
| Quarter grain with no část code | 200 | **0** |
| Street rows with no část | 18,724 | ≤ 6,400 |
| Praha with no quarter | 9,308 | ≤ 4,800 |
| remax "Praha N" bound to Praha | 1,607 | 1,607 (about 1,600 with a část) |
| `sreality/ku` | 1 (19405830) | 0, so the cell leaves fail |
| Town cells / `location_town_coverage` | W1 reading | No redder |
| `disputed/pin_outside_obec` | per §0 | About the same (evidence stays) |
| Shadow gate | — | PASS on every slice |

Reads:
- **The cells:** `select cohort, source, class, n, of_n from location_quality_by_source where metric in ('outside','disputed','grain') and cohort in ('listing','served');`
- **Pin outside (expected 0):** active rows with `disputed in (…)` whose `geom` is within 1 m of the newest coordinate claim and outside the pip polygon of `obec_kod` (the §0 query).
- **Grain:** §0's `grain_of` CASE over `listing_location`, where granularity differs from it.
- **MOMC:** `count(*) where cast_obce_kod in (select code from ruian_admin_units where level='momc')`.
- **SO:** `granularity='cast_obce_or_quarter' and cast_obce_kod is null`.
- **bazos:** grain obec/quarter with `geom` within 1 m of its pin.

**The residual, named.** About 10 rows whose register point (a část door centroid or a street centroid) falls outside its own obec's polygon. They are not pins. About 300 ceskereality ads with only a pin are R7: the pin names its town, the words cannot judge it, and they are never flagged. R8 sliver pins likewise.

---

## 9. Deletion ledger versus additions (measured: `git diff --numstat` against W3's branch, this file aside)

| Area | Deleted | Added | Estimated |
|---|---|---|---|
| Resolver (`bind`, `check`, `grade`, `composite`, `fill`, `core`, `types`, `version`) | −320 | +210 | −217 / +83 |
| Shadow `ids` block (`autodedup/cohort`, `export`, `shadow`) | −25 | +82 | −12 / +60 |
| Tests | −262 | +318 | −170 / +165 |
| Docs (architecture, CLAUDE.md rule 25, PROGRAM, wave-shadow, roadmap) | −108 | +102 | −60 / +35 |
| **Total** | **−715** | **+712 (net −3)** | −460 / +345 |

The resolver nets −110 against the estimate's −134: `part_of` and `town_code` cost more than estimated. The tests net +56: the one-truth table, the MOMC-key, eponymous, Q2, KÚ-fix, `ids` and must-not-link (live) tests outweigh the deletions.

Rule changes:
- Special cases go from 6 to 0: blurred-outside, coarsening, MOMC-as-quarter, SO tie, ZSJ lookup, the unit-id tie between levels.
- Grain writers go from 2 to 1; containment readers stay at 1; level lists at 1.
- The planned "+15 lines and a distance read" is not built.

---

## 10. Rulings: defaults taken, no ruling needed

| Item | Default taken | Why safe |
|---|---|---|
| bazos area marker | The resolver classes the label bazos already emits as an area marker; no contract bump | Q3's text; 52,185 of 52,185 pins carry it; no re-mine |
| Kept pin inside its town | Radius stays the grain constant until W6 | Principle 4; an interim floor would be a band |
| Determinism (`ORDER BY u.code` in `CONTAINING_OBEC_SQL`) | **Deferred** to the next re-render of the scorecard view (W1-c migration B or W6), with the `OFFSET 0` fence and an EXPLAIN rail | 0 of 6,553 sampled pins lie on a shared border. The view embeds the probe verbatim (`test_location_quality_view_contract.py:190`), so changing it now means a 247-line view migration for a case not observed |
| Register-point residual (about 10) | Named, not fixed (a door instead of a centroid would add without deleting) | It is not a pin; the cell stays honest |
| MOMC line ("Brno-Královo Pole") → část of the same name | No | MOMC borders cross části; Q1's about-960-label cost is ruled |
| Qualifier scoring (an obec R4 with 2 qualifiers beats a claimed part) | Deferred explicitly (PROGRAM "Not covered") | W5 changes no score |
| A precise pin deciding the část through its KÚ | Stays deferred (PROGRAM) | W5b refines below the town |

No default here is unsafe, so W5 needs no new ruling. Watch on the shadow:
- bazos `pin_pop` stacking on town and část points (23k ads);
- Praha's quarter split for the about 5.4k rows that gain a quarter.

---

## As built (where the build differs from §§ 1–7)

- **Names.** `bind.PART_NAME_LEVELS = (*MATCH_LEVELS, "katastralni_uzemi")` is the part-claim match list (the R4 part match and the `bind_towns` anchor). `composite.town_code` is the one "the ltree path's obec, else the chain's" read (four readers). `part_of(unit, town, registry)` takes the town's code.
- **`resolve_street`.** A line naming any place below the town, a MOMC included, returns `street_names_part` (trap 2); `named_part` is the one část. A line naming two části of one town now reads as a place too (it was `no_match`, so R3 could run on it). Default: it blocks R3; no such row known as of 2026-10-31. The count, on the verbatim key only (the type-word-stripped key is not modelled):
  ```sql
  SELECT count(*) FROM (
    SELECT c.listing_id
      FROM location_claims c
      JOIN listings l ON l.id = c.listing_id AND l.is_active
      JOIN listing_location ll ON ll.listing_id = c.listing_id
      JOIN ruian_name_index n
        ON n.name_norm = btrim(regexp_replace(lower(unaccent(c.value_text)), '[^a-z0-9]+', ' ', 'g'))
       AND n.entity_kind = 'cast_obce'
       AND n.registry_version_id = (SELECT id FROM registry_versions WHERE is_current)
      JOIN ruian_admin_units u ON u.id = n.entity_id AND u.valid_to IS NULL
     WHERE c.claim_type = 'street_name'
       AND u.path::text ~ ('(^|\.)b' || ll.obec_kod || '(\.|$)')
     GROUP BY c.listing_id
    HAVING count(DISTINCT u.id) >= 2) twice;
  ```
- **The `ids` block** travels as one token, `ids:<id>+<id>…` (`cohort.ids_block`, read back by `parse_blocks`: key `ids`, code its count), so `chunks`, `group.json` and `run_export` carry it unchanged. `parse_blocks` is the one reader of the token: `block_sizes` keys each block by it, `outside_group` counts a mover as exported by the group whose `ids` block lists it, and the size log prints the block's key.
- **The `ids` block's ties** are every ruling the judge reads (`evaluate.read_rulings`): pair verdicts and operator must-not-links, group verdicts and operator merges, plus the property's siblings (review, 2026-10-31; `tests/autodedup/test_shadow_ties_live.py`).
- **Tests.** The grain rail is an autouse fixture in `tests/location_data/conftest.py` around `core.resolve`: every resolution any location test makes asserts `granularity == grain_of(keys)` (a second writer fails it; probed). The MOMC-key case uses the statutory mirror's "Praha 4" + Podolí (the mirror holds no Žižkov). The one-truth table runs every label through `core.resolve` inside, outside and without words.
- **CLAUDE.md** rule 25 reads "ONE resolver writes `listing_location` (quarter = část; grain = finest key; position inside the words' town)", paid inside rule 25 (31,250 of 31,250 bytes).
- **Also trimmed:** the stale `grade.DECLARED_CAP` note on `PRECISE_DECLARED_LABELS`, CHECK's W18 history comment, and `wave-shadow.md`'s W5 dispatch points at § 4.4 instead of copying it.
- **Not run here:** the shadow (§ 4.4) and the acceptance reads (§ 8) need production; they run after the push.
