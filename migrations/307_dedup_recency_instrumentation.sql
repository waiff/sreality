-- 307_dedup_recency_instrumentation.sql
-- Session 5 (dedup-vision-and-backlog-overhaul.md §5/§6, point 2): recency-first
-- compare ordering across the sweep lanes + candidate drain. Two additions:
--
-- 1. first_engine_decision_at — write-once (COALESCE, never overwritten), stamped
--    the FIRST time resolve_pair ever records a decision on a proposed candidate
--    row, whether it stays proposed, gets auto-dismissed, or merges. Distinct from
--    last_engine_decision_at (migration 272, updated on every re-decision): this is
--    the "time to first engine look" signal the acceptance metric below needs to
--    show whether recency-first ordering actually gets fresh pairs looked at sooner,
--    as opposed to last_engine_decision_at which a stale-but-frequently-re-decided
--    pair can keep looking "fresh" on.
--
-- 2. dedup_recency_backlog — the Session 5 acceptance-metric view: unresolved
--    ('proposed') candidate pairs bucketed by how recently either side's property
--    was first seen (GREATEST(left, right).first_seen_at — a pair is "fresh" the
--    moment either side is a new listing). Per-tier + a ROLLUP grand-total row.
--    Plain view (not materialized): ~40k proposed rows, two indexed joins, cheap
--    enough for ad hoc/dashboard queries without a refresh cadence to maintain.

alter table property_identity_candidates
    add column if not exists first_engine_decision_at timestamptz;

create or replace view dedup_recency_backlog as
select
    c.tier,
    count(*) filter (
        where greatest(pl.first_seen_at, pr.first_seen_at) > now() - interval '1 day'
    ) as unresolved_lt_1d,
    count(*) filter (
        where greatest(pl.first_seen_at, pr.first_seen_at) > now() - interval '3 days'
    ) as unresolved_lt_3d,
    count(*) filter (
        where greatest(pl.first_seen_at, pr.first_seen_at) > now() - interval '7 days'
    ) as unresolved_lt_7d,
    count(*) as unresolved_total
from property_identity_candidates c
join properties pl on pl.id = c.left_property_id
join properties pr on pr.id = c.right_property_id
where c.status = 'proposed'
group by rollup (c.tier);
