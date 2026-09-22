# Field capture — every typed listing fact, captured properly, on all nine portals

Opened 2026-09-21. The program document — north star, the binding rulings R1–R12, the
numeric per-wave gates, what is cut from scope and the accepted residual risks — is
`docs/design/field-capture/PROGRAM.md`. **That file is the source of truth; this one is
the checklist.** Don't restate a ruling here.

## North star

> **Every typed listing fact has ONE declared producer per portal, ONE canonical
> vocabulary, and a MEASURED fill rate and accuracy — stated facts parsed at ingest,
> prose-only facts extracted after publication — and nothing ever sits between a sighting
> and the row being visible.**

Subtraction is the deliverable: every wave removes at least as much as it adds. Program
estimate ≈ −7,260 / +2,860 LOC, −30 files, −2 tables, −8 workflows, 0 new columns.

## Waves — one PR each

- [x] **W0 — stop the dead lane pretending.** The `sreality_id`-keyed description-
      enrichment lane deleted wholesale: 4 scripts, 2 workflows, 3 test files, the two
      0-row batch tables (migration 546, destructive, operator OK 2026-09-21) and
      `check_llm_liveness`, whose threshold was sized on "the one recurring producer" that
      no longer exists. Kept for W7: `listing_description_enrichments` (37,754 rows), the
      `enrich_listing_description` called_for, `app_settings.enrichment_model`.
- [x] **W1 — measurement before change.** Per-portal key census (9/9, checked in; staleness
      is a verify_pipeline warning) + a (portal, field) fill **and validity** matrix over the
      whole active stock, scored against a blessed baseline. Repairs the flaky
      `capture-data-quality` job (mig 548) — the Health page reads its series. The one wave
      that only adds: it is the instrument every later wave is judged by. First run: 46 zero-fill
      cells of 234; `condition` off-canon on all nine portals; census in `data/field_capture/`.
- [x] **W2 — vocabulary module + the attribute contract table + CI gates.** Identity-
      preserving and proven so: 52 per-parser normaliser functions, 19 mapping dicts, 13
      regexes and 11 planted-fixture tests collapse onto `scraper/vocabulary.py` (the
      producer side; the canon stays in `toolkit/filter_registry.py` and is imported) plus
      `scraper/attribute_contract.py` (all 9 × 26 cells: producer, key precedence, absence
      semantics, sentinels, known gaps). Gates A1 / A2 / A3 run over the checked-in census —
      A1 found **26** dead reads, not the 6 the investigation named, and review found **13**
      more hiding in the five `areas_from_params` key chains the contract had only restated
      (now consumed, so the gate covers them). The identity rail is
      `tests/fixtures/field_capture/golden/`, recorded from the unchanged parsers.
- [x] **W3 — the one re-parse seam.** `scripts/reparse.py` replays the portal's OWN parse
      entry point over a substrate declared once per portal: `portal_raw_pages.html` on the
      seven HTML portals (100 % coverage incl. inactive, staged in the drain transaction so
      it cannot lag the row) and `listings.raw_json` on sreality + bezrealitky, which stage
      no body. Never writes a snapshot, never blanks what a re-derive cannot produce, never
      bumps `last_seen_at`, enqueues `dirty_properties` in the same CTE, and writes a row
      only while it still holds what the pass read (compare-and-set, so a concurrent detail
      write is never reverted); `--fields` is required and dry-run is the default. **Two
      limits W4/W5/W8 must plan around:** on sreality, which hashes the RAW payload, a heal
      defers no snapshot — it appends NONE, ever — and `parse_listing` cannot read that
      portal's oldest rows at all (234 of the 1,000 lowest ids carry a usable key), which the
      run WARNs about instead of exiting clean. Absorbs `reextract.py`'s registry, deferral
      gate and hash assertion (now derived, over all 27 healable columns) plus its
      `description` arm;
      deletes four area heals, their four workflows and three test files. **Two of the six
      named backfills survive, with evidence:** `backfill_unit_price_masquerade` QUARANTINES
      a price (`price_czk → NULL`), which never-blank forbids by design, and 794 realitymix
      rows still await it; `backfill_idnes_brokers` writes `raw_json`, which the seam does
      not, and 637 of the 20,000 oldest idnes rows still carry no broker block.
- [x] **W4 — close every structured gap the census proves.** ceskereality `parkování` +
      `balkóny` (has_parking/garage/terrace, 0.0% → real on 48,620 rows), remax
      `pocet parkovacich mist`, realitymix lift/cellar/garden/lots, idnes `total_floors`
      on 29.7k houses, mmreality's typed balcony/loggia/garage/equipment keys and a
      group-qualified has_parking (73.4% → 53.9%, "Parkety" gone), bezrealitky's EUR
      prices refused. ONE `has_balcony` (balcony OR loggia) and `has_parking` (a space
      BELONGING to the property) definition, four union helpers collapsed into
      `vocabulary.any_true`. **The W3 seam lands the NULL→value and true→false half;
      the true→unknown half (idnes has_balcony 17,845 rows, bezrealitky 396, bezrealitky
      price_czk 31) is blocked by R9's never-blank rule** and arrives on each row's next
      detail fetch, never for inactive rows — per-portal numbers in the program doc.
- [x] **W5 — the vocabulary collapses.** The canon widened to the values live rows carry
      (condition +5, building_type +4, ownership `jine`, dispositions to 9+1, `price_unit`
      4 spellings → 2 members): 14,068 + 8,351 + 710 + 73 active rows become reachable by a
      Browse option that could not name them. Four true synonyms collapse
      (`ve_vystavbe_(hruba_stavba)`, `urceny_k_demolici`, ceskereality's comma-joined
      materials, the price_unit pair); the legacy tier and the one portal override are gone,
      so the LLM tool schema's enums ARE the canon — `disposition` and `price_unit` included.
      The disposition grammar refuses what cannot exist (819 stored bazos rows across 28
      values, listed for the operator; R9 keeps them). PENB `G` stays one member: measured,
      no portal marks the statutory placeholder. Two migrations carry the canon change
      outside the parsers: **549** teaches the two Browse aggregate RPCs the fourth
      ownership value (rule 16's one `__unknown__` predicate), **550** stops the URL
      parser's DB-resident prompt instructing the retired `price_unit` spelling its new
      enum forbids. **The stored-row heal is dispatched post-merge** — five dry-run lines
      plus the `--bless` re-score, in the PR and the program doc.
- [x] **W6 — close the wipe (R4).** The one shared SET builder now takes the source and
      asks the contract: `structured` / `derived` cells still clear from a parser NULL (a
      portal that stops stating a fact must be able to drop it), `text` / `none` cells
      preserve — and every `none` cell on the eight structured portals is 0-filled live,
      so the rule is a no-op for the parsers and protects only post-publication producers.
      Zero extra statements; the per-item statement is now built once per source instead
      of per listing. The property golden record loses its `bool_or` special case for the
      six amenity booleans (has_parking 1,384, cellar 486, has_balcony 212, garage 162,
      terrace 140, has_lift 64 rolled-up values flip true→false over 23,641 active
      multi-child properties; 4,095 / 1,388 / 682 / 410 / 406 / 357 over all 74,090,
      the surplus on delisted properties Browse hides). `run_incremental_pass` patches
      `browse_list` for the ids it recomputed: seen-to-Browse was a measured mean 11.7 min
      (94 rebuilds / 24 h, worst 36.6) and takes this lane's ~2 min cadence whenever a
      wholesale rebuild is not in flight — which it is ~26 % of the time, and a patch
      inside that window is silently superseded.
- [ ] **W7 — the text lane on the realtime worker.** No flag, no new setting: governed by
      the contract's producer=text cells. Model bake-off in ONE run (gpt-5.6-luna vs OSS on
      RunPod); a field is written only after a labelled panel passes ≥ 95% (R7); the lane
      and its health check share ONE eligibility function (R8).
- [x] **W8 — floor: ground = 0 everywhere.** The convention is contract DATA
      (`ground0` | `ground1` | `word` per portal) and `scraper.floor.floor_from_portal`
      refuses a bare int without one; three per-parser floor readers, maxima's
      int-returning split and five per-parser regexes are gone, `is_plausible_floor`
      tightened to `total_floors - 1`, and one `fmtFloor` replaces six inline SPA
      expressions so the screen says "2. patro z 5 podlaží"
      instead of a bare number. New `floor_convention` check in verify_pipeline (the
      sibling-pair mean vs idnes; 11-35 s measured, the lane's costliest) — there was
      no floor check of any kind.
      The R12 hand-over shipped first: `docs/design/field-capture/handover-autodedup-floor.md`.
      **The 183,745-row heal (63,328 active) is the operator's to run** — six
      `scripts/reparse.py --fields floor` passes, runbook in the hand-over §7; the new
      check reads RED until they have run.
- [~] **W9 — patchwork sweep (non-autodedup).** Done alongside W0: the browse_list cadence
      comments (`*/15` since migration 413, two said 5 min); `location_data/payloads.py`'s
      "NOT WIRED" docstring (826,948 rows, nine portals); Browse Stats/Map now send the
      **seven** size filters they never sent and bound the plot MEASURE (migration 547,
      plus a rail that covers the two RPCs and not only the two Python sites). Still owed,
      each its own PR: maxima coords (the defect is in `scraper/maxima_parser`, NOT the
      contract — it already reads the pin); the bezrealitky `ruianId` rung (**feed it**:
      732 of the 2,836 ruianId-bearing active rows resolve below `address_point` today;
      needs contract v4 + a re-mine); branch protection on `main` (operator action).

## Hand-overs owed to the autodedup program (R12 — this program never edits theirs)

- The false "zero area on all 138,997 bazos rows" sentences (`docs/design/autodedup/PROGRAM.md`
  E12, :241, :1046). Live: bazos `area_m2` is present on 84.1% of active rows.
- The stale `PLOT_TRUNCATING_SOURCES` guard — the truncation it pins was fixed by
  `scraper/area.py` and healed.
- ✅ The W8 floor conversion table + predicate, delivered before W8 merged:
  `docs/design/field-capture/handover-autodedup-floor.md` (per-portal before/after
  distributions, the exact predicate, and every autodedup site that assumes a floor
  convention — `l0_floor_tolerance`, `floor_stated_conflict`, the guards' `band` arm,
  `floor_spread`, and the persisted `floor_lo`/`floor_hi`/`floor_checked` pair state).
