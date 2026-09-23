"""The post-publication text-extraction lane: prose-only facts, AFTER the row is live.

One lane, one selector, one write. It exists because the portals that state a fact in a
table are already read at ingest (`scraper/attribute_contract.py`, producer `structured`)
and the one portal that states nothing but prose is not. Nothing here ever sits between a
sighting and publication: the lane reads rows that are already in `listings`, already in
`properties` and already in Browse.

What the deleted lane got wrong, and what this one does instead:

  * It keyed on `listings.sreality_id`, which listing-identity Gate 2 made NULL for every
    new non-sreality row — so it selected 223 of 50,205 bazos rows and read green for two
    months. Everything here keys on `listings.id`, and the ONE predicate that decides
    eligibility (`_eligible_where`) is also what `scripts/verify_pipeline`'s lag check
    counts, so "selects nothing while green" has no room to happen (R8).
  * Its cache was keyed `(sreality_id, snapshot_id, model)`, so a price-only snapshot
    re-billed the same text (~$65 of ~$207) and a missed extraction was permanent. The key
    is now `(listing_id, text_hash, extractor_version)` — the text, not the snapshot (R6).
    `extractor_version` carries the MODEL and the OPEN-GATE SET, because those are what a
    cached answer is an answer from: migration 249 learned the model half, and the gate
    half is what stops a cached answer for a narrower field set retiring a listing that a
    newly-opened gate now has a column for.
  * A failed call left no trace, so the same listing was re-selected and re-billed on the
    next pass and every pass after it. A failure now writes the cache row too, with an
    attempt counter, and is given up on after GIVE_UP_AFTER — rule #5's shape, in the table
    that already exists rather than a second one.
  * It wrote with a bare `UPDATE listings`, appending no snapshot and marking no property.
    The write here is one statement whose UPDATE and `dirty_properties` enqueue are the
    same CTE (rule 20), sets every column through `coalesce(l.c, …)` so it can only ever
    fill a NULL (R3), and touches neither `listing_snapshots` nor `last_seen_at`. It does
    NOT call `sync_browse_list`: W6 wired the maintenance lane to patch Browse from the
    dirty set, and a second caller would be a second cadence.
  * Its enums lived in the tool's prose, so 'novostavba' (a condition) reached
    `building_type`. They are real JSON `enum` arrays generated from `scraper.vocabulary`.
  * It wrote `false` from silence — 92.9 % precision on `has_lift`, against a filter
    predicate that needs better. A `false` needs an explicit negation inside the evidence
    quote, and every value needs a quote that is verbatim in the description (R7).
  * It converted floors itself, at ~73 % and across two conventions. The model returns the
    advert's OWN words ('3. patro', '1. NP', 'přízemí') and `scraper.floor` converts them.

Nothing is EXTRACTED until R7's gate passes. `attribute_contract.Cell.gate` carries the
per-field verdict with the precision that granted it, and an open gate is the whole of this
lane's scope — today that set is EMPTY, so the lane costs one dict comprehension per
interval and does not even open a cursor. The earlier draft of this wave extracted every
gated cell and wrote only the passed ones, on the theory that the cache would be scored
later; it would not have been (the bake-off builds its own panel and makes its own calls,
and bazos — the only portal in scope — has no structured sibling to grade it against), and
worse, a cached row retires its listing from the selector, so the ~$113 it would have spent
on day one could never have become a column value. Extract what may be written, and the
first money the lane spends is money that lands somewhere.

Opening a gate therefore re-opens the corpus (the gate set is inside `extractor_version`):
open every field the bake-off cleared in ONE edit, or pay for the same descriptions twice.

The lane has no flag, no `app_settings` key of its own and no env var; the one switch is
`app_settings.enrichment_model`, which names the model the bake-off chose.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections import Counter
from threading import Lock
from typing import Any, Iterable, Mapping, Sequence

from scraper import area as area_grammar
from scraper import attribute_contract as contract
from scraper.db import _AREA_BASIS_FOLLOWS
from scraper import floor as floor_grammar
from scraper import vocabulary

LOG = logging.getLogger("description_extraction")

# Kept, not minted: `llm_calls_called_for_check` already permits it, the 60-day per-lane
# cost series and the two frontend cost pages keep reading one name, and a new one would be
# a migration plus a pure addition.
CALLED_FOR = "enrich_listing_description"
MODEL_SETTING = "enrichment_model"

# Bumped when the tool schema or the merge rules change in a way that makes a cached answer
# no longer the answer this code would accept.
SCHEMA_VERSION = "1"

# The lane's service level, and the one number two readers share: `verify_pipeline` reports
# p99 first_seen_at -> extraction against it, and `api/notifications`'s `:new:` re-scan
# window is floored by it, so a filter matching on a late-filled attribute still gets its
# second look. A late fill can only cause a MISSED alert (the `:new:` dedupe key is
# once-ever per property), never a duplicate.
SLO_MINUTES = 20

# Pass sizing, not a knob: 250 rows at the measured p50 of 11.7 s over 8 workers is ~6 min,
# under LANE_PASS_TIMEOUT_SECONDS (1800) and the 1200 s stall warn. A pass abandoned at the
# timeout keeps its threads — and their billing — alive, so the slice is what bounds the
# lane, and the in-process pass lock in the worker is what stops an abandoned pass being
# overlapped by the next one.
#
# ONE arm, newest-first. An earlier draft added an oldest-first backlog arm on an hourly
# sub-cadence, on the theory that the deleted lane's 50k backlog was unreachable because it
# ordered DESC. It was not: the anti-join means a row the lane has read is no longer
# eligible, so DESC walks BACKWARDS through the backlog at 250 rows per pass — ~57k a day
# against a measured bazos inflow of 1,940 a day, i.e. the 50,208-row backlog clears in
# about a day. The deleted lane's backlog was unreachable because it keyed on `sreality_id`
# and selected 223 rows, which no ORDER BY could have fixed. The ASC arm was a measured
# 9.4-10.0 s bitmap heap scan buying nothing, so it is gone.
PASS_SLICE = 250
WORKERS = 8
MAX_TOKENS = 4096
PASS_MAX_SECONDS = 900
# How many times one listing's text may be attempted before the lane gives up on it (rule
# #5's `given_up`, at the same 5). Without it a listing whose call fails permanently — a
# refusal, a description past the context window, a provider that stops honouring the
# forced tool call — is re-selected and re-billed on every pass, for ever.
GIVE_UP_AFTER = 5
# The ONE binding guard, expressed the way every other lane expresses backpressure: the
# slice times the measured unit cost ($0.00225/call, n=15,382), with headroom for a pricier
# winner. `vision_batch` checks it in the worker, under the lock, BEFORE each call.
MAX_USD_PER_PASS = round(PASS_SLICE * 0.005, 2)

_MAX_QUOTE_CHARS = 300


def extractor_version(model: str) -> str:
    """The cache's third key column: schema, OPEN-GATE SET, model.

    All three are things a cached answer is an answer FROM, and all three must invalidate
    it. The model half is migration 249's lesson. The gate half is this wave's: the lane
    asks only for the fields whose gate is open, so a row cached while three gates were
    open is not an answer for the fourth — and without the fingerprint the selector's
    anti-join would retire that listing for ever and the newly-opened column would stay
    NULL on the entire existing corpus."""
    scope = contract.extracted_cells()
    blob = ";".join(f"{p}:{','.join(f)}" for p, f in sorted(scope.items()))
    gates = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8] if blob else "none"
    return f"{SCHEMA_VERSION}:{gates}:{model}"


def text_hash(description: str | None) -> str:
    """The cache's second key column, and what the selector compares against. Hashed here
    and in SQL (`encode(sha256(convert_to(l.description,'UTF8')),'hex')`) — the same bytes
    either side, so a row extracted by the lane is a row the selector stops returning."""
    return hashlib.sha256((description or "").encode("utf-8")).hexdigest()


# --- the extraction contract ------------------------------------------------

# Field semantics, one row per extractable column. Every enum is GENERATED from
# `scraper.vocabulary.CANON` — the whole value space since W5 — because this lane writes
# the same columns the nine parsers write, and a value outside it is a defect, not a spelling.
# `floor` is deliberately a STRING: the advert's own words, converted by `scraper.floor`.
# The model doing that arithmetic is what produced a ~73 %-correct, two-convention column.
_FIELD_SPEC: dict[str, tuple[list[str], str, str | None]] = {
    "floor": (["string", "null"],
              "The storey this unit is on, quoted in the advert's OWN Czech words and "
              "nothing else: '3. patro', '3. NP', 'přízemí', 'suterén', '1. podzemní "
              "podlaží'. Never a number on its own, never converted, never the building's "
              "storey count.", None),
    "total_floors": (["integer", "null"],
                     "How many storeys the BUILDING has, if the advert states it.", None),
    "has_balcony": (["boolean", "null"],
                    "A balcony or a loggia belonging to this unit. A terrace is NOT one.",
                    None),
    "has_lift": (["boolean", "null"], "A lift (výtah) in the building.", None),
    "has_parking": (["boolean", "null"],
                    "A parking space, garage or stání BELONGING to the property. Street "
                    "parking and 'parkování v okolí' are not.", None),
    "building_type": (["string", "null"], "What the building is built of.",
                      "building_type"),
    "condition": (["string", "null"], "The stated condition of the building or unit.",
                  "condition"),
    "energy_rating": (["string", "null"],
                      "The PENB energy class the advert states.", "energy_rating"),
    # A NUMBER, not words: the ingest grammar (`scraper.area`) reads "54 m²" first, so the
    # lane only ever sees adverts the grammar found nothing in, and the figure it returns
    # must itself appear in the quote — digits, or an ares/hectares figure that converts to
    # it — which is the whole of R7 for a quantity: the text states it or the value is null.
    "area_m2": (["number", "null"],
                "The advertised property's OWN area in square metres, as one number: the "
                "living / usable floor area of a flat or a house, the parcel area of land "
                "or a garden. Never the plot a house stands on, never a garage, cellar, "
                "balcony or room, never a sum of several figures. Quote the span that "
                "carries the figure and its unit.", None),
}

_SYSTEM_PROMPT = (
    "You read a Czech classified property advert and record ONLY what its text actually "
    "states. You never infer, never average and never guess from what is usual.\n"
    "Every field takes {value, evidence_quote}. `evidence_quote` must be a VERBATIM span "
    "copied out of the advert text — if you cannot copy one, the value is null.\n"
    "Return false ONLY when the text explicitly denies the fact ('bez výtahu', 'není "
    "balkon', 'bez parkování'). Silence is null, never false."
)


def extraction_tool(fields: Sequence[str]) -> dict[str, Any]:
    """The one tool schema, built from the contract's field list and the vocabulary."""
    properties: dict[str, Any] = {}
    for field in fields:
        value_type, description, enum_field = _FIELD_SPEC[field]
        value: dict[str, Any] = {"type": value_type, "description": description}
        if enum_field:
            value["enum"] = [*sorted(vocabulary.CANON[enum_field]), None]
        properties[field] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "value": value,
                "evidence_quote": {
                    "type": ["string", "null"],
                    "description": (
                        "The verbatim span of the advert text that states this, at most "
                        f"{_MAX_QUOTE_CHARS} characters. Null when value is null."
                    ),
                },
            },
            "required": ["value", "evidence_quote"],
        }
    return {
        "name": "record_description_facts",
        "description": (
            "Record the typed facts the advert's own text states. Call exactly once. "
            "Use null for anything the text does not state."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(fields),
        },
    }


# --- validating one answer --------------------------------------------------

# An explicit denial, as whole words. 'bez' has to be a word: 'bezbariérový' is not a denial
# of anything. Everything else is silence, and silence is null (R7).
_NEGATION_RE = re.compile(
    r"\b(bez|neni|nema|nemaji|nemame|zadn\w*|chybi|nenachazi|nedisponuje)\b"
)
_WS_RE = re.compile(r"\s+")
# A figure WITH AN AREA UNIT in a flattened quote. The number half IS `scraper.area`'s
# (`AREA_NUMBER_SRC` + `area_token_to_float`: the same lookbehind that keeps "3+1 174"
# from reading as 1 174, the same separator class, the same dotted thousands), so the
# lane and the ingest grammar can never disagree on what a figure is (rule 21). The unit
# half is a closed list: square metres at 1, ares at 100, hectares at 10 000 — the only
# conversions a Czech advert writes an area in, so the only ones a quantity check
# accepts. A figure with any other tail (a price, a distance, a ceiling height, a storey)
# is not an area statement and never validates; anything else is arithmetic, and the
# model is not allowed any.
_AREA_UNIT_RE = (
    r"(?P<m2>m2|m\^2|m 2|m\u00b2|metr\w*\s+ctvere\w*|m\s+ctvere\w*)"
    r"|(?P<ar>ar|ary|aru|arech)"
    r"|(?P<ha>ha|hektar\w*)"
)
_FIGURE_RE = re.compile(
    rf"(?<![\d+.,])(?P<number>{area_grammar.AREA_NUMBER_SRC})\s*"
    rf"(?:{_AREA_UNIT_RE})(?![a-z\d])"
)
_UNIT_FACTOR = {"m2": 1.0, "ar": 100.0, "ha": 10_000.0}


def _flat(text: str | None) -> str:
    """Lower-cased, diacritics stripped, whitespace collapsed.

    Not `vocabulary.fold`: that one exists to turn a portal LABEL into a registry key and
    rewrites separators to `_`, which would both destroy word boundaries for the negation
    test and make every quote check a comparison between two strings of underscores.
    A quote is tested against THIS form of the description, so a model that reflowed a line
    break has still copied the span and a model that invented one still fails.
    """
    stripped = "".join(
        c for c in unicodedata.normalize("NFD", text or "") if not unicodedata.combining(c)
    )
    return _WS_RE.sub(" ", stripped.lower()).strip()


def _quote_states_figure(quote: str, value: float) -> bool:
    """True when some AREA figure in the quote IS `value` in m²: the unit the advert wrote
    beside the number decides the one multiplier, and half a square metre of tolerance
    covers a rounded decimal and nothing else."""
    for match in _FIGURE_RE.finditer(_flat(quote)):
        figure = area_grammar.area_token_to_float(match.group("number"))
        unit = next(name for name in _UNIT_FACTOR if match.group(name) is not None)
        if abs(figure * _UNIT_FACTOR[unit] - value) <= 0.5:
            return True
    return False


def quote_supports(description: str, quote: str | None) -> bool:
    if not quote:
        return False
    if len(quote) > _MAX_QUOTE_CHARS:
        return False
    flat = _flat(quote)
    return bool(flat) and flat in _flat(description)


def merge_extraction(
    payload: Mapping[str, Any], *, description: str, fields: Iterable[str],
    category_main: str | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """The model's tool arguments -> the values this lane is willing to store.

    Returns `(values, dropped)`; `dropped` maps a field to WHY, because a rejected value is
    the vocabulary/precision signal and the deleted lane's habit of silently nulling one is
    how 1,396 off-canon `condition` values reached the column unnoticed.
    """
    values: dict[str, Any] = {}
    dropped: dict[str, str] = {}
    for field in fields:
        cell = payload.get(field)
        if not isinstance(cell, Mapping):
            continue
        raw = cell.get("value")
        if raw is None:
            continue
        quote = cell.get("evidence_quote")
        if not isinstance(quote, str) or not quote_supports(description, quote):
            dropped[field] = "quote_not_in_text"
            continue
        value, reason = _coerce(field, raw, quote)
        if reason:
            dropped[field] = reason
            continue
        values[field] = value
    total = values.get("total_floors")
    if "floor" in values and not floor_grammar.is_plausible_floor(values["floor"], total):
        del values["floor"]
        dropped["floor"] = "implausible_floor"
    if "area_m2" in values:
        # The grammar's own category bounds (5 m² for a flat / house / commercial unit,
        # none for land), applied by the one function that stamps `area_basis` at ingest.
        # No category, no bound and no basis to stamp: the value is refused, not guessed.
        if category_main is None:
            del values["area_m2"]
            dropped["area_m2"] = "area_without_category"
        else:
            area, _basis = area_grammar.derive_headline_area(
                category_main=category_main, fallback=values["area_m2"])
            if area is None:
                del values["area_m2"]
                dropped["area_m2"] = "area_out_of_range"
            else:
                values["area_m2"] = area
    return values, dropped


def _coerce(field: str, raw: Any, quote: str) -> tuple[Any, str | None]:
    value_type, _, enum_field = _FIELD_SPEC[field]
    if field == "floor":
        if not isinstance(raw, str):
            return None, "floor_not_words"
        converted = floor_grammar.normalize_floor(raw)
        # A bare integer has no convention to read, which is exactly the trap this lane is
        # not allowed to fall into: `normalize_floor` returns None and the field is dropped.
        return (None, "floor_words_unreadable") if converted is None else (converted, None)
    if "number" in value_type:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None, "not_number"
        value = float(raw)
        # Statement before size: "the text does not say this" is the refusal that means
        # something; a quoted figure the column cannot hold is the rarer, second one.
        if not _quote_states_figure(quote, value):
            return None, "figure_not_in_quote"
        if not 0 < value < area_grammar.MAX_AREA_M2:
            return None, "area_out_of_range"
        return value, None
    if "boolean" in value_type:
        if not isinstance(raw, bool):
            return None, "not_boolean"
        if raw is False and not _NEGATION_RE.search(_flat(quote)):
            return None, "false_without_negation"
        return raw, None
    if "integer" in value_type:
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None, "not_integer"
        return raw, None
    if not isinstance(raw, str):
        return None, "not_string"
    if enum_field and raw not in vocabulary.CANON[enum_field]:
        # The JSON enum should have made this impossible; providers vary, so the value is
        # refused and counted rather than trusted (`vocabulary.refuse` feeds gate A3's
        # counter, which is the instrument that would show a canon gap).
        vocabulary.refuse(enum_field, "text_lane", raw)
        return None, "off_vocabulary"
    return raw, None


# --- the one eligibility predicate (R8) -------------------------------------

_HASH_EXPR = "encode(sha256(convert_to(l.description, 'UTF8')), 'hex')"


def _eligible_where(cells: Mapping[str, Sequence[str]]) -> str:
    """The predicate BOTH the lane's selector and the health check's count are built from.

    `IS NOT DISTINCT FROM` on the hash is deliberate and load-bearing: it keeps `text_hash`
    out of the index condition on the unique key, so the anti-join probes
    `(listing_id, extractor_version)` and the description is only hashed for a listing that
    ALREADY has an extraction at this version. Written as a plain `=` the planner turns the
    hash into an index condition and detoasts + hashes all ~50k descriptions every pass,
    forever, including the steady state where nothing is eligible.
    """
    if not cells:
        return "false"
    arms = [
        "(l.source = '{p}' AND ({nulls}))".format(
            p=portal, nulls=" OR ".join(f"l.{f} IS NULL" for f in fields))
        for portal, fields in sorted(cells.items())
    ]
    return (
        "l.is_active\n"
        "   AND l.description IS NOT NULL AND l.description <> ''\n"
        "   AND (" + "\n        OR ".join(arms) + ")\n"
        "   AND NOT EXISTS (\n"
        "         SELECT 1 FROM listing_description_enrichments e\n"
        "          WHERE e.listing_id = l.id\n"
        "            AND e.extractor_version = %(version)s\n"
        f"            AND e.text_hash IS NOT DISTINCT FROM {_HASH_EXPR}\n"
        # A row whose extraction FAILED is still eligible, up to GIVE_UP_AFTER attempts:
        # a blip must not lose a listing for ever, and a permanent refusal must not be
        # re-billed for ever. The count lives in the cache row it already writes.
        "            AND (e.extracted -> 'error' IS NULL\n"
        "                 OR coalesce((e.extracted ->> 'attempts')::int, 1)\n"
        f"                    >= {GIVE_UP_AFTER}))"
    )


# ONE arm: newest-first, every pass. The eligible rows ARE the newest, so the query stops
# at its LIMIT in ~0.4 s whether or not a backlog exists, and because an extracted row
# leaves the predicate the same arm walks backwards through the backlog at PASS_SLICE a
# pass. An oldest-first companion was measured and removed: once nothing is eligible it
# cannot stop early and becomes a 9.4-10.0 s bitmap heap scan over 55,252 blocks, run to
# find nothing.
_SELECT_SQL_TEMPLATE = """
SELECT l.id, l.source, l.first_seen_at, l.description, l.category_main
  FROM listings l
 WHERE {where}
 ORDER BY l.first_seen_at {direction}
 LIMIT %(limit)s
"""

# Per source: how many rows are waiting and how long the oldest has waited. A literal twin
# of `check_acquisition_lag` — the check whose own docstring argues this exact case: a
# "nothing happened in N hours" alarm needs a baseline the outage itself erodes, while the
# oldest un-extracted row grows without bound and cannot be normalised away.
_LAG_SQL_TEMPLATE = """
SELECT l.source,
       count(*) AS waiting,
       max(extract(epoch from (now() - l.first_seen_at)) / 3600.0) AS oldest_hours,
       percentile_cont(0.5) within group (
         order by extract(epoch from (now() - l.first_seen_at)) / 3600.0
       ) AS median_hours
  FROM listings l
 WHERE {where}
 GROUP BY l.source
"""

# p99 of first_seen_at -> extracted, over the window the SLO is stated for. Reported, never
# a threshold of its own: the lag check above is what rings.
EXTRACTION_LATENCY_SQL = """
SELECT count(*) AS n,
       percentile_cont(0.99) within group (
         order by extract(epoch from (e.created_at - l.first_seen_at)) / 60.0
       ) AS p99_minutes
  FROM listing_description_enrichments e
  JOIN listings l ON l.id = e.listing_id
 WHERE e.created_at > now() - interval '24 hours'
"""

_OPEN = contract.extracted_cells()
SELECT_INFLOW_SQL = _SELECT_SQL_TEMPLATE.format(
    where=_eligible_where(_OPEN), direction="DESC")
ELIGIBLE_LAG_SQL = _LAG_SQL_TEMPLATE.format(where=_eligible_where(_OPEN))
# The same predicate over every gate the contract DECLARES, open or not. With all gates
# closed the two live constants above collapse to `WHERE false`, which PREPAREs and proves
# nothing; this one keeps `tests/sql_corpus` type-checking the column arms a gate flip
# switches on. One function, three spellings of its output — never a second predicate.
_DECLARED_SELECT_SQL = _SELECT_SQL_TEMPLATE.format(
    where=_eligible_where(contract.gated_cells()), direction="DESC")

# The conflict target is the key migration 552 adds. DO UPDATE, not DO NOTHING, and only
# over a row that is itself a recorded FAILURE: a retry either replaces the error with the
# real extraction or bumps its attempt count towards GIVE_UP_AFTER, and a row that already
# holds an answer is never rewritten. `cost_usd` accumulates because every attempt was
# billed, and the lane's cost series must say so.
_CACHE_INSERT_SQL = """
INSERT INTO listing_description_enrichments AS e
    (listing_id, text_hash, extractor_version, extracted, filled,
     model, llm_call_id, cost_usd)
VALUES (%(listing_id)s, %(text_hash)s, %(extractor_version)s,
        %(extracted)s, %(filled)s, %(model)s, %(llm_call_id)s, %(cost_usd)s)
ON CONFLICT (listing_id, extractor_version, text_hash) DO UPDATE
   SET extracted = excluded.extracted
                || CASE WHEN excluded.extracted -> 'error' IS NULL THEN '{}'::jsonb
                        ELSE jsonb_build_object(
                          'attempts',
                          coalesce((e.extracted ->> 'attempts')::int, 0) + 1) END,
       filled = excluded.filled,
       model = excluded.model,
       llm_call_id = coalesce(excluded.llm_call_id, e.llm_call_id),
       cost_usd = coalesce(e.cost_usd, 0) + coalesce(excluded.cost_usd, 0)
 WHERE e.extracted -> 'error' IS NOT NULL
"""

# ONE statement: the column fill and the property enqueue in a single CTE, so the dirty mark
# cannot outlive a rolled-back write (rule 20). `coalesce` is the NULL-only rule itself — a
# value another writer put there while this call was in flight wins, with no compare-and-set
# needed — and the WHERE keeps the statement off rows it would not change, so a pass that
# fills nothing marks nothing. `listing_snapshots` and `last_seen_at` appear nowhere in it.
_WRITE_SQL_TEMPLATE = """
WITH updated AS (
    UPDATE listings AS l
       SET {sets}
     WHERE l.id = %(id)s
       AND ({changed})
    RETURNING l.property_id
)
INSERT INTO dirty_properties (property_id)
SELECT DISTINCT property_id FROM updated WHERE property_id IS NOT NULL
ON CONFLICT (property_id) DO UPDATE SET marked_at = now()
"""


def write_sql(columns: Sequence[str], companions: Mapping[str, str] | None = None) -> str:
    """`companions` maps a DERIVED column onto the extracted column it follows
    (`area_basis` -> `area_m2`, `scraper.db._AREA_BASIS_FOLLOWS`): it is set only in the
    same UPDATE that fills its leader, never on its own, and it is not a reason to write."""
    from scraper.db import _LISTING_COLUMN_PGTYPE

    companions = dict(companions or {})
    casts = {c: _LISTING_COLUMN_PGTYPE[c] for c in (*columns, *companions)}
    sets = [f"{c} = coalesce(l.{c}, %({c})s::{casts[c]})" for c in columns]
    sets += [f"{c} = CASE WHEN l.{leader} IS NULL THEN %({c})s::{casts[c]} ELSE l.{c} END"
             for c, leader in companions.items()]
    return _WRITE_SQL_TEMPLATE.format(
        sets=",\n           ".join(sets),
        changed=" OR ".join(
            f"(l.{c} IS NULL AND %({c})s::{casts[c]} IS NOT NULL)" for c in columns),
    )


# The writer over every declared cell plus its one companion, spelled as a constant so
# CI's schema replay PREPAREs the casts and the CASE arm (`tests/sql_corpus` reads
# module-level `*_SQL` names; a statement only ever built inside a function is invisible
# to it). Never executed as such: the lane writes the subset a row actually filled.
_DECLARED_WRITE_SQL = write_sql(
    sorted({f for fields in contract.gated_cells().values() for f in fields}),
    {follower: leader for leader, follower in [_AREA_BASIS_FOLLOWS]},
)


# --- the pass ---------------------------------------------------------------

def resolve_model(conn: Any) -> str | None:
    """`app_settings.enrichment_model`, or None. No fallback ON PURPOSE: `LLMClient`'s own
    default is a claude id, this project does not use Anthropic models, and a lane that
    silently picks a model nobody chose is worse than a lane that says it has none."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (MODEL_SETTING,))
        row = cur.fetchone()
    value = row[0] if row else None
    return value if isinstance(value, str) and value.strip() else None


def select_eligible(conn: Any, *, version: str,
                    slice_size: int = PASS_SLICE) -> list[dict[str, Any]]:
    """The newest eligible listings. An extracted row leaves the predicate, so this walks
    backwards through a backlog at `slice_size` a pass without a second arm."""
    with conn.cursor() as cur:
        cur.execute(SELECT_INFLOW_SQL, {"version": version, "limit": max(0, slice_size)})
        rows = cur.fetchall()
    return [{"id": listing_id, "source": source, "first_seen_at": first_seen_at,
             "description": description, "category_main": category_main}
            for listing_id, source, first_seen_at, description, category_main in rows]


def _tool_arguments(response: Any) -> Mapping[str, Any] | None:
    for call in getattr(response, "tool_calls", None) or []:
        payload = call.get("input") if isinstance(call, Mapping) else None
        if isinstance(payload, Mapping):
            return payload
    return None


def record_extraction(
    conn: Any, row: Mapping[str, Any], *, values: Mapping[str, Any],
    extracted: Mapping[str, Any], model: str, version: str,
    llm_call_id: int | None, cost_usd: float,
) -> list[str]:
    """Write the columns the lane may write, then the cache row, in ONE transaction.

    Both or neither: a cache row without its fill would retire the listing from selection
    holding a value it never wrote, which is the deleted lane's permanence bug in a new
    key."""
    writable = [c for c in contract.extracted_cells().get(str(row["source"]), ())
                if c in values]
    filled = {c: values[c] for c in writable}
    companions: dict[str, str] = {}
    if "area_m2" in filled:
        # The same stamp ingest gives a grammar-read area (`plot` on land, `unknown`
        # elsewhere), so a lane-filled row is indistinguishable downstream (rule 20's
        # ppm2 basis included). In `filled` too, and the rollback script blanks both
        # (`clear_unmeasured_enrichment_fills.FOLLOWERS`) — never the stamp on its own.
        leader, follower = _AREA_BASIS_FOLLOWS
        _area, basis = area_grammar.derive_headline_area(
            category_main=row.get("category_main"), fallback=filled[leader])
        filled[follower] = basis
        companions[follower] = leader
    with conn.transaction():
        if writable:
            params: dict[str, Any] = {"id": row["id"]}
            params.update(filled)
            with conn.cursor() as cur:
                cur.execute(write_sql(writable, companions), params)
        _cache_row(conn, row, extracted=extracted, filled=filled, model=model,
                   version=version, llm_call_id=llm_call_id, cost_usd=cost_usd)
    return writable


def record_failure(conn: Any, row: Mapping[str, Any], *, error: str, model: str,
                   version: str, cost_usd: float) -> None:
    """A failed attempt is a cache row too (rule #5's shape).

    Without it the listing is re-selected and re-billed on every pass for ever, which is
    what the forced tool call only narrows: a refusal, a description past the context
    window and a provider that stops honouring `tool_choice` are all still permanent, and
    all still billed. The attempt count lives in the payload and GIVE_UP_AFTER retires the
    row; the selector reads both."""
    _cache_row(conn, row, extracted={"error": error[:500], "attempts": 1}, filled={},
               model=model, version=version, llm_call_id=None, cost_usd=cost_usd)


def _cache_row(conn: Any, row: Mapping[str, Any], *, extracted: Mapping[str, Any],
               filled: Mapping[str, Any], model: str, version: str,
               llm_call_id: int | None, cost_usd: float) -> None:
    with conn.cursor() as cur:
        cur.execute(_CACHE_INSERT_SQL, {
            "listing_id": row["id"],
            "text_hash": text_hash(row["description"]),
            "extractor_version": version,
            "extracted": json.dumps(extracted, ensure_ascii=False),
            "filled": json.dumps(filled, ensure_ascii=False),
            "model": model,
            "llm_call_id": llm_call_id,
            "cost_usd": cost_usd,
        })


def _providers() -> dict[str, Any]:
    """The per-worker provider registry. OpenAI AND the self-hosted pod, because
    `app_settings.enrichment_model` is the one switch and the bake-off may crown an `oss:`
    id — a registry that switch cannot reach turns every row of every pass into a
    ProviderError raised before a single call is made."""
    from api.providers.openai import OpenAIProvider
    from api.providers.oss import OssProvider

    return {"openai": OpenAIProvider(), "oss": OssProvider()}


def run_pass(conn: Any, *, slice_size: int = PASS_SLICE,
             workers: int = WORKERS) -> dict[str, Any]:
    """One bounded pass. Returns the dict the worker publishes in its heartbeat."""
    from toolkit import vision_batch

    scope = contract.extracted_cells()
    if not scope:
        # No gate is open, so there is nothing this lane is allowed to write and therefore
        # nothing it is allowed to pay for. Not even a query.
        return {"claimed": 0, "reason": "no_open_gate"}
    model = resolve_model(conn)
    if model is None:
        LOG.warning("text-extract lane idle: app_settings.%s is not set", MODEL_SETTING)
        return {"claimed": 0, "reason": "no_model"}
    version = extractor_version(model)
    rows = select_eligible(conn, version=version, slice_size=slice_size)
    if not rows:
        return {"claimed": 0, "extracted": 0, "written": 0, "failed": 0,
                "spent_usd": 0.0, "errors": 0, "model": model}

    written: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    stored: list[int] = []
    tally = Lock()

    def _call(llm: Any, row: Mapping[str, Any]) -> tuple[float, Any]:
        fields = scope[str(row["source"])]
        tool = extraction_tool(fields)
        res = llm.call(
            called_for=CALLED_FOR, model=model, max_tokens=MAX_TOKENS,
            system=_SYSTEM_PROMPT, tools=[tool],
            # FORCED, because a model that answers in prose produces no tool call. The
            # cost comes back either way: the completion is already billed, and raising
            # here would hide that spend from the pre-call guard as well.
            tool_choice=tool["name"],
            messages=[{"role": "user", "content": str(row["description"])}],
        )
        return (float(getattr(res, "cost_usd", 0.0) or 0.0),
                (res, _tool_arguments(res)))

    def _record(wconn: Any, row: Mapping[str, Any], result: Any,
                error: str | None) -> None:
        res, payload, cost = None, None, 0.0
        if result is not None:
            res, payload = result
            cost = float(getattr(res, "cost_usd", 0.0) or 0.0)
            if payload is None:
                error = "the model returned no record_description_facts call"
        if error is not None:
            LOG.warning("TEXT-EXTRACT listing=%s failed: %s", row["id"], error[:200])
            # A fatal error is the PROVIDER's state, not this listing's: the pass is
            # already aborting, and counting an attempt against every row in flight would
            # retire listings for an outage they had no part in.
            if vision_batch.is_fatal(error):
                return
            try:
                record_failure(wconn, row, error=error, model=model, version=version,
                               cost_usd=cost)
            except Exception as exc:  # noqa: BLE001 - see the write path below
                LOG.warning("TEXT-EXTRACT listing=%s failure not stored: %s",
                            row["id"], str(exc)[:200])
            with tally:
                failures[error.split(":")[0][:60]] += 1
            return
        fields = scope[str(row["source"])]
        values, why = merge_extraction(
            payload, description=str(row["description"]), fields=fields,
            category_main=row.get("category_main"))
        try:
            columns = record_extraction(
                wconn, row, values=values, extracted=dict(payload), model=model,
                version=version, llm_call_id=getattr(res, "llm_call_id", None),
                cost_usd=cost,
            )
        except Exception as exc:  # noqa: BLE001 - the engine does not wrap `record`, and a
            # write failure that escapes here ends this WORKER THREAD, silently, mid-pass.
            LOG.warning("TEXT-EXTRACT listing=%s not stored: %s", row["id"], str(exc)[:200])
            return
        # Counters are touched by eight threads; `c[k] += 1` is a read-modify-write.
        with tally:
            stored.append(row["id"])
            dropped.update(f"{field}:{reason}" for field, reason in why.items())
            written.update(columns)

    stats = vision_batch.run_batch(
        rows=list(rows), call=_call, record=_record, providers=_providers,
        max_usd=MAX_USD_PER_PASS, max_seconds=PASS_MAX_SECONDS, workers=workers,
    )
    return {
        "claimed": len(rows),
        "extracted": len(stored),
        "errors": stats["errors"],
        "failed": sum(failures.values()),
        "failures": dict(failures),
        "written": sum(written.values()),
        "by_column": dict(written),
        "dropped": dict(dropped),
        "spent_usd": round(float(stats["spent"]), 4),
        "aborted": bool(stats["aborted"]),
        "fatal": stats["fatal"],
        "model": model,
        # The backlog signal, free: a full slice means more is waiting.
        "slice_full": len(rows) >= slice_size,
    }
