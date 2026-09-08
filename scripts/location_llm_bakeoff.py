"""W2-10: the three-model bake-off for the bazos free-text location extractor.

READ-ONLY. It writes NO claims, NO absences and NO batch rows — the only rows it creates
anywhere are the `llm_calls` audit rows `LLMClient` writes for the calls it makes, stamped
`called_for='location_llm_bakeoff'` (migration 470) so its spend and its failures are
scored separately from the production lane's. `llm_burn_rate`'s starvation arm is evaluated
PER `called_for`, so a failed bake-off must not red the lane, or vice versa.

WHAT IT MEASURES. A deterministic, md5-seeded sample of active bazos listings; for each,
the body is loaded from the content-addressed payload store, scoped through the DEPLOYED
exclusion-zone register, and the IDENTICAL (system, user) pair is sent to every model. Per
model it reports: per-field yield, gazetteer-resolution rate, evidence-quote validity (the
EXACT production check — the quote must be locatable inside the node it claims to have been
read from), latency, tokens and cost; plus the pairwise inter-model agreement matrix and a
capped list of disagreements for the operator to adjudicate. Adjudication happens OUTSIDE
this repo; the script emits the comparison artefact and nothing else.

WHY NO RESULTS TABLE: a schema for a one-off comparison is schema forever. The JSON
artefact plus the `llm_calls` rows (the real cost ledger) are the durable record.

    python -m scripts.location_llm_bakeoff --sample 150 --seed w2-10 --max-usd 5

THE CONSTRAINED ARM (`--mode constrained|both`, operator order 2026-09-08). The free-form
head-to-head lost 26 of its 40 obec disagreements to Czech declension: a model that copies
"Karlových Varů" out of the prose names a town no registry lists. The constrained arm sends
the SAME title + description plus a closed list of candidate obce — every obec in the
okres(es) the listing's PSČ belongs to — and the model may only pick one of them, with a
verbatim quote, or abstain. A pick outside the list is INVALID and scored as such. `both`
runs the two arms on the same listings so each model's pick can be related to its own
free-form answer (`relation_of`), which is the only fair way to say whether the list helped.

    python -m scripts.location_llm_bakeoff --mode both --sample 100 --seed w2-10 \
        --models gpt-5.6-luna,qwen3.7-flash

Requires SUPABASE_DB_URL, OPENAI_API_KEY, QWEN_API_KEY and the four R2_* vars.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from location_data import payloads
from location_data.claims_llm import (
    BLOCK_ORDER,
    FIELD_CLAIM_TYPES,
    FIELD_ORDER,
    LOCATION_TOOL,
    MAX_TOKENS,
    PAGE_KIND,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    ArchivedPayload,
    Refusal,
    _validated_groups,
    block_texts,
    build_user_message,
    estimated_cost_usd,
    gazetteer_refusal,
    llm_entries,
    load_bodies,
    load_register,
    open_gazetteer,
    parse_field_answer,
)
from location_data.claims_intake import IntakeRefused, guarded, load_entries
from location_data.html_scope import scope_html
from location_data.name_index import normalize_name, normalize_street_name
from scraper import db

LOG = logging.getLogger("location_llm_bakeoff")

CALLED_FOR = "location_llm_bakeoff"
SOURCE = "bazos"
DEFAULT_MODELS = ("gpt-5-nano", "gpt-5.6-luna", "qwen3.7-flash")
DEFAULT_SAMPLE = 150
DEFAULT_SEED = "w2-10"
# A HARD pre-flight cap. `api.llm_client`'s daily-cost check only LOGS, and on a long run
# it notices after the money is gone.
DEFAULT_MAX_USD = 5.0
DEFAULT_MAX_SECONDS = 3000.0
DEFAULT_MAX_DISAGREEMENTS = 40
STATEMENT_TIMEOUT_S = 120
# Cohort filter only — a listing whose description is a one-liner tells the bake-off
# nothing about free-text extraction.
MIN_DESCRIPTION_CHARS = 120

# ------------------------------------------------------------------ the constrained arm

FREE_ARM = "free"
CONSTRAINED_ARM = "constrained"
MODES = (FREE_ARM, CONSTRAINED_ARM, "both")
CONSTRAINED_PROMPT_VERSION = "bzs.loc.pick@1"
# The largest okres holds ~175 obce and a PSČ can straddle two, so this never truncates a
# real list; it is a rail against a registry surprise, not a design number.
MAX_CANDIDATES = 400

# The candidate list is every obec in the okres(es) the listing's PSČ belongs to. bazos files
# an ad under a postal town (`locality_text` is "353 01 Cheb", never a district), so the PSČ
# is the one structured anchor the seller had to choose; the okres around it is wide enough
# to hold the village the prose actually names and narrow enough to stay well under a few
# hundred names. Version-pinned the same way `resolve_db._PSC_OBEC_SQL` is: current rows only.
_CANDIDATES_SQL = """
    WITH obce AS (
        SELECT DISTINCT obec_kod FROM ruian_address_points
         WHERE psc = %(psc)s AND valid_to IS NULL
    ), okresy AS (
        SELECT DISTINCT regexp_replace(u.path::text, '\\.b\\d+$', '') AS okres_path
          FROM ruian_admin_units u
          JOIN obce ON obce.obec_kod = u.code
         WHERE u.level::text = 'obec' AND u.valid_to IS NULL
    )
    SELECT DISTINCT o.name
      FROM ruian_admin_units o
      JOIN okresy k ON o.path::text LIKE k.okres_path || '.b%%'
     WHERE o.level::text = 'obec' AND o.valid_to IS NULL
     ORDER BY o.name
"""

CONSTRAINED_SYSTEM_PROMPT = """\
Jsi extraktor obce (města) z českých realitních inzerátů.

Dostaneš TITULEK a POPIS jednoho inzerátu a SEZNAM OBCÍ. Vyber ze seznamu tu obec, ve které
se nemovitost nachází — ale JEN pokud to z textu jednoznačně plyne.

Pravidla:
- `obec` vracej POUZE jako přesný název ze SEZNAMU, opsaný beze změny. Nikdy nevymýšlej
  název, který v seznamu není.
- Text obec často skloňuje ("v Kolíně", "u Chebu", "do Karlových Varů"); ty vrať tvar ze
  seznamu ("Kolín", "Cheb", "Karlovy Vary").
- Pokud text žádnou obec neuvádí, nebo uvádí obec, která v seznamu NENÍ, nebo si nejsi
  jistý, vrať obec=null.
- `quote` = DOSLOVNÝ, nezkrácený úsek textu (z titulku nebo popisu), ze kterého volba plyne;
  přesný podřetězec včetně diakritiky. Při obec=null vrať quote=null.
- `confidence` = "high" jen když text obec jmenuje výslovně; "medium" když plyne z části
  obce, ulice nebo orientačního bodu; jinak "low" (a obec=null).
"""

CHOICE_TOOL: dict[str, Any] = {
    "name": "pick_obec",
    "description": "Vyber obec ze SEZNAMU OBCÍ, pokud z inzerátu jednoznačně plyne.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["obec", "quote", "confidence"],
        "properties": {
            "obec": {"type": ["string", "null"]},
            "quote": {"type": ["string", "null"]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
    },
}

# How a constrained pick relates to the SAME model's free-form obec on the SAME listing.
# `free_unresolved_pick_valid` is the one the operator ordered this arm to find: the free
# form named a town the registry could not admit (declension, a district in the obec field)
# and the closed list turned it into a member.
RELATIONS = ("same", "free_unresolved_pick_valid", "different", "pick_only", "free_only",
             "neither")


def psc_digits(psc: str | None) -> str | None:
    """'186 00' -> '18600'; anything that is not five digits -> None."""
    digits = "".join(ch for ch in str(psc or "") if ch.isdigit())
    return digits if len(digits) == 5 else None


def candidate_obce(conn: Any, psc: str | None) -> list[str]:
    """The closed list for one listing; empty when the PSČ is missing or unknown."""
    digits = psc_digits(psc)
    if digits is None:
        return []
    with guarded(conn, STATEMENT_TIMEOUT_S) as cur:
        cur.execute(_CANDIDATES_SQL, {"psc": digits})
        names = [str(r[0]) for r in cur.fetchall()]
    return names[:MAX_CANDIDATES]


def build_constrained_message(blocks: dict[str, str], candidates: list[str]) -> str:
    """The free-arm prompt (same blocks, same stored-column ban) plus the closed list."""
    return (build_user_message(blocks)
            + "\nSEZNAM OBCÍ (vrať přesně jeden název z tohoto seznamu, nebo null):\n"
            + "\n".join(candidates) + "\n")


def block_css(conn: Any) -> dict[str, str]:
    """The bazos nodes one call reads, taken from the DEPLOYED contract.

    Not a constant any more: bazos@3 is the bump that declares them (`locator.css` on each
    `llm_location_text` entry), and a harness holding its own copy would, the first time a
    selector moved, measure a page the production lane does not read — while still reporting
    a yield. Read the same way `load_register` reads the exclusion zones: from the projection
    of the contract that is actually deployed, never re-parsed from the YAML on disk.
    `_validated_groups` is the lane's own refusal, applied here for free: one block is one
    node, and one call reads it once.
    """
    entries = load_entries(conn).get(SOURCE) or []
    readable = llm_entries(entries, PAGE_KIND)
    if not readable:
        raise IntakeRefused(
            f"no ACTIVE {SOURCE} contract entry names a reader from the LLM registry, so "
            f"there is no block to read; load the bazos@3 contract with "
            f"`python -m location_data.contracts --load`")
    css_by_block, _groups = _validated_groups(readable)
    return css_by_block


# `listings.description` is a COHORT FILTER here and never reaches the prompt — the prompt
# is built from the scoped body alone (with stored columns in it the design measured 11
# high-confidence "stored echo" claims across 27 listings).
#
# bazos files listings abroad under PSČ 987 66 "Zahraničí" (~3% of the active set); no Czech
# obec can be right for them, so they would only measure abstention. Excluded from the cohort.
_SAMPLE_SQL = """
    SELECT l.id, l.source_id_native, p.id, encode(p.payload_sha256, 'hex'),
           p.first_observed_at, l.raw_json ->> 'psc', l.source_url
    FROM listings l
    JOIN portal_raw_payloads p
      ON p.source = l.source AND p.source_id_native = l.source_id_native
    WHERE l.source = %(source)s
      AND l.is_active
      AND l.description IS NOT NULL
      AND length(l.description) >= %(min_chars)s
      AND coalesce(l.raw_json ->> 'locality_text', '') NOT LIKE '%%Zahraničí'
      AND p.page_kind = 'detail'
      AND (p.http_status IS NULL OR p.http_status BETWEEN 200 AND 299)
      AND NOT EXISTS (
          SELECT 1 FROM portal_raw_payloads n
          WHERE n.source = p.source
            AND n.source_id_native = p.source_id_native
            AND n.page_kind = p.page_kind
            AND (n.http_status IS NULL OR n.http_status BETWEEN 200 AND 299)
            AND (n.first_observed_at, n.id) > (p.first_observed_at, p.id))
    ORDER BY md5(l.source_id_native || %(seed)s)
    LIMIT %(sample)s
"""


# ------------------------------------------------------------------ observations

@dataclass(frozen=True, slots=True)
class FieldObservation:
    """One (model, listing, block, field) answer, already validated against the page."""
    model: str
    listing_id: int
    block: str
    field: str
    value: str | None
    quote: str | None
    confidence: str
    # The EXACT production check: the quote is locatable inside the node the answer claims
    # to have been read from. False on a stated value means a fabricated citation.
    quote_valid: bool
    # True/False when the field has a registry gate; None when it has none (landmark,
    # address_line_verbatim, psc) — "not checked" and "checked and failed" must not
    # collapse into one number.
    resolved: bool | None
    refusal: str | None = None


@dataclass(frozen=True, slots=True)
class CallRecord:
    model: str
    listing_id: int
    source_id_native: str
    duration_ms: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    llm_call_id: int | None = None
    error: str | None = None
    arm: str = FREE_ARM


def evaluate_answer(
    *, model: str, listing_id: int, answer: dict[str, Any], document: Any,
    nodes: dict[str, Any], gazetteer: Any, obec_kod: int | None,
) -> list[FieldObservation]:
    """One model's answer for one listing, scored field by field against the page.

    Pure apart from the gazetteer lookups, and it uses the LANE's own helpers
    (`parse_field_answer`, `document.find_span`, `gazetteer_refusal`) rather than a second
    implementation — a bake-off scored by a looser validator than production would pick the
    model that is best at fooling the looser validator.
    """
    observations: list[FieldObservation] = []
    street_norm: str | None = None
    for field in FIELD_ORDER:
        for block in BLOCK_ORDER:
            parsed = parse_field_answer(answer, block, field)
            if isinstance(parsed, Refusal):
                observations.append(FieldObservation(
                    model=model, listing_id=listing_id, block=block, field=field,
                    value=None, quote=None, confidence="low", quote_valid=False,
                    resolved=None, refusal=parsed.detail))
                continue
            if parsed.value is None:
                observations.append(FieldObservation(
                    model=model, listing_id=listing_id, block=block, field=field,
                    value=None, quote=parsed.quote, confidence=parsed.confidence,
                    quote_valid=False, resolved=None))
                continue
            node = nodes.get(block)
            quote = parsed.quote or parsed.value
            quote_valid = node is not None and document.find_span(
                quote, within=node) is not None
            claim_type = FIELD_CLAIM_TYPES[field][0]
            gate = gazetteer_refusal(
                claim_type, parsed.value, gazetteer=gazetteer, obec_kod=obec_kod,
                street_norm=street_norm)
            gated = claim_type in (
                "obec_name", "cast_obce_name", "street_name",
                "house_number_cp", "house_number_co")
            resolved = None if not gated else gate is None
            if field == "street" and block == "description" and resolved:
                street_norm = normalize_street_name(parsed.value)
            observations.append(FieldObservation(
                model=model, listing_id=listing_id, block=block, field=field,
                value=parsed.value, quote=parsed.quote, confidence=parsed.confidence,
                quote_valid=quote_valid, resolved=resolved,
                refusal=None if gate is None else gate.detail))
    return observations


@dataclass(frozen=True, slots=True)
class ChoiceObservation:
    """One (model, listing) pick from the closed list, validated against list and page."""
    model: str
    listing_id: int
    candidates: int
    obec: str | None
    # True when the pick is a list member (through the name normaliser, so a case or
    # diacritic slip is not an "invention"); False when the model named an obec outside the
    # list; None when it abstained.
    valid: bool | None
    quote: str | None
    quote_valid: bool
    confidence: str
    # `relation_of` against the SAME model's free-form obec, when the free arm ran.
    relation: str | None = None
    free_obec: str | None = None


def evaluate_choice(
    *, model: str, listing_id: int, answer: dict[str, Any], candidates: list[str],
    document: Any, nodes: dict[str, Any],
) -> ChoiceObservation:
    """Pure apart from the DOM span check — the same production quote test the free arm uses."""
    raw = answer.get("obec")
    obec = raw.strip() if isinstance(raw, str) and raw.strip() else None
    quote = answer.get("quote") if isinstance(answer.get("quote"), str) else None
    confidence = answer.get("confidence")
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    valid: bool | None = None
    if obec is not None:
        wanted = normalize_name(obec)
        valid = any(normalize_name(candidate) == wanted for candidate in candidates)
    quote_valid = bool(obec) and bool(quote) and any(
        node is not None and document.find_span(quote, within=node) is not None
        for node in nodes.values())
    return ChoiceObservation(
        model=model, listing_id=listing_id, candidates=len(candidates), obec=obec,
        valid=valid, quote=quote, quote_valid=quote_valid, confidence=confidence)


def relation_of(
    pick: str | None, pick_valid: bool | None, free: str | None, free_resolved: bool | None,
) -> str:
    if pick is None and free is None:
        return "neither"
    if pick is None:
        return "free_only"
    if free is None:
        return "pick_only"
    if normalize_name(pick) == normalize_name(free):
        return "same"
    if free_resolved is False and pick_valid:
        return "free_unresolved_pick_valid"
    return "different"


def _free_obec(observations: list[FieldObservation]) -> tuple[str | None, bool | None]:
    """The description-first obec one model stated in the free arm, with its registry verdict."""
    for block in BLOCK_ORDER:
        for obs in observations:
            if obs.block == block and obs.field == "obec" and obs.value is not None:
                return obs.value, obs.resolved
    return None, None


# ------------------------------------------------------------------ scoring

def _collapsed(observations: list[FieldObservation]) -> dict[tuple[str, int, str], str]:
    """{(model, listing, field) -> value}, DESCRIPTION-FIRST — the lane's own rule, so the
    agreement matrix compares what would actually have been claimed."""
    out: dict[tuple[str, int, str], str] = {}
    for block in BLOCK_ORDER:
        for obs in observations:
            if obs.block != block or obs.value is None:
                continue
            out.setdefault((obs.model, obs.listing_id, obs.field), obs.value)
    return out


def compare_values(field: str, left: str, right: str) -> bool:
    """Do two models mean the same thing? Street names normalise through the street
    normaliser (which drops a leading 'ulice'), house numbers compare as digits, everything
    else through the plain name normaliser."""
    if field == "house_number":
        return ("".join(c for c in left if c.isdigit())
                == "".join(c for c in right if c.isdigit()))
    if field == "street":
        return normalize_street_name(left) == normalize_street_name(right)
    return normalize_name(left) == normalize_name(right)


def score(
    observations: list[FieldObservation], calls: list[CallRecord], models: list[str],
    *, listing_count: int, max_disagreements: int = DEFAULT_MAX_DISAGREEMENTS,
) -> dict[str, Any]:
    """The whole report, from the observations and the call ledger. Pure."""
    per_model: dict[str, Any] = {}
    for model in models:
        mine = [o for o in observations if o.model == model]
        stated = [o for o in mine if o.value is not None]
        gated = [o for o in stated if o.resolved is not None]
        fields: dict[str, Any] = {}
        for field in FIELD_ORDER:
            by_block = {}
            for block in BLOCK_ORDER:
                block_stated = [o for o in stated if o.field == field and o.block == block]
                by_block[block] = {
                    "stated": len(block_stated),
                    "yield": _ratio(len(block_stated), listing_count),
                }
            field_stated = [o for o in stated if o.field == field]
            field_gated = [o for o in field_stated if o.resolved is not None]
            fields[field] = {
                "stated": len(field_stated),
                "yield": _ratio(len(field_stated), listing_count),
                "quote_valid": _ratio(
                    sum(1 for o in field_stated if o.quote_valid), len(field_stated)),
                "gazetteer_resolved": _ratio(
                    sum(1 for o in field_gated if o.resolved), len(field_gated)),
                "by_block": by_block,
            }
        model_calls = [c for c in calls if c.model == model]
        ok_calls = [c for c in model_calls if c.error is None]
        durations = sorted(c.duration_ms for c in ok_calls)
        total_cost = round(sum(c.cost_usd for c in ok_calls), 6)
        per_model[model] = {
            "calls": len(model_calls),
            "errors": sum(1 for c in model_calls if c.error is not None),
            "values_stated": len(stated),
            "quote_valid_rate": _ratio(
                sum(1 for o in stated if o.quote_valid), len(stated)),
            "quote_missing_rate": _ratio(
                sum(1 for o in stated if not o.quote), len(stated)),
            "gazetteer_resolved_rate": _ratio(
                sum(1 for o in gated if o.resolved), len(gated)),
            "latency_ms_p50": _percentile(durations, 0.50),
            "latency_ms_p95": _percentile(durations, 0.95),
            "input_tokens_mean": _mean([c.input_tokens for c in ok_calls]),
            "output_tokens_mean": _mean([c.output_tokens for c in ok_calls]),
            "cost_usd_total": total_cost,
            "cost_usd_per_call": _ratio_f(total_cost, len(ok_calls)),
            # A model whose whole run cost exactly nothing has NO `PRICES` row, and every
            # downstream spend signal (llm_burn_rate's 24h total, llm_cost_today_usd, the
            # lane's own --max-usd) is then lying about it.
            "unpriced": bool(ok_calls) and total_cost == 0.0,
            "fields": fields,
        }

    collapsed = _collapsed(observations)
    agreement: dict[str, Any] = {}
    disagreements: list[dict[str, Any]] = []
    for i, left in enumerate(models):
        for right in models[i + 1:]:
            pair_key = f"{left}|{right}"
            per_field: dict[str, Any] = {}
            agreed_all = comparable_all = 0
            for field in FIELD_ORDER:
                agreed = comparable = 0
                for listing_id in sorted({o.listing_id for o in observations}):
                    a = collapsed.get((left, listing_id, field))
                    b = collapsed.get((right, listing_id, field))
                    if a is None or b is None:
                        continue
                    comparable += 1
                    if compare_values(field, a, b):
                        agreed += 1
                    elif len(disagreements) < max_disagreements:
                        disagreements.append({
                            "listing_id": listing_id, "field": field,
                            left: a, right: b,
                        })
                per_field[field] = {
                    "comparable": comparable, "agreed": agreed,
                    "rate": _ratio(agreed, comparable),
                }
                agreed_all += agreed
                comparable_all += comparable
            per_field["_all"] = {
                "comparable": comparable_all, "agreed": agreed_all,
                "rate": _ratio(agreed_all, comparable_all),
            }
            agreement[pair_key] = per_field

    return {
        "listing_count": listing_count,
        "models": list(models),
        "per_model": per_model,
        "agreement": agreement,
        "disagreements": disagreements,
        "errors": [asdict(c) for c in calls if c.error is not None],
        "cost": {
            "total_usd": round(sum(c.cost_usd for c in calls), 6),
            "per_model_usd": {
                m: round(sum(c.cost_usd for c in calls if c.model == m), 6)
                for m in models
            },
        },
    }


def score_constrained(
    choices: list[ChoiceObservation], calls: list[CallRecord], models: list[str], *,
    listing_count: int, listings_without_candidates: int, titles: dict[int, str],
    urls: dict[int, str], max_disagreements: int = DEFAULT_MAX_DISAGREEMENTS,
) -> dict[str, Any]:
    """The constrained arm's section of the report. Pure."""
    per_model: dict[str, Any] = {}
    for model in models:
        mine = [c for c in choices if c.model == model]
        picked = [c for c in mine if c.obec is not None]
        valid = [c for c in picked if c.valid]
        model_calls = [c for c in calls if c.model == model]
        ok_calls = [c for c in model_calls if c.error is None]
        durations = sorted(c.duration_ms for c in ok_calls)
        total_cost = round(sum(c.cost_usd for c in ok_calls), 6)
        related = [c for c in mine if c.relation is not None]
        per_model[model] = {
            "calls": len(model_calls),
            "errors": sum(1 for c in model_calls if c.error is not None),
            "scored": len(mine),
            "picked": len(picked),
            "pick_rate": _ratio(len(picked), len(mine)),
            "valid_picks": len(valid),
            "valid_rate": _ratio(len(valid), len(picked)),
            "invalid_picks": len(picked) - len(valid),
            "abstained": len(mine) - len(picked),
            "quote_valid_rate": _ratio(sum(1 for c in picked if c.quote_valid), len(picked)),
            "confidence": {level: sum(1 for c in picked if c.confidence == level)
                           for level in ("high", "medium", "low")},
            "relations": (None if not related else
                          {r: sum(1 for c in related if c.relation == r) for r in RELATIONS}),
            "candidates_mean": _mean([c.candidates for c in mine]),
            "latency_ms_p50": _percentile(durations, 0.50),
            "latency_ms_p95": _percentile(durations, 0.95),
            "input_tokens_mean": _mean([c.input_tokens for c in ok_calls]),
            "output_tokens_mean": _mean([c.output_tokens for c in ok_calls]),
            "cost_usd_total": total_cost,
            "cost_usd_per_call": _ratio_f(total_cost, len(ok_calls)),
            "unpriced": bool(ok_calls) and total_cost == 0.0,
        }

    by_key = {(c.model, c.listing_id): c for c in choices}
    agreement: dict[str, Any] = {}
    disagreements: list[dict[str, Any]] = []
    listing_ids = sorted({c.listing_id for c in choices})
    for i, left in enumerate(models):
        for right in models[i + 1:]:
            both = agreed = one_sided = both_abstained = 0
            for listing_id in listing_ids:
                a = by_key.get((left, listing_id))
                b = by_key.get((right, listing_id))
                if a is None or b is None:
                    continue
                if a.obec is None and b.obec is None:
                    both_abstained += 1
                    continue
                if a.obec is not None and b.obec is not None:
                    both += 1
                    if normalize_name(a.obec) == normalize_name(b.obec):
                        agreed += 1
                        continue
                else:
                    one_sided += 1
                if len(disagreements) < max_disagreements:
                    disagreements.append({
                        "listing_id": listing_id,
                        "url": urls.get(listing_id),
                        "title": titles.get(listing_id),
                        left: a.obec, f"{left}_confidence": a.confidence,
                        f"{left}_free": a.free_obec,
                        right: b.obec, f"{right}_confidence": b.confidence,
                        f"{right}_free": b.free_obec,
                    })
            agreement[f"{left}|{right}"] = {
                "both_picked": both, "agreed": agreed, "rate": _ratio(agreed, both),
                "one_sided": one_sided, "both_abstained": both_abstained,
            }

    return {
        "prompt_version": CONSTRAINED_PROMPT_VERSION,
        "listing_count": listing_count,
        "listings_without_candidates": listings_without_candidates,
        "per_model": per_model,
        "agreement": agreement,
        "disagreements": disagreements,
        "errors": [asdict(c) for c in calls if c.error is not None],
        "cost": {
            "total_usd": round(sum(c.cost_usd for c in calls), 6),
            "per_model_usd": {
                m: round(sum(c.cost_usd for c in calls if c.model == m), 6) for m in models
            },
        },
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 4)


def _ratio_f(numerator: float, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 6)


def _mean(values: list[int]) -> float | None:
    return None if not values else round(statistics.fmean(values), 1)


def _percentile(sorted_values: list[int], q: float) -> int | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, int(round(q * (len(sorted_values) - 1))))
    return sorted_values[index]


def summary_markdown(report: dict[str, Any]) -> str:
    """A compact table for `$GITHUB_STEP_SUMMARY`. The JSON artefact is the record; this
    is the thing a human reads without downloading it."""
    constrained = report.get("constrained")
    mode = report.get("mode", FREE_ARM)
    count = max(report["listing_count"], (constrained or {}).get("listing_count", 0))
    total = report.get("cost_all_arms_usd", report["cost"]["total_usd"])
    lines = [
        f"## Location LLM bake-off — {count} bazos listings",
        "",
        f"seed `{report.get('seed')}` · mode `{mode}` · prompt "
        f"`{report.get('prompt_version')}` · total ${total:.4f}",
    ]
    if mode != CONSTRAINED_ARM:
        lines += [
            "",
            "| model | values | quote valid | gazetteer | p50 ms | p95 ms | $ total | $/call |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for model in report["models"]:
            m = report["per_model"][model]
            flag = " ⚠️ UNPRICED" if m["unpriced"] else ""
            per_call = m["cost_usd_per_call"]
            per_call_cell = "—" if per_call is None else f"${per_call:.6f}"
            lines.append(
                f"| `{model}`{flag} | {m['values_stated']} | {_pct(m['quote_valid_rate'])} | "
                f"{_pct(m['gazetteer_resolved_rate'])} | {m['latency_ms_p50']} | "
                f"{m['latency_ms_p95']} | ${m['cost_usd_total']:.4f} | {per_call_cell} |")
        lines += ["", "### Per-field yield (description-first)", "",
                  "| field | " + " | ".join(f"`{m}`" for m in report["models"]) + " |",
                  "| --- | " + " | ".join("---" for _ in report["models"]) + " |"]
        for field in FIELD_ORDER:
            cells = []
            for model in report["models"]:
                f = report["per_model"][model]["fields"][field]
                cells.append(f"{f['stated']} ({_pct(f['yield'])})")
            lines.append(f"| {field} | " + " | ".join(cells) + " |")
        lines += ["", "### Pairwise agreement", "", "| pair | all fields | street | obec |",
                  "| --- | --- | --- | --- |"]
        for pair, per_field in report["agreement"].items():
            lines.append(
                f"| {pair} | {_pct(per_field['_all']['rate'])} "
                f"({per_field['_all']['comparable']}) | "
                f"{_pct(per_field['street']['rate'])} | "
                f"{_pct(per_field['obec']['rate'])} |")
        if report["disagreements"]:
            lines += ["", f"{len(report['disagreements'])} disagreements listed in the JSON "
                          "artefact for adjudication."]
    if constrained:
        lines += _constrained_markdown(constrained, report["models"])
    if report["errors"]:
        lines += ["", f"⚠️ {len(report['errors'])} failed calls — see the artefact."]
    return "\n".join(lines) + "\n"


def _constrained_markdown(section: dict[str, Any], models: list[str]) -> list[str]:
    lines = [
        "",
        "### Constrained arm — pick ONE obec from the PSČ-okres list "
        f"(`{section['prompt_version']}`)",
        "",
        f"{section['listing_count']} listings · {section['listings_without_candidates']} had "
        "no candidate list (PSČ missing or unknown to the registry) and skipped this arm",
        "",
        "| model | picked | valid | invalid | abstained | quote valid | high / medium | "
        "cands (mean) | p50 ms | $/call |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for model in models:
        m = section["per_model"][model]
        flag = " ⚠️ UNPRICED" if m["unpriced"] else ""
        per_call = m["cost_usd_per_call"]
        per_call_cell = "—" if per_call is None else f"${per_call:.6f}"
        lines.append(
            f"| `{model}`{flag} | {m['picked']}/{m['scored']} ({_pct(m['pick_rate'])}) | "
            f"{m['valid_picks']} ({_pct(m['valid_rate'])}) | {m['invalid_picks']} | "
            f"{m['abstained']} | {_pct(m['quote_valid_rate'])} | "
            f"{m['confidence']['high']} / {m['confidence']['medium']} | "
            f"{m['candidates_mean']} | {m['latency_ms_p50']} | {per_call_cell} |")
    if any(section["per_model"][m]["relations"] for m in models):
        lines += ["", "### Same model, same listing: constrained pick vs free-form obec", "",
                  "| model | " + " | ".join(RELATIONS) + " |",
                  "| --- | " + " | ".join("---" for _ in RELATIONS) + " |"]
        for model in models:
            relations = section["per_model"][model]["relations"] or {}
            lines.append(f"| `{model}` | "
                         + " | ".join(str(relations.get(r, 0)) for r in RELATIONS) + " |")
    lines += ["", "| pair | both picked | agreed | one-sided | both abstained |",
              "| --- | --- | --- | --- | --- |"]
    for pair, a in section["agreement"].items():
        lines.append(f"| {pair} | {a['both_picked']} | {a['agreed']} ({_pct(a['rate'])}) | "
                     f"{a['one_sided']} | {a['both_abstained']} |")
    if section["disagreements"]:
        lines += ["", f"{len(section['disagreements'])} constrained disagreements (with "
                      "links) listed in the JSON artefact for adjudication."]
    if section["errors"]:
        lines += ["", f"⚠️ {len(section['errors'])} failed constrained calls — see the "
                      "artefact."]
    return lines


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


# ------------------------------------------------------------------ the run

def _providers() -> dict[str, Any]:
    """Listed EXPLICITLY: no cron script in this repo uses `get_providers()`, and a model
    whose provider is unregistered raises in `LLMClient.call` BEFORE the try/except that
    writes the failure row — leaving zero `llm_calls` evidence and going invisible to every
    health check."""
    from api.providers.openai import OpenAIProvider
    from api.providers.qwen import QwenProvider
    return {"openai": OpenAIProvider(), "qwen": QwenProvider()}


def run(
    conn: Any, *, models: list[str], sample: int, seed: str, max_usd: float,
    max_seconds: float, max_disagreements: int, dry_run: bool, mode: str = FREE_ARM,
) -> dict[str, Any]:
    from api.llm_client import LLMClient, parse_tool_input_json

    if mode not in MODES:
        raise IntakeRefused(f"--mode must be one of {MODES}, got {mode!r}")
    run_free = mode in (FREE_ARM, "both")
    run_constrained = mode in (CONSTRAINED_ARM, "both")
    arms = int(run_free) + int(run_constrained)
    estimate = arms * sum(estimated_cost_usd(model, sample) for model in models)
    if estimate > max_usd:
        raise IntakeRefused(
            f"pre-flight estimate ${estimate:.2f} for {sample} listings x {len(models)} "
            f"models exceeds --max-usd ${max_usd:.2f}; lower --sample or raise the cap")

    register = load_register(conn, SOURCE)
    if register is None:
        raise IntakeRefused(
            f"no active portal_contracts row for {SOURCE}: run "
            f"`python -m location_data.contracts --load` first")
    blocks_css = block_css(conn)
    gazetteer, _version_id, version_label = open_gazetteer(conn)
    store = payloads.open_store()

    with guarded(conn, STATEMENT_TIMEOUT_S) as cur:
        cur.execute(_SAMPLE_SQL, {
            "source": SOURCE, "min_chars": MIN_DESCRIPTION_CHARS, "seed": seed,
            "sample": sample,
        })
        records = cur.fetchall()
    if not records:
        raise IntakeRefused(
            "the seeded sample is empty; check that bazos listings with descriptions and "
            "archived detail bodies exist")

    with guarded(conn, STATEMENT_TIMEOUT_S) as cur:
        bodies, from_r2 = load_bodies(cur, [int(r[2]) for r in records], store=store)

    client = None if dry_run else LLMClient(conn, providers=_providers())
    observations: list[FieldObservation] = []
    choices: list[ChoiceObservation] = []
    calls: list[CallRecord] = []
    titles: dict[int, str] = {}
    urls: dict[int, str] = {}
    deadline = time.monotonic() + max_seconds
    scored_listings = 0
    without_candidates = 0

    def _call(model: str, *, listing_id: int, native: str, arm: str, system: str,
              user: str, tool: dict[str, Any]) -> dict[str, Any] | None:
        """One forced-tool call; None when it failed (the failure is still a CallRecord)."""
        started = time.monotonic()
        try:
            response = client.call(
                called_for=CALLED_FOR, messages=[{"role": "user", "content": user}],
                system=system, tools=[tool], tool_choice=tool["name"], model=model,
                max_tokens=MAX_TOKENS)
        except Exception as exc:  # noqa: BLE001 - one call must not kill the pass
            calls.append(CallRecord(
                model=model, listing_id=listing_id, source_id_native=native,
                duration_ms=int((time.monotonic() - started) * 1000),
                input_tokens=0, output_tokens=0, cost_usd=0.0, error=str(exc)[:500],
                arm=arm))
            LOG.warning("BAKEOFF %s/%s listing_id=%s failed: %s", model, arm, listing_id, exc)
            return None
        answer: dict[str, Any] = {}
        for call in response.tool_calls:
            if call.get("name") == tool["name"]:
                answer = parse_tool_input_json(call.get("input"))
                break
        calls.append(CallRecord(
            model=model, listing_id=listing_id, source_id_native=native,
            duration_ms=int(response.duration_ms or 0),
            input_tokens=int(response.input_tokens or 0),
            output_tokens=int(response.output_tokens or 0),
            cost_usd=float(response.cost_usd or 0.0),
            llm_call_id=response.llm_call_id, arm=arm))
        return answer

    # Listing-outer, model-inner: a budget stop then leaves a COMPLETE row set for every
    # listing processed, so the agreement matrix is never computed over a partial pair.
    for listing_id, native, payload_id, sha_hex, first_observed_at, psc, url in records:
        if time.monotonic() > deadline:
            LOG.info("BAKEOFF stopping: --max-seconds reached after %d listings",
                     scored_listings)
            break
        body = bodies.get(int(payload_id))
        if body is None:
            continue
        payload = ArchivedPayload(
            id=int(payload_id), source=SOURCE, source_id_native=str(native),
            page_kind="detail", payload_sha256=str(sha_hex),
            first_observed_at=first_observed_at)
        document = scope_html(body, register=register)
        if not document.is_complete:
            LOG.warning("BAKEOFF listing_id=%s scoping incomplete; skipped", listing_id)
            continue
        blocks = block_texts(document, blocks_css)
        nodes = {b: document.css_first(css) for b, css in blocks_css.items()}
        user = build_user_message(blocks)
        candidates = candidate_obce(conn, psc) if run_constrained else []
        if run_constrained and not candidates:
            without_candidates += 1
        if dry_run:
            LOG.info("BAKEOFF dry-run listing_id=%s payload=%s chars=%d candidates=%d",
                     listing_id, payload.id, len(user), len(candidates))
            scored_listings += 1
            continue
        scored_listings += 1
        lid = int(listing_id)
        titles[lid] = (blocks.get("title") or "")[:120]
        urls[lid] = str(url or "")
        for model in models:
            free_obec: str | None = None
            free_resolved: bool | None = None
            if run_free:
                answer = _call(model, listing_id=lid, native=str(native), arm=FREE_ARM,
                               system=SYSTEM_PROMPT, user=user, tool=LOCATION_TOOL)
                if answer is not None:
                    obec_kod = _candidate_obec_for_bakeoff(answer, gazetteer, psc)
                    scored = evaluate_answer(
                        model=model, listing_id=lid, answer=answer, document=document,
                        nodes=nodes, gazetteer=gazetteer, obec_kod=obec_kod)
                    observations.extend(scored)
                    free_obec, free_resolved = _free_obec(scored)
            if run_constrained and candidates:
                answer = _call(model, listing_id=lid, native=str(native),
                               arm=CONSTRAINED_ARM, system=CONSTRAINED_SYSTEM_PROMPT,
                               user=build_constrained_message(blocks, candidates),
                               tool=CHOICE_TOOL)
                if answer is not None:
                    choice = evaluate_choice(
                        model=model, listing_id=lid, answer=answer,
                        candidates=candidates, document=document, nodes=nodes)
                    if run_free:
                        choice = replace(
                            choice, free_obec=free_obec,
                            relation=relation_of(choice.obec, choice.valid, free_obec,
                                                 free_resolved))
                    choices.append(choice)

    report = score(observations, [c for c in calls if c.arm == FREE_ARM], models,
                   listing_count=scored_listings if run_free else 0,
                   max_disagreements=max_disagreements)
    report.update({
        "seed": seed, "sample_requested": sample, "source": SOURCE, "mode": mode,
        "prompt_version": PROMPT_VERSION, "registry_version": version_label,
        "bodies_from_r2": from_r2, "dry_run": dry_run, "foreign_excluded": True,
        "sampled_at": datetime.now(UTC).isoformat(),
        "scope_version": register.scope_version,
        "cost_all_arms_usd": round(sum(c.cost_usd for c in calls), 6),
    })
    if run_constrained:
        report["constrained"] = score_constrained(
            choices, [c for c in calls if c.arm == CONSTRAINED_ARM], models,
            listing_count=scored_listings, listings_without_candidates=without_candidates,
            titles=titles, urls=urls, max_disagreements=max_disagreements)
    return report


def _candidate_obec_for_bakeoff(
    answer: dict[str, Any], gazetteer: Any, psc: str | None,
) -> int | None:
    """The lane's own candidate-obec ladder, re-used so the street and house-number gates
    are scored under the same constraint production would apply."""
    from location_data.claims_llm import _candidate_obec

    obec_kod, _rung = _candidate_obec(answer, gazetteer=gazetteer, psc=psc)
    return obec_kod


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD,
                        help="PRE-FLIGHT refusal: the run does not start above it.")
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    parser.add_argument("--max-disagreements", type=int,
                        default=DEFAULT_MAX_DISAGREEMENTS)
    parser.add_argument("--out", default=None,
                        help="JSON artefact path (default bakeoff-<seed>-<date>.json)")
    parser.add_argument("--summary-md", default="bakeoff-summary.md")
    parser.add_argument("--dry-run", action="store_true",
                        help="Sample, load and scope; call NOTHING.")
    parser.add_argument("--mode", choices=MODES, default=FREE_ARM,
                        help="free = the W2-10 extractor; constrained = pick ONE obec from "
                             "the PSČ-okres list; both = the two arms on the same listings.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        print("ERROR: --models is empty.", file=sys.stderr)
        return 2

    out = args.out or (
        f"bakeoff-{args.seed}-{datetime.now(UTC).date().isoformat()}.json")
    with db.connect() as conn:
        try:
            report = run(
                conn, models=models, sample=args.sample, seed=args.seed,
                max_usd=args.max_usd, max_seconds=args.max_seconds,
                max_disagreements=args.max_disagreements, dry_run=args.dry_run,
                mode=args.mode)
        except IntakeRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2

    body = json.dumps(report, indent=2, ensure_ascii=False, default=str, sort_keys=True)
    print(body)
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(body + "\n")
    with open(args.summary_md, "w", encoding="utf-8") as handle:
        handle.write(summary_markdown(report))
    LOG.info("BAKEOFF wrote %s and %s", out, args.summary_md)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
