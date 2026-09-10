"""NEW DEDUP Level 0 — path C candidate generation: the lane behind
`.github/workflows/new_dedup_candidates.yml`.

Two modes, one SQL (toolkit/dedup_candidates_sql.py):

* `estimate` — READS ONLY. Runs the rung statements as counts, one town at a time, and
  reports how many pairs a generation WOULD write, per rung and per town, for every scope
  asked for (`--scopes all,active`), plus the listing-side funnel and the largest
  (town, disposition) buckets. Needs no migration-492 table. This is what the operator sees
  before a single row exists — the 2026-09-10 (a) ledger entry explains why the number
  matters (Praha at obec grain).
* `generate` — writes. Opens a generation run (toolkit/dedup_candidates.begin_generation:
  one simulation_runs row, the parameter set, the generation row), then for every town block
  and every id range inside it runs the C1 and C3 upserts, recording a resume cursor after
  each chunk. A re-dispatch with `--resume` continues the last running generation of the
  same parameter set from that cursor. After the LAST block: the audit statistics onto the
  generation row and — only when the run covered every town — the stale sweep (rows of this
  parameter set that this generation did not re-produce). `--dry-run` prints the plan.

Scope (all listings vs both-sides-active) is the `l0_candidate_scope` SETTING, so it is part of
the fingerprint: two scopes are two parameter sets and never share pair rows.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Sequence

from scraper import db
from toolkit import dedup_candidates as dc
from toolkit import dedup_candidates_sql as sql

log = logging.getLogger("dedup_candidates")

ID_MAX = 1 << 62
STATEMENT_TIMEOUT = "3600s"


@dataclass(frozen=True)
class Block:
    key: str
    listings: int


def _parse_keys(raw: str | None) -> list[str]:
    return [k.strip() for k in (raw or "").split(",") if k.strip()]


def _scopes(raw: str | None) -> list[str]:
    scopes = _parse_keys(raw) or ["all"]
    bad = [s for s in scopes if s not in ("all", "active")]
    if bad:
        raise SystemExit(f"unknown scope(s) {bad}; use all and/or active")
    return scopes


def _set_timeout(cur: Any) -> None:
    cur.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


def fetch_blocks(conn: Any, *, active_only: bool, only: Sequence[str] = ()) -> list[Block]:
    with conn.transaction(), conn.cursor() as cur:
        _set_timeout(cur)
        cur.execute(sql.BLOCKS_SQL, {"active_only": active_only})
        rows = cur.fetchall()
    blocks = [Block(str(r[0]), int(r[1])) for r in rows]
    if only:
        wanted = set(only)
        blocks = [b for b in blocks if b.key in wanted]
        missing = wanted - {b.key for b in blocks}
        if missing:
            log.warning("blocks not found in the projection (skipped): %s", sorted(missing))
    return blocks


def chunk_ranges(ids: Sequence[int], chunk: int) -> list[tuple[int, int]]:
    """Half-open [id_from, id_to) ranges over a sorted id list, `chunk` ids each; the last
    range runs to ID_MAX so an id that appears between two runs still falls inside one."""
    if not ids:
        return []
    out: list[tuple[int, int]] = []
    for i in range(0, len(ids), chunk):
        id_from = int(ids[i])
        id_to = int(ids[i + chunk]) if i + chunk < len(ids) else ID_MAX
        out.append((id_from, id_to))
    return out


def block_ids(conn: Any, block_key: str, *, active_only: bool) -> list[int]:
    with conn.transaction(), conn.cursor() as cur:
        _set_timeout(cur)
        cur.execute(sql.BLOCK_IDS_SQL, {"block_key": block_key, "active_only": active_only})
        return [int(r[0]) for r in cur.fetchall()]


def _rung_args(params: dict[str, Any], *, block_key: str, id_from: int, id_to: int,
               inputs_id: int, generation_id: int) -> dict[str, Any]:
    return {
        **params,
        "block_key": block_key,
        "id_from": id_from,
        "id_to": id_to,
        "inputs_id": inputs_id,
        "generation_id": generation_id,
    }


# --------------------------------------------------------------------------- estimate


def estimate(conn: Any, inputs: dict[str, Any], *, scopes: list[str], only: Sequence[str],
             top: int) -> dict[str, Any]:
    report: dict[str, Any] = {"path": inputs["path"], "inputs": inputs, "scopes": {}}
    base_params = sql.rung_params(inputs)
    for scope in scopes:
        params = {**base_params, "active_only": scope == "active"}
        blocks = fetch_blocks(conn, active_only=params["active_only"], only=only)
        totals = {rung: 0 for rung in sql.RUNG_SQL}
        per_block: list[dict[str, Any]] = []
        t0 = time.monotonic()
        log.info("estimate scope=%s: %d towns", scope, len(blocks))
        for i, b in enumerate(blocks, 1):
            counts: dict[str, int] = {}
            for rung, stmts in sql.RUNG_SQL.items():
                with conn.transaction(), conn.cursor() as cur:
                    _set_timeout(cur)
                    cur.execute(stmts["count"], _rung_args(
                        params, block_key=b.key, id_from=0, id_to=ID_MAX, inputs_id=0, generation_id=0))
                    counts[rung] = int(cur.fetchone()[0])
                totals[rung] += counts[rung]
            per_block.append({"block_key": b.key, "listings": b.listings, **counts})
            if i % 200 == 0 or b.listings >= 5000:
                log.info("  [%d/%d] town %s listings=%d C1=%d C3=%d (%.0fs)",
                         i, len(blocks), b.key, b.listings, counts["C1"], counts["C3"],
                         time.monotonic() - t0)
        per_block.sort(key=lambda r: -(r["C1"] + r["C3"]))
        top_blocks = per_block[:top]
        keys = [r["block_key"] for r in top_blocks]
        names = _block_names(conn, keys)
        for r in top_blocks:
            r["obec_name"] = names.get(r["block_key"])
        report["scopes"][scope] = {
            "towns": len(blocks),
            "listings": sum(b.listings for b in blocks),
            "pairs": {**totals, "total": sum(totals.values())},
            "top_towns": top_blocks,
            "distribution": _distribution(per_block),
            "seconds": round(time.monotonic() - t0, 1),
        }
        report["scopes"][scope]["funnel"] = fetch_funnel(conn, active_only=params["active_only"])
        report["scopes"][scope]["top_buckets"] = fetch_top_buckets(
            conn, active_only=params["active_only"], limit=top)
    report["town_assignment"] = fetch_town_assignment(conn)
    return report


_DIST_EDGES = ((0, 0), (1, 10), (11, 100), (101, 1_000), (1_001, 10_000), (10_001, 100_000),
               (100_001, 1_000_000), (1_000_001, None))


def _distribution(per_block: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """How many towns fall in each pair-count band — the town-level analogue of the
    pin/clique statistics the design asked for."""
    out = []
    for lo, hi in _DIST_EDGES:
        n = sum(1 for r in per_block if lo <= (r["C1"] + r["C3"]) <= (hi if hi is not None else 10**18))
        out.append({"pairs_from": lo, "pairs_to": hi, "towns": n})
    return out


# --------------------------------------------------------------------------- verify


def oracle_pairs(attrs: Sequence[dc.ListingAttrs], inputs: dict[str, Any]) -> dict[tuple[int, int], dc.PairVerdict]:
    """Every pair the Python rule accepts among `attrs` (all in one town) — O(n²), so verify
    mode caps the town size."""
    out: dict[tuple[int, int], dc.PairVerdict] = {}
    for i, a in enumerate(attrs):
        for b in attrs[i + 1:]:
            v = dc.evaluate_pair(a, b, inputs)
            if v is not None:
                lo, hi = sorted((a.listing_id, b.listing_id))
                out[(lo, hi)] = v
    return out


def sql_pairs(conn: Any, params: dict[str, Any], block_key: str) -> dict[tuple[int, int], dict[str, Any]]:
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for rung, stmts in sql.RUNG_SQL.items():
        with conn.transaction(), conn.cursor() as cur:
            _set_timeout(cur)
            cur.execute(stmts["rows"], _rung_args(
                params, block_key=block_key, id_from=0, id_to=ID_MAX, inputs_id=0, generation_id=0))
            for row in cur.fetchall():
                rec = dict(zip(sql.PAIR_COLUMN_NAMES, row))
                key = (int(rec["listing_id_lo"]), int(rec["listing_id_hi"]))
                if key in out:
                    raise AssertionError(f"pair {key} produced twice ({out[key]['rung']} and {rung})")
                out[key] = rec
    return out


def compare_block(oracle: dict[tuple[int, int], dc.PairVerdict],
                  got: dict[tuple[int, int], dict[str, Any]]) -> dict[str, Any]:
    """The disagreement between the oracle and the SQL on one town, in detail."""
    only_oracle = sorted(set(oracle) - set(got))
    only_sql = sorted(set(got) - set(oracle))
    rung_mismatch = []
    evidence_mismatch = []
    for key in sorted(set(oracle) & set(got)):
        o, g = oracle[key], got[key]
        if o.rung != g["rung"]:
            rung_mismatch.append((key, o.rung, g["rung"]))
            continue
        if bool(o.floor_checked) != bool(g["floor_checked"]):
            evidence_mismatch.append((key, "floor_checked", o.floor_checked, g["floor_checked"]))
        if o.rung == "C1" and o.disposition != g["disposition"]:
            evidence_mismatch.append((key, "disposition", o.disposition, g["disposition"]))
        if o.rung == "C3":
            for name, ov, gv in (("area_lo", o.area_lo, g["area_lo"]), ("area_hi", o.area_hi, g["area_hi"]),
                                 ("area_diff_pct", o.area_diff_pct, g["area_diff_pct"])):
                if ov is None or gv is None or abs(float(ov) - float(gv)) > 1e-6:
                    evidence_mismatch.append((key, name, ov, gv))
    return {
        "oracle_pairs": len(oracle), "sql_pairs": len(got),
        "only_oracle": only_oracle[:20], "only_oracle_n": len(only_oracle),
        "only_sql": only_sql[:20], "only_sql_n": len(only_sql),
        "rung_mismatch": rung_mismatch[:20], "rung_mismatch_n": len(rung_mismatch),
        "evidence_mismatch": evidence_mismatch[:20], "evidence_mismatch_n": len(evidence_mismatch),
        "agree": not (only_oracle or only_sql or rung_mismatch or evidence_mismatch),
    }


def verify(conn: Any, inputs: dict[str, Any], *, only: Sequence[str], max_listings: int) -> dict[str, Any]:
    """Hold the SQL to the oracle on real data: for each named town (≤ max_listings rows),
    the pair set the rung statements return must equal the pair set evaluate_pair accepts,
    rung for rung and evidence for evidence. Reads only."""
    if not only:
        raise SystemExit("verify needs --blocks (one or more obec_kod; small towns)")
    params = sql.rung_params(inputs)
    blocks = fetch_blocks(conn, active_only=params["active_only"], only=only)
    results = []
    for b in blocks:
        if b.listings > max_listings:
            results.append({"block_key": b.key, "listings": b.listings, "skipped": f"> {max_listings} listings"})
            continue
        with conn.transaction(), conn.cursor() as cur:
            _set_timeout(cur)
            cur.execute(sql.BLOCK_ATTRS_SQL, {"block_key": b.key, "active_only": params["active_only"]})
            attrs = [dc.ListingAttrs(int(r[0]), b.key, r[1], r[2], r[3], r[4], r[5], r[6]) for r in cur.fetchall()]
        oracle = oracle_pairs(attrs, inputs)
        got = sql_pairs(conn, params, b.key)
        cmp = compare_block(oracle, got)
        results.append({"block_key": b.key, "listings": len(attrs), **cmp})
        log.info("verify town %s: %d listings, oracle %d pairs, sql %d pairs, agree=%s",
                 b.key, len(attrs), cmp["oracle_pairs"], cmp["sql_pairs"], cmp["agree"])
    return {"path": inputs["path"], "inputs": inputs, "blocks": results,
            "agree": all(r.get("agree", False) for r in results if "skipped" not in r) and any("skipped" not in r for r in results)}


# --------------------------------------------------------------------------- stats


def _rows(conn: Any, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with conn.transaction(), conn.cursor() as cur:
        _set_timeout(cur)
        cur.execute(statement, params or {})
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def fetch_funnel(conn: Any, *, active_only: bool) -> list[dict[str, Any]]:
    return _rows(conn, sql.FUNNEL_SQL, {"active_only": active_only})


def fetch_town_assignment(conn: Any) -> list[dict[str, Any]]:
    return _rows(conn, sql.TOWN_ASSIGNMENT_SQL)


def fetch_top_buckets(conn: Any, *, active_only: bool, limit: int) -> list[dict[str, Any]]:
    return _rows(conn, sql.TOP_BUCKETS_SQL, {"active_only": active_only, "limit": limit})


def _block_names(conn: Any, keys: Sequence[str]) -> dict[str, str | None]:
    if not keys:
        return {}
    return {r["obec_kod"]: r["obec_name"] for r in _rows(conn, sql.BLOCK_NAMES_SQL, {"keys": list(keys)})}


def generation_stats(conn: Any, gen: dc.Generation, *, top: int) -> dict[str, Any]:
    """The audit numbers, computed once over the finished store and written onto the
    generation row — the Candidate audit page never scans the pair table itself."""
    params = sql.rung_params(gen.inputs)
    per_block_rows = _rows(conn, sql.PAIRS_PER_BLOCK_SQL, {"inputs_id": gen.inputs_id})
    per_block: dict[str, dict[str, Any]] = {}
    for r in per_block_rows:
        entry = per_block.setdefault(r["block_key"], {"block_key": r["block_key"], "C1": 0, "C3": 0})
        entry[r["rung"]] = int(r["pairs"])
    ranked = sorted(per_block.values(), key=lambda r: -(r["C1"] + r["C3"]))
    top_blocks = ranked[:top]
    names = _block_names(conn, [r["block_key"] for r in top_blocks])
    for r in top_blocks:
        r["obec_name"] = names.get(r["block_key"])
    matrix = _rows(conn, sql.PAIR_MATRIX_SQL, {"inputs_id": gen.inputs_id})
    return {
        "matrix": [{**m, "pairs": int(m["pairs"]), "floor_checked": int(m["floor_checked"])} for m in matrix],
        "pairs": {
            "C1": sum(int(m["pairs"]) for m in matrix if m["rung"] == "C1"),
            "C3": sum(int(m["pairs"]) for m in matrix if m["rung"] == "C3"),
            "total": sum(int(m["pairs"]) for m in matrix),
        },
        "listings_with_candidates": [
            {"category_main": r["category_main"], "listings": int(r["listings"])}
            for r in _rows(conn, sql.LISTINGS_WITH_CANDIDATES_SQL, {"inputs_id": gen.inputs_id})
        ],
        "towns_with_pairs": len(per_block),
        "top_towns": top_blocks,
        "distribution": _distribution(ranked),
        "funnel": fetch_funnel(conn, active_only=params["active_only"]),
        "top_buckets": fetch_top_buckets(conn, active_only=params["active_only"], limit=top),
        "town_assignment": fetch_town_assignment(conn),
    }


# --------------------------------------------------------------------------- generate


def generate(conn: Any, path: str, *, only: Sequence[str], chunk: int, resume: bool,
             top: int, dry_run: bool) -> dict[str, Any]:
    inputs = dc.inputs_for_path(path, conn)
    fp = dc.fingerprint(inputs)
    params = sql.rung_params(inputs)
    blocks = fetch_blocks(conn, active_only=params["active_only"], only=only)
    partial = bool(only)
    plan = {
        "path": path, "fingerprint": fp, "inputs": inputs, "towns": len(blocks),
        "listings": sum(b.listings for b in blocks), "chunk": chunk, "partial": partial,
        "estimated_chunks": sum(-(-b.listings // chunk) for b in blocks),
    }
    if dry_run:
        log.info("DRY RUN — plan: %s", json.dumps(plan, ensure_ascii=False))
        return {"dry_run": True, **plan}

    gen = dc.latest_generation(conn, path, fingerprint_=fp, status="running") if resume else None
    if gen is not None:
        log.info("resuming generation %s (run %s) from %s", gen.id, gen.simulation_run_id, gen.progress)
        progress = dict(gen.progress or {})
    else:
        progress = {"blocks_total": len(blocks), "blocks_done": 0, "chunks_done": 0,
                    "pairs_upserted": 0, "last_block_key": None, "last_id_to": None,
                    "partial": partial, "only": list(only), "chunk": chunk,
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        gen = dc.begin_generation(conn, path, progress=progress)
        log.info("generation %s opened (run %s, inputs %s, fingerprint %s)",
                 gen.id, gen.simulation_run_id, gen.inputs_id, fp)

    last_key = progress.get("last_block_key")
    last_id_to = progress.get("last_id_to")
    t0 = time.monotonic()
    try:
        for b in blocks:
            if last_key is not None and b.key < last_key:
                continue
            ids = block_ids(conn, b.key, active_only=params["active_only"])
            if last_key is not None and b.key == last_key and last_id_to is not None:
                ids = [i for i in ids if i >= int(last_id_to)]
                if last_id_to >= ID_MAX:
                    ids = []
            for id_from, id_to in chunk_ranges(ids, chunk):
                upserted = 0
                for rung, stmts in sql.RUNG_SQL.items():
                    with conn.transaction(), conn.cursor() as cur:
                        _set_timeout(cur)
                        cur.execute(stmts["insert"], _rung_args(
                            params, block_key=b.key, id_from=id_from, id_to=id_to,
                            inputs_id=gen.inputs_id, generation_id=gen.id))
                        upserted += max(cur.rowcount, 0)
                progress.update({
                    "chunks_done": int(progress.get("chunks_done", 0)) + 1,
                    "pairs_upserted": int(progress.get("pairs_upserted", 0)) + upserted,
                    "last_block_key": b.key, "last_id_to": id_to,
                })
                dc.record_progress(conn, gen.id, progress)
            progress["blocks_done"] = int(progress.get("blocks_done", 0)) + 1
            progress["last_block_key"], progress["last_id_to"] = b.key, ID_MAX
            dc.record_progress(conn, gen.id, progress)
            if b.listings >= 5000 or progress["blocks_done"] % 200 == 0:
                log.info("  town %s done (%d listings) — %d/%d towns, %d pairs upserted, %.0fs",
                         b.key, b.listings, progress["blocks_done"], len(blocks),
                         progress["pairs_upserted"], time.monotonic() - t0)
            last_key, last_id_to = None, None

        stale_deleted = 0
        if not partial:
            with conn.transaction(), conn.cursor() as cur:
                _set_timeout(cur)
                cur.execute(sql.STALE_SWEEP_SQL, {"inputs_id": gen.inputs_id, "generation_id": gen.id})
                stale_deleted = max(cur.rowcount, 0)
        log.info("computing statistics …")
        stats = generation_stats(conn, gen, top=top)
        stats.update({
            "partial": partial, "only": list(only), "stale_deleted": stale_deleted,
            "chunks_done": progress["chunks_done"], "pairs_upserted": progress["pairs_upserted"],
            "seconds": round(time.monotonic() - t0, 1), "scope": inputs.get("l0_candidate_scope", "all"),
        })
        dc.finish_generation(conn, gen, status="success", stats=stats, progress=progress)
        log.info("generation %s SUCCESS: %s pairs (C1 %s, C3 %s), %d stale removed",
                 gen.id, stats["pairs"]["total"], stats["pairs"]["C1"], stats["pairs"]["C3"], stale_deleted)
        return {"generation_id": gen.id, "simulation_run_id": gen.simulation_run_id, **stats}
    except Exception as exc:  # noqa: BLE001 — the row must record the failure
        log.exception("generation %s FAILED", gen.id)
        try:
            dc.finish_generation(conn, gen, status="failed", error=str(exc)[:2000], progress=progress)
        except Exception:  # noqa: BLE001
            log.exception("could not record the failure on generation %s", gen.id)
        raise


# --------------------------------------------------------------------------- reporting


def _fmt(n: Any) -> str:
    return f"{int(n):,}" if isinstance(n, (int, float)) and n is not None else str(n)


def estimate_markdown(report: dict[str, Any]) -> str:
    lines = [f"## Path {report['path']} — estimate (nothing written)", ""]
    lines.append("Inputs: `" + json.dumps(report["inputs"], ensure_ascii=False) + "`")
    lines.append("")
    lines.append("| scope | towns | listings | C1 pairs | C3 pairs | total | seconds |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for scope, s in report["scopes"].items():
        p = s["pairs"]
        lines.append(f"| {scope} | {_fmt(s['towns'])} | {_fmt(s['listings'])} | {_fmt(p['C1'])} | "
                     f"{_fmt(p['C3'])} | **{_fmt(p['total'])}** | {s['seconds']} |")
    for scope, s in report["scopes"].items():
        lines += ["", f"### scope `{scope}` — largest towns by pairs", "",
                  "| obec_kod | town | listings | C1 | C3 |", "| --- | --- | ---: | ---: | ---: |"]
        for r in s["top_towns"]:
            lines.append(f"| {r['block_key']} | {r.get('obec_name') or ''} | {_fmt(r['listings'])} | "
                         f"{_fmt(r['C1'])} | {_fmt(r['C3'])} |")
        lines += ["", f"### scope `{scope}` — towns by pair count", "",
                  "| pairs | towns |", "| --- | ---: |"]
        for d in s["distribution"]:
            hi = "+" if d["pairs_to"] is None else f"–{_fmt(d['pairs_to'])}"
            lines.append(f"| {_fmt(d['pairs_from'])}{hi} | {_fmt(d['towns'])} |")
        lines += ["", f"### scope `{scope}` — largest (town, disposition) buckets", "",
                  "| town | disposition | listings | active |", "| --- | --- | ---: | ---: |"]
        for r in s["top_buckets"]:
            lines.append(f"| {r.get('obec_name') or r['obec_kod']} | {r['disposition'] or '(none)'} | "
                         f"{_fmt(r['listings'])} | {_fmt(r['active'])} |")
        lines += ["", f"### scope `{scope}` — funnel per portal × type", "",
                  "| source | type | deal | listings | with town | with dispo | with area | C1-eligible | C3-eligible | town, no attribute |",
                  "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for r in s["funnel"]:
            lines.append(f"| {r['source']} | {r['category_main'] or '—'} | {r['category_type'] or '—'} | "
                         f"{_fmt(r['listings'])} | {_fmt(r['with_town'])} | {_fmt(r['with_disposition'])} | "
                         f"{_fmt(r['with_area'])} | {_fmt(r['c1_eligible'])} | {_fmt(r['c3_eligible'])} | "
                         f"{_fmt(r['town_no_attribute'])} |")
    lines += ["", "### how the town was assigned (path C population)", "", "| method | listings |", "| --- | ---: |"]
    for r in report["town_assignment"]:
        lines.append(f"| {r['method']} | {_fmt(r['listings'])} |")
    return "\n".join(lines) + "\n"


def generate_markdown(result: dict[str, Any]) -> str:
    if result.get("dry_run"):
        return "## Path C — generate DRY RUN\n\n```json\n" + json.dumps(result, indent=2, ensure_ascii=False) + "\n```\n"
    p = result["pairs"]
    lines = [f"## Path C — generation {result['generation_id']} (run {result['simulation_run_id']})", "",
             f"scope `{result['scope']}`, partial={result['partial']}, {_fmt(result['chunks_done'])} chunks, "
             f"{_fmt(result['pairs_upserted'])} upserts, {_fmt(result['stale_deleted'])} stale removed, "
             f"{result['seconds']} s", "",
             f"**{_fmt(p['total'])} pairs** — C1 {_fmt(p['C1'])}, C3 {_fmt(p['C3'])}; "
             f"{_fmt(result['towns_with_pairs'])} towns with pairs", "",
             "| rung | type lo | type hi | deal | pairs | floor checked |", "| --- | --- | --- | --- | ---: | ---: |"]
    for m in sorted(result["matrix"], key=lambda m: -m["pairs"]):
        lines.append(f"| {m['rung']} | {m['category_main_lo'] or '—'} | {m['category_main_hi'] or '—'} | "
                     f"{m['category_type'] or '—'} | {_fmt(m['pairs'])} | {_fmt(m['floor_checked'])} |")
    lines += ["", "| type | listings with ≥ 1 candidate |", "| --- | ---: |"]
    for r in result["listings_with_candidates"]:
        lines.append(f"| {r['category_main'] or '—'} | {_fmt(r['listings'])} |")
    return "\n".join(lines) + "\n"


def verify_markdown(result: dict[str, Any]) -> str:
    lines = [f"## Path {result['path']} — verify (SQL vs the Python oracle, nothing written)", "",
             f"**{'AGREE' if result['agree'] else 'DISAGREE'}**", "",
             "| obec_kod | listings | oracle pairs | SQL pairs | only oracle | only SQL | rung mismatch | evidence mismatch |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for r in result["blocks"]:
        if "skipped" in r:
            lines.append(f"| {r['block_key']} | {_fmt(r['listings'])} | skipped: {r['skipped']} | | | | | |")
            continue
        lines.append(f"| {r['block_key']} | {_fmt(r['listings'])} | {_fmt(r['oracle_pairs'])} | {_fmt(r['sql_pairs'])} | "
                     f"{_fmt(r['only_oracle_n'])} | {_fmt(r['only_sql_n'])} | {_fmt(r['rung_mismatch_n'])} | "
                     f"{_fmt(r['evidence_mismatch_n'])} |")
    for r in result["blocks"]:
        for field in ("only_oracle", "only_sql", "rung_mismatch", "evidence_mismatch"):
            if r.get(field):
                lines += ["", f"### {r['block_key']} — {field} (first {len(r[field])})", "", "```",
                          *[str(x) for x in r[field]], "```"]
    return "\n".join(lines) + "\n"


def _emit_summary(markdown: str) -> None:
    print(markdown)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown)


# --------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default="C", choices=sorted(dc.PATHS))
    ap.add_argument("--mode", required=True, choices=["estimate", "verify", "generate"])
    ap.add_argument("--max-listings", type=int, default=3000,
                    help="verify only: skip a town with more listings than this (the oracle is O(n²))")
    ap.add_argument("--scopes", default="all,active",
                    help="estimate only: which scopes to count (all, active, or both)")
    ap.add_argument("--blocks", default="", help="comma-separated obec_kod list to limit the run (a pilot)")
    ap.add_argument("--chunk", type=int, default=2000, help="generate: listing ids per upsert statement")
    ap.add_argument("--top", type=int, default=30, help="how many towns / buckets to list")
    ap.add_argument("--resume", action="store_true", help="generate: continue the last running generation")
    ap.add_argument("--dry-run", action="store_true", help="generate: print the plan and exit")
    ap.add_argument("--json-out", default="", help="also write the full report as JSON to this path")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    args = build_parser().parse_args(argv)
    only = _parse_keys(args.blocks)
    if args.chunk < 1:
        raise SystemExit("--chunk must be >= 1")
    with db.connect() as conn:
        if args.mode == "estimate":
            report = estimate(conn, dc.inputs_for_path(args.path, conn), scopes=_scopes(args.scopes),
                              only=only, top=args.top)
            _emit_summary(estimate_markdown(report))
        elif args.mode == "verify":
            report = verify(conn, dc.inputs_for_path(args.path, conn), only=only, max_listings=args.max_listings)
            _emit_summary(verify_markdown(report))
            if not report["agree"]:
                log.error("verify: SQL and oracle DISAGREE")
                return 1
        else:
            report = generate(conn, args.path, only=only, chunk=args.chunk, resume=args.resume,
                              top=args.top, dry_run=args.dry_run)
            _emit_summary(generate_markdown(report))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
