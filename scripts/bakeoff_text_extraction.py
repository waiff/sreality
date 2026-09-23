"""The W7 model bake-off: which model may write a prose-only fact, and at what price.

Dispatch-only (`.github/workflows/text_extraction_bakeoff.yml`). It spends money, so
nothing schedules it and nothing in the lane calls it. It answers two questions per
(model, field): does it clear R7's ≥ 95 % precision, and what does a thousand adverts cost.

THE PANEL IS NOT LABELLED BY HAND. The labels are the STRUCTURED portals' own stated
fields on the SAME advert: idnes and sreality publish `has_lift`, `condition`,
`building_type`, `energy_rating`, `floor`, `total_floors`, `has_balcony` and `has_parking`
in a table AND describe the property in prose, so their table is a free label for what a
model reads out of their prose. The model is shown the description alone.

Two caveats that go in every summary this writes:

  * DOMAIN SHIFT. An idnes/sreality description is broker prose; a bazos description is a
    seller writing their own ad. So the panel carries a bazos slice too, scored where a
    cross-portal sibling exists — 842 pairs on 2026-09-22, on unique (price_czk, area_m2,
    disposition) — which is small, and stated as small.
  * FLOOR CONVENTION. idnes stores ground = 0 and sreality ground = 1 (W8/A9, proven by
    sibling pairs), so a sreality label is `floor - 1` and only for `floor >= 1`: that
    portal writes BOTH 0 and 1 for the ground storey ('zvýšené přízemí', 4,591 rows), so a
    row at 0 is ambiguous and is not a label. The MODEL never converts anything — it
    returns the advert's own words and `scraper.floor` reads them, which is the thing
    being measured.

The winner is not decided here and is not hardcoded anywhere: the operator sets
`app_settings.enrichment_model`, which is the lane's one switch.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections import Counter
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from scraper import db
from scraper import area as area_grammar
from toolkit import description_extraction as tx

LOG = logging.getLogger("bakeoff_text_extraction")

# gpt-5.6-luna is the paid arm R10 names (the location lane's winner). The two open
# candidates were checked on the Hub 2026-09-22 — both Apache-2.0 and UNGATED, so neither
# needs HF_TOKEN or an accepted licence, and both are MULTIMODAL, which matters for a
# reason that has nothing to do with this task: `autodedup/oss_pod.build_vllm_args` always
# passes `--limit-mm-per-prompt`, a text-only checkpoint may refuse to bind on it, and R12
# forbids this program editing that module. A text-only candidate is a hand-over, not a
# flag here.
#   Qwen/Qwen3-VL-32B-Instruct   33B dense, 256K context. bf16 ~66 GB -> one 80 GB A100.
#   google/gemma-4-26B-A4B-it    Gemma 4 (released 2026-04-02), 25.2B total / 3.8B active
#                                MoE, 256K context. Every expert stays resident, so bf16
#                                is ~50 GB -> also an 80 GB A100.
# Both are therefore SECURE cloud (~$1.6-2.2/hr), above oss_pod's $1.00/hr community cap:
# run them with `--oss-cloud SECURE --oss-max-price 2.50`.
# `gpt-5-mini` is the incumbent; add it to --models to keep the comparison honest about
# whether changing anything is worth it.
DEFAULT_MODELS = ("gpt-5.6-luna", "oss:Qwen/Qwen3-VL-32B-Instruct",
                  "oss:google/gemma-4-26B-A4B-it")

# The portals whose own table labels the panel, with the offset their floor column carries.
# The two structured portals whose stated fields label the panel. `listings.floor` is
# ground = 0 on every portal since field-capture W8 healed the six ground = 1 portals
# (2026-09-22), so a label is the stored value itself; the per-source offset this table
# used to carry made every arm run after that heal score floor against labels one storey
# off (Gemma, run 35731041090: 82.0 % "floor" was the offset, not the model).
LABEL_SOURCES: tuple[str, ...] = ("idnes", "sreality")
PRECISION_GATE = 0.95
FLOOR_TOLERANCE = 1
# An area read from prose against the portal's table: a rounded decimal or a
# 68 vs 68,5 restatement, never a different measure. 3 % or one square metre.
AREA_TOLERANCE = 0.03
# A gate needs a sample, not a ratio: three correct answers are 100 % and prove nothing.
MIN_ANSWERED = 100

# STRATIFIED BY CATEGORY, in SQL, because drawing the newest N and sorting them afterwards
# is not stratification: idnes's newest 500 are ~40 % flats and a field measured almost
# entirely on flat prose would have its gate opened for a bazos corpus where houses are a
# large share. The quota is `limit / distinct categories`, taken newest-first inside each
# category; a category with fewer rows than its quota contributes all it has and the
# shortfall is NOT redistributed, so the realised mix is reported rather than assumed.
_PANEL_SQL_TEMPLATE = """
WITH pool AS (
  SELECT l.id, l.source, l.category_main, l.description,
         l.floor, l.total_floors, l.has_balcony, l.has_lift, l.has_parking,
         l.building_type, l.condition, l.energy_rating, l.area_m2
    FROM listings l
   WHERE l.is_active
     AND l.source = %(source)s
     AND l.description IS NOT NULL
     AND ({stated})
), quota AS (
  SELECT greatest(1, ceil(%(limit)s::numeric
                          / greatest(count(DISTINCT category_main), 1))::int) AS n
    FROM pool
), ranked AS (
  SELECT pool.*,
         row_number() OVER (PARTITION BY category_main ORDER BY id DESC) AS rn
    FROM pool
)
SELECT ranked.id, ranked.source, ranked.category_main, ranked.description,
       ranked.floor, ranked.total_floors, ranked.has_balcony, ranked.has_lift,
       ranked.has_parking, ranked.building_type, ranked.condition, ranked.energy_rating,
       ranked.area_m2
  FROM ranked, quota
 WHERE ranked.rn <= quota.n
 ORDER BY ranked.category_main, ranked.id DESC
 LIMIT %(limit)s
"""

# The bazos slice: scored only where one structured portal advertises the same unique
# (price_czk, area_m2, disposition), which is the same sibling key the W8 floor gate uses.
_BAZOS_PANEL_SQL = """
WITH b AS (
  SELECT price_czk, area_m2, disposition, min(id) AS id
    FROM listings
   WHERE is_active AND source = 'bazos'
     AND price_czk IS NOT NULL AND area_m2 IS NOT NULL AND disposition IS NOT NULL
   GROUP BY 1, 2, 3 HAVING count(*) = 1
), s AS (
  SELECT price_czk, area_m2, disposition, min(id) AS id
    FROM listings
   WHERE is_active AND source IN ('idnes', 'sreality')
     AND price_czk IS NOT NULL AND area_m2 IS NOT NULL AND disposition IS NOT NULL
   GROUP BY 1, 2, 3 HAVING count(*) = 1
)
SELECT l.id, 'bazos' AS source, l.category_main, l.description,
       sib.floor, sib.total_floors, sib.has_balcony, sib.has_lift, sib.has_parking,
       sib.building_type, sib.condition, sib.energy_rating, sib.area_m2,
       sib.source AS label_source
  FROM b JOIN s USING (price_czk, area_m2, disposition)
  JOIN listings l ON l.id = b.id
  JOIN listings sib ON sib.id = s.id
 WHERE l.description IS NOT NULL
 ORDER BY l.id DESC
 LIMIT %(limit)s
"""

FIELDS: tuple[str, ...] = tuple(tx._FIELD_SPEC)


def panel_sql(fields: Iterable[str]) -> str:
    return _PANEL_SQL_TEMPLATE.format(
        stated=" OR ".join(f"l.{f} IS NOT NULL" for f in fields))


def _label_area(description: str | None, stored: Any) -> float | None:
    """The portal's headline area, but ONLY on an advert whose prose the ingest grammar
    reads nothing from: those are the rows the lane will ever be asked, so a panel that
    also scored "54 m²" would measure the grammar's job, not the model's."""
    if stored is None or area_grammar.parse_area_text(description) is not None:
        return None
    return float(stored)


def _label_floor(source: str, stored: Any) -> int | None:
    """The portal's floor column IS the ground = 0 label (W8); None where it is unset."""
    del source  # every portal counts the same way now; the argument stays for the panel rows
    return None if stored is None else int(stored)


def build_panel(conn: Any, *, per_source: int, bazos: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    columns = ("id", "source", "category_main", "description", *FIELDS)
    with conn.cursor() as cur:
        # The pool is drawn on the eight prose-comparable fields; area is labelled
        # wherever it happens to be present, so adding it never displaces the rows the
        # other eight are measured on.
        for source in LABEL_SOURCES:
            cur.execute(panel_sql([f for f in FIELDS if f != "area_m2"]),
                        {"source": source, "limit": per_source})
            for row in cur.fetchall():
                rows.append(_panel_row(dict(zip(columns, row)), label_source=source))
        if bazos:
            cur.execute(_BAZOS_PANEL_SQL, {"limit": bazos})
            for row in cur.fetchall():
                record = dict(zip((*columns, "label_source"), row))
                rows.append(_panel_row(record, label_source=record["label_source"]))
    return rows


def _panel_row(record: dict[str, Any], *, label_source: str) -> dict[str, Any]:
    labels = {f: record.get(f) for f in FIELDS}
    labels["floor"] = _label_floor(label_source, labels.get("floor"))
    # The bazos slice's sibling key IS the grammar's area, so it cannot label area at all.
    labels["area_m2"] = (None if record["source"] == "bazos" else
                         _label_area(record["description"], labels.get("area_m2")))
    return {
        "id": record["id"],
        "source": record["source"],
        "label_source": label_source,
        "category_main": record["category_main"],
        "description": record["description"],
        "labels": {k: v for k, v in labels.items() if v is not None},
    }


# --- scoring -----------------------------------------------------------------


def agrees(field: str, predicted: Any, label: Any) -> bool:
    if field == "floor":
        return abs(int(predicted) - int(label)) <= FLOOR_TOLERANCE
    if field == "area_m2":
        label = float(label)
        return abs(float(predicted) - label) <= max(1.0, AREA_TOLERANCE * label)
    return predicted == label


def _score_field(field: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    labelled = [r for r in results if field in r["labels"]]
    answered = [r for r in labelled if field in r["values"]]
    correct = sum(
        1 for r in answered if agrees(field, r["values"][field], r["labels"][field]))
    dropped = Counter(r["dropped"][field] for r in results if field in r["dropped"])
    return {
        "labelled": len(labelled),
        "answered": len(answered),
        "answer_rate": round(len(answered) / len(labelled), 4) if labelled else None,
        "correct": correct,
        "precision": round(correct / len(answered), 4) if answered else None,
        "quote_invalid": dropped.get("quote_not_in_text", 0),
        # Every refusal reason, so abstention (null) and refusal (a value the rail
        # rejected) can be told apart in the receipt.
        "dropped": dict(sorted(dropped.items())),
        "passes_gate": (len(answered) >= MIN_ANSWERED
                        and correct / len(answered) >= PRECISION_GATE),
    }


def score(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Per field: precision where the model ANSWERED, answer rate, quote validity."""
    per_field: dict[str, Any] = {}
    for field in FIELDS:
        per_field[field] = _score_field(field, results)
        if field == "area_m2":
            # One number over land (label = the plot, easy) and dwellings (label = the
            # interior, hard) would hide the arm that fails; the split is what to read.
            per_field[field]["by_category"] = {
                category: _score_field(field, [r for r in results
                                               if r.get("category_main") == category])
                for category in sorted({str(r.get("category_main")) for r in results
                                        if field in r["labels"]})
            }
    latencies = [r["ms"] for r in results if r.get("ms")]
    costs = [r["cost_usd"] for r in results]
    return {
        "n": len(results),
        "errors": sum(1 for r in results if r.get("error")),
        "per_field": per_field,
        "p50_ms": round(statistics.median(latencies)) if latencies else None,
        "usd_per_1k_adverts": round(1000 * sum(costs) / len(costs), 3) if costs else None,
        "api_cost_usd": round(sum(costs), 4),
    }


ARM_WORKERS = 8


def _extract_one(model: str, tool: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """One advert through the lane's contract, on this worker thread's own connection.

    `LLMClient` writes `llm_calls` through the connection it is handed and psycopg
    connections are not shared across threads, so each worker opens its own — the shape
    `toolkit.vision_batch.run_batch` already uses (per-worker connections)."""
    from api.llm_client import LLMClient
    from api.providers.openai import OpenAIProvider
    from api.providers.oss import OssProvider

    started = time.monotonic()
    try:
        with db.connect() as wconn:
            llm = LLMClient(wconn, providers={"openai": OpenAIProvider(), "oss": OssProvider()})
            res = llm.call(
                called_for=tx.CALLED_FOR, model=model, max_tokens=tx.MAX_TOKENS,
                system=tx._SYSTEM_PROMPT, tools=[tool], tool_choice=tool["name"],
                messages=[{"role": "user", "content": row["description"]}],
            )
        payload = tx._tool_arguments(res) or {}
        values, dropped = tx.merge_extraction(
            payload, description=row["description"], fields=FIELDS,
            category_main=row.get("category_main"))
        return {
            "id": row["id"], "category_main": row.get("category_main"),
            "labels": row["labels"], "values": values,
            "dropped": dropped, "cost_usd": float(res.cost_usd or 0.0),
            "ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001 — one advert must not end the arm
        LOG.warning("%s listing=%s failed: %s", model, row["id"], str(exc)[:200])
        return {"id": row["id"], "category_main": row.get("category_main"),
                "labels": row["labels"], "values": {},
                "dropped": {}, "cost_usd": 0.0, "ms": None, "error": str(exc)[:300]}


def run_model(conn: Any, model: str, panel: list[dict[str, Any]],
              *, workers: int = ARM_WORKERS) -> dict[str, Any]:
    """One arm over the panel, `workers` adverts in flight at once — the lane's own
    concurrency. Serial, a 32B model behind one A100 needed ~4 h for 1,387 adverts and
    the job cap cancelled it at page 1,387-minus-something (run 35708090261); both vLLM
    and the API serve eight requests as fast as one. Results keep the panel's order."""
    from concurrent.futures import ThreadPoolExecutor

    del conn  # the panel was built on it; every call below runs on a worker's own
    tool = tx.extraction_tool(FIELDS)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda row: _extract_one(model, tool, row), panel))
    return {"model": model, **score(results)}


def _pod_is_gone(client: Any, pod_id: str) -> bool:
    """True only on RunPod's own word: a 404 (or a pod record that reports a terminal
    status). A network error is NOT "gone" — that is the failure this exists to catch."""
    try:
        pod = client.get_pod(pod_id)
    except Exception as exc:  # noqa: BLE001 — classified below, never raised
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return status == 404
    return str(pod.get("desiredStatus") or "").upper() in {"EXITED", "TERMINATED"}


def run_oss_model(conn: Any, model: str, panel: list[dict[str, Any]],
                  *, cloud: str, max_price: float, out_dir: Path) -> dict[str, Any]:
    """The same arm behind a rented pod. The bill is GPU-hours, not tokens, so the pod's
    wall-clock cost is attributed to the arm and reported beside the API cost — a
    per-token price of zero would look like the real number."""
    from api.providers.oss import BASE_URL_ENV
    from autodedup import oss_pod
    from scripts.runpod_client import RunPodClient

    key = os.environ.get("RUNPOD_API_KEY")
    if not key:
        raise SystemExit("RUNPOD_API_KEY must be set to run an oss: arm")
    client = RunPodClient(key)
    hf_id = model.split(":", 1)[1]
    with oss_pod.rented_pod(
        client, model_id=hf_id, cloud_types=(cloud,), max_price_per_hr=max_price,
        min_memory_gb=80.0, hf_token=os.environ.get("HF_TOKEN"),
        name="field-capture-w7-bakeoff",
    ) as handle:
        # The receipt first, before the long wait: a cancelled job does not run a `finally`,
        # and a pod nothing names in the artefact keeps billing.
        oss_pod.write_receipt(handle, out_dir)
        oss_pod.wait_ready(handle, client=client)
        # The handle carries the HTTP root; an OpenAI-compatible client posts to
        # `{base}/chat/completions`. Set BEFORE the client is built — the provider reads
        # its endpoint once, at construction.
        root = handle.base_url.rstrip("/")
        os.environ[BASE_URL_ENV] = root if root.endswith("/v1") else f"{root}/v1"
        result = run_model(conn, model, panel)
        result["pod"] = {
            "gpu": handle.gpu, "usd_per_hr": handle.usd_per_hr,
            "cloud_type": handle.cloud_type,
            "gpu_hours_usd": oss_pod.pod_cost_usd(handle, time.time()),
        }
    # The receipt goes only once the pod is CONFIRMED gone. `rented_pod`'s teardown never
    # raises, so a terminate that timed out on RunPod's side (run 35731041090) used to
    # reach this line, clear the receipt, and leave the reap step with nothing to reap
    # while the pod kept billing.
    if _pod_is_gone(client, handle.pod_id):
        oss_pod.clear_receipt(out_dir)
    else:
        LOG.error("pod %s still answers after teardown — receipt kept for the reap step",
                  handle.pod_id)
    result["usd_per_1k_adverts"] = round(
        1000 * result["pod"]["gpu_hours_usd"] / max(result["n"], 1), 3)
    return result


# --- the summary the PR quotes -----------------------------------------------


def summarise(report: dict[str, Any]) -> str:
    lines = [
        f"# Text-extraction bake-off — {report['generated_at']}",
        "",
        f"Panel: {report['panel']['n']} adverts "
        f"({report['panel']['by_source']}), stratified by category "
        f"({report['panel']['by_category']}) — the REALISED mix, not the quota. Labels "
        "are the structured portals' own stated fields on the same advert. No hand "
        "labelling.",
        "",
        f"Gate: precision >= {PRECISION_GATE:.0%} where the model answers, on at least "
        f"{MIN_ANSWERED} answers (floor: within +-{FLOOR_TOLERANCE}; area: within "
        f"{AREA_TOLERANCE:.0%} or 1 m², labelled only where `scraper.area` reads nothing "
        f"from the prose, never on the bazos slice).",
        "",
        "| model | field | labelled | answered | answer rate | precision | gate |",
        "| --- | --- | ---: | ---: | ---: | ---: | :-: |",
    ]
    for arm in report["arms"]:
        for field, s in arm["per_field"].items():
            if not s["labelled"]:
                continue
            lines.append(
                f"| {arm['model']} | {field} | {s['labelled']} | {s['answered']} | "
                f"{_pct(s['answer_rate'])} | {_pct(s['precision'])} | "
                f"{'PASS' if s['passes_gate'] else 'no'} |")
            for category, c in s.get("by_category", {}).items():
                lines.append(
                    f"| {arm['model']} | {field} / {category} | {c['labelled']} | "
                    f"{c['answered']} | {_pct(c['answer_rate'])} | {_pct(c['precision'])} "
                    f"| {'PASS' if c['passes_gate'] else 'no'} |")
            if s["dropped"]:
                lines.append(f"| {arm['model']} | {field} dropped | "
                             f"{', '.join(f'{k} {v}' for k, v in s['dropped'].items())} "
                             "| | | | |")
    lines += ["", "| model | $/1k adverts | p50 ms | errors | pod |",
              "| --- | ---: | ---: | ---: | --- |"]
    for arm in report["arms"]:
        pod = arm.get("pod")
        lines.append(
            f"| {arm['model']} | {arm['usd_per_1k_adverts']} | {arm['p50_ms']} | "
            f"{arm['errors']} | "
            + (f"{pod['gpu']} @ ${pod['usd_per_hr']}/hr, ${pod['gpu_hours_usd']} "
               f"({pod['cloud_type']})" if pod else "-") + " |")
    lines += [
        "",
        "Caveats that do not go away with a bigger n: the panel is BROKER prose and the "
        "lane's target portal is SELLER prose, so the bazos slice (scored only where a "
        "cross-portal sibling exists) is the only in-domain read there is; and a field "
        "whose gate passes here still ships closed until someone edits its "
        "`attribute_contract` cell.",
    ]
    return "\n".join(lines) + "\n"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--per-source", type=int, default=600,
                    help="panel rows per structured portal, stratified per category "
                         "(600 measured 1,207 structured rows; a thin category is not "
                         "redistributed, so the realised n is at or under 2 x this)")
    ap.add_argument("--bazos", type=int, default=300)
    ap.add_argument("--out-dir", default="bakeoff")
    ap.add_argument("--oss-cloud", default="COMMUNITY", choices=("COMMUNITY", "SECURE"))
    ap.add_argument("--oss-max-price", type=float, default=1.00)
    ap.add_argument("--panel-only", action="store_true",
                    help="build and write the panel, make no LLM call")
    ap.add_argument("--verbose", action="store_true")
    return ap


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with db.connect() as conn:
        panel = build_panel(conn, per_source=args.per_source, bazos=args.bazos)
        by_source: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for row in panel:
            by_source[row["source"]] = by_source.get(row["source"], 0) + 1
            key = str(row["category_main"])
            by_category[key] = by_category.get(key, 0) + 1
        labelled = Counter(field for row in panel for field in row["labels"])
        LOG.info("PANEL n=%d %s %s labelled=%s", len(panel), by_source, by_category,
                 dict(sorted(labelled.items())))
        (out_dir / "panel.json").write_text(
            json.dumps(panel, ensure_ascii=False, default=str), encoding="utf-8")
        if args.panel_only:
            return 0

        arms: list[dict[str, Any]] = []
        for model in [m.strip() for m in args.models.split(",") if m.strip()]:
            LOG.info("ARM %s over %d adverts", model, len(panel))
            if model.startswith("oss:"):
                arms.append(run_oss_model(
                    conn, model, panel, cloud=args.oss_cloud,
                    max_price=args.oss_max_price, out_dir=out_dir))
            else:
                arms.append(run_model(conn, model, panel))

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gate": PRECISION_GATE,
        "floor_tolerance": FLOOR_TOLERANCE,
        "area_tolerance": AREA_TOLERANCE,
        "min_answered": MIN_ANSWERED,
        "panel": {"n": len(panel), "by_source": by_source,
                  "by_category": by_category},
        "arms": arms,
    }
    (out_dir / "bakeoff.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = summarise(report)
    (out_dir / "bakeoff.md").write_text(summary, encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
