"""Rails for the four reads/writes that a key alone no longer answers (rule E58, migration 538).

`(generation, cluster_key)` is the cluster key now, so a bare `cluster_key` names a group PER
PASS. Each statement below used to be correct only because it could not: one row per key, one
member set per key, one cluster ruling per key. These tests pin the four spellings that keep
them correct once the store holds several passes at once.
"""

from __future__ import annotations

from autodedup import ui_sql as usql


def _flat(sql: str) -> str:
    return " ".join(sql.split())


def test_a_cluster_ruling_is_newest_per_key_and_pass() -> None:
    """The newest row per (cluster_key, coalesce(generation, '')) is the group's ruling
    (migration 574): ruling the g5 group 38324 appends beside the operator's g4 ruling and never
    touches it. The pre-574 in-place arm is keyed on the same pass, so it cannot reach another
    pass's row either; `generation` is never reassigned, `member_ids` is part of what changed."""
    flat = _flat(usql.VERDICT_CLUSTER_APPEND_SQL)
    assert (
        "AND coalesce(x.generation, ''::text) = coalesce(%(generation)s::text, ''::text) "
        "ORDER BY x.decided_at DESC, x.id DESC LIMIT 1" in flat
    )
    assert "AND coalesce(u.generation, ''::text) = coalesce(%(generation)s::text, ''::text)" in flat
    assert "generation = %(generation)s" not in flat.split("restated AS (")[1].split("WHERE")[0]
    assert "member_ids = %(member_ids)s::bigint[]" in flat
    assert "AND n.member_ids IS NOT DISTINCT FROM %(member_ids)s::bigint[]" in flat


def test_the_queue_and_the_progress_strip_pick_the_same_ruling() -> None:
    """Both answer "which ruling does this group carry?" — the one that APPLIES, and only when
    none does is the latest kept as the stale hint. Newest-first alone let the queue read a g5
    ruling while browsing g4 (group: unreviewed) while the strip counted the g4 ruling it still
    held (group: done).
    """
    assert "ORDER BY applies DESC, vv.decided_at DESC, vv.id DESC" in usql._CLUSTER_FROM
    lateral = _flat(usql._CLUSTER_VERDICT_LATERAL)
    assert "vv.member_ids = coalesce(mem.ids, '{}'::bigint[])" in lateral
    assert "vv.member_ids = coalesce(mem.ids, '{}'::bigint[])" in _flat(usql._CLUSTER_FROM)


def test_one_key_is_resolved_to_one_group() -> None:
    """`GET /autodedup/groups/{key}` may be called with no generation at all; the row it lands
    on decides the dialog's members, its pairs and whether the verdict reads as stale."""
    flat = _flat(usql.GROUP_ONE_SQL)
    assert flat.endswith("ORDER BY c.last_changed_at DESC, c.generation DESC LIMIT 1")


def test_a_legacy_ruling_never_implies_a_pair() -> None:
    """A row with neither `generation` nor `member_ids` names no set, so it implies no pair: the
    operator's word (`_OPERATOR_WORDS`, one definition for the rulings and the Judge page) reads
    only the sets a ruling RECORDED, and never re-derives one from `cluster_members`, where a
    key names a group per pass and an `array_agg` would union two passes' members.
    """
    flat = _flat(usql._OPERATOR_WORDS)
    assert "WHERE x.kind = 'cluster' AND x.member_ids IS NOT NULL" in flat
    assert "cluster_members" not in flat
