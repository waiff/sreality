"""The property page's ad as a PDF — the sheet inside the property download zip.

Listing data only, in the page's own words: what the ad is, its price and place, the
facts and amenities, the description, the brokers and every ad of the property. Nothing
the operator curated (tags, notes, collections, pipeline) is an input. The PDF base-14
fonts carry no Czech letters, so the sheet embeds the SPA's own (Inter + Fraunces, OFL,
api/fonts/).
"""

from __future__ import annotations

import io
import math
import threading
from datetime import date, datetime
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    HRFlowable,
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from toolkit.filter_registry import (
    BUILDING_TYPE_OPTIONS,
    CATEGORY_TYPE_OPTIONS,
    CONDITION_OPTIONS,
    FURNISHED_OPTIONS,
    OWNERSHIP_OPTIONS,
    PORTAL_OPTIONS,
    PRICE_UNIT_OPTIONS,
    subtype_label_cs,
)

_FONT_DIR = Path(__file__).parent / "fonts"
SANS, SANS_BOLD, DISPLAY = "Inter", "Inter-SemiBold", "Fraunces-SemiBold"

# The SPA's light-theme tokens (frontend/src/styles/globals.css); RULE is its 8% ink
# hairline flattened onto white.
INK = colors.HexColor("#1a1c22")
INK_2 = colors.HexColor("#4a4d57")
INK_3 = colors.HexColor("#7a7d86")
INK_4 = colors.HexColor("#b0b1b7")
COPPER = colors.HexColor("#3c6e63")
BRICK = colors.HexColor("#a04b3d")
SAGE = colors.HexColor("#5e7a4a")
RULE = colors.HexColor("#ebebec")

PAGE_W, PAGE_H = A4
MARGIN_X = 18 * mm
# reportlab's frame keeps 6 pt of padding on each side inside the page margins.
TEXT_X = MARGIN_X + 6
CONTENT_W = PAGE_W - 2 * TEXT_X
COVER_MAX_H = 100 * mm
NBSP = "\u00a0"
# reportlab keeps its font registry and font subsetting state in module globals and does
# not promise thread safety; FastAPI runs this sync route on a thread pool.
_BUILD_LOCK = threading.Lock()

_DEAL = {o.value: o.label_cs for o in CATEGORY_TYPE_OPTIONS}
_BUILDING = {o.value: o.label_cs for o in BUILDING_TYPE_OPTIONS}
_CONDITION = {o.value: o.label_cs for o in CONDITION_OPTIONS}
_FURNISHED = {o.value: o.label_cs for o in FURNISHED_OPTIONS}
_OWNERSHIP = {o.value: o.label_cs for o in OWNERSHIP_OPTIONS}
_PORTAL = {o.value: o.label_cs for o in PORTAL_OPTIONS}
_PRICE_UNIT = {o.value: o.label_cs for o in PRICE_UNIT_OPTIONS}
# Singular nouns, as the SPA's enums.CATEGORY_MAIN_LABELS (the registry's are plural).
_CATEGORY_MAIN = {
    "byt": "Byt",
    "dum": "Dům",
    "komercni": "Komerční prostor",
    "pozemek": "Pozemek",
    "ostatni": "Ostatní",
}


@cache
def _font_chars() -> frozenset[int]:
    for name, file in (
        (SANS, "Inter-Regular.ttf"),
        (SANS_BOLD, "Inter-SemiBold.ttf"),
        (DISPLAY, "Fraunces-SemiBold.ttf"),
    ):
        pdfmetrics.registerFont(TTFont(name, str(_FONT_DIR / file)))
    return frozenset(pdfmetrics.getFont(SANS).face.charToGlyph)


def _hex(c: colors.Color) -> str:
    return "#" + c.hexval()[2:]


def _clean(text: str) -> str:
    """Portal free text minus what Inter cannot draw (emoji, mostly), escaped for Paragraph."""
    chars = _font_chars()
    return escape("".join(c for c in text if ord(c) in chars or c in "\n\t"))


def _style(name: str, **kw: Any) -> ParagraphStyle:
    return ParagraphStyle(name, **{"fontName": SANS, "textColor": INK, **kw})


def _styles() -> dict[str, ParagraphStyle]:
    return {
        "eyebrow": _style("eyebrow", fontName=SANS_BOLD, fontSize=8, leading=10, textColor=COPPER),
        "status": _style("status", fontSize=8.5, leading=10, textColor=INK_2, alignment=TA_RIGHT),
        "title": _style("title", fontName=DISPLAY, fontSize=22, leading=26, spaceBefore=6),
        "place": _style("place", fontSize=11, leading=15, textColor=INK_2, spaceBefore=4),
        "price": _style("price", fontName=DISPLAY, fontSize=26, leading=31, spaceBefore=8),
        "section": _style("section", fontName=SANS_BOLD, fontSize=8, leading=10, textColor=INK_3),
        "label": _style("label", fontSize=8.5, leading=11, textColor=INK_3),
        "value": _style("value", fontSize=10, leading=13),
        "body": _style("body", fontSize=10, leading=14.5, spaceAfter=6),
        "small": _style("small", fontSize=8.5, leading=11.5, textColor=INK_3),
        "link": _style("link", fontSize=8.5, leading=11.5, textColor=COPPER),
        "name": _style("name", fontName=SANS_BOLD, fontSize=11, leading=14),
        "th": _style("th", fontName=SANS_BOLD, fontSize=8, leading=10, textColor=INK_3),
    }


# --- formatting (the SPA's lib/format.ts, in Python) ---------------------------------


def _num(n: float | Decimal) -> str:
    # Math.round, as the page: half up, where Python's round() would go half to even.
    return f"{math.floor(float(n) + 0.5):,}".replace(",", NBSP)


def _czk(n: float | Decimal | None) -> str:
    return "—" if n is None else f"{_num(n)}{NBSP}Kč"


def _area(n: float | Decimal | None) -> str | None:
    return None if n is None else f"{_num(n)}{NBSP}m²"


def _floor(floor: int | None, total: int | None) -> str | None:
    """fmtFloor: ground = 0 on every portal; total_floors counts podlaží, ground included."""
    if floor is None:
        return None
    storey = (
        "přízemí" if floor == 0
        else "suterén" if floor == -1
        else f"{-floor}. podzemní podlaží" if floor < 0
        else f"{floor}. patro"
    )
    return storey if total is None else f"{storey} z {total} podlaží"


def _day(value: datetime | date | None) -> str:
    if value is None:
        return "—"
    d = value.date() if isinstance(value, datetime) else value
    return f"{d.day}.{NBSP}{d.month}.{NBSP}{d.year}"


def _phone(p: str) -> str:
    """prettyPhone: 420731404040 -> +420 731 404 040; anything else as stored."""
    if p.startswith("420") and len(p) == 12:
        n = p[3:]
        return f"+420 {n[:3]} {n[3:6]} {n[6:]}"
    return p


def _label(labels: dict[str, str], slug: str | None) -> str | None:
    """The registry's Czech label; a value outside it is a portal's own word, shown as such."""
    if not slug:
        return None
    return labels.get(slug) or slug[:1].upper() + slug[1:]


def type_label(prop: dict[str, Any]) -> str:
    """listingTypeLabel: the subtype when the portal gave one, else the category noun."""
    return subtype_label_cs(prop.get("subtype")) or _CATEGORY_MAIN.get(
        prop.get("category_main") or "", "Nemovitost"
    )


def headline(prop: dict[str, Any]) -> str:
    """'Byt 2+kk, 54 m²' — the sheet's title."""
    name = " ".join(p for p in (type_label(prop), prop.get("disposition")) if p)
    area = _area(prop.get("area_m2"))
    return f"{name}, {area}" if area else name


# --- sections -------------------------------------------------------------------------


def _section(title: str, st: dict[str, ParagraphStyle]) -> list[Flowable]:
    return [
        HRFlowable(width="100%", thickness=0.6, color=RULE, spaceBefore=14, spaceAfter=8),
        Paragraph(title.upper(), st["section"]),
        Spacer(1, 6),
    ]


def _facts(prop: dict[str, Any]) -> list[tuple[str, str]]:
    """The page's facts strip (lib/listingFacts.buildFacts) plus the header's kind,
    area and floor; a fact shows only when we have it."""
    plot = prop.get("category_main") == "pozemek"
    facts = [
        ("Druh", type_label(prop)),
        ("Dispozice", prop.get("disposition")),
        ("Plocha pozemku" if plot else "Plocha", _area(prop.get("area_m2"))),
        ("Podlaží", _floor(prop.get("floor"), prop.get("total_floors"))),
        ("Pozemek", _area(prop.get("estate_area"))),
        ("Zahrada", _area(prop.get("garden_area"))),
        ("Stavba", _label(_BUILDING, prop.get("building_type"))),
        ("Stav", _label(_CONDITION, prop.get("condition"))),
        ("Energetická třída", prop.get("energy_rating")),
        ("Vlastnictví", _OWNERSHIP.get(prop.get("ownership") or "")),
        ("Vybavení", _FURNISHED.get(prop.get("furnished") or "")),
    ]
    return [(k, v) for k, v in facts if v]


def _facts_table(facts: list[tuple[str, str]], st: dict[str, ParagraphStyle]) -> Table:
    cells = [(Paragraph(escape(k), st["label"]), Paragraph(escape(v), st["value"])) for k, v in facts]
    if len(cells) % 2:
        cells.append(("", ""))
    rows = [[*cells[i], *cells[i + 1]] for i in range(0, len(cells), 2)]
    col = CONTENT_W / 2
    table = Table(rows, colWidths=[col * 0.38, col * 0.62] * 2, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, RULE),
    ]))
    return table


def _amenities(prop: dict[str, Any]) -> str | None:
    """buildAmenities: an unknown (NULL) amenity drops out — unknown is not absent."""
    lots = prop.get("parking_lots")
    items = [
        ("Balkon", prop.get("has_balcony"), None),
        ("Terasa", prop.get("terrace"), None),
        ("Výtah", prop.get("has_lift"), None),
        ("Sklep", prop.get("cellar"), None),
        ("Garáž", prop.get("garage"), None),
        ("Parkování", prop.get("has_parking"),
         f"{lots}{NBSP}{'místo' if lots == 1 else 'místa'}" if lots else None),
    ]
    parts = []
    for label, present, note in items:
        if present is None:
            continue
        text = label + (f" ({note})" if note else "")
        if present:
            parts.append(f'<font color="{_hex(COPPER)}">✓</font>{NBSP}{text}')
        else:
            parts.append(f'<font color="{_hex(INK_4)}">✗{NBSP}</font>'
                         f'<font color="{_hex(INK_3)}">{text}</font>')
    return (NBSP * 4 + " ").join(parts) or None


def _description(text: str, st: dict[str, ParagraphStyle]) -> list[Flowable]:
    blocks = [b.strip() for b in text.replace("\r\n", "\n").split("\n\n")]
    return [Paragraph(_clean(b).replace("\n", "<br/>"), st["body"]) for b in blocks if b]


def _link(url: str, text: str) -> str:
    href = escape(url, {'"': "&quot;"})
    return f'<a href="{href}" color="{_hex(COPPER)}">{escape(text)}</a>'


def _coord(v: float) -> str:
    return f"{round(v, 6):g}"


def _location(prop: dict[str, Any], st: dict[str, ParagraphStyle]) -> list[Flowable]:
    out: list[Flowable] = [Paragraph(_clean(prop.get("display_label") or "—"), st["value"])]
    lat, lng = prop.get("lat"), prop.get("lng")
    if lat is not None and lng is not None:
        # lib/geoLinks: mapy.com's key-free showmap (center is lon,lat) and Google's search.
        mapy = f"https://mapy.com/fnc/v1/showmap?center={_coord(lng)},{_coord(lat)}&zoom=17&marker=true"
        google = f"https://www.google.com/maps/search/?api=1&query={_coord(lat)},{_coord(lng)}"
        out.append(Spacer(1, 3))
        out.append(Paragraph(
            f"GPS {lat:.6f}, {lng:.6f}{NBSP*3}·{NBSP*3}{_link(mapy, 'Mapy.cz')}"
            f"{NBSP*3}·{NBSP*3}{_link(google, 'Google Maps')}",
            st["small"],
        ))
    return out


def _contact(label: str, value: str | None, on_file: bool | None, href: str | None) -> str | None:
    if value:
        shown = _link(href, value) if href else escape(value)
        return f'<font color="{_hex(INK_3)}">{label}</font>{NBSP*2}{shown}'
    if on_file:
        # A masked contact IS on file — saying "none" would misstate it (BrokerContactCard).
        return f'<font color="{_hex(INK_3)}">{label}{NBSP*2}skrytý — jen pro administrátory</font>'
    return None


def _broker_block(b: dict[str, Any], st: dict[str, ParagraphStyle]) -> KeepTogether:
    email = b.get("primary_email")
    phone = b.get("primary_phone")
    lines = [
        _contact("Telefon", _phone(phone) if phone else None, b.get("has_phone"),
                 f"tel:+{phone}" if phone and phone.isdigit() else None),
        _contact("E-mail", email, b.get("has_email"), f"mailto:{email}" if email else None),
    ]
    flow: list[Flowable] = [
        Paragraph(_clean(b.get("broker_display_name") or "Neznámý makléř"), st["name"]),
        Paragraph(_clean(b.get("broker_firm_label") or "nezávislý / neznámá kancelář"), st["small"]),
    ]
    flow += [Paragraph(line, st["value"]) for line in lines if line]
    flow.append(Spacer(1, 8))
    return KeepTogether(flow)


def _ads_table(ads: list[dict[str, Any]], canonical_id: int, st: dict[str, ParagraphStyle]) -> Table:
    rows: list[list[Any]] = [[
        Paragraph("Portál", st["th"]),
        Paragraph("Stav", st["th"]),
        Paragraph("Cena", st["th"]),
        Paragraph("Poprvé viděn", st["th"]),
    ]]
    for ad in ads:
        portal = escape(_PORTAL.get(ad.get("source") or "", ad.get("source") or "—"))
        if ad.get("id") == canonical_id:
            portal += f'<font color="{_hex(INK_3)}">{NBSP}· hlavní inzerát</font>'
        cell: list[Flowable] = [Paragraph(portal, st["value"])]
        if ad.get("source_url"):
            cell.append(Paragraph(_link(ad["source_url"], ad["source_url"]), st["link"]))
        rows.append([
            cell,
            Paragraph("Aktivní" if ad.get("is_active") else "Neaktivní", st["value"]),
            Paragraph(_czk(ad.get("price_czk")), st["value"]),
            Paragraph(_day(ad.get("first_seen_at")), st["value"]),
        ])
    table = Table(rows, colWidths=[CONTENT_W * w for w in (0.52, 0.14, 0.18, 0.16)],
                  hAlign="LEFT", repeatRows=1)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, RULE),
    ]))
    return table


def _cover(photo: bytes | None) -> Image | None:
    """The gallery's first photo, as wide as the text and no taller than COVER_MAX_H.
    A photo that won't decode just leaves the sheet without one."""
    if not photo:
        return None
    try:
        w, h = ImageReader(io.BytesIO(photo)).getSize()
    except Exception:
        return None
    if not w or not h:
        return None
    scale = min(CONTENT_W / w, COVER_MAX_H / h)
    return Image(io.BytesIO(photo), width=w * scale, height=h * scale)


# --- the document ---------------------------------------------------------------------


def render_property_sheet(
    prop: dict[str, Any],
    ads: list[dict[str, Any]],
    brokers: list[dict[str, Any]],
    *,
    brokers_from_inactive: bool,
    cover: bytes | None,
    today: date,
) -> bytes:
    """One property as a PDF. `prop` is its properties_public row, `ads` its
    property_sources_public rows, `brokers` the PII-policed listing_broker_public rows
    in the page's order (lib/brokers.propertyBrokers)."""
    with _BUILD_LOCK:
        _font_chars()
    st = _styles()
    story: list[Flowable] = []

    deal = _DEAL.get(prop.get("category_type") or "")
    eyebrow = " · ".join(p for p in (deal, _CATEGORY_MAIN.get(prop.get("category_main") or "")) if p)
    if prop.get("is_active"):
        status = f'<font color="{_hex(SAGE)}">●</font>{NBSP}Aktivní'
    else:
        status = (f'<font color="{_hex(BRICK)}">Neaktivní</font>'
                  f"{NBSP}· naposledy viděn {_day(prop.get('last_seen_at'))}")
    top = Table([[Paragraph(escape(eyebrow.upper()), st["eyebrow"]), Paragraph(status, st["status"])]],
                colWidths=[CONTENT_W * 0.6, CONTENT_W * 0.4])
    top.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                             ("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
    story.append(top)
    story.append(Paragraph(escape(headline(prop)), st["title"]))
    story.append(Paragraph(_clean(prop.get("display_label") or "—"), st["place"]))

    price = prop.get("price_czk")
    if price is None:
        # The seller hid it — real source state, not missing data (ListingOverview).
        story.append(Paragraph(f'<font color="{_hex(INK_3)}" size="18">Cena na vyžádání</font>', st["price"]))
    else:
        unit = _PRICE_UNIT.get(prop.get("price_unit") or "", prop.get("price_unit"))
        unit_html = (f'<font name="{SANS}" size="11" color="{_hex(INK_3)}">'
                     f"{NBSP}/{NBSP}{escape(unit)}</font>") if unit else ""
        story.append(Paragraph(_czk(price) + unit_html, st["price"]))

    cover_image = _cover(cover)
    if cover_image is not None:
        story += [Spacer(1, 10), cover_image]

    facts = _facts(prop)
    amenities = _amenities(prop)
    if facts or amenities:
        block = _section("Parametry", st)
        if facts:
            block.append(_facts_table(facts, st))
        if amenities:
            block += [Spacer(1, 8), Paragraph(amenities, st["value"])]
        story.append(KeepTogether(block))

    description = (prop.get("description") or "").strip()
    if description:
        paragraphs = _description(description, st)
        story.append(KeepTogether(_section("Popis", st) + paragraphs[:1]))
        story += paragraphs[1:]

    story.append(KeepTogether(_section("Lokalita", st) + _location(prop, st)))

    if brokers:
        title = "Makléři" if len(brokers) > 1 else "Makléř"
        if brokers_from_inactive:
            title += " — z neaktivních inzerátů"
        story += _section(title, st)
        story += [_broker_block(b, st) for b in brokers]

    if ads:
        title = "Inzeráty" if len(ads) > 1 else "Inzerát"
        story.append(KeepTogether(_section(title, st) + [_ads_table(ads, prop.get("listing_id"), st)]))

    footer = f"Nemovitost č. {prop.get('property_id')} · stav k {_day(today)}"

    def _decorate(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.6)
        canvas.line(TEXT_X, 14 * mm, PAGE_W - TEXT_X, 14 * mm)
        canvas.setFont(SANS, 7.5)
        canvas.setFillColor(INK_3)
        canvas.drawString(TEXT_X, 10 * mm, footer)
        canvas.drawRightString(PAGE_W - TEXT_X, 10 * mm, f"Strana {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN_X,
        rightMargin=MARGIN_X,
        topMargin=16 * mm,
        bottomMargin=20 * mm,
        title=" · ".join(p for p in (type_label(prop), prop.get("disposition"),
                                     prop.get("display_label")) if p),
        author="",
        creator="",
    )
    with _BUILD_LOCK:
        doc.build(story, onFirstPage=_decorate, onLaterPages=_decorate)
    return buf.getvalue()
