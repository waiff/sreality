"""Offline contract gate for the location-data W1 schema (migrations 380-384).

Implements the CI checks of `01-schema.md` appendix A.2 as STATIC checks over the
migration SQL text — no database connection, so they run in the normal pytest job
and fail a PR the moment the schema drifts from the design corpus.

The corpus these checks read is NOT frozen at 384 (`_structural_files`): it is
every migration from 380 onward that is location work, so migration 385 and
everything after it is held to the same rules.

Which A.2 check each test covers:

  A.2 #2  every literal cast to a location enum IN A MIGRATION is a member of it.
          NARROWED ON PURPOSE: A.2 #2 as written is a source-tree literal scan
          (any Python/SQL string compared against a location enum anywhere in the
          backend); this file implements only the migration-enum-cast half. The
          source-tree scan lands with the resolver PR, which is the first code to
          hold such literals.
          -> test_enum_types_carry_the_canonical_vocabulary
             test_enum_casts_reference_declared_members
             test_granularity_rank_seeds_every_label_in_declaration_order
             test_level_granularity_seeds_every_ruian_level
             test_seed_literals_are_enum_members
  A.2 #4  no source file emits the string `portal_json`
          -> test_no_source_emits_portal_json
  A.2 #6  a new location_granularity value also touches location_granularity_rank
          -> test_granularity_rank_seeds_every_label_in_declaration_order
             test_granularity_alter_type_always_seeds_a_rank_row
  A.2 #8  `pin_collision_class IS [NOT] NULL` appears nowhere
  01 0.4  enum ordinality never enters an index predicate, a CHECK or a stored
          generated column
          -> test_no_enum_ordinality_in_ddl
  D3/05 P5 every grade axis the answer table declares is NOT NULL (a NULL reads
          as "no gate" and fails open) — `listing_location` declares four
          -> test_the_answer_table_declares_every_axis_not_null
             test_the_answer_table_does_not_re_declare_a_dropped_axis
  rule 25 the W1 projection and every resolver-side relation is dropped WHOLE
          -> test_w2b_drops_the_old_projection_and_the_resolver_side_relations

W2-b deleted the assertions whose objects are gone: the two projections' NOT NULL
axes, the pin-collision-class vocabulary and its IS NULL ban, the three licence
CHECKs (the rail moved upstream into the resolver's claim read — see migration
501's header), `location_resolutions`' six-column UNIQUE, the contradiction
tables' keys, and `location_level_granularity`'s seed. The one that stays is
`location_granularity_rank`'s per-label seed: dedup reads that table.

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
# The structural checks start here — 380 is the first location migration, and
# nothing before it may be rewritten (architecture rule #1).
_MIN_STRUCTURAL_MIGRATION = 380
# Trees whose SQL/Python could compare a literal against a location enum.
_SOURCE_DIRS = ("scraper", "toolkit", "api", "scripts", "migrations")
# A migration >= 380 joins the structural corpus if its name says location or its
# body names a location enum / table family. Enum names come from CANONICAL_ENUMS
# below (resolved at call time).
_LOCATION_TABLE_MARKERS = (
    "location_", "ruian_", "registry_version", "pin_cluster", "pin_collision",
    "dirty_locations", "portal_contract",
)


def _w1_files() -> list[Path]:
    return sorted(_MIGRATIONS_DIR.glob(_W1_GLOB))


def _migration_number(path: Path) -> int | None:
    m = re.match(r"(\d+)_", path.name)
    return int(m.group(1)) if m else None


def _structural_files() -> list[Path]:
    """Every migration the structural checks below must see.

    NOT frozen at 384. A corpus glued to the five W1 files would let migration
    385 add a `location_granularity` label, drop a REVOKE, reintroduce the
    forbidden `pin_collision_class IS NULL` form or bury an ordinal enum
    comparison in a CHECK — with every assertion in this file still green.
    """
    markers = tuple(CANONICAL_ENUMS) + _LOCATION_TABLE_MARKERS
    out: list[Path] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        number = _migration_number(path)
        if number is None or number < _MIN_STRUCTURAL_MIGRATION:
            continue
        if "_location_" in path.name:
            out.append(path)
            continue
        text = path.read_text(encoding="utf-8").lower()
        if any(marker in text for marker in markers):
            out.append(path)
    return out


def _clean() -> str:
    """The location migration corpus, comments stripped and lowercased."""
    sql = "\n".join(p.read_text(encoding="utf-8") for p in _structural_files())
    return _strip_comments(sql).lower()


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
    assert m, f"no location migration creates table {table}"
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
    assert not missing, f"location enum type(s) never declared in any location migration: {missing}"
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


def test_granularity_alter_type_always_seeds_a_rank_row():
    """A.2 #6 over the WHOLE migration directory, not just the location corpus.

    `rank()` is the only legal persisted granularity comparison (01 section 0.4),
    so a label added by `ALTER TYPE location_granularity ADD VALUE` without a
    `location_granularity_rank` row in the SAME migration leaves every rank join
    silently dropping the new rung — and a rank row "one migration later" is a
    live window in which that happens in production."""
    add_value = re.compile(
        r"alter\s+type\s+location_granularity\s+add\s+value\s+"
        r"(?:if\s+not\s+exists\s+)?'([a-z0-9_]+)'"
    )
    offenders: list[str] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        text = _strip_comments(path.read_text(encoding="utf-8")).lower()
        labels = [m.group(1) for m in add_value.finditer(text)]
        if not labels:
            continue
        seeds = "\n".join(
            s for s in _statements(text)
            if re.match(r"\s*insert into location_granularity_rank\b", s.lower())
        )
        for label in labels:
            if f"'{label}'" not in seeds:
                offenders.append(f"{path.name}: '{label}'")
    assert not offenders, (
        "location_granularity label(s) added by ALTER TYPE with no matching "
        "location_granularity_rank INSERT in the same migration (01 section A.2 "
        "check 6):\n  " + "\n  ".join(offenders)
    )


def test_the_bare_literal_seed_tables_are_dropped():
    """A.2 #2 wanted every closed-vocabulary seed spelled with enum members rather than
    bare literals. Both tables that did so are gone: `location_claim_type_meta` (three
    booleans per enum label that nothing consulted — migration 498) and
    `location_uncertainty_policy` (four derivations of a radius that is a per-level
    constant dict in `grade.py` now — migration 502). Nothing seeds a closed vocabulary
    with bare literals any more, so the check is that neither table came back."""
    sql = _clean()
    for table in ("location_claim_type_meta", "location_uncertainty_policy"):
        assert f"drop table if exists {table}" in sql, table


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


# The axes that must be NOT NULL, per table. A NULL axis reads as "no gate" and fails
# open — a NULL uncertainty_radius_m makes both branches of the three-valued containment
# test evaluate NULL, so the row silently drops out of `certain` AND `possible`.
#
# W2-a's answer table declares FOUR, where W1's projections declared six:
# `position_source`, `blur_evidence` and `radius_semantics` are not columns any more (the
# producers went with the policy tables and the collision epoch), and `country_status`
# joins the list because "foreign is a determination, never a default" is the same kind of
# rule — `undetermined` is a VALUE. The two projections were dropped by W2-b, so this is
# now one table's contract, not three.
_NOT_NULL_AXES = {
    "listing_location": (
        "granularity", "match_confidence", "uncertainty_radius_m", "country_status",
    ),
}


def test_the_answer_table_declares_every_axis_not_null():
    sql = _clean()
    offenders: list[str] = []
    for table, axes in _NOT_NULL_AXES.items():
        defs = _column_defs(_table_body(sql, table))
        by_name = {d.split(None, 1)[0]: d for d in defs if d.split(None, 1)}
        for axis in axes:
            col = by_name.get(axis)
            if col is None:
                offenders.append(f"{table}.{axis} is missing")
            elif "not null" not in col:
                offenders.append(f"{table}.{axis} is nullable")
    assert not offenders, (
        "answer-table axis column(s) not NOT NULL:\n  " + "\n  ".join(offenders)
    )


def test_the_answer_table_does_not_re_declare_a_dropped_axis():
    """The three axes W2-a dropped. Adding one back is adding a column nothing produces:
    the policy tables, the collision epoch and the licence CHECK are all deleted."""
    defs = _column_defs(_table_body(_clean(), "listing_location"))
    names = {d.split(None, 1)[0] for d in defs if d.split(None, 1)}
    assert names & {
        "position_source", "blur_evidence", "radius_semantics", "position_licence_class",
        "pin_collision_class", "policy_version",
    } == set()


def test_no_source_emits_portal_json():
    """A.2 #4: `portal_json` is a member of no enum — the migration emits the
    specific surface (sreality -> api_json, bezrealitky -> graphql, mmreality ->
    embedded_json)."""
    offenders = _scan_sources(re.compile(r"portal_json"))
    assert not offenders, f"file(s) emit the non-member literal `portal_json`: {offenders}"


def test_the_licence_rail_is_the_claim_side_index():
    """00 section 6.1 spent three CHECKs on `position_licence_class` so a Mapy-class
    coordinate could not be minted or stored. W2-a moved the guard UPSTREAM — the
    resolver's `_CLAIMS_SELECT` admits only `licence_class IN ('portal','operator')`, so
    such a coordinate is never READ — and W2-b dropped the three relations that carried
    the CHECKs. What must survive is the partial index that keeps the remediation set one
    indexed predicate away, permanently."""
    sql = _clean()
    assert re.search(
        r"create index location_claims_ephemeral on location_claims \(source, first_observed_at\)\s*"
        r"where licence_class = 'ephemeral_display_only'",
        sql,
    ), "missing the location_claims_ephemeral partial index"
    for name in ("llc_licence", "plc_licence", "loc_res_licence"):
        assert f"constraint {name}" in sql, (
            f"{name} must stay in the history — migrations are append-only")


def _last_view_body(sql: str, name: str) -> str:
    """The definition a replay leaves standing: the corpus is every location migration
    concatenated, so the LAST `create [or replace] view <name>` wins."""
    body = sql[sql.rindex(f"view {name} as"):]
    return body[:body.index(";")]


def test_the_contract_shadow_mechanism_is_gone_whole():
    """W1-b (migration 498). Shadow was "claims mined and stored, excluded from resolution
    until a frozen labelled sample clears its floors": a header flag, three views and a
    `dirty_locations` reason. The floors gate was never exercised end to end and every
    contract is live, so the whole mechanism went — and it has to go WHOLE. A surviving
    view over a dropped column is a replay failure; a surviving CHECK value is a vocabulary
    entry nothing can produce, which is invisible until someone reads the constraint."""
    sql = _clean()
    assert "drop column if exists shadow" in sql
    for view in ("location_claims_live", "location_claims_unretracted",
                 "location_claims_shadow"):
        assert f"drop view if exists {view}" in sql, view
        # …and the drop is the LAST word on it: no later migration re-creates one.
        assert sql.rindex(f"drop view if exists {view}") > sql.rindex(f"view {view} as"), view
    reason_check = sql[sql.rindex("add constraint dirty_locations_reason_check"):]
    assert "'contract_shadow'" not in reason_check


def test_the_resolver_reads_the_claim_table_itself():
    """01 §A.2 check 9 said section 03 must never select from `location_claims` directly,
    only from `location_claims_live`, so a retraction could not be silently ignored. W1-b
    inverted the premise: a retraction DELETES, so there is nothing left to subtract and a
    view could only re-introduce a way to forget. The claim spine is the relation."""
    sql = _clean()
    assert "drop table if exists location_claim_retractions" in sql
    body = _table_body(sql, "location_claims")
    assert "claim_fingerprint" in body


def _revoked_roles(sql: str, head: str) -> set[str] | None:
    """Roles named by the LAST REVOKE whose head matches `head`, or None if there
    is no such REVOKE. A REVOKE that names the wrong roles is worse than none: it
    reads as protection and grants nothing back.

    LAST, not first: the corpus is every location migration concatenated in
    number order, and an object can be re-created by a later one. A DROP VIEW +
    CREATE VIEW resets the ACL, so the earlier file's REVOKE protects nothing --
    only the newest one is in force. Same "highest-numbered migration is the
    effective definition" rule tests/test_browse_read_path_guardrail.py applies
    to view bodies. (Read first, this passed migration 506's narrower-than-it-
    looks predecessor and would have missed a real regression.)"""
    hits = re.findall(head + r"\s+from\s+([a-z0-9_,\s]+?);", sql)
    if not hits:
        return None
    return {role.strip() for role in hits[-1].split(",") if role.strip()}


def test_every_created_object_is_revoked():
    """This Supabase project auto-GRANTs anon/authenticated on new tables,
    sequences AND functions, so a location object without an explicit REVOKE is
    reachable from the browser roles. Functions additionally need `public`: the
    default ACL is `EXECUTE TO PUBLIC`, which anon and authenticated INHERIT, so
    revoking only the two named roles leaves the function callable."""
    sql = _clean()
    missing: list[str] = []
    relation_roles = {"anon", "authenticated"}
    function_roles = {"public", "anon", "authenticated"}

    def check(label: str, head: str, required: set[str]) -> None:
        roles = _revoked_roles(sql, head)
        if roles is None:
            missing.append(f"{label} — no REVOKE at all")
        elif not required <= roles:
            missing.append(f"{label} — REVOKE omits {sorted(required - roles)} (got {sorted(roles)})")

    for rel in re.findall(r"create (?:table|view) ([a-z0-9_]+)", sql):
        check(f"table/view {rel}", rf"revoke all on {rel}\b", relation_roles)

    for m in re.finditer(r"create table ([a-z0-9_]+)\s*\(", sql):
        table = m.group(1)
        for col in _column_defs(_balanced(sql, m.end() - 1)):
            parts = col.split()
            if len(parts) >= 2 and parts[1] in ("bigserial", "serial"):
                seq = f"{table}_{parts[0]}_seq"
                check(f"sequence {seq}", rf"revoke all on sequence {seq}\b", relation_roles)

    for m in re.finditer(r"create function ([a-z0-9_]+)\s*\(", sql):
        fn = m.group(1)
        check(f"function {fn}", rf"revoke execute on function {fn}\s*\([^)]*\)", function_roles)

    assert not missing, (
        "location object(s) created without an explicit REVOKE from the browser "
        "roles:\n  " + "\n  ".join(sorted(set(missing)))
    )


def test_the_answer_table_has_no_generated_columns():
    """01 section 7 rule (a): every derived value is written by the resolver, never by a
    stored generated column, which would be silently stale the day an enum gains a value."""
    body = _table_body(_clean(), "listing_location")
    assert "generated always as" not in body, (
        "listing_location declares a generated column; the resolver owns every derived "
        "value (01 section 0.4)")


def test_w2b_drops_the_old_projection_and_the_resolver_side_relations():
    """Rule 25's deletion, as a list. The W1 projection pair and every relation the
    deleted engines wrote go WHOLE: a surviving table with no writer is a table the next
    session reads as live, and a surviving view over a dropped column is a replay
    failure. `location_granularity_rank` is deliberately absent — dedup joins it."""
    sql = _clean()
    for relation in (
        "listing_location_current", "property_location_current",
        "location_resolutions", "location_resolution_candidates",
        "location_resolution_verifications",
        "location_contradictions", "location_contradiction_dispositions",
        "location_contradiction_disposition_log",
        "pin_cluster_epochs", "pin_clusters", "pin_cluster_daily_summary",
        "location_field_policy", "location_uncertainty_policy", "location_collision_policy",
        "location_constants", "location_level_granularity", "location_metrics_rollup",
        "location_labelled_samples", "location_labelled_sample_members",
        "location_compare_cohort", "location_compare_cohort_state",
    ):
        assert f"drop table if exists {relation}" in sql, relation
        assert sql.rindex(f"drop table if exists {relation}") > sql.rindex(
            f"create table {relation}"), f"{relation} is re-created after its drop"
    assert "drop view if exists location_contradictions_open" in sql
    assert "drop function if exists refresh_location_compare_cohort()" in sql
    assert "create table listing_location " in sql and (
        "drop table if exists listing_location;" not in sql), (
        "the answer table must survive the sweep")
