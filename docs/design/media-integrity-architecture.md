# Media integrity architecture — portal extraction contracts, yield observability, generic re-extraction

**Status:** proposal · **Date:** 2026-08-04 · **Trigger:** operator reported missing images across
multiple portals in Browse.

Every claim marked ✅ was verified against production or live portal markup during the
investigation. Claims marked ⚠️ are mechanism-sound but measured on a small sample and should be
re-measured by `scripts/reextract.py --dry-run` before being used to size work.

---

## 0. The one-sentence statement

Nine portals each hand-roll their own image-URL extraction into a **silent** write path, and the
entire monitoring model measures *whether the pipeline ran*, never *what it produced* — so total
media loss on a portal is arithmetically indistinguishable from perfect health.

---

## 1. Diagnosis

### 1.1 ACUTE — realitymix: 100% media blackout on new inventory, 19 days ✅

`scraper/realitymix_parser.py:108`:

```python
_IMG_RE = re.compile(r'https://st\.realitymix\.cz/i/\d+/\d+/nab_\d+\.(?:jpe?g|png|webp)', re.IGNORECASE)
```

realitymix began emitting gallery URLs over plain `http://` around 2026-07-16 18:00 UTC. The
pattern hard-codes `https://`, so `_images()` (line 463) returns `[]`, `parse_detail` puts `[]`
into `raw["image_urls"]`, and `db.record_media(conn, pk, [])` returns 0 with **zero SQL executed
and zero log lines**.

✅ Verified on a live detail page fetched today: **48 `http://st.realitymix.cz` URLs, exactly 1
`https://`**. That listing has 18 full-size photos; the production regex matches none of them.
A scheme-agnostic pattern matches all 18, correctly scoped to the right listing.

| day | zero-image rate for new realitymix listings |
|---|---|
| 07-15 | 2.3% |
| 07-16 | 24.4% |
| **07-17 → 08-04** | **100.0%, every day** |

**It is not a markup change.** The DOM is byte-identical (`.gallery__items`, `data-src`,
`<img src>`). Only the scheme moved, and `http` 301-redirects to `https` serving identical bytes —
the scheme carries zero semantics. A fixer told "the markup changed" would edit the wrong code.

**Blast radius:** 8,109 listings with zero image rows, **6,905 still active**. ~2–3% would have
been photo-less anyway (pre-cutover baseline), so ~97% is bug-attributable.

**No retroactive loss** ✅ — there is no `DELETE FROM images` anywhere in `scraper/`, `toolkit/`,
`api/` or `migrations/`; `record_images` is insert + `ON CONFLICT … DO UPDATE … WHERE
images.storage_path IS NULL`; and the empty list short-circuits before any SQL. Rule #3 holds:
the loss is forward-only.

### 1.2 ACUTE — idnes: the majority of gallery anchors silently discarded ✅

`scraper/idnes_parser.py:551-560`:

```python
for a in tree.css('a[data-fancybox="images"]'):
    href = a.attributes.get("href")
    ...
    href = href.split("?")[0]
    if "1gr.cz" not in href and "sta-reality" not in href:
        continue
```

iDNES migrated gallery delivery to a first-party service,
`https://reality.idnes.cz/file/thumbnail/{mediaId}?profile=front_detail_article_big_fit&gt=r`.
That host matches neither allow-list token, so every such anchor is dropped. The allow-list is
unchanged since the portal's first commit (8d89e595, 2026-05-29) — upstream drift meeting a
static client-side filter.

✅ **Measured at anchor level over stored `portal_raw_pages` HTML** (the bytes the scraper
actually parsed), 100 most recent detail pages:

| | count | share |
|---|---|---|
| gallery anchors | 1,655 | |
| **first-party `reality.idnes.cz/file/` → dropped** | **1,044** | **63.1%** |
| allow-listed `1gr.cz` / `sta-reality` → captured | 611 | 36.9% |
| matterport | 0 | |
| video (`.mp4`) | 8 | |

**Methodology note, and a correction worth recording.** An earlier pass estimated 41% and a
same-day live-fetch sample suggested near-zero. Both are unreliable: comparing a *live* page
fetched today against image rows written days ago conflates "photos added since" with "photos
dropped at parse", and whole-document substring counting overcounts badly (`reality.idnes.cz/file/`
appears 177% as often as there are gallery anchors, because it also appears in thumbnails, related
listings and `og:image`). **The only valid measurement is anchor-level over stored HTML.** The
first-party share is also a monotonic ramp by listing vintage, so any single sample dates quickly.

✅ **The obvious minimal fix would silently store dead URLs.** The parser strips the query *before*
the host check. Probed live today on a real first-party URL:

| URL form | HTTP | bytes |
|---|---|---|
| `…/file/thumbnail/{id}` — query stripped, i.e. what the parser would store | **404** | 3 |
| `…/file/thumbnail/{id}?profile=front_detail_article_big_fit&gt=r` | **200** | 793,480 |
| `…/file/thumbnail/{id}?profile=front_detail_article_big_fit` | **200** | 793,480 |

The path is extension-less and the rendition lives **entirely in `?profile=`**. Merely adding
`reality.idnes.cz` to the allow-list stores 404s, the R2 downloader fails all of them, and the
failure reads as an ordinary CDN problem. `?gt=r` is droppable.

**Two traps in the repair:**
- `a[data-fancybox="images"]` is **not photo-exclusive** — `.mp4` tour anchors carry the same
  attribute (8 in the sample above), and `my.matterport.com` anchors do too. `media.is_image_url`
  accepts matterport URLs (no `/video/`, no video extension), so a naive "trust the anchor" fix
  routes 3D tours into `images`.
- The gallery lazy-loads: only photo 1 has an eager `img src`; the rest carry
  `src="…/no-image-gallery.png"` with the real URL in `data-lazy`. Reading `img[src]` would ingest
  byte-identical placeholder PNGs, which pass `is_image_url` **and** `is_image_bytes`, then collide
  at identical pHash / CLIP cosine 1.0 — and rule #15 auto-merges non-byt listings at ≥0.98. That
  is a property-graph corruption event, not a display bug. **No placeholder guard exists anywhere
  in the ingest path.**

### 1.3 CHRONIC — the publish-before-photos race, with no repair lane ✅

Portals let a seller publish and upload photos minutes later; our detail fetch wins that race and
nothing ever revisits. Timed to the second (bezrealitky CDN filenames embed an upload epoch):

| listing | photo epoch | our `first_seen_at` | gap | image rows today |
|---|---|---|---|---|
| 1050316 | 2026-07-29 08:20:04Z | 08:17:03Z | **+3m01s** | 0 |
| 1050654 | 2026-07-30 06:50:15Z | 06:49:08Z | **+67s** | 0 |

Both have 3 live photos and 1 snapshot, six days on.

**Why nothing recovers them.** `INSERT INTO images` exists at exactly two places repo-wide —
`scraper/db.py:1036` (`record_images`) and `scraper/db.py:2300` (`_BATCH_IMAGES_SQL`) — and both
need a fresh detail parse in hand. The only scheduled repair,
`scripts/refresh_stale_image_urls.py`, gates on:

```sql
AND EXISTS (SELECT 1 FROM images i WHERE i.listing_id = l.id AND i.storage_path IS NULL)
```

A zero-row listing satisfies no `EXISTS`. ✅ Proof it has *never* selected one:
`images_refreshed_at IS NULL` for **100%** of the affected population, while the sweep is
demonstrably alive (33,858 listings stamped all-time, 110 in the last 24h).

A sub-mechanism: ⚠️ 157 of the idnes zero-image rows have a non-empty `image_urls` containing only
an `.mp4` — idnes publishes the video tour first, stills later. `split_media_rows` correctly routes
it to `listing_videos`, leaving zero image rows. Same race, one step earlier.

**Standing backlog, all-time active zero-image:** realitymix 6,905 · idnes 5,955 ·
ceskereality 1,742 · bazos 1,105 · bezrealitky 93 · sreality 11 · remax 2 = **~15,800**.

### 1.4 STRUCTURAL — three independent safety nets, all blind by construction ✅

**(a) The write path is silent in three places, not one.** `record_media` is *not* the single
chokepoint. sreality bypasses it entirely (`write_detail_batch`, guarded by `if image_objs:`) and
calls `record_images` directly from `main.py:631`, `main.py:1368`, `freshness.py:83`,
`url_parser.py:68`. A fix applied only at `record_media` leaves the largest portal uncovered.

**(b) Every image health metric has the wrong denominator.**
- `image_storage_overview_mv` (migrations 110/115/236/354): `count(storage_path)/count(id)` over
  **image rows**, grouped by `(category_main, category_type)` — **no source dimension at all**. A
  listing with zero rows contributes 0/0 and moves nothing. It can even read as an *improvement*.
- `images_failure_overview_mv` was narrowed to failures-only by migration 181 — a row that never
  existed cannot fail.
- Consequence: **realitymix read 99.9% stored during a total discovery blackout.**

**(c) The alerting spine has no portal dimension; the portal dashboard has no alerting.** Disjoint
registries:
- `scraper_health_checks_mv` — 16 per-source checks (`liveness`, `runs_completing`,
  `new_listings`, `field_null_drift`, …). **Not one looks at images.** Its only consumer is the
  browser (`frontend/src/lib/queries.ts:1658`). A `fail` verdict emits nothing.
- `pipeline_check_results` via `scripts/verify_pipeline.py` — 12 checks, the only producer of
  `system_health` notifications. All 42 rows ever written concern LLM credit, the dedup engine, or
  R2 parity. **Zero concern a portal, a field, or a yield.**
- During the blackout realitymix graded **13 pass / 3 warn / 0 fail**.

**(d) `field_null_drift` cannot detect forward-only breakage.** `data_quality_by_source` computes
`pct_populated` over `WHERE l.is_active` — the **cumulative** inventory. A parser that stops
extracting only degrades new rows, so the cumulative percentage decays at
`new_rows / active_inventory`: for realitymix, 6,905 of 48,012 = 13.4 pts over 19 days =
**0.75 pts/day** against a 5-pt warn threshold. It can only fire on a failure that *rewrites
existing rows*. ✅ Additionally, `data_quality_by_source` **tracks no image field at all** — the
tracked fields are `disposition`, `ownership`, `furnished`, `has_balcony`, … So there are **two
independent reasons** it could never have fired.

**(e) CI cannot see upstream drift.** `tests/scraper/test_realitymix_parser.py` plants
`https://st.realitymix.cz/...` at lines 79-81 and asserts those exact strings back at 207-209 —
tautological w.r.t. the scheme. `tests/scraper/test_idnes_parser.py:98-99` hardcodes only
`sta-reality2.1gr.cz`. The real-fixture harness
(`tests/scraper/test_source_parsers/test_real_fixtures.py` + `scripts/fetch_and_anonymize_fixtures.py`)
is well built but **unpopulated** — `tests/fixtures/source_html` does not exist, so every case
`pytest.skip()`s — and it targets the LLM `source_parsers`, not the nine scraper portal parsers.
CI ran green on every push for 19 days.

**(f) The read path cannot distinguish "no photos" from "we failed".** `images_public` does not
project `unavailable_reason`; no consumer in `queries.ts` filters on renderability;
`imageUrl.ts:49-56` emits the raw portal URL when `storage_path` is NULL; `ImageCarousel.tsx:73-75`
handles the resulting 404 with `visibility:'hidden'` — no placeholder, no auto-advance, and the
`"1 / 18"` counter keeps lying. ⚠️ ~5,400 cards render an empty inset box this way; in ~95% of them
*every* image is unrenderable, so the correct render is the placeholder that already exists.

### 1.5 BENIGN — things that look broken and are not

| Signal | Verdict |
|---|---|
| **bazos 2.8–6.7% zero-image** | **Not a bug.** ✅ Live-verified: these are WANTED ads ("Nenabízí někdo pronájem bytu?") and genuinely photo-less adverts. Trend is *improving* (6.74% June → 4.28% now; avg photos 10.80 → 12.71). **Do not "fix" bazos** — and specifically do not touch `bazos_parser.py:566` (`if source_id and source_id not in src: continue`), which is load-bearing: every such page carries ~10 "Podobné inzeráty" thumbnails belonging to *other* ads. |
| **"idnes galleries collapsed to 1 photo"** | **Refuted — Simpson's paradox.** CZ-domestic exactly-1 rate is flat (6.7% → 6.3%), p50 = 10 photos. The blended shift is a 5.1× volume surge of foreign syndication (785 → 4,006 rows/week), ~80% from one advertiser posting genuine 0–1-photo Spanish coastal listings. |
| **R2 download health (97.6–100%)** | Numerically correct, structurally uninformative — conditioned on rows that exist. **Never use it as image health.** |
| **`is_image_bytes` / `MAX_IMAGE_BYTES`** | Correctly calibrated. 43 `not_an_image` rows across ~9M. `media.py`'s reject-list philosophy is right. |
| **Photo/video split** | Clean. `listing_videos` = 21,840 rows, zero lacking a real video extension. |
| **Image download workflows** | Green, on schedule, not implicated. |

---

## 2. Assumptions invalidated

Ordered by damage done. Each is a live claim in the repo that this investigation falsified.

**A. `CLAUDE.md` rule #6** — *"Images download to Cloudflare R2 (bytes, not just URLs)."*
True but **incomplete as an invariant, and the incompleteness is what hid this.** The rule governs
URL→bytes only. Nothing anywhere asserts page→URL completeness: *"a detail-fetched listing on a
gallery-bearing portal must yield ≥1 image row or be explicitly recorded as photo-less."* Rules
#2/#4/#5 protect the listing row's lifecycle meticulously; nothing protects what the row contains.

**B. `CLAUDE.md` rule #21's implied corollary** — *"one shared framework ⇒ media ingest is
uniformly safe."* False. The framework unified fetch/queue/write/inactive. Media **extraction** is
100% per-portal ad-hoc with no shared contract, no shared validation, no shared shape test — nine
independent single points of failure. And the "single chokepoint" is actually three.

**C. `migrations/176_health_checks_v2.sql` header** — *"Catches a parser silently losing a field
within a day."* False: cumulative denominator + day-over-day delta = 0.75 pts/day vs a 5-pt
threshold. Its live value for realitymix throughout the blackout was `disposition −0.2 pts / pass`.

**D. `scraper/idnes_parser.py:555` `href.split("?")[0]`** — assumes query strings are disposable.
✅ **False, probed live today**: the bare URL is a 404; the rendition is entirely in `?profile=`.

**E. `scraper/media.py` docstring** — *"Reject-list, NOT accept-list… allow-listing would silently
drop legitimate photos."* The principle is **correct and self-indicting**: it is violated one layer
up, at the host/scheme level, in three parsers — `idnes_parser.py:556` (host allow-list),
`ceskereality_parser.py:531` (`"ceskereality.cz" in href`), `realitymix_parser.py:108` (scheme+host
regex). **Two of the three have already tripped.**

**F. `scripts/verify_pipeline.py:417-443` `check_eligibility_funnel`** — returns a hardcoded
`"status": "ok"`. It computes exactly the right shape of per-source data-quality telemetry, on the
alerting spine, every 6h — and throws the verdict away. 122 runs, 0 fails, ever. So "the alerting
system has no per-source data-quality dimension" is *literally false*, which is worse: the
dimension exists and is structurally incapable of ringing the bell.

**G. `migrations/277:38`, `283:37`, `299:196`** — `rebuild_browse_list()` declares
`set statement_timeout = '600s'`. ✅ **Inert, and never once in force**: `statement_timeout` is armed
when the top-level `select public.rebuild_browse_list();` begins, *before* the function is entered.
Every cancellation lands at exactly 120.0s (the cluster value). "Raise the function's timeout" is a
non-fix — set it in the cron command or at role level.

**H. `api/notifications.py:1225-1233`** — `IMAGE_GATE_ZERO_ROWS_FLOOR_MINUTES = 5`, documented as
"zero image rows AND past a short floor ⇒ genuinely photo-less, release". Falsified: listing
1050654's photos landed **67 seconds** after our fetch and it still holds zero rows six days later.
The 5-minute floor is exactly the window the race lives in. The one component that notices zero
image rows treats it as expected and bypasses.

**I. `scripts/refresh_stale_image_urls.py`** — two doc errors: (i) *"Lowest priority so the refresh
never delays genuine new-listing detail fetches"* — repairs are enqueued at the **same**
`QUEUE_PRIORITY_NEW = 0`, and claim order preserves the original `enqueued_at`, so a repair batch
drains *ahead of* every listing discovered after it; (ii) *"`images.listing_id`, the NOT-NULL FK"* —
live schema says `is_nullable = YES`. Zero NULLs today, but the sweep's correctness rests on an
invariant the database does not enforce.

**J. `migrations/015_images_public.sql` is not the live view definition.** The live shape comes from
236/239/335. A fixer following the file path would edit a dead file.

**K. `CLAUDE.md` § Frontend territory** — *"reads `*_public` views [with the anon key]."* Stale
post-Wave-1: live ACLs are `browse_list = {authenticated=r}`,
`images_public/listings_public/properties_public = {authenticated=rDxtm}`; **anon has no grant on
any of them.**

**L. `CLAUDE.md` § Autonomy** — *"CI + branch protection is the autopilot safety net."* True for
code regressions, **structurally false for upstream drift**, which is what happened.

**M. `frontend/src/lib/imageUrl.ts` docstring** — *"other portals serve their bare URLs directly."*
✅ Half false, probed with browser UA ± Referer: bazos 200, bezrealitky 200, mmreality 200,
sreality 401 bare / 200 with transform — but **ceskereality 404, idnes 404, realitymix 404,
maxima 415**. Referer changes nothing, so this is object expiry, not hotlink protection. The
fallback is a coin flip the code treats as a guarantee.

**N. Framing assumptions from the investigation brief itself**, for the record:
- *"realitymix broke because of a markup change"* — upstream yes, **markup no**.
- *"bazos 2.8–6.7% is a defect"* — legitimate classifieds behaviour.
- *"zero-image rate is the defect metric"* — it misses idnes's **partial**-loss cohort entirely,
  which is invisible to every zero-row metric ever built.
- *"R2 download health is GOOD so this is not the problem"* — true, and the most dangerous sentence
  in the brief, because the metric is conditioned on the missing rows.

---

## 3. The architecture

Four layers. Each builds on something that already exists and is sound; where it isn't, it says so.

```
   PARSE TIME          WRITE TIME            DB / COHORT              SURFACES
 ┌──────────────┐   ┌─────────────────┐   ┌──────────────────┐   ┌──────────────┐
 │ MediaExtract │──▶│ portal_write.   │──▶│ listings.        │──▶│ verify_      │
 │ (3-valued)   │   │ write_staged_   │   │  media_advertised│   │ pipeline     │
 │ per-portal   │   │ details()       │   │  media_extracted │   │ check → alert│
 │ GallerySpec  │   │ ← ONE chokepoint│   │                  │   │              │
 └──────────────┘   └─────────────────┘   │ data_quality_    │   │ browse_      │
        │                                 │  snapshots+cohort│   │ projection   │
        ▼                                 │ source_output_   │   │ .media_state │
   reextract.py ◀── portal_raw_pages      │  contracts       │   │ (UI)         │
   (no network)     listings.raw_json     └──────────────────┘   └──────────────┘
```

### 3.1 The extraction contract — and the three-valued answer

Rule #21 says per-portal code is fetcher + parser + config row. The contract therefore splits:
the **obligation** (a type) lives in shared code; the **declaration** (selectors, precedence,
deny-lists) lives in a config row; the **resolver** lives in shared code.

#### The crux: "photo-less" vs "extraction broke"

This needs **two independent signals, not one**. Today `image_urls = []` collapses three different
worlds. Separate *the portal's own assertion of how many photos exist* from *how many we resolved* —
and compute them by **independent code paths**. If the count and the resolution share a filter they
can never disagree, which is exactly the tautology that made idnes's partial loss invisible.

```python
# scraper/media_extract.py
@dataclass(frozen=True)
class MediaExtraction:
    """A parser's media result. NEVER a bare list — the whole incident is that
    `[]` meant three different things."""
    urls: list[str]          # resolved, ordered, deduped
    advertised: int | None   # the PORTAL's own count; None = unknown
    basis: str               # 'asserted' | 'structural' | 'unknown'
    denied: int = 0          # matched an anchor, rejected by deny-list
    unresolved: int = 0      # anchor found, no usable URL attribute

    @property
    def extracted(self) -> int: return len(self.urls)
```

| `basis` | how derived | portals | meaning of `advertised == 0` |
|---|---|---|---|
| `asserted` | length of an explicit JSON array | sreality (`advert_images`), bezrealitky (`publicImages`), mmreality | **authoritative**: the portal says none |
| `structural` | anchors inside a *declared, present* gallery container | idnes, realitymix, bazos, ceskereality, remax, maxima | **positive fact**: gallery rendered, empty |
| `unknown` | container **not found at all** → `advertised = None` | any portal, any time | **not** photo-less — the selector may have rotted |

The `unknown` state is what makes this honest. A design that only counts anchors would report
`advertised = 0` when the *container selector* rots — re-creating the exact failure one level up.
Container-present and item-count must be separate observations.

Resulting per-listing states — the vocabulary everything downstream uses:

| `media_advertised` | `media_extracted` | state | consequence |
|---|---|---|---|
| `0` | `0` | **`none_at_source`** | UI shows a calm placeholder; repair never retries |
| `NULL` | `0` | **`unknown`** | aggregate alarm (selector rot) |
| `k > 0` | `0` | **`missing`** | **loud** — would have caught realitymix in one run |
| `k > 0` | `0 < m < k` | **`partial`** | **catches idnes** — invisible to every zero-row metric |
| `k > 0` | `k` | `ok` | — |

#### The declarative resolver, and why it is not a DSL trap

```python
@dataclass(frozen=True)
class GallerySpec:
    container: str                      # CSS selector; ABSENT => basis='unknown'
    item: str                           # anchors/imgs inside it = the assertion
    url_attrs: tuple[str, ...] = ("href", "data-src", "data-lazy", "data-original", "src")
    keep_query: tuple[str, ...] = ()    # query params that are LOAD-BEARING
    deny_substrings: tuple[str, ...] = ()
    scope_native_id: bool = False       # URL must carry the listing's own id
    force_https: bool = True
    full_size: Callable[[str], str] | None = None
```

Each knob exists because a *real* portal needs it:

```python
"realitymix": GallerySpec(
    container="div.gallery__items", item="a[data-src], img[src]",
    deny_substrings=("_nahled", "_detail", "/makleri/"),
    scope_native_id=True,          # kills the related-listings block BY CONSTRUCTION
),                                 # force_https handles the http:// flip; the scheme
                                   # never appears in a pattern again
"idnes": GallerySpec(
    container="div.b-gallery, div.carousel", item='a[data-fancybox="images"]',
    url_attrs=("href",),
    keep_query=("profile",),       # LOAD-BEARING — bare URL is a verified 404
    deny_substrings=("my.matterport.com", "no-image-gallery.png", "/ui/image/"),
),
"bazos": GallerySpec(
    container="table, div.inzeratydetail", item="img[src*='bazos.cz/img/']",
    scope_native_id=True,          # preserves today's load-bearing guard
    full_size=_bazos_full_size,
),
```

**The escape hatch, stated plainly.** Over-abstracting extraction into a DSL that cannot express
portal #10 is a classic trap. So the **mandatory** part is the *return type*, not the resolver. A
parser may still hand-build a `MediaExtraction` (mmreality's Vue JSON, bezrealitky's GraphQL,
sreality's `parse_images`) — it just may not return a bare list. `media_extract.resolve()` is the
default implementation the six HTML portals share.

What disappears as a **class** of bug: scheme pinning, absolute-URL-prefix pinning, host
allow-lists, whole-document regexing, unscoped extraction, blanket query stripping. All six are in
production today.

#### Enforcement — one shared writer

✅ Verified: **seven of the eight non-sreality `write_details` bodies are byte-identical** (modulo
the `SOURCE` constant); bezrealitky's is the same minus raw-page staging. ~7 copies of the same 17
lines — per-portal code that rule #21 says should be shared.

```python
# scraper/portal_write.py
def write_staged_details(conn, *, source: str, items: list[DrainItem],
                         stage_raw: bool = True) -> dict[str, int]:
    """The one detail-write chokepoint for every non-sreality portal."""
```

```python
def record_media_checked(conn, listing_id: int, ex: MediaExtraction) -> dict[str, int]:
    inserted = db.record_media(conn, listing_id, ex.urls)
    db.record_media_yield(conn, listing_id, ex.advertised, ex.extracted)   # ← the invariant
    if ex.basis == "unknown":
        LOG.warning("MEDIA unknown-container listing_id=%s", listing_id)
    elif ex.advertised and ex.extracted < ex.advertised:
        LOG.warning("MEDIA shortfall listing_id=%s advertised=%d extracted=%d denied=%d",
                    listing_id, ex.advertised, ex.extracted, ex.denied)
    return {...}
```

sreality keeps its batched path but writes the same two counters inside `write_detail_batch`'s
existing transaction — otherwise the largest portal stays uncovered, which is exactly the mistake
that made "record_media is the single chokepoint" wrong.

#### Schema (additive)

```sql
-- ADDITIVE — autonomous per the database skill
ALTER TABLE listings
  ADD COLUMN media_advertised smallint,   -- NULL = unknown / not yet observed
  ADD COLUMN media_extracted  smallint;

COMMENT ON COLUMN listings.media_advertised IS
  'Photos the PORTAL asserted at last detail parse. 0 = genuinely photo-less (positive '
  'fact). NULL = gallery container not found (unknown) or parsed before this migration. '
  'Counterpart to rule #6: rule #6 guarantees URL->bytes, this guarantees page->URL.';

-- Repair predicate becomes an index scan instead of a NOT EXISTS anti-join over 9.05M
-- image rows. (That anti-join timed out twice on the pooler during this investigation.)
CREATE INDEX CONCURRENTLY listings_media_shortfall_idx
  ON listings (source, first_seen_at)
  WHERE media_advertised IS DISTINCT FROM media_extracted;
```

Two `smallint`s on `listings` — ~4 bytes/row, ~2 MB total. No new table, no new join.

### 3.2 The yield layer — industry pattern, honestly scoped

**The pattern:** declarative data contracts + a data-observability assertion layer — Great
Expectations / dbt tests for the *declaration*, Monte-Carlo-style pillars (freshness, volume,
distribution, completeness) for the *assertions*, evaluated **per partition** (source × recent
cohort), alerting on **state transitions** rather than every red run.

**Where it fits.** The transition-alerting spine (`pipeline_check_results` +
`toolkit/system_alerts.emit_transition_alerts` — onset/recovery edges, dedupe keys, `ON CONFLICT`
idempotence) is genuinely good and needs a *producer*, not a rewrite.

**Where it does NOT fit — say it out loud.** GE/dbt assume you own the producer and can assert a
*schema*. Here the producer is a third party who changes markup without notice:
- **Schema assertions are useless** — there is no upstream schema to pin.
- **Row-level expectations are the wrong grain.** One listing with zero photos is normal on bazos
  and abnormal on remax. Only the *distribution over a cohort* carries signal.
- **The live-markup canary has no GE/dbt analogue.** It is closer to consumer-driven contract
  testing (Pact) against an uncooperative provider. It will be flaky (rate limits, A/B renders, geo
  variance), so it must be scheduled, never a PR gate, and must alarm only on N consecutive fails.

#### The cohort dimension — the single change that makes forward breakage visible

`data_quality_snapshots` is already correctly shaped `(captured_at, source, field, n_active,
n_populated, pct_populated)`. The **only** missing dimension is cohort.

```sql
ALTER TABLE data_quality_snapshots
  ADD COLUMN cohort text NOT NULL DEFAULT 'active_all'
    CHECK (cohort IN ('new_24h', 'new_7d', 'active_all'));
-- Existing rows keep their exact meaning. Nothing that reads it breaks.
```

and `data_quality_by_source` gains the cohort cross-join plus the child-table outputs that were
unrepresentable before: `media`, `media_complete`, `media_photoless`, `media_unknown`,
`image_stored`, `phash`, `clip_tag`, `broker_identity`, `condition_level`, `property_grouped`.

**Segment note, evidence-backed:** any media metric on idnes must be split by `obec_id IS NULL`
(domestic vs foreign). One bulk syndication feed moved the blended exactly-1 rate from 8.6% to
22.4% with zero change in CZ-domestic behaviour. Segmenting by *broker* is over-engineering;
domestic/foreign is one predicate and was proven necessary.

#### The declared contract table

The genuinely missing piece: nothing records that "bazos has no broker because bazos is private
sellers" is *intentional* while "remax has 0 descriptions across 7,877 active listings since
2026-06-01" is a **64-day-old undetected bug**. ✅ Both read as 0.0%.

```sql
CREATE TABLE source_output_contracts (
  source        text    NOT NULL REFERENCES portals(source) ON DELETE CASCADE,
  field         text    NOT NULL,                -- matches data_quality_by_source.field
  cohort        text    NOT NULL DEFAULT 'new_24h',
  min_pct       numeric NOT NULL DEFAULT 0,      -- absolute floor
  band_tolerance_pp numeric,                     -- NULL = floor only; else trailing-median band
  severity      text    NOT NULL DEFAULT 'warn'
                        CHECK (severity IN ('none','warn','fail')),
  min_n         integer NOT NULL DEFAULT 50,     -- suppress small-cohort noise
  note          text,
  effective_from date   NOT NULL DEFAULT current_date,
  updated_by    text,
  PRIMARY KEY (source, field, cohort)
);
```

`severity='none'` is how a legitimate zero is declared once and stays quiet forever — **no code
branch, no per-portal special case**. This mirrors two patterns the project already uses
successfully: `curated_cities` (rule #17) and `pipeline_stages` (rule #22), both operator-curated
tables rather than enums.

#### What actually fires

One new check in `scripts/verify_pipeline.py`'s `_CHECKS` registry — same 6h cadence, same
isolation, same `pipeline_check_results` row, same transition alerting.

| Metric | Fires when | Would have caught | Expected FP rate |
|---|---|---|---|
| `media_unknown` share | > 2% warn / > 10% fail, n ≥ 50 | gallery-container selector rot | **~0** |
| `media` (advertised>0 & extracted=0) | > 2% warn / > 10% fail | **realitymix, within one drain run (~40 min)** | **~0** |
| `media_complete` (partial loss) | below trailing-14d median − tolerance | **idnes, at 63% today** | moderate at first → ship **warn-only**, promote after 14d |
| `media_photoless` share | outside `median × 2 + 2pp` band | a portal starting to serve empty galleries | **real** — mix shift is the enemy; mitigated by the domestic/foreign split |
| scalar fields vs `min_pct` floor | below floor, n ≥ 50 | **remax description (64 days undetected)** | ~0 once seeded from observed reality |
| volume vs trailing-14d median | below 25% of median | a category dropping out / a WAF block | low; needs a wide band (portal seasonality is real) |

**Surfacing.** `pipeline_check_results` → `emit_transition_alerts` → one `notification_dispatches`
row → the SPA nav bell, plus per-source detail on `/health`. **Note:**
`app_settings.system_health_channels` is currently `[]`, so today every system alert is
in-app-bell-only — no email, no Telegram. That is an operator decision (§6 Q3), not a gap.

### 3.3 Repair — one generic re-extraction path, not an image script

**The key discovery: re-extraction needs no network on any of the nine portals.** ✅ Verified:

| substrate | portals | scale |
|---|---|---|
| `portal_raw_pages.html` | bazos, ceskereality, idnes, maxima, mmreality, realitymix, remax (7) | **429,316 pages / 13 GB** |
| `listings.raw_json` | sreality (`advert_images`), bezrealitky (`raw = dict(advert)`) | full history |

So the repair primitive is a **generic re-extraction driver** — the generic form of the ~20 one-off
`backfill_*.py` files already in the repo (`backfill_idnes_brokers.py`, `backfill_idnes_areas.py`,
`backfill_bazos_street_locality.py` all re-parse staged HTML with no re-fetch).

```
scripts/reextract.py --source realitymix --field media \
  --where "media_advertised IS DISTINCT FROM media_extracted" --since 2026-07-16 --dry-run
```

Load substrate → run the **current** parser → write **only** the named field's child rows.
Never touches `listings` content columns, so no content hash is recomputed.

**Rule compliance, by construction:**
- **Rule #2** ✅ — `_HASH_FIELDS` (`scraper/scraped_listing.py:34-41`) is 28 typed content columns
  and contains no media field. A media-only repair **cannot** change the hash and appends **zero**
  snapshots. The driver never calls `ingest_scraped_listing`.
- **Rule #3** — never touches `is_active`, never runs `mark_inactive`, never runs an index walk. It
  also repairs **inactive** listings, which a re-fetch structurally cannot.
- **Rule #4** — untouched; re-extraction is not a sighting.
- **Idempotence** — `record_images` is `ON CONFLICT … DO UPDATE … WHERE storage_path IS NULL`.
  Already-downloaded bytes are never disturbed.

**The second lane — re-fetch — for what re-extraction cannot fix.** bezrealitky's race victims have
`publicImages: []` *in the stored payload*; only the network has the photos. That lane is the fixed
`refresh_stale_image_urls.py` predicate:

```sql
--  BEFORE: EXISTS (images WHERE storage_path IS NULL)   ← zero-row listings invisible
--  AFTER:
AND ( EXISTS (SELECT 1 FROM images i WHERE i.listing_id = l.id AND i.storage_path IS NULL)
   OR (l.media_advertised IS DISTINCT FROM l.media_extracted)
   OR (l.media_advertised IS NULL AND l.first_seen_at > now() - interval '7 days') )
AND l.media_advertised IS DISTINCT FROM 0        -- terminal state: never retry photo-less
```

The `media_advertised = 0` exclusion is the **terminal state** that stops the lane chasing 1,105
legitimately photo-less bazos ads forever — without it every dashboard reads permanently red. Two
further fixes from §2: give the repair lane its own priority tier below `QUEUE_PRIORITY_NEW`, and
stamp `images_refreshed_at` **on outcome, not on enqueue** (today a failed repair marks the listing
handled and suppresses retries for 14 days). Backoff at +6h/+24h/+72h then stop — one `smallint`
counter, not a new subsystem.

### 3.4 The read path — the two must not look identical

They looked identical for 19 days. That is the whole incident, restated as a UI requirement.

**Step 1 — project renderability server-side.** `images_public` does not expose
`unavailable_reason`. ⚠️ Key detail: **14,155 rows carry `unavailable_reason` AND a `storage_path`**
and render fine from R2 — filtering on `unavailable_reason` alone would blank 14k working photos.
The predicate must be `storage_path IS NULL AND unavailable_reason IS NOT NULL`.

```sql
CREATE OR REPLACE VIEW images_public AS
SELECT id, sreality_id, listing_id, sequence, sreality_url, storage_path,
       phash, clip_primary_tag, clip_confidence,
       (storage_path IS NOT NULL OR unavailable_reason IS NULL) AS renderable
FROM images;
```

Then `queries.ts` filters `.eq('renderable', true)`; `images.length` collapses to 0 for dead-cover
cards, the **existing** placeholder takes over, and the `"1 / 18"` counter stops lying. No new
component.

**Step 2 — a typed media state at property grain.** Browse is property-grain but hydrates from one
representative child listing. Fold a state into `browse_projection`, aggregating coverage across
**all child listings of the property** — which also fixes the ~495 cards that are blank while a
merged sibling's full gallery sits in R2, and makes the cover dedup-stable per rules #15/#18.

**Step 3 — the UI distinction.**

| state | render | operator reads |
|---|---|---|
| `none_at_source` | calm neutral placeholder, "Bez fotografií" | normal — the seller posted none |
| `pending` | skeleton shimmer | photos on the way |
| `unavailable` | placeholder + subtle "Fotografie nedostupné" | portal took them down |
| `missing` | placeholder + a **distinct diagnostic marker** | **our pipeline failed here** |

`missing` should be filterable and counted on `/health`. An operator scrolling Browse would then
have seen every realitymix card flagged *missing* on day one instead of a wall of ambiguous grey.

### 3.5 Generalization — the class, not the instance

**Layer A — extraction contract (parse time, portal-facing).** Same three-valued shape wherever a
portal *asserts* something we then extract:

| output | assertion signal | today's silent failure mode |
|---|---|---|
| **media** | gallery container + item count | this incident |
| **video** | same container, video items | idnes video-first race, already visible |
| **geo** | map/coords block present | `geom` NULL indistinguishable from "page had no map" |
| **broker** | broker block present | 0.0% on 5 portals — intentional or broken? nobody knows |
| **description** | description container present | **remax: 0/7,877 active, 64 days, undetected** |

**Layer B — cohort yield ledger (DB-side).** Outputs produced *after* ingest have no portal
assertion, so they get a cohort-conversion metric: what share of the eligible cohort was produced
within the expected window? `image_stored` · `phash` · `clip_tag` · `condition_level` ·
`property_grouped` · `street` · `obec_id` · `broker_identity`. All computable from
`data_quality_by_source` once `cohort` and the child-table fields exist. Nothing bespoke.

**Layer A feeds Layer B; Layer B works without Layer A.** That ordering lets the plan ship value at
every step.

---

## 4. What NOT to build

**Do not add `reality.idnes.cz` to the host allow-list.** Third tripwire of the same design,
re-arms in 6 months, and — verified — it stores 404s because the query strip runs first.

**Do not fall back to `og:image` when the gallery yields nothing.** It papers over extractor
breakage with one low-resolution thumbnail, permanently hides the signal the whole design exists to
raise, and feeds a wrong-resolution near-duplicate into pHash/CLIP where NULL is strictly safer.

**Do not read `img[src]` on idnes.** It ingests `no-image-gallery.png` at scale → identical pHash +
CLIP cosine 1.0 → rule #15 auto-merges non-byt at ≥0.98 → mass false property merges. Reversible
via `property_merge_events`, but the cleanup at scale is severe.

**Do not build the alarm on `scrape_runs.images_discovered`.** The most tempting artefact in the
whole investigation — a first-class per-run column that sat at exactly 0 for 19 days with no
reader. But it counts only **newly inserted** rows: `images=0` is also the correct, healthy output
for a flush of already-known listings, it goes silent when a drain stalls (denominator 0 → NULL,
not red), and it is blind to re-fetch. **Keep it as a chart; never make it a gate.** The gate must
be per-*listing*, per-*cohort*.

**Do not extend `image_storage_overview_mv` / `images_failure_overview_mv` into "image health".**
Their denominator is image rows, permanently. Keep them, rename their Health tiles to **"R2 mirror"**
and **"Download failures"**, and stop letting them stand for media health.

**Do not re-tune `field_null_drift`.** The measured quantity is wrong (cumulative denominator), not
the threshold. Keep it as a secondary retroactive-corruption detector; fix migration 176's header
claim in the same PR.

**Delete, don't extend, `check_eligibility_funnel`'s hardcoded `"status": "ok"`.** Fold its
per-source telemetry into the new check. Two checks computing overlapping per-source yields, one of
which cannot fail, is precisely the debt to avoid.

**Do not build a bespoke "image repair worker."** `listing_detail_queue` + `enqueue_detail`
(source-generic, rules #19/#21) is already the right primitive.

**Do not raise `rebuild_browse_list()`'s function-level `statement_timeout`.** ✅ Verified inert.
Fix the cron command or the role. And keep browse-rebuild scaling out of this program — the recent
failures are substantially observer effect from this audit; re-measure first.

**Do not fix bazos.** Its 4% is real inventory. Touching `bazos_parser.py:566` would attach ~10
foreign photos to every photo-less ad — strictly worse than the missing photos.

**Do not make the live-markup canary a PR gate.** Scheduled only, alarm on N consecutive failures.
Blocking a merge on a third party's deploy is how a good idea becomes a disabled workflow.

---

## 5. Sequenced plan

### PR 1 — `fix/media-extraction-acute` · ~2 hours, no migration · ship first
Stop the bleeding on both portals. `realitymix_parser.py` (`_IMG_RE` → scheme-agnostic, normalize
to `https`), `idnes_parser.py` (drop the host allow-list; strip only `gt`, **keep `profile`**; deny
`my.matterport.com`, `no-image-gallery.png`, `/ui/image/`, and video extensions). Tests use **real
production HTML** pulled from `portal_raw_pages` — the first non-tautological portal-parser tests
in the repo. Low risk, extraction-only.

### PR 2 — `fix/media-reextract-backfill` · ~1 day, no migration
Recover listings from bytes we already own. New `scripts/reextract.py` +
`workflow_dispatch`-only workflow. Rules #2/#3 satisfied by construction.

> **CORRECTION (implemented in #949).** This entry originally said *"scope idnes by shortfall,
> not zero-row"*. ✅ That is **unsafe** and was dropped. `record_images` upserts on
> `(listing_id, sequence)` where sequence is the URL's position in the parsed gallery, and
> refreshes the URL only `WHERE storage_path IS NULL` (`scraper/db.py:1044-1063`). Re-parsing a
> listing that already holds photos and now yields *more* of them — exactly idnes, where the fix
> recovers first-party anchors interleaved in document order — shifts every later photo:
> already-downloaded rows keep their old URL at a sequence the new parse means for a different
> photo, while not-yet-downloaded rows get repointed. The gallery silently reorders.
>
> So re-extraction repairs **zero-row listings only** (nothing to collide with). That recovers
> realitymix (6,905 active) and the idnes zero-row cohort (5,955), but **not** idnes's partial-loss
> cohort. Partial loss needs a **stable media identity** rather than a positional one — folded
> into PR 3, which is where the contract lands anyway. This is a real reduction in PR 2's scope,
> not a detail.
>
> Also implemented differently: only realitymix and idnes are wired. The other parsers build
> `image_urls` inline inside `parse_detail`; lifting that out per portal in the backfill would be
> the per-portal special-casing rule #21 forbids. The registry collapses to one lookup once every
> parser returns a `MediaExtraction`.

### PR 3 — `feature/media-extraction-contract` · ~2 days, additive migration
Make a zero return mean something, forever. New `scraper/media_extract.py`, `GallerySpec` per
portal, new `scraper/portal_write.py` replacing 7 byte-identical `write_details` bodies, all nine
parsers return `MediaExtraction`, `write_detail_batch` writes the counters for sreality. Largest
PR, and a **net line reduction** in the `*_main.py` files.

### PR 4 — `feature/yield-observability` · ~2 days, 2 additive migrations
The alarm. `check_source_output_contract` in `verify_pipeline.py` (delete
`check_eligibility_funnel`), cohort column + widened `data_quality_by_source`,
`source_output_contracts` + seed, Health panel, migration 176 header correction. The existing
`capture-data-quality` pg_cron job just captures more rows — no new job. **Every assertion ships at
`severity='warn'` for 14 days** to learn baselines.

### PR 5 — `fix/read-path-renderability` · ~1.5 days, 2 additive migrations
`images_public.renderable`, `browse_projection.media_state`, and the four UI states. Unify the two
divergent broken-image behaviours in `ImageCarousel.tsx` and `Gallery.tsx`. Include an RLS/grant
test — the "GRANT-not-self-contained" trap has bitten three times.

### PR 6 — `feature/repair-lane` · ~1 day, additive migration
Close the publish-before-photos race: widened predicate + `media_advertised = 0` terminal state +
outcome-stamped cursor + `QUEUE_PRIORITY_REPAIR` + backoff.

### PR 7 — `feature/markup-canary` · ~1.5 days, no migration
Populate `tests/fixtures/source_html/` (does not exist today — which is why every real-fixture test
skips), extend `fetch_and_anonymize_fixtures.py` to the nine scraper portals, add a daily canary
that fetches ~5 live detail URLs/portal, runs the live parser, asserts each contracted output, and
writes its verdict into `pipeline_check_results`. Scheduled, never a PR gate, alarm on 3 consecutive.

### PR 8 — `roadmap/generalize-extraction-contract` · ~2 days, migration
Extend the three-valued shape to geo / broker / description / video; seed their contracts; retire
`field_null_drift`'s advertised role; update `CLAUDE.md` rule #6 and `docs/architecture.md` in-PR
per the same-PR-doc rule.

---

## 6. Operator decisions — ANSWERED 2026-08-04

All six are decided. They are binding inputs to PRs 3–8, not open questions.

**Q1 — which zeros are intentional?** → **Three are bugs, two are intentional.**

| field | verdict | action |
|---|---|---|
| `description` = 0.0% on remax (7,877 active, since 2026-06-01) | **BUG** — confirmed | its own fix |
| `broker_identity_id` = 0.0% on bazos / bezrealitky / maxima / mmreality / remax | **BUG** — brokers are expected there | its own fix |
| `total_floors` = 0.0% on ceskereality | **suspected bug** | investigate, then fix or declare |
| `broker_phone` = 0.0% on all nine | **intentional** | seed `severity='none'` |
| `locality_district_id` = 0.0% on eight of nine | **intentional** | seed `severity='none'` |

**Q2 — `portal_raw_pages` retention** → **(a) keep unbounded** until PR 4 is live, then revisit.
It is now load-bearing repair substrate, so this must be written down as policy (do it in PR 8's
`docs/architecture.md` pass), not left as an accident of migration 099 having no pruner.

**Q3 — alert delivery** → **bell only.** No email, no Telegram; `system_health_channels` stays `[]`.
Consequence for PR 4: the badge is the *only* signal, so the two zero-false-positive checks
(`media`, `media_unknown`) ship straight at `severity='fail'` rather than warming up on `warn`.
The FP-prone ones (`media_complete`, `media_photoless`) still start at `warn`.

**Q4 — foreign inventory** → **segment metrics AND flag in the UI.** Split every media/quality
metric by `obec_id IS NULL`, and surface a visible marker + Browse filter so foreign stock can be
excluded from browsing without being dropped from the database. Do not stop ingesting.

**Q5 — ship posture** → **PR 1 + PR 2 together.** Shipped as #949 (merged 2026-08-04), with the
scope correction in the PR 2 entry above.

**Q6 — R2 key namespace** → **fold into PR 3.** Namespace new keys `l/{listing_id}/{seq}.jpg`;
forward-only, no backfill.

> **SHIPPED 2026-08-05 as `img/{listing_id}/{images.id}.jpg`** — forward-only and no backfill as
> decided, but with a **different second segment than this answer specified**, because the
> collision turned out to have a second half. Keying on `{seq}` keeps the key POSITIONAL, and
> `sequence` is not a stable identity for a photo (the same `record_images` hazard § 3.3 warns
> about): every NULL-sequence row of a listing maps to one `.../0000.jpg`, and a re-parse that
> shifts positions re-points a key at a different photo. `images.id` is the primary key, so the
> key is unique per ROW by construction. The prefix is `img/` rather than `l/` purely for
> legibility when browsing the bucket — say the word and it is a one-line change plus the
> `_KEY_RE` alternative in `api/routes/images.py`. Live damage found and repaired: 16 objects /
> 32 rows (migration 371); rationale in `docs/architecture.md` rule 6.
