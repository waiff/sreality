"""THE CENSUS — a 65th unit-blind per-m² site cannot be added silently.

North star: one measure, one definition, one label. W1–W7 and W9 moved 64 call
sites onto `public.measure_price_per_m2` / `..._basis` (migration 425). This
file is the part of W8 that keeps them there: it re-counts the population on
every push and fails unless every occurrence is named in
`toolkit.measures.REGISTERED_SITES` with a stated reason.

WHY A CENSUS AND NOT A BAN. Some occurrences are legitimate and always will be:
the measure's own SQL body divides a price by an area — that is what a measure
IS — and the unit strings have to be spelled somewhere. A ban would be either
false or unenforceable. A census is neither: it allows exactly the enumerated
population and reds on the next one, whatever it is.

OFFLINE, NO DB, NO NEW DEPENDENCY. Pure `re` + `tokenize` over files on disk,
collected by the existing `pytest -q`. It reaches the three territories the type
system cannot: migrations, the Chrome extension, and Python-emitted SQL strings.

TWO ARMS, because one is provably not enough.
  * `division` — a price identifier over an area identifier. Catches the classic
    `price_czk / area_m2`. It CANNOT catch `12.0 * rent_per_m2_month /
    sale_per_m2` (`scraper/price_stats_metrics.py`), which divides one per-m²
    rate by another and names no area at all.
  * `unit` — a per-m² unit literal (`Kč/m²`, `CZK/m2`, `Kč/m²/měs`, …). That is
    the arm that catches it, and every render surface besides.

WHAT IS SCANNED, AND WHAT IS NOT.
  * Source: `.py` / `.ts` / `.tsx` under scraper, toolkit, api, scripts,
    frontend/src, chrome-extension/src. COMMENTS ARE STRIPPED; STRING LITERALS
    AND DOCSTRINGS ARE NOT. A comment is prose ABOUT the code and reformatting
    one must not red CI; a string is something the program can EMIT, and a unit
    inside one is a label a user will read. (This is also why the
    `price_stats_metrics` docstring counts: it is the module's own declaration
    of the two units it cancels.)
  * SQL: the EFFECTIVE definition of each database object — the highest-numbered
    `create` for that object, unless a strictly later migration drops it.
    Superseded history is not scanned, which is the point of "effective":
    `migrations/420`'s `listings_public` is dead the moment 425 replaces it.
  * `ruian_*` identifiers and `area_km2` / `area_ha` are excluded by name.
    `location_data/ruian_boundaries.py` and `migrations/381` use `area_m2` for
    POLYGON area — a name collision, not a measure — and a naive regex
    false-positives on `neighborhoods.py`'s `active_count / area_km2` density.

TWO TREES ARE DELIBERATELY OUT OF SCOPE, and the omission is a choice rather
than an oversight. `location_data/` holds the RÚIAN boundary code, whose
`area_m2` is cadastral polygon area — the name collision the exclusion list
exists for — and it computes no price; `tests/` is fixtures and fakes. Neither
ships a per-m² figure to a human today. If either ever does, add it to
`SOURCE_DIRS` rather than reasoning around it.

TO ADD A SITE: put it in `REGISTERED_SITES` with a `why` that says which of the
three legitimate things it is — it calls the measure, it IS the measure's
definition, or it is a different quantity — or mark it `KIND_DEBT` and say who
owes what. Do not widen the regexes.
"""

from __future__ import annotations

import io
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

from toolkit.measures import (
    MEASURES,
    REGISTERED_SITES,
    SITE_KINDS,
    RegisteredSite,
)

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "migrations"

SOURCE_DIRS: tuple[str, ...] = (
    "scraper",
    "toolkit",
    "api",
    "scripts",
    "frontend/src",
    "chrome-extension/src",
)
SOURCE_SUFFIXES: tuple[str, ...] = (".py", ".ts", ".tsx")

ARM_DIVISION = "division"
ARM_UNIT = "unit"

# --- the two arms ---------------------------------------------------------

_IDENT = r"[A-Za-z_$][A-Za-z0-9_.$]*"
# A division whose left operand is a price-ish identifier and whose right
# operand is an area-ish one, tolerating a `::numeric` cast, a closing paren
# from `round(`, and one wrapping call on the denominator (`nullif(area_m2, 0)`).
# Every gap is a SINGLE `\s*` and every optional piece absorbs its own
# whitespace: three consecutive `\s*` separated by optional groups is
# ambiguous, and the resulting backtracking cost 5 s on one 2 000-line module.
_DIVISION = re.compile(
    rf"(?P<num>{_IDENT})(?:\s*::\s*[A-Za-z_][A-Za-z0-9_]*)?(?:\s*\))?\s*/\s*"
    rf"(?:\(\s*)?"
    rf"(?:(?:nullif|coalesce|greatest|Number|parseFloat|float|Decimal)\s*\(\s*)?"
    rf"(?P<den>{_IDENT})"
)
_PRICE_TOKEN = re.compile(r"price|cena|rent|czk", re.IGNORECASE)
_AREA_TOKEN = re.compile(r"area|plocha|vymera", re.IGNORECASE)
# Excluded by name: polygon area (a name collision) and the density denominator.
_AREA_EXCLUDED = re.compile(r"ruian|area_km2|area_ha", re.IGNORECASE)

# `Kč/m²`, `CZK/m2`, `Kč / m²`, `Kč&nbsp;/&nbsp;m²`, and the `…/měs` forms
# (the suffix rides along on the same match).
#
# ONE star per gap, over alternatives that do not overlap: the first draft
# wrote `\s*(?:…| |\s)*` around the slash, whose nested quantifiers over
# overlapping whitespace backtrack catastrophically — 53 seconds across the six
# trees, against 0.5 s for the form below. A gate nobody will wait for is a
# gate that gets switched off.
_UNIT_LITERAL = re.compile(
    r"(?:K[čc]|CZK)(?:&nbsp;|\\u00a0|\s)*/(?:&nbsp;|\\u00a0|\s)*m\s*[²2]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Hit:
    path: str
    arm: str
    line: int
    text: str


def _scan(path: str, source: str) -> list[Hit]:
    """Both arms over one already-comment-stripped body."""
    hits: list[Hit] = []
    lines = source.split("\n")

    def _line_of(offset: int) -> int:
        return source.count("\n", 0, offset) + 1

    for m in _DIVISION.finditer(source):
        num, den = m.group("num"), m.group("den")
        if not _PRICE_TOKEN.search(num) or not _AREA_TOKEN.search(den):
            continue
        if _AREA_EXCLUDED.search(num) or _AREA_EXCLUDED.search(den):
            continue
        line = _line_of(m.start())
        hits.append(Hit(path, ARM_DIVISION, line, lines[line - 1].strip()[:140]))

    for m in _UNIT_LITERAL.finditer(source):
        line = _line_of(m.start())
        hits.append(Hit(path, ARM_UNIT, line, lines[line - 1].strip()[:140]))

    return hits


# --- comment stripping ----------------------------------------------------


def strip_python_comments(source: str) -> str:
    """Blank `#` comments, preserving every other byte's line and column.

    `tokenize` rather than a regex: a `#` inside a string literal is not a
    comment, and this file must not report `PPM2_UNIT_CS` entries as prose.
    Falls back to the raw source if the file does not tokenize (it will then be
    scanned including comments — noisier, never blinder).
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    lines = source.split("\n")
    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        (row, col_start), (_, col_end) = tok.start, tok.end
        line = lines[row - 1]
        lines[row - 1] = line[:col_start] + " " * (col_end - col_start) + line[col_end:]
    return "\n".join(lines)


def strip_ts_comments(source: str) -> str:
    """Blank `//` and `/* */`, preserving newlines, strings and regex literals.

    A regex literal (`/\\s*\\/\\//`) contains slashes and can contain `/*`, so it
    has to be recognised or a comment scanner walks off the end of the file. The
    standard heuristic applies: a `/` is a regex only where a value cannot
    already have ended.
    """
    out: list[str] = []
    i, n = 0, len(source)
    prev_significant = ""
    while i < n:
        c = source[i]
        if c in "'\"`":
            quote = c
            out.append(c)
            i += 1
            while i < n:
                if source[i] == "\\" and i + 1 < n:
                    out.append(source[i : i + 2])
                    i += 2
                    continue
                out.append(source[i])
                if source[i] == quote:
                    i += 1
                    break
                i += 1
            prev_significant = quote
            continue
        if source.startswith("//", i):
            j = source.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        if source.startswith("/*", i):
            j = source.find("*/", i)
            j = n if j < 0 else j + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in source[i:j]))
            i = j
            continue
        if c == "/" and prev_significant in ("", "=", "(", ",", ":", "[", "!", "&", "|", "?", "{", "}", ";", "+", "-", "*", "%", "~", "^", "<", ">", "\n"):
            # regex literal — copy verbatim through the unescaped closing slash
            out.append(c)
            i += 1
            in_class = False
            while i < n:
                if source[i] == "\\" and i + 1 < n:
                    out.append(source[i : i + 2])
                    i += 2
                    continue
                if source[i] == "[":
                    in_class = True
                elif source[i] == "]":
                    in_class = False
                elif source[i] == "/" and not in_class:
                    out.append(source[i])
                    i += 1
                    break
                elif source[i] == "\n":
                    break  # not a regex after all; bail out rather than eat the file
                out.append(source[i])
                i += 1
            prev_significant = "/"
            continue
        out.append(c)
        if not c.isspace():
            prev_significant = c
        elif c == "\n":
            prev_significant = "\n"
        i += 1
    return "".join(out)


def strip_sql_comments(source: str) -> str:
    """Blank `--` and `/* */`, preserving newlines. Single-quoted SQL strings
    are kept: a unit rendered into a jsonb `detail` is a label a user reads."""
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c == "'":
            out.append(c)
            i += 1
            while i < n:
                out.append(source[i])
                if source[i] == "'":
                    if i + 1 < n and source[i + 1] == "'":
                        out.append(source[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if source.startswith("--", i):
            j = source.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        if source.startswith("/*", i):
            j = source.find("*/", i)
            j = n if j < 0 else j + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in source[i:j]))
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


# --- the source-tree half -------------------------------------------------


def _source_files() -> list[Path]:
    files: list[Path] = []
    for rel in SOURCE_DIRS:
        root = REPO / rel
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            if "__pycache__" in path.parts or "node_modules" in path.parts:
                continue
            files.append(path)
    return files


def scan_sources() -> list[Hit]:
    hits: list[Hit] = []
    for path in _source_files():
        rel = path.relative_to(REPO).as_posix()
        raw = path.read_text(encoding="utf-8")
        body = strip_python_comments(raw) if path.suffix == ".py" else strip_ts_comments(raw)
        hits.extend(_scan(rel, body))
    return hits


# --- the SQL half: the EFFECTIVE definition of each database object --------

_SQL_LEAD = re.compile(r"^(?:\s|--[^\n]*\n|/\*.*?\*/)+", re.DOTALL)
_SQL_CREATE = re.compile(
    r"create\s+(?:or\s+replace\s+)?"
    r"(?P<kind>materialized\s+view|view|function|procedure|table)\s+"
    r"(?:if\s+not\s+exists\s+)?(?P<name>[A-Za-z_][\w.\"]*)",
    re.IGNORECASE,
)
_SQL_DROP = re.compile(
    r"\bdrop\s+(?P<kind>materialized\s+view|view|function|table)\s+"
    r"(?:if\s+exists\s+)?(?P<name>[A-Za-z_][\w.]*)",
    re.IGNORECASE,
)


def _sql_statements(sql: str) -> list[str]:
    """Split on top-level `;`, respecting `$tag$` bodies, `'…''…'` strings and
    both comment forms — so a whole plpgsql function is one statement."""
    stmts: list[str] = []
    i, n, start = 0, len(sql), 0
    while i < n:
        c = sql[i]
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        if c == "$":
            m = re.match(r"\$[A-Za-z_]\w*\$|\$\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                i = n if j < 0 else j + len(tag)
                continue
        if c == ";":
            stmts.append(sql[start:i])
            start = i + 1
        i += 1
    if sql[start:].strip():
        stmts.append(sql[start:])
    return stmts


def _migration_number(path: Path) -> int:
    return int(path.name.split("_", 1)[0])


def effective_sql_definitions() -> dict[str, tuple[Path, str]]:
    """`"<kind>:<name>" -> (migration file, statement text)` for every object
    whose newest `create` is not superseded by a strictly later `drop`."""
    created: dict[str, tuple[int, Path, str]] = {}
    dropped: dict[str, int] = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        num = _migration_number(path)
        sql = path.read_text(encoding="utf-8")
        for stmt in _sql_statements(sql):
            m = _SQL_CREATE.match(_SQL_LEAD.sub("", stmt))
            if not m:
                continue
            kind = " ".join(m.group("kind").lower().split())
            name = m.group("name").lower().replace('"', "").split(".")[-1]
            key = f"{kind}:{name}"
            if key not in created or num >= created[key][0]:
                created[key] = (num, path, stmt)
        for m in _SQL_DROP.finditer(strip_sql_comments(sql)):
            kind = " ".join(m.group("kind").lower().split())
            name = m.group("name").lower().split(".")[-1]
            key = f"{kind}:{name}"
            dropped[key] = max(dropped.get(key, 0), num)
    return {
        key: (path, stmt)
        for key, (num, path, stmt) in created.items()
        # `>` not `>=`: a migration that drops-then-recreates in one file (083,
        # 425) is a redefinition, not a removal.
        if dropped.get(key, 0) <= num
    }


def scan_sql() -> list[Hit]:
    hits: list[Hit] = []
    for key, (path, stmt) in sorted(effective_sql_definitions().items()):
        site = f"migrations/{path.name}::{key}"
        hits.extend(_scan(site, strip_sql_comments(stmt)))
    return hits


def scan_all() -> list[Hit]:
    return scan_sources() + scan_sql()


# ------------------------------------------------------------------ gate --


def _registry_index() -> dict[tuple[str, str], RegisteredSite]:
    index: dict[tuple[str, str], RegisteredSite] = {}
    for site in REGISTERED_SITES:
        key = (site.path, site.arm)
        assert key not in index, f"{site.path} [{site.arm}] registered twice"
        index[key] = site
    return index


def _render(hits: list[Hit]) -> str:
    return "\n".join(f"        {h.line}: {h.text}" for h in hits)


def test_every_per_m2_site_is_registered_with_the_right_count() -> None:
    """The census proper: the population on disk == the population declared.

    Counts, not line numbers. A count moves only when a site is added or
    removed, which is exactly when someone should read the justification next
    to it before deciding the new one is fine.
    """
    found: dict[tuple[str, str], list[Hit]] = {}
    for hit in scan_all():
        found.setdefault((hit.path, hit.arm), []).append(hit)
    index = _registry_index()

    problems: list[str] = []
    for key in sorted(found.keys() | index.keys()):
        path, arm = key
        hits, site = found.get(key, []), index.get(key)
        if site is None:
            problems.append(
                f"\n  UNREGISTERED — {path} [{arm}] x{len(hits)}\n{_render(hits)}\n"
                f"      This is a per-m² site the registry does not know about. If it "
                f"calls measure_price_per_m2 / measure_price_per_m2_basis, or renders "
                f"the label the server published, add a RegisteredSite in "
                f"toolkit/measures.py saying so. If it RE-DERIVES the formula, do not "
                f"register it — move it onto the measure (CLAUDE.md rule #23)."
            )
        elif not hits:
            problems.append(
                f"\n  STALE — {path} [{arm}] is registered (x{site.hits}) but no "
                f"longer matches. Delete the entry: an allowlist nobody prunes stops "
                f"being a census."
            )
        elif len(hits) != site.hits:
            problems.append(
                f"\n  COUNT MOVED — {path} [{arm}]: registered {site.hits}, found "
                f"{len(hits)}\n{_render(hits)}\n"
                f"      Registered reason: {site.why}\n"
                f"      Re-read that reason, confirm it still covers every occurrence "
                f"above, then update `hits`."
            )

    assert not problems, (
        "The per-m² census does not match toolkit/measures.REGISTERED_SITES."
        + "".join(problems)
    )


def test_every_measure_declares_numerator_denominator_unit_and_bounds() -> None:
    """A measure missing any of the four cannot be labelled, so it must not
    exist. `bounds` especially: 'when is there no number' is the half that gets
    dropped, and dropping it is how a 136 Kč commercial rental became 1 Kč/m²."""
    for key, measure in MEASURES.items():
        for part in ("name", "numerator", "denominator", "unit", "bounds"):
            value = getattr(measure, part)
            floor = 3 if part == "unit" else 12
            assert value and len(value.strip()) > floor, (
                f"MEASURES[{key!r}].{part} is empty or a placeholder — a measure "
                f"is not declared until all four halves are written out."
            )


def test_every_measure_has_a_defining_site_and_every_site_a_known_measure() -> None:
    """No orphans in either direction: a measure with no definition is a name
    for nothing, and a site pointing at a measure that does not exist is an
    unexamined allowlist row."""
    referenced = {s.measure for s in REGISTERED_SITES}
    for key in MEASURES:
        assert key in referenced, (
            f"MEASURES[{key!r}] is referenced by no registered site — a measure "
            f"nothing computes or labels is a name for nothing. Point a site at "
            f"it, or delete it."
        )
    assert "ppm2" in {s.measure for s in REGISTERED_SITES if s.kind == "defines"}, (
        "the per-m² measure itself must have a site registered as its definition"
    )
    for site in REGISTERED_SITES:
        assert site.kind in SITE_KINDS, f"{site.path}: unknown kind {site.kind!r}"
        assert site.arm in (ARM_DIVISION, ARM_UNIT), f"{site.path}: bad arm"
        assert site.hits >= 1, f"{site.path}: a site with no hits is not a site"
        assert len(site.why.strip()) > 40, (
            f"{site.path}: `why` must say which of the three legitimate things "
            f"this is — it calls the measure, it IS the measure, or it is a "
            f"different quantity."
        )
        if site.kind in ("defines", "calls", "labels", "guards"):
            assert site.measure in MEASURES, (
                f"{site.path}: kind={site.kind!r} must name a measure in MEASURES"
            )


def test_registered_debt_names_an_owner_and_a_blocker() -> None:
    """`debt` is the one kind that admits a site is WRONG. It stays honest only
    while it says who owes what — otherwise it is an allowlist row with a sad
    adjective."""
    for site in REGISTERED_SITES:
        if site.kind != "debt":
            continue
        assert "OWNER:" in site.why and "BLOCKER:" in site.why, (
            f"{site.path}: registered as debt without an OWNER: and a BLOCKER:."
        )


# --------------------------------------------------------- the self-test --
#
# A gate that cannot fail is not a gate. These run the two arms over synthetic
# text so the regexes are proven to fire (and proven not to fire on the two
# name collisions the census deliberately excludes) without waiting for a real
# regression to prove it in production.


def test_the_division_arm_fires_on_a_bare_re_derivation() -> None:
    for snippet in (
        "select price_czk::numeric / area_m2 as ppm2 from listings",
        "const ppm2 = row.price_czk / row.area_m2;",
        "round(p.current_price_czk::numeric / nullif(p.area_m2, 0), 2)",
        "median_rent / usable_area",
    ):
        assert [h for h in _scan("x", snippet) if h.arm == ARM_DIVISION], snippet


def test_the_unit_arm_fires_where_the_division_arm_cannot() -> None:
    # scraper/price_stats_metrics.py's shape: two per-m² rates, no `area`.
    snippet = 'return 12.0 * rent_per_m2_month / sale_per_m2\n"""rent is Kč/m²/month"""'
    hits = _scan("x", snippet)
    assert not [h for h in hits if h.arm == ARM_DIVISION]
    assert [h for h in hits if h.arm == ARM_UNIT]
    for spelling in ("Kč/m²", "CZK/m2", "Kč/m²/měs", "Kč / m²", "CZK/m²"):
        assert _scan("x", f'const u = "{spelling}";'), spelling


def test_the_census_does_not_fire_on_the_two_name_collisions() -> None:
    """`area_m2` on a RÚIAN polygon is cadastral area, and `area_km2` is a
    density denominator. Neither is a measure, and a census that cried wolf on
    them would be turned off within a month."""
    for snippet in (
        "ruian_price_czk / ruian_area_m2",
        "active_count / area_km2",
        "price_czk / area_ha",
    ):
        assert not _scan("x", snippet), snippet


def test_comments_are_stripped_but_strings_are_not() -> None:
    """The line the whole design turns on: prose ABOUT the measure is free,
    a unit the program can EMIT is registered."""
    py = 'X = "Kč/m²"  # and a Kč/m² in a comment\n'
    assert len(_scan("x", strip_python_comments(py))) == 1
    ts = "const u = 'Kč/m²'; // a Kč/m² in a comment\n/* and Kč/m² in a block */\n"
    assert len(_scan("x", strip_ts_comments(ts))) == 1
    sql = "-- a Kč/m² in a comment\nselect 'Kč/m²' as unit"
    assert len(_scan("x", strip_sql_comments(sql))) == 1


def test_the_effective_sql_definition_is_the_newest_undropped_one() -> None:
    """`listings_public` divided price by area until migration 425 replaced it.
    Scanning superseded history would make the census permanently red and
    permanently ignored."""
    effective = effective_sql_definitions()
    assert effective["view:listings_public"][0].name.startswith("425_"), (
        "listings_public should resolve to migration 425, not to 420"
    )
    assert "measure_price_per_m2" in effective["view:listings_public"][1]
    # dropped in 425, rebuilt from browse_projection by rebuild_properties_map_mv
    assert "materialized view:properties_map_mv" not in effective


def test_per_m2_sql_requires_an_alias() -> None:
    """Part (a) of the rail on the Python side: there is no zero-arg variant to
    fall back to, so a unit-blind fragment cannot be requested."""
    import pytest

    from toolkit import measures

    with pytest.raises(TypeError):
        measures.per_m2_sql()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        measures.per_m2_basis_sql()  # type: ignore[call-arg]
