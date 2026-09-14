"""W10: a gone page is not a body.

Three gates, all offline.

1. THE STAMP SAYS WHAT IT DOES. Migration 519 corrects `http_status` to 410 on the
   stored bazos bodies whose bytes ARE the category-index page a removed ad answers
   with, and on nothing else: both #1451 signals must fire, the archived HTML must
   hash to the payload row's own `body_sha256`, and only a 2xx/NULL row is touched.
   Nothing is deleted.

2. THE PASS ALREADY READS THE STAMP. `location_data.claims_intake` asks for the
   latest body whose fetch SUCCEEDED in all three places it looks at a payload row,
   so a 410 row stops being "the latest body" everywhere at once and the previous
   version becomes minable with no downstream filter. That is why the fix is a
   status and not a flag — pin the three predicates so a fourth reader cannot be
   added without one.

3. A GONE PAGE IS NEVER STORED AGAIN. Every HTML portal decides "gone" inside its
   client's `fetch_detail`, BEFORE the body is returned to the caller that stages it
   (`scraper.db.upsert_portal_raw_page`, the one chokepoint the payload archive hangs
   off). The guard is structural, not a filter: there is no body for the writer to
   refuse. Asserted per portal, and against a live body at the same ref so the test
   cannot pass by raising for the wrong reason.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from location_data import claims_intake
from scraper import (
    bazos_client,
    ceskereality_client,
    idnes_client,
    mmreality_client,
    realitymix_client,
    remax_client,
)
from scraper.portal_base import ListingGoneError

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "519_location_w10_gone_bodies.sql"
).read_text(encoding="utf-8")

# The `do $$ ... $$` block that does the stamping — the readout and the assertion
# blocks read the same shape and must not be mistaken for it.
_UPDATE = _MIGRATION.split("update portal_raw_payloads p")[1].split("get diagnostics")[0]


def test_the_stamp_is_410_and_only_410():
    assert "set http_status = 410" in _UPDATE
    # 410 is the truthful status of a removed ad, not a new column and not a flag.
    assert "alter table" not in _MIGRATION.lower()
    assert "delete from" not in _MIGRATION.lower()


def test_the_stamp_only_touches_a_successful_bazos_detail_body():
    assert "(p.http_status is null or p.http_status between 200 and 299)" in _UPDATE
    assert "p.source = 'bazos'" in _UPDATE
    assert "p.page_kind = 'detail'" in _UPDATE
    assert "r.source = 'bazos'" in _UPDATE
    assert "r.page_kind = 'detail'" in _UPDATE


def test_both_gone_signals_must_fire_and_the_bytes_must_match():
    # The conjunction, not #1451's disjunction: this stamp is retroactive.
    assert _UPDATE.count("r.html ilike") == 2
    assert "inzerce - Reality | Bazoš.cz%'" in _UPDATE
    assert "%Inzerát byl vymazán%" in _UPDATE
    # The archived HTML must be THIS payload row's bytes: content hash, corroborated
    # by the one-transaction timestamp both writers share.
    assert "p.body_sha256 = sha256(convert_to(r.html, 'UTF8'))" in _UPDATE
    assert "abs(extract(epoch from (r.fetched_at - p.last_observed_at))) <= 5" in _UPDATE


def test_the_stamp_is_idempotent_and_batched():
    # A second run re-reads the same pages and finds every hit already at 410.
    assert "set statement_timeout = '900s'" in _MIGRATION
    assert "commit;" in _MIGRATION
    # The keyset probe is what makes the file a no-op on the CI replay's empty tables.
    assert "exit when v_hi is null" in _MIGRATION


_OK_BODY = "(p.http_status is null or p.http_status between 200 and 299)"


@pytest.mark.parametrize(
    "name,fragment",
    [
        ("_BODY_JOIN", claims_intake._BODY_JOIN),
        ("_UNMINED_WINDOW_WHERE", claims_intake._UNMINED_WINDOW_WHERE),
        ("_LATEST_BODY_ONLY", claims_intake._LATEST_BODY_ONLY),
    ],
)
def test_every_payload_predicate_skips_a_gone_body(name: str, fragment: str):
    """"Latest body" means latest body whose fetch succeeded — in all three places."""
    normalised = re.sub(r"\bp2?\.", "p.", fragment.lower())
    assert _OK_BODY in normalised, name


# (module, client class, a ref whose built URL is on the portal's detail path,
#  a body carrying one of that portal's own removed-listing markers)
_PORTALS = [
    (
        bazos_client,
        bazos_client.BazosClient,
        "/inzerat/1/x.php",
        "<title>Byty inzerce - Reality | Bazoš.cz</title><b>Inzerát byl vymazán.</b>",
    ),
    (mmreality_client, mmreality_client.MmRealityClient, "1", "Nabídka již není aktivní"),
    (
        ceskereality_client,
        ceskereality_client.CeskerealityClient,
        "/prodej/byt-1.html",
        "Nemovitost již byla smazána",
    ),
    (
        realitymix_client,
        realitymix_client.RealitymixClient,
        "/detail/byt-1.html",
        "Nabídka již neexistuje",
    ),
    (remax_client, remax_client.RemaxClient, "1", "Tato nemovitost již není v nabídce"),
    (
        idnes_client,
        idnes_client.IdnesClient,
        "/detail/prodej/byt/1/",
        "Nabídka již není aktivní",
    ),
]


class _Response:
    def __init__(self, text: str, url: str) -> None:
        self.status_code = 200
        self.text = text
        self.url = url
        self.headers = {"Content-Type": "text/html"}


def _client(cls, text: str):
    client = object.__new__(cls)
    client._request = lambda url: _Response(text, url)  # noqa: SLF001
    return client


@pytest.mark.parametrize(
    "module,cls,ref,gone_body",
    _PORTALS,
    ids=[p[0].__name__.split(".")[-1] for p in _PORTALS],
)
def test_a_gone_page_is_never_returned_to_the_writer(module, cls, ref, gone_body):
    """The body the archive would store never exists: the client raises first.

    `scraper.db.upsert_portal_raw_page` (and with it `append_payload` behind it) is
    only ever reached with the `(text, status)` a client RETURNED, so a fetch that
    raises cannot store anything. Paired with a live body at the same ref, which
    proves the ref is on the detail path and that the raise is the marker's doing.
    """
    live, status = _client(cls, "<html><h1>Byt 3+1</h1></html>").fetch_detail(ref)
    assert status == 200 and "Byt 3+1" in live

    with pytest.raises(ListingGoneError):
        _client(cls, gone_body).fetch_detail(ref)
