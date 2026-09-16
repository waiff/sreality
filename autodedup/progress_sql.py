"""SQL behind the AUTODEDUP progress page (docs/design/autodedup/PROGRAM.md §12).

Three statements over `autodedup.iterations` (migration 528) and nothing else: the catalog
probe that lets a pre-migration database render an empty page instead of a 500, one keyset
page of the ledger newest-first, and the per-wave rollup the header strip sums.

The SQL lives here rather than in `api/routes/autodedup.py` for the repo's split (a route
shapes an answer, a module owns the statement) and because `tests/sql_corpus.py`
`RUNTIME_DIRS` carries `autodedup`, so every constant below joins the PREPARE gate for free.

`ITERATION_COLUMNS` is the contract of the select list: rows are zipped onto it rather than
read off `cursor.description`, which the tests' fake connections do not carry.
"""

from __future__ import annotations

ITERATION_COLUMNS: tuple[str, ...] = (
    "id",
    "wave",
    "title",
    "status",
    "approach",
    "tools",
    "sample_stats",
    "metrics",
    "cost_usd",
    "artifacts",
    "run_id",
    "notes",
    "started_at",
    "finished_at",
    "created_at",
)

# The route owns its own probe rather than importing the lane's (`autodedup.iterations.store_ready`):
# a read path must not boot through the ledger WRITER's module, and the two answer for
# different layers — the lane's swallows every error so a pass never dies on a probe.
AUTODEDUP_STORE_READY_SQL = "SELECT to_regclass('autodedup.iterations') IS NOT NULL"

# Keyset, not OFFSET: `after_id` is the previous page's last id and the order is `id desc`
# (bigserial, so it is the write order and can never reshuffle under a concurrent insert the
# way `created_at desc` could tie). The explicit `::bigint` is load-bearing — psycopg sends
# no type OID for a None, so an uncast NULL fails Parse with 42P18.
AUTODEDUP_ITERATIONS_SQL = (
    "SELECT id, wave, title, status, approach, tools, sample_stats, metrics, "
    "cost_usd, artifacts, run_id, notes, started_at, finished_at, created_at "
    "FROM autodedup.iterations "
    "WHERE %(after_id)s::bigint IS NULL OR id < %(after_id)s::bigint "
    "ORDER BY id DESC "
    "LIMIT %(limit)s::int"
)

WAVE_COLUMNS: tuple[str, ...] = ("wave", "n", "cost_usd", "last_at", "last_status")

# Per-wave rollup; the page's three headline numbers are summed from these rows rather than
# asked for in a second statement, so the header strip and the wave table can never disagree.
# `last_status` is the status of the wave's NEWEST iteration, not an aggregate over all of
# them — a wave whose latest pass died reads `failed` even if ten passes before it were
# `done`. "Newest" is the SAME row `last_at` reports (the ordering key picks `last_id`, id
# breaking a tie), so a backfilled row can never make the wave sort by one clock and show
# another row's status.
AUTODEDUP_STATS_SQL = """
WITH per_wave AS (
    SELECT
        wave                                               AS wave,
        count(*)                                           AS n,
        coalesce(sum(cost_usd), 0)                         AS cost_usd,
        max(coalesce(finished_at, started_at, created_at)) AS last_at,
        (array_agg(id ORDER BY coalesce(finished_at, started_at, created_at) DESC, id DESC))[1]
                                                           AS last_id
    FROM autodedup.iterations
    GROUP BY wave
)
SELECT w.wave, w.n, w.cost_usd, w.last_at, i.status AS last_status
FROM per_wave w
JOIN autodedup.iterations i ON i.id = w.last_id
ORDER BY w.last_at DESC, w.wave DESC
"""
