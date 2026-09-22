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
    `extractor_version` carries the MODEL, because the model is what a cached answer is an
    answer from: migration 249 learned that with `model` in the old key and the lesson does
    not change when the rest of the key does.
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

Nothing is written until R7's gate passes. `attribute_contract.Cell.gate` carries the
per-field verdict with the precision that granted it; until a gate flips, the lane extracts
and CACHES, which is what lets the bake-off panel be scored on the portal that has no
structured sibling. The lane has no flag, no `app_settings` key of its own and no env var:
the contract's gated `text` cells are its entire scope, and zero of them means one indexed
query per interval and nothing else.
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

from scraper import attribute_contract as contract
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

# Pass sizing, not a knob: 1,000 rows at the measured p50 of 11.7 s over 8 workers is ~24
# min, comfortably under LANE_PASS_TIMEOUT_SECONDS (1800) and the 1200 s stall warn. A pass
# abandoned at the timeout keeps its threads — and their billing — alive, so the slice is
# what bounds the lane, and the in-process pass lock in the worker is what stops an
# abandoned pass being overlapped by the next one.
PASS_SLICE = 1000
# Of that slice, the newest-first share. The deleted lane ordered `first_seen_at DESC` and
# nothing else, which is why its 50k backlog was structurally unreachable: inflow always
# filled the slice. The oldest-first remainder is what drains a backlog.
INFLOW_SLICE = 250
# How often a pass ALSO walks oldest-first. Hourly at the 300 s cadence: the backlog arm
# costs ~10 s of DB once nothing is eligible (measured), and 750 rows an hour still drains
# the 50k bazos backlog in under three days, which is the estimate the wave was sized on.
BACKLOG_EVERY_PASSES = 12
WORKERS = 8
MAX_TOKENS = 4096
PASS_MAX_SECONDS = 900
# The ONE binding guard, expressed the way every other lane expresses backpressure: the
# slice times the measured unit cost ($0.00225/call, n=15,382), with headroom for a pricier
# winner. `vision_batch` checks it in the worker, under the lock, BEFORE each call.
MAX_USD_PER_PASS = round(PASS_SLICE * 0.005, 2)

_MAX_QUOTE_CHARS = 300


def extractor_version(model: str) -> str:
    """The cache's third key column. The model is IN it: a cached answer is an answer from
    a particular model, and a model swap must re-attempt rather than serve the old one."""
    return f"{SCHEMA_VERSION}:{model}"


def text_hash(description: str | None) -> str:
    """The cache's second key column, and what the selector compares against. Hashed here
    and in SQL (`encode(sha256(convert_to(l.description,'UTF8')),'hex')`) — the same bytes
    either side, so a row extracted by the lane is a row the selector stops returning."""
    return hashlib.sha256((description or "").encode("utf-8")).hexdigest()


# --- the extraction contract ------------------------------------------------

# Field semantics, one row per extractable column. Every enum is GENERATED from
# `scraper.vocabulary.known_values` — the canon PLUS today's legacy spellings, because this
# lane writes the same columns the nine parsers write and W5 has not collapsed them yet.
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
            value["enum"] = [*sorted(vocabulary.known_values(enum_field)), None]
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


def quote_supports(description: str, quote: str | None) -> bool:
    if not quote:
        return False
    if len(quote) > _MAX_QUOTE_CHARS:
        return False
    flat = _flat(quote)
    return bool(flat) and flat in _flat(description)


def merge_extraction(
    payload: Mapping[str, Any], *, description: str, fields: Iterable[str],
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
    if enum_field and raw not in vocabulary.known_values(enum_field):
        # The JSON enum should have made this impossible; providers vary, so the value is
        # refused and counted rather than trusted (`vocabulary.refuse` feeds gate A3's
        # counter, which is the instrument that would show a canon gap).
        vocabulary.refuse(enum_field, "text_lane", raw)
        return None, "off_vocabulary"
    return raw, None


# --- the one eligibility predicate (R8) -------------------------------------

_HASH_EXPR = "encode(sha256(convert_to(l.description, 'UTF8')), 'hex')"


def _eligible_where() -> str:
    """The predicate BOTH the lane's selector and the health check's count are built from.

    `IS NOT DISTINCT FROM` on the hash is deliberate and load-bearing: it keeps `text_hash`
    out of the index condition on the unique key, so the anti-join probes
    `(listing_id, extractor_version)` and the description is only hashed for a listing that
    ALREADY has an extraction at this version. Written as a plain `=` the planner turns the
    hash into an index condition and detoasts + hashes all ~50k descriptions every pass,
    forever, including the steady state where nothing is eligible.
    """
    cells = contract.extracted_cells()
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
        f"            AND e.text_hash IS NOT DISTINCT FROM {_HASH_EXPR})"
    )


# TWO arms, ONE predicate, and they run on different cadences because they cost different
# amounts. Measured on the live table 2026-09-22 with 50,374 eligible bazos rows:
#   * NEWEST-first stops at its LIMIT as soon as it has a slice, and the eligible rows ARE
#     the newest ones, so it is ~0.4 s whether or not a backlog exists. Every pass.
#   * OLDEST-first is cheap only WHILE a backlog exists. Once nothing is eligible it cannot
#     stop early and walks the whole source: the same predicate as an aggregate over all
#     50,374 rows measured 9.4-10.0 s (a 55,252-block bitmap heap scan), warm and cold
#     alike. Paying that every 5 minutes for ever, to find nothing, is not a backlog drain
#     — it is a standing query. It runs on BACKLOG_EVERY_PASSES instead.
# The deleted lane had the newest arm and nothing else, which is why its 50k backlog was
# structurally unreachable; one arm is the bug, and two arms at one cadence is a tax.
_SELECT_SQL_TEMPLATE = """
SELECT l.id, l.source, l.first_seen_at, l.description
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

SELECT_INFLOW_SQL = _SELECT_SQL_TEMPLATE.format(where=_eligible_where(), direction="DESC")
SELECT_BACKLOG_SQL = _SELECT_SQL_TEMPLATE.format(where=_eligible_where(), direction="ASC")
ELIGIBLE_LAG_SQL = _LAG_SQL_TEMPLATE.format(where=_eligible_where())

_CACHE_INSERT_SQL = """
INSERT INTO listing_description_enrichments
    (listing_id, text_hash, extractor_version, extracted, filled,
     model, llm_call_id, cost_usd)
VALUES (%(listing_id)s, %(text_hash)s, %(extractor_version)s,
        %(extracted)s, %(filled)s, %(model)s, %(llm_call_id)s, %(cost_usd)s)
ON CONFLICT (listing_id, text_hash, extractor_version) DO NOTHING
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


def write_sql(columns: Sequence[str]) -> str:
    from scraper.db import _LISTING_COLUMN_PGTYPE

    casts = {c: _LISTING_COLUMN_PGTYPE[c] for c in columns}
    return _WRITE_SQL_TEMPLATE.format(
        sets=",\n           ".join(
            f"{c} = coalesce(l.{c}, %({c})s::{casts[c]})" for c in columns),
        changed=" OR ".join(
            f"(l.{c} IS NULL AND %({c})s::{casts[c]} IS NOT NULL)" for c in columns),
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


def select_eligible(conn: Any, *, version: str, slice_size: int = PASS_SLICE,
                    inflow: int = INFLOW_SLICE,
                    backlog: bool = False) -> list[dict[str, Any]]:
    """The newest eligible listings, plus — only on a backlog pass — the oldest."""
    plan = [(SELECT_INFLOW_SQL, max(0, inflow))]
    if backlog:
        plan.append((SELECT_BACKLOG_SQL, max(0, slice_size - inflow)))
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for sql, limit in plan:
        if limit <= 0:
            continue
        with conn.cursor() as cur:
            cur.execute(sql, {"version": version, "limit": limit})
            rows = cur.fetchall()
        for listing_id, source, first_seen_at, description in rows:
            if listing_id in seen:
                continue
            seen.add(listing_id)
            out.append({"id": listing_id, "source": source,
                        "first_seen_at": first_seen_at, "description": description})
    return out


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
    """Write the columns the gate allows, then the cache row, in ONE transaction.

    Both or neither: a cache row without its fill would retire the listing from selection
    holding a value it never wrote, which is the deleted lane's permanence bug in a new
    key."""
    writable = [c for c in contract.writable_cells(str(row["source"])) if c in values]
    filled = {c: values[c] for c in writable}
    with conn.transaction():
        if writable:
            params: dict[str, Any] = {"id": row["id"]}
            params.update(filled)
            with conn.cursor() as cur:
                cur.execute(write_sql(writable), params)
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
    return writable


def run_pass(conn: Any, *, slice_size: int = PASS_SLICE, workers: int = WORKERS,
             backlog: bool = False) -> dict[str, Any]:
    """One bounded pass. Returns the dict the worker publishes in its heartbeat."""
    from toolkit import vision_batch

    scope = contract.extracted_cells()
    if not scope:
        return {"claimed": 0, "reason": "no_text_cells"}
    model = resolve_model(conn)
    if model is None:
        LOG.warning("text-extract lane idle: app_settings.%s is not set", MODEL_SETTING)
        return {"claimed": 0, "reason": "no_model"}
    version = extractor_version(model)
    rows = select_eligible(conn, version=version, slice_size=slice_size, backlog=backlog)
    if not rows:
        return {"claimed": 0, "extracted": 0, "written": 0, "dropped": 0,
                "spent_usd": 0.0, "errors": 0, "model": model, "backlog": backlog}

    written: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    tally = Lock()

    def _call(llm: Any, row: Mapping[str, Any]) -> tuple[float, Any]:
        fields = scope[str(row["source"])]
        tool = extraction_tool(fields)
        res = llm.call(
            called_for=CALLED_FOR, model=model, max_tokens=MAX_TOKENS,
            system=_SYSTEM_PROMPT, tools=[tool],
            # FORCED. A model that answers in prose produces no tool call, the listing
            # stays eligible and the next pass pays for the same refusal — for ever.
            tool_choice=tool["name"],
            messages=[{"role": "user", "content": str(row["description"])}],
        )
        payload = _tool_arguments(res)
        if payload is None:
            raise ValueError(f"the model returned no {tool['name']} call")
        return float(getattr(res, "cost_usd", 0.0) or 0.0), (res, payload)

    def _record(wconn: Any, row: Mapping[str, Any], result: Any,
                error: str | None) -> None:
        if error is not None or result is None:
            LOG.warning("TEXT-EXTRACT listing=%s failed: %s", row["id"], (error or "")[:200])
            return
        res, payload = result
        fields = scope[str(row["source"])]
        values, why = merge_extraction(
            payload, description=str(row["description"]), fields=fields)
        try:
            columns = record_extraction(
                wconn, row, values=values, extracted=dict(payload), model=model,
                version=version, llm_call_id=getattr(res, "llm_call_id", None),
                cost_usd=float(getattr(res, "cost_usd", 0.0) or 0.0),
            )
        except Exception as exc:  # noqa: BLE001 - the engine does not wrap `record`, and a
            # write failure that escapes here ends this WORKER THREAD, silently, mid-pass.
            LOG.warning("TEXT-EXTRACT listing=%s not stored: %s", row["id"], str(exc)[:200])
            return
        # Counters are touched by eight threads; `c[k] += 1` is a read-modify-write.
        with tally:
            dropped.update(f"{field}:{reason}" for field, reason in why.items())
            written.update(columns)

    stats = vision_batch.run_batch(
        rows=list(rows), call=_call, record=_record,
        max_usd=MAX_USD_PER_PASS, max_seconds=PASS_MAX_SECONDS, workers=workers,
    )
    return {
        "claimed": len(rows),
        "extracted": stats["ok"],
        "errors": stats["errors"],
        "written": sum(written.values()),
        "by_column": dict(written),
        "dropped": dict(dropped),
        "spent_usd": round(float(stats["spent"]), 4),
        "aborted": bool(stats["aborted"]),
        "fatal": stats["fatal"],
        "model": model,
        # The backlog signal, free: a full slice means more is waiting.
        "slice_full": len(rows) >= (slice_size if backlog else INFLOW_SLICE),
        "backlog": backlog,
    }
