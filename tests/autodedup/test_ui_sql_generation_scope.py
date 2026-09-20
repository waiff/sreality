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


def test_a_cluster_ruling_can_only_replace_a_ruling_of_the_same_pass() -> None:
    """The conflict target carries the generation, matching the partial unique index migration
    538 creates. Without it the upsert re-stamped the one row a key was allowed — so ruling the
    g5 group 38324 destroyed the operator's g4 ruling, the last record of what they saw there.
    """
    flat = _flat(usql.VERDICT_CLUSTER_UPSERT_SQL)
    assert (
        "ON CONFLICT (kind, cluster_key, (coalesce(generation, ''::text)), decided_by) "
        "WHERE kind = 'cluster'" in flat
    )
    # `generation` is part of the key now, so a re-ruling never reassigns it; `member_ids`
    # still is, because re-running the SAME pass can move the group under the key.
    assert "generation = excluded.generation" not in flat
    assert "member_ids = excluded.member_ids" in flat


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


def test_a_legacy_ruling_never_unions_member_sets_across_passes() -> None:
    """A row with neither `generation` nor `member_ids` names no set. The agreement read still
    falls back to `cluster_members` for it — but only where the key belongs to ONE pass, or the
    `array_agg` would invent operator labels out of two passes' members and feed them to the D6
    gate.
    """
    guard = (
        "SELECT CASE WHEN count(DISTINCT m.generation) = 1 "
        "THEN array_agg(m.listing_id ORDER BY m.listing_id) END AS ids"
    )
    assert guard in _flat(usql.AGREEMENT_PAIRS_SQL)
    assert guard in _flat(usql.AGREEMENT_OVERSIZE_SQL)
