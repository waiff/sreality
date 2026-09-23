# AUTODEDUP — rollout report

**For:** the operator, to decide on rollout. **Date:** 2026-09-23. **Program:** `docs/design/autodedup/PROGRAM.md` holds every rule, measurement and decision by number; this page is the summary.

## 1. What was built

An engine that finds the same real-world property across the nine portals and across time and decides, on its own, whether two adverts are one property. It runs in **shadow mode**: it writes its groups into its own tables (`autodedup` schema) and the review pages read them. It has never written a merge into production data.

It costs **$0 per month** to run. Every signal it uses is already produced by the platform (scraped attributes, price history, resolved location, broker identity, photo fingerprints, CLIP vectors, room tags). No LLM is called at decision time. LLMs were used only during development, to make labels and to test ideas; the total development spend is about **$52 of the $200 budget**.

## 2. The ruling that shaped it

On 2026-09-21 you ruled that **identical units merge**: two adverts are one entry unless a *stated fact* tells the units apart — a printed unit number or code, a floor, a headline or plot area, a price that is not one advert's own price path, a parcel number, a street or house number, a room or desk count, a deposit or service charge on rentals let side by side, a stated interior difference. A *false merge* is a merge across such a fact. Everything after that ruling was measured against it.

## 3. How it was tested

Eleven areas of the country were exported, about 20,000 adverts each, none overlapping. Each new engine version was tuned on the areas already used and then **confirmed once on an area no rule had seen**, by independent readers who:

- ran a hunt for mixed groups with their own text readers and read every flagged group by hand;
- read 160 newly merged pairs blind, from the raw adverts, before seeing anything the engine said;
- read every group on the hazard ground of that area (new developments, land, side-by-side rentals, the largest groups).

The bar for each confirmation was written down before the area was opened. "Today's engine" below is g7, the engine that was live when you made the ruling.

## 4. Results

### Recall — certain duplicates ending up in the same group

"Certain duplicates" are pairs proven without any label: a shared rare agency order number, four or more identical non-stock photos, or byte-identical text.

| Area | Adverts | Today's engine (g7) | Live shadow engine (g10) |
| --- | --- | --- | --- |
| Jablonec / Turnov / Vysočany (trial, labelled) | 5,687 | 50 % | 75 % |
| 140 towns of the Jablonec–Semily region | 15,017 | 38 % | 92 % |
| Olomouc + ring, Praha-Holešovice / Hlubočepy | 17,306 | 37 % | 89 % |
| Plzeň + ring, Praha-Hloubětín | 19,886 | 55 % | 87 % |
| Hradec Králové / Pardubice + belt, Praha-Letňany | 19,641 | 51 % | 87 % |
| Karlovy Vary / Mariánské Lázně / Cheb, Praha-Modřany | 19,874 | 38 % | 87 % |
| Ústí nad Labem / Teplice + districts, Praha-Hostivař | 19,936 | 30 % | 94 % |
| Brno quarters + ring, Praha-Záběhlice | 18,889 | 69 % | 86 % |
| Ostrava / Opava + ring, Praha-Michle | 18,214 | 65 % | 88 % |
| South Bohemia's seven district towns, Praha-Strašnice | 18,450 | 71 % | 88 % |
| Karviná / Havířov / Frýdek-Místek / Třinec / Č. Těšín, Kroměříž, UH, Praha-Vinohrady | 19,630 | 66 % | 85 % |

Rentals alone: 81–91 % on every area. Adverts located only to the town: 87–97 % where today's engine finds 5–50 %.

### Safety — read by hand on areas the engine had never seen

| Area | Mixed groups found, today's engine | Mixed groups found, new engine | Of those, neighbouring units of one development or plots | Blind read of 160 new merges: wrong |
| --- | --- | --- | --- | --- |
| Karlovy Vary | 7 | 1 (g7 makes it too) | 1 | 0 |
| Ústí / Teplice | 8 | 4 | 1 | 0 |
| Brno | 13 | 7 | 3 | 3 |
| Ostrava | 11 | 6 | 1 | 16 (all rentals; fixed, see below) |
| South Bohemia | 15 | 7 | 4 | 1 |
| Coal-basin towns / Kroměříž / Vinohrady | 3 | 6 (all one shape, see §5) | 0 | 2 |

- **Merges across a printed, provable difference:** 0 on every area, for every version since the ruling. Today's engine makes 1–4 on three areas.
- The Ostrava result exposed one weakness: two flats in the same house let side by side on one portal at similar rents. Version S7 reads the deposit, charges, flooring, sanitary arrangement and renovation state as facts; on the South Bohemia area 60 such pairs were then read blind with none wrong.
- Two groups of 16 and 24 identical adverts of one turnkey project, all the same size, plot and price with identical text, are **one entry each** under your ruling. They are flagged in the ledger as the ruling's own case.

### The correction to what was said before the ruling

The engine that was live then was reported as having "zero known false merges". That was true against the labels available. Under the hand hunts above, it carries 3–15 mixed groups per area, including houses "č. 13, 14, 15" of one project and flats "202, 205, 209" of one residence. The new engine separates every one of those and makes fewer of its own.

## 5. What remains, named

1. **Printed house numbers.** One agency's flats at different house numbers on one street, let side by side, group together (6 groups on the last area). The house-number columns are empty on every row of every area, but the number is printed in the address string. A reader for it is being built and confirmed (S9).
2. **Two adverts of one property under two agency order numbers** are merged, and that is correct: on two areas, 15 of 16 such pairs were one property re-listed under a new number. Treating the numbers as a fact would split hundreds of true duplicates; it stays refused.
3. **A property sold as two land packages** by one seller within minutes (house + 2,830 m² vs house + 1,483 m²) — read as two offers since S7.
4. **Adverts located only to the town, from the same agency, with identical text** cannot be told apart by anything printed. Under your ruling they merge.
5. **A per-category switch** exists (`w22_rentals_hold.json`): rentals can be held at "propose only" while sales merge. On every area it would have prevented every rental mistake at a cost of 10–45 points of overall recall. It ships switched off; it is yours to flip.

## 6. Latency, storage, spend

- **Latency.** The batch engine decides a listing in under a second. The real-time shadow lane runs on GitHub's ten-minute schedule, which in practice fires hours apart; photo-dependent merges wait a median of 2.5 hours for the hourly fingerprint jobs.
- **Storage.** The program's tables hold about 250 MB for the trial area across five kept generations. Corpus-wide, at today's retention, the store would grow about 3.4 GB a month plus about 9.6 GB to seed; cutting the stored reject pairs brings that down by roughly two thirds.
- **Spend.** About $52 of $200 for development; $0 per month at rollout. The LLM "fact card" experiment cost $2.32 and was refused: no model beat the free text readers cleanly.

## 7. The four decisions, and my recommendation

1. **Production merges.** Everything so far writes only to the shadow tables. Going live means the engine's merge groups write through the existing merge function, which already carries your notes, tags, collections and pipeline cards onto the survivor and can be undone group by group. *Recommendation: start with sales in the trial area, watch the review pages for a week, then widen.*
2. **Rentals.** *Recommendation: hold rentals at propose-only (the switch in §5.5) until the house-number reader (S9) is confirmed, then merge them too.*
3. **True real-time.** Sub-minute decisions need the same code inside the always-on worker, which changes the production image. *Recommendation: after production merges have run for a week on the batch lane.*
4. **Whole corpus and its storage.** *Recommendation: cut stored reject pairs first, then widen by region, watching the 400 MB guard the lane enforces.*
5. **Photo latency.** Fingerprinting photos at download time removes the 2.5-hour wait. *Recommendation: yes, it is a small change in the image drain and helps every consumer of the fingerprints.*

## 8. How to look at it yourself

The review pages open on the latest generation (g10). The dropdown switches generations; g7 is the engine from before the ruling. The largest groups and the new-development groups are where a mistake would show. Every "different" verdict you record is honoured by every later generation and is never shown as a merge proposal again.
