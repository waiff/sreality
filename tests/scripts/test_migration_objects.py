"""What the migration-object parser must and must not see.

Every case here is drawn from a real migration in this repo. The parser feeds a
check that can page the operator, so a false positive is as costly as a miss:
this repo's migrations open with long prose headers that quote the very DDL they
run, and mis-reading that prose would alarm on migrations that are perfectly
applied.
"""

from __future__ import annotations

from pathlib import Path

from scripts.migration_objects import load_migrations, parse_objects

_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def _idents(sql: str) -> set[str]:
    return {str(o) for o in parse_objects(sql)}


# --- what it must see ------------------------------------------------------


def test_reads_the_shapes_this_repo_actually_uses():
    sql = """
    CREATE TABLE IF NOT EXISTS public.foo (id bigserial primary key);
    create unlogged table bar (id int);
    CREATE MATERIALIZED VIEW public.baz AS SELECT 1;
    CREATE OR REPLACE VIEW qux AS SELECT 1;
    CREATE UNIQUE INDEX IF NOT EXISTS foo_idx ON public.foo (id);
    CREATE OR REPLACE FUNCTION public.do_thing(a int) RETURNS void AS $$ BEGIN END $$ LANGUAGE plpgsql;
    ALTER TABLE public.listings ADD COLUMN IF NOT EXISTS discovered_at timestamptz;
    ALTER TABLE listings ADD CONSTRAINT listings_check CHECK (true);
    CREATE POLICY tenant_read ON public.listings FOR SELECT USING (true);
    """
    assert _idents(sql) == {
        "relation:public.foo", "relation:bar", "relation:public.baz", "relation:qux",
        "relation:foo_idx", "function:public.do_thing",
        "column:listings.discovered_at", "constraint:listings.listings_check",
        "policy:listings.tenant_read",
    }


def test_sees_ddl_inside_a_do_block():
    """Migration 438 — the outage this check exists for — wraps its
    ALTER TABLE ... ADD CONSTRAINT in a lock-race-retrying do-block. A parser
    that treats every dollar-quoted region as a function body is blind to it."""
    objs = parse_objects((_MIGRATIONS / "438_listings_area_basis_check_plot.sql").read_text())
    assert {str(o) for o in objs} == {"constraint:listings.listings_area_basis_check"}


def test_sees_the_column_added_by_444():
    objs = parse_objects((_MIGRATIONS / "444_listings_discovered_at.sql").read_text())
    assert {str(o) for o in objs} == {"column:listings.discovered_at"}


# --- what it must NOT see --------------------------------------------------


def test_ignores_ddl_quoted_in_a_prose_header():
    """444's header discusses the ALTER it performs AND names first_seen_at and
    detail_queue_completions in prose. Only the executed statement counts."""
    sql = """
    -- This migration will ALTER TABLE public.properties ADD COLUMN nonsense text
    -- and CREATE TABLE public.imaginary, but only in this comment.
    /* CREATE FUNCTION public.also_imaginary() RETURNS void */
    ALTER TABLE public.listings ADD COLUMN IF NOT EXISTS real_one timestamptz;
    """
    assert _idents(sql) == {"column:listings.real_one"}


def test_ignores_ddl_inside_a_function_body():
    """A CREATE FUNCTION body's statements run when it is CALLED, not when the
    migration is applied — the opposite of a do-block."""
    sql = """
    CREATE OR REPLACE FUNCTION public.rebuild() RETURNS void AS $$
    BEGIN
      CREATE TEMP TABLE scratch AS SELECT 1;
      CREATE INDEX scratch_idx ON scratch (id);
    END $$ LANGUAGE plpgsql;
    """
    assert _idents(sql) == {"function:public.rebuild"}


def test_ignores_ddl_inside_a_string_literal():
    sql = """
    COMMENT ON TABLE public.listings IS 'see CREATE TABLE public.decoy for why';
    ALTER TABLE public.listings ADD COLUMN IF NOT EXISTS real_two int;
    """
    assert _idents(sql) == {"column:listings.real_two"}


def test_drops_and_grants_yield_nothing_rather_than_guessing():
    """A migration that only drops or grants declares no object to probe. It must
    fall into the reported `unverifiable` bucket, never into a false pass or a
    false alarm."""
    assert _idents("DROP TABLE IF EXISTS public.gone; REVOKE ALL ON public.x FROM anon;") == set()


# --- the loader ------------------------------------------------------------


def test_load_migrations_is_ordered_numerically_not_lexically():
    migs = load_migrations(_MIGRATIONS, newest=0)
    numbers = [m.number for m in migs]
    assert numbers == sorted(numbers)
    # Lexical sorting would put 100 before 99; numeric must not.
    assert numbers.index(99) < numbers.index(100)


def test_load_migrations_window_takes_the_newest():
    newest = load_migrations(_MIGRATIONS, newest=5)
    every = load_migrations(_MIGRATIONS, newest=0)
    assert [m.number for m in newest] == [m.number for m in every[-5:]]


def test_every_recent_migration_is_either_probeable_or_openly_unverifiable():
    """A parser that silently returns nothing for most files would render the
    whole check green while proving nothing. Hold the line: the great majority of
    recent migrations must declare at least one probeable object."""
    migs = load_migrations(_MIGRATIONS, newest=40)
    probeable = [m for m in migs if m.objects]
    assert len(probeable) >= 28, [m.filename for m in migs if not m.objects]
