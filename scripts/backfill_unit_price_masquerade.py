"""One-off backfill: quarantine per-m² unit prices stored as `listings.price_czk`.

ceskereality, realitymix and bazos each render commercial and land offers at a
RATE ("100 Kč za m²/měsíc", "45 Kč / (za m²)") and the pre-W1 parsers wrote that
rate into `price_czk`, which every consumer reads as a total: 1,254 / 1,178 /
291 active rows sit under 1,000 Kč on ~310 m² units, and the ceskereality set
runs all the way up to 979,620 Kč "za m²" — magnitude does not separate them.

Unlike the mmreality area damage this is NOT repairable. The true total was
never on the page, so it is not recoverable from anything stored. So this is a
QUARANTINE: `price_czk` goes to NULL, no surface presents a unit price as a
total, and the next successful refetch repopulates it correctly through W1's
parse-time rail. The value is not lost — the row it came from is recorded in
`raw_json.unit_price_quarantined_czk` beside the text that convicted it, so the
write is reversible until the next refetch replaces raw_json wholesale.

CONFIRMATION, NEVER MAGNITUDE. A row is quarantined only when the portal's OWN
price cell, re-read from already-staged state, carries the anchored per-area
marker (`scraper.price_text.is_per_area_price` — the same test the parsers now
run). A cheap-but-real listing is left alone because its cell says nothing about
m². Rows whose cell cannot be read at all are LEFT and counted as `unconfirmed`,
never guessed at:

  * ceskereality — `raw_json.params.cena`, present on 100% of the damaged set.
  * bazos        — `raw_json.price_text`. Measured across all 26,592 priced
                   active bazos rows, ZERO carry any per-area marker: the cell
                   is a bare "170 Kč" and the m² basis lives only in prose. So
                   bazos confirms nothing and this script quarantines none of
                   it. It stays wired to produce that count honestly.
  * realitymix   — `params` has no `cena` key at all, so the price row is read
                   back out of the staged detail page. Only the `<tr>` fragment
                   is fetched (379 bytes against a 72 kB page), then handed to
                   realitymix's own `_detail_price_text` so the selector stays
                   in one place.

This writes NO snapshot (rule #2 governs source-content changes; correcting our
own mis-parse of the SAME staged state is a data-quality fix — the
backfill_idnes_areas posture). `price_czk` IS in the content hash, so each
quarantined listing's NEXT successful detail refetch computes a differing hash
and appends ONE genuine snapshot — bounded, correct, self-limiting.

Keyed on the surrogate `listings.id`, NOT `sreality_id`: since the identity
refactor's Gate 2, `sreality_id` is NULL on 17,296 / 27,107 / 11,850 of the
ceskereality / bazos / realitymix rows, so a sreality_id cursor would walk past
roughly a fifth of the damage without saying so.

Idempotent + resumable WITHOUT a marker column, which is the second place this
diverges from backfill_idnes_areas: the selection requires `price_czk IS NOT
NULL`, so a quarantined row drops out of the next run by construction, and a
kept row is re-examined and kept again at zero writes. `--after` resumes from a
`listings.id` cursor. That spares ~136k unchanged rows a pointless raw_json
rewrite.

The read is KEYSET-PAGINATED and exhaustive by construction. The three portals
hold 220,456 priced rows, so a single capped `LIMIT` would stop short of the
corpus and — because the order is `id ASC` — the block it skipped would be the
NEWEST inventory, while the log still read like a clean finish. Each page is one
short statement (`id > cursor ORDER BY id LIMIT --batch-size`): the ceskereality
and bazos arms detoast `raw_json` per row, and the cluster's `statement_timeout`
is 120s, so a whole-corpus statement is also a real cancellation risk. The run
walks pages until one comes back short, `--max-seconds` is checked BETWEEN pages
(not only inside one), and the summary + resume cursor are emitted from a
`finally`, so an aborted or Ctrl-C'd pass still tells the operator where it got
to. A pass that stopped early logs `BACKFILL INCOMPLETE` — silence means done.

Usage:  python -m scripts.backfill_unit_price_masquerade --dry-run
        python -m scripts.backfill_unit_price_masquerade --source realitymix --write
Required: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter

from selectolax.parser import HTMLParser

from scraper import db
from scraper.bazos_parser import _PRICE_DIGITS_RE as _BAZOS_AMOUNT_RE
from scraper.ceskereality_parser import _PRICE_RUN_RE as _CESKEREALITY_AMOUNT_RE
from scraper.price_text import is_per_area_price
from scraper.realitymix_parser import _PRICE_RUN_RE as _REALITYMIX_AMOUNT_RE
from scraper.realitymix_parser import _detail_price_text as _realitymix_price_cell

LOG = logging.getLogger("backfill_unit_price_masquerade")

SOURCES: tuple[str, ...] = ("ceskereality", "realitymix", "bazos")

# Each portal's own amount scanner — the marker test is anchored to the text
# immediately AFTER the amount, so the amount has to be found the way that
# portal's parser finds it.
_AMOUNT_RE = {
    "bazos": _BAZOS_AMOUNT_RE,
    "ceskereality": _CESKEREALITY_AMOUNT_RE,
    "realitymix": _REALITYMIX_AMOUNT_RE,
}

# The portals whose price cell is NOT in raw_json, and the `<tr>` that holds it.
_FRAGMENT_PATTERN = {
    "realitymix": '<tr class="advert-description__short-props-price".*?</tr>',
}

QUARANTINE, KEEP, UNCONFIRMED = "quarantine", "keep", "unconfirmed"

_SELECT_SQL = """
    SELECT l.id, l.source, l.source_id_native, l.category_main,
           l.category_type, l.property_id, l.price_czk, l.area_m2,
           CASE l.source
               WHEN 'ceskereality' THEN l.raw_json->'params'->>'cena'
               WHEN 'bazos'        THEN l.raw_json->>'price_text'
           END AS raw_price_text
    FROM listings l
    WHERE l.source = ANY(%(sources)s::text[])
      AND l.price_czk IS NOT NULL
      AND l.id > %(after)s::bigint
    ORDER BY l.id
    LIMIT %(page)s::int
"""

_COUNT_SQL = """
    SELECT source, count(*) FROM listings
    WHERE source = ANY(%(sources)s::text[]) AND price_czk IS NOT NULL
    GROUP BY source
"""

_FRAGMENT_SQL = """
    SELECT substring(html from %(pattern)s::text)
    FROM portal_raw_pages
    WHERE source = %(source)s::text AND source_id_native = %(native)s::text
      AND page_kind = 'detail'
    ORDER BY fetched_at DESC NULLS LAST
    LIMIT 1
"""

_QUARANTINE_SQL = """
    UPDATE listings
    SET price_czk = NULL,
        raw_json = raw_json || jsonb_build_object(
            'unit_price_quarantined_czk', %(old_price)s::int,
            'unit_price_quarantined_text', %(price_text)s::text)
    WHERE id = %(id)s::bigint
"""


def price_text_from_fragment(source: str, fragment: str | None) -> str | None:
    """Run the portal's own price-cell extractor over the staged `<tr>` fragment.

    The fragment is re-wrapped in a `<table>` because selectolax drops a bare
    orphan `<tr>` — the row has to have a table to belong to before the
    parser's `tr.…` selector can see it.
    """
    if not fragment or source not in _FRAGMENT_PATTERN:
        return None
    tree = HTMLParser(f"<table>{fragment}</table>")
    return _realitymix_price_cell(tree)


def decide(source: str, price_text: str | None, stored_price: int | None) -> tuple[str, str]:
    """Quarantine / keep / unconfirmed for one row, from its staged price cell."""
    if stored_price is None:
        return KEEP, "no stored price"
    if not price_text or not price_text.strip():
        return UNCONFIRMED, "no staged price cell"
    match = _AMOUNT_RE[source].search(price_text)
    if match is None:
        return UNCONFIRMED, "no amount in the staged price cell"
    if not is_per_area_price(price_text[match.end():]):
        return KEEP, "no per-area marker"
    return QUARANTINE, "per-area marker"


def page_size(limit: int | None, examined: int, batch: int) -> int:
    """Rows to ask for next: the batch, trimmed by whatever `--limit` has left."""
    if limit is None:
        return batch
    return max(0, min(batch, limit - examined))


def _log_summary(quarantined: Counter[str], unconfirmed: Counter[str],
                 verdicts: Counter[str], cursor: int, exhausted: bool,
                 dry_run: bool) -> None:
    for key, n in sorted(quarantined.items(), key=lambda kv: -kv[1]):
        LOG.info("BACKFILL quarantine %-46s %6d", key, n)
    for key, n in sorted(unconfirmed.items(), key=lambda kv: -kv[1]):
        LOG.info("BACKFILL left_alone  %-46s %6d", key, n)
    for key, n in sorted(verdicts.items()):
        LOG.info("BACKFILL verdict     %-46s %6d", key, n)
    LOG.info("BACKFILL done examined=%d quarantine=%d unconfirmed=%d kept=%d "
             "cursor=%d exhausted=%s dry_run=%s",
             sum(verdicts.values()), sum(quarantined.values()),
             sum(unconfirmed.values()),
             sum(n for k, n in verdicts.items() if k.endswith(f":{KEEP}")),
             cursor, exhausted, dry_run)
    if not exhausted:
        LOG.warning("BACKFILL INCOMPLETE — rows above id=%d were never examined; "
                    "resume with --after %d", cursor, cursor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", choices=SOURCES,
                        help="Restrict to one portal; repeatable. Default: all three.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max listings examined this run. Default: the whole corpus.")
    parser.add_argument("--batch-size", type=int, default=5000,
                        help="Rows per keyset page. Keeps each statement well "
                             "inside the cluster's 120s statement_timeout.")
    parser.add_argument("--after", type=int, default=0,
                        help="Resume from this listings.id cursor (exclusive).")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="Wall-clock budget; stop claiming and exit cleanly.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Report what would change; write nothing (the default).")
    parser.add_argument("--write", dest="dry_run", action="store_false",
                        help="Actually write. Without it this script only reports.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    sources = list(args.source or SOURCES)
    LOG.info("BACKFILL start sources=%s batch=%d limit=%s after=%d dry_run=%s",
             ",".join(sources), args.batch_size, args.limit, args.after, args.dry_run)
    start = time.monotonic()
    verdicts: Counter[str] = Counter()
    quarantined: Counter[str] = Counter()
    unconfirmed: Counter[str] = Counter()
    cursor = args.after
    examined = 0
    exhausted = False

    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_COUNT_SQL, {"sources": sources})
                for source, n in cur.fetchall():
                    LOG.info("BACKFILL priced_rows source=%-14s %7d", source, n)

            while True:
                page = page_size(args.limit, examined, args.batch_size)
                if page == 0:
                    break
                with conn.cursor() as cur:
                    cur.execute(_SELECT_SQL, {"sources": sources, "page": page,
                                              "after": cursor})
                    rows = cur.fetchall()
                if not rows:
                    exhausted = True
                    break

                dirty: list[int] = []
                for row in rows:
                    (listing_id, source, native, cmain, ctype, prop_id,
                     price_czk, area_m2, price_text) = row
                    cursor = listing_id
                    examined += 1

                    if source in _FRAGMENT_PATTERN:
                        with conn.cursor() as cur:
                            cur.execute(_FRAGMENT_SQL,
                                        {"pattern": _FRAGMENT_PATTERN[source],
                                         "source": source, "native": native})
                            frag = cur.fetchone()
                        price_text = price_text_from_fragment(
                            source, frag[0] if frag else None)

                    verdict, reason = decide(source, price_text, price_czk)
                    verdicts[f"{source}:{verdict}"] += 1
                    if verdict == UNCONFIRMED:
                        unconfirmed[f"{source}:{reason}"] += 1
                    elif verdict == QUARANTINE:
                        quarantined[f"{source}:{cmain}:{ctype}"] += 1
                        LOG.debug("BACKFILL would quarantine id=%d %s price=%s "
                                  "area=%s text=%r",
                                  listing_id, source, price_czk, area_m2, price_text)
                        if not args.dry_run:
                            with conn.cursor() as cur:
                                cur.execute(_QUARANTINE_SQL,
                                            {"id": listing_id, "old_price": price_czk,
                                             "price_text": price_text})
                            if prop_id is not None:
                                dirty.append(prop_id)

                if dirty:
                    db.mark_properties_dirty(conn, dirty)
                LOG.info("BACKFILL progress examined=%d quarantine=%d cursor=%d",
                         examined, sum(quarantined.values()), cursor)

                if len(rows) < page:
                    exhausted = True
                    break
                if args.max_seconds and time.monotonic() - start > args.max_seconds:
                    LOG.info("BACKFILL stopping: --max-seconds reached after=%d", cursor)
                    break
    finally:
        _log_summary(quarantined, unconfirmed, verdicts, cursor, exhausted, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
