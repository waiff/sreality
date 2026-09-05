"""The bake-off manifest builder, against a fake connection — no database, no R2.

The rails these tests exist for, in order of how badly each would hurt:
  * the sealed exam must be excluded from every population that reads labels;
  * P1a's DEFINITION and the pod harness's contamination FILTER are separate numbers
    (sharing them empties the positive set by construction);
  * `images` joins `listings` on `listing_id`, never `sreality_id` — the predecessor
    keyed on sreality_id, which is NULL for every non-sreality portal since Gate 2;
  * a dry run mints no presigned URL and uploads nothing.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from scripts import dinov3_bakeoff_manifest as mf
from toolkit import tag_holdout


# ---------------------------------------------------------------------------
# The statements
# ---------------------------------------------------------------------------

def _norm(sql: str) -> str:
    return " ".join(sql.split())


def test_the_label_read_carries_the_shared_holdout_exclusion() -> None:
    """`tests/test_holdout_exclusion_census.py` is the real rail; this pins the
    intent locally so a refactor that drops the anti-join fails HERE first, with a
    message that says why."""
    marker = _norm(tag_holdout.exclusion_for("l"))
    assert marker in _norm(mf._LABELLED_IMAGES_SQL), (
        "every population drawn from image_tag_labels must exclude the sealed exam")


def test_no_statement_joins_images_to_listings_on_sreality_id() -> None:
    """The predecessor's manifest keyed `images` to `listings` on `i.sreality_id`,
    which has been NULL for every non-sreality portal since Gate 2. images.listing_id
    is NOT NULL-enforced (migration 350) and is the only correct arbiter."""
    for name in dir(mf):
        if not name.endswith("_SQL"):
            continue
        sql = _norm(getattr(mf, name))
        assert "sreality_id" not in sql, f"{name} still keys on sreality_id"


def test_every_statement_is_a_read() -> None:
    for name in dir(mf):
        if not name.endswith("_SQL"):
            continue
        first = _norm(getattr(mf, name)).split(None, 1)[0].upper()
        assert first in {"SELECT", "WITH"}, f"{name} is not a read ({first})"


def test_no_statement_touches_the_production_embedding_table() -> None:
    """`image_dinov3_embeddings` belongs to the production embedding job. This one
    reads the INCUMBENT's vectors and writes none of its own."""
    for name in dir(mf):
        if name.endswith("_SQL"):
            assert "image_dinov3_embeddings" not in getattr(mf, name), name


# ---------------------------------------------------------------------------
# P1a — the pigeonhole near-duplicate matcher
# ---------------------------------------------------------------------------

def test_near_duplicate_pairs_finds_exact_and_one_bit_matches_across_listings() -> None:
    rows = [
        (1, 100, 0b1010),
        (2, 200, 0b1010),   # identical hash, different listing -> a P1a pair
        (3, 300, 0b1011),   # 1 bit away
        (4, 100, 0b1010),   # SAME listing as 1 -> P2's business, not P1a's
        (5, 400, (1 << 40)),  # far away
    ]
    pairs = mf.near_duplicate_pairs(rows, max_hamming=2, want=100)
    found = {(a, b) for a, b, _ in pairs}
    assert (1, 2) in found and (1, 3) in found and (2, 3) in found
    assert (1, 4) not in found, "same-listing pairs are not P1a"
    assert all(d <= 2 for _, _, d in pairs)


def test_near_duplicate_pairs_respects_the_want_cap_and_dedupes() -> None:
    rows = [(i, i * 10, 0b1010) for i in range(1, 20)]
    pairs = mf.near_duplicate_pairs(rows, max_hamming=0, want=5)
    assert len(pairs) == 5
    assert len({(a, b) for a, b, _ in pairs}) == 5


def test_near_duplicate_pairs_skips_oversized_buckets() -> None:
    """Blank/near-white images all collide on the same chunk. Without the cap one
    bucket is a quadratic blow-up that starves every other bucket."""
    rows = [(i, i * 10, 0) for i in range(1, 50)]
    assert mf.near_duplicate_pairs(rows, max_hamming=0, want=100, bucket_max=10) == []
    assert mf.near_duplicate_pairs(rows, max_hamming=0, want=5, bucket_max=100)


def test_near_duplicate_pairs_handles_signed_phashes() -> None:
    rows = [(1, 100, -1), (2, 200, -1)]
    pairs = mf.near_duplicate_pairs(rows, max_hamming=0, want=10)
    assert pairs == [(1, 2, 0)]


def test_chunks_partition_the_full_64_bits() -> None:
    a, b, c = mf._chunks(0xFFFFFFFFFFFFFFFF)
    assert a.bit_length() == 22 and b.bit_length() == 21 and c.bit_length() == 21


# ---------------------------------------------------------------------------
# Pair formation for P2 / P3 / P4
# ---------------------------------------------------------------------------

def _rec(image_id: int, listing_id: int, obec_id: int | None) -> dict[str, Any]:
    return {"image_id": image_id, "listing_id": listing_id, "obec_id": obec_id}


def test_p3_pairs_come_from_different_obce() -> None:
    groups = {22: [_rec(1, 10, 500), _rec(2, 20, 500), _rec(3, 30, 900)]}
    pairs = mf.pairs_within_groups(groups, want=10, rng=random.Random(1),
                                   same=False, key="obec_id")
    for a, b, _tag in pairs:
        obce = {r["obec_id"] for r in groups[22] if r["image_id"] in (a, b)}
        assert len(obce) == 2, "a P3 pair must span two obce"


def test_p2_pairs_come_from_the_same_listing() -> None:
    groups = {(10, 22): [_rec(1, 10, 500), _rec(2, 10, 500), _rec(3, 10, 500)]}
    pairs = mf.pairs_within_groups(groups, want=10, rng=random.Random(1),
                                   same=True, key="listing_id")
    assert len(pairs) == 1, "three members yield one pair; members are consumed"
    a, b, _ = pairs[0]
    assert {a, b} <= {1, 2, 3}


def test_pair_formation_is_round_robin_across_groups() -> None:
    """One crowded tag must not own the sample — P3's whole point is breadth."""
    groups = {
        1: [_rec(i, i, i % 2) for i in range(100, 140)],
        2: [_rec(i, i, i % 2) for i in range(200, 204)],
    }
    pairs = mf.pairs_within_groups(groups, want=6, rng=random.Random(7),
                                   same=False, key="obec_id")
    tags = [t for _, _, t in pairs]
    assert tags.count(2) >= 2, f"crowded group starved the small one: {tags}"


def test_pair_formation_never_repeats_an_image_pair() -> None:
    groups = {1: [_rec(i, i, i % 2) for i in range(1, 11)]}
    pairs = mf.pairs_within_groups(groups, want=50, rng=random.Random(3),
                                   same=False, key="obec_id")
    keys = {(a, b) for a, b, _ in pairs}
    assert len(keys) == len(pairs)


def test_a_single_obec_tag_yields_nothing_without_a_quadratic_scan() -> None:
    """A tag whose every positive sits in ONE obec supports no P3 pair. Discovering
    that by comparing every ordered pair would be 4e8 comparisons for an empty
    answer, so the bucketing has to make it cheap as well as correct."""
    groups = {22: [_rec(i, i, 500) for i in range(20_000)]}
    assert mf.pairs_within_groups(groups, want=1000, rng=random.Random(1),
                                  same=False, key="obec_id") == []


def test_each_image_is_consumed_by_at_most_one_pair() -> None:
    groups = {1: [_rec(i, i, i % 2) for i in range(1, 21)]}
    pairs = mf.pairs_within_groups(groups, want=50, rng=random.Random(5),
                                   same=False, key="obec_id")
    used = [i for a, b, _ in pairs for i in (a, b)]
    assert len(used) == len(set(used)) == 20


def test_pair_formation_is_deterministic_under_a_seed() -> None:
    groups = {1: [_rec(i, i, i % 3) for i in range(1, 20)]}
    first = mf.pairs_within_groups(dict(groups), want=5, rng=random.Random(11),
                                   same=False, key="obec_id")
    second = mf.pairs_within_groups(dict(groups), want=5, rng=random.Random(11),
                                    same=False, key="obec_id")
    assert first == second


# ---------------------------------------------------------------------------
# Budgeting
# ---------------------------------------------------------------------------

def test_the_image_budget_is_spent_round_robin_across_populations() -> None:
    pops = {
        "P1a": [(i, i + 1000, 0) for i in range(1, 40)],
        "P3": [(i, i + 2000, 22) for i in range(100, 140)],
    }
    kept = mf.truncate_to_image_budget(pops, solo_images=[], max_images=20)
    assert kept["P1a"] and kept["P3"], "truncation must not spend the budget on one"
    used = {i for rows in kept.values() for r in rows for i in (r[0], r[1])}
    assert len(used) <= 20


def test_solo_images_count_against_the_budget() -> None:
    pops = {"P1a": [(1, 2, 0)]}
    kept = mf.truncate_to_image_budget(pops, solo_images=range(100, 200),
                                       max_images=100)
    assert kept["P1a"] == [], "P1b and the canary already spent the budget"


def test_sample_pct_scales_inversely_with_the_table_size() -> None:
    assert mf.sample_pct(1000, 0) == 100.0
    small = mf.sample_pct(1000, 1_000_000)
    big = mf.sample_pct(1000, 10_000_000)
    assert small > big and 0.0 < big <= 100.0
    assert mf.sample_pct(10_000_000, 1000) == 100.0  # clamped


# ---------------------------------------------------------------------------
# End to end against a fake connection
# ---------------------------------------------------------------------------

class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        flat = _norm(sql)
        self._conn.executed.append((flat, params))
        for needle, rows in self._conn.script:
            if needle in flat:
                self._rows = rows(params) if callable(rows) else list(rows)
                return
        self._rows = []

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, script: list[tuple[str, Any]]) -> None:
        self.script = script
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)


def _labelled(params: Any) -> list[tuple]:
    """Positives for the requested tags. Tag 3 is the document tag; 22 is a photo tag
    spread over two obce and two listings."""
    tags = set(params["tag_ids"])
    rows: list[tuple] = []
    if 22 in tags:
        rows += [
            (501, 22, 71, 900), (502, 22, 71, 900),   # same listing -> P2
            (503, 22, 72, 901),                        # different obec -> P3
        ]
    if 3 in tags:
        rows += [(601, 3, 81, 950), (602, 3, 82, 951)]  # documents, two listings
    return rows


def _script() -> list[tuple[str, Any]]:
    return [
        ("FROM pg_class", [(11_000_000,)]),
        ("FROM tag_taxonomy WHERE active AND label ILIKE", [(3, "interier - pudorys")]),
        ("SELECT id, label FROM tag_taxonomy WHERE active",
         [(3, "interier - pudorys"), (22, "interier - koupelna")]),
        ("FROM images i TABLESAMPLE SYSTEM (%(pct)s) JOIN image_clip_tags",
         [(301, 71, 5, "img/301.jpg", 0.1, "kitchen"),
          (302, 72, 6, "img/302.jpg", None, "bathroom")]),
        ("FROM images i TABLESAMPLE SYSTEM",
         [(101, 11, 0b1010), (102, 22, 0b1010), (103, 33, 0b1011)]),
        ("FROM image_tag_labels l", _labelled),
        ("FROM images i WHERE i.storage_path IS NOT NULL AND i.phash IS NOT NULL",
         [(1, "img/1.jpg"), (2, "img/2.jpg")]),
        ("FROM unnest(%(a)s::bigint[]",
         lambda p: [(a, b, 0.42) for a, b in zip(p["a"], p["b"])]),
        ("FROM image_clip_embeddings e",
         [("openai/clip-vit-base-patch32", None, 12)]),
        ("LEFT JOIN image_clip_tags t",
         lambda p: [(i, f"img/{i}.jpg", 1234, 70 + (i % 3), 900, 0.1, "kitchen")
                    for i in p["ids"]]),
    ]


def _args(**overrides: Any):
    args = mf.parse_args(["--dry-run", "--p1a-scan-images", "1000"])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_a_dry_run_draws_every_population_and_mints_no_url() -> None:
    conn = _FakeConn(_script())
    manifest = mf.build_manifest(conn, _args())
    pops = manifest["populations"]
    assert manifest["dry_run"] is True
    assert manifest["images"] == {}, "a dry run must not presign a single URL"
    assert pops["P1a"]["pairs"], "P1a drew nothing"
    assert pops["P1b"]["images"] == [301, 302]
    assert len(pops["P2"]["pairs"]) == 1
    assert len(pops["P3"]["pairs"]) == 1
    assert len(pops["P4"]["pairs"]) == 1
    assert manifest["canary"]["image_ids"] == [1, 2]
    assert [t["name"] for t in manifest["transform_recipes"]] == pops["P1b"]["transforms"]


def test_the_p2_pair_is_same_listing_and_the_p3_pair_is_not() -> None:
    conn = _FakeConn(_script())
    pops = mf.build_manifest(conn, _args())["populations"]
    assert {pops["P2"]["pairs"][0]["a"], pops["P2"]["pairs"][0]["b"]} == {501, 502}
    assert 503 in {pops["P3"]["pairs"][0]["a"], pops["P3"]["pairs"][0]["b"]}


def test_the_stored_clip_cosine_rides_along_per_pair() -> None:
    conn = _FakeConn(_script())
    pops = mf.build_manifest(conn, _args())["populations"]
    assert pops["P3"]["pairs"][0]["clip_cos"] == 0.42


def test_document_tags_are_reported_with_the_patterns_that_found_them() -> None:
    conn = _FakeConn(_script())
    p4 = mf.build_manifest(conn, _args())["populations"]["P4"]
    assert p4["tags"] == [{"id": 3, "label": "interier - pudorys"}]
    assert p4["patterns"] == mf.DEFAULT_DOCUMENT_TAG_PATTERNS
    assert "INSPECTION ONLY" in p4["role"]


def test_the_label_read_is_scoped_to_photo_tags_then_document_tags() -> None:
    """P4's tags must be drawn separately, not swept into P3 — a floor-plan pair is
    an eyeball set, never a scored negative."""
    conn = _FakeConn(_script())
    mf.build_manifest(conn, _args())
    label_calls = [p for sql, p in conn.executed if "FROM image_tag_labels l" in sql]
    assert len(label_calls) == 2
    assert 3 not in label_calls[0]["tag_ids"], "document tags leaked into P3's draw"
    assert label_calls[1]["tag_ids"] == [3]


def test_p1a_hamming_is_recorded_as_the_populations_definition() -> None:
    conn = _FakeConn(_script())
    p1a = mf.build_manifest(conn, _args(p1a_hamming=2))["populations"]["P1a"]
    assert p1a["max_hamming"] == 2
    assert p1a["partial_above_2"] is False
    conn = _FakeConn(_script())
    wide = mf.build_manifest(conn, _args(p1a_hamming=5))["populations"]["P1a"]
    assert wide["partial_above_2"] is True, (
        "three chunks only guarantee completeness up to distance 2 — say so")


def test_full_mode_uses_the_collision_aggregate_not_the_sample() -> None:
    script = _script()
    script.insert(0, ("GROUP BY i.phash",
                      [(0b1010, [101, 102], [11, 22])]))
    conn = _FakeConn(script)
    manifest = mf.build_manifest(conn, _args(p1a_mode="full"))
    assert any("GROUP BY i.phash" in sql for sql, _ in conn.executed)
    assert not any("TABLESAMPLE SYSTEM (%(pct)s) WHERE i.phash" in sql
                   for sql, _ in conn.executed)
    assert manifest["populations"]["P1a"]["pairs"]


def _fake_r2(monkeypatch: pytest.MonkeyPatch, *, configured: bool,
             minted: list[str] | None = None) -> None:
    """Stand in for scraper.image_storage on BOTH lookup paths.

    `from scraper import image_storage` reads the attribute off the already-imported
    `scraper` package, so patching sys.modules alone leaves the real client in place
    the moment any earlier test has imported it — which is how this passed in
    isolation and minted nothing under the full suite."""
    import sys
    from types import ModuleType

    import scraper

    class _R2:
        @classmethod
        def from_env(cls) -> "_R2":
            return cls()

        def presigned_get(self, key: str, expires_in: int = 0) -> str:
            if minted is not None:
                minted.append(key)
            return f"https://r2.invalid/{key}?exp={expires_in}"

    fake = ModuleType("scraper.image_storage")
    fake.is_configured = lambda: configured  # type: ignore[attr-defined]
    fake.R2Client = _R2                      # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scraper.image_storage", fake)
    monkeypatch.setattr(scraper, "image_storage", fake, raising=False)


def test_a_real_run_presigns_every_drawn_image(monkeypatch: pytest.MonkeyPatch) -> None:
    minted: list[str] = []
    _fake_r2(monkeypatch, configured=True, minted=minted)

    conn = _FakeConn(_script())
    manifest = mf.build_manifest(conn, _args(dry_run=False))
    assert manifest["images"], "a real run must carry presigned URLs"
    assert len(minted) == manifest["n_images"]
    any_url = next(iter(manifest["images"].values()))["url"]
    assert any_url.startswith("https://r2.invalid/") and "exp=604800" in any_url


def test_a_real_run_refuses_to_presign_without_r2(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_r2(monkeypatch, configured=False)
    with pytest.raises(RuntimeError, match="R2 not configured"):
        mf.build_manifest(_FakeConn(_script()), _args(dry_run=False))


def test_the_default_image_budget_matches_the_designs_20k() -> None:
    assert mf.parse_args([]).max_images == 20000
