"""Shape gate for migration 538 — the generation-scoped store (PROGRAM.md rule E58).

Offline, no DB. The generic RLS/grant rails see every statement here; this file checks what a
generic rail cannot know: that the three keys really carry the generation, that no sweep or
key can reach across one, and that the 224 cluster rulings taken on g4 groups are backfilled
with the member set the operator was looking at rather than with today's membership.

The member sets are cross-checked against the evidence that produced them only where the
evidence is in the repo; what is asserted here is the SHAPE — sorted ids, the idempotency
guard, one UPDATE per ruled key — because the artifacts live outside the tree.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION = _ROOT / "migrations" / "538_autodedup_generation_scoped_store.sql"


def _sql() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    body = "\n".join(line.split("--")[0] for line in _sql().lower().splitlines())
    return re.sub(r"\s+", " ", body)


def test_the_migration_exists_and_touches_only_schema_autodedup() -> None:
    assert _MIGRATION.is_file()
    code = _code()
    for table in re.findall(r"alter table (\S+)", code):
        assert table.startswith("autodedup."), table
    for table in re.findall(r"update (\S+)", code):
        assert table.startswith("autodedup."), table
    for table in re.findall(r"delete from (\S+)", code):
        assert table.startswith("autodedup."), table


def test_it_sets_a_plain_lock_timeout_and_resets_it() -> None:
    code = _code()
    assert "set lock_timeout = '5s';" in code
    assert "set local" not in code
    assert "reset lock_timeout;" in code


def test_the_three_keys_lead_with_the_generation() -> None:
    """E58's mechanical half: a pass can only ever overwrite its own rows."""
    code = _code()
    assert ("add constraint autodedup_clusters_pkey primary key "
            "(generation, cluster_key)") in code
    assert ("add constraint autodedup_cluster_members_pkey primary key "
            "(generation, cluster_key, listing_id)") in code
    assert ("add constraint autodedup_pairs_pkey primary key "
            "(generation, listing_lo, listing_hi)") in code
    # And the old ones are dropped, or the new key never applies.
    for name in ("clusters_pkey", "cluster_members_pkey", "pairs_pkey"):
        assert f"drop constraint if exists {name};" in code


def test_every_new_column_is_additive_and_guarded() -> None:
    code = _code()
    for table, column in (
        ("cluster_members", "generation"),
        ("pairs", "generation"),
        ("verdicts", "generation"),
        ("verdicts", "member_ids"),
    ):
        assert (f"alter table autodedup.{table} add column if not exists "
                f"{column}") in code, (table, column)
    assert "drop column" not in code


def test_the_generation_columns_end_up_not_null_on_the_two_engine_tables() -> None:
    code = _code()
    for table in ("cluster_members", "pairs"):
        assert (f"alter table autodedup.{table} alter column generation set not "
                "null;") in code
    # `verdicts.generation` is deliberately NULLABLE: a legacy row has no honest answer,
    # and NULL reads as legacy rather than as a claim.
    assert "alter table autodedup.verdicts alter column generation set not null" not in code


def test_a_pair_grain_verdict_stays_generation_free() -> None:
    """A pair ruling is about two listings and says as much about g4 as about g5 (E58)."""
    assert ("check (kind = 'cluster' or (generation is null and member_ids is null))"
            in _code())


def test_every_new_index_leads_with_the_generation_and_replaces_a_named_old_one() -> None:
    code = _code()
    created = dict(re.findall(r"create index if not exists (\S+) on (autodedup\.\S+)", code))
    dropped = set(re.findall(r"drop index if exists autodedup\.(\S+);", code))
    assert dropped, "no superseded index is dropped"
    # A new name for every replacement, so a re-apply rebuilds nothing.
    assert not (dropped & set(created))
    generation_first = [
        name for name in created
        if name != "autodedup_clusters_key_idx"
    ]
    for name in generation_first:
        assert "_gen_" in name or name.endswith("_gen_idx"), name


def test_the_pairs_backfill_reads_the_runs_ledger_and_never_guesses() -> None:
    code = _code()
    assert "from autodedup.runs r" in code
    assert "r.params ->> 'generation'" in code
    assert "r.mode = 'score'" in code and "r.status = 'success'" in code
    # The fallback chain ends in a literal, not in a NULL that `set not null` would reject.
    assert "'legacy'" in code
    # Compared as text: a cast would turn one malformed param into an error for the whole
    # statement, and `feature_version` is a jsonb string on one side.
    assert "p.feature_version::text" in code


def test_the_g4_rulings_are_backfilled_from_the_evidence_one_row_at_a_time() -> None:
    updates = re.findall(
        r"update autodedup\.verdicts set generation = 'g4', member_ids = "
        r"array\[([0-9,]+)\]::bigint\[\] where kind = 'cluster' and cluster_key = (\d+) "
        r"and member_ids is null"
        r" and decided_at < timestamptz '2026-09-19 00:00\+00';",
        _sql(),
    )
    # The operator's 224 cluster-grain confirmations taken on g4 groups.
    assert len(updates) == 224
    keys = [int(key) for _, key in updates]
    assert len(set(keys)) == len(keys), "a cluster key is backfilled twice"
    for ids_text, key in updates:
        ids = [int(value) for value in ids_text.split(",")]
        assert ids == sorted(ids), key
        assert len(set(ids)) == len(ids), key
        assert len(ids) >= 2, key
        # The cluster key IS the smallest listing id ever admitted to the group.
        assert ids[0] == int(key), key


def test_the_time_rule_only_stamps_a_pass_that_actually_holds_the_key() -> None:
    """Six rulings sit on g1 groups no later pass re-clustered; they stay NULL (legacy)."""
    code = _code()
    assert "r.finished_at <= v.decided_at" in code
    assert "order by r.finished_at desc" in code
    assert "and c.generation = era.generation" in code
    assert "v.kind = 'cluster' and v.generation is null" in code


def test_the_header_says_what_happens_to_the_surviving_g4_rows() -> None:
    header = _sql().split("set lock_timeout")[0].lower()
    assert "34 surviving g4 rows" in header
    assert "re-running the score lane" in header


def test_the_backfill_cannot_claim_a_ruling_taken_after_the_evidence() -> None:
    """The NULL guard alone is not idempotency here. The apply lands BEFORE the PR merges, so
    for a few minutes the old api still writes cluster rulings with `generation` and
    `member_ids` both NULL — rows a re-run (a lock-timeout retry, a replay) could not tell from
    September's. Every cluster ruling in the store was decided by 2026-09-18 14:17 UTC, so the
    backfill is bounded there as well as on NULL.
    """
    sql = _sql()
    bound = "decided_at < timestamptz '2026-09-19 00:00+00'"
    literals = [
        line for line in sql.splitlines()
        if line.startswith("update autodedup.verdicts set generation = 'g4'")
    ]
    assert len(literals) == 224
    for line in literals:
        assert bound in line, line
    # The era rule that stamps every OTHER cluster ruling is bounded by the same clock.
    era = sql.split("with era as (")[1].split("update autodedup.verdicts v")[0]
    assert bound.replace("decided_at", "v.decided_at") in era


def test_a_cluster_ruling_is_unique_PER_PASS_and_never_overwrites_another() -> None:
    """E58's write half. `autodedup_verdicts_cluster_uidx` was unique on
    (kind, cluster_key, decided_by), so ruling a g5 group UPDATEd the row holding the g4
    ruling — the same defect this file removes from `clusters`, on the one table with no
    history behind it. `coalesce`, because NULL never equals NULL in a unique index.
    """
    sql = _sql()
    created = sql.index(
        "create unique index if not exists autodedup_verdicts_cluster_gen_uidx"
    )
    dropped = sql.index("drop index if exists autodedup.autodedup_verdicts_cluster_uidx;")
    assert created < dropped, "the replacement must exist before the old one goes"
    stanza = sql[created:dropped]
    assert "(kind, cluster_key, (coalesce(generation, ''::text)), decided_by)" in stanza
    assert "where kind = 'cluster'" in stanza
    # A pair ruling is about two adverts and no pass owns it: its uniqueness is untouched.
    assert "autodedup_verdicts_pair_uidx" not in sql
