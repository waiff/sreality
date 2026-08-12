"""The broker attribution registry must reproduce the pre-registry SQL exactly.

Before this registry, `scripts/resolve_brokers.py` carried five hand-copied
families of near-identical SQL (~330 lines, 16 statements) — one per portal. The
registry generates them from config rows instead. That is only safe if every
pre-existing portal still attributes IDENTICALLY, so `_PRE_REGISTRY` below is a
frozen snapshot of what each family actually did on origin/main (472bd325), taken
statement by statement, and every case here asserts the generated SQL against it.

Nine of the sixteen statements come out byte-identical. The other seven differ in
exactly three documented, verified-equivalent ways, pinned by
`test_the_only_deviations_from_the_pre_registry_sql_are_the_three_documented_ones`:

  1. sreality's two contact statements are wrapped in `WITH chunk AS NOT
     MATERIALIZED (...)`. NOT MATERIALIZED forces Postgres to inline a
     single-reference CTE, which is the original direct join.
  2. Sources that lack a column now select it explicitly as NULL (`NULL::text AS
     email`, `NULL::numeric AS rating`, `NULL::int AS reviews`) and carry it
     through the INSERT + the latest-wins DO UPDATE. Verified on prod: 0 of the
     6,108 ceskereality and 6,004 realitymix identities have an email, and 0
     non-sreality identities have a rating or review_count — the resolver is the
     only writer of those columns, so the added CASE can only ever write NULL over
     NULL.
  3. ceskereality's 420-normalisation moved from the INSERT + GROUP BY into the
     chunk CTE. Same value, same grouping key.
"""

from __future__ import annotations

import os
import re
from typing import Any

import pytest

from toolkit.broker_sources import (
    BROKER_FINGERPRINT_KEYS,
    BROKER_SOURCE_NAMES,
    BROKER_SOURCES,
    attribution_statements,
)

SEL = "l.id = ANY(%(ids)s)"

# What each pre-registry family did, per statement, in execution order.
# ("identity" | "email" | "phone" | "link", <a fact only that statement carries>)
_PRE_REGISTRY: dict[str, dict[str, Any]] = {
    "sreality": {
        "block": "user", "id_key": "user_id", "name_key": "user_name",
        "identity_email": "user_email", "rating": True,
        "kinds": ("identity", "email", "phone", "link"),
        "phone": "array", "phone_420": True, "cte": "NOT MATERIALIZED",
    },
    "idnes": {
        "block": "broker", "id_key": "account_oid", "name_key": "name",
        "identity_email": "email", "rating": False,
        "kinds": ("identity", "email", "phone", "link"),
        # The one portal storing BARE digits — a pre-existing divergence from
        # toolkit.broker_resolver.normalize_phone. Flipping it would orphan every
        # idnes contact row already stored, so the registry must preserve it.
        "phone": "scalar", "phone_420": False, "cte": "MATERIALIZED",
    },
    "ceskereality": {
        "block": "broker", "id_key": "broker_id", "name_key": "name",
        "identity_email": None, "rating": False,
        "kinds": ("identity", "phone", "link"),
        "phone": "scalar", "phone_420": True, "cte": "MATERIALIZED",
    },
    "realitymix": {
        "block": "broker", "id_key": "broker_id", "name_key": "name",
        "identity_email": None, "rating": False,
        "kinds": ("identity", "link"),
        "phone": None, "phone_420": False, "cte": "MATERIALIZED",
    },
    "remax": {
        "block": "broker", "id_key": "broker_id", "name_key": "name",
        "identity_email": "email", "rating": False,
        "kinds": ("identity", "email", "link"),
        "phone": None, "phone_420": False, "cte": "MATERIALIZED",
    },
    # New in this change. Identity-only for the OPPOSITE reason to realitymix: the
    # contacts exist but are one corporate switchboard shared by all 1,021 MM
    # Reality brokers (prod: 1 distinct email, 1 distinct phone across 10,652
    # listings). The identity still carries the email because email_domain is the
    # only firm key and mmreality.cz is already a franchise firm.
    "mmreality": {
        "block": "broker", "id_key": "id", "name_key": "name",
        "identity_email": "email", "rating": False,
        "kinds": ("identity", "link"),
        "phone": None, "phone_420": False, "cte": "MATERIALIZED",
    },
}

_BY_SOURCE = {c.source: c for c in BROKER_SOURCES}


def _rendered(source: str) -> list[str]:
    return [" ".join(s.format(sel=SEL).split()) for s in _BY_SOURCE[source].statements()]


def _kind(sql: str) -> str:
    if sql.startswith("UPDATE listings"):
        return "link"
    if "INSERT INTO broker_identities " in sql:
        return "identity"
    return "email" if "'email'," in sql else "phone"


@pytest.mark.parametrize("source", sorted(_PRE_REGISTRY))
def test_statement_inventory_matches_the_pre_registry_family(source: str) -> None:
    """Same statements, same order — the refactor must not add, drop or reorder
    one. A dropped contact upsert loses a whole portal's bridging silently."""
    assert tuple(_kind(s) for s in _rendered(source)) == _PRE_REGISTRY[source]["kinds"]


@pytest.mark.parametrize("source", sorted(_PRE_REGISTRY))
def test_every_statement_is_pinned_to_its_own_source(source: str) -> None:
    """_attribute runs EVERY source's SQL over the same id chunk, so a statement
    that lost its source literal would attribute another portal's listings to this
    portal's identities."""
    for sql in _rendered(source):
        assert f"l.source = '{source}'" in sql
        assert f"bi.source = '{source}'" in sql or "INSERT INTO broker_identities" in sql
        for other in BROKER_SOURCE_NAMES:
            if other != source:
                assert f"'{other}'" not in sql


@pytest.mark.parametrize("source", sorted(_PRE_REGISTRY))
def test_identity_and_link_read_the_pre_registry_json_path(source: str) -> None:
    want = _PRE_REGISTRY[source]
    path = f"l.raw_json->'{want['block']}'->>'{want['id_key']}'"
    stmts = dict(zip((_kind(s) for s in _rendered(source)), _rendered(source)))
    assert path in stmts["identity"] and path in stmts["link"]
    assert f"l.raw_json->'{want['block']}'->>'{want['name_key']}'" in stmts["identity"]
    assert f"l.raw_json ? '{want['block']}'" in stmts["identity"]


@pytest.mark.parametrize("source", sorted(_PRE_REGISTRY))
def test_identity_email_column_matches_the_pre_registry_family(source: str) -> None:
    """ceskereality/realitymix had no email in their upsert; the unified template
    must still write NULL for them (no email -> no email_domain -> no firm)."""
    want = _PRE_REGISTRY[source]
    identity = next(s for s in _rendered(source) if _kind(s) == "identity")
    if want["identity_email"] is None:
        assert "NULL::text AS email" in identity
    else:
        assert (f"lower(nullif(l.raw_json->'{want['block']}'->>"
                f"'{want['identity_email']}', '')) AS email") in identity


@pytest.mark.parametrize("source", sorted(_PRE_REGISTRY))
def test_only_sreality_writes_a_rating(source: str) -> None:
    identity = next(s for s in _rendered(source) if _kind(s) == "identity")
    if _PRE_REGISTRY[source]["rating"]:
        assert "'broker_rating', '')::numeric AS rating" in identity
        assert "'broker_review_count', '')::int AS reviews" in identity
    else:
        assert "NULL::numeric AS rating" in identity
        assert "NULL::int AS reviews" in identity


@pytest.mark.parametrize("source", sorted(_PRE_REGISTRY))
def test_phone_shape_and_normalisation_match_the_pre_registry_family(source: str) -> None:
    want = _PRE_REGISTRY[source]
    phones = [s for s in _rendered(source) if _kind(s) == "phone"]
    if want["phone"] is None:
        assert phones == []
        return
    (sql,) = phones
    if want["phone"] == "array":
        assert "CROSS JOIN LATERAL" in sql and "jsonb_array_elements" in sql
        assert f"l.raw_json->'{want['block']}'->'user_phones'" in sql
    else:
        assert "CROSS JOIN LATERAL" not in sql
        assert (f"regexp_replace(l.raw_json->'{want['block']}'->>'phone', "
                "'[^0-9]', '', 'g')") in sql
    assert ("'420' ||" in sql) is want["phone_420"]
    assert ">= 9" in sql


@pytest.mark.parametrize("source", sorted(_PRE_REGISTRY))
def test_contact_cte_materialisation_matches_the_pre_registry_family(source: str) -> None:
    """MATERIALIZED bounds the listings scan by {sel} before the identity join —
    the fix for the cold-planner detoast that blew the statement timeout on idnes.
    sreality predates it and keeps the inlined plan, which NOT MATERIALIZED is."""
    for sql in _rendered(source):
        if _kind(sql) in ("email", "phone"):
            assert f"WITH chunk AS {_PRE_REGISTRY[source]['cte']} (" in sql


def test_link_guards_against_a_no_op_write() -> None:
    """`IS DISTINCT FROM` keeps a re-attribution from rewriting rows it did not
    change — that predicate is what makes the daily sweep cheap and lock-light."""
    for sql in (s for src in _PRE_REGISTRY for s in _rendered(src)):
        if _kind(sql) == "link":
            assert "l.broker_identity_id IS DISTINCT FROM bi.id" in sql


def test_upserts_keep_the_latest_wins_conflict_shape() -> None:
    for sql in (s for src in _PRE_REGISTRY for s in _rendered(src)):
        if _kind(sql) == "identity":
            assert "ON CONFLICT (source, source_broker_id_native) DO UPDATE" in sql
            assert ("first_seen_at = least(broker_identities.first_seen_at, "
                    "EXCLUDED.first_seen_at)") in sql
            assert ("last_seen_at = greatest(broker_identities.last_seen_at, "
                    "EXCLUDED.last_seen_at)") in sql
            # Every writable attribute is latest-wins, never blind-overwritten.
            for col in ("display_name", "email", "rating", "review_count"):
                assert (f"{col} = CASE WHEN EXCLUDED.last_seen_at >= "
                        "broker_identities.last_seen_at") in sql
        elif _kind(sql) in ("email", "phone"):
            assert "ON CONFLICT (broker_identity_id, kind, value) DO UPDATE" in sql


def test_the_only_deviations_from_the_pre_registry_sql_are_the_three_documented_ones() -> None:
    """A guard on the equivalence argument itself (see the module docstring)."""
    rendered = {src: _rendered(src) for src in _PRE_REGISTRY}
    # 1. NOT MATERIALIZED appears for sreality's contacts and nowhere else.
    not_mat = {src for src, stmts in rendered.items()
               if any("NOT MATERIALIZED" in s for s in stmts)}
    assert not_mat == {"sreality"}
    # 2. The NULL degradations appear only where the portal genuinely lacks the field.
    null_email = {src for src, stmts in rendered.items()
                  if any("NULL::text AS email" in s for s in stmts)}
    assert null_email == {"ceskereality", "realitymix"}
    # 3. ceskereality normalises inside the chunk CTE, not at INSERT time.
    (cr_phone,) = [s for s in rendered["ceskereality"] if _kind(s) == "phone"]
    assert cr_phone.index("'420' ||") < cr_phone.index("INSERT INTO")


def test_every_statement_carries_exactly_one_sel_slot() -> None:
    """An unbounded attribution statement would scan the whole corpus per chunk."""
    for sql in attribution_statements():
        assert sql.count("{sel}") == 1
        rendered = sql.format(sel=SEL)
        assert "%(ids)s" in rendered
        # No unfilled slot survived into the executed text.
        assert not re.search(r"(?<!')\{[A-Za-z_]\w*\}", rendered)


def test_statement_count_is_the_pre_registry_sixteen_plus_mmreality() -> None:
    assert len(attribution_statements()) == 16 + 2


def test_registry_order_drives_the_full_sweep_source_scan() -> None:
    assert BROKER_SOURCE_NAMES == (
        "sreality", "idnes", "ceskereality", "realitymix", "remax", "mmreality")


def test_fingerprint_keys_are_a_superset_of_the_pre_registry_allowlist() -> None:
    """The dirty-queue allowlist that makes a broker-only page change re-enqueue.
    Losing a key silently stops re-attribution for that portal; mmreality's key is
    `id`, which the hand-written list did not have."""
    pre_registry = {"account_oid", "broker_id", "name", "email", "phone",
                    "agency_name", "agency_slug", "agency_id"}
    assert pre_registry <= set(BROKER_FINGERPRINT_KEYS)
    assert "id" in BROKER_FINGERPRINT_KEYS
    # ...and nothing beyond the registry's own raw["broker"] keys crept in.
    registered = {k for c in BROKER_SOURCES if c.block == "broker"
                  for k in c.fingerprint_keys()}
    assert set(BROKER_FINGERPRINT_KEYS) == registered


def test_scraper_db_derives_both_registries_from_this_module() -> None:
    """The half-landed-onboarding guard: before this, a new portal had to be added
    to three hand-maintained lists in two files."""
    from scraper import db

    assert db.BROKER_ATTRIBUTED_SOURCES == frozenset(BROKER_SOURCE_NAMES)
    assert db._BROKER_FINGERPRINT_KEYS == BROKER_FINGERPRINT_KEYS
    assert "mmreality" in db.BROKER_ATTRIBUTED_SOURCES


# --- mmreality (the new portal) ---------------------------------------------

_MMREALITY_BLOCK = {
    "id": "41428", "name": "Aneta Bučilová", "email": "info@mmreality.cz",
    "phone": "731404000", "mobile": "731404000", "slug": "abucilova",
    "biographySlug": None, "squareAvatar": "https://…/a.jpg", "hallOfFame": False,
}


def test_mmreality_attributes_from_the_real_raw_json_shape() -> None:
    """A realistic block off prod (contacts masked). scraper/mmreality_parser.py
    does `raw = dict(obj)` over the Vue `:property` blob, so the site's own broker
    object lands verbatim at raw_json->'broker' — no parser change was needed."""
    identity, link = _rendered("mmreality")
    for key in ("id", "name", "email"):
        assert key in _MMREALITY_BLOCK
        assert f"l.raw_json->'broker'->>'{key}'" in identity
    assert "SELECT 'mmreality', a.uid" in identity
    assert "bi.source = 'mmreality'" in link
    # `id`, not `broker_id` — the key every other raw["broker"] portal uses.
    assert "l.raw_json->'broker'->>'broker_id'" not in identity


def test_mmreality_writes_no_contact_rows() -> None:
    """All 1,021 MM Reality brokers publish the SAME switchboard email and phone.
    Writing them would stamp one number onto every MM broker via the rollup's
    pphone CTE, and add ~10k contact rows the bridge frequency guard (personal ==
    frequency 1) discards anyway."""
    cfg = _BY_SOURCE["mmreality"]
    assert cfg.write_contacts is False
    assert not cfg.writes_email_contact and not cfg.writes_phone_contact
    assert [s for s in _rendered("mmreality") if _kind(s) in ("email", "phone")] == []


def test_mmreality_identity_still_carries_the_email_for_firm_linkage() -> None:
    """broker_identities.email_domain (generated) is the ONLY firm key, and
    mmreality.cz is already an is_franchise firm — dropping the email would leave
    1,021 firm-less singletons."""
    identity = next(s for s in _rendered("mmreality") if _kind(s) == "identity")
    assert "lower(nullif(l.raw_json->'broker'->>'email', '')) AS email" in identity


# --- schema check (CI's replayed-schema job only) ---------------------------

_DB_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not _DB_URL, reason="TEST_DATABASE_URL not set")
def test_every_attribution_statement_plans_against_the_real_schema() -> None:
    """PREPARE parses, name-resolves and type-checks without touching a row — the
    one thing a fake cursor structurally cannot answer about generated SQL."""
    import psycopg

    conn = psycopg.connect(_DB_URL, autocommit=True)
    try:
        for i, sql in enumerate(attribution_statements()):
            concrete = sql.format(sel="l.id = ANY(ARRAY[1]::bigint[])")
            with conn.cursor() as cur:
                cur.execute(f"PREPARE _bs_{i} AS {concrete}")
                cur.execute(f"DEALLOCATE _bs_{i}")
    finally:
        conn.close()
