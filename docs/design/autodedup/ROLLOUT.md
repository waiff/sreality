# AUTODEDUP: rollout report

**For:** the operator, to decide on rollout. **Date:** 2026-09-25. **Program:** `docs/design/autodedup/PROGRAM.md` holds every rule (E), decision (D) and measurement (M) by number; this page is the summary. This update adds waves W25 to W29 (E250–E298, D71–D86, M560–M666), the final numbers and the end of the accuracy work.

## 1. What was built

An engine that finds the same real-world property across the nine portals and across time and decides, on its own, whether two adverts are one property. It runs in **shadow mode**: it writes its groups into its own tables (`autodedup` schema) and the review pages read them. It has never written a merge into production data.

It costs **$0 per month** to run. Every signal it uses is already produced by the platform (scraped attributes, price history, resolved location, broker identity, photo fingerprints, CLIP vectors, room tags). No LLM is called at decision time. LLMs were used only during development, to make labels and to test ideas; the total development spend is about **$52 of the $200 budget**.

## 2. The ruling that shaped it

On 2026-09-21 you ruled that **identical units merge**: two adverts are one entry unless a *stated fact* tells the units apart: a printed unit number or code, a floor, a headline or plot area, a price that is not one advert's own price path, a parcel number, a street or house number, a room or desk count, a deposit or service charge on rentals let side by side, a stated interior difference. A *false merge* is a merge across such a fact. Everything after that ruling was measured against it.

## 3. How it was tested

Sixteen areas of the country were exported, about 20,000 adverts each, none overlapping. Each new engine version was tuned on the areas already used and then **confirmed once on an area no rule had seen**, by independent readers who:

- ran a hunt for mixed groups with their own text readers and read every flagged group by hand;
- read 160 newly merged pairs blind, from the raw adverts, before seeing anything the engine said;
- read every group on the hazard ground of that area (new developments, land, side-by-side rentals, the largest groups).

The bar for each confirmation was written down before the area was opened. The last version, S14, was built from what the sixteenth area's reading found and only adds reasons to keep adverts apart, so instead of a seventeenth area it was checked by reading, from the raw adverts, every pair it separates or joins compared with S13 on all sixteen (D82). Three generations appear below: **g7**, the engine live when you made the ruling; **g11** (version S9), what the review pages show today; **g12** (version S14), this update.

## 4. Results

### Recall: certain duplicates ending up in the same group

"Certain duplicates" are pairs proven without any label: a shared rare agency order number, four or more identical non-stock photos, or byte-identical text.

<!-- Paths in these comments are under /home/hejtm/autodedup-artifacts/w14/. g7 + g11 (= S9, settings w24): s12/rollout_table.txt (trial..cohort14); cohort 15: confirm_cohort15/table.txt "ALL INCL 25 (20380)"; cohort 16: confirm_cohort16/table.txt "RECALL (all) n=25671". g12 (= S14, settings w29): s14/rollout_table.txt. Every file uses the same certain denominators. Adverts: listing rows of cohortN/export/cohort.jsonl.gz and the confirm_cohortN/table.txt headers; trial and region as in the 2026-09-23 report. Rentals: s14/rollout_table.txt pronajem% (S14). Town-only: s14/rollout_townonly.json (s14/scripts/rollout_townonly.py; reproduces s12/measure_s12.json recall_by_grain for S12 and confirm_cohort16/table.txt obec|obec). -->
| Area | Adverts | g7 (before the ruling) | g11 (review pages today) | g12 (this update) |
| --- | --- | --- | --- | --- |
| Jablonec / Turnov / Vysočany (trial, labelled) | 5,687 | 50 % | 76 % | 78 % |
| 140 towns of the Jablonec–Semily region | 15,017 | 38 % | 92 % | 95 % |
| Olomouc + ring, Praha-Holešovice / Hlubočepy | 17,306 | 37 % | 90 % | 92 % |
| Plzeň + ring, Praha-Hloubětín | 19,886 | 55 % | 88 % | 91 % |
| Hradec Králové / Pardubice + belt, Praha-Letňany | 19,641 | 51 % | 87 % | 92 % |
| Karlovy Vary / Mariánské Lázně / Cheb, Praha-Modřany | 19,874 | 38 % | 88 % | 93 % |
| Ústí nad Labem / Teplice + districts, Praha-Hostivař | 19,936 | 30 % | 94 % | 95 % |
| Brno quarters + ring, Praha-Záběhlice | 18,889 | 69 % | 86 % | 90 % |
| Ostrava / Opava + ring, Praha-Michle | 18,214 | 65 % | 88 % | 91 % |
| South Bohemia's seven district towns, Praha-Strašnice | 18,450 | 71 % | 89 % | 94 % |
| Karviná / Havířov / Frýdek-Místek / Třinec / Č. Těšín, Kroměříž, UH, Praha-Vinohrady | 19,630 | 66 % | 88 % | 92 % |
| Vysočina's five district towns, Liberec / Česká Lípa, Praha-Chodov | 19,980 | 67 % | 89 % | 93 % |
| Praha-západ commuter ring, Zlín / Vsetín, Praha-Dolní Měcholupy | 20,164 | 55 % | 90 % | 93 % |
| Praha-východ commuter ring, Jičín / Náchod / Rychnov / Trutnov, Praha-Kunratice | 19,190 | 64 % | 87 % | 91 % |
| Okres Most + okres Břeclav, Praha-Bohnice | 19,075 | 61 % | 91 % | 93 % |
| 22 bazoš-heavy towns, rest of okres Český Krumlov (Lipno) and okres Kroměříž, Praha-Řepy | 19,253 | 45 % | 90 % | 93 % |

Rentals alone under g12: 90–96 % on fifteen areas, 73 % on the labelled trial area. Adverts located only to the town: 87–97 % on the fifteen areas that have them, where g7 finds 5–56 %.

Past about 93 %, recall is limited partly by the reference itself, which counts some pairs of two units as certain duplicates: developer rosters posted under one shared text, and plots whose printed lot labels differ (all 25 such lot-label pairs on the Most / Břeclav area are two plots). On the last area, recall without the flagged roster pairs is 92.9 %; a hand sample of 40 of them found 37 genuine duplicates, so that flag is loose and the true ceiling is not known.
<!-- confirm_cohort16/table.txt xF2% (92.88); F2 = cohort16/ref/contamination_flags.json F2_roster_generic_body; sample: confirm_cohort16/hazard_ground.json F2_sample (37/40 one unit); lot labels: confirm_cohort15/REPORTS.json agent3 -->

### Safety: read by hand on areas the engine had never seen

<!-- rows 1-6: the 2026-09-23 report. Most / Brecl.: confirm_cohort15/REPORTS.json agent2 (fused g7 19 / S12 2, bar 2 18 / 2; fused_groups_confirmed_{g7,S12}.json) + agent1 (verdicts_pairs.json, 0/160) + agent3 (hazard_ground.json); g12 there: s14/ledger_delta.json (cohort 15 unchanged S12 -> S14) + s13/handread_s13.json. Lipno: confirm_cohort16/table.txt HAND-CONFIRMED FUSED (g7 8 / S14 4, bar2 6 / 3; fused_groups_confirmed_{g7,S14}.json); blind: verdicts_pairs.json (S13 2 of 160) + hazard_ground.json BAR_LINES 4 (S14 0). Ostrava's 2: s14/measure_s14.json blind_different_co + PROGRAM.md W22. -->
| Area | Mixed groups found, g7 | Mixed groups found, engine confirmed there | Of those, neighbouring units of one development or plots | Blind read of 160 new merges: wrong |
| --- | --- | --- | --- | --- |
| Karlovy Vary | 7 | 1 (g7 makes it too) | 1 | 0 |
| Ústí / Teplice | 8 | 4 | 1 | 0 |
| Brno | 13 | 7 | 3 | 3 |
| Ostrava | 11 | 6 | 1 | 16 (all rentals; g12 still merges 2, see below) |
| South Bohemia | 15 | 7 | 4 | 1 |
| Coal-basin towns / Kroměříž / Vinohrady | 3 | 6 (all one shape, fixed in S9, §5.1) | 0 | 2 |
| Most / Břeclav / Bohnice | 19 | 2 (g12) | 2 | 0 |
| Lipno / Kroměříž / bazoš towns | 8 | 4 (g12) | 3 | 0 (S13 had 2; g12 separates both) |

Areas 12 to 14 were read the same way; their counts are in PROGRAM.md (W25 to W27). On Most / Břeclav the hand read was of S12; g12 changes no mixed group there, and every new join S13 made there that carried any machine-detected difference was read, with none wrong.

- **The four mixed groups g12 carries on the Lipno area.** Two readers counted four each; they agree on the first three.
  - *Two Lipno villa groups (Arktida / Louka).* The villa name is printed only on idnes; the sreality copies of two villas share one text and one gallery, so nothing stated tells them apart. A reader for the villa name made things worse: it cut each idnes advert off its own sreality copy and moved one advert into another villa's group. Refused (D84).
  - *Hotel rooms, Kostelec nad Černými lesy.* From 09-09 every portal carries a second advert, identical except that it drops "a smaller fridge" from the furnishings. Leaving something out is not a stated fact (D86); read as one it would split 221 of 444 certain duplicates.
  - *Hoštice 2+kk (first reader only).* The portal column and headline say 76 m² on one advert and 77 m² on the other, but every body prints 77 m². The second reader ruled them identical units, which merge under your ruling; a 1 m² rule would split 5,402 certain duplicates (D85).
  - *Němčice house (second reader only).* In August the house plus a separate building plot, 1,462 m² for 5.35 M; in September the house alone on 629 m² for 4.2 M. Contested: the first reader read one house offered again. g7 merges it too.
- **Pairs the reference proves different from structure alone** (for example two different registered addresses): 0 merged on all sixteen areas, by every version since the ruling. g7 merges 1–4 on six areas.
- **The ledger.** Every mixed group any reader has found is kept and re-checked against each new version. g12 makes none worse than S12 or S13 did and fully separates five more than S13. Against g11 (on the review pages today), on the first fourteen areas g12 separates 18 entries further and 6 less far; five of those six were examined and carry no fact the engine can read (W26, W27).
- **Ostrava's blind read:** g12 still merges 2 of its 16 wrong pairs, an Opava bar and two Velká Polom units. The readings that would part them cost 258 and 43 certain duplicates, so they were refused in S7.
- The Ostrava result exposed one weakness: two flats in the same house let side by side on one portal at similar rents. Version S7 reads the deposit, charges, flooring, sanitary arrangement and renovation state as facts; on the South Bohemia area 60 such pairs were then read blind with none wrong.
- Two groups of 16 and 24 identical adverts of one turnkey project, all the same size, plot and price with identical text, are **one entry each** under your ruling. They are flagged in the ledger as the ruling's own case.

### The correction to what was said before the ruling

The engine that was live then was reported as having "zero known false merges". That was true against the labels available. Under the hand hunts above, it carries 3–26 mixed groups per area where it was read, including houses "č. 13, 14, 15" of one project and flats "202, 205, 209" of one residence. g12 separates most of them and makes fewer of its own; the ones it still carries are named in §4 and §5.
<!-- g7 hand-read fused per area: s12/rollout_table.txt (cohorts 5-14: 3..26) + confirm_cohort15 (19) + confirm_cohort16 (8); ledger: s14/table.txt + s14/ledger_delta.json; vs g11: s12/measure_s12.json fusions S9 against s14/measure/S14_*.json fusions S14 (208 same, 18 better, 6 worse); negatives: every rollout table's neg column -->

## 5. What remains, named

1. **Printed house numbers: done.** One agency's flats at different house numbers of one street, let side by side, used to group together. S9 reads the house number an advert prints (E240–E242) and S12 corrected which of the two numbers in a Czech address is the building and which the entrance (E271); the coal-basin area's six groups are separated.
2. **Two adverts of one property under two agency order numbers** are merged, and that is correct: on two areas, 15 of 16 such pairs were one property re-listed under a new number. Treating the numbers as a fact would split hundreds of true duplicates; it stays refused.
3. **Rezidence Bulhary's packages stay merged (refused, D78).** One development sold as house-and-plot packages of 530, 689 and 764 m², one after the other. Packages on sale at the same time have been read as two offers since S7; for packages offered in sequence the reading would cost 510 certain duplicates over fifteen areas and still leave one of the two groups merged.
4. **Adverts located only to the town, from the same agency, with identical text** cannot be told apart by anything printed. Under your ruling they merge.
5. **Two switches** hold a category at "propose only" (the engine suggests, you decide) while the rest merges: rentals (`w29_rentals_hold.json`) and land (`w29_land_hold.json`). Their cost in recall: on the Most / Břeclav area 93 % falls to 71 % with the rentals hold and to 80 % with the land hold (measured on S13, whose result there g12 repeats exactly); on the Lipno area 93 % falls to 82 % and 81 %. There the rentals hold removes one mixed group (the hotel rooms) and the land hold none. Both ship switched off.
6. **The Herínk halls stay merged (refused, D76).** Two identical 1,106 m² halls of one park, let on bazoš under two agency numbers at two rents at the same time. A rule reading that combination as a difference would split 12 certain duplicates, 7 of them real (5 are one Karlovy Vary building posted under two numbers at two prices).
7. **Named villas stay merged (refused, D84).** The Lipno villa groups of §4; the reader is kept in the code, switched off.
8. **The reference is not perfect.** Some "certain" pairs are two units (§4), so part of the last few points of recall cannot be won and should not be.
<!-- 1: PROGRAM.md W24 (E240-E242), E271; s14/rollout_table.txt cohort11 cured 9. 3: PROGRAM.md D78, M647 (s13/e292_bulhary.json). 5: s13/rollout_table.txt cohort15 S13/S13H/S13L 93.01/71.05/79.66; s14/rollout_table.txt cohort16 S14/S14H/S14L 92.56/81.89/80.55; confirm_cohort16/table.txt fused S14 4, S14H 3, S14L 4. 6: PROGRAM.md D76, E276. 7: D84, E296. -->

## 5a. Why the accuracy waves stop here

- **Sixteen waves since your ruling on 2026-09-21**, eight of them (W22 to W29) in the last three days.
- **Each wave now buys almost nothing.** The first wave after the ruling lifted recall on the 140-town region from 38 % to 75 %. S10, S11 and S12 each added 0 to 1.2 points per area, S13 0.7 to 5, and S14 nothing on fifteen areas and 0.04 points on the sixteenth, because it was built only to tighten (every rule in it can only add a reason to keep two adverts apart, D82).
- **What is left has no common shape.** The next point of recall would need roughly one rule per remaining miss, and every rule that merges more risks a merge across a stated fact: while building S13, two candidate versions merged across a fact seven times before they were narrowed, and in S14 even a tightening reader (the villa name) produced a new wrong merge.
- **No unseen ground is left.** All sixteen areas have now been used to build rules, so a further wave that merges more would need a seventeenth area, exported and read from scratch, to be confirmed honestly.
<!-- PROGRAM.md wave rows W14-W29 (dates), W15 (region 16,809 -> 20,588 of 22,421; g7 8,618), W25-W29 (per-wave recall), W28 (seven merges across a fact in candidate builds), W29 + D82 ("no seventeenth cohort: all sixteen are dev ground"), D83/D84 -->

## 5b. S15 (w30): the trial's leftovers, proposed and not yet confirmed

Reading the live trial's remaining duplicates against the g13 export found one engine defect and three questions for you. **The defect:** the engine only compared adverts' size and layout inside the same town *part* when the location resolver had found one, so an advert placed only in "Jablonec" never met the same flat placed in "Mšeno nad Nisou". S15 adds a town-wide comparison, following your location rule (town parts split the town only in Praha, Brno and Ostrava). It also reads a floor and a building height that both differ by one storey in the same direction as one counting habit, not two differences.

| version | areas | recall change (points) | certain pairs gained / lost | mixed groups made worse | pairs proven different merged | trial: operator pairs lost vs g7 |
| --- | --- | --- | --- | --- | --- | --- |
| g12 (S14, w29) | 16 | (the §4 table) | — | — | 0 | 23 |
| S15 (w30) | 16 | −0.02 to +0.12 (two areas lower: 140-town region −0.01, Ostrava −0.02) | 240 / 128 | 0 | 0 | 23 |
| S15 (w30) on the unseen area 17 | 1 | +0.02 | 26 / 21 | **2 (refused)** | 0 | — |
| S15b (w31 = g12 + the town-wide comparison + the cellar reader) on the unseen area 18 | 1 | 0.00 | 0 / 0 | 0 | 0 | — |

Because S15 can merge more, it was confirmed on a fresh, unseen area (17): there it joined two pairs of different units (two neighbouring offices, and two flats on different floors of one Klatovy house), so it was **refused** (D92: the floor-and-height counting rule caused both). The rebuilt version **S15b (w31)** keeps the town-wide comparison and the cellar reader, drops that rule, and **passed** on another unseen area (18: Prostějov, Přerov, Kolín, Beroun, Vyškov, Žatec, Tachov, Litoměřice, Praha-Nusle), where every one of its 14 new groups was read as one property (M680). Two close calls there are named for your review: a Vinary building plot offered whole-or-half in June and as its front half in September, and one Penčice house spelled on three street names. Three readings are **built and switched off, waiting for your ruling**, each measured on all sixteen areas: a building's storey count alone as a difference when floor, size, layout and price agree (D87: the Kolmá, Mozartova and 122 m² flats of the trial), a price that includes the agency's commission on an advert whose photos and house number match (D88: Pražská 930/47; as specified it does not yet join that flat, because one of its adverts files no house number), and a floor and building height that differ by one storey in opposite directions (D89: the Mechová flat). A fourth trial case, Krkonošská 353, was not an engine problem: the production-merge adapter refused to undo an old merge because it contained a pair you had ruled "same", although the engine keeps that pair together; it now proceeds when the engine re-joins the pair (E907).
<!-- s15/rollout_table.txt (S14 vs S15 and the ablation rows), s15/trial_g13_s15.json (g13), s15/trial_tier_s15.json (operator tier), s15/preregistration_s15.json; PROGRAM.md W30, M667-M672, D87-D90, E907 -->

## 6. Latency, storage, spend

- **Latency.** The batch engine decides a listing in under a second. The real-time shadow lane runs on GitHub's ten-minute schedule, which in practice fires hours apart; photo-dependent merges wait a median of 2.5 hours for the hourly fingerprint jobs. *Correction, 2026-09-24:* the photo fingerprint (pHash) has been taken at download time since July (PR #692), and its hourly job is only a backstop. That does not fit the program's measurement that about 80% of new listings were decided before any of their photos had a fingerprint, and the cause is not yet known: either the download-time fingerprint is failing, or the photos are stored later than the program measured. The 2.5-hour median was measured to the first room tag (CLIP, hourly at :40), not to the fingerprint. Every image run now reports `phash_missed`, the photos stored without a fingerprint; it should read 0.
- **Storage.** The program's tables hold about 250 MB for the trial area across five kept generations. Corpus-wide, at today's retention, the store would grow about 3.4 GB a month plus about 9.6 GB to seed; cutting the stored reject pairs brings that down by roughly two thirds.
- **Spend.** About $52 of $200 for development; $0 per month at rollout. The LLM "fact card" experiment cost $2.32 and was refused: no model beat the free text readers cleanly.

## 7. The five decisions, and my recommendation

1. **Production merges.** Everything so far writes only to the shadow tables. The adapter that turns the engine's groups into real merges is built and switched off (PR #1605). It writes through the existing merge function, which carries your notes, tags, collections and pipeline cards onto the survivor; it first runs as a *dry run* (lists every merge it would make and changes nothing); each group can be undone on its own; and it refuses any group that goes against a "different" verdict you recorded, a must-not-link pair (two adverts marked never to be joined) or a Browse asset link (properties you marked as different units of one building). It went through several rounds of adversarial review, each by a fresh reviewer trying to break it. *Recommendation, unchanged: start with sales in the trial area, watch the review pages for a week, then widen.* Before the engine merges in an area, the same dispatch undoes the old engine's merges there (`retire_legacy=1`, a temporary step deleted in W5): only groups still as the old engine left them (none you have already taken an advert out of), wholly inside the area and of the deal types being merged (sales during the trial; a merge that put a rental onto a sale is undone either way), and never one that would separate two adverts you marked "same" or put an advert beside one you marked "different", nor one the new engine still sees as one property across the area's edge (it waits for the country-wide step); nothing is undone unless the engine's run has groups to merge there. The undo records no verdict of yours in either direction; the rest are listed, not touched.
2. **Rentals and land.** *Recommendation: merge both. On the last two areas the blind reads found 0 wrong of 30 rental pairs each time and 0 wrong of 50 and 40 land pairs, with S12 to S14 confirmed. The two holds (§5.5) stay available if you want sales alone first.*
3. **True real-time.** Built and switched off (PR #1604): the always-on worker runs the same shadow pass. Decisions then arrive about 5 to 6 minutes after a change, because the engine lets a new listing settle for 5 minutes and a photo-based merge waits for the photo fingerprints. Sub-minute decisions would need a change to the engine itself, a separate decision. *Recommendation: switch it on after production merges have run for a week on the batch lane.*
4. **Whole corpus and its storage.** *Recommendation: cut stored reject pairs first, then widen by region, watching the 400 MB guard the lane enforces.*
5. **Photo latency.** The photo fingerprint (pHash) has been taken at download time since PR #692, yet the program measured about 80% of new listings decided before any of their photos had one; the cause is not yet known (§6). *Recommendation: decide nothing yet. First read `phash_missed` on the image runs and measure the time from a listing's first sighting to its first fingerprint. If the fingerprint is failing, fix that; if the photos are downloaded late, the fix is the download schedule. Running the room tagger (CLIP) inside the image download is a separate question, because it adds torch, a large new dependency (rule 7).* (see PR #1602)
<!-- 7.1: branch feature/autodedup-apply-adapter (a3c07b8f, d6a14f99, fec46d67, 08578f6c, 796c8b9a). 7.2: confirm_cohort15/REPORTS.json agent1 + confirm_cohort16/verdicts_pairs.json counts. 7.3: branch feature/realtime-autodedup-lane, docs/design/realtime-scrapers.md (SETTLE_LAG_S 300 s, ~5-6 minutes). -->

## 8. How to look at it yourself

The review pages open on the latest scored generation: g11 today, g12 once this update is scored. The dropdown switches generations; g7 is the engine from before the ruling. The largest groups and the new-development groups are where a mistake would show. Every "different" verdict you record is honoured by every later generation and is never shown as a merge proposal again. Once production merges start, each property's page lists the adverts merged into it in the **Sloučené inzeráty** ("merged adverts") section (PR #1603).
