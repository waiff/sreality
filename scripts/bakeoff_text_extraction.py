"""The W7 model bake-off: which model may write a prose-only fact, and at what price.

Dispatch-only (`.github/workflows/text_extraction_bakeoff.yml`). It spends money, so
nothing schedules it and nothing in the lane calls it. It answers two questions per
(model, field): does it clear R7's ≥ 95 % precision, and what does a thousand adverts cost.

THE PANEL IS NOT LABELLED BY HAND. The labels are the STRUCTURED portals' own stated
fields on the SAME advert: idnes and sreality publish `has_lift`, `condition`,
`building_type`, `energy_rating`, `floor`, `total_floors`, `has_balcony` and `has_parking`
in a table AND describe the property in prose, so their table is a free label for what a
model reads out of their prose. The model reads the advert text (headline + description) under
the lane's prompt; a location row gets the lane's own tool for its portal, an attribute row all 8.

The LOCATION half (location reader W1, `--location-rows`) scores the lane's `location` block:
sreality/idnes/realitymix/ceskereality rows whose street the portal stated in a table
(`label_kind=structured`, free labels), and bazos rows labelled by the STORED
`listing_location` (`label_kind=stored` — not ground truth: its disagreements are the review).

The caveat that goes in every summary this writes, DOMAIN SHIFT: an idnes/sreality
description is broker prose; a bazos description is a seller writing their own ad. So the
panel carries a bazos slice too, scored where a cross-portal sibling exists — 842 pairs on
2026-09-22, on unique (price_czk, area_m2, disposition) — which is small, and stated as small.
(Floor labels are the stored value on every portal since W8; the MODEL never converts a floor.)

The winner is not decided here and is not hardcoded anywhere: the operator sets
`app_settings.enrichment_model`, which is the lane's one switch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from location_data import text_reading
from location_data.resolver.composite import street_match_keys
from location_data.resolver.normalize import normalize_match_key
from scraper import attribute_contract as contract
from scraper import db
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

# The two structured portals whose stated fields label the panel. `listings.floor` is
# ground = 0 on every portal since field-capture W8 healed the six ground = 1 portals
# (2026-09-22), so a label is the stored value itself; the per-source offset this table
# used to carry made every arm run after that heal score floor against labels one storey
# off (Gemma, run 35731041090: 82.0 % "floor" was the offset, not the model).
LABEL_SOURCES: tuple[str, ...] = ("idnes", "sreality")
PRECISION_GATE = 0.95
FLOOR_TOLERANCE = 1

# STRATIFIED BY CATEGORY, in SQL, because drawing the newest N and sorting them afterwards
# is not stratification: idnes's newest 500 are ~40 % flats and a field measured almost
# entirely on flat prose would have its gate opened for a bazos corpus where houses are a
# large share. The quota is `limit / distinct categories`, taken newest-first inside each
# category; a category with fewer rows than its quota contributes all it has and the
# shortfall is NOT redistributed, so the realised mix is reported rather than assumed.
# The advert text is composed for the LIMITed rows only: `raw_json ->> 'title'` on every
# active sreality/idnes row detoasts ~200k large payloads and cancelled the panel build at
# the statement timeout (run 36679009472).
_PANEL_SQL_TEMPLATE = """
WITH pool AS (
  SELECT l.id, l.source, l.category_main,
         l.floor, l.total_floors, l.has_balcony, l.has_lift, l.has_parking,
         l.building_type, l.condition, l.energy_rating
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
), picked AS (
  SELECT ranked.* FROM ranked, quota
   WHERE ranked.rn <= quota.n
   ORDER BY ranked.category_main, ranked.id DESC
   LIMIT %(limit)s
)
SELECT picked.id, picked.source, picked.category_main, {text} AS advert_text,
       picked.floor, picked.total_floors, picked.has_balcony, picked.has_lift,
       picked.has_parking, picked.building_type, picked.condition, picked.energy_rating
  FROM picked JOIN listings l ON l.id = picked.id
 ORDER BY picked.category_main, picked.id DESC
"""

# The bazos slice: scored only where one structured portal advertises the same unique
# (price_czk, area_m2, disposition), which is the same sibling key the W8 floor gate uses.
_BAZOS_PANEL_SQL = f"""
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
SELECT l.id, 'bazos' AS source, l.category_main, {tx._TEXT_EXPR} AS advert_text,
       sib.floor, sib.total_floors, sib.has_balcony, sib.has_lift, sib.has_parking,
       sib.building_type, sib.condition, sib.energy_rating, sib.source AS label_source
  FROM b JOIN s USING (price_czk, area_m2, disposition)
  JOIN listings l ON l.id = b.id
  JOIN listings sib ON sib.id = s.id
 WHERE l.description IS NOT NULL
 ORDER BY l.id DESC
 LIMIT %(limit)s
"""

FIELDS: tuple[str, ...] = tuple(tx._FIELD_SPEC)

# The location half. bazos: md5-sampled, plus the trigger, plus hard cases on their OWN md5 key
# (exchange/wanted by headline intent, Slovak, k.ú., a town by distance, a streetless big city).
TRIGGER_ID = 18909736
_LOCATED = """ll.obec_name, ll.okres_name, ll.obec_kod, ll.cast_obce_name, ll.street_name,
       ll.house_number_cp, ll.psc"""
_LOCATION_BAZOS_SQL = f"""
WITH pool AS (
  SELECT x.id, md5(x.id::text) AS rk,
         split_part(x.t, E'\\n', 1)
           ~* '\\m(vyměním|výměna\\s+za|hledám|hledáme|koupím|koupíme|sháním|poptávám)' AS exchange_wanted,
         x.t ~* '\\mponúk|\\mnehnuteľnos|\\mbyt na predaj' AS slovak,
         x.t ~* 'k\\.ú\\.|katastráln' AS village_ku,
         x.t ~* '\\m[0-9]+([,.][0-9]+)?\\s*km\\s+(od|z|ze)\\s+[[:upper:]]' AS by_distance,
         coalesce(x.obec_name IN ('Praha', 'Brno', 'Plzeň', 'Ostrava')
                  AND x.street_name IS NULL, false) AS big_city
    FROM (SELECT l.id, {tx._TEXT_EXPR} AS t, ll.obec_name, ll.street_name
            FROM listings l LEFT JOIN listing_location ll ON ll.listing_id = l.id
           WHERE l.source = 'bazos' AND l.is_active
             AND l.description IS NOT NULL AND l.description <> '') x
), picked AS (
  (SELECT id, 'md5' AS stratum FROM pool ORDER BY rk LIMIT %(n)s)
  UNION ALL (SELECT id, 'exchange_wanted' FROM pool WHERE exchange_wanted ORDER BY md5(id || 'exchange_wanted') LIMIT %(hard)s)
  UNION ALL (SELECT id, 'slovak' FROM pool WHERE slovak ORDER BY md5(id || 'slovak') LIMIT %(hard)s)
  UNION ALL (SELECT id, 'village_ku' FROM pool WHERE village_ku ORDER BY md5(id || 'village_ku') LIMIT %(hard)s)
  UNION ALL (SELECT id, 'by_distance' FROM pool WHERE by_distance ORDER BY md5(id || 'by_distance') LIMIT %(hard)s)
  UNION ALL (SELECT id, 'big_city' FROM pool WHERE big_city ORDER BY md5(id || 'big_city') LIMIT %(hard)s)
  UNION ALL SELECT %(trigger)s::bigint, 'trigger'
)
SELECT DISTINCT ON (l.id) l.id, l.source, l.category_main, p.stratum,
       {tx._TEXT_EXPR} AS advert_text, {_LOCATED}
  FROM picked p JOIN listings l ON l.id = p.id
  LEFT JOIN listing_location ll ON ll.listing_id = l.id
 ORDER BY l.id, p.stratum <> 'trigger', p.stratum <> 'md5'
"""
# The structured portals: a street the portal stated in a table is a free label.
LOCATION_SOURCES: tuple[str, ...] = ("sreality", "idnes", "realitymix", "ceskereality")
_LOCATION_STRUCTURED_SQL = f"""
WITH picked AS (
  SELECT l.id
    FROM listing_location ll JOIN listings l ON l.id = ll.listing_id
   WHERE l.source = %(source)s AND l.is_active
     AND l.description IS NOT NULL AND l.description <> ''
     AND ll.street_name IS NOT NULL AND ll.granularity IN ('street', 'address_point')
   ORDER BY md5(l.id::text)
   LIMIT %(n)s
)
SELECT l.id, l.source, l.category_main, 'structured' AS stratum,
       {tx._TEXT_EXPR} AS advert_text, {_LOCATED}
  FROM picked JOIN listings l ON l.id = picked.id
  LEFT JOIN listing_location ll ON ll.listing_id = l.id
 ORDER BY md5(l.id::text)
"""
# ONE statement over every reading: does the text town exist in the register (obec, část
# obce, k.ú.), which namesake (the obec nearest the pin, preferring the obec's own unit), how
# many namesakes, the km from the pin (a část obce has no point in the register: its nearest
# address point), and does the text street bind in the stored obec and in the text town's obec.
_REGISTER_SQL = """
WITH q AS (
  SELECT * FROM unnest(%(ids)s::bigint[], %(towns)s::text[], %(streets)s::text[])
         AS q(listing_id, town_norm, street_keys)
)
SELECT q.listing_id, t.level, t.name, t.okres, t.km, t.namesakes, ll.geom IS NOT NULL,
       EXISTS (SELECT 1 FROM ruian_streets s JOIN ruian_admin_units o ON o.id = s.obec_unit_id
                WHERE o.level = 'obec' AND o.code = ll.obec_kod AND s.valid_to IS NULL
                  AND s.name_norm = ANY (string_to_array(q.street_keys, '|'))),
       EXISTS (SELECT 1 FROM ruian_streets s
                WHERE s.obec_unit_id = t.obec_unit_id AND s.valid_to IS NULL
                  AND s.name_norm = ANY (string_to_array(q.street_keys, '|')))
  FROM q
  LEFT JOIN listing_location ll ON ll.listing_id = q.listing_id
  LEFT JOIN LATERAL (
    SELECT c.level, c.name, c.okres, c.obec_unit_id, count(*) OVER () AS namesakes,
           min(c.unit_km) OVER (PARTITION BY c.obec_unit_id) AS km
      FROM (SELECT u.level::text AS level, u.name, ok.name AS okres,
                   CASE WHEN u.level = 'obec' THEN u.id ELSE u.parent_id END AS obec_unit_id,
                   coalesce(ST_Distance(ll.geom::geography, g.representative_point::geography),
                            (SELECT min(ST_Distance(ll.geom::geography, ap.geom::geography))
                               FROM ruian_address_points ap
                              WHERE ap.cast_obce_unit_id = u.id AND ap.valid_to IS NULL)
                   ) / 1000.0 AS unit_km
              FROM ruian_admin_units u
              LEFT JOIN ruian_admin_units ok
                ON ok.level = 'okres' AND ok.retired_at IS NULL AND ok.path @> u.path
              LEFT JOIN LATERAL (
                SELECT g.representative_point FROM ruian_admin_unit_geometries g
                 WHERE g.unit_id = u.id AND g.purpose = 'authoritative'
                 ORDER BY g.registry_version_id DESC LIMIT 1) g ON true
             WHERE u.name_norm = q.town_norm AND u.retired_at IS NULL
               AND u.level IN ('obec', 'cast_obce', 'katastralni_uzemi')) c
     ORDER BY km NULLS LAST, c.level <> 'obec'
     LIMIT 1
  ) t ON true
"""


def panel_sql(fields: Iterable[str]) -> str:
    return _PANEL_SQL_TEMPLATE.format(
        text=tx._TEXT_EXPR, stated=" OR ".join(f"l.{f} IS NOT NULL" for f in fields))


def build_panel(conn: Any, *, per_source: int, bazos: int,
                location_rows: int = 0) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    columns = ("id", "source", "category_main", "advert_text", *FIELDS)
    with conn.cursor() as cur:
        for source in LABEL_SOURCES:
            cur.execute(panel_sql(FIELDS), {"source": source, "limit": per_source})
            for row in cur.fetchall():
                rows.append(_panel_row(dict(zip(columns, row)), label_source=source))
        if bazos:
            cur.execute(_BAZOS_PANEL_SQL, {"limit": bazos})
            for row in cur.fetchall():
                record = dict(zip((*columns, "label_source"), row))
                rows.append(_panel_row(record, label_source=record["label_source"]))
        if location_rows:
            loc_columns = ("id", "source", "category_main", "stratum", "advert_text",
                           "obec_name", "okres_name", "obec_kod", "cast_obce_name",
                           "street_name", "house_number_cp", "psc")
            cur.execute(_LOCATION_BAZOS_SQL, {"n": location_rows, "trigger": TRIGGER_ID,
                                              "hard": max(1, location_rows // 10)})
            fetched = cur.fetchall()
            for source in LOCATION_SOURCES:
                cur.execute(_LOCATION_STRUCTURED_SQL, {"source": source, "n": location_rows})
                fetched += cur.fetchall()
            for r in (dict(zip(loc_columns, row)) for row in fetched):
                rows.append({**{k: r[k] for k in ("id", "source", "category_main", "stratum",
                                                   "advert_text")},
                             "label_source": r["source"], "labels": {},
                             "label_kind": "structured" if r["stratum"] == "structured"
                             else "stored",
                             "stored": {k: r[k] for k in ("obec_name", "okres_name", "psc")},
                             "location_labels": {"town": r["obec_name"],
                                                 "part_of_town": r["cast_obce_name"],
                                                 "street": r["street_name"],
                                                 "house_number_cp": r["house_number_cp"]}})
    return rows


def _panel_row(record: dict[str, Any], *, label_source: str) -> dict[str, Any]:
    labels = {f: record.get(f) for f in FIELDS}
    return {
        "id": record["id"],
        "source": record["source"],
        "label_source": label_source,
        "category_main": record["category_main"],
        "advert_text": record["advert_text"],
        "labels": {k: v for k, v in labels.items() if v is not None},
    }


# --- scoring -----------------------------------------------------------------


def agrees(field: str, predicted: Any, label: Any) -> bool:
    if field == "floor":
        return abs(int(predicted) - int(label)) <= FLOOR_TOLERANCE
    if field == "house_number_cp":
        return str(predicted).strip() == str(label).strip()
    if field == "street":  # W3's binder keys, both tiers: 'nám. Míru' == 'náměstí Míru'
        return bool(street_match_keys(str(predicted))[1] & street_match_keys(str(label))[1])
    if field in text_reading.SLOTS:
        return normalize_match_key(str(predicted)) == normalize_match_key(str(label))
    return predicted == label


def _rate(hits: int, of: int) -> float | None:
    return round(hits / of, 4) if of else None


def score(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Per field: precision where the model ANSWERED, answer rate, quote validity. Per
    location slot: answer rate, quote validity, agreement with a structured label; then the
    town's register bind rate, the bazos stored-town agreement and the street bind rates."""
    per_field: dict[str, Any] = {}
    for field in FIELDS:
        labelled = [r for r in results if field in r["labels"]]
        answered = [r for r in labelled if field in r["values"]]
        correct = sum(
            1 for r in answered if agrees(field, r["values"][field], r["labels"][field]))
        bad_quote = sum(1 for r in results if r["dropped"].get(field) == "quote_not_in_text")
        per_field[field] = {
            "labelled": len(labelled),
            "answered": len(answered),
            "answer_rate": round(len(answered) / len(labelled), 4) if labelled else None,
            "correct": correct,
            "precision": round(correct / len(answered), 4) if answered else None,
            "quote_invalid": bad_quote,
            "passes_gate": bool(answered) and correct / len(answered) >= PRECISION_GATE,
        }
    loc = [r for r in results if "location" in r]
    slots: dict[str, Any] = {}
    for slot in text_reading.SLOTS:
        claimed = [r for r in loc if slot in r["claimed"]]
        labelled = [r for r in loc if r["label_kind"] == "structured"
                    and r["location_labels"].get(slot) and r["location"][slot]["value"]]
        slots[slot] = {
            "answered": len(claimed), "answer_rate": _rate(len(claimed), len(loc)),
            "quote_valid": _rate(sum(slot in r["quoted"] for r in claimed), len(claimed)),
            "structured_labelled": len(labelled),
            "structured_agreement": _rate(sum(agrees(
                slot, r["location"][slot]["value"], r["location_labels"][slot])
                for r in labelled), len(labelled)),
        }
    strata: dict[str, Any] = {}
    for r in loc:  # a hard case is scored by what it admitted: a Slovak advert should admit none
        st = strata.setdefault(r["stratum"], {"n": 0, "admitted": 0, "ad_kind": Counter()})
        st["n"] += 1
        st["admitted"] += any(r["location"][k]["value"] for k in text_reading.SLOTS)
        st["ad_kind"][str(r["location"]["ad_kind"]["value"])] += 1
    towns = [r for r in loc if r["location"]["town"]["value"]]
    stored = [r for r in towns if r["label_kind"] == "stored" and r["location_labels"]["town"]]
    streets = [r for r in loc if r["location"]["street"]["value"]]
    location = {
        "n": len(loc),
        "ad_kind": dict(Counter(str(r["location"]["ad_kind"]["value"]) for r in loc)),
        "per_slot": slots, "by_stratum": strata,
        "quote_validity": _rate(sum(len(r["quoted"]) for r in loc),
                                sum(len(r["claimed"]) for r in loc)),
        "town_binds": _rate(sum(bool(r["register"].get("level")) for r in towns), len(towns)),
        "bazos_town_agreement": _rate(sum(agrees(
            "town", r["location"]["town"]["value"], r["location_labels"]["town"])
            for r in stored), len(stored)),
        **{f"street_{k}": _rate(sum(bool(r["register"].get(k)) for r in streets), len(streets))
           for k in ("binds_stored_town", "binds_text_town")},
    }
    measured = {"structured street agreement": (slots["street"]["structured_agreement"], .95),
                "town binds": (location["town_binds"], .90),
                "quote validity": (location["quote_validity"], .95),
                **{f: (per_field[f]["precision"], PRECISION_GATE) for f in ("floor", "has_lift")}}
    gates = {f"{k} >= {bar:.0%}": (v or 0) >= bar for k, (v, bar) in measured.items()}
    latencies = [r["ms"] for r in results if r.get("ms")]
    costs = [r["cost_usd"] for r in results]
    return {
        "n": len(results),
        "errors": sum(1 for r in results if r.get("error")),
        "per_field": per_field,
        **({"location": location, "gates": gates} if loc else {}),
        "p50_ms": round(statistics.median(latencies)) if latencies else None,
        "usd_per_1k_adverts": round(1000 * sum(costs) / len(costs), 3) if costs else None,
        "api_cost_usd": round(sum(costs), 4),
    }


ARM_WORKERS = 8


_CARRIED = ("source", "stratum", "label_kind", "location_labels", "stored", "advert_text")


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
                messages=[{"role": "user", "content": row["advert_text"]}],
            )
        payload = tx._tool_arguments(res) or {}
        text = row["advert_text"]
        values, dropped = tx.merge_extraction(payload, text=text, fields=FIELDS)
        out = {
            "id": row["id"], "labels": row["labels"], "values": values,
            "dropped": dropped, "cost_usd": float(res.cost_usd or 0.0),
            "ms": int((time.monotonic() - started) * 1000),
        }
        if "label_kind" in row:
            block = payload.get("location") if isinstance(payload.get("location"), dict) else {}
            raw = {k: v for k, v in block.items() if isinstance(v, dict) and v.get("value")}
            out.update({k: row[k] for k in _CARRIED}, register={},
                       location=text_reading.read_location(payload, text),
                       claimed=[k for k in text_reading.SLOTS if k in raw],
                       quoted=[k for k in text_reading.SLOTS if k in raw
                               and tx.quote_supports(text, raw[k].get("evidence_quote"))])
        return out
    except Exception as exc:  # noqa: BLE001 — one advert must not end the arm
        LOG.warning("%s listing=%s failed: %s", model, row["id"], str(exc)[:200])
        return {"id": row["id"], "labels": row["labels"], "values": {},
                "dropped": {}, "cost_usd": 0.0, "ms": None, "error": str(exc)[:300]}


def _register(results: list[dict[str, Any]]) -> None:
    """Annotate each location reading with its register facts, in ONE statement."""
    rows = [r for r in results if "location" in r]
    if not rows:
        return
    read = [(r["location"]["town"]["value"], r["location"]["street"]["value"]) for r in rows]
    params = {"ids": [r["id"] for r in rows],
              "towns": [normalize_match_key(t) if t else None for t, _ in read],
              "streets": ["|".join(sorted(street_match_keys(s)[0])) if s else None
                          for _, s in read]}
    columns = ("level", "name", "okres", "km", "namesakes", "pinned", "binds_stored_town",
               "binds_text_town")
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(_REGISTER_SQL, params)
        found = {row[0]: dict(zip(columns, row[1:])) for row in cur.fetchall()}
    for r in rows:
        r["register"] = found.get(r["id"], {})


def run_model(conn: Any, model: str, panel: list[dict[str, Any]],
              *, workers: int = ARM_WORKERS) -> dict[str, Any]:
    """One arm over the panel, `workers` adverts in flight at once — the lane's own
    concurrency. Serial, a 32B model behind one A100 needed ~4 h for 1,387 adverts and
    the job cap cancelled it at page 1,387-minus-something (run 35708090261); both vLLM
    and the API serve eight requests as fast as one. Results keep the panel's order."""
    from concurrent.futures import ThreadPoolExecutor

    del conn  # the panel was built on it; every call below runs on a worker's own
    tool, lane = tx.extraction_tool(FIELDS), contract.extracted_cells()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda row: _extract_one(model, tx.extraction_tool(
            lane.get(row["source"], ())) if "label_kind" in row else tool, row), panel))
    _register(results)
    return {"model": model, **score(results),
            "readings": [r for r in results if "location" in r]}


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
        f"Gate: precision >= {PRECISION_GATE:.0%} where the model answers "
        f"(floor: within +-{FLOOR_TOLERANCE}).",
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
    lines += ["", "| model | $/1k adverts | p50 ms | errors | pod |",
              "| --- | ---: | ---: | ---: | --- |"]
    for arm in report["arms"]:
        pod = arm.get("pod")
        lines.append(
            f"| {arm['model']} | {arm['usd_per_1k_adverts']} | {arm['p50_ms']} | "
            f"{arm['errors']} | "
            + (f"{pod['gpu']} @ ${pod['usd_per_hr']}/hr, ${pod['gpu_hours_usd']} "
               f"({pod['cloud_type']})" if pod else "-") + " |")
    for arm in (a for a in report["arms"] if "location" in a):
        loc = arm["location"]
        lines += ["", f"## Location — {arm['model']}: {loc['n']} adverts, ad_kind "
                  f"{loc['ad_kind']}", "", "| slot | answered | answer rate | quote valid | "
                  "structured labelled | structured agreement |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"]
        lines += [f"| {k} | {v['answered']} | {_pct(v['answer_rate'])} | "
                  f"{_pct(v['quote_valid'])} | {v['structured_labelled']} | "
                  f"{_pct(v['structured_agreement'])} |" for k, v in loc["per_slot"].items()]
        lines += ["", "| stratum | n | admitted a slot | ad_kind |", "| --- | ---: | ---: | --- |"]
        lines += [f"| {k} | {v['n']} | {v['admitted']} | {dict(v['ad_kind'])} |"
                  for k, v in loc["by_stratum"].items()]
        lines += ["", f"Town binds in the register {_pct(loc['town_binds'])}; bazos town = "
                  f"stored town {_pct(loc['bazos_town_agreement'])}; street binds in the "
                  f"stored obec {_pct(loc['street_binds_stored_town'])}, in the text town's "
                  f"obec {_pct(loc['street_binds_text_town'])}.", "", "Gates: " + "; ".join(
                      f"{k} {'PASS' if ok else 'FAIL'}" for k, ok in arm["gates"].items())]
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


def review(report: dict[str, Any]) -> str:
    """The sheet the operator pre-reads: (i) 40 bazos readings (the trigger first, then md5
    order), (ii) up to 30 bazos town disagreements, (iii) every hard case that admitted a
    slot. One advert = one block of list items, so Markdown cannot merge or restyle them."""
    lines: list[str] = []
    for arm in (a for a in report["arms"] if a.get("readings")):
        bazos = sorted((r for r in arm["readings"] if r["label_kind"] == "stored"),
                       key=lambda r: (r["id"] != TRIGGER_ID,
                                      hashlib.md5(str(r["id"]).encode()).hexdigest()))
        lines += [f"# Location review — {arm['model']}", "", "## (i) 40 bazos readings", ""]
        lines += [x for r in bazos[:40] for x in _block(r)]
        disagree = [r for r in bazos if r["location"]["town"]["value"]
                    and r["location_labels"]["town"] and not agrees(
                        "town", r["location"]["town"]["value"], r["location_labels"]["town"])]
        lines += ["## (ii) Town disagreements", ""]
        for r in disagree[:30]:
            reg, town = r["register"], r["location"]["town"]
            km = (f"{reg['km']:.1f} km from the pin" if reg.get("km") is not None
                  else "unit has no point" if reg.get("pinned") else "no pin")
            where = (f"{reg['level']} {reg['name']} (1 of {reg['namesakes']}), okres "
                     f"{reg['okres'] or '-'}, {km}" if reg.get("level") else "NOT IN THE REGISTER")
            lines += [f"### {r['id']}", f"- Stored: {r['stored']['obec_name']} (okres "
                      f"{r['stored']['okres_name'] or '-'})", f"- Text: {town['value']} -> {where}",
                      f'- Quote: "{town["quote"]}"', ""]
        lines += ["## (iii) Hard cases that admitted a slot", ""]
        lines += [x for r in bazos if r["stratum"] not in ("md5", "trigger") and any(
            c["value"] for k, c in r["location"].items() if k != "ad_kind") for x in _block(r)]
    return "\n".join(lines) + "\n"


def _block(r: dict[str, Any]) -> list[str]:
    title, _, body = r["advert_text"].partition("\n")
    read = "; ".join(f'{k} "{c["value"]}" <- "{c["quote"]}"'
                     for k, c in r["location"].items() if k != "ad_kind" and c["value"])
    return [f"### {r['id']} ({r['stratum']}, ad_kind {r['location']['ad_kind']['value']})",
            f"- Headline: {title}", f"- Text: {' '.join(body[:300].split())}",
            f"- Read: {read or '-'}", f"- Stored: {r['stored']['obec_name'] or '-'} / "
            f"{r['location_labels']['street'] or '-'}", ""]


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--per-source", type=int, default=600,
                    help="panel rows per structured portal, stratified per category "
                         "(600 measured 1,207 structured rows; a thin category is not "
                         "redistributed, so the realised n is at or under 2 x this)")
    ap.add_argument("--bazos", type=int, default=300)
    ap.add_argument("--location-rows", type=int, default=100,
                    help="location half: md5 bazos rows (+ the trigger + ~10 %% per hard "
                         "case) and this many per structured portal; 0 skips it")
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
        panel = build_panel(conn, per_source=args.per_source, bazos=args.bazos,
                            location_rows=args.location_rows)
        by_source: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for row in panel:
            by_source[row["source"]] = by_source.get(row["source"], 0) + 1
            key = str(row["category_main"])
            by_category[key] = by_category.get(key, 0) + 1
        LOG.info("PANEL n=%d %s %s strata=%s", len(panel), by_source, by_category,
                 dict(Counter(r["stratum"] for r in panel if "stratum" in r)))
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
        "panel": {"n": len(panel), "by_source": by_source,
                  "by_category": by_category},
        "arms": arms,
    }
    (out_dir / "bakeoff.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out_dir / "review.md").write_text(review(report), encoding="utf-8")
    summary = summarise(report)
    (out_dir / "bakeoff.md").write_text(summary, encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
