# remax detail extraction — root cause, fix, and the three deliberate deferrals

**Status:** description fixed; price/broker/backfill scoped but NOT shipped · **Date:** 2026-08-04

## 1. What was actually wrong

`scraper/remax_parser.py` read `.pd-detail-text`, falling back to `#popis`. **Neither selector
exists on any remax page** — 0/300 stored `portal_raw_pages` detail pages carry either, and no
live fetch has ever produced one. remax therefore sat at **0.0% description across 11,091 rows
since 2026-06-01**, the portal's first day. It never worked; this is not drift.

The real container is server-rendered in the first response:

```
div.pd-base-info__content-collapse-inner div[ref="content-inner"]
```

Present on **300/300** stored pages. Verified end-to-end on a random live sample of 12 listings
that our DB records as description-NULL: **12/12 recovered**, 399–2,984 characters of genuine
Czech listing prose.

### The hypothesis that was wrong, and why it was seductive

The working hypothesis was "remax client-renders the description, so we need a headless browser."
**That was false**, and it nearly cost a heavyweight dependency (rule #7) to fix five lines of
parsing. Three independent checks appeared to confirm it and all three were bad:

1. A scan for long text blocks sorted candidates **ascending** and printed the shortest — the
   footer. The 1,217-character description was in the same list, at the other end.
2. A grep for `Popis`/`popis` returned 0 — but remax has **no such heading**; the section is
   unlabelled. `popis` matches at most 5/400 pages, and those are the Czech word *popisné*
   ("číslo popisné", house number) in address markup. It is not a marker on this site.
3. A consent/session-gate test: a jar warmed from the homepage returned a **byte-identical**
   response (66,503 bytes). True — but the description was inside those bytes the whole time.

The lesson generalises beyond remax: **the page element is only a read-more CSS collapse over
already-rendered text.** "Vue is on the page" is not evidence that content is client-rendered.
Confirm absence by parsing, never by grepping for a label you assume exists.

## 2. Assumptions invalidated

- **`scraper/remax_parser.py`'s module docstring** describes a DOM the server does not serve. The
  `.pd-detail-text` / `#popis` selectors appear to have been **invented or copied, not observed** —
  they match no state of the page, pre- or post-JS.
- **`tests/scraper/test_remax_parser.py`** planted `<div class="pd-detail-text">` in its inline
  fixture and asserted the text back. Tautological w.r.t. the selector, so it passed green for
  two months over a field that was 0.0% in production. Fixed here to real markup; the
  corpus-backed test lives in `tests/scraper/test_portal_media_fixtures.py`.
- **`og:description` is a trap, not a fallback.** It is REMAX marketing boilerplate
  ("Spolehněte se na jedničku mezi realitkami…"), byte-identical on every listing. Substituting it
  would look like a fix while poisoning all 11,091 rows with one constant string. There is now a
  regression test asserting the parser never returns it.
- **`scripts/fetch_and_anonymize_fixtures.py` does not scrub agent names** (documented, but easy
  to miss) — and this repo is **public**. The committed remax fixture was hand-scrubbed: broker
  name, profile slug, and the numeric ids embedded in the agent photo URL.

## 3. Three deliberate deferrals

These are real defects found during the investigation. Each is deferred for a stated reason, not
overlooked.

### 3.1 Price — the fallback is dead, and the obvious repair is HARMFUL

`data-advert-price` (the primary) is absent on **132/300** stored pages. The `.pd-price` fallback
is absent on **300/300**. So ~44% of remax listings have no price path at all — which is most of
the live 31.3% `price_czk` gap.

The obvious replacement, `.pd-table__value--price`, is present on 300/300 **and must not be used
as-is.** It renders three different units and `_parse_price` scrapes digits naively:

| page | cell text | `_parse_price` yields | truth |
|---|---|---|---|
| Brno land, 1,276 m² | `7 759 CZK/ za m2` | **77592** | 7,759 Kč **per m²** |
| Litoměřice office | `2 429 CZK/ za měsíc` | 2429 | 2,429 Kč/month ✅ |
| Úvaly house | `7 800 000 CZK/ za nemovitost` | 7800000 | ✅ |

The land case is doubly wrong: it swallows the `2` from `m2` into the number, **and** a per-m²
figure written into `price_czk` reads as a total. A unit price stored as a total is far worse than
a NULL — it poisons Kč/m² statistics, estimation comparables, Browse sorting and price-drop
watchdogs. This is the already-known "Kč/m² garbage prices" hazard.

**Prerequisite:** a unit-aware `_parse_price` that reads the `za m2` / `za měsíc` / `za nemovitost`
suffix and either maps it onto `price_unit` or rejects the value. Then the fallback becomes safe.

### 3.2 Broker — tractable, but it moves downstream machinery

remax is also **0.0% `broker_identity_id`**, same root cause: the data is on the page and unread.
`div.pd-sidebar__agent-info` is present on **300/300** and carries the agent name, a profile URL
(`/reality/{office-slug}/{agent-slug}/`) and a photo URL with numeric ids
(`.../uzivatele/{id}/{photo}_{agent}_photo…`) — the same photo-URL-derived key realitymix already
uses (`_MAKLER_IMG_RE`), so the pattern is established.

Deferred because it writes `broker_identities` and triggers firm rollups + leaderboard recompute —
a different risk profile from a pure parser change, deserving its own verification.

**Constraint to respect:** the operator has declared `broker_phone` an intentional zero. The agent
block exposes a phone; **do not start collecting it.** Extract identity only.

### 3.3 Backfill — blocked on a policy decision, not on code

11,091 listings could be repaired offline from `portal_raw_pages` with no refetch. But
**`description` is in `_HASH_FIELDS`** (`scraper/scraped_listing.py:34-41`), unlike media. So
unlike the media backfill, this one **cannot be snapshot-free**:

- The content hash lives on `listing_snapshots` and is compared against the latest snapshot. Set
  the description and the hash genuinely changes, so one snapshot per listing is appended —
  whether at backfill time or on the listing's next natural re-scrape. It is deferred, not avoided.
- Consequence: **~11k remax listings would stamp a fresh `last_change_at`** and surface as
  "changed today" in Browse recency filters — an operator-visible artefact of our extraction
  changing, not the listing changing.

`scripts/reextract.py` currently states snapshot-safety *by construction* because `_HASH_FIELDS`
excludes media. Extending it to `description` without qualification would silently falsify its own
contract. The correct shape is a **per-field hashed/unhashed declaration** in the driver, so a
hashed field requires explicit opt-in rather than inheriting a guarantee that does not apply.

**Operator decision required** — see §5.

## 4. What NOT to do

- **Do not add a headless browser for remax.** The content is server-rendered. It would be a
  heavyweight dependency solving a non-existent problem.
- **Do not use `og:description`.** Constant boilerplate; a regression test now blocks it.
- **Do not wire `.pd-table__value--price` before `_parse_price` is unit-aware.** It produces
  garbage on land plots.
- **Do not collect broker phone numbers** — declared intentional.
- **Do not "fix" the remaining description gap by loosening the selector.** 300/300 coverage means
  a miss is genuinely a miss and should be visible, not papered over.

## 5. Open questions for the operator

**Q1 — Backfill the 11,091 descriptions, accepting ~11k snapshots and a `last_change_at` bump?**
- (a) **Backfill now** — full history recovered immediately; ~11k remax listings appear as
  "changed today" in Browse recency for one day. *Recommended:* the artefact is one-off and
  cosmetic, the data value is permanent.
- (b) **Let it land naturally** on each listing's next detail re-fetch — no concentrated
  artefact, but recovery is as slow as the refetch cadence and inactive listings never recover.
- (c) **Don't backfill** — new listings only.

**Q2 — Priority for the two remaining remax defects?** Unit-aware price (unblocks ~44% of remax
prices, and the unit-awareness likely benefits other portals) versus broker identity (unblocks
remax broker analytics). Both are real; neither is urgent.
