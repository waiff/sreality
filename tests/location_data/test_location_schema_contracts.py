"""Offline contract gate for the location-data W1 schema (migrations 380-384).

Implements the CI checks of `01-schema.md` appendix A.2 as STATIC checks over the
migration SQL text — no database connection, so they run in the normal pytest job
and fail a PR the moment the schema drifts from the design corpus.

Which A.2 check each test covers:

  A.2 #2  every literal compared against a location enum is a member of it
          -> test_enum_types_carry_the_canonical_vocabulary
             test_enum_casts_reference_declared_members
             test_granularity_rank_seeds_every_label_in_declaration_order
             test_level_granularity_seeds_every_ruian_level
             test_seed_literals_are_enum_members
  A.2 #4  no source file emits the string `portal_json`
          -> test_no_source_emits_portal_json
  A.2 #6  a new location_granularity value also touches location_granularity_rank
          -> test_granularity_rank_seeds_every_label_in_declaration_order
  A.2 #8  `pin_collision_class IS [NOT] NULL` appears nowhere
          -> test_pin_collision_class_is_never_null_tested
             test_pin_collision_class_vocabulary_is_not_null_default_normal
  01 0.4  enum ordinality never enters an index predicate, a CHECK or a stored
          generated column
          -> test_no_enum_ordinality_in_ddl
  D3/05 P5 all four axes plus blur_evidence and radius_semantics are NOT NULL on
          both serving projections (a NULL reads as "no gate" and fails open)
          -> test_projections_declare_every_axis_not_null
  00 6.1  the three-artifact licensing guard ships whole
          -> test_licence_guard_ships_all_three_artifacts
  00 10.3 collision_epoch_id is inside the resolution's unique key
          -> test_collision_epoch_is_in_the_resolution_identity
  00 8    dispositions key on the version-free dedupe_key, not contradiction_id
          -> test_dispositions_key_on_dedupe_key

Plus the project's own rule, which the design assumes but does not state: this
Supabase project auto-GRANTs anon/authenticated on new tables, sequences AND
functions, so every object these migrations create must be explicitly revoked
(test_every_created_object_is_revoked).
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_migration_rls_grants import _statements, _strip_comments

_ROOT = Path(__file__).resolve().parent.parent.parent
_MIGRATIONS_DIR = _ROOT / "migrations"
_W1_GLOB = "38[0-4]_location_w1_*.sql"
# Trees whose SQL/Python could compare a literal against a location enum.
_SOURCE_DIRS = ("scraper", "toolkit", "api", "scripts", "migrations")


def _w1_files() -> list[Path]:
    return sorted(_MIGRATIONS_DIR.glob(_W1_GLOB))


def _w1_sql() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _w1_files())


def _clean() -> str:
    """The W1 migration corpus, comments stripped and lowercased."""
    return _strip_comments(_w1_sql()).lower()


# --------------------------------------------------------------------------
# Canonical vocabularies. Transcribed from 01-schema.md sections 2 and 4.1,
# which 00-shared-contracts.md sections 1-6 confirm as the tie-breaker. These
# literal sets ARE the contract: a label added, dropped or respelled in a
# migration without the design changing first fails here.
# --------------------------------------------------------------------------

CANONICAL_ENUMS: dict[str, tuple[str, ...]] = {
    # ORDINAL, declared coarse -> fine.
    "location_granularity": (
        "unknown", "country", "kraj", "okres", "obec", "cast_obce_or_quarter",
        "street", "street_segment", "parcel", "building", "address_point",
    ),
    "position_source": (
        "none", "admin_centroid", "derived_geocode", "carried_forward",
        "portal_pin_blurred", "portal_pin", "registry_point",
    ),
    "blur_evidence": ("none", "declared", "detected", "both"),
    "match_confidence": ("low", "medium", "high", "exact"),
    "radius_semantics": ("r95_empirical", "geometric_bound", "declared"),
    "resolution_status": ("resolved", "ambiguous", "unmatched", "no_input", "skipped_foreign"),
    "country_status": ("cz", "foreign", "disputed", "undetermined"),
    "country_determination_method": (
        "portal_field", "registry_containment", "portal_bucket", "text_claim",
        "classifier", "assumed_default", "unknown",
    ),
    "admin_assignment_method": (
        "registry", "pip_containment", "pip_nearest_within_n_m", "unresolved_sliver",
        "outside_country", "claimed", "unresolved",
    ),
    "licence_class": (
        "portal", "cc_by_ruian", "odbl", "commercial_permanent",
        "ephemeral_display_only", "operator",
    ),
    "ruian_level": (
        "stat", "region_soudrznosti", "kraj", "okres", "orp", "pou", "obec",
        "spravni_obvod", "momc", "cast_obce", "katastralni_uzemi", "zsj", "ulice",
        "adresni_misto", "stavebni_objekt", "parcela",
    ),
    "location_claim_type": (
        "coordinate", "uncertainty_geometry", "precision_declaration", "blur_hint",
        "map_zoom", "geohash", "admin_polygon",
        "address_point_id", "building_id", "obec_code", "portal_admin_id",
        "portal_street_id", "osm_relation_id", "cadastral_territory_name",
        "cadastral_territory_code", "parcel_number",
        "street_name", "house_number_cp", "house_number_co", "evidencni", "house_unit",
        "psc", "postal_town", "obec_name", "cast_obce_name", "quarter_name",
        "mestsky_obvod_name", "okres_name", "orp_name", "kraj_name", "country",
        "homonym_qualifier", "address_line_verbatim",
        "development_name", "landmark", "relative_distance", "poi_distance",
        "micro_position", "neighbour_listing_ref", "foreign_indicator",
    ),
    "location_claim_surface": (
        "api_json", "graphql", "embedded_json", "html_selector", "map_config",
        "og_meta", "jsonld", "url_slug", "description", "archived_html",
        "legacy_column", "registry", "operator_input",
    ),
    "location_page_kind": (
        "index", "detail", "map", "gazetteer", "snapshot", "archive", "none",
    ),
    "location_extraction_method": (
        "portal_structured_field", "portal_declared_quality", "html_selector_parse",
        "url_slug_parse", "breadcrumb_parse", "jsonld_parse", "map_widget_parse",
        "regex_text", "llm_text", "legacy_column", "registry_derived", "operator_manual",
    ),
}

# The six values pin_clusters.classification declares and
# listing_location_current.pin_collision_class carries VERBATIM. One vocabulary,
# never NULL (00 section 10.2).
PIN_COLLISION_CLASSES = (
    "normal", "legitimate_multiunit", "building_1_to_many", "town_centroid_suspect",
    "parser_collapse_suspect", "foreign_resort_centroid",
)

# Ordinal enums: comparing these with </>/<=/>= is legal in a QUERY and illegal
# in an index predicate, a CHECK or a stored generated column (01 section 0.4).
ORDINAL_ENUMS = ("location_granularity", "match_confidence")


# --------------------------------------------------------------------------
# Tiny SQL readers. Deliberately text-level: this file must never need a DB.
# --------------------------------------------------------------------------

def _balanced(text: str, open_idx: int) -> str:
    """Body between the parens starting at `open_idx`, quotes respected."""
    assert text[open_idx] == "("
    depth, i, n = 0, open_idx, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            i += 1
            while i < n:
                if text[i:i + 2] == "''":
                    i += 2
                    continue
                if text[i] == "'":
                    break
                i += 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
        i += 1
    raise AssertionError("unbalanced parentheses in migration SQL")


def _split_top_level(body: str, sep: str = ",") -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    depth, i, n = 0, 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < n:
                if body[i:i + 2] == "''":
                    buf.append("''")
                    i += 2
                    continue
                buf.append(body[i])
                if body[i] == "'":
                    i += 1
                    break
                i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _table_body(sql: str, table: str) -> str:
    m = re.search(rf"create table {re.escape(table)}\s*\(", sql)
    assert m, f"migrations 380-384 do not create table {table}"
    return _balanced(sql, m.end() - 1)


def _column_defs(body: str) -> list[str]:
    return [
        frag for frag in _split_top_level(body)
        if not frag.startswith(("constraint ", "primary key", "unique ", "check ", "foreign key"))
    ]


def _declared_enums(sql: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"create type ([a-z0-9_]+) as enum\s*\(", sql):
        body = _balanced(sql, m.end() - 1)
        out[m.group(1)] = re.findall(r"'([^']*)'", body)
    return out


def _values_rows(sql: str, insert_into: str) -> list[list[str]]:
    """Per-row token lists for `insert into <table> ... values (...), (...);`."""
    stmt = next(
        (s for s in _statements(sql)
         if re.match(rf"\s*insert into {re.escape(insert_into)}\b", s.lower())),
        None,
    )
    assert stmt, f"no seed INSERT found for {insert_into}"
    low = stmt.lower()
    kw = list(re.finditer(r"\bvalues\b", low))
    assert kw, f"seed INSERT for {insert_into} has no VALUES list"
    rows: list[list[str]] = []
    i = kw[-1].end()
    while True:
        j = low.find("(", i)
        if j == -1:
            break
        row = _balanced(low, j)
        rows.append([tok.strip() for tok in _split_top_level(row)])
        i = j + len(row) + 2
    return rows


def _unquote(token: str) -> str | None:
    m = re.fullmatch(r"'([^']*)'(?:::[a-z0-9_\[\]]+)?", token.strip())
    return m.group(1) if m else None


def _scan_sources(pattern: re.Pattern[str]) -> list[str]:
    """`<path>: <match>` for every hit of `pattern` in the backend's .sql/.py
    trees. SQL comments are stripped first, so a design note that NAMES a
    forbidden form (as these migrations do, deliberately) is not a hit."""
    hits: list[str] = []
    for directory in _SOURCE_DIRS:
        root = _ROOT / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix not in (".sql", ".py") or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if path.suffix == ".sql":
                text = _strip_comments(text)
            for m in pattern.finditer(text):
                hits.append(f"{path.relative_to(_ROOT)}: {m.group(0).strip()}")
    return hits


def _ddl_predicate_contexts(sql: str) -> list[tuple[str, str]]:
    """(label, expression) for every CHECK body, index WHERE predicate and stored
    generated expression in the corpus — the three places 01 section 0.4 forbids
    an ordinal enum comparison."""
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"\bcheck\s*\(", sql):
        out.append(("check", _balanced(sql, m.end() - 1)))
    for m in re.finditer(r"\bgenerated always as\s*\(", sql):
        out.append(("generated", _balanced(sql, m.end() - 1)))
    for stmt in _statements(sql):
        low = stmt.lower().strip()
        if not low.startswith("create ") or " index " not in low:
            continue
        parts = re.split(r"\bwhere\b", low)
        if len(parts) > 1:
            out.append(("index predicate", parts[-1]))
    return out


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_the_five_w1_migrations_exist():
    names = [p.name for p in _w1_files()]
    assert names == [
        "380_location_w1_enums_and_config.sql",
        "381_location_w1_ruian_mirror.sql",
        "382_location_w1_claims.sql",
        "383_location_w1_resolutions.sql",
        "384_location_w1_serving.sql",
    ], f"unexpected W1 migration set: {names}"


def test_enum_types_carry_the_canonical_vocabulary():
    """A.2 #2's precondition: the enums themselves must match the design corpus,
    label for label AND (for the ordinal ones) in declaration order."""
    declared = _declared_enums(_clean())
    missing = sorted(set(CANONICAL_ENUMS) - set(declared))
    assert not missing, f"location enum type(s) never declared in migrations 380-384: {missing}"
    drift = {
        name: {"declared": declared[name], "canonical": list(labels)}
        for name, labels in CANONICAL_ENUMS.items()
        if declared[name] != list(labels)
    }
    assert not drift, (
        "enum label set or ORDER drifted from 01-schema.md sections 2/4.1. Order is "
        "load-bearing for location_granularity and match_confidence (Postgres compares "
        "enums by declaration order):\n" + "\n".join(
            f"  {n}: declared={d['declared']} canonical={d['canonical']}" for n, d in drift.items())
    )


def test_enum_casts_reference_declared_members():
    """A.2 #2: every `'literal'::<location enum>` (including array casts) in the
    migrations is a member of that enum."""
    sql = _clean()
    declared = _declared_enums(sql)
    offenders: list[str] = []
    for enum_name, labels in declared.items():
        if enum_name not in CANONICAL_ENUMS:
            continue
        for m in re.finditer(rf"'([^']*)'::{enum_name}\b", sql):
            if m.group(1) not in labels:
                offenders.append(f"'{m.group(1)}'::{enum_name}")
        for m in re.finditer(rf"array\[([^\]]*)\]::{enum_name}\[\]", sql):
            for lit in re.findall(r"'([^']*)'", m.group(1)):
                if lit not in labels:
                    offenders.append(f"'{lit}' in an array[]::{enum_name}[]")
    assert not offenders, (
        "literal(s) cast to a location enum that is not a member of it "
        f"(01 section A.2 check 2): {sorted(set(offenders))}"
    )


def test_granularity_rank_seeds_every_label_in_declaration_order():
    """A.2 #6 / 01 section 0.4: rank() is the ONLY legal way to persist a
    granularity comparison, so every enum label needs a rank row and the ranks
    must preserve the enum's coarse->fine order."""
    sql = _clean()
    rows = _values_rows(sql, "location_granularity_rank")
    seeded = [(_unquote(r[0]), int(r[1])) for r in rows]
    labels = [lbl for lbl, _ in seeded]
    canonical = list(CANONICAL_ENUMS["location_granularity"])
    assert labels == canonical, (
        "location_granularity_rank must carry exactly one row per enum label, in "
        f"declaration order. seeded={labels} canonical={canonical}"
    )
    ranks = [rank for _, rank in seeded]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks), (
        f"ranks must be strictly increasing coarse->fine and unique, got {ranks}"
    )


def test_level_granularity_seeds_every_ruian_level():
    sql = _clean()
    rows = _values_rows(sql, "location_level_granularity")
    levels = [_unquote(r[0]) for r in rows]
    grains = [_unquote(r[1]) for r in rows]
    assert sorted(levels) == sorted(CANONICAL_ENUMS["ruian_level"]), (
        "location_level_granularity must map every ruian_level exactly once "
        f"(ORP/POU/MOMC/ZSJ/katastr have no D3 slot of their own): got {levels}"
    )
    bad = [g for g in grains if g not in CANONICAL_ENUMS["location_granularity"]]
    assert not bad, f"non-member location_granularity literal(s) in the seed: {bad}"


def test_seed_literals_are_enum_members():
    """A.2 #2 over the two remaining closed-vocabulary seeds that use bare
    literals rather than casts: location_claim_type_meta's flag sets and
    location_uncertainty_policy's (position_source, granularity, semantics)."""
    sql = _clean()
    claim_types = set(CANONICAL_ENUMS["location_claim_type"])
    offenders: list[str] = []
    for m in re.finditer(r"update location_claim_type_meta.*?where claim_type in\s*\(", sql, re.S):
        for lit in re.findall(r"'([^']*)'", _balanced(sql, m.end() - 1)):
            if lit not in claim_types:
                offenders.append(f"location_claim_type_meta seed: '{lit}'")
    for m in re.finditer(r"update location_claim_type_meta.*?where claim_type = '([^']*)'", sql, re.S):
        if m.group(1) not in claim_types:
            offenders.append(f"location_claim_type_meta seed: '{m.group(1)}'")

    for row in _values_rows(sql, "location_uncertainty_policy"):
        checks = (
            (row[1], "position_source"),
            (row[2], "location_granularity"),
            (row[5], "radius_semantics"),
        )
        for token, enum_name in checks:
            lit = _unquote(token)
            if lit is None or lit not in CANONICAL_ENUMS[enum_name]:
                offenders.append(f"location_uncertainty_policy seed: {token} is not a {enum_name}")
    assert not offenders, "non-member enum literal(s) in a seed: " + "; ".join(sorted(set(offenders)))


def test_no_enum_ordinality_in_ddl():
    """01 section 0.4: Postgres neither recomputes stored generated columns nor
    re-evaluates existing partial-index predicates when an enum gains a value, so
    an ordinal comparison in a CHECK / index predicate / generated expression is
    silently invalidated the day a rung is inserted. Persisted comparisons go
    through location_granularity_rank instead."""
    sql = _clean()
    ordinal_members = {
        lit for name in ORDINAL_ENUMS for lit in CANONICAL_ENUMS[name]
    }
    col_cmp = re.compile(r"\b(granularity|match_confidence)\s*(<=|>=|<|>)(?!=)")
    lit_cmp = re.compile(r"(<=|>=|<|>)\s*'([a-z0-9_]+)'")
    offenders: list[str] = []
    for label, expr in _ddl_predicate_contexts(sql):
        flat = re.sub(r"\s+", " ", expr).strip()
        if col_cmp.search(expr):
            offenders.append(f"{label}: {flat}")
            continue
        for m in lit_cmp.finditer(expr):
            if m.group(2) in ordinal_members:
                offenders.append(f"{label}: {flat}")
                break
    assert not offenders, (
        "ordinal enum comparison inside a CHECK, an index predicate or a stored "
        "generated column (01 section 0.4). Compare "
        "location_granularity_rank.rank instead:\n  " + "\n  ".join(sorted(set(offenders)))
    )


def test_projections_declare_every_axis_not_null():
    """D3 / 05 P5: a NULL axis reads as 'no gate' and fails open — a NULL
    uncertainty_radius_m makes both branches of the three-valued containment test
    evaluate NULL, so the row silently drops out of `certain` AND `possible`."""
    sql = _clean()
    axes = (
        "granularity", "position_source", "match_confidence",
        "uncertainty_radius_m", "blur_evidence", "radius_semantics",
    )
    offenders: list[str] = []
    for table in ("listing_location_current", "property_location_current"):
        defs = _column_defs(_table_body(sql, table))
        by_name = {d.split(None, 1)[0]: d for d in defs if d.split(None, 1)}
        for axis in axes:
            col = by_name.get(axis)
            if col is None:
                offenders.append(f"{table}.{axis} is missing")
            elif "not null" not in col:
                offenders.append(f"{table}.{axis} is nullable")
    assert not offenders, (
        "serving projection axis column(s) not NOT NULL:\n  " + "\n  ".join(offenders)
    )


def test_pin_collision_class_vocabulary_is_not_null_default_normal():
    """00 section 10.2: ONE vocabulary, carried verbatim from
    pin_clusters.classification, NOT NULL DEFAULT 'normal'. The producer-less
    `agency_pin` is dropped."""
    sql = _clean()
    col = next(
        (d for d in _column_defs(_table_body(sql, "listing_location_current"))
         if d.startswith("pin_collision_class")),
        None,
    )
    assert col, "listing_location_current has no pin_collision_class column"
    assert "not null" in col and "default 'normal'" in col, (
        "pin_collision_class must be NOT NULL DEFAULT 'normal' — an unclustered "
        f"listing is 'normal', never NULL. got: {col}"
    )
    for table, column in (
        ("listing_location_current", "pin_collision_class"),
        ("pin_clusters", "classification"),
    ):
        defn = next(
            (d for d in _column_defs(_table_body(sql, table)) if d.startswith(column)), "")
        got = set(re.findall(r"'([a-z0-9_]+)'", defn)) - {"normal"} | {"normal"}
        assert got == set(PIN_COLLISION_CLASSES), (
            f"{table}.{column} vocabulary drifted from the canonical six values: "
            f"{sorted(got)}"
        )


def test_pin_collision_class_is_never_null_tested():
    """A.2 #8: `pin_collision_class IS NULL` is ALWAYS the class-vocabulary bug —
    the column has no NULL member, so the old geo_blockable predicate could never
    fire. Scanned across the whole backend, not only these migrations."""
    offenders = _scan_sources(re.compile(r"pin_collision_class\s+is\s+(not\s+)?null", re.I))
    assert not offenders, (
        "forbidden `pin_collision_class IS [NOT] NULL` test (01 section A.2 check 8). "
        "Use the class-aware predicate: pin_collision_class IN "
        "('normal','building_1_to_many'):\n  " + "\n  ".join(offenders)
    )


def test_no_source_emits_portal_json():
    """A.2 #4: `portal_json` is a member of no enum — the migration emits the
    specific surface (sreality -> api_json, bezrealitky -> graphql, mmreality ->
    embedded_json)."""
    offenders = _scan_sources(re.compile(r"portal_json"))
    assert not offenders, f"file(s) emit the non-member literal `portal_json`: {offenders}"


def test_licence_guard_ships_all_three_artifacts():
    """00 section 6.1: the structural guard is three artifacts, all required — a
    CHECK on each projection, a CHECK on the resolution (so a non-storable
    coordinate can never become a winner), and the partial index that makes the
    affected set one indexed predicate away, permanently."""
    sql = _clean()
    for name, table in (
        ("llc_licence", "listing_location_current"),
        ("plc_licence", "property_location_current"),
        ("loc_res_licence", "location_resolutions"),
    ):
        assert re.search(
            rf"constraint {name} check \(\s*position_licence_class <> 'ephemeral_display_only'\s*\)",
            sql,
        ), f"missing or altered {name} CHECK on {table}"
    assert re.search(
        r"create index location_claims_ephemeral on location_claims \(source, first_observed_at\)\s*"
        r"where licence_class = 'ephemeral_display_only'",
        sql,
    ), "missing the location_claims_ephemeral partial index"


def test_collision_epoch_is_in_the_resolution_identity():
    """00 section 10.3: without the epoch id inside the unique key, a collision
    recompute cannot invalidate the resolutions that consumed the old
    classification."""
    body = _table_body(_clean(), "location_resolutions")
    uniques = [f for f in _split_top_level(body) if f.startswith("unique (")]
    assert uniques, "location_resolutions declares no UNIQUE key"
    key = _balanced(uniques[0], uniques[0].index("("))
    cols = {c.strip() for c in key.split(",")}
    assert cols == {
        "listing_id", "claim_set_hash", "resolver_version", "registry_version_id",
        "policy_version", "collision_epoch_id",
    }, f"resolution identity must be the FIVE version inputs plus the listing, got {sorted(cols)}"
    assert re.search(r"collision_epoch_id\s+bigint not null references pin_cluster_epochs\(id\)",
                     _clean()), "collision_epoch_id must be NOT NULL and FK pin_cluster_epochs(id)"


def test_dispositions_key_on_dedupe_key():
    """00 section 8.2: bumping reconciler_version is routine and re-detects every
    still-true finding as a NEW row; a disposition keyed on contradiction_id would
    orphan on every bump. contradiction_id is demoted to a nullable FK."""
    sql = _clean()
    defs = _column_defs(_table_body(sql, "location_contradiction_dispositions"))
    pk = next((d for d in defs if "primary key" in d), "")
    assert pk.startswith("dedupe_key"), (
        f"location_contradiction_dispositions must be keyed on dedupe_key, got: {pk!r}")
    fk = next((d for d in defs if d.startswith("contradiction_id")), "")
    assert "references location_contradictions(id)" in fk and "not null" not in fk, (
        f"contradiction_id must stay a NULLABLE FK recording which detection prompted "
        f"the decision, got: {fk!r}")
    for column in ("status", "disposition"):
        assert any(d.startswith(column + " ") for d in defs), (
            f"status (lifecycle) and disposition (judgement) are TWO columns; {column} is missing")
    # The detection UNIQUE keeps the version tuple (harmless, and it preserves
    # "one detection row per finding per version tuple").
    assert re.search(
        r"unique \(dedupe_key, listing_id, reconciler_version, registry_version_id\)", sql
    ), "location_contradictions must keep its version-tuple UNIQUE"


def test_every_created_object_is_revoked():
    """This Supabase project auto-GRANTs anon/authenticated on new tables,
    sequences AND functions, so a location object without an explicit REVOKE is
    reachable from the browser roles."""
    sql = _clean()
    missing: list[str] = []

    relations = re.findall(r"create (?:table|view) ([a-z0-9_]+)", sql)
    for rel in relations:
        if not re.search(rf"revoke all on {rel}\s+from anon,\s*authenticated", sql):
            missing.append(f"table/view {rel}")

    for m in re.finditer(r"create table ([a-z0-9_]+)\s*\(", sql):
        table = m.group(1)
        for col in _column_defs(_balanced(sql, m.end() - 1)):
            parts = col.split()
            if len(parts) >= 2 and parts[1] in ("bigserial", "serial"):
                seq = f"{table}_{parts[0]}_seq"
                if not re.search(rf"revoke all on sequence {seq}\s+from anon,\s*authenticated", sql):
                    missing.append(f"sequence {seq}")

    for m in re.finditer(r"create function ([a-z0-9_]+)\s*\(", sql):
        fn = m.group(1)
        if not re.search(rf"revoke execute on function {fn}\s*\(", sql):
            missing.append(f"function {fn}")

    assert not missing, (
        "location W1 object(s) created without an explicit REVOKE from the browser "
        "roles:\n  " + "\n  ".join(sorted(set(missing)))
    )


def test_serving_projections_have_no_generated_columns():
    """01 section 7 rule (a): every derived value on a projection is written by
    the builder from a named function. A stored generated column would be
    silently stale the day an enum gains a value."""
    sql = _clean()
    for table in ("listing_location_current", "property_location_current"):
        body = _table_body(sql, table)
        assert "generated always as" not in body, (
            f"{table} declares a generated column; the projection builder owns every "
            "derived value (01 section 0.4)")
