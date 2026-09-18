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

PII (E28): no broker column is selected anywhere (autodedup/ui_sql.py), and every advert text
— description or title — reaches a response only through the judge's own scrubber
(`autodedup.judge.listing_digest` / `scrubbed_text`, the same regexes either way). The two
OPERATOR surfaces (the group dialog, the pair page) are shown the WHOLE scrubbed text rather
than the judge's token-capped slice: reading costs no tokens, and the sentence that tells two
developer units apart is as often in the last paragraph as the first.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api import dependencies as deps
from autodedup import agreement as agreement_math
from autodedup import candidates as candidate_groups
from autodedup import progress_sql as psql
from autodedup import ui_sql as usql
from autodedup import verdict_reasons as reasons_registry
from autodedup.dataset import Listing, hamming64
from autodedup.judge import listing_digest, scrubbed_text
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
    # Migration 533 added `autodedup.verdicts.reasons`. A store without it raises
    # UndefinedColumn on every verdict write — the same class of answer, one migration on.
    _UNDEFINED_COLUMN: tuple[type[BaseException], ...] = (_pg_errors.UndefinedColumn,)
except Exception:  # noqa: BLE001 — psycopg absent (tests run on fake connections)
    _MISSING_RELATION = ()
    _CHECK_VIOLATION = ()
    _UNDEFINED_COLUMN = ()

# What a READ answers when the store is behind the code. `store_ready` asks the catalog for
# the RELATION only, so a database that has 528 but not 533 passes it and then raises
# UndefinedColumn on `v.reasons` — which every review statement now selects. A write refuses
# loudly (503 naming the migration); a read must degrade to "not ready" instead, or one
# un-applied additive migration takes the whole review UI down rather than the new column.
_STORE_BEHIND: tuple[type[BaseException], ...] = _MISSING_RELATION + _UNDEFINED_COLUMN

router = APIRouter(
    prefix="/autodedup",
    tags=["autodedup"],
    dependencies=[Depends(deps.require_admin)],
)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# What a queue keys its cards on — a cluster key on the groups queue, a candidate key on the
# candidate one. The member-verdict fan-out is the same read either way.
_Key = TypeVar("_Key", int, str)

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
    except _STORE_BEHIND:
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
    except _STORE_BEHIND:
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

# There is NO default generation. `g1` was one — the first hand-prior pass, which over-merged
# developer units and was superseded twice — and every validation view opened on it long after
# the engine had moved on: the operator reviewed certificate edges that the current pass never
# proposed. An unnamed generation is resolved against the store instead (`_resolve_generation`),
# and the answer is echoed back so the page can say which pass it is showing.
GROUP_PAGE_SIZE = 25
GROUP_MAX_PAGE_SIZE = 100
RESIDUAL_MIN_SCORE = 0.20
IMAGES_PER_LISTING = 30
# The card gallery (§12): enough frames to page an advert on the queue card itself, far
# short of the album the dialog opens — a group card renders four members at once. BOTH
# queues read this one cap: a residual row pages its two sides exactly like a group card
# pages its members, so a second number would make "+K fotek v detailu" mean two things.
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
    # The unbiased order (D6): a seeded hash, so the sample is stable across pages and reloads.
    "random": usql.GROUPS_RANDOM_SQL,
}
RESIDUAL_SORTS: dict[str, str] = {
    "score_desc": usql.RESIDUAL_SQL,
    "random": usql.RESIDUAL_RANDOM_SQL,
}
# The sorts whose cursor's first part is the seeded hash rather than a number or a timestamp.
RANDOM_SORT = "random"

GROUP_FILTER_KEYS: frozenset[str] = frozenset(
    {
        "generation", "after", "limit", "block", "block_grain", "source", "category_main",
        "category_type", "min_size", "max_size", "min_score", "max_score", "verdict",
        "shared_photo", "has_judgement", "sort", "seed",
    }
)
RESIDUAL_FILTER_KEYS: frozenset[str] = frozenset(
    {
        "generation", "after", "limit", "block", "block_grain", "zone", "min_score",
        "source_pair", "has_judgement", "verdict", "sort", "seed",
    }
)
DETAIL_FILTER_KEYS: frozenset[str] = frozenset({"generation"})
BLOCK_FILTER_KEYS: frozenset[str] = frozenset({"generation"})
# The candidate queue (E56) reads the residual cohort at ITS OWN floor, so there is no
# `min_score` here: the floor is `RESIDUAL_MIN_SCORE` and the packing — and its cache — is one
# structure per generation. `source_pair` is likewise absent: a candidate group spans several
# adverts, so "the pair of portals" is not a question it can answer.
CANDIDATE_FILTER_KEYS: frozenset[str] = frozenset(
    {"generation", "after", "limit", "block", "block_grain", "zone", "verdict", "sort", "seed"}
)
PROGRESS_FILTER_KEYS: frozenset[str] = frozenset(
    {"generation", "seed", "surface", "min_score"}
)
AGREEMENT_FILTER_KEYS: frozenset[str] = frozenset({"generation"})

# THE SEED IS A NAME, NOT A PREDICATE. It reaches SQL as a parameter of `md5(key || seed)`, so
# it could not inject anything — but it is also the IDENTITY of a sample, and a sample nobody
# can retype is a sample nobody can reproduce. Lower-case alphanumerics, 16 characters, one
# default: `v1` is the first session's sample and it stays that unless the operator says
# otherwise (a rotating default would silently re-draw the sample every reload).
DEFAULT_SEED = "v1"
SEED_RE = re.compile(r"^[a-z0-9]{1,16}$")
# 32 hex characters — the shape `md5()` returns, validated here so an edited cursor is a 400
# rather than a predicate.
HASH_RE = re.compile(r"^[0-9a-f]{32}$")

# THE SAMPLE IS THE FIRST 100 OF THE SEEDED ORDER. Two of these (one per surface) is the
# ~200-pair session D6 names; the counter counts against exactly that.
VALIDATION_SAMPLE_SIZE = 100
VALIDATION_SURFACES: tuple[str, ...] = ("groups", "residual", "candidates")

# ------------------------------------------------------------------ the candidate queue (E56)
#
# The residual pairs of one generation, packed into small groups so ONE save rules many pairs.
# The packing is `autodedup/candidates.py` — pure, cached per generation — and everything here
# is the page around it: a closed sort vocabulary, a closed filter vocabulary, and paging.
CANDIDATE_SORTS: tuple[str, ...] = ("weakest", "strongest", "largest", "random")
# Reviewed is DERIVED, not stored: there is no candidate row in `autodedup.verdicts` to carry a
# verdict, so a group counts as reviewed when EVERY residual pair inside it carries an operator
# pair verdict — by ANY operator, this being a single-operator platform, which is the same
# reading `AGREEMENT_PAIRS_SQL` and the progress strip make of the same rows.
CANDIDATE_VERDICT_VALUES: tuple[str, ...] = ("unreviewed", "reviewed")
# `<smallest listing id>-<10 hex>` — `candidates.candidate_key`. Validated before it is looked
# up, so a hand-edited key is a 400 rather than a scan of the whole index.
CANDIDATE_KEY_RE = re.compile(r"^[0-9]{1,19}-[0-9a-f]{10}$")

# How big a confirmed cluster may be before the agreement read stops expanding it into pairs
# (n members imply n(n-1)/2 of them). 12 is far above the trial's group sizes and bounds the
# implied set at 66 pairs per group; what it skipped is reported, never dropped silently.
AGREEMENT_MAX_CLUSTER_SIZE = 12

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


def _resolve_generation(conn: Any, generation: str | None) -> str | None:
    """The pass a view reads: the one the caller named, else the newest one persisted.

    None comes back only from a store that holds no cluster at all — an empty queue is then
    the honest answer, where a fabricated generation name would be an empty queue that looks
    like a filter result."""
    if generation:
        return generation
    rows = _fetch(conn, usql.LATEST_GENERATION_SQL)
    return str(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else None


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


def _seeded_hash(key: str, seed: str) -> str:
    """Postgres' `md5(key || seed)`, computed here for the NEXT cursor.

    Not a security hash — a stable shuffle (D6's unbiased sample). It must agree with the
    statement's own `md5()` exactly or the next page would start in the wrong place, which is
    why the key is spelled the same way on both sides (`cluster_key::text`, `lo:hi`) and both
    hash UTF-8 bytes."""
    return hashlib.md5((key + seed).encode("utf-8")).hexdigest()


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


def _seed(value: str | None) -> str:
    """The sample's name, validated against a closed charset (400 otherwise).

    Absent means the default seed — NOT a fresh one: a sample that re-draws itself whenever a
    caller forgets the parameter is not a sample anybody can finish."""
    if value is None or value == "":
        return DEFAULT_SEED
    if not SEED_RE.match(value):
        raise _bad("seed must be 1-16 characters of a-z0-9")
    return value


def _as_hash(text: str) -> str:
    """The md5 half of a seeded cursor. Validated here for the same reason `_as_stamp` is: an
    arbitrary string reaching `%(after_hash)s::text` is not a 500, it is worse — it silently
    compares as itself and pages from somewhere nobody asked for."""
    if not HASH_RE.match(text):
        raise _bad("after is not a cursor from this endpoint")
    return text


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


def _member_verdicts(
    conn: Any, members: dict[_Key, list[dict[str, Any]]]
) -> dict[_Key, list[dict[str, Any]]]:
    """The operator's pair verdicts that fall INSIDE each listed cluster.

    A queue card has to show the split that is STORED — letters reading "A" over a group the
    operator partitioned last week is a write-only record, and the next save would silently
    retract the must-not-links the first one wrote.
    """
    ids = sorted({row["listing_id"] for rows in members.values() for row in rows})
    if not ids:
        return {key: [] for key in members}
    rows = _rows(usql.VERDICT_COLUMNS, _fetch(conn, usql.MEMBER_PAIR_VERDICTS_SQL, {"ids": ids}))
    out: dict[_Key, list[dict[str, Any]]] = {}
    for key, member_rows in members.items():
        inside = {row["listing_id"] for row in member_rows}
        out[key] = [
            row
            for row in rows
            if row["listing_lo"] in inside and row["listing_hi"] in inside
        ]
    return out


def _cluster_member_ids(conn: Any, cluster_key: int) -> list[int]:
    return sorted(
        {int(row[0]) for row in _fetch(conn, usql.CLUSTER_MEMBER_IDS_SQL,
                                       {"cluster_key": cluster_key})}
    )


def _needs_migration_532() -> HTTPException:
    """A store that predates migration 532 rejects the new value with a CHECK violation.
    Naming the migration is the whole point: a bare 500 sends the operator to the logs."""
    return HTTPException(
        status_code=503,
        detail="this verdict needs migration 532 (autodedup.verdicts vocabulary)",
    )


def _needs_migration_533() -> HTTPException:
    """The reasons column is younger than the route. Same treatment as 532's widening: name
    the migration, because a bare 500 sends the operator to the logs — and the two are told
    apart by the SQLSTATE, a rejected VALUE being a different fault from a missing COLUMN."""
    return HTTPException(
        status_code=503,
        detail="this verdict needs migration 533 (autodedup.verdicts.reasons)",
    )


def _reasons(values: list[str] | None) -> list[str]:
    """The registry is the vocabulary (§9). An unknown code is a 400 and not a silently
    dropped chip: a reason the operator clicked and the store never kept is worse than none."""
    try:
        return reasons_registry.normalise(values)
    except ValueError as exc:
        raise _bad(str(exc)) from exc


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


# A member whose `listings` row is gone (the LEFT JOIN case) still renders — with no text
# rather than a missing key, so the client never has to tell "absent" from "not selected here".
_NO_TEXT: dict[str, Any] = {
    "title": None,
    "description": None,
    "description_truncated": False,
    "description_chars": 0,
}


def _member_texts(conn: Any, ids: list[int]) -> dict[int, dict[str, Any]]:
    """The advert text of each member of ONE cluster, scrubbed (E28), for the DETAIL route.

    Deliberately not part of `_member_row`: the queue renders the same member shape from
    `GROUP_MEMBERS_SQL` and must not carry a description — 20 cards x N members of TOASTed text
    for a payload the operator has not opened. Here the dialog asked for exactly one cluster.
    """
    if not ids:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for row in _rows(usql.MEMBER_TEXT_COLUMNS, _fetch(conn, usql.MEMBER_TEXT_SQL, {"ids": ids})):
        description = scrubbed_text(row["description"])
        out[int(row["listing_id"])] = {
            "title": scrubbed_text(row["title"]),
            "description": description,
            # The operator's copy is never cut; the key stays so one client component can render
            # this text and the judge digest, which IS cut on the paid path.
            "description_truncated": False,
            "description_chars": len(description or ""),
        }
    return out


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
    # The SAME pageable gallery a group card's member carries, `images[0]` being the frame
    # `cover` names. A side whose lateral found nothing is an EMPTY list, never a missing key:
    # the page would otherwise have to tell `undefined` from "this advert has no photos".
    side["images"] = _json_safe(row.get(f"{prefix}images") or [])
    return side


def _cluster_row(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The cluster, and the operator's latest verdict on it, as two objects."""
    verdict = (
        {
            "verdict": row["verdict"],
            "note": row["verdict_note"],
            "reasons": list(row["verdict_reasons"] or []),
            "decided_by": row["verdict_decided_by"],
            "decided_at": row["verdict_decided_at"],
        }
        if row["verdict"] is not None
        else None
    )
    cluster = {
        key: value
        for key, value in row.items()
        if key not in ("verdict", "verdict_note", "verdict_reasons", "verdict_decided_by",
                       "verdict_decided_at")
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
    """`autodedup.judge.listing_digest` over a DB row — the judge's own PII-free record, so
    the operator reads what the model was shown (and never a broker field). One deliberate
    difference: the description is NOT cut here. The cap is the paid lane's token budget."""
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
    # The operator reads the WHOLE scrubbed advert; the judge's own digest stays capped.
    digest = listing_digest(listing, truncate=False)
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
        "description_chars": len(digest.description or ""),
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
    # A READ degrades where a write refuses: the header strip must still render against a
    # store that predates 533, so the histogram comes back empty rather than 500ing the page.
    try:
        verdict_reason_rows = _rows(
            usql.REASON_COUNT_COLUMNS, _fetch(conn, usql.REASON_COUNTS_SQL)
        )
    except _UNDEFINED_COLUMN:
        verdict_reason_rows = []
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
        # Per (kind, reason), never summed across the two grains — see REASON_COUNTS_SQL.
        "verdict_reasons": verdict_reason_rows,
        "judgements": judgements,
        "n_judgements": sum(int(row["n"]) for row in judgements),
        "last_score_run": _json_safe(last_run) if last_run else None,
    }


# ------------------------------------------------------------------ the generations on record


@router.get("/generations")
def generations(
    request: Request,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Every clustering pass the store holds, newest first — the picker's vocabulary.

    A free-text field for a generation name is how a superseded pass stays on screen: `g1`
    reads like a valid answer forever. This is the rollup the Progress header already shows,
    read here so the queue's picker and that header cannot disagree about what exists."""
    _reject_unknown_filters(request, frozenset())
    if not store_ready(conn):
        return _not_ready()
    try:
        items = _json_safe(
            _rows(usql.GENERATION_COLUMNS, _fetch(conn, usql.GENERATION_COUNTS_SQL))
        )
    except _STORE_BEHIND:
        return _not_ready()
    return {
        "data": {"items": items, "latest": items[0]["generation"] if items else None},
        "store_ready": True,
    }


# --------------------------------------------------------------------------- proposed groups


@router.get("/groups")
def groups(
    request: Request,
    generation: str | None = Query(None),
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
    seed: str | None = Query(None),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """One keyset page of proposed clusters, weakest edge first (§12: that is where errors live).

    `sort=random` is the other order this queue serves: the seeded, unbiased sample D6 is
    measured on, stable across pages and reloads for one seed.

    Shadow mode: nothing on this page has been applied to production, and nothing on it can be.
    """
    _reject_unknown_filters(request, GROUP_FILTER_KEYS)
    _one_of("sort", sort, tuple(GROUP_SORTS))
    sample_seed = _seed(seed)
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
        "seed": sample_seed,
        "after_score": None,
        "after_ts": None,
        "after_size": None,
        "after_hash": None,
        "after_key": None,
    }
    parts = _split_cursor(after, 2)
    if parts is not None:
        params["after_key"] = _as_int(parts[1])
        if sort == "weakest":
            params["after_score"] = _as_float(parts[0])
        elif sort == "largest":
            params["after_size"] = _as_int(parts[0])
        elif sort == RANDOM_SORT:
            params["after_hash"] = _as_hash(parts[0])
        else:
            params["after_ts"] = _as_stamp(parts[0])

    # The guard covers EVERY statement of the route, not the first: `drop schema autodedup
    # cascade` is one statement in this program, and it can land between any two reads.
    try:
        # Resolved here rather than as a Query default: the newest pass is a fact of the
        # store, and it is read under the same guard as every other statement.
        generation = _resolve_generation(conn, generation)
        params["generation"] = generation
        rows = _rows(usql.CLUSTER_COLUMNS, _fetch(conn, GROUP_SORTS[sort], params))
        has_more = len(rows) > limit
        rows = rows[:limit]
        keys = [row["cluster_key"] for row in rows]
        total = _total(conn, usql.GROUPS_COUNT_SQL, params, after)

        members: dict[int, list[dict[str, Any]]] = {key: [] for key in keys}
        edges: dict[int, dict[str, Any]] = {}
        member_verdicts: dict[int, list[dict[str, Any]]] = {key: [] for key in keys}
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
            member_verdicts = _member_verdicts(conn, members)
    except _STORE_BEHIND:
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
                # The operator's own rulings on the members' PAIRS, so a card can show the
                # split that is stored rather than a blank set of letters over it (E50).
                "member_verdicts": member_verdicts.get(cluster["cluster_key"], []),
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
        elif sort == RANDOM_SORT:
            next_after = _cursor(
                _seeded_hash(str(last["cluster_key"]), sample_seed), last["cluster_key"]
            )
        else:
            next_after = _cursor(last["last_changed_at"], last["cluster_key"])

    return {
        "data": {
            "items": items,
            "has_more": has_more,
            "next_after": next_after,
            "generation": generation,
            "sort": sort,
            # Echoed like the generation is: the seed NAMES the sample, and a page that does
            # not say which one it drew cannot be resumed tomorrow.
            "seed": sample_seed,
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
        # The advert TEXT is the dialog's reason to exist for a developer project: five units
        # share one photo set and one attribute row, and differ only in what the ad says.
        texts = _member_texts(conn, ids)
        members = [
            {
                **_member_row(row),
                "images": galleries.get(row["listing_id"], []),
                **texts.get(row["listing_id"], _NO_TEXT),
            }
            for row in member_rows
        ]

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
        # Member-grain, NOT edge-grain: a split rules on every member pair, including the
        # ones the engine never scored, and those are precisely the rulings a dialog
        # rehydrating the assignment must not miss.
        pair_verdicts = _rows(
            usql.VERDICT_COLUMNS,
            _fetch(conn, usql.MEMBER_PAIR_VERDICTS_SQL, {"ids": ids}),
        )
        cluster_verdicts = _rows(
            usql.VERDICT_COLUMNS,
            _fetch(conn, usql.CLUSTER_VERDICTS_SQL, {"cluster_key": cluster_key}),
        )
        conflicts = _rows(
            usql.CONFLICT_COLUMNS,
            _fetch(conn, usql.CLUSTER_CONFLICTS_SQL, {"cluster_key": cluster_key, "ids": ids}),
        )
    except _STORE_BEHIND:
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
            "member_verdicts": pair_verdicts,
        },
        "store_ready": True,
    }


# ------------------------------------------------------------------------- residual duplicates


@router.get("/residual")
def residual(
    request: Request,
    generation: str | None = Query(None),
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
    seed: str | None = Query(None),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Pairs the engine scored but did NOT join into one cluster, richest first.

    The scroll is ordered by expected yield (§12), and every row carries the one field that
    turns a review session into design feedback: why it was not merged. `sort=random` swaps
    that working order for the seeded, unbiased one the D6 session is measured on."""
    _reject_unknown_filters(request, RESIDUAL_FILTER_KEYS)
    _one_of("sort", sort, tuple(RESIDUAL_SORTS))
    sample_seed = _seed(seed)
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
        "seed": sample_seed,
        # The per-side gallery cap. Unused by `RESIDUAL_COUNT_SQL`, which runs no photo
        # LATERAL — psycopg binds the names a statement spells, so one params dict serves both.
        "card_frames": GROUP_CARD_IMAGES,
        "after_score": None,
        "after_hash": None,
        "after_lo": None,
        "after_hi": None,
    }
    parts = _split_cursor(after, 3)
    if parts is not None:
        if sort == RANDOM_SORT:
            params["after_hash"] = _as_hash(parts[0])
        else:
            params["after_score"] = _as_float(parts[0])
        params["after_lo"] = _as_int(parts[1])
        params["after_hi"] = _as_int(parts[2])

    try:
        generation = _resolve_generation(conn, generation)
        params["generation"] = generation
        rows = _rows(usql.RESIDUAL_COLUMNS, _fetch(conn, RESIDUAL_SORTS[sort], params))
        total = _total(conn, usql.RESIDUAL_COUNT_SQL, params, after)
    except _STORE_BEHIND:
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
                        "reasons": list(row["verdict_reasons"] or []),
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
        head = (
            _seeded_hash(f"{last['listing_lo']}:{last['listing_hi']}", sample_seed)
            if sort == RANDOM_SORT
            else last["score"]
        )
        next_after = _cursor(head, last["listing_lo"], last["listing_hi"])

    return {
        "data": {
            "items": items,
            "has_more": has_more,
            "next_after": next_after,
            "generation": generation,
            "min_score": min_score,
            "sort": sort,
            "seed": sample_seed,
            "total": total,
        },
        "store_ready": True,
    }


# ------------------------------------------------------- the candidate groups (§12, E56)


def _candidate_index(conn: Any, generation: str) -> candidate_groups.CandidateIndex:
    """This generation's candidate groups, built once per store fingerprint.

    Three cheap statements: the fingerprint, the residual cohort (ids + score + zone + family
    mask + block, a few thousand rows) and the generation's locks. The packing itself is pure
    Python, so a second page of the same queue re-reads nothing."""
    params = {"generation": generation, "min_score": RESIDUAL_MIN_SCORE}
    stamp = _fetch(conn, usql.CANDIDATE_FINGERPRINT_SQL, params)
    fingerprint = tuple(_jsonable(value) for value in (stamp[0] if stamp else ()))

    def build() -> tuple[candidate_groups.CandidateGroup, ...]:
        pairs = [
            candidate_groups.ResidualPair(
                listing_lo=int(row["listing_lo"]),
                listing_hi=int(row["listing_hi"]),
                score=float(row["score"] or 0.0),
                zone=row["zone"],
                families=int(row["families"] or 0),
                block_key=None if row["block_key"] is None else int(row["block_key"]),
                block_grain=row["block_grain"],
            )
            for row in _rows(
                usql.CANDIDATE_PAIR_COLUMNS, _fetch(conn, usql.CANDIDATE_PAIRS_SQL, params)
            )
        ]
        locks = [
            (int(row[0]), int(row[1]))
            for row in _fetch(
                conn, usql.CANDIDATE_CLUSTER_MEMBERS_SQL, {"generation": generation}
            )
        ]
        return candidate_groups.build_candidates(pairs, locks)

    return candidate_groups.cached_index(generation, fingerprint, build)


def _pair_verdict_map(conn: Any) -> dict[tuple[int, int], str]:
    """Every pair the operator has ruled on, latest ruling per pair."""
    return {
        (int(row["listing_lo"]), int(row["listing_hi"])): str(row["verdict"])
        for row in _rows(
            usql.CANDIDATE_VERDICT_COLUMNS, _fetch(conn, usql.OPERATOR_PAIR_VERDICTS_SQL)
        )
    }


def _candidate_review(
    group: candidate_groups.CandidateGroup, verdicts: dict[tuple[int, int], str]
) -> dict[str, Any]:
    """Whether the operator has answered this card — DERIVED off the pair verdicts (E56).

    A candidate group is not a row anywhere, so "reviewed" cannot be stored on it: it is
    reviewed when every residual pair inside it carries an operator pair verdict. Partly-ruled
    groups are counted too (`n_pairs_reviewed`), because a card that was abandoned halfway is
    exactly what the operator wants to find again."""
    ruled = [verdicts.get((pair.listing_lo, pair.listing_hi)) for pair in group.pairs]
    n_ruled = sum(1 for value in ruled if value is not None)
    return {
        "n_pairs": len(group.pairs),
        "n_pairs_reviewed": n_ruled,
        "reviewed": bool(group.pairs) and n_ruled == len(group.pairs),
        "n_pairs_not_same": sum(
            1 for value in ruled if value is not None and value != "same"
        ),
    }


def _candidate_order(
    groups: list[candidate_groups.CandidateGroup], sort: str, seed: str
) -> list[candidate_groups.CandidateGroup]:
    """The four orders, each a TOTAL order (the key always ends in the candidate key).

    `weakest` first, like the groups queue: the weakest link of a card is where its error is.
    `strongest` is the residual queue's working order — the likeliest duplicates on top.
    `random` is the seeded, unbiased one D6 is measured on, keyed on the candidate key so it
    is stable across pages, reloads and days for one seed (E55)."""
    if sort == "strongest":
        return sorted(groups, key=lambda g: (-(g.score_max or 0.0), g.candidate_key))
    if sort == "largest":
        return sorted(groups, key=lambda g: (-g.size, g.candidate_key))
    if sort == RANDOM_SORT:
        return sorted(groups, key=lambda g: (_seeded_hash(g.candidate_key, seed), g.candidate_key))
    return sorted(groups, key=lambda g: ((g.score_min if g.score_min is not None else -1.0),
                                         g.candidate_key))


def _candidate_matches(
    group: candidate_groups.CandidateGroup,
    zone: str | None,
    block: int | None,
    block_grain: str | None,
    verdict: str | None,
    review: dict[str, Any],
) -> bool:
    """The filter bar, applied to a BUILT group — never to the packing.

    A filter that narrowed the cohort before the packing would hand the operator different
    groups per filter combination, and a card whose membership changes with a select is not a
    card anybody can rule on."""
    if zone is not None and zone not in group.zones():
        return False
    if block is not None:
        blocks = group.blocks()
        if not any(
            code == block and (block_grain is None or grain == block_grain)
            for code, grain in blocks
        ):
            return False
    if verdict == "reviewed" and not review["reviewed"]:
        return False
    if verdict == "unreviewed" and review["reviewed"]:
        return False
    return True


def _candidate_header(
    group: candidate_groups.CandidateGroup,
    generation: str,
    review: dict[str, Any],
) -> dict[str, Any]:
    """The card's own facts — no judge artefact among them (E55: blind mode is the page's
    default here, and a header chip that leaked the judge's word would defeat it)."""
    blocks = group.blocks()
    return {
        "candidate_key": group.candidate_key,
        "generation": generation,
        "size": group.size,
        "n_units": group.n_units,
        "score_min": group.score_min,
        "score_max": group.score_max,
        "zones": group.zones(),
        "families": group.families(),
        "family_names": _families(group.families()),
        "block_key": blocks[0][0] if blocks else None,
        "block_grain": blocks[0][1] if blocks else None,
        "locked_cluster_keys": list(group.cluster_keys),
        "units": [
            {
                "unit_key": unit.key,
                "cluster_key": unit.cluster_key,
                "listing_ids": list(unit.listing_ids),
            }
            for unit in group.units
        ],
        **review,
    }


def _candidate_locks(group: candidate_groups.CandidateGroup) -> dict[int, dict[str, Any]]:
    """listing id -> which unit it belongs to, and whether that unit is a LOCK.

    `unit_lock` is the g4 cluster key or null, and it is the whole contract between the packing
    and the card: members sharing a lock share one letter, and the save refuses anything else."""
    return {
        listing_id: {"unit_key": unit.key, "unit_lock": unit.cluster_key}
        for unit in group.units
        for listing_id in unit.listing_ids
    }


def _candidate_members(
    conn: Any, groups: list[candidate_groups.CandidateGroup]
) -> dict[int, dict[str, Any]]:
    """ONE statement over every listing the page shows — not one per group (the N+1 the groups
    queue avoids by keying its member statement on `any(keys)`)."""
    ids = sorted({listing_id for group in groups for listing_id in group.listing_ids})
    if not ids:
        return {}
    rows = _rows(
        usql.LISTING_CARD_COLUMNS,
        _fetch(conn, usql.LISTING_CARDS_SQL, {"ids": ids, "card_frames": GROUP_CARD_IMAGES}),
    )
    return {int(row["listing_id"]): _member_row(row) for row in rows}


@router.get("/candidates")
def candidates(
    request: Request,
    generation: str | None = Query(None),
    after: str | None = Query(None),
    limit: int = Query(GROUP_PAGE_SIZE, ge=1, le=GROUP_MAX_PAGE_SIZE),
    block: int | None = Query(None),
    block_grain: str | None = Query(None),
    zone: str | None = Query(None),
    verdict: str | None = Query(None),
    sort: str = Query("weakest"),
    seed: str | None = Query(None),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """The residual cohort as CANDIDATE GROUPS — the Groups card UX on the unmerged side (E56).

    The pair queue asks one question per pair, and the pairs are not independent: one advert
    against each member of a merged group is the same question asked five times. So the pairs
    are lifted to UNIT level (an existing cluster of this generation, whole, or a lone advert),
    packed greedily by score under an 8-advert cap, and served as cards. Every residual pair
    lands in exactly one card, so nothing stops being asked.
    """
    _reject_unknown_filters(request, CANDIDATE_FILTER_KEYS)
    _one_of("sort", sort, CANDIDATE_SORTS)
    sample_seed = _seed(seed)
    _one_of("zone", zone, ZONE_VALUES)
    _one_of("verdict", verdict, CANDIDATE_VERDICT_VALUES)
    _one_of("block_grain", block_grain, usql.BLOCK_GRAIN_VALUES)
    if after is not None and not CANDIDATE_KEY_RE.match(after):
        raise _bad("after is not a cursor from this endpoint")
    if not store_ready(conn):
        return _not_ready()

    empty = {
        "items": [],
        "has_more": False,
        "next_after": None,
        "generation": None,
        "sort": sort,
        "seed": sample_seed,
        "total": 0,
    }
    try:
        resolved = _resolve_generation(conn, generation)
        if resolved is None:
            # A store with no clustering resolves to no pass at all (E54) — and with no locks
            # there is no unit vocabulary either, so the honest answer is an empty queue.
            return {"data": empty, "store_ready": True}
        index = _candidate_index(conn, resolved)
        verdicts = _pair_verdict_map(conn)
        reviews = {
            group.candidate_key: _candidate_review(group, verdicts) for group in index.groups
        }
        selected = [
            group
            for group in index.groups
            if _candidate_matches(
                group, zone, block, block_grain, verdict, reviews[group.candidate_key]
            )
        ]
        ordered = _candidate_order(selected, sort, sample_seed)
        start = 0
        if after is not None:
            keys = [group.candidate_key for group in ordered]
            if after not in keys:
                # The group the cursor names is gone (a new score run repacked it, or a filter
                # moved). Starting from the top is wrong and guessing is worse, so say so.
                raise _bad("after names no group of this queue — reload the first page")
            start = keys.index(after) + 1
        page = ordered[start:start + limit]
        has_more = len(ordered) > start + limit
        members = _candidate_members(conn, page)
        by_group = {
            group.candidate_key: [
                members[listing_id]
                for listing_id in group.listing_ids
                if listing_id in members
            ]
            for group in page
        }
        member_verdicts = _member_verdicts(conn, by_group)
    except _STORE_BEHIND:
        return _not_ready()

    items: list[dict[str, Any]] = []
    for group in page:
        locks = _candidate_locks(group)
        items.append(
            {
                **_candidate_header(group, resolved, reviews[group.candidate_key]),
                "sources": sorted(
                    {
                        str(member["source"])
                        for member in by_group[group.candidate_key]
                        if member["source"]
                    }
                ),
                "members": [
                    {**member, **locks.get(member["listing_id"], {})}
                    for member in by_group[group.candidate_key]
                ],
                # The stored ruling, so the unit letters hydrate after a reload (E50).
                "member_verdicts": member_verdicts.get(group.candidate_key, []),
            }
        )

    return {
        "data": {
            "items": items,
            "has_more": has_more,
            "next_after": page[-1].candidate_key if page and has_more else None,
            "generation": resolved,
            "sort": sort,
            "seed": sample_seed,
            # Counted on EVERY page, unlike the keyset queues: the whole ordered set is already
            # in memory, so "20 of N" costs nothing and cannot go stale between pages.
            "total": len(ordered),
        },
        "store_ready": True,
    }


@router.get("/candidates/{candidate_key}")
def candidate_detail(
    request: Request,
    candidate_key: str,
    generation: str | None = Query(None),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """One candidate group, whole: the members with their own advert text, every scored pair
    among them with "why it wasn't merged", and the judge's latest word per tier.

    The text is the reason this dialog exists, exactly as on the groups queue: a developer
    project's units share the photos and the attribute row and differ only in what the ad says.
    It reaches the response through the judge's own scrubber and no other path (E28)."""
    _reject_unknown_filters(request, DETAIL_FILTER_KEYS)
    if not CANDIDATE_KEY_RE.match(candidate_key):
        raise _bad("candidate_key is not a key from this endpoint")
    if not store_ready(conn):
        return _not_ready()
    try:
        resolved = _resolve_generation(conn, generation)
        if resolved is None:
            raise HTTPException(status_code=404, detail="no such candidate group")
        index = _candidate_index(conn, resolved)
        group = index.get(candidate_key)
        if group is None:
            raise HTTPException(status_code=404, detail="no such candidate group")
        ids = list(group.listing_ids)
        cards = _candidate_members(conn, [group])
        galleries = _images_by_listing(conn, ids)
        texts = _member_texts(conn, ids)
        locks = _candidate_locks(group)
        members = [
            {
                **cards[listing_id],
                **locks.get(listing_id, {}),
                "images": galleries.get(listing_id, []),
                **texts.get(listing_id, _NO_TEXT),
            }
            for listing_id in ids
            if listing_id in cards
        ]
        residual_keys = {(pair.listing_lo, pair.listing_hi) for pair in group.pairs}
        pairs = []
        for row in _rows(usql.PAIR_COLUMNS, _fetch(conn, usql.CLUSTER_PAIRS_SQL, {"ids": ids})):
            view = _pair_view(row)
            view["why_not_merged"] = _why_not_merged(
                row["zone"], row["decision"], row["guard_veto"]
            )
            # Which of the scored edges among these adverts are the ones this CARD is asking
            # about: an edge inside a locked group is evidence, never a question here.
            view["residual"] = (
                int(row["listing_lo"]), int(row["listing_hi"])
            ) in residual_keys
            pairs.append(view)
        judgements = (
            _rows(
                usql.JUDGEMENT_COLUMNS,
                _fetch(
                    conn,
                    usql.JUDGEMENTS_LATEST_SQL,
                    {
                        "los": [pair["listing_lo"] for pair in pairs],
                        "his": [pair["listing_hi"] for pair in pairs],
                    },
                ),
            )
            if pairs
            else []
        )
        member_verdicts = _rows(
            usql.VERDICT_COLUMNS, _fetch(conn, usql.MEMBER_PAIR_VERDICTS_SQL, {"ids": ids})
        )
        verdicts = _pair_verdict_map(conn)
    except _STORE_BEHIND:
        return _not_ready()

    return {
        "data": {
            "candidate": _candidate_header(
                group, resolved, _candidate_review(group, verdicts)
            ),
            "members": members,
            "pairs": pairs,
            "judgements": judgements,
            "member_verdicts": member_verdicts,
        },
        "store_ready": True,
    }


# --------------------------------------------------------------- the blocks of a generation


@router.get("/blocks")
def blocks(
    request: Request,
    generation: str | None = Query(None),
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
        generation = _resolve_generation(conn, generation)
        rows = _rows(
            usql.BLOCK_COLUMNS,
            _fetch(conn, usql.BLOCKS_SQL, {"generation": generation, "limit": BLOCKS_LIMIT}),
        )
    except _STORE_BEHIND:
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


# ------------------------------------------------------- the validation session (D6) — where am I?


@router.get("/validation-progress")
def validation_progress(
    request: Request,
    generation: str | None = Query(None),
    seed: str | None = Query(None),
    surface: str = Query("groups"),
    min_score: float = Query(RESIDUAL_MIN_SCORE),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """"How many do I need to do?" — in two numbers, for the surface that asked.

    `total` is the whole generation: how much of the queue carries a ruling at all. `sample` is
    the first 100 of the SEEDED order (D6's unbiased draw), which is the number that gates the
    program — an error rate measured on the weakest-edge queue is an error rate about the
    weakest edges. The sample is deliberately NOT narrowed by the filter bar: a sample that
    moved with the filters would mean something different on every page of the same session.
    """
    _reject_unknown_filters(request, PROGRESS_FILTER_KEYS)
    _one_of("surface", surface, VALIDATION_SURFACES)
    sample_seed = _seed(seed)
    if not store_ready(conn):
        return _not_ready()

    groups = surface == "groups"
    params = {
        "generation": generation,
        "seed": sample_seed,
        "sample_size": VALIDATION_SAMPLE_SIZE,
        "min_score": min_score,
    }
    try:
        generation = _resolve_generation(conn, generation)
        params["generation"] = generation
        if surface == "candidates":
            sample, total = _candidate_counts(conn, generation, sample_seed)
        else:
            sample = _counts(
                conn,
                usql.VALIDATION_GROUPS_SAMPLE_SQL
                if groups
                else usql.VALIDATION_RESIDUAL_SAMPLE_SQL,
                params,
            )
            total = _counts(
                conn,
                usql.VALIDATION_GROUPS_TOTAL_SQL
                if groups
                else usql.VALIDATION_RESIDUAL_TOTAL_SQL,
                params,
            )
    except _STORE_BEHIND:
        return _not_ready()

    return {
        "data": {
            "generation": generation,
            "surface": surface,
            "seed": sample_seed,
            "sample_size": VALIDATION_SAMPLE_SIZE,
            # The grain each number is counted at, said out loud: a cluster verdict, a pair
            # verdict and a candidate CARD are three units of work and must never be added up
            # on a page. A candidate card is reviewed when every pair inside it is (E56), so
            # its counter is deliberately not comparable with the pair queue's.
            "grain": "cluster" if groups else "candidate" if surface == "candidates" else "pair",
            "sample": sample,
            "total": total,
        },
        "store_ready": True,
    }


def _candidate_counts(
    conn: Any, generation: str | None, seed: str
) -> tuple[dict[str, int], dict[str, int]]:
    """The candidate queue's two counters, at CARD grain (E56).

    Both halves are computed off the cached index rather than a statement, because a candidate
    group is not a row: the whole generation, and the first 100 of the SEEDED order over it —
    the same order `sort=random` pages, so the sample the strip counts is the sample the
    operator walks. Unfiltered, like the other two surfaces' samples are."""
    zero = {name: 0 for name in usql.VALIDATION_COUNT_COLUMNS}
    if generation is None:
        return zero, dict(zero)
    index = _candidate_index(conn, generation)
    verdicts = _pair_verdict_map(conn)
    ordered = _candidate_order(list(index.groups), RANDOM_SORT, seed)

    def counted(groups: list[candidate_groups.CandidateGroup]) -> dict[str, int]:
        reviews = [_candidate_review(group, verdicts) for group in groups]
        return {
            "n": len(groups),
            "n_reviewed": sum(1 for review in reviews if review["reviewed"]),
            "n_not_same": sum(
                1
                for review in reviews
                if review["reviewed"] and review["n_pairs_not_same"] > 0
            ),
        }

    return counted(ordered[:VALIDATION_SAMPLE_SIZE]), counted(ordered)


def _counts(conn: Any, sql: str, params: dict[str, Any]) -> dict[str, int]:
    """One row of `VALIDATION_COUNT_COLUMNS`, as ints. An empty answer is three zeros — the
    statement is an aggregate, so it always returns a row; this is the fake-connection floor."""
    rows = _fetch(conn, sql, params)
    if not rows:
        return {name: 0 for name in usql.VALIDATION_COUNT_COLUMNS}
    row = _row(usql.VALIDATION_COUNT_COLUMNS, rows[0])
    return {name: int(row[name] or 0) for name in usql.VALIDATION_COUNT_COLUMNS}


# ----------------------------------------------------------- operator vs judge (the D6 gate)


@router.get("/agreement")
def agreement(
    request: Request,
    generation: str | None = Query(None),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """How often the operator and the LLM judge say the same thing about one pair (D6).

    The operator's label set is explicit pair verdicts UNION the pairs IMPLIED by confirmed
    clusters; the judge's is the best tier per pair (gold > vision > text, `oss` excluded).
    `insufficient_evidence` is counted, never scored. The arithmetic — the binary mapping and
    the Wilson interval — is `autodedup/agreement.py`, so it can be checked by hand."""
    _reject_unknown_filters(request, AGREEMENT_FILTER_KEYS)
    if not store_ready(conn):
        return _not_ready()
    try:
        generation = _resolve_generation(conn, generation)
        params = {
            "generation": generation,
            "max_cluster_size": AGREEMENT_MAX_CLUSTER_SIZE,
        }
        rows = _rows(usql.AGREEMENT_COLUMNS, _fetch(conn, usql.AGREEMENT_PAIRS_SQL, params))
        oversize_rows = _fetch(conn, usql.AGREEMENT_OVERSIZE_SQL, params)
    except _STORE_BEHIND:
        return _not_ready()

    summary = agreement_math.summarize(rows)
    n_oversize = int(oversize_rows[0][0] or 0) if oversize_rows and oversize_rows[0] else 0
    return {
        "data": {
            "generation": generation,
            **summary,
            # The bound of the implied half, reported rather than assumed away.
            "max_cluster_size": AGREEMENT_MAX_CLUSTER_SIZE,
            "n_clusters_over_cap": n_oversize,
        },
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
        # The pair itself is generation-free — `autodedup.pairs` is one scored edge, not a
        # clustering. The echo still names the pass the caller is validating against, so a
        # link written from this page cannot silently mean a superseded one.
        generation = _resolve_generation(conn, generation)
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
    except _STORE_BEHIND:
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
    # WHY the operator ruled this way (migration 533), validated against the registry.
    reasons: list[str] = Field(default_factory=list,
                               max_length=reasons_registry.MAX_REASONS)


@router.get("/verdict-reasons")
def verdict_reasons() -> dict[str, Any]:
    """The reason vocabulary, served so the SPA hard-codes none of it (§9).

    No `store_ready` and no connection: the registry is code, not data, so the chips render
    against a database that has not been migrated at all — and a page that cannot reach the
    list would silently offer the operator an empty one."""
    return {"data": {"reasons": reasons_registry.registry()}}


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

    A cluster verdict of `same`, though, RETRACTS the operator's own veto on every pair of the
    group (E52). "These are all one property" and a standing must-not-link between two of them
    are a contradiction `guards.py` resolves against the page: it would keep refusing the union
    while this surface showed a green badge — the correction path the pair route already has,
    at the grain the button is offered on.

    Unlike the reads, an un-migrated store is a 503 here and not a `store_ready: false` 200: a
    write that silently did nothing would be recorded by the optimistic client as a decision.
    """
    _one_of("kind", body.kind, VERDICT_KINDS)
    _one_of("verdict", body.verdict, VERDICT_VALUES)
    reasons = _reasons(body.reasons)
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
        try:
            stored = _fetch(
                conn,
                usql.VERDICT_PAIR_UPSERT_SQL,
                {
                    "listing_lo": body.listing_lo,
                    "listing_hi": body.listing_hi,
                    "verdict": body.verdict,
                    "note": body.note,
                    "reasons": reasons,
                    "decided_by": str(decided_by),
                },
            )
        except _CHECK_VIOLATION as exc:
            raise _needs_migration_532() from exc
        except _UNDEFINED_COLUMN as exc:
            raise _needs_migration_533() from exc
        must_not_link = body.verdict in NEGATIVE_VERDICTS
        retracted = 0
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
            retracted = 1
    else:
        if body.cluster_key is None:
            raise _bad("a cluster verdict needs cluster_key")
        if body.listing_lo is not None or body.listing_hi is not None:
            raise _bad("a cluster verdict carries no listing ids")
        if not _fetch(conn, usql.CLUSTER_EXISTS_SQL, {"cluster_key": body.cluster_key}):
            raise HTTPException(status_code=404, detail="no such cluster")
        try:
            stored = _fetch(
                conn,
                usql.VERDICT_CLUSTER_UPSERT_SQL,
                {
                    "cluster_key": body.cluster_key,
                    "verdict": body.verdict,
                    "note": body.note,
                    "reasons": reasons,
                    "decided_by": str(decided_by),
                },
            )
        except _CHECK_VIOLATION as exc:
            raise _needs_migration_532() from exc
        except _UNDEFINED_COLUMN as exc:
            raise _needs_migration_533() from exc
        must_not_link = False
        retracted = 0
        if body.verdict == "same":
            ids = _cluster_member_ids(conn, body.cluster_key)
            for index, lo in enumerate(ids):
                for hi in ids[index + 1:]:
                    _execute(
                        conn,
                        usql.MUST_NOT_LINK_RETRACT_SQL,
                        {"listing_lo": lo, "listing_hi": hi},
                    )
                    retracted += 1

    return {
        "data": {
            "verdict": _row(usql.VERDICT_COLUMNS, stored[0]) if stored else None,
            "must_not_link": must_not_link,
            # Pairs whose operator veto this verdict dropped. The DELETE is a no-op where no
            # veto stood, so this is "pairs no longer vetoed by the operator", not "rows gone".
            "must_not_link_retracted": retracted,
        },
        "store_ready": True,
    }


class SplitUnitIn(BaseModel):
    listing_id: int
    unit: str = Field(max_length=40)


class SplitRelationIn(BaseModel):
    """The relation between TWO units of one split.

    One relation for a whole split cannot describe the group the operator actually meets: two
    adverts are different units of one BUILDING while a third is a different building of the
    same DEVELOPMENT. Stamping either statement onto the other pair records a building the
    adverts do not share, or throws the building away — and both land as permanent
    must-not-links and as calibration labels, corrupting the one distinction the developer
    rails (§2) are measured against. So the relation is per unit pair, with `relation` as the
    fill for the pairs the client did not name.
    """

    unit_a: str = Field(max_length=40)
    unit_b: str = Field(max_length=40)
    relation: str


class SplitIn(BaseModel):
    cluster_key: int
    generation: str
    units: list[SplitUnitIn]
    relation: str
    relations: list[SplitRelationIn] = Field(default_factory=list)
    # Saving a split that drops a veto the operator wrote earlier takes a second, deliberate
    # send: a blank-slate assignment must never silently retract a permanent must-not-link.
    confirm_retract: bool = False
    note: str | None = Field(default=None, max_length=2000)
    # Stamped on EVERY pair verdict the split writes and on the cluster row (migration 533):
    # a split is one ruling, so its reasons are one set — split per pair they would claim the
    # operator said something about each edge that they never said.
    reasons: list[str] = Field(default_factory=list,
                               max_length=reasons_registry.MAX_REASONS)


# How much two adverts have in common, weakest first. A split that says different things
# about different unit pairs is summarised on the CLUSTER by its weakest claim — the only
# statement true of the whole group.
RELATION_STRENGTH: dict[str, int] = {
    "different": 0,
    "same_project_different_unit": 1,
    "same_building_different_unit": 2,
}


def _unit_pair(unit_a: str, unit_b: str) -> tuple[str, str]:
    return (unit_a, unit_b) if unit_a <= unit_b else (unit_b, unit_a)


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


def _relation_summary(relations: dict[tuple[str, str], str]) -> str:
    """`A-B: same_building_different_unit · A-C: same_project_different_unit` — written only
    when the split says more than one thing, so the common case keeps its short note."""
    return " · ".join(
        f"{a}-{b}: {relation}" for (a, b), relation in sorted(relations.items())
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
    different units are the relation named for THOSE TWO UNITS (E51) — the same building, the
    same development project, or unrelated — and each such pair takes a permanent `must_not_link`,
    because a unit the operator has separated must never come back as a merge proposal. The
    cluster itself is stored as `same` when one unit was used and otherwise as the WEAKEST
    relation the split used, with the assignment as its note. Everything lands in ONE
    transaction: a half-applied split would leave the pair rows and the cluster row saying
    different things.

    A split that would RETRACT a veto the same operator wrote earlier is refused with a 409
    unless `confirm_retract` is set (E52). An assignment defaults every unnamed member into one
    unit, so a blank-slate save over a group that was already split would otherwise reverse
    the earlier ruling — permanently, and with nothing on any surface to show it.

    THE PAIR NEED NOT EXIST IN `autodedup.pairs`, which is why this route does not check it:
    a cluster is the union of the edges the engine accepted, so two members can sit in one
    cluster with no scored edge between them at all. CLUSTER MEMBERSHIP is the validation, and
    the assignment must cover every member exactly once and name nobody else.
    """
    _one_of("relation", body.relation, SPLIT_RELATIONS)
    reasons = _reasons(body.reasons)
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

    named: dict[tuple[str, str], str] = {}
    for entry in body.relations:
        unit_a, unit_b = entry.unit_a.strip(), entry.unit_b.strip()
        _one_of("relation", entry.relation, SPLIT_RELATIONS)
        if unit_a == unit_b:
            raise _bad("a relation is between two DIFFERENT units")
        if unit_a not in distinct or unit_b not in distinct:
            raise _bad(f"no such unit in this split: {unit_a if unit_a not in distinct else unit_b}")
        key = _unit_pair(unit_a, unit_b)
        if key in named:
            raise _bad(f"the units {key[0]} and {key[1]} are given two relations")
        named[key] = entry.relation

    def relation_of(lo: int, hi: int) -> str:
        return named.get(_unit_pair(assignment[lo], assignment[hi]), body.relation)

    # What the operator has already said about these pairs, under their OWN name — the upsert
    # conflicts on `decided_by`, so nobody else's ruling is at stake here.
    stored_verdicts: dict[tuple[int, int], str] = {}
    for row in _rows(
        usql.VERDICT_COLUMNS,
        _fetch(conn, usql.MEMBER_PAIR_VERDICTS_SQL, {"ids": member_ids}),
    ):
        if row["decided_by"] != str(decided_by):
            continue
        key = (int(row["listing_lo"]), int(row["listing_hi"]))
        # Newest first (the statement's own ORDER BY), so the first row per pair is the live one.
        stored_verdicts.setdefault(key, row["verdict"])

    pairs: list[tuple[int, int, str, bool]] = []
    reversed_pairs: list[tuple[int, int]] = []
    used: set[str] = set()
    for index, lo in enumerate(member_ids):
        for hi in member_ids[index + 1:]:
            same_unit = assignment[lo] == assignment[hi]
            relation = "same" if same_unit else relation_of(lo, hi)
            if not same_unit:
                used.add(relation)
            if same_unit and stored_verdicts.get((lo, hi)) in NEGATIVE_VERDICTS:
                reversed_pairs.append((lo, hi))
            pairs.append((lo, hi, relation, same_unit))

    if reversed_pairs and not body.confirm_retract:
        listed = ", ".join(f"{lo}-{hi}" for lo, hi in reversed_pairs[:8])
        raise HTTPException(
            status_code=409,
            detail=(
                f"this split takes back your earlier ruling on {len(reversed_pairs)} pair(s) "
                f"({listed}) and drops their permanent must-not-link — re-send with "
                "confirm_retract to go ahead"
            ),
        )

    summary = _split_summary(assignment)
    if len(distinct) == 1:
        cluster_verdict = "same"
    else:
        cluster_verdict = min(used, key=lambda value: RELATION_STRENGTH[value])
    parts = [summary]
    if len(used) > 1:
        parts.append(_relation_summary({
            _unit_pair(assignment[lo], assignment[hi]): relation
            for lo, hi, relation, same_unit in pairs
            if not same_unit
        }))
    if body.note:
        parts.insert(0, body.note)
    note = " · ".join(parts)

    n_pairs_same = 0
    n_pairs_negative = 0
    try:
        with conn.transaction():
            for lo, hi, relation, same_unit in pairs:
                _execute(
                    conn,
                    usql.VERDICT_PAIR_UPSERT_SQL,
                    {
                        "listing_lo": lo,
                        "listing_hi": hi,
                        "verdict": relation,
                        "note": f"operator split: {summary}",
                        # The reasons ride on the CLUSTER row alone. A split is ONE ruling;
                        # stamping it on the fan-out would post C(n,2) rows from a single
                        # click, so the pair histogram would measure cluster size instead of
                        # operator evidence and stop being comparable with the judge's
                        # per-pair `unit_discriminator` — the readout's whole purpose (§9).
                        "reasons": [],
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
                            "reason": f"operator split: {relation}",
                        },
                    )
            stored = _fetch(
                conn,
                usql.VERDICT_CLUSTER_UPSERT_SQL,
                {
                    "cluster_key": body.cluster_key,
                    "verdict": cluster_verdict,
                    "note": note,
                    "reasons": reasons,
                    "decided_by": str(decided_by),
                },
            )
    except _CHECK_VIOLATION as exc:
        raise _needs_migration_532() from exc
    except _UNDEFINED_COLUMN as exc:
        raise _needs_migration_533() from exc

    return {
        "data": {
            "cluster_verdict": _row(usql.VERDICT_COLUMNS, stored[0]) if stored else None,
            "n_pairs_same": n_pairs_same,
            "n_pairs_negative": n_pairs_negative,
            "must_not_link_written": n_pairs_negative,
            # Counted per same-unit pair: the DELETE is keyed on the pair and is a no-op where
            # no operator veto stood, so this is "pairs whose veto was dropped or never was".
            "must_not_link_retracted": n_pairs_same,
            # The pairs this save actually took back — the operator had ruled them negative
            # and the split re-ruled them as one unit. Named, so the page can say which.
            "reversed_pairs": [[lo, hi] for lo, hi in reversed_pairs],
        },
        "store_ready": True,
    }


class CandidateSplitIn(BaseModel):
    """The candidate card's one write — the split route's body, minus the cluster.

    Same fields, same meanings, same 409: `units` names every member exactly once, `relations`
    names each unit pair (E51) with `relation` as the fill, and `confirm_retract` is how a save
    that takes back an earlier veto says it meant to (E52).
    """

    candidate_key: str
    generation: str
    units: list[SplitUnitIn]
    relation: str
    relations: list[SplitRelationIn] = Field(default_factory=list)
    confirm_retract: bool = False
    note: str | None = Field(default=None, max_length=2000)
    # DELIBERATELY NOT ACCEPTED as chips. A split stamps its reasons on the CLUSTER row, and a
    # candidate group has no row: stamping them on the pairwise fan-out instead would post
    # C(n,2) reason rows from one click, so the §9 histogram would measure card size rather
    # than operator evidence and stop being comparable with the judge's `unit_discriminator`.
    # A non-empty list is a 400 rather than a silent drop — a reason the operator clicked and
    # the store never kept is worse than a reason they were never offered. The note stays.
    reasons: list[str] = Field(default_factory=list,
                               max_length=reasons_registry.MAX_REASONS)


@router.post("/verdict/candidate-split")
def verdict_candidate_split(
    body: CandidateSplitIn,
    claims: dict = Depends(deps.require_admin),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """One CANDIDATE GROUP, ruled unit by unit — many residual pairs answered in one save (E56).

    The fan-out, the permanent must-not-links, the retraction of the operator's own vetoes and
    the 409 confirmation are the split route's, unchanged: members sharing a letter are one
    property, members in different letters are the relation named for THOSE TWO LETTERS.

    TWO THINGS ARE DIFFERENT, and both follow from there being no cluster.
    (1) NO CLUSTER VERDICT IS WRITTEN. A candidate group is a packing of this generation's
        residual pairs, not a proposal the engine made, so there is nothing to confirm or
        reject — the ruling IS its pair rows, which is also how the card reads itself back.
    (2) A LOCK IS NEVER WRITTEN THROUGH. Two adverts the engine already merged into one g4
        group arrive locked to one letter, and the pairs INSIDE that lock are skipped: they are
        the Groups page's ruling, and this route neither confirms them nor retracts the
        must-not-links a split there wrote. An assignment that splits a lock is refused (400)
        and says where to do it instead.

    The pair need not exist in `autodedup.pairs` — the same reason the split route gives: the
    card's membership is the validation.
    """
    _one_of("relation", body.relation, SPLIT_RELATIONS)
    if body.reasons:
        raise _bad(
            "a candidate split writes no cluster row, so it carries no reason chips — "
            "put the why in the note"
        )
    if not CANDIDATE_KEY_RE.match(body.candidate_key):
        raise _bad("candidate_key is not a key from this endpoint")
    decided_by = claims.get("email") or claims.get("sub")
    if not decided_by:
        raise HTTPException(status_code=403, detail="the admin identity carries no email")
    if not store_ready(conn):
        raise HTTPException(status_code=503, detail="the autodedup store is not created yet")

    # A pass nothing clustered would make EVERY scored pair "unclustered" and pack a cohort
    # out of the whole table — so the name is checked before it is packed, not after.
    if not _fetch(conn, usql.GENERATION_EXISTS_SQL, {"generation": body.generation}):
        raise HTTPException(status_code=404, detail="no such generation")
    index = _candidate_index(conn, body.generation)
    group = index.get(body.candidate_key)
    if group is None:
        raise HTTPException(
            status_code=404, detail="no such candidate group in this generation"
        )
    member_ids = sorted(int(listing_id) for listing_id in group.listing_ids)

    assignment: dict[int, str] = {}
    for entry in body.units:
        unit = entry.unit.strip()
        if not unit:
            raise _bad("a unit label cannot be empty")
        if entry.listing_id in assignment:
            raise _bad(f"listing {entry.listing_id} is assigned to two units")
        assignment[entry.listing_id] = unit
    if set(assignment) != set(member_ids):
        raise _bad(
            "the unit assignment must name every advert of the candidate group, and only them"
        )
    distinct = sorted(set(assignment.values()))
    if len(distinct) > MAX_SPLIT_UNITS:
        raise _bad(f"a split names at most {MAX_SPLIT_UNITS} units")

    # THE LOCK RULE. Adverts the engine merged share one unit letter or the save is refused —
    # separating them is a statement about that g4 group, and the Groups page is where a group
    # is split (with a cluster verdict, which this route does not write).
    lock_of: dict[int, int | None] = {
        listing_id: unit.cluster_key
        for unit in group.units
        for listing_id in unit.listing_ids
    }
    by_lock: dict[int, set[str]] = {}
    for listing_id, cluster_key in lock_of.items():
        if cluster_key is not None:
            by_lock.setdefault(cluster_key, set()).add(assignment[listing_id])
    for cluster_key, letters in sorted(by_lock.items()):
        if len(letters) > 1:
            raise _bad(
                f"the adverts of merged group #{cluster_key} are one unit here and must share "
                "one letter — split that group on the Groups page instead"
            )

    named: dict[tuple[str, str], str] = {}
    for entry in body.relations:
        unit_a, unit_b = entry.unit_a.strip(), entry.unit_b.strip()
        _one_of("relation", entry.relation, SPLIT_RELATIONS)
        if unit_a == unit_b:
            raise _bad("a relation is between two DIFFERENT units")
        if unit_a not in distinct or unit_b not in distinct:
            raise _bad(
                f"no such unit in this split: {unit_a if unit_a not in distinct else unit_b}"
            )
        key = _unit_pair(unit_a, unit_b)
        if key in named:
            raise _bad(f"the units {key[0]} and {key[1]} are given two relations")
        named[key] = entry.relation

    stored_verdicts: dict[tuple[int, int], str] = {}
    for row in _rows(
        usql.VERDICT_COLUMNS,
        _fetch(conn, usql.MEMBER_PAIR_VERDICTS_SQL, {"ids": member_ids}),
    ):
        if row["decided_by"] != str(decided_by):
            continue
        key = (int(row["listing_lo"]), int(row["listing_hi"]))
        stored_verdicts.setdefault(key, row["verdict"])

    pairs: list[tuple[int, int, str, bool]] = []
    reversed_pairs: list[tuple[int, int]] = []
    n_skipped_locked = 0
    for index_lo, lo in enumerate(member_ids):
        for hi in member_ids[index_lo + 1:]:
            lock = lock_of.get(lo)
            if lock is not None and lock_of.get(hi) == lock:
                n_skipped_locked += 1
                continue
            same_unit = assignment[lo] == assignment[hi]
            relation = (
                "same"
                if same_unit
                else named.get(_unit_pair(assignment[lo], assignment[hi]), body.relation)
            )
            if same_unit and stored_verdicts.get((lo, hi)) in NEGATIVE_VERDICTS:
                reversed_pairs.append((lo, hi))
            pairs.append((lo, hi, relation, same_unit))

    if reversed_pairs and not body.confirm_retract:
        listed = ", ".join(f"{lo}-{hi}" for lo, hi in reversed_pairs[:8])
        raise HTTPException(
            status_code=409,
            detail=(
                f"this split takes back your earlier ruling on {len(reversed_pairs)} pair(s) "
                f"({listed}) and drops their permanent must-not-link — re-send with "
                "confirm_retract to go ahead"
            ),
        )

    summary = _split_summary(assignment)
    note = f"operator candidate split: {summary}"
    if body.note:
        note = f"{body.note} · {note}"

    n_pairs_same = 0
    n_pairs_negative = 0
    try:
        with conn.transaction():
            for lo, hi, relation, same_unit in pairs:
                _execute(
                    conn,
                    usql.VERDICT_PAIR_UPSERT_SQL,
                    {
                        "listing_lo": lo,
                        "listing_hi": hi,
                        "verdict": relation,
                        "note": note,
                        # The fan-out carries no reason chips — see the body's own comment.
                        "reasons": [],
                        "decided_by": str(decided_by),
                    },
                )
                if same_unit:
                    n_pairs_same += 1
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
                            "reason": f"operator split: {relation}",
                        },
                    )
    except _CHECK_VIOLATION as exc:
        raise _needs_migration_532() from exc
    except _UNDEFINED_COLUMN as exc:
        raise _needs_migration_533() from exc

    return {
        "data": {
            "candidate_key": body.candidate_key,
            # There is no cluster here, and the key says so rather than leaving the client to
            # guess from an absent field: the split route's own shape, honestly empty.
            "cluster_verdict": None,
            "n_pairs_same": n_pairs_same,
            "n_pairs_negative": n_pairs_negative,
            "must_not_link_written": n_pairs_negative,
            "must_not_link_retracted": n_pairs_same,
            # Pairs inside one already-merged group: the Groups page's ruling, untouched.
            "n_pairs_locked": n_skipped_locked,
            "reversed_pairs": [[lo, hi] for lo, hi in reversed_pairs],
        },
        "store_ready": True,
    }
