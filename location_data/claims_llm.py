"""W2-10: the bazos free-text location lane — one structured LLM call per archived body.

A FOLLOWER lane. It reads the content-addressed payload store (`portal_raw_payloads` +
R2) minutes behind the scrape, never inline in the detail drain, and writes evidence-quoted
`llm_text` claims into `location_claims` through the shared intake write API.

WHY IT EXISTS. bazos' `raw_json` carries no description at all, so every street, část obce
and house number an ad states in prose is invisible to W1; the regex path is junk-prone
(regression 220870847 mined street='Nový' out of "Nový 2 pokojový byt" and geocoded ~130 km
away); and the maps-link pin cannot stand in for the text (the pin-derived obec is itself
wrong on measured rows). 29,546 active bazos rows share only 90 distinct `locality` values.

WHAT THE MODEL IS ASKED, and what the lane does with it (the operator's ruling, option (b)):
ONE call per listing asks for `from_description` and `from_title` SEPARATELY, each field
carrying a VERBATIM quote. The lane then emits exactly ONE claim per field per listing,
DESCRIPTION-FIRST — the title is a fallback rung consulted only when the description says
nothing about that field. The ranking is therefore an EXTRACTOR decision, taken here where
it is expressible, and not a resolution one: `location_field_policy` matches on
`(source, extraction_method)` only, so two `llm_text` claims from one portal cannot be
ranked as data at all (`survivorship.matches`).

THE LANE IS INERT TODAY. `LLM_READERS` is populated, but no shipped portal contract names
`llm_location_text`, so `run()` returns `outcome='inert'` BEFORE opening a batch row — a
batch stamped 'ok' moves the incremental watermark and would claim coverage of an archive
nothing ever read. The bazos contract entries that turn it on land in a separate bump
(bazos@3), together with the `location_field_policy` rungs without which a single-source
bazos LLM claim can never win a field.

KNOWN ASYMMETRY, stated rather than discovered later: the spend anti-join skips a payload
this MODEL and PROMPT already produced a claim from. A payload the model extracted NOTHING
from leaves no claim row and is therefore re-called on a later `--mode full` pass. The
incremental watermark bounds that in normal operation; the durable fix is a
`location_llm_attempts` table keyed `(payload_sha256, model, prompt_version)`, deferred.

Two rails the lane cannot relax:
  * `location_claim_observations` has NO unique constraint (dedupe is a NOT EXISTS
    anti-join against a PK of `(claim_id, observed_at, seq)` with a bigserial `seq`), so
    two concurrent runs at the same `observed_at` both insert. The `location_jobs` lease is
    the only guard.
  * `location_claim_fingerprint` (migration 386) hashes NEITHER `model` NOR
    `prompt_version` NOR any evidence field. A prompt edit without a `PROMPT_VERSION` bump
    is therefore permanently silent: the re-run dedupes onto the old row and can never
    correct its attribution. `tests/location_data/test_claims_llm.py` pins a digest of the
    prompt so the edit reds instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field as dataclass_field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import psycopg

from location_data import loader_db, payloads
from location_data.claims_intake import (
    _ACTIVE_CONTRACT_SQL,
    _BATCH_FINISH_SQL,
    _BATCH_INSERT_SQL,
    _RESUME_SQL,
    _WATERMARK_SQL,
    Absence,
    Claim,
    Entry,
    IntakeRefused,
    IntakeResult,
    ListingRow,
    MAX_CLAIM_VALUE_BYTES_ENV,
    DEFAULT_MAX_CLAIM_VALUE_BYTES,
    SOURCES,
    _base,
    _text,
    apply_transforms,
    assert_inventory_ready,
    env_positive_int,
    guarded,
    load_entries,
    missing_relations,
    write_result,
)
from location_data.claims_remine_archive import (
    ArchivedPayload,
    BodyStore,
    _DUMMY_LEGACY_COLUMNS,
    archived_claim_value_bytes,
    assert_evidence_complete,
    assert_stampable,
    load_bodies,
)
from location_data.html_scope import ScopeRegister, ScopedDocument, scope_html
from location_data.name_index import normalize_name, normalize_street_name
from location_data.resolver import lease
from scraper import db

LOG = logging.getLogger("location_data.claims_llm")

# ------------------------------------------------------------------ lane identity

# Bumped whenever this lane's extraction SEMANTICS change. It rides in
# `location_claim_batches.extractor_version` and on every absence row; the PER-CLAIM
# `extractor_version` stays the contract's own `contract:{source}@{version}`.
LLM_VERSION = "claims_llm@1"
LANE = "location_claims_llm"
WAVE = "W2"

JOB_NAME = "location_claims_llm"
CONCURRENCY_GROUP = "location-llm"
DEFAULT_LEASE_TTL_S = 3600

STATEMENT_TIMEOUT_ENV = "LOCATION_LLM_TIMEOUT_S"
# Every statement here is small — a keyset scan of tens of rows, one `id = ANY(...)` body
# read, one chunked write. 600 is the corpus-sweep lanes' budget and would hide a wedge.
DEFAULT_STATEMENT_TIMEOUT_S = 120
_FAILURE_STAMP_TIMEOUT_S = 30
DEFAULT_OVERLAP_HOURS = 3

# NOT `claims_intake.MIN/MAX_BATCH_SIZE` (10_000 / 30_000). A batch here is a BILL, not a
# scan: every row costs one model call, and the batch is the unit between write
# transactions.
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 500
DEFAULT_BATCH_SIZE = 50

PAGE_KIND = "detail"
DEFAULT_SOURCE = "bazos"

CALLED_FOR = "extract_location_claims"
PROMPT_VERSION = "bzs.loc@1"
MODEL_SETTING_KEY = "location_llm_model"
DEFAULT_MODEL = "gpt-5-nano"
# The house convention, learned twice the hard way: a GPT-5-series model spends its budget
# on REASONING before it emits anything, so 512 killed 99.6% of the enrichment lane's calls
# (PR #791) and ~300 killed half the exam lane's calibration calls. The answer here is ~200
# tokens; the rest is headroom for thinking.
MAX_TOKENS = 4096
# A provider outage (dead key, exhausted credit, sustained 5xx) fails EVERY call; without
# an abort the loop burns the whole wall clock logging per-listing errors and exits green.
_MAX_CONSECUTIVE_ERRORS = 5
# Planning figures only, and they are the pre-flight cap's whole basis — RE-MEASURE them
# from `llm_calls` after the bake-off (the exam lane's calibrate-first doctrine; a
# pre-flight sized from a guess is a cap in name only). ~1,200 input tokens (system +
# title + description) and a reasoning-dominated output.
ESTIMATED_USD_PER_CALL: dict[str, float] = {
    "gpt-5-nano": 0.00066,
    "gpt-5.6-luna": 0.00204,
    "qwen3.7-flash": 0.000114,
}
DEFAULT_ESTIMATED_USD_PER_CALL = 0.0025

# Per block, halved from the enrichment lane's single-block 8000 because there are two.
MAX_BLOCK_CHARS = 4000

# ------------------------------------------------------------------ the field vocabulary

# The model's field names -> the `location_claim_type` enum members a claim may carry.
# EVERY member here is verified against migration 380's enum by
# `contracts.CLAIM_TYPES` (mirrored, not invented) and by a test. `house_number` maps to
# TWO claim types because one stated number ("1216/46") carries both halves and the
# contract entry's `split_cp_co:cp` / `:co` transform is what selects one.
#
# Nothing was dropped: `landmark` and `address_line_verbatim` are both real enum members
# (they are permanently `is_admin_bearing = FALSE`, which is why neither is gazetteer-gated
# below, not a reason to refuse to store them).
FIELD_CLAIM_TYPES: dict[str, tuple[str, ...]] = {
    "obec": ("obec_name",),
    "cast_obce": ("cast_obce_name",),
    "psc": ("psc",),
    "street": ("street_name",),
    "house_number": ("house_number_cp", "house_number_co"),
    "landmark": ("landmark",),
    "lokalita_line": ("address_line_verbatim",),
}

# The order the lane RESOLVES fields in, which is not the order a contract lists them.
# `street` must be decided before the house numbers, because the address-point check is
# keyed on the street this listing actually claimed; `obec`/`psc` before `street` for the
# same reason one rung up.
FIELD_ORDER: tuple[str, ...] = (
    "obec", "cast_obce", "psc", "street", "house_number", "landmark", "lokalita_line",
)

# The blocks one call reads, in the OPERATOR'S PRIORITY ORDER. This tuple IS the
# "free text beats headline" ruling: the lane walks it and stops at the first block that
# states the field, so a listing whose street appears in both produces ONE claim, from the
# description. A block absent from a contract is simply skipped.
BLOCK_ORDER: tuple[str, ...] = ("description", "title")

# The model's own confidence, mapped to `match_confidence`. 'low' is never claimed — it is
# recorded as `stated_but_ambiguous`, the same treatment every other tool-using lane in
# this repo gives a low-confidence answer.
_MODEL_CONFIDENCE: dict[str, str] = {"high": "high", "medium": "medium"}
_ANSWER_CONFIDENCES = frozenset({"high", "medium", "low"})

# Absence reasons, ranked by how much they tell the operator. When no block produced a
# claim for a field, the lane records the MOST informative refusal it hit — "the registry
# does not carry this name" is a finding; "the model said nothing" is the null result.
_REASON_RANK: dict[str, int] = {
    "stated_but_ambiguous": 0,
    "only_in_excluded_block": 1,
    "not_attempted": 2,
    "not_stated": 3,
}


# ------------------------------------------------------------------ prompt + tool schema

SYSTEM_PROMPT = """\
Jsi extraktor adresních údajů z českých realitních inzerátů.

Dostaneš DVA bloky textu z jednoho inzerátu: TITULEK a POPIS. Pro KAŽDÝ blok zvlášť
vyplň strukturovanou odpověď. Nikdy nepřenášej údaj z jednoho bloku do druhého.

Pravidla:
- Vyplň pouze to, co je v daném bloku DOSLOVA napsáno. Nic nedovozuj, nic nedoplňuj
  z obecné znalosti, nikdy si nevymýšlej.
- Ke každé vyplněné hodnotě uveď `quote`: DOSLOVNÝ, nezkrácený úsek textu z TOHOTO bloku,
  ve kterém hodnota stojí. Musí jít o přesný podřetězec bloku, včetně diakritiky.
- Pokud údaj v bloku není, vrať value=null, quote=null, confidence="low".
- `street` = jen jméno ulice bez čísla a bez slova "ulice"/"ul." ("28. října", "náměstí Míru").
- `house_number` = jen číslo, ve tvaru "234" nebo "1216/46". Nikdy s ulicí.
- `obec` = město nebo obec. `cast_obce` = část obce / čtvrť ("Karlín", "Žižkov").
- `psc` = poštovní směrovací číslo, pět číslic ("186 00").
- `landmark` = orientační bod, pokud je uveden ("u kostela", "naproti Kauflandu").
- `lokalita_line` = doslovný celý řádek začínající "Lokalita:", pokud v bloku je.
- confidence="high" jen když hodnota stojí v textu jednoznačně a bez výkladu.
"""

_FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value", "quote", "confidence"],
    "properties": {
        "value": {"type": ["string", "null"]},
        "quote": {"type": ["string", "null"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}
_BLOCK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(FIELD_CLAIM_TYPES),
    "properties": {name: _FIELD_SCHEMA for name in FIELD_CLAIM_TYPES},
}
# STRUCTURED OUTPUT IS A FORCED FUNCTION TOOL. It is the only pattern in this repo
# (`grep -rn response_format` returns zero hits) and the only one every provider here
# speaks. `additionalProperties: false` reaches OpenAI/Qwen as an ordinary schema key with
# no `strict: true`, so it is ADVISORY — which is why `extract_payload` tolerates a
# malformed field rather than raising on it.
LOCATION_TOOL: dict[str, Any] = {
    "name": "record_location",
    "description": "Zapiš adresní údaje nalezené v titulku a v popisu inzerátu.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["from_title", "from_description"],
        "properties": {"from_title": _BLOCK_SCHEMA, "from_description": _BLOCK_SCHEMA},
    },
}


def build_user_message(blocks: dict[str, str]) -> str:
    """The prompt, built ONLY from the scoped document.

    `listings.description`, `listings.street`, `listings.locality`, the stored PSČ and the
    pin must NEVER appear here: with stored columns in the prompt the design measured 11
    high-confidence "stored echo" claims across 27 listings — the model reads back what we
    already believed and the claim looks like independent evidence.
    """
    return (f"TITULEK:\n{blocks.get('title', '')}\n\n"
            f"POPIS:\n{blocks.get('description', '')}\n")


# ------------------------------------------------------------------ value objects

@dataclass(frozen=True, slots=True)
class FieldAnswer:
    """One `{value, quote, confidence}` envelope out of the model's forced tool call."""
    value: str | None
    quote: str | None
    confidence: str


@dataclass(frozen=True, slots=True)
class ReadContext:
    """What the lane decided about ONE answer before handing it to the reader."""
    model: str
    prompt_version: str
    block: str
    field: str
    node: Any
    quote: str
    confidence: str


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why one (block, field) rung produced no claim. Never raised — a model response is
    not deterministic and one bad shape must not roll back a batch of paid work."""
    reason: str
    detail: str


class Gazetteer(Protocol):
    """The registry questions this lane asks. A Protocol so the tests inject a fake and
    production injects a version-pinned RÚIAN view."""

    def name_exists(self, name_norm: str) -> bool: ...
    def obec_codes_for_name(self, name_norm: str) -> list[int]: ...
    def street_in_obec(self, obec_kod: int, street_norm: str) -> bool: ...
    def address_point_exists(self, *, obec_kod: int, street_norm: str | None,
                             cp: int | None, co: int | None) -> bool: ...
    def obec_codes_for_psc(self, psc: str) -> list[int]: ...


class RegistryGazetteer:
    """The production adapter over a version-pinned `resolver.resolve_db` registry view.

    The semantics mirror `core._gazetteer_validate` (the resolver's own invariant 3)
    EXACTLY where they overlap, so the lane never drops a claim S7 would have accepted and
    never writes one S7 would then reject:
      * a name check is `admin_units_by_name` with NO level filter;
      * a street check passes when the candidate obec is unknown.

    It is deliberately STRICTER than S7 in one direction: the street must be a real
    `ruian_streets.name_norm` inside the candidate obec. That is the phantom-street guard
    regression 220870847 asked for, and it is a registry membership test rather than the
    morphology heuristic `bzs.det.street_text` declared and never implemented.

    Normalisation goes through `name_index` — the LOADER functions that WROTE `name_norm`,
    registry-versioned. NOT `resolver.normalize.normalize_match_key` (byte-identical
    algorithm, resolver-versioned lifecycle) and never the SQL `location_value_norm` (which
    writes `location_claims.value_norm` and is applied by the database, not by us).
    """

    def __init__(self, view: Any) -> None:
        self._view = view

    def name_exists(self, name_norm: str) -> bool:
        return bool(name_norm) and bool(self._view.admin_units_by_name(name_norm))

    def obec_codes_for_name(self, name_norm: str) -> list[int]:
        if not name_norm:
            return []
        units = self._view.admin_units_by_name(name_norm, levels=("obec",))
        return sorted({int(u.obec_kod) for u in units if u.obec_kod is not None})

    def street_in_obec(self, obec_kod: int, street_norm: str) -> bool:
        if not street_norm:
            return False
        return any(s.name_norm == street_norm
                   for s in self._view.streets_in_obec(obec_kod))

    def address_point_exists(self, *, obec_kod: int, street_norm: str | None,
                             cp: int | None, co: int | None) -> bool:
        return bool(self._view.address_points_by_number(
            obec_kod=obec_kod, street_name_norm=street_norm,
            cislo_domovni=cp, cislo_orientacni=co))

    def obec_codes_for_psc(self, psc: str) -> list[int]:
        return [int(code) for code in self._view.obec_codes_for_psc(psc)]


def open_gazetteer(conn: psycopg.Connection) -> tuple[RegistryGazetteer, int, str]:
    """A cached, version-pinned registry view. Raises `LookupError` when the RÚIAN mirror
    is unloaded — refusing is the only honest answer, because the alternative is writing
    claims nothing validated."""
    from location_data.resolver import resolve_db

    version_id, version_label = resolve_db.current_registry_version(conn)
    view = resolve_db.CachedRegistryView(
        resolve_db.SqlRegistryView(conn, version_id),
        resolve_db.RunCache(max_entries=250_000))
    return RegistryGazetteer(view), version_id, version_label


# ------------------------------------------------------------------ the third registry

LlmReaderFn = Callable[
    [Entry, ListingRow, ArchivedPayload, ScopedDocument, FieldAnswer, ReadContext],
    list[Claim]]

# A THIRD runtime registry, deliberately not an extension of either sibling: W1's readers
# take `(entry, row)` over `raw_json`, the archive lane's take a scoped DOM, and these take
# a scoped DOM PLUS one structured answer from a model. A name resolving in the wrong
# registry would silently read the wrong substrate.
#
# `claims_intake.LLM_ONLY_READERS` mirrors this set BY NAME (importing this module from the
# hourly W1 lane would be circular and would drag a provider key into it), and a test
# asserts the two are equal — a reader added here without a line there stops being SKIPPED
# by W1 and starts being REFUSED by it, taking that portal's hourly intake down.
LLM_READERS: dict[str, LlmReaderFn] = {}


def llm_reader(name: str) -> Callable[[LlmReaderFn], LlmReaderFn]:
    def register(fn: LlmReaderFn) -> LlmReaderFn:
        LLM_READERS[name] = fn
        return fn
    return register


def _entry_css(entry: Entry) -> str:
    """The CSS selector a free-text entry must declare — the node the evidence span is
    anchored INTO. Refused rather than defaulted: "match nothing" would be a coverage hole
    with no claim, no absence and no error."""
    css = entry.locator.get("css")
    if not css or not isinstance(css, str):
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} uses an LLM reader but declares no "
            f"`locator.css` (got {css!r})")
    return css


@llm_reader("llm_location_text")
def _read_llm_location_text(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
    answer: FieldAnswer, ctx: ReadContext,
) -> list[Claim]:
    """One validated model answer, stamped as a claim. Empty list = the transform kept
    nothing, or the quote does not occur in the node the answer claims to have read.

    The LANE decides whether this rung is even attempted (value present, confidence not
    low, gazetteer satisfied); this states the value and the evidence. `apply_transforms`
    is called HERE, which is what `READER_CONTRACTS['llm_location_text']
    .consults_transforms = True` records — a `house_number` answer of "1216/46" becomes
    "1216" under `split_cp_co:cp` and "46" under `:co`, so one answer serves two entries.
    """
    if str(entry.locator["llm_block"]) != ctx.block:
        raise IntakeRefused(
            f"{entry.entry_id} declares llm_block='{entry.locator['llm_block']}' but was "
            f"offered the '{ctx.block}' block; the projection and the lane disagree")
    if str(entry.locator["llm_field"]) != ctx.field:
        raise IntakeRefused(
            f"{entry.entry_id} declares llm_field='{entry.locator['llm_field']}' but was "
            f"offered the '{ctx.field}' answer; the projection and the lane disagree")
    value = apply_transforms(_text(answer.value), entry.transform)
    if value is None:
        return []
    # THE HALLUCINATION GUARD. An unlocatable quote is a claim with no evidence, and
    # `loc_claim_text_evidence` would refuse it at the constraint — after the batch is
    # already open. Returning nothing here is the skip; the lane writes the absence.
    span = document.find_span(ctx.quote, within=ctx.node)
    if span is None:
        return []
    return [_base(
        entry, row,
        value_text=value,
        evidence_quote=ctx.quote,
        span_start=span[0],
        span_end=span[1],
        claim_confidence=ctx.confidence,
        model=ctx.model,
        prompt_version=ctx.prompt_version,
        payload_id=payload.id,
        payload_sha256=payload.payload_sha256,
        payload_scope_version=document.scope_version,
        first_observed_at=payload.first_observed_at,
    )]


# ------------------------------------------------------------------ the pure extraction

def llm_entries(entries: Iterable[Entry], page_kind: str) -> list[Entry]:
    """The entries this lane may execute against ONE archived body: a reader it
    implements, declared for the page kind the body actually is."""
    return [e for e in entries
            if e.reader in LLM_READERS and e.page_kind == page_kind]


def _validated_groups(
    applicable: list[Entry],
) -> tuple[dict[str, str], dict[tuple[str, tuple[str, ...]], dict[str, Entry]]]:
    """(css per block, {output field -> {block -> entry}}), refusing a contradictory set.

    The OUTPUT FIELD is `(claim_type, transform)`, not the model's field name: one
    `house_number` answer legitimately produces a `house_number_cp` claim and a
    `house_number_co` one, and each of those gets its own description-first ladder.
    """
    css_by_block: dict[str, str] = {}
    groups: dict[tuple[str, tuple[str, ...]], dict[str, Entry]] = {}
    for entry in applicable:
        block = str(entry.locator.get("llm_block") or "")
        field = str(entry.locator.get("llm_field") or "")
        if block not in BLOCK_ORDER:
            raise IntakeRefused(
                f"{entry.source}:{entry.entry_id} declares llm_block={block!r}; this lane "
                f"reads {list(BLOCK_ORDER)}")
        if field not in FIELD_CLAIM_TYPES:
            raise IntakeRefused(
                f"{entry.source}:{entry.entry_id} declares llm_field={field!r}; the tool "
                f"schema carries {sorted(FIELD_CLAIM_TYPES)}")
        if entry.claim_type not in FIELD_CLAIM_TYPES[field]:
            raise IntakeRefused(
                f"{entry.source}:{entry.entry_id} maps llm_field='{field}' to "
                f"claim_type='{entry.claim_type}'; that field may only claim "
                f"{list(FIELD_CLAIM_TYPES[field])}")
        css = _entry_css(entry)
        seen_css = css_by_block.setdefault(block, css)
        if seen_css != css:
            raise IntakeRefused(
                f"{entry.source}: the '{block}' block is declared with two different "
                f"selectors ({seen_css!r} and {css!r}); one block is one node, and one "
                f"call reads it once")
        key = (entry.claim_type, entry.transform)
        rungs = groups.setdefault(key, {})
        if block in rungs:
            raise IntakeRefused(
                f"{entry.source}: {rungs[block].entry_id} and {entry.entry_id} both claim "
                f"'{entry.claim_type}' from the '{block}' block; one field per block is "
                f"one claim, so the second one could only overwrite the first")
        rungs[block] = entry
    return css_by_block, groups


def _group_order(key: tuple[str, tuple[str, ...]], entry: Entry) -> tuple[int, str]:
    field = str(entry.locator.get("llm_field") or "")
    index = FIELD_ORDER.index(field) if field in FIELD_ORDER else len(FIELD_ORDER)
    return index, key[0]


def parse_field_answer(answer: dict[str, Any], block: str, field: str) -> FieldAnswer | Refusal:
    """One `{value, quote, confidence}` envelope out of the parsed tool input.

    A missing key, a non-dict or an out-of-vocabulary confidence is a REFUSAL, never an
    exception: `additionalProperties: false` is advisory on every provider this repo
    speaks to, and one malformed field must not roll back a batch of paid calls.
    """
    block_answer = answer.get(f"from_{block}")
    if not isinstance(block_answer, dict):
        return Refusal("not_attempted",
                       f"the model returned no '{block}' block")
    envelope = block_answer.get(field)
    if not isinstance(envelope, dict):
        return Refusal("not_attempted",
                       f"the model returned no '{field}' envelope in the '{block}' block")
    confidence = str(envelope.get("confidence") or "").lower()
    if confidence not in _ANSWER_CONFIDENCES:
        return Refusal(
            "not_attempted",
            f"'{block}.{field}' carries confidence={envelope.get('confidence')!r}, which "
            f"is outside {sorted(_ANSWER_CONFIDENCES)}")
    value = _text(envelope.get("value"))
    quote = _text(envelope.get("quote"))
    return FieldAnswer(value=value, quote=quote, confidence=confidence)


def _candidate_obec(
    answer: dict[str, Any], *, gazetteer: Gazetteer, psc: str | None,
) -> tuple[int | None, str]:
    """The one cross-field dependency, resolved ONCE per listing and deterministically.

    Description obec, then title obec, then the stored PSČ — and each rung only counts when
    it resolves to EXACTLY ONE obec code, because an ambiguous name is not a candidate. The
    PSČ never reaches the prompt; it is a registry key here and nothing else.
    """
    for block in BLOCK_ORDER:
        parsed = parse_field_answer(answer, block, "obec")
        if isinstance(parsed, Refusal) or parsed.value is None:
            continue
        codes = gazetteer.obec_codes_for_name(normalize_name(parsed.value))
        if len(codes) == 1:
            return codes[0], f"obec_from_llm_{block}"
    if psc:
        digits = "".join(ch for ch in str(psc) if ch.isdigit())
        if len(digits) == 5:
            codes = gazetteer.obec_codes_for_psc(digits)
            if len(codes) == 1:
                return codes[0], "obec_from_psc"
    return None, "obec_unknown"


def gazetteer_refusal(
    claim_type: str, value: str, *, gazetteer: Gazetteer, obec_kod: int | None,
    street_norm: str | None,
) -> Refusal | None:
    """None when the registry admits the value; a `Refusal` naming the exact check when it
    does not. An unresolvable name writes NO claim — doctrine #5's "no address point exists
    here must be an honest answer, never a nearest-neighbour snap"."""
    if claim_type in ("obec_name", "cast_obce_name"):
        if not gazetteer.name_exists(normalize_name(value)):
            return Refusal("stated_but_ambiguous",
                           f"gazetteer: no admin unit named {value!r}")
        return None
    if claim_type == "street_name":
        if obec_kod is None:
            # Mirrors S7 exactly (`core._gazetteer_validate` passes when obec_kod is None):
            # no constraint is not a rejection. Counted as `street_unvalidated` by the run.
            return None
        if not gazetteer.street_in_obec(obec_kod, normalize_street_name(value)):
            return Refusal("stated_but_ambiguous",
                           f"gazetteer: no street {value!r} in obec {obec_kod}")
        return None
    if claim_type in ("house_number_cp", "house_number_co"):
        if obec_kod is None:
            return Refusal(
                "stated_but_ambiguous",
                "gazetteer: no candidate obec, so no address point can be checked")
        digits = value.strip()
        if not digits.isdigit():
            return Refusal("stated_but_ambiguous",
                           f"house number {value!r} is not an integer")
        number = int(digits)
        cp = number if claim_type == "house_number_cp" else None
        co = number if claim_type == "house_number_co" else None
        if not gazetteer.address_point_exists(
                obec_kod=obec_kod, street_norm=street_norm, cp=cp, co=co):
            return Refusal(
                "stated_but_ambiguous",
                f"gazetteer: no address point {value!r} in obec {obec_kod}"
                + (f" on street {street_norm!r}" if street_norm else ""))
        return None
    # `psc`, `landmark`, `address_line_verbatim`: not admin-bearing, nothing to reconcile
    # against an admin unit. Storing them un-gated is the honest treatment, not a gap.
    return None


def extract_payload(
    payload: ArchivedPayload,
    row: ListingRow,
    entries: list[Entry],
    answer: dict[str, Any],
    *,
    document: ScopedDocument,
    model: str,
    prompt_version: str = PROMPT_VERSION,
    gazetteer: Gazetteer,
    psc: str | None = None,
    max_value_bytes: int | None = None,
    stats: dict[str, int] | None = None,
) -> IntakeResult:
    """Everything this lane knows about one archived body and one model answer.

    Pure — no DB, no clock, no network. This is what the hermetic tests drive.
    """
    if max_value_bytes is None:
        max_value_bytes = env_positive_int(MAX_CLAIM_VALUE_BYTES_ENV,
                                           DEFAULT_MAX_CLAIM_VALUE_BYTES)
    result = IntakeResult()
    applicable = llm_entries(entries, payload.page_kind)
    if not applicable:
        return result

    css_by_block, groups = _validated_groups(applicable)

    if not document.is_complete:
        # `html_scope` fails CLOSED and an incomplete result admits nothing: "the scoper
        # broke" must never read as "no zones matched, extract freely". The attempt is
        # still recorded, so the cohort is countable rather than indistinguishable from a
        # page that genuinely carried no address.
        for entry in applicable:
            result.absences.append(Absence(
                listing_id=row.listing_id, surface=entry.surface,
                field_=entry.claim_type, reason="not_attempted",
                extraction_method=entry.extraction_method,
                detail="exclusion-zone scoping incomplete; the boundary had a hole"))
        return result

    nodes = {block: document.css_first(css) for block, css in css_by_block.items()}
    obec_kod, obec_rung = _candidate_obec(answer, gazetteer=gazetteer, psc=psc)
    if stats is not None:
        stats[obec_rung] = stats.get(obec_rung, 0) + 1

    street_norm: str | None = None
    ordered = sorted(groups.items(), key=lambda item: _group_order(
        item[0], next(iter(item[1].values()))))
    for key, rungs in ordered:
        claim_type, _transform = key
        chosen: Claim | None = None
        refusals: list[tuple[Entry, Refusal]] = []
        for block in BLOCK_ORDER:
            entry = rungs.get(block)
            if entry is None:
                continue
            node = nodes.get(block)
            if node is None:
                refusals.append((entry, Refusal(
                    "not_stated",
                    f"the '{block}' block ({css_by_block[block]}) is not on this page")))
                continue
            parsed = parse_field_answer(answer, block, str(entry.locator["llm_field"]))
            if isinstance(parsed, Refusal):
                refusals.append((entry, parsed))
                continue
            if parsed.value is None:
                refusals.append((entry, Refusal(
                    "not_stated", f"the model states no {claim_type} in the {block}")))
                continue
            if parsed.confidence not in _MODEL_CONFIDENCE:
                refusals.append((entry, Refusal(
                    "stated_but_ambiguous",
                    f"the model's own confidence in this {claim_type} is "
                    f"'{parsed.confidence}'")))
                continue
            quote = parsed.quote or parsed.value
            if document.find_span(quote, within=node) is None:
                reason = "not_attempted" if document.admits(quote) \
                    else "only_in_excluded_block"
                refusals.append((entry, Refusal(
                    reason,
                    f"quote not locatable in the scoped '{block}' block: "
                    f"{quote[:120]!r}")))
                continue
            ctx = ReadContext(
                model=model, prompt_version=prompt_version, block=block,
                field=str(entry.locator["llm_field"]), node=node, quote=quote,
                confidence=_MODEL_CONFIDENCE[parsed.confidence])
            claims = LLM_READERS[str(entry.reader)](
                entry, row, payload, document, parsed, ctx)
            if not claims:
                refusals.append((entry, Refusal(
                    "not_stated",
                    f"nothing left of the {block} {claim_type} after the entry's "
                    f"transform")))
                continue
            candidate = claims[0]
            # THE GAZETTEER GATE RUNS ON WHAT WOULD BE WRITTEN, never on the model's raw
            # answer: the entry's transform is what turns one stated "1216/46" into the čp
            # "1216" and the čo "46", and checking the pre-transform string against
            # `ruian_address_points` would refuse both halves of every real address.
            gate = gazetteer_refusal(
                claim_type, candidate.value_text or "", gazetteer=gazetteer,
                obec_kod=obec_kod, street_norm=street_norm)
            if gate is not None:
                refusals.append((entry, gate))
                continue
            assert_stampable(candidate)
            assert_evidence_complete(candidate)
            if archived_claim_value_bytes(candidate) > max_value_bytes:
                # An absence and NO refetch row: an archived body is immutable and
                # content-addressed, so re-reading it yields the same oversized value
                # forever. What fixes this is a narrower prompt or a transform.
                refusals.append((entry, Refusal(
                    "not_attempted",
                    f"{claim_type} plus its evidence quote exceeds "
                    f"{max_value_bytes} bytes on this body")))
                result.oversized += 1
                continue
            chosen = candidate
            break
        if chosen is not None:
            if claim_type == "street_name":
                street_norm = normalize_street_name(chosen.value_text or "")
            result.claims.append(chosen)
            continue
        if refusals:
            entry, refusal = min(
                refusals, key=lambda pair: _REASON_RANK.get(pair[1].reason, 9))
            result.absences.append(Absence(
                listing_id=row.listing_id, surface=entry.surface, field_=claim_type,
                reason=refusal.reason, extraction_method=entry.extraction_method,
                detail=refusal.detail))
    return result


# ------------------------------------------------------------------ the model call

class LlmCaller(Protocol):
    """One structured call. Returns (parsed tool input, cost_usd)."""

    def answer(self, blocks: dict[str, str]) -> tuple[dict[str, Any], float]: ...


class ProviderCaller:
    """The production caller: one forced-tool call per listing through `LLMClient`.

    The providers are listed EXPLICITLY rather than taken from
    `api.dependencies.get_providers()`, because no cron script in this repo uses that
    accessor — and a model whose provider is unregistered raises in `LLMClient.call`
    BEFORE the try/except that writes the failure row, leaving zero `llm_calls` evidence
    and going invisible to llm_errors, llm_burn_rate and llm_liveness alike.
    """

    def __init__(self, conn: psycopg.Connection, *, model: str) -> None:
        from api.llm_client import LLMClient
        from api.providers.openai import OpenAIProvider
        from api.providers.qwen import QwenProvider

        self._model = model
        self._client = LLMClient(conn, providers={
            "openai": OpenAIProvider(), "qwen": QwenProvider(),
        })

    def answer(self, blocks: dict[str, str]) -> tuple[dict[str, Any], float]:
        from api.llm_client import parse_tool_input_json

        response = self._client.call(
            called_for=CALLED_FOR,
            messages=[{"role": "user", "content": build_user_message(blocks)}],
            system=SYSTEM_PROMPT,
            tools=[LOCATION_TOOL],
            tool_choice=LOCATION_TOOL["name"],
            model=self._model,
            max_tokens=MAX_TOKENS,
        )
        for call in response.tool_calls:
            if call.get("name") == LOCATION_TOOL["name"]:
                return parse_tool_input_json(call.get("input")), float(
                    response.cost_usd or 0.0)
        # A model that answered without calling the forced tool has said nothing this lane
        # can use. An empty dict is the honest record: every field becomes a missing key,
        # which becomes one `not_attempted` absence per field.
        return {}, float(response.cost_usd or 0.0)


class FileCaller:
    """`--fake-llm PATH`: the same answer for every listing, read off disk. Hermetic
    smoke-testing of the scan/scope/write path with no provider key and no spend."""

    def __init__(self, path: str) -> None:
        with open(path, encoding="utf-8") as handle:
            self._answer = json.load(handle)

    def answer(self, blocks: dict[str, str]) -> tuple[dict[str, Any], float]:
        return dict(self._answer), 0.0


def block_texts(document: ScopedDocument, css_by_block: dict[str, str]) -> dict[str, str]:
    """The scoped text of each declared block, truncated. The ONLY input to the prompt."""
    texts: dict[str, str] = {}
    for block, css in css_by_block.items():
        node = document.css_first(css)
        if node is None:
            texts[block] = ""
            continue
        text = node.text(strip=True) or ""
        texts[block] = text[:MAX_BLOCK_CHARS]
    return texts


# ------------------------------------------------------------------ SQL

# ONE scan literal for both modes, unlike the archive lane's two: `--mode full` passes the
# epoch as both the watermark and the keyset anchor, so the `(first_observed_at, id)` order
# is correct either way and the SQL-correctness gate has one statement to PREPARE.
#
# THE JOIN IS ON `(source, source_id_native)`, NEVER on `portal_raw_payloads.listing_id` —
# that column is nullable and populated by nothing, so an inner join on it matches zero
# rows over the whole archive while the batch still stamps 'ok'.
#
# "Latest body per key" is the NOT EXISTS anti-join on `(first_observed_at, id)`, never
# `version_seq` (migration 403 added it with no backfill, so NULL would rank an older row
# latest) and never `last_observed_at` (an unchanged refetch bumps it).
#
# THE SECOND ANTI-JOIN IS THE SPEND GUARD: a payload this model and prompt already produced
# a claim from is never re-called. `location_claims_payload` (migration 382) indexes
# `payload_sha256`. Its known asymmetry is in the module docstring.
#
# `l.is_active` bounds the bill to the serving cohort — a delisted listing keeps every claim
# it already has, we simply stop paying for new ones. `raw_json ->> 'psc'` is one scalar out
# of bazos' slim payload dict and reaches the GAZETTEER only, never the prompt.
_LLM_SCAN_SQL = """
    SELECT p.id, p.source, p.source_id_native, p.page_kind::text,
           encode(p.payload_sha256, 'hex'), p.first_observed_at,
           l.id, (a.listing_id IS NOT NULL) AS in_mapy_inventory,
           l.raw_json ->> 'psc'
    FROM portal_raw_payloads p
    JOIN listings l ON l.source = p.source AND l.source_id_native = p.source_id_native
    LEFT JOIN mapy_affected a ON a.listing_id = l.id
    WHERE p.source = %(source)s
      AND p.page_kind = 'detail'
      AND l.is_active
      AND p.first_observed_at >= %(watermark)s
      AND (p.first_observed_at, p.id) > (%(after_ts)s, %(after_id)s)
      AND (p.http_status IS NULL OR p.http_status BETWEEN 200 AND 299)
      AND NOT EXISTS (
          SELECT 1 FROM portal_raw_payloads n
          WHERE n.source = p.source
            AND n.source_id_native = p.source_id_native
            AND n.page_kind = p.page_kind
            AND (n.http_status IS NULL OR n.http_status BETWEEN 200 AND 299)
            AND (n.first_observed_at, n.id) > (p.first_observed_at, p.id))
      AND NOT EXISTS (
          SELECT 1 FROM location_claims c
          WHERE c.payload_sha256 = p.payload_sha256
            AND c.listing_id = l.id
            AND c.extraction_method = 'llm_text'
            AND c.model = %(model)s
            AND c.prompt_version = %(prompt_version)s)
    ORDER BY p.first_observed_at, p.id
    LIMIT %(batch_size)s
"""

_LLM_EXCLUSION_ZONES_SQL = """
    SELECT exclusion_zones
    FROM portal_contracts
    WHERE source = %(source)s AND is_active
"""

_MODEL_SETTING_SQL = "SELECT value FROM app_settings WHERE key = %(key)s"

# The `--mode full` floor. Timezone-AWARE, because `first_observed_at` is `timestamptz`
# and a naive comparison would be read in the server's own zone.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _row_from_record(record: tuple[Any, ...]) -> tuple[ArchivedPayload, ListingRow, str | None]:
    (payload_id, source, native, page_kind, sha_hex, first_observed_at, listing_id,
     in_inventory, psc) = record
    payload = ArchivedPayload(
        id=int(payload_id), source=source, source_id_native=str(native),
        page_kind=page_kind, payload_sha256=str(sha_hex),
        first_observed_at=first_observed_at)
    row = ListingRow(
        listing_id=int(listing_id), source=source, source_id_native=str(native),
        raw_json={}, lat=None, lon=None,
        # The BODY's own first observation, never now() and never `last_observed_at`
        # (which an unchanged refetch moves without new evidence).
        observed_at=first_observed_at, in_mapy_inventory=bool(in_inventory),
        legacy_columns=dict(_DUMMY_LEGACY_COLUMNS))
    return payload, row, (str(psc) if psc else None)


def load_register(conn: psycopg.Connection, source: str) -> ScopeRegister | None:
    """The DEPLOYED exclusion-zone register, whose hash is the `payload_scope_version`
    every claim carries — read from `portal_contracts`, never re-parsed from the YAML on
    disk. A lane must scope by the register that is deployed."""
    with conn.cursor() as cur:
        cur.execute(_LLM_EXCLUSION_ZONES_SQL, {"source": source})
        row = cur.fetchone()
    if row is None:
        return None
    return ScopeRegister.from_zones(source, row[0] or ())


def resolve_model(conn: psycopg.Connection, override: str | None) -> str:
    if override:
        return override
    with conn.cursor() as cur:
        cur.execute(_MODEL_SETTING_SQL, {"key": MODEL_SETTING_KEY})
        row = cur.fetchone()
    if row and isinstance(row[0], str) and row[0].strip():
        return row[0].strip()
    return DEFAULT_MODEL


def _resume_point(
    conn: psycopg.Connection, *, mode: str, source: str, watermark: datetime | None,
) -> dict[str, Any] | None:
    """`claims_intake._resume_point`'s logic against THIS lane's rows — that function
    closes over its own module-level `LANE`, so it cannot be shared."""
    with conn.cursor() as cur:
        cur.execute(_RESUME_SQL, {"lane": LANE, "source": source, "scan_mode": mode})
        row = cur.fetchone()
    if not row:
        return None
    outcome, after_id, after_ts, coverage_since = row
    if outcome != "stopped" or after_id is None or after_ts is None:
        return None
    if watermark is not None and after_ts < watermark:
        return None
    return {"after_id": int(after_id), "after_ts": after_ts,
            "coverage_since": coverage_since}


# ------------------------------------------------------------------ the run

@dataclass(slots=True)
class RunStats:
    payloads: int = 0
    calls: int = 0
    claims: int = 0
    claims_inserted: int = 0
    observations: int = 0
    enqueued: int = 0
    absences: int = 0
    oversized_values: int = 0
    bodies_from_r2: int = 0
    errors: int = 0
    spent_usd: float = 0.0
    obec: dict[str, int] = dataclass_field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "payloads": self.payloads, "calls": self.calls, "claims": self.claims,
            "claims_inserted": self.claims_inserted, "observations": self.observations,
            "enqueued": self.enqueued, "absences": self.absences,
            "oversized_values": self.oversized_values,
            "bodies_from_r2": self.bodies_from_r2, "errors": self.errors,
            "spent_usd": round(self.spent_usd, 6), "obec_rungs": dict(self.obec),
        }


def estimated_cost_usd(model: str, rows: int) -> float:
    return rows * ESTIMATED_USD_PER_CALL.get(model, DEFAULT_ESTIMATED_USD_PER_CALL)


def run(
    conn: psycopg.Connection,
    *,
    mode: str = "incremental",
    source: str = DEFAULT_SOURCE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_seconds: float | None = None,
    limit: int | None = None,
    max_usd: float | None = None,
    model: str | None = None,
    overlap_hours: int = DEFAULT_OVERLAP_HOURS,
    statement_timeout: int = DEFAULT_STATEMENT_TIMEOUT_S,
    dry_run: bool = False,
    note: str | None = None,
    store: BodyStore | None = None,
    llm: LlmCaller | None = None,
    gazetteer: Gazetteer | None = None,
) -> dict[str, Any]:
    """Preflight, then batches of `batch_size` listings against one source.

    THE TRANSACTION RULE, and it is the one real divergence from the archived-HTML lane:
    the model is NEVER called inside a `guarded()` block. That lane holds one transaction
    across its scan, its R2 GETs, its extraction and its write; here a single call takes
    seconds, and holding a transaction across a batch of them is idle-in-transaction on the
    transaction-mode pooler for the whole batch. Each iteration is scan / body-load /
    (no transaction: scope, prompt, call, extract) / write.
    """
    missing = missing_relations(conn)
    if missing:
        raise IntakeRefused(
            f"location schema not applied; missing {', '.join(missing)} "
            f"(migrations 380-389)")
    # Kept from the sibling lanes' preflight even though this lane emits no `coordinate`
    # claim, so the Mapy licence gate is not its own input: `in_mapy_inventory` is still
    # projected onto every row, and a location lane running while the affected-set
    # inventory is unestablished is a state the program has decided not to operate in.
    assert_inventory_ready(conn)

    entries_by_source = load_entries(conn)
    entries = entries_by_source.get(source) or []
    if not entries:
        raise IntakeRefused(
            f"no ACTIVE portal contract for {source}: git is the store of record and the "
            f"DB tables are its projection — run `python -m location_data.contracts "
            f"--load` (02 §2.1.8)")

    readable = llm_entries(entries, PAGE_KIND)
    if not readable:
        # INERT, and it returns BEFORE `_BATCH_INSERT_SQL` and before `open_store()`. A
        # batch that reached the end of its scan is stamped 'ok', and 'ok' is what the
        # incremental watermark reads — so opening one here would let a lane with no
        # executable entry claim it had mined the whole archive, and the contract bump that
        # turns the lane on would start behind a watermark covering bodies nothing read.
        LOG.info("CLAIMS-LLM inert: no ACTIVE %s contract entry names a reader from "
                 "LLM_READERS. No batch opened, no model called.", source)
        return {"outcome": "inert", "mode": mode, "source": source,
                **RunStats().as_dict(), "batch_id": None}

    resolved_model = model or resolve_model(conn, None)
    planned = limit if limit is not None else batch_size
    estimate = estimated_cost_usd(resolved_model, planned)
    if max_usd is not None and estimate > max_usd:
        # THE CAP IS PRE-FLIGHT. `api.llm_client`'s daily-cost check only LOGS, and on a
        # long run it notices after the money is gone.
        raise IntakeRefused(
            f"pre-flight estimate ${estimate:.2f} for {planned} calls on "
            f"{resolved_model} exceeds --max-usd ${max_usd:.2f}; lower --limit or raise "
            f"the cap. Re-measure ESTIMATED_USD_PER_CALL from llm_calls after a real pass")

    register = load_register(conn, source)
    if register is None:
        raise IntakeRefused(f"no active portal_contracts row for {source}")
    if gazetteer is None:
        try:
            gazetteer, _version_id, version_label = open_gazetteer(conn)
        except LookupError as exc:
            raise IntakeRefused(
                f"the RÚIAN mirror has no current registry version ({exc}); this lane "
                f"validates every admin-bearing value against it and will not write "
                f"unvalidated claims") from exc
        LOG.info("CLAIMS-LLM gazetteer pinned to registry version %s", version_label)
    if store is None:
        store = payloads.open_store()
    if llm is None and not dry_run:
        llm = ProviderCaller(conn, model=resolved_model)

    # Validated ONCE, not per listing: the entry set is constant for the whole run, and a
    # contradictory contract must refuse before a single call is billed.
    css_by_block, _groups = _validated_groups(readable)

    contract_id: int | None = None
    with guarded(conn, statement_timeout) as cur:
        cur.execute(_ACTIVE_CONTRACT_SQL, {"source": source})
        row = cur.fetchone()
        contract_id = int(row[0]) if row else None

    watermark: datetime | None = None
    if mode == "incremental":
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_WATERMARK_SQL, {"lane": LANE, "source": source})
            row = cur.fetchone()
        watermark = row[0] - timedelta(hours=overlap_hours) if row and row[0] else None
        if watermark is None:
            LOG.info("CLAIMS-LLM no prior successful batch for source=%s; incremental "
                     "degrades to a full pass", source)
            mode = "full"

    after_ts = watermark
    after_id = 0
    resumed_from = _resume_point(conn, mode=mode, source=source, watermark=watermark)
    if resumed_from is not None:
        after_id = int(resumed_from["after_id"])
        after_ts = resumed_from["after_ts"]
        LOG.info("CLAIMS-LLM resuming a budget-stopped %s scan from after_id=%d "
                 "after_ts=%s", mode, after_id, after_ts)

    batch_id: int | None = None
    if not dry_run:
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_BATCH_INSERT_SQL, {
                "lane": LANE, "source": source, "extractor_version": LLM_VERSION,
                "contract_id": contract_id, "wave": WAVE,
                "job_run_id": os.environ.get("GITHUB_RUN_ID"), "note": note,
                "scan_mode": mode, "resumable": True,
                "coverage_since": (resumed_from or {}).get("coverage_since"),
            })
            batch_id = int(cur.fetchone()[0])

    stats = RunStats()
    deadline = None if max_seconds is None else time.monotonic() + max_seconds
    reached_end = False
    stopped_early = False
    consecutive_errors = 0
    try:
        while True:
            if limit is not None and stats.payloads >= limit:
                stopped_early = True
                break
            if deadline is not None and time.monotonic() > deadline:
                LOG.info("CLAIMS-LLM stopping: --max-seconds reached")
                stopped_early = True
                break
            if max_usd is not None and stats.spent_usd >= max_usd:
                LOG.info("CLAIMS-LLM stopping: --max-usd reached (spent %.4f)",
                         stats.spent_usd)
                stopped_early = True
                break
            size = batch_size if limit is None else min(batch_size, limit - stats.payloads)

            with guarded(conn, statement_timeout) as cur:
                cur.execute(_LLM_SCAN_SQL, {
                    "source": source,
                    "watermark": watermark or EPOCH,
                    "after_ts": after_ts or EPOCH,
                    "after_id": after_id,
                    "model": resolved_model,
                    "prompt_version": PROMPT_VERSION,
                    "batch_size": size,
                })
                records = cur.fetchall()
            if not records:
                reached_end = True
                break

            scanned = [_row_from_record(record) for record in records]
            with guarded(conn, statement_timeout) as cur:
                bodies, from_r2 = load_bodies(
                    cur, [payload.id for payload, _row, _psc in scanned], store=store)
            stats.bodies_from_r2 += from_r2

            # --- NO TRANSACTION OPEN from here until the write below. ---
            result = IntakeResult()
            processed = 0
            budget_hit = False
            for payload, row, psc in scanned:
                # Checked PER LISTING, not merely per batch: a batch is a bill, and a
                # 50-call overshoot past `--max-usd` is 50 calls nobody authorised. The
                # cursor below then advances only over the rows actually looked at, so
                # stopping mid-batch leaves the rest of it in the next run's window rather
                # than skipping it forever.
                if max_usd is not None and stats.spent_usd >= max_usd:
                    budget_hit = True
                    break
                if deadline is not None and time.monotonic() > deadline:
                    budget_hit = True
                    break
                if limit is not None and stats.payloads >= limit:
                    budget_hit = True
                    break
                stats.payloads += 1
                processed += 1
                body = bodies.get(payload.id)
                if body is None:
                    continue
                document = scope_html(body, register=register)
                blocks = block_texts(document, css_by_block)
                if dry_run:
                    LOG.debug("CLAIMS-LLM dry-run listing_id=%s blocks=%s",
                              row.listing_id, {k: len(v) for k, v in blocks.items()})
                    continue
                assert llm is not None
                try:
                    answer, cost = llm.answer(blocks)
                except Exception as exc:  # noqa: BLE001 - one listing must not kill the run
                    stats.errors += 1
                    consecutive_errors += 1
                    LOG.warning("CLAIMS-LLM listing_id=%s call failed: %s",
                                row.listing_id, exc)
                    if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                        raise IntakeRefused(
                            f"{consecutive_errors} consecutive model failures (provider "
                            f"outage?); the run is stamped failed rather than reporting a "
                            f"quiet success") from exc
                    continue
                consecutive_errors = 0
                stats.calls += 1
                stats.spent_usd += cost
                try:
                    listing_result = extract_payload(
                        payload, row, entries, answer, document=document,
                        model=resolved_model, prompt_version=PROMPT_VERSION,
                        gazetteer=gazetteer, psc=psc, stats=stats.obec)
                except IntakeRefused:
                    raise
                except Exception as exc:  # noqa: BLE001
                    stats.errors += 1
                    LOG.warning("CLAIMS-LLM listing_id=%s extraction failed: %s",
                                row.listing_id, exc)
                    continue
                # Appended per listing, contiguously — `write_result` chunks by row count
                # and bytes and never splits a listing.
                result.extend(listing_result)

            if processed:
                after_ts = records[processed - 1][5]
                after_id = int(records[processed - 1][0])
            stats.claims += len(result.claims)
            stats.absences += len(result.absences)
            stats.oversized_values += result.oversized
            if not dry_run and batch_id is not None:
                with guarded(conn, statement_timeout) as cur:
                    inserted, observed, enqueued = write_result(
                        cur, result, batch_id=batch_id, extractor_version=LLM_VERSION)
                stats.claims_inserted += inserted
                stats.observations += observed
                stats.enqueued += enqueued
            LOG.info("CLAIMS-LLM progress payloads=%d calls=%d claims=%d inserted=%d "
                     "absences=%d errors=%d spent=%.4f through_id=%d",
                     stats.payloads, stats.calls, stats.claims, stats.claims_inserted,
                     stats.absences, stats.errors, stats.spent_usd, after_id)
            if budget_hit:
                stopped_early = True
                break
    except Exception as exc:
        if batch_id is not None:
            try:
                with guarded(conn, _FAILURE_STAMP_TIMEOUT_S) as cur:
                    cur.execute(_BATCH_FINISH_SQL, {
                        "batch_id": batch_id, "outcome": "failed",
                        "row_count": stats.claims_inserted,
                        "cursor_after_id": after_id, "cursor_after_ts": after_ts,
                        "note": f"{type(exc).__name__}: {exc}"[:500],
                    })
            except Exception:  # noqa: BLE001 - never mask the exception being reported
                LOG.exception("CLAIMS-LLM could not stamp batch %s as failed", batch_id)
        raise

    outcome = "ok" if reached_end else "stopped"
    if batch_id is not None:
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_BATCH_FINISH_SQL, {
                "batch_id": batch_id, "outcome": outcome,
                "row_count": stats.claims_inserted,
                "cursor_after_id": after_id, "cursor_after_ts": after_ts,
                "note": f"payloads={stats.payloads} calls={stats.calls} "
                        f"spent_usd={stats.spent_usd:.4f} errors={stats.errors} "
                        f"stopped_early={stopped_early} reached_end={reached_end}",
            })
    return {
        "outcome": outcome, "mode": mode, "source": source, "model": resolved_model,
        "prompt_version": PROMPT_VERSION, "batch_id": batch_id,
        "reached_end": reached_end, "stopped_early": stopped_early,
        "cursor_after_id": after_id, **stats.as_dict(),
    }


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument("--source", choices=SOURCES, default=DEFAULT_SOURCE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--max-usd", type=float, default=None,
                        help="PRE-FLIGHT refusal AND an in-loop stop.")
    parser.add_argument("--model", default=None,
                        help=f"overrides app_settings.{MODEL_SETTING_KEY}")
    parser.add_argument("--overlap-hours", type=int, default=DEFAULT_OVERLAP_HOURS)
    parser.add_argument(
        "--statement-timeout", type=int,
        default=loader_db.env_timeout_s(STATEMENT_TIMEOUT_ENV, DEFAULT_STATEMENT_TIMEOUT_S))
    parser.add_argument("--lease-ttl-seconds", type=int, default=DEFAULT_LEASE_TTL_S)
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan, scope and build the prompt; call NOTHING, write NOTHING.")
    parser.add_argument("--fake-llm", default=None,
                        help="Read the tool answer from a JSON file instead of calling a "
                             "provider (local smoke-testing).")
    parser.add_argument("--note", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    batch_size = max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, args.batch_size))
    caller: LlmCaller | None = FileCaller(args.fake_llm) if args.fake_llm else None

    with db.connect() as conn:
        # Lease-row CAS, never an advisory lock: the transaction-mode pooler strands a lock
        # acquired on one backend and released on another. Setting
        # `location_jobs.enabled = false` for this job name is the operator kill switch.
        with lease.held(
            conn, JOB_NAME, cadence="1 hour", concurrency_group=CONCURRENCY_GROUP,
            ttl_seconds=args.lease_ttl_seconds,
        ) as acquired:
            if not acquired:
                LOG.info("CLAIMS-LLM skipped: another run holds the %s lease", JOB_NAME)
                return 0
            try:
                stats = run(
                    conn, mode=args.mode, source=args.source, batch_size=batch_size,
                    max_seconds=args.max_seconds, limit=args.limit, max_usd=args.max_usd,
                    model=args.model, overlap_hours=args.overlap_hours,
                    statement_timeout=args.statement_timeout, dry_run=args.dry_run,
                    note=args.note, llm=caller)
            except IntakeRefused as exc:
                print(f"REFUSED: {exc}", file=sys.stderr)
                return 2
    LOG.info("CLAIMS-LLM done %s", json.dumps(stats, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
