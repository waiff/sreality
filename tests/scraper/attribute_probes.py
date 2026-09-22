"""Drive every portal's parse entry point over REAL payload shapes.

The characterisation harness W2 refactors against. Three corpora, one recorder:

  * **pages** — every real detail payload checked into the repo (the location program's
    scrubbed per-portal pages, the `portal_html` set, the refetch set, the regression
    set, the sreality sample), through the portal's own `parse_detail` / `parse_listing`.
  * **labels** — one probe per (portal, source key, live value) taken from the checked-in
    key census in `data/field_capture/census/`. That census is the portal's OWN key space
    and its OWN values, so this corpus covers the whole live vocabulary — which no
    hand-authored fixture can (remax's parser reads `balkon`/`lodzie` and the portal has
    never emitted either).
  * **precedence** — one probe per source-key chain a parser walks with `or`, carrying
    EVERY key of the chain at once with distinguishable values, so which key wins is
    recorded rather than assumed.

Each probe records the 26 typed attribute columns (`field_census.ATTRIBUTE_FIELDS`) the
parse produced. The goldens in `tests/fixtures/field_capture/golden/` were generated from
the parsers as they stood BEFORE the vocabulary module existed; the refactor is correct
exactly when they do not move.

Re-bless a reviewed change:  python -m tests.scraper.attribute_probes --bless
"""

from __future__ import annotations

import datetime as _dt
import json
from html import escape
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from scraper import (
    bazos_parser,
    bezrealitky_parser,
    ceskereality_parser,
    field_census,
    idnes_parser,
    maxima_parser,
    mmreality_parser,
    parser as sreality_parser,
    realitymix_parser,
    remax_parser,
)
from scraper.scraped_listing import ScrapedListing

_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = _ROOT / "tests" / "fixtures"
GOLDEN_DIR = FIXTURES / "field_capture" / "golden"

BLESS_COMMAND = "python -m tests.scraper.attribute_probes --bless"

# Keys a probe must never carry: the census records them because the portal ships them,
# but they are identity or prose, and a probe that set them would record the fixture's own
# text back rather than a typed attribute.
_PROBE_SKIP_KEYS: frozenset[str] = frozenset({
    "listings.description", "description", "advert_description", "image_urls", "images",
    "image", "publicImages", "mainImage", "source_url", "uri", "slug", "broker",
    "originalTitle", "shortTitle", "regionTree", "accessoryGroups", "poi",
})


# --- what a probe records --------------------------------------------------


def _attributes(parsed: ScrapedListing | Mapping[str, Any]) -> dict[str, Any]:
    """The typed attribute columns a parse produced, non-null only.

    Recording only the non-null cells keeps a golden readable: a probe that sets one key
    prints the one or two attributes that key reaches, not 26 nulls."""
    out: dict[str, Any] = {}
    for field in field_census.ATTRIBUTE_FIELDS:
        value = (
            parsed.get(field) if isinstance(parsed, Mapping)
            else getattr(parsed, field, None)
        )
        if value is None:
            continue
        out[field] = value if isinstance(value, (bool, int, float, str)) else str(value)
    return out


# --- the per-portal drivers ------------------------------------------------
#
# One function per portal turning a params/payload mapping into the portal's own parse
# input, so every probe runs through the REAL entry point rather than through a normaliser
# picked by name. The five HTML-table portals get their own spec markup rebuilt around the
# params (the shape `_detail_params` reads back); the three JSON portals get their native
# object; bazos has no spec table at all and mines its title block.


def _rows_html(rows: str, *, title: str, body: str = "") -> str:
    return (
        f"<html><body><h1>{escape(title)}</h1><h3>{escape(title)}</h3>"
        f"<title>{escape(title)}</title>{rows}{body}</body></html>"
    )


def _drive_ceskereality(params: Mapping[str, Any], title: str) -> ScrapedListing:
    rows = "".join(
        f'<div class="i-info"><span class="i-info__title">{escape(str(k))}</span>'
        f'<span class="i-info__value">{escape(str(v))}</span></div>'
        for k, v in params.items()
    )
    return ceskereality_parser.parse_detail(
        _rows_html(rows, title=title),
        source_url="https://www.ceskereality.cz/prodej/byty/x-1234567.html",
        category_main="byt", category_type="prodej",
    )


def _drive_idnes(params: Mapping[str, Any], title: str) -> ScrapedListing:
    rows = "<dl>" + "".join(
        f"<dt>{escape(str(k))}</dt><dd>{escape(str(v))}</dd>" for k, v in params.items()
    ) + "</dl>"
    return idnes_parser.parse_detail(
        _rows_html(rows, title=title),
        source_url="https://reality.idnes.cz/detail/prodej/byt/x/0123456789abcdef/",
        category_main="byt", category_type="prodej",
    )


def _drive_realitymix(params: Mapping[str, Any], title: str) -> ScrapedListing:
    rows = "<ul>" + "".join(
        f'<li class="detail-information__data-item"><span>{escape(str(k))}:</span>'
        f"<span>{escape(str(v))}</span></li>"
        for k, v in params.items()
    ) + "</ul>"
    return realitymix_parser.parse_detail(
        _rows_html(rows, title=title),
        source_url="https://reality.mixer.cz/detail/mesto/prodej-bytu-1234567.html",
    )


def _drive_remax(params: Mapping[str, Any], title: str) -> ScrapedListing:
    rows = "".join(
        f'<div class="pd-detail-info__row">'
        f'<div class="pd-detail-info__label">{escape(str(k))}:</div>'
        f'<div class="pd-detail-info__value">{escape(str(v))}</div></div>'
        for k, v in params.items()
    )
    return remax_parser.parse_detail(
        _rows_html(rows, title=title),
        source_url="https://www.remax-czech.cz/reality/detail/1234567/",
    )


def _drive_maxima(params: Mapping[str, Any], title: str) -> ScrapedListing:
    rows = "<table>" + "".join(
        f'<tr><th class="slider_label">{escape(str(k))}</th>'
        f'<td class="slider_value">{escape(str(v))}</td></tr>'
        for k, v in params.items()
    ) + "</table>"
    return maxima_parser.parse_detail(
        _rows_html(rows, title=title),
        source_url="https://www.maxima-reality.cz/nemovitosti/b50087758/",
    )


def _drive_bazos(params: Mapping[str, Any], title: str) -> ScrapedListing:
    rows = "<table>" + "".join(
        f"<tr><td>{escape(str(k))}:</td><td>{escape(str(v))}</td></tr>"
        for k, v in params.items()
    ) + "</table>"
    html = (
        f'<html><body><h1 class="nadpisdetail">{escape(title)}</h1>{rows}'
        '<div class="popisdetail"></div></body></html>'
    )
    return bazos_parser.parse_detail(
        html, source_url="https://reality.bazos.cz/inzerat/123456789/x.php",
        category_main="byt", category_type="prodej",
    )


def _drive_sreality(payload: Mapping[str, Any], title: str) -> dict[str, Any]:
    raw = {"hash_id": 1234567890, "advert_name": title, **payload}
    return sreality_parser.parse_listing(raw)


def _drive_bezrealitky(payload: Mapping[str, Any], title: str) -> ScrapedListing:
    return bezrealitky_parser.parse_advert({"id": "1234567", "title": title, **payload})


def _drive_mmreality(payload: Mapping[str, Any], title: str) -> ScrapedListing:
    obj = {"id": 1234567, "title": title, **payload}
    html = f'<div :property="{escape(json.dumps(obj, ensure_ascii=False), quote=True)}"></div>'
    return mmreality_parser.parse_detail(
        html, source_url=f"https://www.mmreality.cz/nemovitosti/{obj['id']}/"
    )


Driver = Callable[[Mapping[str, Any], str], Any]

DRIVERS: dict[str, Driver] = {
    "bazos": _drive_bazos,
    "bezrealitky": _drive_bezrealitky,
    "ceskereality": _drive_ceskereality,
    "idnes": _drive_idnes,
    "maxima": _drive_maxima,
    "mmreality": _drive_mmreality,
    "realitymix": _drive_realitymix,
    "remax": _drive_remax,
    "sreality": _drive_sreality,
}

# Whether a census value is the portal's own JSON (sreality's `{name, value}` objects,
# bezrealitky's scalars, mmreality's nested enums) or the plain text an HTML spec cell
# carries. The census stores both as text; only the JSON portals get parsed back.
_JSON_SUBSTRATE: frozenset[str] = frozenset({"sreality", "bezrealitky", "mmreality"})

PROBE_TITLE = "Prodej bytu 3+kk 75 m²"


def _probe_value(portal: str, raw: str) -> Any:
    if portal not in _JSON_SUBSTRATE:
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


# --- the three corpora -----------------------------------------------------

# Every real detail payload checked into the repo, by the portal that parses it. Kept as
# an explicit list because these directories serve four different programs, and a glob
# would silently absorb the next one's fixtures into this program's goldens.
PAGE_FIXTURES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        [("bazos", "portal_html/bazos_detail.html"),
         ("idnes", "portal_html/idnes_detail.html"),
         ("mmreality", "portal_html/mmreality_detail.html"),
         ("realitymix", "portal_html/realitymix_detail.html"),
         ("remax", "portal_html/remax_detail.html")]
        + [(p, f"location_w2/{p}_detail.html") for p in
           ("bazos", "ceskereality", "idnes", "maxima", "mmreality", "realitymix", "remax")]
        + [(f.split("_")[0], f"location_w2a_refetch/{f}") for f in (
            "ceskereality_a1.html", "ceskereality_a2.html", "ceskereality_b1.html",
            "idnes_a1.html", "idnes_a2.html", "idnes_b1.html", "idnes_c1.html",
            "mmreality_a1.html", "mmreality_a2.html", "mmreality_b1.html",
            "realitymix_a1.html", "realitymix_a2.html", "realitymix_b1.html",
            "remax_a1.html", "remax_a2.html", "remax_b1.html")]
        # The location program's sreality fixtures carry only `locality`/`premise` —
        # scrubbed of every attribute key, `hash_id` included — so they are that
        # program's evidence, not this one's. `sample_listing.json` is a full payload.
        + [("sreality", "sample_listing.json")]
        + [("mmreality", "location_w2/regressions/mmreality/951845.json"),
           ("realitymix", "location_w2/regressions/realitymix/8662169.json"),
           ("remax", "location_w2/regressions/remax/437234.json")]
        + [("maxima", f"location_w2/regressions/maxima/{n}.json") for n in
           ("d40026367", "f60012522", "f60012682")]
    )
)

# The source-key chains a parser walks with `or` today. Every key of a chain in ONE probe
# with a distinguishable value, so the golden records which one the parser picks.
PRECEDENCE_PROBES: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("ceskereality", "floor", {"patro": "3.", "podlaží": "7."}),
    ("ceskereality", "total_floors", {"počet podlaží": "4", "podlaží v domě": "9"}),
    ("ceskereality", "has_balcony", {"balkóny": "Balkon", "balkon": "Ne"}),
    ("ceskereality", "building_type",
     {"konstrukce": "Zděná", "typ stavby": "Panelová", "stavba": "Dřevěná"}),
    ("ceskereality", "condition",
     {"stav nemovitosti": "Bezvadný", "stav objektu": "Špatný"}),
    ("ceskereality", "energy_rating",
     {"energetická náročnost": "B - Velmi úsporná", "penb": "F"}),
    ("ceskereality", "disposition", {"dispozice": "2+1"}),
    ("idnes", "condition",
     {"stav bytu": "velmi dobrý stav", "stav domu": "dobrý stav",
      "stav objektu": "novostavba", "stav budovy": "projekt"}),
    ("idnes", "furnished", {"vybavení": "zařízený", "vybavení domu": "nezařízený"}),
    ("idnes", "energy_rating",
     {"penb": "C (vyhl. č. 264/2020 Sb.)", "energetická náročnost": "F"}),
    ("idnes", "has_balcony", {"balkon": "jih", "lodžie": "4 m 2", "terasa": "sever"}),
    ("idnes", "has_parking",
     {"parkování": "garáž , parkování na ulici", "počet parkovacích míst": "3"}),
    ("idnes", "garage", {"garáž": "ano", "dvojgaráž": "", "parkování": "garáž"}),
    ("maxima", "energy_rating", {"energetická náročnost": "B", "penb": "G"}),
    ("maxima", "has_parking", {"parkovací stání": "Ano", "garáž": "Ano"}),
    ("realitymix", "floor", {"číslo podlaží v domě": "5", "podlaží": "8"}),
    ("realitymix", "has_balcony",
     {"balkon": "4 m²", "balkón": "Ne", "lodžie": "3 m²"}),
    ("realitymix", "building_type",
     {"druh objektu": "cihlová", "konstrukce": "panelová"}),
    ("realitymix", "furnished", {"vybaveno": "ano", "vybavení": "ne"}),
    ("realitymix", "energy_rating",
     {"energetická náročnost budovy": "A - Mimořádně úsporná",
      "energetická náročnost": "E"}),
    ("realitymix", "disposition", {"dispozice bytu": "2+kk", "dispozice": "4+1"}),
    ("realitymix", "has_parking", {"ostatní": "garáž a parkování", "garáž": "ano"}),
    ("remax", "has_balcony", {"balkon": "Ano", "lodzie": "Ne"}),
    ("remax", "has_parking", {"parkovani": "Ano", "garaz": "Ne"}),
    ("remax", "energy_rating",
     {"energeticka narocnost budovy": "B", "energeticka narocnost": "F"}),
    ("sreality", "price_unit",
     {"price_summary_unit_cb": {"name": "za měsíc", "value": 2},
      "price_unit_cb": {"name": "za nemovitost", "value": 1}}),
    ("sreality", "price_czk", {"price_summary_czk": 111, "price_czk": 222}),
    ("sreality", "has_balcony",
     {"balcony": False, "terrace": True, "loggia": False}),
    ("sreality", "has_parking",
     {"parking_lots": False, "garage": True, "parking": False}),
    ("mmreality", "has_parking", {"parkingPlaces": 0,
                                  "accessoryGroups": [{"name": "Parkování", "accessories": [
                                      {"name": "Parkování na ulici"}]}]}),
    ("mmreality", "cellar", {"cellar": False,
                             "accessoryGroups": [{"name": "Vedlejší prostory a stavby",
                                                  "accessories": [{"name": "Sklep"}]}]}),
    ("bezrealitky", "has_parking", {"parking": False, "garage": True}),
    ("bezrealitky", "has_balcony",
     {"balconySurface": None, "terraceSurface": 12, "loggiaSurface": None}),
)


def _run(call: Callable[[], Any]) -> dict[str, Any]:
    """The attributes a parse produced — or the exception it raised.

    A fixture some parser refuses (the location program scrubbed mmreality's image
    objects down to strings, and `_image_urls` calls `.get` on them) is still evidence:
    WHETHER a payload parses is behaviour this refactor must not change either."""
    try:
        return _attributes(call())
    except Exception as exc:  # noqa: BLE001 — the exception IS the recorded behaviour
        return {"__raises__": type(exc).__name__}


def iter_page_probes() -> Iterator[tuple[str, str, Any]]:
    for portal, rel in PAGE_FIXTURES:
        path = FIXTURES / rel
        if not path.exists():
            continue
        yield f"{portal}::{rel}", portal, _run(lambda: _parse_page(portal, path))


def _parse_page(portal: str, path: Path) -> Any:
    if path.suffix == ".json":
        doc = json.loads(path.read_text(encoding="utf-8"))
        raw = doc.get("raw_json", doc) if isinstance(doc, dict) else doc
        if portal == "sreality":
            return sreality_parser.parse_listing(raw)
        if portal == "mmreality":
            return _drive_mmreality(raw, str(raw.get("title") or ""))
        # The location regression fixtures for the HTML portals store the parser's own
        # `raw_json`, whose `params` is exactly what `_detail_params` produced.
        return DRIVERS[portal](raw.get("params") or {}, str(raw.get("title") or ""))
    html = path.read_text(encoding="utf-8", errors="replace")
    return _PAGE_PARSERS[portal](html)


_PAGE_PARSERS: dict[str, Callable[[str], Any]] = {
    "bazos": lambda html: bazos_parser.parse_detail(
        html, source_url="https://reality.bazos.cz/inzerat/123456789/x.php",
        category_main="byt", category_type="prodej"),
    "ceskereality": lambda html: ceskereality_parser.parse_detail(
        html, source_url="https://www.ceskereality.cz/prodej/byty/x-1234567.html",
        category_main="byt", category_type="prodej"),
    "idnes": lambda html: idnes_parser.parse_detail(
        html, source_url="https://reality.idnes.cz/detail/prodej/byt/x/0123456789abcdef/",
        category_main="byt", category_type="prodej"),
    "maxima": lambda html: maxima_parser.parse_detail(
        html, source_url="https://www.maxima-reality.cz/nemovitosti/b50087758/"),
    # No id in the URL, so the parser takes the page's fullest `:property` blob: each
    # checked-in page carries its own listing id and a probe must not have to know it.
    "mmreality": lambda html: mmreality_parser.parse_detail(
        html, source_url="https://www.mmreality.cz/nemovitosti/"),
    "realitymix": lambda html: realitymix_parser.parse_detail(
        html, source_url="https://reality.mixer.cz/detail/mesto/prodej-bytu-1234567.html"),
    "remax": lambda html: remax_parser.parse_detail(
        html, source_url="https://www.remax-czech.cz/reality/detail/1234567/"),
}


def iter_label_probes() -> Iterator[tuple[str, str, Any]]:
    for census in field_census.load_censuses():
        portal = str(census["portal"])
        driver = DRIVERS.get(portal)
        if driver is None:
            continue
        for key, entry in sorted((census.get("keys") or {}).items()):
            if key in _PROBE_SKIP_KEYS:
                continue
            for value in sorted((entry.get("values") or {})):
                # A value at the census cut is a PREFIX, not the portal's value; replaying
                # it would characterise the truncation rather than the parser.
                if (value == field_census.JSON_NULL
                        or len(value) >= field_census.VALUE_TRUNCATE_CHARS):
                    continue
                payload = {key: _probe_value(portal, value)}
                yield (f"{portal}::{key}={value}", portal,
                       _run(lambda: driver(payload, PROBE_TITLE)))


def iter_precedence_probes() -> Iterator[tuple[str, str, Any]]:
    for portal, field, payload in PRECEDENCE_PROBES:
        yield (f"{portal}::{field}", portal,
               _run(lambda: DRIVERS[portal](payload, PROBE_TITLE)))


CORPORA: dict[str, Callable[[], Iterator[tuple[str, str, Any]]]] = {
    "pages": iter_page_probes,
    "labels": iter_label_probes,
    "precedence": iter_precedence_probes,
}


def record(corpus: str) -> dict[str, Any]:
    return {name: probe for name, _portal, probe in CORPORA[corpus]()}


def golden_path(corpus: str) -> Path:
    return GOLDEN_DIR / f"{corpus}.json"


def load_golden(corpus: str) -> dict[str, Any]:
    return json.loads(golden_path(corpus).read_text(encoding="utf-8"))


def write_golden(corpus: str, recorded: Mapping[str, Any]) -> Path:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = golden_path(corpus)
    body = ",\n".join(
        f"  {json.dumps(k, ensure_ascii=False)}: "
        f"{json.dumps(v, ensure_ascii=False, sort_keys=True)}"
        for k, v in sorted(recorded.items())
    )
    path.write_text("{\n" + body + "\n}\n", encoding="utf-8")
    return path


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bless", action="store_true")
    args = ap.parse_args()
    for corpus in CORPORA:
        recorded = record(corpus)
        if args.bless:
            print(f"{write_golden(corpus, recorded)}  ({len(recorded)} probes)")
        else:
            print(json.dumps({"corpus": corpus, "probes": len(recorded)}))
    if args.bless:
        print(_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
