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
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from location_data import payloads
from location_data.claims_llm import (
    BLOCK_ORDER,
    FIELD_CLAIM_TYPES,
    FIELD_ORDER,
    LOCATION_TOOL,
    MAX_TOKENS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    ArchivedPayload,
    Refusal,
    block_texts,
    build_user_message,
    estimated_cost_usd,
    gazetteer_refusal,
    load_bodies,
    load_register,
    open_gazetteer,
    parse_field_answer,
)
from location_data.claims_intake import IntakeRefused, guarded
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

# The two bazos nodes one call reads. The production lane takes these from the CONTRACT
# (`locator.css` on each entry); the contract entries that declare them land with bazos@3,
# which has not shipped, so the harness names them here and this is the one place that has
# to be re-pointed at the contract once they exist. Deliberately the same selectors the
# lane's own fixture uses, so a green bake-off and a green unit test describe one page.
BAKEOFF_BLOCK_CSS: dict[str, str] = {
    "description": "div.popisdetail",
    "title": "h1.nadpisdetail",
}

# `listings.description` is a COHORT FILTER here and never reaches the prompt — the prompt
# is built from the scoped body alone (with stored columns in it the design measured 11
# high-confidence "stored echo" claims across 27 listings).
_SAMPLE_SQL = """
    SELECT l.id, l.source_id_native, p.id, encode(p.payload_sha256, 'hex'),
           p.first_observed_at, l.raw_json ->> 'psc'
    FROM listings l
    JOIN portal_raw_payloads p
      ON p.source = l.source AND p.source_id_native = l.source_id_native
    WHERE l.source = %(source)s
      AND l.is_active
      AND l.description IS NOT NULL
      AND length(l.description) >= %(min_chars)s
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
    lines = [
        f"## Location LLM bake-off — {report['listing_count']} bazos listings",
        "",
        f"seed `{report.get('seed')}` · prompt `{report.get('prompt_version')}` · "
        f"total ${report['cost']['total_usd']:.4f}",
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
    if report["errors"]:
        lines += ["", f"⚠️ {len(report['errors'])} failed calls — see the artefact."]
    return "\n".join(lines) + "\n"


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
    max_seconds: float, max_disagreements: int, dry_run: bool,
) -> dict[str, Any]:
    from api.llm_client import LLMClient, parse_tool_input_json

    estimate = sum(estimated_cost_usd(model, sample) for model in models)
    if estimate > max_usd:
        raise IntakeRefused(
            f"pre-flight estimate ${estimate:.2f} for {sample} listings x {len(models)} "
            f"models exceeds --max-usd ${max_usd:.2f}; lower --sample or raise the cap")

    register = load_register(conn, SOURCE)
    if register is None:
        raise IntakeRefused(
            f"no active portal_contracts row for {SOURCE}: run "
            f"`python -m location_data.contracts --load` first")
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
    calls: list[CallRecord] = []
    deadline = time.monotonic() + max_seconds
    scored_listings = 0

    # Listing-outer, model-inner: a budget stop then leaves a COMPLETE row set for every
    # listing processed, so the agreement matrix is never computed over a partial pair.
    for listing_id, native, payload_id, sha_hex, first_observed_at, psc in records:
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
        blocks = block_texts(document, BAKEOFF_BLOCK_CSS)
        nodes = {b: document.css_first(css) for b, css in BAKEOFF_BLOCK_CSS.items()}
        user = build_user_message(blocks)
        if dry_run:
            LOG.info("BAKEOFF dry-run listing_id=%s payload=%s chars=%d",
                     listing_id, payload.id, len(user))
            scored_listings += 1
            continue
        scored_listings += 1
        for model in models:
            started = time.monotonic()
            try:
                response = client.call(
                    called_for=CALLED_FOR,
                    messages=[{"role": "user", "content": user}],
                    system=SYSTEM_PROMPT, tools=[LOCATION_TOOL],
                    tool_choice=LOCATION_TOOL["name"], model=model,
                    max_tokens=MAX_TOKENS)
            except Exception as exc:  # noqa: BLE001 - one call must not kill the pass
                calls.append(CallRecord(
                    model=model, listing_id=int(listing_id),
                    source_id_native=str(native),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    input_tokens=0, output_tokens=0, cost_usd=0.0, error=str(exc)[:500]))
                LOG.warning("BAKEOFF %s listing_id=%s failed: %s", model, listing_id, exc)
                continue
            answer: dict[str, Any] = {}
            for call in response.tool_calls:
                if call.get("name") == LOCATION_TOOL["name"]:
                    answer = parse_tool_input_json(call.get("input"))
                    break
            calls.append(CallRecord(
                model=model, listing_id=int(listing_id), source_id_native=str(native),
                duration_ms=int(response.duration_ms or 0),
                input_tokens=int(response.input_tokens or 0),
                output_tokens=int(response.output_tokens or 0),
                cost_usd=float(response.cost_usd or 0.0),
                llm_call_id=response.llm_call_id))
            obec_kod = _candidate_obec_for_bakeoff(answer, gazetteer, psc)
            observations.extend(evaluate_answer(
                model=model, listing_id=int(listing_id), answer=answer,
                document=document, nodes=nodes, gazetteer=gazetteer,
                obec_kod=obec_kod))

    report = score(observations, calls, models, listing_count=scored_listings,
                   max_disagreements=max_disagreements)
    report.update({
        "seed": seed, "sample_requested": sample, "source": SOURCE,
        "prompt_version": PROMPT_VERSION, "registry_version": version_label,
        "bodies_from_r2": from_r2, "dry_run": dry_run,
        "sampled_at": datetime.now(UTC).isoformat(),
        "scope_version": register.scope_version,
    })
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
                max_disagreements=args.max_disagreements, dry_run=args.dry_run)
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
