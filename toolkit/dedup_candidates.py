"""NEW DEDUP Level 0 — candidate selection: what a path is, what its rungs need, the
inputs fingerprint that identifies a parameter set, the pair rule as pure Python, and the
generation-run lifecycle over the store of migration 492.

Design: docs/design/new-dedup/PROGRAM.md — "Simulation architecture" (evidence tier) and
the ledger entry of 2026-09-10 (a), where the operator ruled path C. The vocabulary, once:

* A PATH is one way of looking for pairs. Path C blocks on the TOWN (the projection's
  `obec_kod`, see docs/design/location-serving-contract.md) and compares attributes;
  path A (not built yet) blocks on street / geo cells and a radius. Path B (Wave 3) finds
  pairs by image similarity. A path is a `PathDef` row in `PATHS`, not a second engine —
  the store, the lifecycle and the audit page are path-agnostic.
* A RUNG is which attributes the pair was compared on. Path C has C1 (town + disposition)
  and C3 (town + area); C2 does not exist because "town" already replaced both of path A's
  first two location tests. A pair is evaluated on exactly ONE rung: C1 when both sides
  carry a disposition, else C3 — the operator's "if not available then fall back" rule.
  A disposition MISMATCH never falls back; only absence does.
* "NOT AVAILABLE", per side and per field: disposition = NULL or blank after trimming;
  area = NULL or zero (`usable_area`, `estate_area` for pozemek); town = no `obec_kod` on
  the projection row (or no projection row at all); floor = NULL. A missing floor does not
  drop the pair — the byt ±N floor rule simply cannot be checked, and the pair is kept with
  `floor_checked = false` so the audit can count how often that happened.
* The category rules are the merge chokepoint's (toolkit/property_identity.py), not a
  second opinion: sale and rent never pair (`category_type` equal, NULL = unknown = not a
  conflict) and `category_main_compatible` allows only the dům ↔ komerční cross-type.

`evaluate_pair` is the rule as Python. It is the ORACLE the set-based SQL of the generation
lane is held to in tests; the lane never calls it per pair.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from toolkit import dedup_sim_settings as dss
from toolkit.room_taxonomy import category_main_compatible

if TYPE_CHECKING:
    import psycopg


# Bump a path's version when the MEANING of its SQL changes (a predicate, a rung, an
# availability definition). It is part of the fingerprint, so every pair generated under the
# old meaning lands in a different key space from the new one — never silently mixed.
GENERATOR_VERSION: dict[str, str] = {"C": "c2"}


@dataclass(frozen=True)
class RungDef:
    code: str
    label: str
    needs: tuple[str, ...]
    explanation: str


@dataclass(frozen=True)
class PathDef:
    code: str
    label: str
    block_key: str
    district_key: str
    floor_feature: str
    settings_keys: tuple[str, ...]
    rungs: tuple[RungDef, ...]
    explanation: str


PATHS: dict[str, PathDef] = {
    "C": PathDef(
        code="C",
        label="town + attributes",
        block_key="obec_kod",
        district_key="cast_obce_kod",
        floor_feature="dedup_path_c",
        settings_keys=(
            "l0_path_c_town_key",
            "l0_path_c_district_key",
            "l0_path_c_district_split_towns",
            "l0_candidate_scope",
            "l0_floor_tolerance",
            "l0_c1_area_tolerance_pct",
            "l0_area_tolerance_pct_general",
            "l0_area_tolerance_pct_pozemek",
        ),
        rungs=(
            RungDef(
                code="C1",
                label="town + disposition + area",
                needs=("disposition",),
                explanation=(
                    "Both listings are in the same town and state the same disposition "
                    "(e.g. 2+kk). When both also state an area, the two areas must be "
                    "within the disposition rung's own, wide tolerance — a sanity check "
                    "on top of the disposition, not the match itself; when either states "
                    "no area the check is skipped and the pair is kept. For apartments "
                    "the floors, when both are known, must be within the floor tolerance."
                ),
            ),
            RungDef(
                code="C3",
                label="town + area",
                needs=("area",),
                explanation=(
                    "Taken only when at least one side has no disposition: same town and "
                    "the two areas differ by no more than the area tolerance (a percent of "
                    "the larger one; the pozemek tolerance when a side is land). The floor "
                    "rule applies as on C1."
                ),
            ),
        ),
        explanation=(
            "Path C replaces every 'street + coordinates + N metres' test of path A with "
            "'same town' (the location engine's obec_kod), because the input location data "
            "is only reliably right at town grain. There is no radius. C2 does not exist: "
            "town already stands in for both of path A's first two location tests. In the "
            "few towns big enough for 'same town' to mean very little — Praha, Brno, "
            "Ostrava — the town is split by city district, but only for the listings that "
            "state one: a listing whose district is unknown still reaches the whole town, "
            "so the split costs no reach."
        ),
    ),
}

PATH_CODES: tuple[str, ...] = ("A", "B", "C")


def path_def(code: str) -> PathDef:
    try:
        return PATHS[code]
    except KeyError as exc:
        raise KeyError(f"no candidate path {code!r} is defined (defined: {sorted(PATHS)})") from exc


# --------------------------------------------------------------------------- inputs


def path_inputs(code: str, settings: dict[str, Any]) -> dict[str, Any]:
    """The parameter set one generation runs under: the path's settings (by registry key),
    the path code and its generator version. Only these enter the fingerprint — a knob the
    path does not read must not re-generate its pairs."""
    pd = path_def(code)
    missing = [k for k in pd.settings_keys if k not in settings]
    if missing:
        raise KeyError(f"path {code} inputs missing settings {missing}")
    inputs: dict[str, Any] = {
        "path": pd.code,
        "generator_version": GENERATOR_VERSION[pd.code],
    }
    for k in pd.settings_keys:
        inputs[k] = settings[k]
    if pd.code == "C" and inputs["l0_path_c_district_key"] != pd.district_key:
        raise ValueError(
            f"l0_path_c_district_key={inputs['l0_path_c_district_key']!r} is not implemented "
            f"by path C's SQL (district key {pd.district_key!r})"
        )
    if pd.code == "C" and inputs["l0_path_c_town_key"] != pd.block_key:
        # The setting exists so the town key is visible and in the fingerprint; the
        # generation SQL implements exactly one column. A second choice is a code change
        # to both, never a silent no-op.
        raise ValueError(
            f"l0_path_c_town_key={inputs['l0_path_c_town_key']!r} is not implemented by path C's "
            f"SQL (block key {pd.block_key!r})"
        )
    return inputs


def _canonical(value: Any) -> Any:
    """JSON has no int/float distinction but Python does: an override typed as 5.0 and the
    default 5 are the same parameter set, so integral floats hash as ints."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def fingerprint(inputs: dict[str, Any]) -> str:
    """16 hex chars of the SHA-256 over the canonical JSON of `inputs`. Canonical = sorted
    keys, no whitespace, integral floats as ints — so the same values always hash the same
    regardless of dict order or how a number was typed."""
    canon = json.dumps(_canonical(inputs), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def inputs_for_path(code: str, conn: "psycopg.Connection | None" = None) -> dict[str, Any]:
    return path_inputs(code, dss.effective_settings(conn))


# --------------------------------------------------------------------------- the rule


@dataclass(frozen=True)
class ListingAttrs:
    """The attribute side of a listing as the rule sees it. `block_key` is the town
    (obec_kod) for path C; None means "not available" for every field."""

    listing_id: int
    block_key: str | None
    category_type: str | None
    category_main: str | None
    disposition: str | None
    floor: int | None
    usable_area: float | None
    estate_area: float | None
    district: str | None = None


@dataclass(frozen=True)
class PairVerdict:
    rung: str
    disposition: str | None
    area_lo: float | None
    area_hi: float | None
    area_diff_pct: float | None
    floor_checked: bool


def disposition_available(value: str | None) -> bool:
    return bool(value) and bool(value.strip())


def normalized_disposition(value: str | None) -> str | None:
    return value.strip() if disposition_available(value) else None


def area_of(category_main: str | None, usable_area: float | None, estate_area: float | None) -> float | None:
    """The area the rule compares: plot area for land, floor area otherwise; None when
    missing or zero (a zero area is a parser blank, not a measurement)."""
    raw = estate_area if category_main == "pozemek" else usable_area
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def area_diff_pct(a: float, b: float) -> float:
    return abs(a - b) / max(a, b) * 100.0


def split_towns(inputs: dict[str, Any]) -> tuple[str, ...]:
    """The town codes that are split by city district, as strings."""
    raw = str(inputs.get("l0_path_c_district_split_towns") or "")
    return tuple(t.strip() for t in raw.split(",") if t.strip())


def district_of(
    block_key: str | None, district_code: str | None, inputs: dict[str, Any]
) -> str | None:
    """A listing's district for blocking: its district code when its town is one of the
    split towns and the code is known, else None. None means "reaches the whole town"."""
    if block_key is None or district_code is None:
        return None
    return district_code if str(block_key) in split_towns(inputs) else None


def districts_compatible(a: "ListingAttrs", b: "ListingAttrs") -> bool:
    """Two listings in one split town must name the same district — but only when BOTH
    name one. An unknown district cannot veto, exactly as an unknown floor cannot."""
    if a.district is None or b.district is None:
        return True
    return a.district == b.district


def area_tolerance_pct(cm_a: str | None, cm_b: str | None, inputs: dict[str, Any]) -> float:
    if "pozemek" in (cm_a, cm_b):
        return float(inputs["l0_area_tolerance_pct_pozemek"])
    return float(inputs["l0_area_tolerance_pct_general"])


def categories_compatible(a: ListingAttrs, b: ListingAttrs) -> bool:
    """The merge chokepoint's two guards: sale ≠ rent (NULL = unknown, not a conflict),
    and category_main equal or the one sanctioned dům ↔ komerční cross-type."""
    if a.category_type is not None and b.category_type is not None:
        if a.category_type != b.category_type:
            return False
    return category_main_compatible(a.category_main, b.category_main)


def floor_rule(a: ListingAttrs, b: ListingAttrs, tolerance: int) -> tuple[bool, bool]:
    """(checked, passes). Checked only when BOTH sides are byt with a known floor; an
    unchecked rule passes — it cannot veto what it cannot see."""
    if a.category_main == "byt" and b.category_main == "byt" and a.floor is not None and b.floor is not None:
        return True, abs(int(a.floor) - int(b.floor)) <= int(tolerance)
    return False, True


def rung_for(a: ListingAttrs, b: ListingAttrs) -> str:
    """C1 when both sides carry a disposition; C3 otherwise (the fallback on absence)."""
    if disposition_available(a.disposition) and disposition_available(b.disposition):
        return "C1"
    return "C3"


def evaluate_pair(a: ListingAttrs, b: ListingAttrs, inputs: dict[str, Any]) -> PairVerdict | None:
    """Path C, as Python: the verdict for one pair, or None when it is not a candidate.
    Symmetric in (a, b). The SQL of the generation lane must agree with this on every
    case its tests spell out."""
    if inputs.get("path") != "C":
        raise ValueError("evaluate_pair implements path C only")
    if a.listing_id == b.listing_id:
        return None
    if a.block_key is None or b.block_key is None or a.block_key != b.block_key:
        return None
    if not districts_compatible(a, b):
        return None
    if not categories_compatible(a, b):
        return None
    checked, passes = floor_rule(a, b, int(inputs["l0_floor_tolerance"]))
    if not passes:
        return None
    rung = rung_for(a, b)
    if rung == "C1":
        da, db = normalized_disposition(a.disposition), normalized_disposition(b.disposition)
        if da != db:
            return None
        area_a = area_of(a.category_main, a.usable_area, a.estate_area)
        area_b = area_of(b.category_main, b.usable_area, b.estate_area)
        if area_a is None or area_b is None:
            # the area check cannot be made; the pair is kept, as with an unknown floor
            return PairVerdict(rung, da, None, None, None, checked)
        diff = area_diff_pct(area_a, area_b)
        if diff > float(inputs["l0_c1_area_tolerance_pct"]):
            return None
        lo, hi = (area_a, area_b) if a.listing_id < b.listing_id else (area_b, area_a)
        return PairVerdict(rung, da, lo, hi, diff, checked)
    area_a = area_of(a.category_main, a.usable_area, a.estate_area)
    area_b = area_of(b.category_main, b.usable_area, b.estate_area)
    if area_a is None or area_b is None:
        return None
    diff = area_diff_pct(area_a, area_b)
    if diff > area_tolerance_pct(a.category_main, b.category_main, inputs):
        return None
    lo, hi = (area_a, area_b) if a.listing_id < b.listing_id else (area_b, area_a)
    return PairVerdict(rung, None, lo, hi, diff, checked)


# --------------------------------------------------------------------------- lifecycle

RUN_STATUSES: tuple[str, ...] = ("pending", "running", "success", "failed")


@dataclass(frozen=True)
class Generation:
    id: int
    simulation_run_id: int
    inputs_id: int
    path: str
    fingerprint: str
    inputs: dict[str, Any]
    status: str
    progress: dict[str, Any] | None
    stats: dict[str, Any] | None


_INSERT_RUN_SQL = (
    "INSERT INTO dedup_sim.simulation_runs (status, triggered_by, started_at, settings_snapshot) "
    "VALUES ('running', %(triggered_by)s, now(), %(snapshot)s::jsonb) RETURNING id"
)

_UPSERT_INPUTS_SQL = (
    "INSERT INTO dedup_sim.candidate_inputs (path, fingerprint, generator_version, inputs) "
    "VALUES (%(path)s, %(fingerprint)s, %(generator_version)s, %(inputs)s::jsonb) "
    "ON CONFLICT (path, fingerprint) DO UPDATE SET fingerprint = excluded.fingerprint "
    "RETURNING id"
)

_INSERT_GENERATION_SQL = (
    "INSERT INTO dedup_sim.candidate_generations "
    "(simulation_run_id, inputs_id, status, started_at, progress) "
    "VALUES (%(run_id)s, %(inputs_id)s, 'running', now(), %(progress)s::jsonb) RETURNING id"
)

_SELECT_GENERATION_SQL = (
    "SELECT g.id, g.simulation_run_id, g.inputs_id, i.path, i.fingerprint, i.inputs, "
    "g.status, g.progress, g.stats "
    "FROM dedup_sim.candidate_generations g "
    "JOIN dedup_sim.candidate_inputs i ON i.id = g.inputs_id "
    "WHERE g.id = %(id)s"
)

_LATEST_GENERATION_SQL = (
    "SELECT g.id, g.simulation_run_id, g.inputs_id, i.path, i.fingerprint, i.inputs, "
    "g.status, g.progress, g.stats "
    "FROM dedup_sim.candidate_generations g "
    "JOIN dedup_sim.candidate_inputs i ON i.id = g.inputs_id "
    "WHERE i.path = %(path)s AND (%(fingerprint)s::text IS NULL OR i.fingerprint = %(fingerprint)s) "
    "AND (%(status)s::text IS NULL OR g.status = %(status)s) "
    "ORDER BY g.created_at DESC, g.id DESC LIMIT 1"
)

_UPDATE_PROGRESS_SQL = (
    "UPDATE dedup_sim.candidate_generations SET progress = %(progress)s::jsonb WHERE id = %(id)s"
)

_FINISH_GENERATION_SQL = (
    "UPDATE dedup_sim.candidate_generations SET status = %(status)s, completed_at = now(), "
    "stats = COALESCE(%(stats)s::jsonb, stats), progress = COALESCE(%(progress)s::jsonb, progress), "
    "error_message = %(error)s WHERE id = %(id)s"
)

_REOPEN_GENERATION_SQL = (
    "UPDATE dedup_sim.candidate_generations SET status = 'running', completed_at = NULL, "
    "error_message = NULL WHERE id = %(id)s"
)

_REOPEN_RUN_SQL = (
    "UPDATE dedup_sim.simulation_runs SET status = 'running', completed_at = NULL, "
    "error_message = NULL WHERE id = %(id)s"
)

_FINISH_RUN_SQL = (
    "UPDATE dedup_sim.simulation_runs SET status = %(status)s, completed_at = now(), "
    "stats = COALESCE(%(stats)s::jsonb, stats), error_message = %(error)s WHERE id = %(id)s"
)


def _row_to_generation(row: tuple[Any, ...]) -> Generation:
    inputs = row[5] if isinstance(row[5], dict) else json.loads(row[5])
    progress = row[7] if row[7] is None or isinstance(row[7], dict) else json.loads(row[7])
    stats = row[8] if row[8] is None or isinstance(row[8], dict) else json.loads(row[8])
    return Generation(
        id=int(row[0]),
        simulation_run_id=int(row[1]),
        inputs_id=int(row[2]),
        path=str(row[3]),
        fingerprint=str(row[4]),
        inputs=inputs,
        status=str(row[6]),
        progress=progress,
        stats=stats,
    )


def begin_generation(
    conn: "psycopg.Connection",
    code: str,
    *,
    triggered_by: str = "lane",
    progress: dict[str, Any] | None = None,
) -> Generation:
    """Open one generation run of `code` under the CURRENT effective settings: one
    `simulation_runs` row (the full settings snapshot — that is what makes a run
    reproducible after a knob moves), the `candidate_inputs` row for this parameter set
    (created on first sight, reused after), and the `candidate_generations` row. One
    transaction; the caller owns the run from here and must finish it."""
    settings = dss.effective_settings(conn)
    inputs = path_inputs(code, settings)
    fp = fingerprint(inputs)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            _INSERT_RUN_SQL,
            {"triggered_by": triggered_by, "snapshot": json.dumps(settings, sort_keys=True)},
        )
        run_id = int(cur.fetchone()[0])
        cur.execute(
            _UPSERT_INPUTS_SQL,
            {
                "path": code,
                "fingerprint": fp,
                "generator_version": inputs["generator_version"],
                "inputs": json.dumps(inputs, sort_keys=True),
            },
        )
        inputs_id = int(cur.fetchone()[0])
        cur.execute(
            _INSERT_GENERATION_SQL,
            {
                "run_id": run_id,
                "inputs_id": inputs_id,
                "progress": json.dumps(progress) if progress is not None else None,
            },
        )
        gen_id = int(cur.fetchone()[0])
    return Generation(
        id=gen_id,
        simulation_run_id=run_id,
        inputs_id=inputs_id,
        path=code,
        fingerprint=fp,
        inputs=inputs,
        status="running",
        progress=progress,
        stats=None,
    )


def get_generation(conn: "psycopg.Connection", generation_id: int) -> Generation | None:
    with conn.cursor() as cur:
        cur.execute(_SELECT_GENERATION_SQL, {"id": generation_id})
        row = cur.fetchone()
    return _row_to_generation(row) if row else None


def latest_generation(
    conn: "psycopg.Connection",
    code: str,
    *,
    fingerprint_: str | None = None,
    status: str | None = None,
) -> Generation | None:
    """The newest generation of `code`, optionally narrowed to one parameter set and/or
    one status — `status='success'` is what the audit page reads; `status='running'` with
    the current fingerprint is what a resumed lane picks up."""
    with conn.cursor() as cur:
        cur.execute(
            _LATEST_GENERATION_SQL,
            {"path": code, "fingerprint": fingerprint_, "status": status},
        )
        row = cur.fetchone()
    return _row_to_generation(row) if row else None


def record_progress(conn: "psycopg.Connection", generation_id: int, progress: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(_UPDATE_PROGRESS_SQL, {"id": generation_id, "progress": json.dumps(progress)})


def reopen_generation(conn: "psycopg.Connection", gen: Generation) -> None:
    """A FAILED generation resumes from its cursor: both rows go back to `running`, the
    error is cleared, and `finish_generation` will write the final verdict later."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_REOPEN_GENERATION_SQL, {"id": gen.id})
        cur.execute(_REOPEN_RUN_SQL, {"id": gen.simulation_run_id})


def finish_generation(
    conn: "psycopg.Connection",
    gen: Generation,
    *,
    status: str,
    stats: dict[str, Any] | None = None,
    progress: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Close the generation AND its simulation run with one terminal status. A failed run
    still keeps its row (the design's estimation_runs convention) so the failure is on
    record next to what it had done so far."""
    if status not in ("success", "failed"):
        raise ValueError(f"terminal status expected, got {status!r}")
    params_common = {
        "status": status,
        "stats": json.dumps(stats) if stats is not None else None,
        "error": error,
    }
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            _FINISH_GENERATION_SQL,
            {
                **params_common,
                "progress": json.dumps(progress) if progress is not None else None,
                "id": gen.id,
            },
        )
        cur.execute(_FINISH_RUN_SQL, {**params_common, "id": gen.simulation_run_id})


# --------------------------------------------------------------------------- audit reads

# What the Candidate audit page (api/routes/new_dedup_candidates.py) reads. It never scans
# the pair table: the numbers were computed once, at the end of a generation, onto
# `candidate_generations.stats` (scripts/dedup_candidates_generate.py:generation_stats).

_STORE_READY_SQL = "SELECT to_regclass('dedup_sim.candidate_pairs') IS NOT NULL"

# Column order IS the contract of `list_recent_generations` — the rows are zipped onto
# `_RECENT_COLUMNS`, because the fake connections of the tests carry no cursor.description.
_RECENT_COLUMNS: tuple[str, ...] = (
    "id",
    "status",
    "created_at",
    "completed_at",
    "fingerprint",
    "scope",
    "partial",
    "pairs_total",
)

_RECENT_GENERATIONS_SQL = (
    "SELECT g.id, g.status, g.created_at, g.completed_at, i.fingerprint, "
    "g.stats->>'scope' AS scope, (g.stats->>'partial')::boolean AS partial, "
    "(g.stats->'pairs'->>'total')::bigint AS pairs_total "
    "FROM dedup_sim.candidate_generations g "
    "JOIN dedup_sim.candidate_inputs i ON i.id = g.inputs_id "
    "WHERE i.path = %(path)s "
    "ORDER BY g.created_at DESC, g.id DESC LIMIT %(limit)s"
)

_GENERATION_TIMES_COLUMNS: tuple[str, ...] = (
    "created_at",
    "started_at",
    "completed_at",
    "error_message",
)

_GENERATION_TIMES_SQL = (
    "SELECT created_at, started_at, completed_at, error_message "
    "FROM dedup_sim.candidate_generations WHERE id = %(id)s"
)


def store_ready(conn: "psycopg.Connection") -> bool:
    """Does the candidate store of migration 492 exist? A catalog probe (`to_regclass`
    answers NULL for a missing relation instead of raising), asked BEFORE any other query
    so a database without the migration renders "store not created yet" rather than a 500."""
    with conn.cursor() as cur:
        cur.execute(_STORE_READY_SQL)
        row = cur.fetchone()
    return bool(row and row[0])


def list_recent_generations(
    conn: "psycopg.Connection", code: str, limit: int = 20
) -> list[dict[str, Any]]:
    """The newest generations of one path, every status, for the audit page's run picker:
    id, status, the two timestamps, the parameter set's fingerprint, and the three facts
    read straight off `stats` (scope, partial, total pairs — NULL while a run has no stats
    yet). Newest first, `id` breaking a `created_at` tie so the order never reshuffles."""
    with conn.cursor() as cur:
        cur.execute(_RECENT_GENERATIONS_SQL, {"path": code, "limit": limit})
        rows = cur.fetchall()
    return [dict(zip(_RECENT_COLUMNS, row)) for row in rows]


def generation_timestamps(conn: "psycopg.Connection", generation_id: int) -> dict[str, Any] | None:
    """The four generation-row columns `get_generation` does not carry (its `Generation` is
    the lane's working record, not the audit's): when the run was created, started and
    completed, and the message of a failed one."""
    with conn.cursor() as cur:
        cur.execute(_GENERATION_TIMES_SQL, {"id": generation_id})
        row = cur.fetchone()
    return dict(zip(_GENERATION_TIMES_COLUMNS, row)) if row else None
