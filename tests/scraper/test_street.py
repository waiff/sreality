"""Unit tests for the shared street extractor (scraper/street.py).

The cases are drawn from the LIVE fabrication traps: idnes ~37% foreign
localities + "Town - Quarter" tails, maxima village last-segments, bazos
prefix/description pollution. A wrong street is worse than NULL, so the bias is
toward returning None when uncertain."""

from __future__ import annotations

import pytest

from scraper.street import (
    clean_street,
    looks_like_czech_street,
    reject_as_town,
    street_from_locality,
)


class TestCleanStreet:
    @pytest.mark.parametrize("raw,expected", [
        (None, None),
        ("", None),
        ("   ", None),
        # bazos prefix decoration -> stripped (uniform with sreality bare names)
        ("ul. Neumannova", "Neumannova"),
        ("ul. Sokolovská", "Sokolovská"),
        ("ulice Klapálkova", "Klapálkova"),
        ("ulici Technického", "Technického"),
        # bazos description bleed (space + glued)
        ("ul. Vrchlického Nabízíme", "Vrchlického"),
        ("ul. SNP Nabízíme", "SNP"),
        ("ul. MasarykovaNabízíme", "Masarykova"),
        ("MasarykovaNabízíme Vám", "Masarykova"),
        ("ul. Jakuba Obrovského Nabízíme", "Jakuba Obrovského"),
        # trailing house number stripped (bare name; bazos has no house_number)
        ("Slavíčkova 3", "Slavíčkova"),
        ("Koterovská 12/45", "Koterovská"),
        # náměstí / třída kept (integral to the proper name)
        ("náměstí Míru", "náměstí Míru"),
        ("Vinohradská třída", "Vinohradská třída"),
        # genuine multi-word streets preserved
        ("ulice Víta Nejedlého", "Víta Nejedlého"),
        ("ul. Za Penzionem", "Za Penzionem"),
        # already-clean structured value is a no-op
        ("Jesenická", "Jesenická"),
    ])
    def test_clean(self, raw, expected):
        assert clean_street(raw) == expected


class TestLooksLikeStreet:
    @pytest.mark.parametrize("name", [
        "Huťská", "Trojická", "Kozinova", "Brodského", "Hornoměcholupská",
        "U Hotelu", "Pod Višňovkou", "náměstí 1. máje", "Na Výsluní",
    ])
    def test_streets(self, name):
        assert looks_like_czech_street(name) is True

    @pytest.mark.parametrize("name", [
        "Velká Hraštice", "Hlubočinka", "Vesce", "Předlánce", "Mokrá",
        None, "",
    ])
    def test_villages(self, name):
        assert looks_like_czech_street(name) is False


class TestRejectAsTown:
    def test_empty_rejected(self):
        assert reject_as_town(None) is True
        assert reject_as_town("") is True

    def test_foreign_country(self):
        assert reject_as_town("Španělsko") is True

    def test_town_quarter_form(self):
        assert reject_as_town("Praha 9 - Klánovice") is True

    def test_okres_qualifier(self):
        assert reject_as_town("okres Břeclav") is True

    def test_foreign_coords(self):
        assert reject_as_town("Marbella", lat=36.5, lon=-4.9) is True

    def test_cz_coords_ok(self):
        assert reject_as_town("Sokolovská", lat=50.1, lon=14.4) is False

    def test_matches_geo_name(self):
        assert reject_as_town("Roztoky", geo_names=("Roztoky",)) is True
        assert reject_as_town("Roztoky", geo_names=("roztoky",)) is True  # folded

    def test_real_street_kept(self):
        assert reject_as_town("Bělehradská", geo_names=("Pardubice",)) is False


class TestIdnesFirstSegment:
    @pytest.mark.parametrize("locality,expected", [
        ("Bělehradská, Pardubice - Polabiny", "Bělehradská"),
        ("Sokolovská, Plzeň - Severní Předměstí", "Sokolovská"),
        ("Boženy Němcové, Sokolov", "Boženy Němcové"),
        ("K Haltýři, Praha 8 - Troja", "K Haltýři"),
        # fabrication traps -> None
        ("Estepona, Španělsko", None),
        ("Dubaj, Spojené arabské emiráty", None),
        ("Studénka, okres Nový Jičín", None),
        ("Bavory, okres Břeclav", None),
        ("Třinec - Konská, okres Frýdek-Místek", None),
        ("Brno", None),
        ("Praha", None),
    ])
    def test_extract(self, locality, expected):
        assert street_from_locality(locality, position="first") == expected

    def test_foreign_coords_guard(self):
        assert street_from_locality(
            "Marbella, Málaga", position="first", lat=36.5, lon=-4.9
        ) is None

    def test_geo_cross_check(self):
        # obec name leaking as the first segment is rejected by geo cross-check
        assert street_from_locality(
            "Studénka, Studénka", position="first", geo_names=("Studénka",)
        ) is None


class TestMaximaLastSegment:
    @pytest.mark.parametrize("locality,expected", [
        ("Praha 6, Suchdol, U Hotelu", "U Hotelu"),
        ("Praha 4, Chodov, Brodského", "Brodského"),
        ("Kladno, Huťská", "Huťská"),
        ("Chrastava, náměstí 1. máje", "náměstí 1. máje"),
        # village last-segments -> None (require_morphology)
        ("Malá Hraštice, Velká Hraštice", None),
        ("Sulice, Hlubočinka", None),
        ("Týn nad Vltavou, Vesce", None),
        ("Frýdlant", None),
    ])
    def test_extract(self, locality, expected):
        assert street_from_locality(
            locality, position="last", require_morphology=True
        ) == expected

    def test_drops_area_prefix(self):
        assert street_from_locality(
            "114 m², Praha 6, Suchdol, U Hotelu", position="last",
            require_morphology=True,
        ) == "U Hotelu"


class TestRemaxDataAddress:
    def test_street_present(self):
        assert street_from_locality(
            "Na vrcholu, Praha 3 - Žižkov, Praha", position="first",
            geo_names=("Praha 3 - Žižkov",),
        ) == "Na vrcholu"

    def test_town_quarter_only(self):
        assert street_from_locality(
            "Praha 9 - Klánovice, Praha", position="first"
        ) is None

    def test_town_only(self):
        # data-address that is just the municipality -> no street
        assert street_from_locality("Roztoky", position="first") is None

    def test_geo_rejects_town_as_street(self):
        # parts[0] equals the listing's own locality (from the title) -> not a street
        assert street_from_locality(
            "Roztoky, Praha-západ", position="first", geo_names=("Roztoky",)
        ) is None
