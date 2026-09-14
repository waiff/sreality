"""W10: a gone page is not a body.

Three gates, all offline.

1. THE STAMP SAYS WHAT IT DOES. Migrations 519 and 520 correct `http_status` to 410
   on the stored bazos bodies whose bytes ARE the category-index page a removed ad
   answers with, and on nothing else: both #1451 signals must fire, the archived HTML
   must hash to the payload row's own `body_sha256`, and only a 2xx/NULL row is
   touched. Nothing is deleted. 520 is the one that runs — 519's title pattern carried
   TWO internal wildcards, which makes LIKE quadratic over a 100 KB document (>180 ms
   a page against ~2.2 ms) and spent the statement timeout on prod. Both are pinned,
   and 520 is additionally pinned against ever growing a second wildcard back.

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

_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
_FIRST_CUT = (_MIGRATIONS / "519_location_w10_gone_bodies.sql").read_text(encoding="utf-8")
# The one that runs. Append-only means 519 stays on disk; 520 supersedes its pattern.
_LIVE = (_MIGRATIONS / "520_location_w10_gone_bodies_retry.sql").read_text(encoding="utf-8")

_BOTH = [("519", _FIRST_CUT), ("520", _LIVE)]


def _update_block(migration: str) -> str:
    """The `do $$ ... $$` block that stamps — the readout and the assertion blocks
    read the same shape and must not be mistaken for it."""
    return migration.split("update portal_raw_payloads p")[1].split("get diagnostics")[0]


@pytest.mark.parametrize("name,migration", _BOTH)
def test_the_stamp_is_410_and_only_410(name: str, migration: str):
    assert "set http_status = 410" in _update_block(migration), name
    # 410 is the truthful status of a removed ad, not a new column and not a flag.
    assert "alter table" not in migration.lower(), name
    assert "delete from" not in migration.lower(), name


@pytest.mark.parametrize("name,migration", _BOTH)
def test_the_stamp_only_touches_a_successful_bazos_detail_body(name: str, migration: str):
    block = _update_block(migration)
    assert "(p.http_status is null or p.http_status between 200 and 299)" in block, name
    assert "p.source = 'bazos'" in block, name
    assert "p.page_kind = 'detail'" in block, name
    assert "r.source = 'bazos'" in block, name
    assert "r.page_kind = 'detail'" in block, name


@pytest.mark.parametrize("name,migration", _BOTH)
def test_both_gone_signals_must_fire_and_the_bytes_must_match(name: str, migration: str):
    block = _update_block(migration)
    # The conjunction, not #1451's disjunction: this stamp is retroactive.
    assert block.count("r.html ilike") == 2, name
    assert "inzerce - Reality | Bazoš.cz%'" in block, name
    assert "%Inzerát byl vymazán%" in block, name
    # The archived HTML must be THIS payload row's bytes: content hash, corroborated
    # by the one-transaction timestamp both writers share.
    assert "p.body_sha256 = sha256(convert_to(r.html, 'UTF8'))" in block, name
    assert "abs(extract(epoch from (r.fetched_at - p.last_observed_at))) <= 5" in block, name


@pytest.mark.parametrize("name,migration", _BOTH)
def test_the_stamp_is_idempotent_and_batched(name: str, migration: str):
    # A second run re-reads the same pages and finds every hit already at 410.
    assert "set statement_timeout = '900s'" in migration, name
    assert "commit;" in migration, name
    # The keyset probe is what makes the file a no-op on the CI replay's empty tables.
    assert "exit when v_hi is null" in migration, name


def test_no_like_pattern_backtracks():
    """The regression 519 shipped: a LIKE pattern with TWO internal wildcards.

    `'%<title>%inzerce - Reality | Bazoš.cz%'` makes the matcher retry every
    '<title>' position against every later position, over a ~100 KB document —
    >180 ms a page against ~2.2 ms for the single-wildcard form, which is how a
    5 000-page batch spent a 900 s statement timeout. Every pattern in the file
    that runs must be one leading and one trailing wildcard and nothing else.
    """
    # COMMENTS OUT FIRST: 520's header quotes 519's bad pattern verbatim to explain
    # it, and a scan that reads prose as code would fail on the explanation.
    code = "\n".join(
        line for line in _LIVE.splitlines() if not line.lstrip().startswith("--")
    )
    patterns = re.findall(r"ilike '([^']*)'", code)
    assert patterns, "the stamp reads the archived HTML with ILIKE"
    for pattern in patterns:
        assert pattern.startswith("%") and pattern.endswith("%"), pattern
        assert pattern.count("%") == 2, pattern


def test_the_batch_is_small_enough_that_a_slow_region_is_only_slow():
    assert "order by id limit 1000" in _LIVE
    # A bad day ends in a committed partial pass with a NOTICE, not a killed job.
    assert "v_deadline" in _LIVE


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
