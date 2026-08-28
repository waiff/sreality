"""Candidate retrieval (migration 450) — the selection POLICY as pure functions,
plus a local fake conn for the orchestration around it.

WHAT THIS FILE CAN AND CANNOT PROVE. `select_candidates`, `allocate_counts` and
`property_key` are pure — no DB, no clock, no randomness — so the band mix, the
near-duplicate collapse and the per-property cap are really exercised here. The
fake conn is not a database: it cannot compute a cosine, cannot enforce the
(tag_id, image_id) PK or the `draw` CHECK, cannot roll a transaction back, and
cannot run migration 446's trigger. So nothing below asserts that retrieval RANKS
anything — the ranking lives in `_DRAW_POOL_SQL`, whose only executing gate is
tests/test_sql_schema_prepare.py (PREPARE against the replayed schema, pgvector
included), and whose retrieval QUALITY is an operator observation, not an
assertion. Deliberately NOT faked: a cosine ranker. A fake that ranked vectors
would be a second implementation of the retriever and would drift from it.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from toolkit import tag_candidates as tc

MODEL = tc.embedding_model()


# --- a local fake conn ------------------------------------------------------


def _pool_row(
    image_id: int, *, draw: str = "centroid_head", rank: int = 1, pool_size: int = 100,
    listing_id: int | None = None, property_id: int | None = None,
    phash: int | None = None, distance: float = 0.1, positives: int = 31,
) -> tuple[Any, ...]:
    """One row in _DRAW_POOL_SQL's own SELECT order."""
    return (
        image_id, listing_id if listing_id is not None else image_id, property_id,
        phash, distance, rank, pool_size, draw, positives,
    )


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        c = self._conn

        if s.startswith("SET LOCAL statement_timeout"):
            self._rows = []

        elif s.startswith("SELECT 1 FROM tag_taxonomy WHERE id"):
            self._rows = [(1,)] if params["tag_id"] in c.tags else []

        elif s.startswith("SELECT routing_categories FROM tag_taxonomy"):
            self._rows = [(c.routing_categories,)]

        elif s.startswith("SELECT count(*)::int FROM image_tag_labels itl"):
            self._rows = [(c.verified_positives,)]

        elif s.startswith("SELECT count(*)::bigint FROM listings WHERE category_main"):
            self._rows = [(c.category_counts.get(params["category_main"], 0),)]

        elif s.startswith("SELECT q.image_id, q.phash, q.listing_id, q.property_id"):
            self._rows = c.existing[: params["limit"]]

        elif s.startswith("WITH centroid AS"):
            category = params["category_main"]
            if category in c.cancel_categories:
                raise psycopg.errors.QueryCanceled("statement timeout")
            self._rows = list(c.pool_by_category.get(category, []))

        elif s.startswith("INSERT INTO tag_candidates"):
            c.inserted.append(params)

        elif s.startswith("SELECT c.draw,"):
            self._rows = list(c.summary_by_draw)

        elif s.startswith("SELECT c.category_main,"):
            self._rows = list(c.summary_by_category)

        else:
            raise AssertionError(f"unhandled SQL in fake conn: {s}")

    def executemany(self, sql: str, params_seq: Any) -> None:
        for params in params_seq:
            self.execute(sql, params)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Txn:
    def __enter__(self) -> "_Txn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    """Canned answers keyed on the exact SQL toolkit/tag_candidates.py issues.
    NOT a database — see this module's docstring for what that costs."""

    def __init__(self) -> None:
        self.tags: set[int] = {1, 2}
        self.verified_positives = 40
        self.routing_categories: list[str] | None = None
        # Real 2026-08-28 shares, so the sample percentages the tests see are the
        # ones production computes.
        self.category_counts: dict[str, int] = {
            "byt": 320_909, "dum": 172_310, "pozemek": 124_048,
            "komercni": 96_903, "ostatni": 14_171,
        }
        self.existing: list[tuple[Any, ...]] = []
        self.pool_by_category: dict[str, list[tuple[Any, ...]]] = {}
        self.cancel_categories: set[str] = set()
        self.summary_by_draw: list[tuple[Any, ...]] = []
        self.summary_by_category: list[tuple[Any, ...]] = []
        self.inserted: list[dict[str, Any]] = []
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Txn:
        return _Txn()

    def pool_params(self, category_main: str) -> dict[str, Any]:
        return next(
            p for s, p in self.executed
            if s.startswith("WITH centroid AS") and p["category_main"] == category_main
        )


@pytest.fixture()
def conn() -> _FakeConn:
    return _FakeConn()


# --- allocate_counts --------------------------------------------------------


@pytest.mark.parametrize("total", [1, 7, 13, 120, 400])
def test_allocate_counts_sums_to_the_total_exactly(total: int) -> None:
    # A split that loses a candidate to rounding would silently shrink every draw.
    assert sum(tc.allocate_counts(total, tc.CATEGORY_MIX).values()) == total
    assert sum(tc.allocate_counts(total, tc.BAND_MIX).values()) == total


def test_allocate_counts_is_deterministic_and_largest_remainder_first() -> None:
    first = tc.allocate_counts(7, tc.CATEGORY_MIX)
    assert first == tc.allocate_counts(7, tc.CATEGORY_MIX)
    # 7 x the mix = byt 2.1, dum 1.75, pozemek 1.4, komercni 1.05, ostatni 0.7;
    # floors give 5, and the two biggest fractions (dum .75, ostatni .7) take the
    # remaining 2.
    assert first == {"byt": 2, "dum": 2, "pozemek": 1, "komercni": 1, "ostatni": 1}


def test_allocate_counts_of_nothing_is_all_zeros() -> None:
    assert tc.allocate_counts(0, tc.BAND_MIX) == {b: 0 for b in tc.DRAWS}


def test_the_band_mix_covers_exactly_the_draw_vocabulary() -> None:
    # The DB CHECK mirrors DRAWS (migration 450); a band with no quota could never
    # be drawn, and a quota with no CHECK value would fail at INSERT time.
    assert set(tc.BAND_MIX) == set(tc.DRAWS)
    assert sum(tc.BAND_MIX.values()) == pytest.approx(1.0)
    assert sum(tc.CATEGORY_MIX.values()) == pytest.approx(1.0)


def test_the_category_mix_caps_byt_below_its_corpus_share() -> None:
    # The labeled set is 83.8 percent byt against a 43.9 percent corpus. A draw at
    # or above the corpus share would stop adding to the skew but never dilute it.
    assert tc.CATEGORY_MIX["byt"] < 0.439


# --- property_key -----------------------------------------------------------


def test_property_key_falls_back_to_the_listing_when_the_property_is_null() -> None:
    # New rows land property_id NULL (rule 19); a bare coalesce onto listing_id
    # could collide with a real property id, so the fallback is namespaced.
    assert tc.property_key(7, None) == "listing:7"
    assert tc.property_key(7, 7) == "7"
    assert tc.property_key(7, None) != tc.property_key(7, 7)


# --- select_candidates ------------------------------------------------------


def _row(
    image_id: int, *, draw: str = "centroid_head", rank: int = 1,
    listing_id: int | None = None, property_id: int | None = None,
    phash: int | None = None,
) -> tc.PoolRow:
    return tc.PoolRow(
        image_id=image_id,
        listing_id=listing_id if listing_id is not None else image_id,
        property_id=property_id, phash=phash, distance=0.1, pool_rank=rank,
        pool_size=500, draw=draw,
    )


def test_select_candidates_stops_each_band_at_its_quota() -> None:
    rows = [_row(i, draw="centroid_head", rank=i) for i in range(1, 6)]
    rows += [_row(i, draw="random", rank=i) for i in range(10, 15)]
    accepted, dropped = tc.select_candidates(
        rows, quotas={"centroid_head": 2, "centroid_mid": 0, "random": 1},
        existing_phashes=[], existing_property_counts={},
    )
    assert [r.image_id for r in accepted] == [1, 2, 10]
    # A met quota is the normal stop condition, not a loss.
    assert dropped == {"near_dup": 0, "property_cap": 0}


def test_select_candidates_keeps_the_first_band_a_row_appears_in() -> None:
    # mid and rnd sample overlapping rank ranges, so the same image can arrive
    # twice; it must not consume two quotas or land twice.
    rows = [
        _row(5, draw="centroid_mid", rank=40),
        _row(5, draw="random", rank=40),
        _row(6, draw="random", rank=900),
    ]
    accepted, _ = tc.select_candidates(
        rows, quotas={"centroid_head": 0, "centroid_mid": 1, "random": 1},
        existing_phashes=[], existing_property_counts={},
    )
    assert [(r.image_id, r.draw) for r in accepted] == [
        (5, "centroid_mid"), (6, "random"),
    ]


def test_select_candidates_drops_a_hamming_5_near_dup_and_keeps_a_hamming_6() -> None:
    # NEAR_DUP_MIN_HAMMING = 6: strictly below is a duplicate, exactly 6 is kept.
    # Deliberately stricter than the dedup engine's 11 — dHash collapses distinct
    # floor plans, and a false collapse hides an image from review permanently
    # while a false keep costs one click.
    rows = [
        _row(1, phash=0b0),
        _row(2, phash=0b11111),    # hamming 5 from image 1
        _row(3, phash=0b1111110),  # hamming 6 from image 1
    ]
    accepted, dropped = tc.select_candidates(
        rows, quotas={"centroid_head": 9, "centroid_mid": 0, "random": 0},
        existing_phashes=[], existing_property_counts={},
    )
    assert [r.image_id for r in accepted] == [1, 3]
    assert dropped["near_dup"] == 1


def test_select_candidates_compares_against_the_pool_already_stored() -> None:
    # The check spans draws, not just this one: the stored phashes come from
    # tag_candidates, so a re-draw cannot re-offer last week's duplicate.
    accepted, dropped = tc.select_candidates(
        [_row(1, phash=0b11)],
        quotas={"centroid_head": 9, "centroid_mid": 0, "random": 0},
        existing_phashes=[0b0], existing_property_counts={},
    )
    assert accepted == []
    assert dropped["near_dup"] == 1


def test_select_candidates_lets_an_unhashed_image_through_the_near_dup_check() -> None:
    # ~1 percent of images have no phash. They skip the check entirely and are
    # bounded only by the per-property cap — stated, not silently assumed.
    rows = [_row(1, phash=None, listing_id=1), _row(2, phash=None, listing_id=2)]
    accepted, dropped = tc.select_candidates(
        rows, quotas={"centroid_head": 9, "centroid_mid": 0, "random": 0},
        existing_phashes=[0b0], existing_property_counts={},
    )
    assert [r.image_id for r in accepted] == [1, 2]
    assert dropped["near_dup"] == 0


def test_select_candidates_caps_rows_per_property() -> None:
    # Several shots of the same room are not several examples: without the cap a
    # head looks like it has 200 examples when it has 40.
    rows = [_row(i, listing_id=i, property_id=99) for i in range(1, 6)]
    accepted, dropped = tc.select_candidates(
        rows, quotas={"centroid_head": 9, "centroid_mid": 0, "random": 0},
        existing_phashes=[], existing_property_counts={},
    )
    assert len(accepted) == tc.PER_PROPERTY_CAP == 2
    assert dropped["property_cap"] == 3


def test_select_candidates_counts_rows_already_in_the_store_against_the_cap() -> None:
    accepted, dropped = tc.select_candidates(
        [_row(1, listing_id=1, property_id=99)],
        quotas={"centroid_head": 9, "centroid_mid": 0, "random": 0},
        existing_phashes=[], existing_property_counts={"99": tc.PER_PROPERTY_CAP},
    )
    assert accepted == []
    assert dropped["property_cap"] == 1


def test_select_candidates_caps_an_unattached_listing_on_its_own_key() -> None:
    # property_id NULL is not "one shared property": two unattached listings are
    # two keys, and the cap applies within each.
    rows = [_row(i, listing_id=li) for i, li in ((1, 10), (2, 10), (3, 10), (4, 11))]
    accepted, _ = tc.select_candidates(
        rows, quotas={"centroid_head": 9, "centroid_mid": 0, "random": 0},
        existing_phashes=[], existing_property_counts={},
    )
    assert [r.image_id for r in accepted] == [1, 2, 4]


def test_select_candidates_does_not_mutate_the_callers_state() -> None:
    counts: dict[str, int] = {}
    phashes: list[int] = []
    tc.select_candidates(
        [_row(1, phash=0b1010, property_id=5)],
        quotas={"centroid_head": 1, "centroid_mid": 0, "random": 0},
        existing_phashes=phashes, existing_property_counts=counts,
    )
    assert counts == {} and phashes == []


# --- the centroid's population ----------------------------------------------


def test_the_centroid_is_built_only_from_human_verified_positives() -> None:
    """Migration 442 manufactured 72,000 negatives and `machine` rows are nobody's
    decision; both are excluded BY PREDICATE, never by deletion. Asserted on the
    SQL text because a fake conn cannot average a vector."""
    sql = " ".join(tc._DRAW_POOL_SQL.split())
    centroid = sql.split("pool_listings AS", 1)[0]
    assert "avg(e.embedding)" in centroid
    assert "itl.state = 'positive'" in centroid
    assert "itl.source IN ('human', 'human_confirmed')" in centroid
    assert "backfill_442" not in sql
    assert "'machine'" not in sql


def test_the_floor_check_counts_exactly_the_centroids_population() -> None:
    # If the floor check and the centroid disagreed about the population, a tag
    # could pass the gate and still produce an empty pool — the floor would be a
    # lie. The two predicates are kept byte-identical.
    count_sql = " ".join(tc._COUNT_VERIFIED_POSITIVES_SQL.split())
    centroid = " ".join(tc._DRAW_POOL_SQL.split()).split("pool_listings AS", 1)[0]
    predicate = (
        "FROM image_tag_labels itl JOIN image_clip_embeddings e "
        "ON e.image_id = itl.image_id AND e.model = %(model)s::text "
        "WHERE itl.tag_id = %(tag_id)s AND itl.state = 'positive' "
        "AND itl.source IN ('human', 'human_confirmed')"
    )
    assert predicate in count_sql
    assert predicate in centroid


def test_the_bands_are_ordered_by_their_own_ordinal_not_by_similarity() -> None:
    """select_candidates walks this order greedily and stops each band at its
    quota. Ordering the whole union by pool_rank would therefore hand the mid and
    random bands the NEAREST THIRD of their overfetch and nothing else: the tail
    becomes structurally unreachable, the random band's base rate is biased high,
    and the mode-discovery claim is false — a mode the centroid misses sits at
    high rank, which is exactly the region that would be cut."""
    sql = " ".join(tc._DRAW_POOL_SQL.split())
    assert sql.rstrip().endswith("u.band_ord")
    assert "'centroid_head' THEN 0 WHEN 'centroid_mid' THEN 1 ELSE 2 END, u.band_ord" in sql
    # head keeps nearest-first; the two sampled bands carry a shuffle.
    assert "'centroid_head'::text AS draw, r.pool_rank AS band_ord" in sql
    assert sql.count("(row_number() OVER (ORDER BY random()))::int AS band_ord") == 2


def test_the_random_band_samples_the_whole_pool_including_the_head() -> None:
    """A sample of the pool MINUS its highest-yield region is not a base rate,
    and the base rate is the band's whole documented job. The overlap is resolved
    in select_candidates (head walked first, repeat image_id skipped), not by a
    rank floor in SQL."""
    sql = " ".join(tc._DRAW_POOL_SQL.split())
    rnd = sql.split("rnd AS (", 1)[1]
    assert "'random'::text AS draw FROM ranked r ORDER BY random()" in rnd
    assert "pool_rank" not in rnd.split("LIMIT %(random_fetch)s", 1)[0]


def test_the_mid_bands_window_never_closes_below_its_own_fetch() -> None:
    """The lower bound is the head's OVERFETCHED window and the upper bound is a
    percentile of the pool, so on a thin pool the predicate can become
    `rank > H and rank <= M` with H > M — a band empty by arithmetic, taking with
    it every hard negative near the three confusion clusters."""
    sql = " ".join(tc._DRAW_POOL_SQL.split())
    assert (
        "r.pool_rank <= greatest( ceil(r.pool_size * %(mid_percentile)s::double precision), "
        "%(mid_floor)s::double precision)"
    ) in sql
    params = tc._draw_pool_params(
        tag_id=1, model="m", category_main="byt",
        band_quotas=tc.allocate_counts(120, tc.BAND_MIX), scoped=True,
        category_count=320_909,
    )
    assert params["mid_floor"] == params["head_fetch"] + params["mid_fetch"]


def test_the_pool_excludes_images_already_decided_or_already_queued() -> None:
    # Decided images are done, and a row already in the queue is already waiting;
    # re-offering either wastes a review slot. Scoped to THIS tag both times.
    sql = " ".join(tc._DRAW_POOL_SQL.split())
    assert (
        "NOT EXISTS ( SELECT 1 FROM image_tag_labels a "
        "WHERE a.image_id = p.image_id AND a.tag_id = %(tag_id)s )"
    ) in sql
    assert (
        "NOT EXISTS ( SELECT 1 FROM tag_candidates tc "
        "WHERE tc.tag_id = %(tag_id)s AND tc.image_id = p.image_id )"
    ) in sql


def test_the_exact_hash_collapse_keeps_unhashed_rows() -> None:
    # A naive DISTINCT ON (phash) would collapse EVERY un-hashed image into one
    # row, because SQL treats all NULLs as one group there.
    sql = " ".join(tc._DRAW_POOL_SQL.split())
    assert "PARTITION BY s.phash ORDER BY s.distance, s.image_id" in sql
    assert "WHERE x.phash IS NULL OR x.phash_rn = 1" in sql


def test_the_pool_is_sampled_by_listing_lottery_not_by_id_range() -> None:
    # Consecutive image_id samples LISTINGS, not images (30,000 ids came from
    # 2,106 listings), and ordering a listing's images by `sequence` would put the
    # hero shots first and make `pudorys` structurally unreachable.
    #
    # The listing lottery is now TABLESAMPLE rather than ORDER BY random() — the
    # sort cost 35s on byt and timed out 2 of 3 categories in the first live
    # background draw. What must NOT come back is id-range sampling; the guarantee
    # this test defends is that neither stage walks ids in order.
    sql = " ".join(tc._DRAW_POOL_SQL.split())
    assert "FROM listings l TABLESAMPLE SYSTEM (%(sample_pct)s)" in sql
    assert "ORDER BY random() LIMIT %(images_per_listing)s" in sql
    assert "sequence" not in sql
    assert "l.id >" not in sql and "l.id BETWEEN" not in sql


def test_the_sample_percentage_scales_inversely_with_the_categorys_size() -> None:
    # TABLESAMPLE reads a fraction of the TABLE and the category filter runs after,
    # so a fixed percentage starves the small categories. Measured at 3%: byt and
    # dum filled 5,000, but ostatni returned 401.
    big = tc.sample_pct(pool_listings=5000, category_count=320_909)
    small = tc.sample_pct(pool_listings=5000, category_count=14_171)
    assert big < small
    assert big < 5.0    # byt: a few percent of the table is plenty
    assert small > 50.0  # ostatni: most of the table, because 1.9% of it is ostatni


def test_the_sample_percentage_is_bounded_at_both_ends() -> None:
    assert tc.sample_pct(pool_listings=1, category_count=10**9) == tc.SAMPLE_PCT_MIN
    assert tc.sample_pct(pool_listings=10**6, category_count=10) == tc.SAMPLE_PCT_MAX


def test_an_uncountable_category_falls_back_to_a_full_scan() -> None:
    # A draw returning zero because of an arithmetic edge would look exactly like
    # a thin corpus, which is the one thing the report must never lie about.
    assert tc.sample_pct(pool_listings=5000, category_count=0) == tc.SAMPLE_PCT_MAX


def test_the_category_count_is_read_outside_the_timed_transaction(
    conn: _FakeConn,
) -> None:
    # It SIZES the sample, so charging it to the pool query's own ceiling would let
    # a slow count eat the budget it exists to protect.
    conn.pool_by_category = {"byt": []}
    tc.draw_candidates(conn, tag_id=1, count=20, category_main="byt")
    kinds = [s for s, _ in conn.executed
             if s.startswith("SELECT count(*)::bigint FROM listings")
             or s.startswith("SET LOCAL statement_timeout")]
    assert kinds[0].startswith("SELECT count(*)::bigint FROM listings")


def test_the_sample_percentage_reaches_the_pool_query(conn: _FakeConn) -> None:
    conn.pool_by_category = {"byt": []}
    tc.draw_candidates(conn, tag_id=1, count=20, category_main="byt")
    params = conn.pool_params("byt")
    assert params["sample_pct"] == tc.sample_pct(
        pool_listings=params["pool_listings"], category_count=320_909,
    )


def test_the_insert_resolves_the_definition_from_the_rows_own_tag() -> None:
    # Migration 446's rule: a caller that could name a definition could cite
    # another tag's. Never a parameter.
    sql = " ".join(tc._INSERT_CANDIDATE_SQL.split())
    assert "(SELECT id FROM tag_definitions WHERE tag_id = %(tag_id)s AND status = 'active')" in sql
    assert "ON CONFLICT (tag_id, image_id) DO NOTHING" in sql


# --- draw_candidates: guards ------------------------------------------------


def test_draw_candidates_raises_for_an_unknown_tag(conn: _FakeConn) -> None:
    with pytest.raises(KeyError):
        tc.draw_candidates(conn, tag_id=404)


@pytest.mark.parametrize("count", [0, -5, tc.DRAW_COUNT_MAX + 1])
def test_draw_candidates_rejects_an_out_of_range_count(conn: _FakeConn, count: int) -> None:
    with pytest.raises(ValueError):
        tc.draw_candidates(conn, tag_id=1, count=count)


def test_draw_candidates_rejects_an_unknown_category(conn: _FakeConn) -> None:
    with pytest.raises(ValueError, match="unknown category_main"):
        tc.draw_candidates(conn, tag_id=1, category_main="chata")


def test_draw_candidates_below_the_floor_writes_nothing_and_says_why(
    conn: _FakeConn,
) -> None:
    """The honest-degradation rail. A centroid over fewer positives than were ever
    measured is one operator's idiosyncrasies, and a garbage pool costs a whole
    review sitting — so no pool is drawn at all and the caller is told the two
    numbers."""
    conn.verified_positives = tc.MIN_VERIFIED_POSITIVES - 1
    res = tc.draw_candidates(conn, tag_id=1)
    assert res["status"] == "insufficient_positives"
    assert res["inserted"] == 0
    assert res["verified_positive_count"] == tc.MIN_VERIFIED_POSITIVES - 1
    assert res["min_verified_positives"] == tc.MIN_VERIFIED_POSITIVES
    assert res["by_draw"] == {b: 0 for b in tc.DRAWS}
    assert res["by_category"] == {} and res["categories"] == []
    assert conn.inserted == []
    assert not any(s.startswith("WITH centroid AS") for s, _ in conn.executed)


def test_the_floor_is_the_measured_one(conn: _FakeConn) -> None:
    # Retrieval quality was measured only at >= 15 positives (median AUC 0.942
    # over 28 tags, min 0.859). Below it, quality is unmeasured.
    assert tc.MIN_VERIFIED_POSITIVES == 15


# --- draw_candidates: the pool query ----------------------------------------


def test_draw_candidates_draws_every_category_of_the_mix(conn: _FakeConn) -> None:
    tc.draw_candidates(conn, tag_id=1, count=120)
    drawn = [
        p["category_main"] for s, p in conn.executed if s.startswith("WITH centroid AS")
    ]
    assert set(drawn) == set(tc.CATEGORY_MIX)


def test_draw_candidates_runs_the_smallest_quota_first(conn: _FakeConn) -> None:
    """Order IS the degradation policy: whatever the budget cuts is cut from the
    END. Running in CATEGORY_MIX order would always sacrifice komercni and
    ostatni — the two thinnest, the two the mix gives a floor to — while
    guaranteeing byt, the one capped below its corpus share to dilute the labeled
    set's 83.8% byt skew. category_main is stored per row, so that drift would be
    durable."""
    tc.draw_candidates(conn, tag_id=1, count=120)
    drawn = [
        p["category_main"] for s, p in conn.executed if s.startswith("WITH centroid AS")
    ]
    quotas = tc.allocate_counts(120, tc.CATEGORY_MIX)
    assert drawn == sorted(quotas, key=lambda k: (quotas[k], k))
    assert drawn[0] == "ostatni" and drawn[-1] == "byt"


def test_draw_candidates_binds_the_band_quotas_with_overfetch(conn: _FakeConn) -> None:
    # Over-fetch is what lets the greedy pass drop near-dups and over-cap rows and
    # still fill the quota.
    tc.draw_candidates(conn, tag_id=1, count=120)
    params = conn.pool_params("byt")
    bands = tc.allocate_counts(tc.allocate_counts(120, tc.CATEGORY_MIX)["byt"], tc.BAND_MIX)
    assert params["head_fetch"] == bands["centroid_head"] * tc.OVERFETCH
    assert params["mid_fetch"] == bands["centroid_mid"] * tc.OVERFETCH
    assert params["random_fetch"] == bands["random"] * tc.OVERFETCH
    assert params["mid_percentile"] == tc.MID_BAND_PERCENTILE


def test_draw_candidates_sizes_the_listing_lottery_from_the_pool_target(
    conn: _FakeConn,
) -> None:
    tc.draw_candidates(conn, tag_id=1, count=120)
    params = conn.pool_params("byt")
    assert params["images_per_listing"] == tc.POOL_IMAGES_PER_LISTING
    # 20,000 x 0.30 images / 4 per listing => 1,500 listings, not ~430.
    assert params["pool_listings"] == round(
        tc.POOL_IMAGES_TARGET * tc.CATEGORY_MIX["byt"]
    ) // tc.POOL_IMAGES_PER_LISTING


def test_a_category_pinned_draw_takes_the_whole_pool_budget(conn: _FakeConn) -> None:
    """A pinned draw allocates the WHOLE count to one category, so sizing its pool
    by that category's share of the mix would shrink the pool while the head's
    fetch window grew — and the mid band, whose upper bound is a percentile OF the
    pool, would be squeezed out from below. At count=120 pinned to komercni the
    old sizing gave a 3,000-image ceiling (5% = 150) under a head window of 180:
    a band empty by arithmetic, and with it every hard negative near the three
    confusion clusters."""
    tc.draw_candidates(conn, tag_id=1, count=120, category_main="komercni")
    pinned = conn.pool_params("komercni")
    assert pinned["pool_listings"] == tc.POOL_IMAGES_TARGET // tc.POOL_IMAGES_PER_LISTING
    pool_ceiling = pinned["pool_listings"] * tc.POOL_IMAGES_PER_LISTING
    assert pool_ceiling * tc.MID_BAND_PERCENTILE > pinned["head_fetch"]


def test_draw_candidates_defaults_the_model_to_the_checkpoint_in_the_taxonomy_file(
    conn: _FakeConn,
) -> None:
    tc.draw_candidates(conn, tag_id=1, count=10)
    assert conn.pool_params("byt")["model"] == MODEL
    assert conn.pool_params("byt")["min_positives"] == tc.MIN_VERIFIED_POSITIVES


def test_draw_candidates_lets_an_explicit_model_override_the_default(
    conn: _FakeConn,
) -> None:
    tc.draw_candidates(conn, tag_id=1, count=10, model="some/other-checkpoint")
    assert conn.pool_params("byt")["model"] == "some/other-checkpoint"


def test_draw_candidates_scoped_to_one_category_draws_only_that_pool(
    conn: _FakeConn,
) -> None:
    tc.draw_candidates(conn, tag_id=1, count=40, category_main="pozemek")
    drawn = [
        p["category_main"] for s, p in conn.executed if s.startswith("WITH centroid AS")
    ]
    assert drawn == ["pozemek"]
    assert conn.pool_params("pozemek")["head_fetch"] == (
        tc.allocate_counts(40, tc.BAND_MIX)["centroid_head"] * tc.OVERFETCH
    )


def test_draw_candidates_bounds_the_existing_pool_read(conn: _FakeConn) -> None:
    # An unbounded scan of one tag's history costs a request; a missed near-dup on
    # the oldest rows costs one click.
    tc.draw_candidates(conn, tag_id=1, count=10)
    params = next(
        p for s, p in conn.executed
        if s.startswith("SELECT q.image_id, q.phash, q.listing_id, q.property_id")
    )
    assert params["limit"] == tc.PHASH_HISTORY_MAX


def test_the_near_dup_history_also_reads_the_images_already_decided(
    conn: _FakeConn,
) -> None:
    """The 1,440 human positives predate tag_candidates entirely, so a
    queue-only history is blind to the whole of today's ground truth: the
    byte-identical twin of a stored positive would be queued, labeled again, and
    inflate the head. Asserted on the SQL text — a fake conn has no images table.
    Human decisions are ordered first so the bound sheds backfill, not truth."""
    sql = " ".join(tc._EXISTING_POOL_SQL.split())
    assert "FROM tag_candidates c" in sql
    assert "FROM image_tag_labels itl JOIN images i ON i.id = itl.image_id" in sql
    assert "UNION ALL" in sql
    assert "ORDER BY (itl.source IN ('human', 'human_confirmed')) DESC" in sql


# --- draw_candidates: what gets written -------------------------------------


def test_draw_candidates_writes_the_provenance_of_every_accepted_row(
    conn: _FakeConn,
) -> None:
    conn.pool_by_category["byt"] = [
        _pool_row(11, draw="centroid_head", rank=1, pool_size=5820, listing_id=71,
                  property_id=91, phash=1234, distance=0.0412, positives=31),
    ]
    res = tc.draw_candidates(conn, tag_id=1, count=120, drawn_by="runner")
    assert res["inserted"] == 1
    written = conn.inserted[0]
    assert written == {
        "tag_id": 1, "image_id": 11, "draw": "centroid_head", "category_main": "byt",
        "distance": 0.0412, "pool_rank": 1, "pool_size": 5820, "listing_id": 71,
        "property_id": 91, "phash": 1234, "centroid_positive_count": 31,
        "model": MODEL, "drawn_by": "runner",
    }
    # definition_id is resolved inside the INSERT, never handed in by a caller.
    assert "definition_id" not in written


def test_draw_candidates_reports_the_band_and_category_composition(
    conn: _FakeConn,
) -> None:
    conn.pool_by_category["byt"] = [
        _pool_row(1, draw="centroid_head", rank=1, listing_id=1),
        _pool_row(2, draw="centroid_mid", rank=40, listing_id=2),
    ]
    conn.pool_by_category["dum"] = [_pool_row(3, draw="random", rank=900, listing_id=3)]
    res = tc.draw_candidates(conn, tag_id=1, count=120)
    assert res["status"] == "drawn"
    assert res["inserted"] == 3
    assert res["by_draw"] == {"centroid_head": 1, "centroid_mid": 1, "random": 1}
    assert res["by_category"]["byt"] == 2
    assert res["by_category"]["dum"] == 1
    byt = next(c for c in res["categories"] if c["category_main"] == "byt")
    assert byt["status"] == "drawn"
    assert byt["pool_size"] == 100
    assert byt["requested"] == tc.allocate_counts(120, tc.CATEGORY_MIX)["byt"]
    assert "elapsed_ms" in byt


def test_draw_candidates_reports_an_empty_pool_as_such(conn: _FakeConn) -> None:
    # A category whose pool came back empty is reported, not silently absent: it
    # is the difference between "nothing matched" and "never asked".
    res = tc.draw_candidates(conn, tag_id=1, count=120)
    assert {c["status"] for c in res["categories"]} == {"empty_pool"}
    assert res["inserted"] == 0


def test_draw_candidates_reports_the_shortfall_rather_than_backfilling_it(
    conn: _FakeConn,
) -> None:
    # There is no redistribution between bands: a thin mid band stays thin and the
    # gap shows as requested vs inserted.
    conn.pool_by_category["byt"] = [_pool_row(1, draw="centroid_head", listing_id=1)]
    res = tc.draw_candidates(conn, tag_id=1, count=120)
    assert res["requested"] == 120
    assert res["inserted"] == 1
    assert res["by_draw"]["centroid_mid"] == 0


def test_draw_candidates_counts_its_drops(conn: _FakeConn) -> None:
    conn.pool_by_category["byt"] = [
        _pool_row(1, rank=1, listing_id=10, property_id=5, phash=0b0),
        _pool_row(2, rank=2, listing_id=10, property_id=5, phash=0b11111),  # near-dup
        _pool_row(3, rank=3, listing_id=11, property_id=5, phash=0b1111110),
        _pool_row(4, rank=4, listing_id=12, property_id=5, phash=0b111111000000),
    ]
    res = tc.draw_candidates(conn, tag_id=1, count=120)
    assert res["dropped_near_dup"] == 1
    assert res["dropped_property_cap"] == 1  # the 4th row, property 5 already full
    assert res["inserted"] == 2


def test_a_later_category_cannot_re_introduce_an_accepted_near_duplicate(
    conn: _FakeConn,
) -> None:
    # The running state is folded forward between categories; without that, two
    # categories could each accept the same reused agency photo. dum (quota 30)
    # runs before byt (quota 36) — smallest quota first — so image 2 lands and
    # image 1 is the one dropped.
    conn.pool_by_category["byt"] = [_pool_row(1, listing_id=1, phash=0b0)]
    conn.pool_by_category["dum"] = [_pool_row(2, listing_id=2, phash=0b111)]
    res = tc.draw_candidates(conn, tag_id=1, count=120)
    assert [p["image_id"] for p in conn.inserted] == [2]
    assert res["dropped_near_dup"] == 1


def test_the_stored_pool_seeds_the_per_property_cap(conn: _FakeConn) -> None:
    conn.existing = [(90, None, 70, 91), (91, None, 71, 91)]  # property 91 is full
    conn.pool_by_category["byt"] = [_pool_row(1, listing_id=72, property_id=91)]
    res = tc.draw_candidates(conn, tag_id=1, count=120)
    assert conn.inserted == []
    assert res["dropped_property_cap"] == 1


# --- draw_candidates: degrading -------------------------------------------


def test_a_cancelled_category_degrades_alone(conn: _FakeConn) -> None:
    """A statement timeout on one category rolls that category back and the draw
    carries on — a slow `byt` must not cost the operator the other four."""
    conn.cancel_categories = {"byt"}
    conn.pool_by_category["dum"] = [_pool_row(3, listing_id=3)]
    res = tc.draw_candidates(conn, tag_id=1, count=120)
    byt = next(c for c in res["categories"] if c["category_main"] == "byt")
    assert byt["status"] == "timeout"
    assert byt["inserted"] == 0
    assert res["inserted"] == 1
    assert [p["image_id"] for p in conn.inserted] == [3]


def test_an_exhausted_budget_skips_the_remaining_categories(
    conn: _FakeConn, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The draw is called synchronously from an admin route, so it finalizes on a
    # wall-clock budget instead of running until the request dies.
    # The clock stays at 0 through the first category's own calls, then jumps past
    # the budget — so exactly one category is drawn and the rest are reported.
    calls = {"n": 0}

    def _clock() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] <= 4 else 999.0

    monkeypatch.setattr(tc.time, "monotonic", _clock)
    res = tc.draw_candidates(conn, tag_id=1, count=120, max_seconds=45)
    statuses = [c["status"] for c in res["categories"]]
    assert statuses[0] != "skipped_budget"
    assert set(statuses[1:]) == {"skipped_budget"}


def test_a_zero_budget_means_no_budget(conn: _FakeConn) -> None:
    res = tc.draw_candidates(conn, tag_id=1, count=120, max_seconds=0)
    assert set(c["category_main"] for c in res["categories"]) == set(tc.CATEGORY_MIX)
    assert {c["status"] for c in res["categories"]} == {"empty_pool"}
    # With no budget the per-statement ceiling is the constant, not a remainder.
    timeouts = {s for s, _ in conn.executed if s.startswith("SET LOCAL statement_timeout")}
    assert timeouts == {f"SET LOCAL statement_timeout = {tc.DRAW_STATEMENT_TIMEOUT_MS}"}


def test_the_statement_timeout_is_derived_from_the_remaining_budget(
    conn: _FakeConn, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fixed 60s ceiling under a 45s budget is not a bound: a category that
    starts at 44.9s can still run a further 60s, so one synchronous admin request
    reaches ~105s and dies at the proxy on top of already-committed categories.
    Here the clock reads 40s of a 45s budget when the first category starts, so
    that category gets the 5s that are actually left."""
    clock = iter([0.0, 40.0, 40.0, 40.0] + [999.0] * 20)
    monkeypatch.setattr(tc.time, "monotonic", lambda: next(clock))
    tc.draw_candidates(conn, tag_id=1, count=120, max_seconds=45)
    first = next(s for s, _ in conn.executed if s.startswith("SET LOCAL statement_timeout"))
    assert first == "SET LOCAL statement_timeout = 5000"


# --- candidate_summary ------------------------------------------------------


def test_candidate_summary_raises_for_an_unknown_tag(conn: _FakeConn) -> None:
    with pytest.raises(KeyError):
        tc.candidate_summary(conn, tag_id=404)


def test_candidate_summary_folds_the_two_groupings_into_one_readout(
    conn: _FakeConn,
) -> None:
    conn.summary_by_draw = [
        ("centroid_mid", 72, 30, 12, 30, "2026-08-27T10:00:00Z"),
        ("centroid_head", 120, 40, 61, 19, "2026-08-27T12:00:00Z"),
    ]
    conn.summary_by_category = [
        ("byt", 96, 44, 40, 12, "2026-08-27T12:00:00Z"),
        ("pozemek", 96, 26, 33, 37, "2026-08-27T10:00:00Z"),
    ]
    out = tc.candidate_summary(conn, tag_id=1)
    assert (out["total"], out["open"], out["reviewed"]) == (192, 70, 122)
    assert out["last_drawn_at"] == "2026-08-27T12:00:00Z"
    # DRAWS order, not the order the rows came back in — and the band that has
    # never produced a row is OMITTED, not zero-filled: "never drawn" and "drawn,
    # all decided" are different facts.
    assert [b["key"] for b in out["by_draw"]] == ["centroid_head", "centroid_mid"]
    assert out["by_draw"][0] == {
        "key": "centroid_head", "total": 120, "open": 40,
        "positive": 61, "negative": 19,
    }
    assert [b["key"] for b in out["by_category"]] == ["byt", "pozemek"]


def test_candidate_summary_reports_each_bands_yield(conn: _FakeConn) -> None:
    """The random band's yield is the ONE self-check this design has — an
    unranked sample of the pool that keeps coming back positive means the
    centroid is missing a mode. Without it a band readout can only say how much
    work is left, never whether the retrieval works."""
    conn.summary_by_draw = [("random", 40, 10, 9, 21, "2026-08-27T12:00:00Z")]
    band = tc.candidate_summary(conn, tag_id=1)["by_draw"][0]
    assert (band["positive"], band["negative"]) == (9, 21)
    sql = next(s for s, _ in conn.executed if s.startswith("SELECT c.draw,"))
    assert "count(*) FILTER (WHERE lab.state = 'positive')::int AS positive" in sql
    assert "count(*) FILTER (WHERE lab.state = 'negative')::int AS negative" in sql


def test_candidate_summary_reports_whether_the_tag_can_be_drawn_for(
    conn: _FakeConn,
) -> None:
    conn.verified_positives = tc.MIN_VERIFIED_POSITIVES
    assert tc.candidate_summary(conn, tag_id=1)["can_draw"] is True
    conn.verified_positives = tc.MIN_VERIFIED_POSITIVES - 1
    out = tc.candidate_summary(conn, tag_id=1)
    assert out["can_draw"] is False
    assert out["verified_positive_count"] == tc.MIN_VERIFIED_POSITIVES - 1
    assert out["min_verified_positives"] == tc.MIN_VERIFIED_POSITIVES


def test_candidate_summary_of_an_empty_queue_is_zeros_not_an_error(
    conn: _FakeConn,
) -> None:
    out = tc.candidate_summary(conn, tag_id=1)
    assert (out["total"], out["open"], out["reviewed"]) == (0, 0, 0)
    assert out["last_drawn_at"] is None
    assert out["by_draw"] == [] and out["by_category"] == []


def test_candidate_summary_derives_open_by_joining_the_labels(conn: _FakeConn) -> None:
    # The queue stores no state — "decided" is a LEFT JOIN onto image_tag_labels,
    # the only place a decision has ever lived.
    tc.candidate_summary(conn, tag_id=1)
    sql = next(s for s, _ in conn.executed if s.startswith("SELECT c.draw,"))
    assert "LEFT JOIN image_tag_labels lab" in sql
    assert "count(*) FILTER (WHERE lab.image_id IS NULL)::int AS open" in sql


# --- routing scope (migration 457) ------------------------------------------


def test_scoped_mix_renormalises_so_a_narrowed_draw_is_not_a_short_draw() -> None:
    # Filtering CATEGORY_MIX without renormalising leaves the weights summing to
    # 0.70 for a byt/dum/komercni tag, and allocate_counts would hand back ~84 of a
    # requested 120 — a narrowed scope that reads as a thin corpus.
    mix = tc.scoped_mix(("byt", "dum", "komercni"))
    assert set(mix) == {"byt", "dum", "komercni"}
    assert abs(sum(mix.values()) - 1.0) < 1e-9
    assert sum(tc.allocate_counts(120, mix).values()) == 120


def test_scoped_mix_of_no_scope_is_the_global_mix() -> None:
    assert tc.scoped_mix(()) == tc.CATEGORY_MIX


def test_scoped_mix_keeps_the_relative_weights_of_the_categories_it_keeps() -> None:
    mix = tc.scoped_mix(("byt", "dum"))
    assert mix["byt"] / mix["dum"] == pytest.approx(
        tc.CATEGORY_MIX["byt"] / tc.CATEGORY_MIX["dum"]
    )


def test_routing_categories_drops_values_outside_the_known_vocabulary(
    conn: _FakeConn,
) -> None:
    # Operator-owned free text: a typo should narrow a draw, never break one.
    conn.routing_categories = ["byt", "hausboat", "dum"]
    assert tc.routing_categories(conn, tag_id=1) == ("byt", "dum")


def test_a_scope_that_filters_to_nothing_falls_back_to_the_whole_mix(
    conn: _FakeConn,
) -> None:
    # Otherwise one bad edit leaves a tag permanently undrawable.
    conn.routing_categories = ["nonsense"]
    assert tc.routing_categories(conn, tag_id=1) == ()
    assert tc.scoped_mix(tc.routing_categories(conn, tag_id=1)) == tc.CATEGORY_MIX


def test_a_scoped_tag_never_draws_from_a_category_it_does_not_serve(
    conn: _FakeConn,
) -> None:
    # The bug this fixes, measured live 2026-08-28: koupelna's first draw returned
    # 54 rows — pozemek 24, komercni 18, ostatni 12, byt 0, dum 0. Every row came
    # from a property type where bathrooms essentially do not occur.
    conn.routing_categories = ["byt", "dum", "komercni"]
    conn.pool_by_category = {c: [] for c in ("byt", "dum", "komercni")}
    tc.draw_candidates(conn, tag_id=1, count=120)
    asked = {p["category_main"] for s, p in conn.executed
             if s.startswith("WITH centroid AS")}
    assert asked == {"byt", "dum", "komercni"}
    assert "pozemek" not in asked and "ostatni" not in asked


def test_an_explicit_category_still_overrides_the_tags_scope(conn: _FakeConn) -> None:
    # The caller naming one category is a deliberate request, not a mistake.
    conn.routing_categories = ["byt", "dum"]
    conn.pool_by_category = {"pozemek": []}
    tc.draw_candidates(conn, tag_id=1, count=20, category_main="pozemek")
    asked = {p["category_main"] for s, p in conn.executed
             if s.startswith("WITH centroid AS")}
    assert asked == {"pozemek"}


def test_an_unscoped_tag_still_draws_the_full_mix(conn: _FakeConn) -> None:
    conn.routing_categories = None
    conn.pool_by_category = {c: [] for c in tc.CATEGORY_MIX}
    tc.draw_candidates(conn, tag_id=1, count=120)
    asked = {p["category_main"] for s, p in conn.executed
             if s.startswith("WITH centroid AS")}
    assert asked == set(tc.CATEGORY_MIX)


def test_the_summary_reports_the_scope_so_a_narrow_draw_explains_itself(
    conn: _FakeConn,
) -> None:
    conn.routing_categories = ["byt", "dum", "komercni"]
    out = tc.candidate_summary(conn, tag_id=1)
    assert out["routing_categories"] == ["byt", "dum", "komercni"]


# --- budget fairness --------------------------------------------------------


def test_no_category_may_claim_the_whole_remaining_budget(conn: _FakeConn) -> None:
    # Greedy ceilings let the first category eat the budget and leave the rest
    # 'skipped_budget'. Because the loop runs smallest-quota-first, the starved ones
    # were always byt and dum — the two most tags care about most.
    conn.pool_by_category = {c: [] for c in tc.CATEGORY_MIX}
    tc.draw_candidates(conn, tag_id=1, count=120, max_seconds=50)
    timeouts = [int(s.split("=")[1].strip())
                for s, _ in conn.executed
                if s.startswith("SET LOCAL statement_timeout")]
    assert timeouts, "expected a per-category statement timeout to be set"
    # Five categories, ~50s: the first must not be handed anything like all of it.
    assert timeouts[0] <= 50_000 // 5 + 1_000


def test_unspent_budget_rolls_forward_to_the_later_categories(
    conn: _FakeConn,
) -> None:
    # Fair share must not become a straitjacket: the loop runs smallest-first, so
    # the LAST category is the largest and should inherit what the small ones left.
    conn.pool_by_category = {c: [] for c in tc.CATEGORY_MIX}
    tc.draw_candidates(conn, tag_id=1, count=120, max_seconds=50)
    timeouts = [int(s.split("=")[1].strip())
                for s, _ in conn.executed
                if s.startswith("SET LOCAL statement_timeout")]
    assert timeouts[-1] > timeouts[0]


def test_a_zero_budget_still_means_no_wall_clock_limit(conn: _FakeConn) -> None:
    # The runner passes max_seconds=0 deliberately: the 45s default is shaped for a
    # synchronous admin request, not a background job.
    conn.pool_by_category = {c: [] for c in tc.CATEGORY_MIX}
    tc.draw_candidates(conn, tag_id=1, count=120, max_seconds=0)
    timeouts = [int(s.split("=")[1].strip())
                for s, _ in conn.executed
                if s.startswith("SET LOCAL statement_timeout")]
    assert timeouts and all(t == tc.DRAW_STATEMENT_TIMEOUT_MS for t in timeouts)
