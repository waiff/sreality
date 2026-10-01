"""Census of every write into `listings` outside the one writer (rule 2).

A fetched payload reaches `listings` ONLY through scraper/listing_write.py
`write_listings`, which owns snapshot-on-change. Every other Python write of the table
outside scraper/db.py is payload-free and ledgered here by name with a class from a
closed vocabulary that has no slot for "fetched payload", a reason and a dirty rule
(rule 20). The scan reads raw text, comments included, so SQL spelled with a literal
table name is found wherever it sits (an f-string included); an unledgered `UPDATE
listings` or a count change fails until a reviewer adds or re-pins its line.

Blind spots — a green run does NOT prove these:
(a) scraper/db.py is EXEMPT wholesale: its lifecycle/identity `UPDATE listings` sites
    (touch, delist, singleton link) are owned there and never ledgered.
(b) Only `*.py` under `tests.sql_corpus.RUNTIME_DIRS` is read: DB-side writers
    (triggers such as migration 276's `trg_listings_geo_cell_key`, plpgsql functions,
    pg_cron jobs) are invisible.
(c) The regexes need the literal name: an interpolated table (`f"UPDATE {table} …"`,
    as toolkit/operator_state.py does), `UPDATE ONLY listings`, a quoted identifier,
    `MERGE INTO` and `COPY` are not matched.
(d) Counts are per file, so a ledgered statement swapped for a payload write in the
    same file keeps the count.
(e) The dirty-enqueue and derived-write checks read the whole file's text, not the
    ledgered statement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.sql_corpus import RUNTIME_DIRS

_ROOT = Path(__file__).resolve().parents[2]

_INSERT_LISTINGS = re.compile(r"\bINSERT\s+INTO\s+(?:public\.)?listings\b(?!_)", re.I)
_INSERT_SNAPSHOTS = re.compile(r"\bINSERT\s+INTO\s+(?:public\.)?listing_snapshots\b", re.I)
_UPDATE_LISTINGS = re.compile(r"\bUPDATE\s+(?:public\.)?listings\b(?!_)", re.I)
_WHOLESALE_RAW = re.compile(r"\braw_json\s*=\s*(?:%|EXCLUDED\.)", re.I)
_LAST_SEEN = re.compile(r"\blast_seen_at\s*=", re.I)

WRITER = "scraper/listing_write.py"
EXEMPT = {"scraper/db.py", WRITER}   # lifecycle/identity owners; the writer
CLASSES = {"lifecycle", "identity", "derived", "heal", "attribution", "bookkeeping", "condition"}


@dataclass(frozen=True)
class Site:
    count: int
    cls: str        # one of CLASSES
    reason: str
    dirty: str      # "same-statement" | "separate-statement" | "sweep:<why>" | "n/a" | "gap:<owner>"


LEDGER: dict[str, Site] = {
    "scripts/reparse.py": Site(
        1, "heal", "re-derives --fields from stored substrate, compare-and-set",
        "same-statement"),
    "toolkit/description_extraction.py": Site(
        2, "derived", "NULL-only fill of contract text/none cells (incl. the module docstring)",
        "same-statement"),
    "scripts/clear_unmeasured_enrichment_fills.py": Site(
        1, "heal", "one --column -> NULL, with a backup table", "same-statement"),
    "scripts/backfill_unit_price_masquerade.py": Site(
        1, "heal", "price_czk -> NULL + raw_json quarantine keys via ||", "separate-statement"),
    "scripts/reextract.py": Site(
        1, "heal", "raw_json.broker via jsonb_set", "sweep:daily broker sweep"),
    "scripts/backfill_idnes_brokers.py": Site(
        1, "heal", "raw_json.broker via jsonb_set", "sweep:daily broker sweep"),
    "toolkit/condition_scoring.py": Site(
        2, "condition", "rule 14's two derived levels (lane paused)", "gap:rule-14 paused"),
    "toolkit/broker_sources.py": Site(1, "attribution", "broker_identity_id FK", "n/a"),
    "scripts/resolve_brokers.py": Site(
        2, "attribution", "broker_identity_id FK (and its comment)", "n/a"),
    "scripts/refresh_stale_image_urls.py": Site(
        1, "bookkeeping", "images_refreshed_at cooldown stamp", "n/a"),
    "toolkit/property_identity.py": Site(
        3, "identity", "property_id re-point: the rule-15 merge chokepoint", "n/a"),
    "scraper/freshness.py": Site(1, "lifecycle", "_record_gone's gone flip", "gap:item-3"),
    "scripts/backfill_listing_surrogate_id.py": Site(
        1, "identity", "dead surrogate backfill (separate cleanup PR)", "n/a"),
}


def _runtime_files() -> dict[str, str]:
    out: dict[str, str] = {}
    for d in RUNTIME_DIRS:
        for path in sorted((_ROOT / d).rglob("*.py")):
            if "__pycache__" not in path.parts:
                out[path.relative_to(_ROOT).as_posix()] = path.read_text(encoding="utf-8")
    return out


FILES = _runtime_files()


def _sites(pattern: re.Pattern[str]) -> dict[str, int]:
    return {path: n for path, text in FILES.items() if (n := len(pattern.findall(text)))}


def test_exactly_one_insert_into_listings_and_it_is_the_writer() -> None:
    assert _sites(_INSERT_LISTINGS) == {WRITER: 1}


def test_exactly_one_snapshot_append_and_it_is_the_writer() -> None:
    assert _sites(_INSERT_SNAPSHOTS) == {WRITER: 1}


def test_the_writer_never_updates_listings() -> None:
    assert WRITER not in _sites(_UPDATE_LISTINGS)


def test_every_update_listings_outside_the_owners_is_ledgered() -> None:
    found = {p: n for p, n in _sites(_UPDATE_LISTINGS).items() if p not in EXEMPT}
    wrong = {
        p: (n, LEDGER[p].count if p in LEDGER else 0)
        for p in sorted(set(found) | set(LEDGER))
        if found.get(p, 0) != (LEDGER[p].count if p in LEDGER else 0)
    }
    assert not wrong, (
        f"UPDATE listings sites (found, ledgered): {wrong} — add a LEDGER line naming its "
        "class; a fetched payload goes through listing_write.write_listings")


@pytest.mark.parametrize("path", sorted(LEDGER))
def test_every_ledger_line_is_well_formed(path: str) -> None:
    site = LEDGER[path]
    assert site.cls in CLASSES and site.reason
    assert (site.dirty in {"same-statement", "separate-statement", "n/a"}
            or site.dirty.startswith(("sweep:", "gap:")))


@pytest.mark.parametrize(
    "path", sorted(p for p, s in LEDGER.items() if s.dirty == "same-statement"))
def test_a_same_statement_site_carries_its_dirty_enqueue(path: str) -> None:
    assert "dirty_properties" in FILES[path]


@pytest.mark.parametrize(
    "path", sorted(p for p, s in LEDGER.items() if s.cls in {"derived", "heal"}))
def test_a_derived_write_never_replaces_the_payload_or_claims_a_sighting(path: str) -> None:
    """A payload replacement is the writer's (key heals use jsonb_set or ||), and
    last_seen_at is rule 4's: sightings and successful fetches only."""
    text = FILES[path]
    assert not _WHOLESALE_RAW.search(text)
    assert not _LAST_SEEN.search(text)
