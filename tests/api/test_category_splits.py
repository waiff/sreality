"""GET /autodedup/category-splits — the category review (E937), read-only.

The connection is faked and dispatches on the statement (the module's own constants, reused).
The properties echo the production cases the page was built for: 12664 (six sale ads and three
rental ads of one flat), 9737 (two flat ads and two commercial ads, every pair across ruled
`same`: one side since E938), 53488 (two house ads, a flat ad and a commercial ad: the commercial
ad starts on the flat's side, E938), 914 (nineteen rentals and one contentless Bazoš record
stored under `byt` / `prodej`), 197654 (two house sales and a share sale) and 31 (an ad of
unknown deal type rides along); 36 (a commercial canonical ad between a flat and a house) and 41
(a flat and a house ruled `same`) are made up. 5 holds one ad, 6 was merged away, 999 does not
exist.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from autodedup import ui_sql as usql
from toolkit import property_identity as pi

AT = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
SALE, RENT = 2_990_000, 9_500


def _ad(pid: int, lid: int, source: str, deal: str | None, main: str | None, *,
        price: int | None = SALE, area: float | None = 30.0, disposition: str | None = "1+kk",
        canonical: int | None = None, active: bool = True) -> tuple[Any, ...]:
    return (pid, canonical, lid, source, active, deal, main, price, area, disposition)


ADS = [
    *(_ad(12664, 1266400 + i, src, "prodej", "byt") for i, src in enumerate(
        ("sreality", "idnes", "bazos", "remax", "ceskereality", "realitymix"), start=1)),
    *(_ad(12664, 1266400 + i, src, "pronajem", "byt", price=price) for i, src, price in (
        (7, "sreality", 9_500), (8, "idnes", 9_800), (9, "bezrealitky", 10_000))),
    _ad(9737, 973701, "sreality", "prodej", "komercni", area=58.0),
    _ad(9737, 973702, "sreality", "prodej", "komercni", area=58.0),
    _ad(9737, 973703, "idnes", "prodej", "byt", area=58.0),
    _ad(9737, 973704, "idnes", "prodej", "byt", area=58.0),
    _ad(53488, 534881, "sreality", "prodej", "dum", area=251.0, disposition=None),
    _ad(53488, 534882, "idnes", "prodej", "dum", area=251.0, disposition=None),
    _ad(53488, 534883, "bazos", "prodej", "byt", area=251.0, disposition=None),
    _ad(53488, 534884, "remax", "prodej", "komercni", area=251.0, disposition=None),
    *(_ad(914, 91400 + i, "sreality", "pronajem", "byt", price=RENT) for i in range(19)),
    _ad(914, 91419, "bazos", "prodej", "byt", price=None, area=None, disposition=None),
    _ad(197654, 1976541, "sreality", "prodej", "dum", price=6_500_000, area=140.0),
    _ad(197654, 1976542, "idnes", "prodej", "dum", price=6_500_000, area=140.0),
    _ad(197654, 1976543, "sreality", "podil", "dum", price=1_625_000, area=140.0),
    _ad(31, 3101, "sreality", "prodej", "byt"),
    _ad(31, 3102, "idnes", "pronajem", "byt", price=RENT),
    _ad(31, 3103, "realitymix", None, "byt"),
    _ad(36, 3601, "sreality", "prodej", "komercni"),
    _ad(36, 3602, "idnes", "prodej", "byt"),
    _ad(36, 3603, "remax", "prodej", "dum"),
    _ad(41, 4101, "sreality", "prodej", "byt"),
    _ad(41, 4102, "idnes", "prodej", "dum"),
    _ad(5, 501, "sreality", "prodej", "byt"),
]
CANONICAL = {12664: 1266401, 9737: 973701, 53488: 534881, 914: 91400, 197654: 1976541,
             31: 3101, 36: 3601, 41: 4101, 5: 501}
ADS = [(*a[:1], CANONICAL[a[0]], *a[2:]) for a in ADS]
LIVE = set(CANONICAL)  # 6 is merged away, 999 never existed

TEXTS = {
    1266401: ("Prodej bytu 1+kk, 30 m²",
              "Byt 1+kk ve 4. patře. Volejte 777 123 456 nebo pis@example.cz."),
    1266407: ("Pronájem bytu 1+kk, 30 m²", "Pronájem od listopadu, 9 500 Kč/měs."),
    91419: ("Byt 1+kk", None),
}


def _verdict(lo: int, hi: int, verdict: str, at: datetime = AT) -> tuple[Any, ...]:
    values = {"listing_lo": lo, "listing_hi": hi, "verdict": verdict, "kind": "pair",
              "decided_by": "op@example.com", "decided_at": at, "reasons": [], "note": None}
    return tuple(values.get(c) for c in usql.VERDICT_COLUMNS)


VERDICTS = [  # newest first
    *(_verdict(lo, hi, "same") for lo in (973701, 973702) for hi in (973703, 973704)),
    _verdict(4101, 4102, "same"),
    _verdict(1266401, 1266407, "different"),
    _verdict(1976541, 1976543, "unsure"),
    _verdict(1976541, 1976543, "same", datetime(2026, 9, 1, tzinfo=timezone.utc)),
]


class _Conn:
    def __init__(self) -> None:
        self.ready, self.calls = True, []
        # 1266408/09 came from 50008/50009 by merges; 973703/04 from 9738; 534883 from 53489.
        self.moves = [(1266408, 1, "g1", 12664, 50008, "operator", AT),
                      (1266409, 2, "g2", 12664, 50009, "autodedup", AT),
                      (973703, 3, "g3", 9737, 9738, "operator", AT),
                      (973704, 4, "g3", 9737, 9738, "operator", AT),
                      (534883, 5, "g5", 53488, 53489, "operator", AT)]
        self.status = {50008: ("merged_away", 12664), 50009: ("merged_away", 12664),
                       9738: ("merged_away", 9737), 53489: ("merged_away", 53488)}

    def cursor(self) -> "_Conn":
        return self

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *_: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))
        ids = set((params or {}).get("ids") or [])
        if "to_regclass" in sql:
            self.rows = [(self.ready,)]
        elif sql == usql.CATEGORY_SPLIT_ADVERTS_SQL:
            self.rows = sorted((a for a in ADS if a[0] in ids and a[0] in LIVE),
                               key=lambda a: (a[0], a[2]))
        elif sql == usql.MEMBER_TEXT_SQL:
            self.rows = [(a[2], *TEXTS.get(a[2], (None, f"Inzerát {a[2]}")))
                         for a in ADS if a[2] in ids]
        elif sql == usql.MEMBER_PAIR_VERDICTS_SQL:
            self.rows = [v for v in VERDICTS if v[3] in ids and v[4] in ids]
        elif sql == pi._LIVE_MOVES_SQL:
            self.rows = [m for m in self.moves if m[0] in ids]
        elif sql == pi._PLACES_SQL:
            self.rows = [(a[2], a[0]) for a in ADS if a[2] in ids]
        elif sql == pi._SIZES_SQL:
            merged = {m[0] for m in self.moves}
            self.rows = [(pid, sum(a[0] == pid for a in ADS),
                          sum(a[0] == pid and a[2] not in merged for a in ADS))
                         for pid in sorted(ids)]
        elif sql == pi._STATUS_SQL:
            self.rows = [(pid, *self.status[pid]) for pid in sorted(ids) if pid in self.status]
        else:
            raise AssertionError(f"unexpected statement: {sql[:80]}")

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return list(self.rows)


@pytest.fixture()
def conn() -> _Conn:
    return _Conn()


@pytest.fixture()
def client(conn: _Conn):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: conn
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


ASKED = "914,12664,5,9737,53488,197654,6,31,36,41,999"


def _items(client) -> dict[int, dict[str, Any]]:
    data = client.get(f"/autodedup/category-splits?properties={ASKED}").json()["data"]
    return {item["property_id"]: item for item in data["items"]}


def _ids(group: dict[str, Any]) -> list[int]:
    return [a["listing_id"] for a in group["adverts"]]


def test_the_items_come_in_the_order_asked_and_the_rest_is_missing(client, conn):
    body = client.get(f"/autodedup/category-splits?properties={ASKED}").json()
    assert body["store_ready"] is True
    assert [i["property_id"] for i in body["data"]["items"]] == [914, 12664, 9737, 53488,
                                                                 197654, 31, 36, 41]
    assert body["data"]["missing"] == [5, 6, 999]
    assert not any(s.lstrip().lower().startswith(("insert", "update", "delete"))
                   for s, _p in conn.calls), "the review is read-only"


def test_a_sale_and_a_rental_of_one_flat_are_two_sides(client):
    one = _items(client)[12664]
    assert (one["mixed"], one["confirmed"], one["ruled"], one["unseen"]) == (True, False, False, [])
    assert one["canonical_listing_id"] == 1266401
    sale, rent = one["groups"]
    assert (sale["label"], sale["kept"], sale["cluster_key"], _ids(sale)) == (
        "prodej · byt", True, None, list(range(1266401, 1266407)))
    assert (rent["label"], rent["kept"], _ids(rent)) == ("pronajem · byt", False,
                                                        [1266407, 1266408, 1266409])
    assert (rent["category_type"], rent["category_main"]) == ("pronajem", ["byt"])
    assert one["splits"] == [{
        "listing_lo": 1266401, "listing_hi": 1266407, "reason_source": "category",
        "reason": "category_type: prodej vs pronajem",
        "ruling": {"verdict": "different", "decided_by": "op@example.com", "note": None,
                   "decided_at": AT.isoformat(), "reasons": []}}]


def test_each_ad_carries_the_proposed_splits_fields_and_its_scrubbed_text(client):
    rent = _items(client)[12664]["groups"][1]["adverts"]
    assert rent[0] == {
        "listing_id": 1266407, "source": "sreality", "is_active": True,
        "origin_property_id": None, "detach_outcome": "split_native", "splittable": True,
        "empty": False, "unknown": False,
        "text": {"title": "Pronájem bytu 1+kk, 30 m²",
                 "description": "Pronájem od listopadu, 9 500 Kč/měs."}}
    assert (rent[1]["origin_property_id"], rent[1]["detach_outcome"], rent[1]["splittable"]) == (
        50008, "detached", True)
    text = _items(client)[12664]["groups"][0]["adverts"][0]["text"]
    assert "777 123 456" not in text["description"] and "pis@example.cz" not in text["description"]
    assert "[telefon]" in text["description"] and "[email]" in text["description"]


def test_a_commercial_ad_beside_a_flat_and_a_house_starts_on_the_flat_s_side(client):
    """E938: the commercial ad may be one property with the flat or with the house, never with
    both; no side holds a clash."""
    one = _items(client)[53488]
    assert [(g["label"], _ids(g)) for g in one["groups"]] == [
        ("prodej · dum", [534881, 534882]), ("prodej · byt + komercni", [534883, 534884])]
    assert [s["reason"] for s in one["splits"]] == ["category_main: dum vs byt"]
    assert one["groups"][1]["adverts"][0]["origin_property_id"] == 53489


def test_a_flat_and_a_commercial_unit_are_one_side_and_the_property_is_not_mixed(client):
    """9737 since E938: a studio filed as a flat and as a commercial unit is one property by the
    rule; its two units are a question of letters on the property page, not of categories."""
    one = _items(client)[9737]
    assert [(g["label"], g["kept"], _ids(g)) for g in one["groups"]] == [
        ("prodej · byt + komercni", True, [973701, 973702, 973703, 973704])]
    assert (one["mixed"], one["confirmed"], one["ruled"], one["splits"]) == (
        False, False, False, [])


def test_two_sides_are_named_by_their_first_clashing_pair(client):
    """36: the canonical ad is commercial, which a house does not clash with; the pair that
    keeps the sides apart is the flat and the house."""
    one = _items(client)[36]
    assert [(g["label"], _ids(g)) for g in one["groups"]] == [
        ("prodej · byt + komercni", [3601, 3602]), ("prodej · dum", [3603])]
    assert [(s["listing_lo"], s["listing_hi"], s["reason"]) for s in one["splits"]] == [
        (3602, 3603, "category_main: byt vs dum")]


def test_every_pair_across_sides_ruled_same_is_confirmed(client):
    one = _items(client)[41]
    assert (one["mixed"], one["confirmed"], one["ruled"]) == (True, True, True)
    assert [(g["label"], g["kept"]) for g in one["groups"]] == [
        ("prodej · byt", True), ("prodej · dum", False)]
    assert one["splits"][0]["ruling"]["verdict"] == "same"


def test_a_newer_unsure_takes_the_same_back(client):
    one = _items(client)[197654]
    assert [_ids(g) for g in one["groups"]] == [[1976541, 1976542], [1976543]]
    assert one["splits"][0]["reason"] == "category_type: prodej vs podil"
    assert one["splits"][0]["ruling"]["verdict"] == "unsure"
    assert (one["mixed"], one["confirmed"], one["ruled"]) == (True, False, False)


def test_a_contentless_record_never_makes_a_property_mixed(client):
    one = _items(client)[914]
    assert (one["mixed"], one["confirmed"], one["splits"]) == (False, False, [])
    [group] = one["groups"]
    assert (group["label"], group["kept"]) == ("pronajem · byt", True)
    assert _ids(group) == [*range(91400, 91419), 91419]
    record = group["adverts"][-1]
    assert (record["empty"], record["unknown"], record["source"]) == (True, False, "bazos")
    assert record["text"] == {"title": "Byt 1+kk", "description": None}


def test_an_ad_of_unknown_deal_type_rides_with_the_kept_side(client):
    one = _items(client)[31]
    assert [(g["label"], g["kept"], _ids(g)) for g in one["groups"]] == [
        ("prodej · byt", True, [3101, 3103]), ("pronajem · byt", False, [3102])]
    rider = one["groups"][0]["adverts"][1]
    assert (rider["unknown"], rider["empty"]) == (True, False)


@pytest.mark.parametrize("query", [
    "", "?properties=", "?properties=12,ab", "?properties=1,,2", "?properties=1,",
    "?properties=-1", "?properties=1234567890123456789",
    "?properties=" + ",".join(str(i) for i in range(1, 102)),
])
def test_bad_input_is_a_422(client, query):
    assert client.get(f"/autodedup/category-splits{query}").status_code == 422


def test_a_hundred_ids_are_fine_and_a_repeat_is_one(client):
    many = ",".join(str(i) for i in range(1, 101))
    assert client.get(f"/autodedup/category-splits?properties={many}").status_code == 200
    data = client.get("/autodedup/category-splits?properties=9737,9737,5").json()["data"]
    assert ([i["property_id"] for i in data["items"]], data["missing"]) == ([9737], [5])


def test_unknown_keys_are_refused_and_an_unmigrated_store_renders(client, conn):
    assert client.get("/autodedup/category-splits?properties=1&generation=rt").status_code == 400
    conn.ready = False
    assert client.get("/autodedup/category-splits?properties=12664").json() == {
        "data": None, "store_ready": False}


def test_the_statement_selects_no_broker_column():
    """E28: the text reaches the page only through `scrubbed_text`."""
    assert "broker" not in usql.CATEGORY_SPLIT_ADVERTS_SQL.lower()
    assert "description" not in usql.CATEGORY_SPLIT_ADVERTS_SQL.lower()
