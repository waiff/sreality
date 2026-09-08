"""W2-10 candidates probe — which candidate-list source holds the town an ad names?

READ-ONLY, borrowing the bake-off's sampling, scoping and scoring wholesale (it writes
only the `llm_calls` audit rows, under the bake-off's own `called_for`). It exists because
the bake-off artifact carries aggregates, which left two operator questions bounded rather
than answered (2026-09-08):

  1. How often is the town the ad names OUTSIDE the PSČ-okres list — the list the
     constrained arm was measured on, and the one source only bazos can supply?
  2. How often is it outside R km of the portal's PIN — a source every portal has, even
     when the pin is only a postcode centroid?
  3. How long does a TEXT-MATCHED list (registry names found in the ad, declension-tolerant)
     come out, and does it hold the town?

REFERENCE = the model's pick from the NATIONAL list (every current obec name, ~5.3k names,
~20k tokens): a registry name chosen from the text under NO geographic anchor, so it can be
tested for membership in every anchored list without circularity. It is a reference, not
ground truth — every row ships with the ad's link, and the operator adjudicates.

Three arms per ad, same model, same prompt as the constrained bake-off arm:
`national` (the reference), `psc` (the tested design), `union` (the proposed design:
text-match ∪ pin radius). PER-AD rows go into the artifact — the thing the bake-off lacks.

    python -m scripts.location_llm_candidates_probe --sample 100 --seed w2-10

Requires SUPABASE_DB_URL, OPENAI_API_KEY (QWEN_API_KEY only for a qwen model) and the four
R2_* vars.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from location_data import payloads
from location_data.claims_intake import IntakeRefused, guarded
from location_data.claims_llm import MAX_TOKENS, block_texts, load_bodies, load_register
from location_data.html_scope import scope_html
from location_data.name_index import normalize_name
from location_data.resolver import resolve_db
from scraper import db
from scripts.location_llm_bakeoff import (
    CALLED_FOR,
    CHOICE_TOOL,
    CONSTRAINED_SYSTEM_PROMPT,
    MAX_CANDIDATES,
    MIN_DESCRIPTION_CHARS,
    SOURCE,
    STATEMENT_TIMEOUT_S,
    CallRecord,
    _mean,
    _pct,
    _percentile,
    _providers,
    _ratio,
    _ratio_f,
    _SAMPLE_SQL,
    block_css,
    build_constrained_message,
    candidate_obce,
    estimated_cost_usd,
    evaluate_choice,
)

LOG = logging.getLogger("location_llm_candidates_probe")

DEFAULT_MODELS = ("gpt-5.6-luna",)
DEFAULT_SAMPLE = 100
DEFAULT_SEED = "w2-10"
DEFAULT_MAX_USD = 3.0
DEFAULT_MAX_SECONDS = 3000.0
DEFAULT_RADIUS_KM = 25.0
DEFAULT_MAX_LISTED = 40
ARMS = ("national", "psc", "union")
# The reference is tested against the pin at these distances; the pin LIST (the union's
# second half) is built at `--radius-km`. The query reaches to MAX_DISTANCE_KM — a reference
# farther than that from the pin reads as "beyond reach", which is the finding.
DISTANCE_TIERS_KM = (15.0, 25.0, 40.0)
# Equal to the widest tier: every extra kilometre of reach widens the `&&` box that bounds
# the per-ad geometry scan, and the first run paid 24 s per ad for a 60 km reach.
MAX_DISTANCE_KM = 40.0

# The bake-off's cohort, plus the pin. Derived from `_SAMPLE_SQL` rather than copied so the
# two harnesses cannot drift apart on who is in the sample. `listings.geom` is a GEOGRAPHY
# column; ST_X/ST_Y exist only for geometry, hence the cast.
_PROBE_SAMPLE_SQL = _SAMPLE_SQL.replace(
    "l.raw_json ->> 'psc', l.source_url",
    "l.raw_json ->> 'psc', l.source_url, ST_Y(l.geom::geometry), ST_X(l.geom::geometry)")
if _PROBE_SAMPLE_SQL == _SAMPLE_SQL:
    raise RuntimeError("the bake-off sample SQL moved; re-anchor the probe's pin columns")

_OBEC_UNITS_SQL = """
    SELECT id, name FROM ruian_admin_units
     WHERE level::text = 'obec' AND valid_to IS NULL
"""

# `resolve_db._NEAREST_OBEC_BRANCH`'s shape: the `&&` box is the Index Cond on the partial
# `pip` GiST index (a geography `ST_DWithin` alone reads every boundary row — 6.7 s/point
# measured there), 60,000 is the deliberate under-estimate of metres per degree so the box
# contains the circle, and the MIN over the subdivided `pip` pieces is the distance to the
# polygon — zero when the pin is inside the obec. NO JOIN on purpose: with `ruian_admin_units`
# in the query the planner mis-estimated the geometry side at one row, materialised it, and
# looped every admin unit over it — 32 million join-filter comparisons, 23.7 s per ad
# (EXPLAIN ANALYZE, 2026-09-08). The unit → obec name map is fetched once per run instead.
_PIN_DISTANCES_SQL = """
    SELECT g.unit_id,
           MIN(ST_Distance(g.geom::geography,
                           ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography)) AS d
      FROM ruian_admin_unit_geometries g
     WHERE g.registry_version_id = %(version)s
       AND g.purpose = 'pip'
       AND g.geom && ST_Expand(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                               %(reach_m)s / 60000.0)
       AND ST_DWithin(g.geom::geography,
                      ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
                      %(reach_m)s)
     GROUP BY g.unit_id
"""


# ------------------------------------------------------------------ candidate sources

# The text matcher moved into the lane's own module once the probe settled the design
# (`location_data.town_candidates`); re-exported here so the probe's tests keep their
# names. ONE implementation: the probe must measure exactly what the lane runs.
from location_data.town_candidates import _word_matches, text_match_candidates  # noqa: E402,F401


def obec_names_by_unit(conn: Any) -> dict[int, str]:
    """{admin-unit id -> obec name} for every current obec; the national list is its values."""
    with guarded(conn, STATEMENT_TIMEOUT_S) as cur:
        cur.execute(_OBEC_UNITS_SQL)
        return {int(r[0]): str(r[1]) for r in cur.fetchall()}


def pin_distances(
    conn: Any, version_id: int, lat: float, lon: float, reach_m: float, *,
    obec_names: dict[int, str],
) -> dict[str, float]:
    """{normalised obec name -> metres from the pin to the obec polygon}, within reach.

    Pieces of a non-obec unit (a kraj or okres also carries pip rows) are dropped by the
    map lookup; homonymous obce collapse to the nearer one under the shared name.
    """
    with guarded(conn, STATEMENT_TIMEOUT_S) as cur:
        cur.execute(_PIN_DISTANCES_SQL, {
            "version": version_id, "lat": lat, "lon": lon, "reach_m": reach_m})
        rows = cur.fetchall()
    out: dict[str, float] = {}
    for unit_id, metres in rows:
        name = obec_names.get(int(unit_id))
        if name is None:
            continue
        key = normalize_name(name)
        out[key] = min(out.get(key, float("inf")), float(metres))
    return out


# ------------------------------------------------------------------ per-ad rows

@dataclass(slots=True)
class ProbeRow:
    """One (model, ad): the lists, the three picks, and the reference's membership."""
    model: str
    listing_id: int
    url: str
    title: str
    psc: str | None
    lat: float | None
    lon: float | None
    sizes: dict[str, int]
    picks: dict[str, dict[str, Any]] = field(default_factory=dict)
    reference: str | None = None
    reference_distance_km: float | None = None
    membership: dict[str, bool | None] = field(default_factory=dict)
    agreement: dict[str, bool | None] = field(default_factory=dict)


def _in(name: str, candidates: Iterable[str]) -> bool:
    wanted = normalize_name(name)
    return any(normalize_name(c) == wanted for c in candidates)


def relate(
    row: ProbeRow, *, psc_list: list[str], text_list: list[str], union_list: list[str],
    distances: dict[str, float] | None, radius_km: float,
) -> None:
    """Fill `reference`, `membership` and `agreement` from the picks. Pure; mutates the row.

    A membership is None when the list could not be built (no PSČ, no pin), never False —
    "the source had nothing to say" and "the source got it wrong" are different findings.
    """
    national = row.picks.get("national")
    if not national or not national.get("obec") or not national.get("valid"):
        return
    reference = str(national["obec"])
    row.reference = reference
    row.membership["psc"] = _in(reference, psc_list) if psc_list else None
    row.membership["text"] = _in(reference, text_list)
    row.membership["union"] = _in(reference, union_list) if union_list else None
    if distances is None:
        for tier in DISTANCE_TIERS_KM:
            row.membership[f"pin{int(tier)}"] = None
    else:
        metres = distances.get(normalize_name(reference))
        row.reference_distance_km = None if metres is None else round(metres / 1000.0, 1)
        for tier in DISTANCE_TIERS_KM:
            row.membership[f"pin{int(tier)}"] = metres is not None and metres <= tier * 1000.0
        row.membership[f"pin_radius_{int(radius_km)}"] = (
            metres is not None and metres <= radius_km * 1000.0)
    for arm in ("psc", "union"):
        pick = row.picks.get(arm)
        if not pick or not pick.get("obec"):
            row.agreement[arm] = None
        else:
            row.agreement[arm] = normalize_name(str(pick["obec"])) == normalize_name(reference)


# ------------------------------------------------------------------ scoring

def summarise(
    rows: list[ProbeRow], calls: list[CallRecord], models: list[str], *,
    radius_km: float, max_listed: int = DEFAULT_MAX_LISTED,
) -> dict[str, Any]:
    """The report body. Pure."""
    per_model: dict[str, Any] = {}
    for model in models:
        mine = [r for r in rows if r.model == model]
        with_ref = [r for r in mine if r.reference is not None]
        membership: dict[str, Any] = {}
        for key in ("psc", "text", "union", *[f"pin{int(t)}" for t in DISTANCE_TIERS_KM]):
            decided = [r for r in with_ref if r.membership.get(key) is not None]
            membership[key] = {
                "decided": len(decided),
                "inside": sum(1 for r in decided if r.membership[key]),
                "rate": _ratio(sum(1 for r in decided if r.membership[key]), len(decided)),
            }
        sizes: dict[str, Any] = {}
        for key in ("psc", "text", "pin", "union", "national"):
            values = sorted(r.sizes.get(key, 0) for r in mine)
            sizes[key] = {
                "empty": sum(1 for v in values if v == 0),
                "p50": _percentile(values, 0.5), "p95": _percentile(values, 0.95),
                "max": values[-1] if values else None,
            }
        arms: dict[str, Any] = {}
        for arm in ARMS:
            ran = [r for r in mine if arm in r.picks]
            picked = [r for r in ran if r.picks[arm].get("obec")]
            valid = [r for r in picked if r.picks[arm].get("valid")]
            arm_calls = [c for c in calls if c.model == model and c.arm == arm]
            ok_calls = [c for c in arm_calls if c.error is None]
            durations = sorted(c.duration_ms for c in ok_calls)
            total_cost = round(sum(c.cost_usd for c in ok_calls), 6)
            agreed = [r for r in ran if r.agreement.get(arm) is True]
            comparable = [r for r in ran if r.agreement.get(arm) is not None]
            arms[arm] = {
                "ran": len(ran), "picked": len(picked),
                "pick_rate": _ratio(len(picked), len(ran)),
                "valid_picks": len(valid), "invalid_picks": len(picked) - len(valid),
                "quote_valid_rate": _ratio(
                    sum(1 for r in picked if r.picks[arm].get("quote_valid")), len(picked)),
                "agrees_with_reference": (None if arm == "national" else {
                    "comparable": len(comparable), "agreed": len(agreed),
                    "rate": _ratio(len(agreed), len(comparable))}),
                "errors": sum(1 for c in arm_calls if c.error is not None),
                "latency_ms_p50": _percentile(durations, 0.5),
                "latency_ms_p95": _percentile(durations, 0.95),
                "input_tokens_mean": _mean([c.input_tokens for c in ok_calls]),
                "cost_usd_total": total_cost,
                "cost_usd_per_call": _ratio_f(total_cost, len(ok_calls)),
                "unpriced": bool(ok_calls) and total_cost == 0.0,
            }
        outside = [
            r for r in with_ref
            if r.membership.get("psc") is False or r.membership.get("pin25") is False
            or r.membership.get("text") is False
        ]
        per_model[model] = {
            "rows": len(mine), "with_reference": len(with_ref),
            "membership": membership, "sizes": sizes, "arms": arms,
            "outside_some_list": [{
                "listing_id": r.listing_id, "url": r.url, "title": r.title,
                "reference": r.reference, "psc": r.psc,
                "distance_km": r.reference_distance_km,
                "in_psc": r.membership.get("psc"), "in_text": r.membership.get("text"),
                "in_pin25": r.membership.get("pin25"),
                "psc_pick": (r.picks.get("psc") or {}).get("obec"),
                "union_pick": (r.picks.get("union") or {}).get("obec"),
            } for r in outside[:max_listed]],
        }
    return {
        "radius_km": radius_km,
        "listing_count": len({r.listing_id for r in rows}),
        "models": list(models),
        "per_model": per_model,
        "rows": [asdict(r) for r in rows],
        "errors": [asdict(c) for c in calls if c.error is not None],
        "cost": {
            "total_usd": round(sum(c.cost_usd for c in calls), 6),
            "per_model_usd": {
                m: round(sum(c.cost_usd for c in calls if c.model == m), 6) for m in models},
        },
    }


def summary_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"## Candidate-list probe — {report['listing_count']} bazos listings",
        "",
        f"seed `{report.get('seed')}` · radius {report['radius_km']:g} km · "
        f"total ${report['cost']['total_usd']:.4f}",
    ]
    for model in report["models"]:
        m = report["per_model"][model]
        lines += [
            "", f"### `{model}` — reference picked on {m['with_reference']}/{m['rows']} ads",
            "", "| the reference town is… | decided | inside | rate |",
            "| --- | --- | --- | --- |",
        ]
        labels = {
            "psc": "in the PSČ-okres list", "text": "in the text-matched list",
            "union": "in text ∪ pin-radius list",
        }
        for tier in DISTANCE_TIERS_KM:
            labels[f"pin{int(tier)}"] = f"within {tier:g} km of the pin"
        for key, label in labels.items():
            v = m["membership"][key]
            lines.append(f"| {label} | {v['decided']} | {v['inside']} | {_pct(v['rate'])} |")
        lines += ["", "| list | empty | p50 | p95 | max |", "| --- | --- | --- | --- | --- |"]
        for key, v in m["sizes"].items():
            lines.append(f"| {key} | {v['empty']} | {v['p50']} | {v['p95']} | {v['max']} |")
        lines += ["", "| arm | ran | picked | invalid | quote valid | agrees w/ reference | "
                      "p50 ms | in tokens | $/call |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for arm, a in m["arms"].items():
            agree = a["agrees_with_reference"]
            agree_cell = "—" if agree is None else f"{agree['agreed']}/{agree['comparable']} ({_pct(agree['rate'])})"
            per_call = a["cost_usd_per_call"]
            flag = " ⚠️ UNPRICED" if a["unpriced"] else ""
            lines.append(
                f"| {arm}{flag} | {a['ran']} | {a['picked']} ({_pct(a['pick_rate'])}) | "
                f"{a['invalid_picks']} | {_pct(a['quote_valid_rate'])} | {agree_cell} | "
                f"{a['latency_ms_p50']} | {a['input_tokens_mean']} | "
                f"{'—' if per_call is None else f'${per_call:.6f}'} |")
        if m["outside_some_list"]:
            lines += ["", f"{len(m['outside_some_list'])} ads where the reference is outside "
                          "at least one list are in the JSON artefact with links."]
    if report["errors"]:
        lines += ["", f"⚠️ {len(report['errors'])} failed calls — see the artefact."]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ the run

def call_pick(
    client: Any, model: str, *, listing_id: int, native: str, arm: str, user: str,
    calls: list[CallRecord],
) -> dict[str, Any] | None:
    """One forced `pick_obec` call; None when it failed (the failure is still a CallRecord)."""
    from api.llm_client import parse_tool_input_json

    started = time.monotonic()
    try:
        response = client.call(
            called_for=CALLED_FOR, messages=[{"role": "user", "content": user}],
            system=CONSTRAINED_SYSTEM_PROMPT, tools=[CHOICE_TOOL],
            tool_choice=CHOICE_TOOL["name"], model=model, max_tokens=MAX_TOKENS)
    except Exception as exc:  # noqa: BLE001 - one call must not kill the pass
        calls.append(CallRecord(
            model=model, listing_id=listing_id, source_id_native=native,
            duration_ms=int((time.monotonic() - started) * 1000),
            input_tokens=0, output_tokens=0, cost_usd=0.0, error=str(exc)[:500], arm=arm))
        LOG.warning("PROBE %s/%s listing_id=%s failed: %s", model, arm, listing_id, exc)
        return None
    answer: dict[str, Any] = {}
    for call in response.tool_calls:
        if call.get("name") == CHOICE_TOOL["name"]:
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


def run(
    conn: Any, *, models: list[str], sample: int, seed: str, max_usd: float,
    max_seconds: float, radius_km: float, max_listed: int, dry_run: bool,
) -> dict[str, Any]:
    from api.llm_client import LLMClient

    estimate = len(ARMS) * sum(estimated_cost_usd(model, sample) for model in models)
    if estimate > max_usd:
        raise IntakeRefused(
            f"pre-flight estimate ${estimate:.2f} for {sample} listings x {len(models)} "
            f"models x {len(ARMS)} arms exceeds --max-usd ${max_usd:.2f}")

    register = load_register(conn, SOURCE)
    if register is None:
        raise IntakeRefused(f"no active portal_contracts row for {SOURCE}")
    blocks_css = block_css(conn)
    version_id, version_label = resolve_db.current_registry_version(conn)
    obec_names = obec_names_by_unit(conn)
    names = sorted(set(obec_names.values()))
    if not names:
        raise IntakeRefused("the RÚIAN mirror has no current obec rows")
    store = payloads.open_store()

    with guarded(conn, STATEMENT_TIMEOUT_S) as cur:
        cur.execute(_PROBE_SAMPLE_SQL, {
            "source": SOURCE, "min_chars": MIN_DESCRIPTION_CHARS, "seed": seed,
            "sample": sample})
        records = cur.fetchall()
    if not records:
        raise IntakeRefused("the seeded sample is empty")
    with guarded(conn, STATEMENT_TIMEOUT_S) as cur:
        bodies, from_r2 = load_bodies(cur, [int(r[2]) for r in records], store=store)

    client = None if dry_run else LLMClient(conn, providers=_providers())
    rows: list[ProbeRow] = []
    calls: list[CallRecord] = []
    deadline = time.monotonic() + max_seconds
    reach_m = MAX_DISTANCE_KM * 1000.0
    radius_m = radius_km * 1000.0

    for listing_id, native, payload_id, sha_hex, _observed, psc, url, lat, lon in records:
        if time.monotonic() > deadline:
            LOG.info("PROBE stopping: --max-seconds reached after %d listings", len(rows))
            break
        body = bodies.get(int(payload_id))
        if body is None:
            continue
        document = scope_html(body, register=register)
        if not document.is_complete:
            LOG.warning("PROBE listing_id=%s scoping incomplete; skipped", listing_id)
            continue
        blocks = block_texts(document, blocks_css)
        nodes = {b: document.css_first(css) for b, css in blocks_css.items()}
        lid = int(listing_id)

        psc_list = candidate_obce(conn, psc)
        text_list = text_match_candidates(
            f"{blocks.get('title', '')}\n{blocks.get('description', '')}", names)
        distances = None
        if lat is not None and lon is not None:
            distances = pin_distances(conn, version_id, float(lat), float(lon), reach_m,
                                      obec_names=obec_names)
        pin_list = sorted(
            name for name in names
            if distances is not None and distances.get(normalize_name(name), 1e12) <= radius_m)
        union_list = sorted(set(text_list) | set(pin_list))[:MAX_CANDIDATES]
        sizes = {"psc": len(psc_list), "text": len(text_list), "pin": len(pin_list),
                 "union": len(union_list), "national": len(names)}
        if dry_run:
            LOG.info("PROBE dry-run listing_id=%s sizes=%s", lid, sizes)
            for model in models:
                rows.append(ProbeRow(
                    model=model, listing_id=lid, url=str(url or ""),
                    title=(blocks.get("title") or "")[:120], psc=psc,
                    lat=None if lat is None else float(lat),
                    lon=None if lon is None else float(lon), sizes=sizes))
            continue

        for model in models:
            row = ProbeRow(
                model=model, listing_id=lid, url=str(url or ""),
                title=(blocks.get("title") or "")[:120], psc=psc,
                lat=None if lat is None else float(lat),
                lon=None if lon is None else float(lon), sizes=sizes)
            for arm, candidates in (("national", names), ("psc", psc_list),
                                    ("union", union_list)):
                if not candidates:
                    continue
                answer = call_pick(
                    client, model, listing_id=lid, native=str(native), arm=arm,
                    user=build_constrained_message(blocks, candidates), calls=calls)
                if answer is None:
                    continue
                choice = evaluate_choice(
                    model=model, listing_id=lid, answer=answer, candidates=candidates,
                    document=document, nodes=nodes)
                row.picks[arm] = {
                    "obec": choice.obec, "valid": choice.valid,
                    "quote": choice.quote, "quote_valid": choice.quote_valid,
                    "confidence": choice.confidence,
                }
            relate(row, psc_list=psc_list, text_list=text_list, union_list=union_list,
                   distances=distances, radius_km=radius_km)
            rows.append(row)

    report = summarise(rows, calls, models, radius_km=radius_km, max_listed=max_listed)
    report.update({
        "seed": seed, "sample_requested": sample, "source": SOURCE,
        "registry_version": version_label, "bodies_from_r2": from_r2, "dry_run": dry_run,
        "national_names": len(names), "sampled_at": datetime.now(UTC).isoformat(),
        "scope_version": register.scope_version,
    })
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD)
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    parser.add_argument("--radius-km", type=float, default=DEFAULT_RADIUS_KM)
    parser.add_argument("--max-listed", type=int, default=DEFAULT_MAX_LISTED)
    parser.add_argument("--out", default=None)
    parser.add_argument("--summary-md", default="bakeoff-summary.md")
    parser.add_argument("--dry-run", action="store_true",
                        help="Sample, scope and build every list; call NOTHING.")
    # Accepted so the bake-off workflow can route `--mode candidates` here unchanged.
    parser.add_argument("--mode", default="candidates", help=argparse.SUPPRESS)
    parser.add_argument("--max-disagreements", type=int, default=None, help=argparse.SUPPRESS)
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
        f"bakeoff-candidates-{args.seed}-{datetime.now(UTC).date().isoformat()}.json")
    with db.connect() as conn:
        try:
            report = run(
                conn, models=models, sample=args.sample, seed=args.seed,
                max_usd=args.max_usd, max_seconds=args.max_seconds,
                radius_km=args.radius_km, max_listed=args.max_listed, dry_run=args.dry_run)
        except IntakeRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
    body = json.dumps(report, indent=2, ensure_ascii=False, default=str, sort_keys=True)
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(body + "\n")
    with open(args.summary_md, "w", encoding="utf-8") as handle:
        handle.write(summary_markdown(report))
    print(summary_markdown(report))
    LOG.info("PROBE wrote %s and %s", out, args.summary_md)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
