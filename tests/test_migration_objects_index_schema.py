"""The apply receipt probes objects with to_regclass on the default search_path, so an
index created on a schema-qualified table must be reported under that schema."""

from scripts.migration_objects import parse_objects


def _relations(sql: str) -> set[str]:
    return {o.ident for o in parse_objects(sql) if o.kind == "relation"}


def test_index_on_schema_qualified_table_is_qualified_with_that_schema() -> None:
    sql = "create index if not exists runs_started_idx on autodedup.runs (started_at desc);"
    assert "autodedup.runs_started_idx" in _relations(sql)


def test_index_on_public_table_stays_bare() -> None:
    sql = "create unique index concurrently foo_idx on only listings (id);"
    assert "foo_idx" in _relations(sql)


def test_explicitly_qualified_index_name_is_kept() -> None:
    sql = "create index dedup_sim.x_idx on dedup_sim.t (a);"
    assert "dedup_sim.x_idx" in _relations(sql)


def test_index_on_explicit_public_table_stays_bare() -> None:
    sql = "create unique index if not exists foo_idx on public.foo (id);"
    assert "foo_idx" in _relations(sql)
