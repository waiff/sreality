"""AUTODEDUP — what the Progress page reads (docs/design/autodedup/PROGRAM.md §12, W1).

Read-only, admin-gated, and the first UI of the program: one row per ITERATION of any wave,
newest first, plus the header strip's spend-to-date against the $200 program cap (D2). The
numbers are read from `autodedup.iterations` exactly as the lane wrote them — cost comes from
`llm_calls` at lane time (E32) and is never forecast here.

The store lives in migration 528. Until it is applied every route answers 200 with
`store_ready: false` and `data: null`, so an un-migrated database shows "the store is not
created yet" rather than a 500 — the probe-first idiom of api/routes/new_dedup_candidates.py.

W5 adds the validation UI's reads on top: proposed GROUPS (clusters + members + edge
evidence), the RESIDUAL pairs the engine did not join, one PAIR's full evidence, and the one
write this program's API owns — the operator's verdict (`autodedup.verdicts`, plus a permanent
`autodedup.must_not_link` row on a negative pair verdict, dropped again when the operator
reverses that verdict, §9's feedback loop). That write stays
inside schema `autodedup`: shadow mode (D4) is untouched, no production table is written, and
`listings` / `images` are read for display only.

PII (E28): no broker column is selected anywhere (autodedup/ui_sql.py), and a description
reaches a response only through `autodedup.judge.listing_digest`, which scrubs it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api import dependencies as deps
from autodedup import progress_sql as psql
from autodedup import ui_sql as usql
from autodedup.dataset import Listing, hamming64
from autodedup.judge import listing_digest
from autodedup.model import LogisticModel, hand_initialised

try:  # the two SQLSTATEs a missing store raises, if the catalog probe ever misses it
    from psycopg import errors as _pg_errors

    _MISSING_RELATION: tuple[type[BaseException], ...] = (
        _pg_errors.UndefinedTable,
        _pg_errors.InvalidSchemaName,
    )
    # Migration 532 widened the verdict domain. A store that predates it rejects the new
    # value with a CHECK violation, which is a MISSING MIGRATION and not a 500.
    _CHECK_VIOLATION: tuple[type[BaseException], ...] = (_pg_errors.CheckViolation,)
except Exception:  # noqa: BLE001 — psycopg absent (tests run on fake connections)
    _MISSING_RELATION = ()
    _CHECK_VIOLATION = ()

router = APIRouter(
    prefix="/autodedup",
    tags=["autodedup"],
    dependencies=[Depends(deps.require_admin)],
)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# D2, the program's spend gate: $25 is the hard per-run cap wired into a lane's `--max-usd`,
# $200 the total for the whole program. They are served next to the spend so the header strip
# reads the cap from the server that also reports the dollars, not from a number retyped in
# the page.
RUN_CAP_USD = 25.0
PROGRAM_CAP_USD = 200.0


def _not_ready() -> dict[str, Any]:
    return {"data": None, "store_ready": False}


def store_ready(conn: Any) -> bool:
    """Does the store of migration 528 exist? `to_regclass` answers NULL for a missing
    relation instead of raising, and is asked BEFORE any other query."""
    with conn.cursor() as cur:
        cur.execute(psql.AUTODEDUP_STORE_READY_SQL)
        row = cur.fetchone()
    return bool(row and row[0])


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _row(columns: tuple[str, ...], row: tuple[Any, ...]) -> dict[str, Any]:
    return {name: _jsonable(value) for name, value in zip(columns, row)}


def _fetch(conn: Any, sql: str, params: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        return list(cur.fetchall())


@router.get("/iterations")
def iterations(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    after: int | None = Query(None),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """One keyset page of the program ledger, newest first.

    `after` is the previous page's `next_after_id`; absent, the first page. One row over the
    asked-for size is fetched and dropped, so `has_more` is a fact rather than the guess a
    full page would be at the exact end of the table.
    """
    if not store_ready(conn):
        return _not_ready()
    try:
        rows = _fetch(
            conn,
            psql.AUTODEDUP_ITERATIONS_SQL,
            {"after_id": after, "limit": limit + 1},
        )
    except _MISSING_RELATION:
        return _not_ready()
    has_more = len(rows) > limit
    items = [_row(psql.ITERATION_COLUMNS, r) for r in rows[:limit]]
    return {
        "data": {
            "items": items,
            "has_more": has_more,
            "next_after_id": items[-1]["id"] if (items and has_more) else None,
        },
        "store_ready": True,
    }


@router.get("/stats")
def stats(conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    """The header strip: iterations run, dollars spent to date against the D2 caps, when the
    last pass moved, and the per-wave breakdown. Summed from the one per-wave statement so the
    headline and the table cannot disagree.

    "Waves closed vs open" (§12) is deliberately NOT here: closing a wave is a gate decision
    taken in PROGRAM.md §14, not a state the ledger carries — `status` is per iteration, and a
    wave whose every pass read `done` may still be open. Mode is likewise a constant of the
    program (SHADOW, D4), not a column.
    """
    if not store_ready(conn):
        return _not_ready()
    try:
        rows = _fetch(conn, psql.AUTODEDUP_STATS_SQL)
        engine = _engine_stats(conn)
    except _MISSING_RELATION:
        return _not_ready()
    waves: list[dict[str, Any]] = []
    n_iterations = 0
    total_cost = Decimal("0")
    last_at: Any = None
    for r in rows:
        row = dict(zip(psql.WAVE_COLUMNS, r))
        n = int(row["n"] or 0)
        # Summed as the numeric(10,4) it is stored as, floated once at the end: adding the
        # floats of many waves is not the same number as adding the numerics.
        raw = row["cost_usd"]
        cost_exact = raw if isinstance(raw, Decimal) else Decimal(str(raw or 0))
        cost = float(cost_exact)
        n_iterations += n
        total_cost += cost_exact
        # Compared as timestamps, not as their rendered strings: the isoformat of two
        # offsets orders lexicographically by the wrong key.
        if row["last_at"] is not None and (last_at is None or row["last_at"] > last_at):
            last_at = row["last_at"]
        waves.append(
            {
                "wave": row["wave"],
                "n": n,
                "last_status": row["last_status"],
                "cost_usd": cost,
            }
        )
    return {
        "data": {
            "n_iterations": n_iterations,
            "total_cost_usd": round(float(total_cost), 4),
            "run_cap_usd": RUN_CAP_USD,
            "program_cap_usd": PROGRAM_CAP_USD,
            "last_iteration_at": _jsonable(last_at),
            "waves": waves,
            # W5: what the ENGINE has produced, beside what the program has spent. Nested
            # under one key so the header strip's existing fields keep their shape.
            "engine": engine,
        },
        "store_ready": True,
    }


# ============================================================ the validation UI (W5, §12)

DEFAULT_GENERATION = "g1"
GROUP_PAGE_SIZE = 25
GROUP_MAX_PAGE_SIZE = 100
RESIDUAL_MIN_SCORE = 0.20
IMAGES_PER_LISTING = 30
# The card gallery (§12): enough frames to page a member on the queue card itself, far
# short of the album the dialog opens — a group card renders four members at once.
GROUP_CARD_IMAGES = 12
TOP_FEATURES = 5

# The pair-level evidence bitmask, mirroring `autodedup.score_lane.FAMILY_BITS` — the lane
# writes the integer, this decodes it, and the two are one contract (PROGRAM.md §5/E11).
FAMILY_BITS: tuple[tuple[str, int], ...] = (
    ("ATTR", 1),
    ("PRICE", 2),
    ("TXT", 4),
    ("BRK", 8),
    ("LOC", 16),
    ("IMG", 32),
    ("TIME", 64),
)

VERDICT_VALUES: tuple[str, ...] = (
    "same",
    "different",
    "same_building_different_unit",
    # E49, migration 532: a DIFFERENT BUILDING of the same development project. `different`
    # throws the project away, `same_building_different_unit` claims a building the adverts do
    # not share — and both would lose the one fact the operator actually established.
    "same_project_different_unit",
    "unsure",
)
# A negative verdict is what writes the permanent must-not-link (§9): "unsure" is not one.
NEGATIVE_VERDICTS: frozenset[str] = frozenset(
    {"different", "same_building_different_unit", "same_project_different_unit"}
)
# What a whole-cluster SPLIT may say about two members the operator put in different units.
# "same" is not offered: two different units are never one property (E1).
SPLIT_RELATIONS: tuple[str, ...] = (
    "same_building_different_unit",
    "same_project_different_unit",
    "different",
)
# A unit per letter of the alphabet. Past that the operator is not splitting a group, they are
# rejecting it — and the whole-cluster `different` verdict says that in one click.
MAX_SPLIT_UNITS = 26
VERDICT_FILTER_VALUES: tuple[str, ...] = ("unreviewed", *VERDICT_VALUES)
ZONE_VALUES: tuple[str, ...] = ("merge", "band", "reject")
VERDICT_KINDS: tuple[str, ...] = ("pair", "cluster")

# Server-side filter registries — a key not listed here is a 400, never a silently ignored
# query param (the AUDIT_BUCKETS idiom). Sort keys map to the ONE statement that serves them,
# so an ordering can never arrive as text off the wire.
GROUP_SORTS: dict[str, str] = {
    "weakest": usql.GROUPS_WEAKEST_SQL,
    "newest": usql.GROUPS_NEWEST_SQL,
    "largest": usql.GROUPS_LARGEST_SQL,
}
RESIDUAL_SORTS: dict[str, str] = {"score_desc": usql.RESIDUAL_SQL}

GROUP_FILTER_KEYS: frozenset[str] = frozenset(
    {
        "generation", "after", "limit", "block", "block_grain", "source", "category_main",
        "category_type", "min_size", "max_size", "min_score", "max_score", "verdict",
        "shared_photo", "has_judgement", "sort",
    }
)
RESIDUAL_FILTER_KEYS: frozenset[str] = frozenset(
    {
        "generation", "after", "limit", "block", "block_grain", "zone", "min_score",
        "source_pair", "has_judgement", "verdict", "sort",
    }
)
DETAIL_FILTER_KEYS: frozenset[str] = frozenset({"generation"})
BLOCK_FILTER_KEYS: frozenset[str] = frozenset({"generation"})

# How many blocks the picker is offered. A generation's vocabulary is the busiest blocks
# first: a select with thousands of options is not a control anyone uses, and the page keeps
# a key that arrived in a URL whether or not the cap listed it — so the cap costs no filter.
BLOCKS_LIMIT = 200

_MODELS_DIR = Path(__file__).resolve().parents[2] / "autodedup" / "models"
_MODEL_CACHE: dict[str, Any] = {}


# --------------------------------------------------------------------------- small helpers


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _reject_unknown_filters(request: Request, allowed: frozenset[str]) -> None:
    unknown = sorted(set(request.query_params) - allowed)
    if unknown:
        raise _bad(f"unknown filter: {', '.join(unknown)}")


def _one_of(name: str, value: str | None, allowed: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    if value not in allowed:
        raise _bad(f"{name} must be one of: {', '.join(allowed)}")
    return value


def _flag(name: str, value: int | None) -> bool | None:
    """`0|1` off the wire -> the boolean the statement's `::boolean` arm compares."""
    if value is None:
        return None
    if value not in (0, 1):
        raise _bad(f"{name} must be 0 or 1")
    return bool(value)


def _json_safe(value: Any) -> Any:
    """`_jsonable`, but through containers — a digest is nested dicts and lists."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return _jsonable(value)


def _rows(columns: tuple[str, ...], rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [_row(columns, r) for r in rows]


def _families(mask: Any) -> list[str]:
    bits = int(mask or 0)
    return [name for name, bit in FAMILY_BITS if bits & bit]


def _certificate(decision: Any) -> str | None:
    """`certificate:K-A` / `certificate:K-A:evidence_gate` -> `K-A`; anything else -> None.

    The certificate is not a column of `autodedup.pairs`: `decide.decide_pair` spells it into
    the reason string, and this is the single place that reads it back out."""
    text = str(decision or "")
    parts = text.split(":")
    return parts[1] if len(parts) >= 2 and parts[0] == "certificate" and parts[1] else None


def _why_not_merged(zone: Any, decision: Any, veto: Any) -> str:
    text = str(decision or "")
    if veto:
        return f"hard guard refused the pair: {veto}"
    if text.startswith("auto_reject:"):
        return f"auto-rejected on {text.split(':', 1)[1]}"
    if text.endswith(":evidence_gate") or text == "evidence_gate":
        return "held in the band: fewer than two independent evidence families"
    if zone == "band":
        return "scored inside the review band — above the reject floor, below auto-merge"
    if zone == "reject":
        return "scored below the review band"
    if zone == "merge":
        return "the edge was accepted, but no cluster of this generation holds both listings"
    return "not accepted"


def _feats(raw: Any) -> dict[str, tuple[float, bool]]:
    """The stored compact `{name: [value, present]}` back into the model's `Feats` shape."""
    out: dict[str, tuple[float, bool]] = {}
    if not isinstance(raw, dict):
        return out
    for name, entry in raw.items():
        if isinstance(entry, (list, tuple)) and entry:
            value: Any = entry[0]
            present = bool(entry[1]) if len(entry) > 1 else True
        else:
            value, present = entry, True
        try:
            out[str(name)] = (float(value), bool(present))
        except (TypeError, ValueError):
            continue
    return out


def _load_model(version: Any) -> Any:
    """The model a pair was scored by, for the legible contribution breakdown (E21).

    A version with no file on disk and no `hand` prefix yields None, and the caller falls back
    to the highest present features — a breakdown attributed to the WRONG model would be worse
    than no breakdown at all."""
    key = str(version or "hand_v1")
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached
    model: Any = None
    if "/" not in key and ".." not in key:
        path = _MODELS_DIR / f"{key}.json"
        try:
            if path.is_file():
                model = LogisticModel.from_json(path.read_text(encoding="utf-8"))
            elif key.startswith("hand"):
                model = hand_initialised()
        except Exception:  # noqa: BLE001 — a malformed model file must not 500 the page
            model = None
    # Only a HIT is cached: a model file that lands in the image after boot (or a version the
    # lane writes before its file ships) would otherwise stay missing until the API restarts.
    if model is not None:
        _MODEL_CACHE[key] = model
    return model


def _fold_presence(contributions: dict[str, float]) -> dict[str, float]:
    """`name` + `name:present` are one feature's log-odds, not two rows.

    `LogisticModel.contributions` reports the value term and the presence term apart. Ranked
    apart they compete for the same five slots, and the presence half arrives with no value to
    show: under the hand prior its 0.1-0.3 weights outrank most value terms and the breakdown
    fills up with blank duplicate rows. Summed, each feature appears once with its whole
    contribution. An interaction (`left*right`) is its own term and stays its own row."""
    folded: dict[str, float] = {}
    for name, value in contributions.items():
        base = name[: -len(":present")] if name.endswith(":present") else name
        folded[base] = folded.get(base, 0.0) + float(value)
    return folded


def _top_features(
    feats: dict[str, tuple[float, bool]], model_version: Any, limit: int = TOP_FEATURES
) -> list[dict[str, Any]]:
    model = _load_model(model_version)
    if model is not None:
        try:
            contributions = _fold_presence(model.contributions(feats))
        except Exception:  # noqa: BLE001 — a feature vocabulary mismatch is not a 500
            contributions = {}
        # A term that contributed nothing is not evidence: the model reports every feature of
        # its `feature_order`, and an absent one lands at exactly 0 with no value to show.
        ranked = sorted(
            ((name, c) for name, c in contributions.items() if c),
            key=lambda kv: abs(kv[1]),
            reverse=True,
        )
        if ranked:
            out: list[dict[str, Any]] = []
            for name, contribution in ranked[:limit]:
                value, present = feats.get(name, (None, None))
                out.append(
                    {
                        "name": name,
                        "value": value,
                        "present": present,
                        "contribution": round(float(contribution), 4),
                    }
                )
            return out
    present_feats = [(name, value) for name, (value, flag) in feats.items() if flag]
    present_feats.sort(key=lambda kv: abs(kv[1]), reverse=True)
    return [
        {"name": name, "value": value, "present": True, "contribution": None}
        for name, value in present_feats[:limit]
    ]


def _cover(storage_path: Any, sreality_url: Any) -> dict[str, Any] | None:
    if storage_path is None and sreality_url is None:
        return None
    return {"storage_path": storage_path, "sreality_url": sreality_url}


def _cursor(*parts: Any) -> str:
    return "|".join("" if part is None else str(part) for part in parts)


def _split_cursor(raw: str | None, arity: int) -> list[str] | None:
    if raw is None:
        return None
    parts = raw.split("|")
    if len(parts) != arity or not all(parts):
        raise _bad("after is not a cursor from this endpoint")
    return parts


def _as_float(text: str) -> float:
    try:
        return float(text)
    except ValueError as exc:
        raise _bad("after is not a cursor from this endpoint") from exc


def _as_int(text: str) -> int:
    try:
        return int(text)
    except ValueError as exc:
        raise _bad("after is not a cursor from this endpoint") from exc


def _as_stamp(text: str) -> str:
    """The timestamp half of a cursor, validated here rather than by Postgres — an unparsable
    one reaching `%(after_ts)s::timestamptz` is a 500 where the numeric sorts answer 400."""
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise _bad("after is not a cursor from this endpoint") from exc
    return text


def _execute(conn: Any, sql: str, params: dict[str, Any]) -> None:
    """A write with no RETURNING — fetching one would raise "didn't produce a result"."""
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _member_row(row: dict[str, Any]) -> dict[str, Any]:
    member = {
        key: row[key]
        for key in (
            "listing_id", "source", "source_url", "category_main", "category_type",
            "disposition", "area_m2", "floor", "price_czk", "first_seen_at", "last_seen_at",
            "is_active", "n_images",
        )
    }
    member["cover"] = _cover(row["cover_storage_path"], row["cover_sreality_url"])
    # The card gallery, `images[0]` being the very frame `cover` names. The DETAIL route
    # replaces it with the full 30-frame album — same key, more frames, one client shape.
    member["images"] = _json_safe(row.get("images") or [])
    return member


def _side(row: dict[str, Any], prefix: str, listing_id: int) -> dict[str, Any]:
    side = {
        field: row[f"{prefix}{field}"]
        for field in (
            "source", "source_url", "category_main", "category_type", "disposition",
            "area_m2", "floor", "total_floors", "price_czk", "first_seen_at", "last_seen_at",
            "is_active", "n_images",
        )
    }
    side["listing_id"] = listing_id
    side["cover"] = _cover(row[f"{prefix}cover_storage_path"], row[f"{prefix}cover_sreality_url"])
    return side


def _cluster_row(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The cluster, and the operator's latest verdict on it, as two objects."""
    verdict = (
        {
            "verdict": row["verdict"],
            "note": row["verdict_note"],
            "decided_by": row["verdict_decided_by"],
            "decided_at": row["verdict_decided_at"],
        }
        if row["verdict"] is not None
        else None
    )
    cluster = {
        key: value
        for key, value in row.items()
        if key not in ("verdict", "verdict_note", "verdict_decided_by", "verdict_decided_at")
    }
    cluster["evidence_family_names"] = _families(cluster.get("evidence_families"))
    return cluster, verdict


def _pair_view(row: dict[str, Any]) -> dict[str, Any]:
    pair = dict(row)
    pair["certificate"] = _certificate(row["decision"])
    pair["family_names"] = _families(row["families"])
    pair["why_not_merged"] = _why_not_merged(row["zone"], row["decision"], row["guard_veto"])
    return pair


def _digest(row: dict[str, Any]) -> dict[str, Any]:
    """`autodedup.judge.listing_digest` over a DB row — the SAME PII-free record the judge
    sees, so the operator reads exactly what the model was shown (and never a broker field)."""
    attrs = {
        key: row[key]
        for key in (
            "price_unit", "area_basis", "has_balcony", "has_parking", "has_lift",
            "building_type", "condition", "energy_rating", "estate_area", "usable_area",
            "garden_area", "category_sub_cb", "furnished", "terrace", "cellar", "garage",
            "parking_lots", "ownership", "published_at",
        )
        if row.get(key) is not None
    }
    listing = Listing(
        id=int(row["id"]),
        block="",
        source=row["source"],
        source_id_native=row["source_id_native"],
        source_url=row["source_url"],
        category_main=row["category_main"],
        category_type=row["category_type"],
        subtype=row["subtype"],
        disposition=row["disposition"],
        area_m2=_float(row["area_m2"]),
        floor=row["floor"],
        total_floors=row["total_floors"],
        price=_float(row["price_czk"]),
        attrs=_json_safe(attrs),
        description=row["description"],
        first_seen_at=_stamp(row["first_seen_at"]),
        last_seen_at=_stamp(row["last_seen_at"]),
        inactive_at=_stamp(row["inactive_at"]),
        is_active=bool(row["is_active"]),
    )
    digest = listing_digest(listing)
    return {
        "listing_id": digest.listing_id,
        "portal": digest.portal,
        "deal": digest.deal,
        "category": digest.category,
        "subtype": digest.subtype,
        "disposition": digest.disposition,
        "area_m2": digest.area_m2,
        "floor": digest.floor,
        "total_floors": digest.total_floors,
        "price": digest.price,
        # Portal provenance the OPERATOR reads, not part of the judge digest any more:
        # W4 dropped `price_unit` from `ListingDigest` (the "celkem" / "za nemovitost"
        # suffix is one fact in two vocabularies and cost gold votes). The pair page still
        # renders the raw row value, so the wire keeps the key and sources it here.
        "price_unit": listing.attrs.get("price_unit"),
        "attributes": digest.attributes,
        "first_seen": digest.first_seen,
        "last_seen": digest.last_seen,
        "active": digest.active,
        "description": digest.description,
        "description_truncated": digest.description_truncated,
        "absent": digest.absent,
        "source_url": listing.source_url,
    }


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stamp(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _images_by_listing(
    conn: Any, ids: list[int], per_listing: int = IMAGES_PER_LISTING
) -> dict[int, list[dict[str, Any]]]:
    if not ids:
        return {}
    rows = _rows(
        usql.IMAGE_COLUMNS,
        _fetch(conn, usql.LISTING_IMAGES_SQL, {"ids": ids, "per_listing": per_listing}),
    )
    out: dict[int, list[dict[str, Any]]] = {listing_id: [] for listing_id in ids}
    for row in rows:
        out.setdefault(row["listing_id"], []).append(row)
    return out


def _phashes_by_listing(conn: Any, ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Every frame that HAS a dHash, uncapped — the distance basis, not the gallery."""
    if not ids:
        return {}
    rows = _rows(
        usql.LISTING_PHASH_COLUMNS, _fetch(conn, usql.LISTING_PHASHES_SQL, {"ids": ids})
    )
    out: dict[int, list[dict[str, Any]]] = {listing_id: [] for listing_id in ids}
    for row in rows:
        out.setdefault(row["listing_id"], []).append(row)
    return out


def _best_matches(
    frames: list[dict[str, Any]], others: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Per rendered frame, its nearest dHash anywhere on the other side of the pair.

    `others` is the WHOLE album, not the 30 frames the gallery shows: the engine's IMG family
    was computed over every image, so a search that stopped at the render cap would answer "no
    match" on the same page that flies the photo-evidence chip.

    A NULL phash has no distance to anything (E12: unknown is not zero), so it carries no
    match rather than a fabricated one. The match is served both nested and flat — the two
    halves of the pair view read it under different names."""
    out: list[dict[str, Any]] = []
    for image in frames:
        best: dict[str, Any] | None = None
        if image["phash"] is not None:
            for other in others:
                if other["phash"] is None:
                    continue
                distance = hamming64(int(image["phash"]), int(other["phash"]))
                if best is None or distance < best["hamming"]:
                    best = {"image_id": other["image_id"], "hamming": distance}
        out.append(
            {
                **image,
                "best_match": best,
                "best_match_image_id": best["image_id"] if best else None,
                "best_hamming": best["hamming"] if best else None,
            }
        )
    return out


def _total(conn: Any, sql: str, params: dict[str, Any], after: str | None) -> int | None:
    """How many rows the CURRENT filter selects, for "20 of N".

    Asked on the FIRST page only: paging does not change the number, and counting again per
    page is pure cost on a statement that already reads every matching row. `None` on a later
    page is the honest answer — "not counted here", not "zero" — and the page keeps the count
    the first read gave it."""
    if after is not None:
        return None
    rows = _fetch(conn, sql, params)
    return int(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else None


def _engine_stats(conn: Any) -> dict[str, Any]:
    """What the engine has produced, for the header strip beside the program's spend."""
    zones = {
        row["zone"]: int(row["n"])
        for row in _rows(usql.ZONE_COUNT_COLUMNS, _fetch(conn, usql.PAIR_ZONES_SQL))
    }
    certificates = {
        row["certificate"]: int(row["n"])
        for row in _rows(usql.CERTIFICATE_COUNT_COLUMNS, _fetch(conn, usql.CERTIFICATE_COUNTS_SQL))
    }
    generations = _rows(usql.GENERATION_COLUMNS, _fetch(conn, usql.GENERATION_COUNTS_SQL))
    verdicts = _rows(usql.VERDICT_COUNT_COLUMNS, _fetch(conn, usql.VERDICT_COUNTS_SQL))
    judgements = _rows(usql.JUDGEMENT_COUNT_COLUMNS, _fetch(conn, usql.JUDGEMENT_COUNTS_SQL))
    run_rows = _fetch(conn, usql.LAST_SCORE_RUN_SQL, {"mode": "score"})
    last_run = _row(usql.SCORE_RUN_COLUMNS, run_rows[0]) if run_rows else None
    return {
        "pairs_by_zone": zones,
        "n_pairs": sum(zones.values()),
        "certificates": certificates,
        "generations": generations,
        "latest_generation": generations[0]["generation"] if generations else None,
        "verdicts": verdicts,
        "n_verdicts": sum(int(row["n"]) for row in verdicts),
        "judgements": judgements,
        "n_judgements": sum(int(row["n"]) for row in judgements),
        "last_score_run": _json_safe(last_run) if last_run else None,
    }


# --------------------------------------------------------------------------- proposed groups


@router.get("/groups")
def groups(
    request: Request,
    generation: str = Query(DEFAULT_GENERATION),
    after: str | None = Query(None),
    limit: int = Query(GROUP_PAGE_SIZE, ge=1, le=GROUP_MAX_PAGE_SIZE),
    block: int | None = Query(None),
    block_grain: str | None = Query(None),
    source: str | None = Query(None),
    category_main: str | None = Query(None),
    category_type: str | None = Query(None),
    min_size: int | None = Query(None, ge=1),
    max_size: int | None = Query(None, ge=1),
    min_score: float | None = Query(None),
    max_score: float | None = Query(None),
    verdict: str | None = Query(None),
    shared_photo: int | None = Query(None),
    has_judgement: int | None = Query(None),
    sort: str = Query("weakest"),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """One keyset page of proposed clusters, weakest edge first (§12: that is where errors live).

    Shadow mode: nothing on this page has been applied to production, and nothing on it can be.
    """
    _reject_unknown_filters(request, GROUP_FILTER_KEYS)
    _one_of("sort", sort, tuple(GROUP_SORTS))
    _one_of("verdict", verdict, VERDICT_FILTER_VALUES)
    _one_of("block_grain", block_grain, usql.BLOCK_GRAIN_VALUES)
    if not store_ready(conn):
        return _not_ready()

    params: dict[str, Any] = {
        "generation": generation,
        "block": block,
        # The grain travels WITH the code, never instead of it: a cast-obce code and an obec
        # code share one number space (migration 529), so `block` alone can name two blocks.
        # Absent means "either" — which is what a link written before the grain existed says.
        "block_grain": block_grain,
        "source": source,
        "category_main": category_main,
        "category_type": category_type,
        "min_size": min_size,
        "max_size": max_size,
        "min_score": min_score,
        "max_score": max_score,
        "verdict": verdict,
        "shared_photo": _flag("shared_photo", shared_photo),
        "has_judgement": _flag("has_judgement", has_judgement),
        "limit": limit + 1,
        "after_score": None,
        "after_ts": None,
        "after_size": None,
        "after_key": None,
    }
    parts = _split_cursor(after, 2)
    if parts is not None:
        params["after_key"] = _as_int(parts[1])
        if sort == "weakest":
            params["after_score"] = _as_float(parts[0])
        elif sort == "largest":
            params["after_size"] = _as_int(parts[0])
        else:
            params["after_ts"] = _as_stamp(parts[0])

    # The guard covers EVERY statement of the route, not the first: `drop schema autodedup
    # cascade` is one statement in this program, and it can land between any two reads.
    try:
        rows = _rows(usql.CLUSTER_COLUMNS, _fetch(conn, GROUP_SORTS[sort], params))
        has_more = len(rows) > limit
        rows = rows[:limit]
        keys = [row["cluster_key"] for row in rows]
        total = _total(conn, usql.GROUPS_COUNT_SQL, params, after)

        members: dict[int, list[dict[str, Any]]] = {key: [] for key in keys}
        edges: dict[int, dict[str, Any]] = {}
        if keys:
            for row in _rows(
                usql.MEMBER_COLUMNS,
                _fetch(
                    conn,
                    usql.GROUP_MEMBERS_SQL,
                    {"keys": keys, "card_frames": GROUP_CARD_IMAGES},
                ),
            ):
                members.setdefault(row["cluster_key"], []).append(_member_row(row))
            for row in _rows(
                usql.EDGE_SUMMARY_COLUMNS, _fetch(conn, usql.EDGE_SUMMARY_SQL, {"keys": keys})
            ):
                row["family_names"] = _families(row.pop("families"))
                edges[row.pop("cluster_key")] = row
    except _MISSING_RELATION:
        return _not_ready()

    items: list[dict[str, Any]] = []
    for row in rows:
        cluster, cluster_verdict = _cluster_row(row)
        items.append(
            {
                "cluster": cluster,
                "members": members.get(cluster["cluster_key"], []),
                "edges": edges.get(cluster["cluster_key"]),
                "verdict": cluster_verdict,
            }
        )

    next_after = None
    if items and has_more:
        last = items[-1]["cluster"]
        if sort == "weakest":
            score = last["min_edge_score"]
            next_after = _cursor(-1.0 if score is None else score, last["cluster_key"])
        elif sort == "largest":
            next_after = _cursor(last["size"], last["cluster_key"])
        else:
            next_after = _cursor(last["last_changed_at"], last["cluster_key"])

    return {
        "data": {
            "items": items,
            "has_more": has_more,
            "next_after": next_after,
            "generation": generation,
            "sort": sort,
            # "20 of N". Null on a continuation page — the page keeps the first read's number.
            "total": total,
        },
        "store_ready": True,
    }


@router.get("/groups/{cluster_key}")
def group_detail(
    request: Request,
    cluster_key: int,
    generation: str | None = Query(None),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """One cluster, whole: every member with its gallery, every internal edge with its
    evidence, the judge's latest word per tier, the conflicts that touch it, the verdicts."""
    _reject_unknown_filters(request, DETAIL_FILTER_KEYS)
    if not store_ready(conn):
        return _not_ready()
    try:
        rows = _fetch(
            conn,
            usql.GROUP_ONE_SQL,
            {"cluster_key": cluster_key, "generation": generation},
        )
        if not rows:
            raise HTTPException(status_code=404, detail="no such cluster")
        cluster, cluster_verdict = _cluster_row(_row(usql.CLUSTER_COLUMNS, rows[0]))

        member_rows = _rows(
            usql.MEMBER_COLUMNS,
            _fetch(
                conn,
                usql.GROUP_MEMBERS_SQL,
                {"keys": [cluster_key], "card_frames": GROUP_CARD_IMAGES},
            ),
        )
        ids = [row["listing_id"] for row in member_rows]
        galleries = _images_by_listing(conn, ids)
        members = [{**_member_row(row), "images": galleries.get(row["listing_id"], [])}
                   for row in member_rows]

        pairs = [
            _pair_view(row)
            for row in _rows(
                usql.PAIR_COLUMNS, _fetch(conn, usql.CLUSTER_PAIRS_SQL, {"ids": ids})
            )
        ]
        los = [pair["listing_lo"] for pair in pairs]
        his = [pair["listing_hi"] for pair in pairs]
        judgements = (
            _rows(
                usql.JUDGEMENT_COLUMNS,
                _fetch(conn, usql.JUDGEMENTS_LATEST_SQL, {"los": los, "his": his}),
            )
            if pairs
            else []
        )
        pair_verdicts = (
            _rows(
                usql.VERDICT_COLUMNS,
                _fetch(conn, usql.PAIR_VERDICTS_SQL, {"los": los, "his": his}),
            )
            if pairs
            else []
        )
        cluster_verdicts = _rows(
            usql.VERDICT_COLUMNS,
            _fetch(conn, usql.CLUSTER_VERDICTS_SQL, {"cluster_key": cluster_key}),
        )
        conflicts = _rows(
            usql.CONFLICT_COLUMNS,
            _fetch(conn, usql.CLUSTER_CONFLICTS_SQL, {"cluster_key": cluster_key, "ids": ids}),
        )
    except _MISSING_RELATION:
        return _not_ready()

    return {
        "data": {
            "cluster": cluster,
            "verdict": cluster_verdict,
            "members": members,
            "pairs": pairs,
            "judgements": judgements,
            "conflicts": _json_safe(conflicts),
            "verdicts": cluster_verdicts + pair_verdicts,
        },
        "store_ready": True,
    }


# ------------------------------------------------------------------------- residual duplicates


@router.get("/residual")
def residual(
    request: Request,
    generation: str = Query(DEFAULT_GENERATION),
    after: str | None = Query(None),
    limit: int = Query(GROUP_PAGE_SIZE, ge=1, le=GROUP_MAX_PAGE_SIZE),
    block: int | None = Query(None),
    block_grain: str | None = Query(None),
    zone: str | None = Query(None),
    min_score: float = Query(RESIDUAL_MIN_SCORE),
    source_pair: str | None = Query(None),
    has_judgement: int | None = Query(None),
    verdict: str | None = Query(None),
    sort: str = Query("score_desc"),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Pairs the engine scored but did NOT join into one cluster, richest first.

    The scroll is ordered by expected yield (§12), and every row carries the one field that
    turns a review session into design feedback: why it was not merged."""
    _reject_unknown_filters(request, RESIDUAL_FILTER_KEYS)
    _one_of("sort", sort, tuple(RESIDUAL_SORTS))
    _one_of("zone", zone, ZONE_VALUES)
    _one_of("verdict", verdict, VERDICT_FILTER_VALUES)
    _one_of("block_grain", block_grain, usql.BLOCK_GRAIN_VALUES)
    if not store_ready(conn):
        return _not_ready()

    params: dict[str, Any] = {
        "generation": generation,
        "min_score": min_score,
        "zone": zone,
        "block": block,
        "block_grain": block_grain,
        "source_pair": source_pair,
        "has_judgement": _flag("has_judgement", has_judgement),
        "verdict": verdict,
        "limit": limit + 1,
        "after_score": None,
        "after_lo": None,
        "after_hi": None,
    }
    parts = _split_cursor(after, 3)
    if parts is not None:
        params["after_score"] = _as_float(parts[0])
        params["after_lo"] = _as_int(parts[1])
        params["after_hi"] = _as_int(parts[2])

    try:
        rows = _rows(usql.RESIDUAL_COLUMNS, _fetch(conn, RESIDUAL_SORTS[sort], params))
        total = _total(conn, usql.RESIDUAL_COUNT_SQL, params, after)
    except _MISSING_RELATION:
        return _not_ready()
    has_more = len(rows) > limit
    rows = rows[:limit]

    items: list[dict[str, Any]] = []
    for row in rows:
        feats = _feats(row["features"])
        items.append(
            {
                "listing_lo": row["listing_lo"],
                "listing_hi": row["listing_hi"],
                "score": row["score"],
                "zone": row["zone"],
                "decision": row["decision"],
                "guard_veto": row["guard_veto"],
                "certificate": _certificate(row["decision"]),
                "families": _families(row["families"]),
                "probes": row["probes"],
                "block_key": row["block_key"],
                "why_not_merged": _why_not_merged(
                    row["zone"], row["decision"], row["guard_veto"]
                ),
                "a": _side(row, "a_", row["listing_lo"]),
                "b": _side(row, "b_", row["listing_hi"]),
                "top_features": _top_features(feats, row["model_version"]),
                "judge": (
                    {
                        "verdict": row["judge_verdict"],
                        "confidence": row["judge_confidence"],
                        "tier": row["judge_tier"],
                    }
                    if row["judge_verdict"] is not None
                    else None
                ),
                "verdict": (
                    {
                        "verdict": row["verdict"],
                        "note": row["verdict_note"],
                        "decided_by": row["verdict_decided_by"],
                        "decided_at": row["verdict_decided_at"],
                    }
                    if row["verdict"] is not None
                    else None
                ),
            }
        )

    next_after = None
    if items and has_more:
        last = items[-1]
        next_after = _cursor(last["score"], last["listing_lo"], last["listing_hi"])

    return {
        "data": {
            "items": items,
            "has_more": has_more,
            "next_after": next_after,
            "generation": generation,
            "min_score": min_score,
            "total": total,
        },
        "store_ready": True,
    }


# --------------------------------------------------------------- the blocks of a generation


@router.get("/blocks")
def blocks(
    request: Request,
    generation: str = Query(DEFAULT_GENERATION),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Every block this generation clustered, NAMED — what the BLOCK filter offers instead of
    a free-text field for a RÚIAN code.

    A block is a (code, grain) pair: `o563510` is a town, `c490245` a quarter (migration 529),
    and the two vocabularies share their number space. `block_key` alone is what the list
    statements filter on TOGETHER, so both are handed back and a picker sends both: the code
    alone would re-create the conflation migration 529 exists to end. The name is read from
    `listing_location`, the same store the engine derives the block key from.
    A block whose grain predates migration 529 carries no name rather than a guessed one — a
    town's number is a quarter's number somewhere else."""
    _reject_unknown_filters(request, BLOCK_FILTER_KEYS)
    if not store_ready(conn):
        return _not_ready()
    try:
        rows = _rows(
            usql.BLOCK_COLUMNS,
            _fetch(conn, usql.BLOCKS_SQL, {"generation": generation, "limit": BLOCKS_LIMIT}),
        )
    except _MISSING_RELATION:
        return _not_ready()
    items = [
        {
            "block_key": row["block_key"],
            "block_grain": row["block_grain"],
            "name": row["name"],
            "n_clusters": int(row["n_clusters"] or 0),
            "n_listings": int(row["n_listings"] or 0),
        }
        for row in rows
    ]
    return {
        "data": {"items": items, "generation": generation},
        "store_ready": True,
    }


# ------------------------------------------------------------------------------- one pair


@router.get("/pair/{listing_lo}/{listing_hi}")
def pair(
    request: Request,
    listing_lo: int,
    listing_hi: int,
    generation: str | None = Query(None),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Everything the engine knows about one pair: every feature with its presence flag, both
    PII-free digests, both galleries with the nearest dHash across the pair, the judge
    transcript and the operator's verdicts."""
    _reject_unknown_filters(request, DETAIL_FILTER_KEYS)
    if listing_lo >= listing_hi:
        raise _bad("listing_lo must be smaller than listing_hi")
    if not store_ready(conn):
        return _not_ready()
    try:
        rows = _fetch(
            conn, usql.PAIR_ONE_SQL, {"listing_lo": listing_lo, "listing_hi": listing_hi}
        )
        if not rows:
            raise HTTPException(status_code=404, detail="no such pair")
        row = _row(usql.PAIR_COLUMNS, rows[0])
        feats = _feats(row["features"])

        digests = {
            int(detail["id"]): _digest(detail)
            for detail in _rows(
                usql.LISTING_DETAIL_COLUMNS,
                _fetch(conn, usql.LISTING_DETAIL_SQL, {"ids": [listing_lo, listing_hi]}),
            )
        }
        galleries = _images_by_listing(conn, [listing_lo, listing_hi])
        left = galleries.get(listing_lo, [])
        right = galleries.get(listing_hi, [])
        # Rendered frames are capped; the distances behind them are not.
        albums = _phashes_by_listing(conn, [listing_lo, listing_hi])

        judgements = _rows(
            usql.JUDGEMENT_COLUMNS,
            _fetch(
                conn,
                usql.JUDGEMENTS_LATEST_SQL,
                {"los": [listing_lo], "his": [listing_hi]},
            ),
        )
        verdicts = _rows(
            usql.VERDICT_COLUMNS,
            _fetch(conn, usql.PAIR_VERDICTS_SQL, {"los": [listing_lo], "his": [listing_hi]}),
        )
    except _MISSING_RELATION:
        return _not_ready()

    return {
        "data": {
            "pair": _pair_view(row),
            "features": [
                {"name": name, "value": value, "present": present}
                for name, (value, present) in sorted(feats.items())
            ],
            "top_features": _top_features(feats, row["model_version"]),
            "digests": {
                "a": digests.get(listing_lo),
                "b": digests.get(listing_hi),
            },
            "images": {
                "a": _best_matches(left, albums.get(listing_hi, [])),
                "b": _best_matches(right, albums.get(listing_lo, [])),
                # The gallery is capped at 30 frames a side; the album sizes say so plainly
                # rather than letting a truncated strip read as the whole evidence.
                "n_frames_shown": {"a": len(left), "b": len(right)},
                "n_hashed_frames": {
                    "a": len(albums.get(listing_lo, [])),
                    "b": len(albums.get(listing_hi, [])),
                },
            },
            "judgements": judgements,
            "verdicts": verdicts,
            "generation": generation,
        },
        "store_ready": True,
    }


# --------------------------------------------------------------------- the operator's verdict


class VerdictIn(BaseModel):
    kind: str
    verdict: str
    listing_lo: int | None = None
    listing_hi: int | None = None
    cluster_key: int | None = None
    note: str | None = Field(default=None, max_length=2000)


@router.post("/verdict")
def verdict(
    body: VerdictIn,
    claims: dict = Depends(deps.require_admin),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """The operator's decision, and the only write this API owns (§9's feedback loop).

    A negative PAIR verdict also writes `autodedup.must_not_link` — permanent, surviving every
    generation and recalibration; re-deciding the same pair the other way RETRACTS that row,
    because a veto the operator has taken back must stop vetoing. A negative CLUSTER verdict
    deliberately writes no must-not-link at all: flagging a group is not the same statement as
    forbidding each of its pairs, and the splits belong on the pair view where the operator
    can see which edge is wrong.

    Unlike the reads, an un-migrated store is a 503 here and not a `store_ready: false` 200: a
    write that silently did nothing would be recorded by the optimistic client as a decision.
    """
    _one_of("kind", body.kind, VERDICT_KINDS)
    _one_of("verdict", body.verdict, VERDICT_VALUES)
    decided_by = claims.get("email") or claims.get("sub")
    if not decided_by:
        raise HTTPException(status_code=403, detail="the admin identity carries no email")
    if not store_ready(conn):
        raise HTTPException(status_code=503, detail="the autodedup store is not created yet")

    if body.kind == "pair":
        if body.listing_lo is None or body.listing_hi is None:
            raise _bad("a pair verdict needs listing_lo and listing_hi")
        if body.cluster_key is not None:
            raise _bad("a pair verdict carries no cluster_key")
        if body.listing_lo >= body.listing_hi:
            raise _bad("listing_lo must be smaller than listing_hi")
        if not _fetch(
            conn,
            usql.PAIR_EXISTS_SQL,
            {"listing_lo": body.listing_lo, "listing_hi": body.listing_hi},
        ):
            raise HTTPException(status_code=404, detail="no such pair")
        stored = _fetch(
            conn,
            usql.VERDICT_PAIR_UPSERT_SQL,
            {
                "listing_lo": body.listing_lo,
                "listing_hi": body.listing_hi,
                "verdict": body.verdict,
                "note": body.note,
                "decided_by": str(decided_by),
            },
        )
        must_not_link = body.verdict in NEGATIVE_VERDICTS
        if must_not_link:
            _execute(
                conn,
                usql.MUST_NOT_LINK_UPSERT_SQL,
                {
                    "listing_lo": body.listing_lo,
                    "listing_hi": body.listing_hi,
                    "reason": body.note or f"operator: {body.verdict}",
                },
            )
        else:
            # The correction path. Without it a mis-clicked "not the same" keeps `guards.py`
            # vetoing the pair on every future run while this page shows the corrected
            # verdict — a contradiction visible on no surface at all.
            _execute(
                conn,
                usql.MUST_NOT_LINK_RETRACT_SQL,
                {"listing_lo": body.listing_lo, "listing_hi": body.listing_hi},
            )
    else:
        if body.cluster_key is None:
            raise _bad("a cluster verdict needs cluster_key")
        if body.listing_lo is not None or body.listing_hi is not None:
            raise _bad("a cluster verdict carries no listing ids")
        if not _fetch(conn, usql.CLUSTER_EXISTS_SQL, {"cluster_key": body.cluster_key}):
            raise HTTPException(status_code=404, detail="no such cluster")
        stored = _fetch(
            conn,
            usql.VERDICT_CLUSTER_UPSERT_SQL,
            {
                "cluster_key": body.cluster_key,
                "verdict": body.verdict,
                "note": body.note,
                "decided_by": str(decided_by),
            },
        )
        must_not_link = False

    return {
        "data": {
            "verdict": _row(usql.VERDICT_COLUMNS, stored[0]) if stored else None,
            "must_not_link": must_not_link,
        },
        "store_ready": True,
    }


class SplitUnitIn(BaseModel):
    listing_id: int
    unit: str = Field(max_length=40)


class SplitIn(BaseModel):
    cluster_key: int
    generation: str
    units: list[SplitUnitIn]
    relation: str
    note: str | None = Field(default=None, max_length=2000)


def _split_summary(assignment: dict[int, str]) -> str:
    """`A: 94020,140903 | B: 94492` — the assignment as one line, stored as the cluster
    verdict's note so the ruling is readable without re-deriving it from the pair rows."""
    units: dict[str, list[int]] = {}
    for listing_id, unit in assignment.items():
        units.setdefault(unit, []).append(listing_id)
    return " | ".join(
        f"{unit}: {','.join(str(i) for i in sorted(ids))}"
        for unit, ids in sorted(units.items())
    )


@router.post("/verdict/split")
def verdict_split(
    body: SplitIn,
    claims: dict = Depends(deps.require_admin),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """A proposed group, ruled on unit by unit — the answer a whole-cluster verdict cannot give.

    The operator assigns every member a UNIT LABEL. Two members in the same unit are one
    property (`same`, and any must-not-link the operator wrote earlier is retracted); two in
    different units are `relation` — the same building, the same development project, or
    unrelated — and each such pair takes a permanent `must_not_link`, because a unit the
    operator has separated must never come back as a merge proposal. The cluster itself is
    stored as `same` when one unit was used and as `relation` otherwise, with the assignment
    as its note. Everything lands in ONE transaction: a half-applied split would leave the pair
    rows and the cluster row saying different things.

    THE PAIR NEED NOT EXIST IN `autodedup.pairs`, which is why this route does not check it:
    a cluster is the union of the edges the engine accepted, so two members can sit in one
    cluster with no scored edge between them at all. CLUSTER MEMBERSHIP is the validation, and
    the assignment must cover every member exactly once and name nobody else.
    """
    _one_of("relation", body.relation, SPLIT_RELATIONS)
    decided_by = claims.get("email") or claims.get("sub")
    if not decided_by:
        raise HTTPException(status_code=403, detail="the admin identity carries no email")
    if not store_ready(conn):
        raise HTTPException(status_code=503, detail="the autodedup store is not created yet")

    if not _fetch(
        conn,
        usql.GROUP_ONE_SQL,
        {"cluster_key": body.cluster_key, "generation": body.generation},
    ):
        raise HTTPException(status_code=404, detail="no such cluster in this generation")

    members = _rows(
        usql.MEMBER_COLUMNS,
        _fetch(
            conn,
            usql.GROUP_MEMBERS_SQL,
            {"keys": [body.cluster_key], "card_frames": GROUP_CARD_IMAGES},
        ),
    )
    member_ids = sorted({int(row["listing_id"]) for row in members})
    if not member_ids:
        raise _bad("the cluster has no members to split")

    assignment: dict[int, str] = {}
    for entry in body.units:
        unit = entry.unit.strip()
        if not unit:
            raise _bad("a unit label cannot be empty")
        if entry.listing_id in assignment:
            raise _bad(f"listing {entry.listing_id} is assigned to two units")
        assignment[entry.listing_id] = unit
    if set(assignment) != set(member_ids):
        raise _bad("the unit assignment must name every member of the cluster, and only them")
    distinct = sorted(set(assignment.values()))
    if len(distinct) > MAX_SPLIT_UNITS:
        raise _bad(f"a split names at most {MAX_SPLIT_UNITS} units")

    summary = _split_summary(assignment)
    cluster_verdict = "same" if len(distinct) == 1 else body.relation
    note = f"{body.note} · {summary}" if body.note else summary

    n_pairs_same = 0
    n_pairs_negative = 0
    try:
        with conn.transaction():
            for index, lo in enumerate(member_ids):
                for hi in member_ids[index + 1:]:
                    same_unit = assignment[lo] == assignment[hi]
                    _execute(
                        conn,
                        usql.VERDICT_PAIR_UPSERT_SQL,
                        {
                            "listing_lo": lo,
                            "listing_hi": hi,
                            "verdict": "same" if same_unit else body.relation,
                            "note": f"operator split: {summary}",
                            "decided_by": str(decided_by),
                        },
                    )
                    if same_unit:
                        n_pairs_same += 1
                        # The correction half of the loop (§9): re-ruling a pair as one unit
                        # has to drop the veto an earlier ruling wrote, or `guards.py` keeps
                        # refusing a pair this page now shows as confirmed.
                        _execute(
                            conn,
                            usql.MUST_NOT_LINK_RETRACT_SQL,
                            {"listing_lo": lo, "listing_hi": hi},
                        )
                    else:
                        n_pairs_negative += 1
                        _execute(
                            conn,
                            usql.MUST_NOT_LINK_UPSERT_SQL,
                            {
                                "listing_lo": lo,
                                "listing_hi": hi,
                                "reason": f"operator split: {body.relation}",
                            },
                        )
            stored = _fetch(
                conn,
                usql.VERDICT_CLUSTER_UPSERT_SQL,
                {
                    "cluster_key": body.cluster_key,
                    "verdict": cluster_verdict,
                    "note": note,
                    "decided_by": str(decided_by),
                },
            )
    except _CHECK_VIOLATION as exc:
        # The store predates migration 532 and does not know the value being written. Naming
        # the migration is the whole point: a bare 500 sends the operator to the logs.
        raise HTTPException(
            status_code=503,
            detail="this verdict needs migration 532 (autodedup.verdicts vocabulary)",
        ) from exc

    return {
        "data": {
            "cluster_verdict": _row(usql.VERDICT_COLUMNS, stored[0]) if stored else None,
            "n_pairs_same": n_pairs_same,
            "n_pairs_negative": n_pairs_negative,
            "must_not_link_written": n_pairs_negative,
            # Counted per same-unit pair: the DELETE is keyed on the pair and is a no-op where
            # no operator veto stood, so this is "pairs whose veto was dropped or never was".
            "must_not_link_retracted": n_pairs_same,
        },
        "store_ready": True,
    }
