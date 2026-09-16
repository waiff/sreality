"""Every statement the judge lane runs, as module-level constants (the PREPARE gate).

Two tables and nothing else: `autodedup.judgements` (this program's own verdict store,
migration 528) and a read of `llm_calls` for the cost the provider actually billed. The
lane writes no other shared relation — `llm_calls` itself is written by `LLMClient`, never
from here.

Nullable parameters carry explicit casts: psycopg sends no type OID for a Python `None`, so
an uncast NULL parameter fails Parse with 42P18. `confidence`, `unit_discriminator`,
`llm_call_id` and both evidence arrays are all legitimately null on an
`insufficient_evidence` verdict, so every one of them is cast.
"""

from __future__ import annotations

# E29: a pair is judged once per (version, tier). A re-run of the same tier is a RETRY, not a
# second opinion, so it overwrites — and `created_at` is restamped with it, because a row whose
# timestamp predates the verdict it holds cannot be reconciled against `llm_calls` by time.
JUDGEMENT_UPSERT_SQL = """
    insert into autodedup.judgements (
        listing_lo, listing_hi, judge_version, tier, model, verdict, confidence,
        unit_discriminator, key_evidence, contradicting_evidence,
        developer_project_suspected, llm_call_id, cost_usd
    ) values (
        %(listing_lo)s, %(listing_hi)s, %(judge_version)s, %(tier)s, %(model)s,
        %(verdict)s, %(confidence)s::real, %(unit_discriminator)s::text,
        %(key_evidence)s::text[], %(contradicting_evidence)s::text[],
        %(developer_project_suspected)s::boolean, %(llm_call_id)s::bigint,
        %(cost_usd)s::numeric
    )
    on conflict (listing_lo, listing_hi, judge_version, tier) do update set
        model = excluded.model,
        verdict = excluded.verdict,
        confidence = excluded.confidence,
        unit_discriminator = excluded.unit_discriminator,
        key_evidence = excluded.key_evidence,
        contradicting_evidence = excluded.contradicting_evidence,
        developer_project_suspected = excluded.developer_project_suspected,
        llm_call_id = excluded.llm_call_id,
        cost_usd = excluded.cost_usd,
        created_at = now()
"""

# E29's cache read: which of this sample's pairs already carry a verdict at this version and
# tier, so a re-dispatched lane pays for the remainder only. The two arrays are zipped by
# `unnest`, not crossed: matching `lo = any(los) and hi = any(his)` would read back the whole
# 400x400 product of a 400-pair sample instead of the 400 pairs it actually drew.
JUDGEMENT_CACHED_SQL = """
    select listing_lo, listing_hi
      from autodedup.judgements
     where judge_version = %(judge_version)s
       and tier = %(tier)s
       and (listing_lo, listing_hi) in (
             select lo, hi
               from unnest(%(los)s::bigint[], %(his)s::bigint[]) as pair(lo, hi)
           )
"""

# E32: spend is READ from `llm_calls`, never extrapolated from token prices. The fallback for a
# response that carried no cost of its own, and the lane's own end-of-run reconciliation.
JUDGEMENT_COST_SQL = """
    select coalesce(sum(cost_usd), 0)::numeric as cost_usd,
           count(*)                            as n_calls
      from llm_calls
     where id = any(%(ids)s::bigint[])
"""
