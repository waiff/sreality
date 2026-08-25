"""THE CENSUS — a unit-blind per-m² site cannot be added silently.

North star: one measure, one definition, one label. W1–W7 and W9 moved 64 call
sites onto `public.measure_price_per_m2` / `..._basis` (migration 425). This
file is the part of W8 that keeps them there: it re-counts the population on
every push and fails unless every occurrence is named in
`toolkit.measures.REGISTERED_SITES` with a stated reason.

WHY A CENSUS AND NOT A BAN. Some occurrences are legitimate and always will be:
the measure's own SQL body divides a price by an area — that is what a measure
IS — and the unit strings have to be spelled somewhere. A ban would be either
false or unenforceable. A census is neither: it allows exactly the population
enumerated in the registry and reds on anything else it can see.

WHAT IT CAN SEE, STATED HONESTLY. This is the contract, and it is narrower than
"any new site reds the build" — a rail that oversells itself is worse than no
rail, because the next session reads the guarantee as proof. THREE ARMS:
  * `division` — a price-ish expression over an area-ish one. Both operands are
    resolved by a bracket-balanced walk outward from the operator, so
    `sum(l.price_czk)::numeric / nullif(sum(l.area_m2), 0)`, `r["price_czk"] /
    r["area_m2"]`, `coalesce(price_czk, 0) / area_m2` and `price // area_m2` all
    land, not only bare identifiers. It CANNOT catch `12.0 * rent_per_m2_month /
    sale_per_m2` (`scraper/price_stats_metrics.py`), which divides one per-m²
    rate by another and names no area at all.
  * `unit` — a per-m² unit literal (`Kč/m²`, `CZK/m2`, `Kč/m²/měs`, …). That is
    the arm that catches the one above, and every render surface besides.
  * `vocab` — any file that reads `PPM2_UNIT` / `PPM2_UNIT_CS` /
    `PPM2_VALUE_LABEL` / `PPM2_BASIS_TOKEN`, one hit per FILE. This arm exists
    because the other two are spelling filters and W8 teaches developers to
    IMPORT the label rather than spell it: a site that labels correctly and
    computes the number itself spells no unit and names no price identifier, so
    it walks through both. Consuming the vocabulary is a census event, and the
    registry entry must say where the number beside the label came from.

THE NAMED BLIND SPOTS — real, and listed so nobody has to rediscover them:
  * Both value arms are CLOSED-VOCABULARY SPELLING FILTERS. `price_czk / sqm`
    and `amount / area_m2` miss (`sqm` is not an area token, `amount` is not a
    price one), as does a unit assembled at runtime (`'Kč' + '/m²'`, or a bare
    `/m²` suffix with the currency formatted separately).
  * A division through a helper — `np.divide(price, area)`, `ratio(a, b)` — has
    no operator to walk out from and is invisible.
  * `ruian_*`, `area_km2` and `area_ha` are exempt BY NAME on the denominator,
    with no inspection of what the code does.
  * The SQL half is a census of `migrations/` ON DISK, not of the database. An
    object created by dynamic DDL inside plpgsql (`execute 'create …'`, e.g.
    migrations 283 / 299 / 371 / 376), or one that drifted into production with
    no create statement in any numbered file (`property_sources_mv` today), is
    unregisterable and unseen.
  * The census is VALUE-BLIND: it counts occurrences, never compares them. The
    two tests at the foot of this file are the exception, and they exist because
    counting could not tell a verbatim copy of a unit from a wrong one.
A rail that documents its own edges cannot manufacture confidence.

OFFLINE, NO DB, NO NEW DEPENDENCY. Pure `re` + `tokenize` over files on disk,
collected by the existing `pytest -q`. It reaches the three territories the type
system cannot: migrations, the Chrome extension, and Python-emitted SQL strings.

WHAT IS SCANNED, AND WHAT IS NOT.
  * Source: `.py` / `.ts` / `.tsx` under scraper, toolkit, api, scripts,
    frontend/src, chrome-extension/src. COMMENTS ARE STRIPPED; STRING LITERALS
    AND DOCSTRINGS ARE NOT. A comment is prose ABOUT the code and reformatting
    one must not red CI; a string is something the program can EMIT, and a unit
    inside one is a label a user will read. (This is also why the
    `price_stats_metrics` docstring counts: it is the module's own declaration
    of the two units it cancels.)
  * SQL, in two halves. OBJECT DEFINITIONS: the EFFECTIVE one per object — the
    highest-numbered `create` for it, unless a strictly later migration drops
    it. Superseded history is not scanned, which is the point of "effective":
    `migrations/420`'s `listings_public` is dead the moment 425 replaces it.
    EVERY OTHER STATEMENT — `alter table … generated always as`, `insert` /
    `update` backfills, `create index ((…))`, `comment on column`, `grant`,
    `do $$ … $$` — is scanned UNCONDITIONALLY, because it is not a definition
    anything can supersede: it ran once, in order. Leaving that half out is how
    a persisted, unfloored, basis-blind second definition of the measure ships
    through a green rail, and it left the database catalog — a declared label
    surface of this program, migration 425 § 7 — uncovered.
  * `ruian_*` identifiers and `area_km2` / `area_ha` are excluded by name, on
    the DENOMINATOR only. `location_data/ruian_boundaries.py` and
    `migrations/381` use `area_m2` for POLYGON area — a name collision, not a
    measure — and a naive regex false-positives on `neighborhoods.py`'s
    `active_count / area_km2` density.

TWO TREES ARE DELIBERATELY OUT OF SCOPE, and the omission is a choice rather
than an oversight. `location_data/` holds the RÚIAN boundary code, whose
`area_m2` is cadastral polygon area — the name collision the exclusion list
exists for — and it computes no price; `tests/` is fixtures and fakes. Neither
ships a per-m² figure to a human today. If either ever does, add it to
`SOURCE_DIRS` rather than reasoning around it.

TO ADD A SITE: put it in `REGISTERED_SITES` with a `why` that says which of the
three legitimate things it is — it calls the measure, it IS the measure's
definition, or it is a different quantity — or mark it `KIND_DEBT` and say who
owes what. A match on ordinary PROSE (a docstring listing column names, a
sentence naming the anti-pattern) belongs in the registry as `KIND_PROSE`; do
NOT reword the sentence to dodge the gate, and do not widen the regexes.
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
ARM_VOCAB = "vocab"

# --- the three arms -------------------------------------------------------

_PRICE_TOKEN = re.compile(r"price|cena|rent|czk", re.IGNORECASE)
_AREA_TOKEN = re.compile(r"area|plocha|vymera", re.IGNORECASE)
# Excluded by name, on the DENOMINATOR only: polygon area (a name collision)
# and the density denominator. Applying it to the numerator too would exempt
# `ruian_price_czk / area_m2`, which is a real re-derivation.
_AREA_EXCLUDED = re.compile(r"ruian|area_km2|area_ha", re.IGNORECASE)

# Every division operator, `/` and Python's floor `//` alike, taken as one
# token so the right operand starts after both slashes.
_SLASH = re.compile(r"/{1,2}")
# A trailing `::numeric` on the numerator, stripped before the walk.
_TRAILING_CAST = re.compile(r"::\s*[A-Za-z_][A-Za-z0-9_]*\s*$")
# No operand this program cares about is longer; the bound is what keeps the
# walk linear and what stops one unbalanced paren swallowing a whole module.
_MAX_OPERAND = 160
_OPERAND_EXTRA = "_.$"


def _left_operand(source: str, end: int) -> str:
    """The whole expression the `/` at `end` divides — not just an identifier.

    Walks backwards over a BALANCED bracket run, so `sum(l.price_czk)`,
    `coalesce(price_czk, 0)` and `r["price_czk"]` all resolve to text that
    still carries the token, instead of vanishing the way an identifier-only
    pattern made them vanish. Returns "" when the brackets do not balance
    inside the bound — a miss is better than a 160-character false positive.
    """
    j = end
    while j > 0 and source[j - 1] in " \t\r\n":
        j -= 1
    cast = _TRAILING_CAST.search(source[max(0, j - _MAX_OPERAND) : j])
    if cast:
        j = max(0, j - _MAX_OPERAND) + cast.start()
        while j > 0 and source[j - 1] in " \t\r\n":
            j -= 1
    limit, depth, k = max(0, j - _MAX_OPERAND), 0, j
    while k > limit:
        c = source[k - 1]
        if c in ")]":
            depth += 1
        elif c in "([":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and not (c.isalnum() or c in _OPERAND_EXTRA):
            break
        k -= 1
    return "" if depth else source[k:j]


def _right_operand(source: str, start: int) -> str:
    """The mirror walk, forwards: `nullif(sum(l.area_m2), 0)`, `r['area_m2']`."""
    i, n = start, len(source)
    while i < n and source[i] in " \t\r\n":
        i += 1
    limit, depth, k = min(n, i + _MAX_OPERAND), 0, i
    while k < limit:
        c = source[k]
        if c in "([":
            depth += 1
        elif c in ")]":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and not (c.isalnum() or c in _OPERAND_EXTRA):
            break
        k += 1
    return "" if depth else source[i:k]


# The shared per-m² vocabulary. Importing one of these names is itself a census
# event (arm three) — see the module docstring.
_VOCAB_SYMBOL = re.compile(
    r"\bPPM2_(?:UNIT_CS|VALUE_LABEL|BASIS_TOKEN|UNIT)\b"
)

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
    """All three arms over one already-comment-stripped body."""
    hits: list[Hit] = []
    lines = source.split("\n")

    def _line_of(offset: int) -> int:
        return source.count("\n", 0, offset) + 1

    def _add(arm: str, offset: int) -> None:
        line = _line_of(offset)
        hits.append(Hit(path, arm, line, lines[line - 1].strip()[:140]))

    for m in _SLASH.finditer(source):
        num = _left_operand(source, m.start())
        if not _PRICE_TOKEN.search(num):
            continue
        den = _right_operand(source, m.end())
        if not _AREA_TOKEN.search(den) or _AREA_EXCLUDED.search(den):
            continue
        _add(ARM_DIVISION, m.start())

    for m in _UNIT_LITERAL.finditer(source):
        _add(ARM_UNIT, m.start())

    # ONE hit per file, deliberately: this arm censuses FILES that consume the
    # shared vocabulary, not occurrences of it. Counting occurrences would red
    # the build when a component reads `PPM2_UNIT[basis]` twice instead of
    # once — churn on a correct edit, which is how a gate gets switched off.
    vocab = _VOCAB_SYMBOL.search(source)
    if vocab:
        _add(ARM_VOCAB, vocab.start())

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


def effective_sql_definitions(root: Path = MIGRATIONS) -> dict[str, tuple[Path, str]]:
    """`"<kind>:<name>" -> (migration file, statement text)` for every object
    whose newest `create` is not superseded by a strictly later `drop`.

    `root` is a parameter so the supersede logic can be exercised against
    synthetic migration text in a tmp_path instead of against live filenames,
    which churn (`listings_public` has been replaced eighteen times).
    """
    created: dict[str, tuple[int, Path, str]] = {}
    dropped: dict[str, int] = {}
    for path in sorted(root.glob("*.sql")):
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


def one_shot_sql_statements(root: Path = MIGRATIONS) -> dict[Path, list[str]]:
    """Every statement that is NOT one of the five tracked `create` forms.

    `alter table … generated always as`, `insert` / `update` backfills,
    `create index ((price / area))`, `comment on column … 'CZK/m²'`, `grant`,
    `do $$ … $$`. The supersede logic above decides which OBJECT DEFINITIONS
    are still live; these are not definitions — they execute exactly once, in
    order, and nothing later replaces them — so they are scanned
    unconditionally. Leaving them out is how a persisted, unfloored,
    basis-blind second definition of the measure ships through a green rail.
    """
    out: dict[Path, list[str]] = {}
    for path in sorted(root.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        stmts = [
            stmt
            for stmt in _sql_statements(sql)
            if not _SQL_CREATE.match(_SQL_LEAD.sub("", stmt))
        ]
        if stmts:
            out[path] = stmts
    return out


def scan_sql() -> list[Hit]:
    hits: list[Hit] = []
    for key, (path, stmt) in sorted(effective_sql_definitions().items()):
        site = f"migrations/{path.name}::{key}"
        hits.extend(_scan(site, strip_sql_comments(stmt)))
    for path, stmts in sorted(one_shot_sql_statements().items()):
        site = f"migrations/{path.name}::statements"
        for stmt in stmts:
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
        assert site.arm in (ARM_DIVISION, ARM_UNIT, ARM_VOCAB), (
            f"{site.path}: bad arm"
        )
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
    """Every shape below was probed against the W8 draft of this arm and MISSED.

    The draft matched a bare or dotted identifier on each side, so it saw
    `row.price_czk / row.area_m2` and nothing else. But psycopg dict rows and
    JSON payloads are SUBSCRIPTED throughout this repo (`r["price_czk"]` occurs
    twenty-odd times across toolkit, api, scraper and six portal mains), and the
    natural shape of a new region- or obec-stats RPC is an AGGREGATE ratio —
    which is the same class of site as `region_stats`, the worst find of the
    whole program. A rail that reds on the naive spelling and passes the
    idiomatic one manufactures exactly the confidence it is supposed to earn.
    """
    for snippet in (
        # what the identifier-only pattern already caught
        "select price_czk::numeric / area_m2 as ppm2 from listings",
        "const ppm2 = row.price_czk / row.area_m2;",
        "round(p.current_price_czk::numeric / nullif(p.area_m2, 0), 2)",
        "median_rent / usable_area",
        # subscripted operands — the dominant row-access idiom here
        'return r["price_czk"] / r["area_m2"]',
        'r.get("price_czk") / r.get("area_m2")',
        'float(r["price_czk"]) / float(r["area_m2"])',
        "const v = l['price_czk'] / l['area_m2'];",
        # aggregates and wrapped operands, on EITHER side
        "sum(price_czk) / sum(area_m2)",
        "avg(price_czk)/avg(area_m2)",
        "sum(l.price_czk)::numeric / nullif(sum(l.area_m2), 0) as ppm2",
        "coalesce(price_czk, 0) / area_m2",
        # Python's floor division
        "total_price_czk // area_m2",
        # a persisted second definition, the worst outcome of all
        "alter table listings add column ppm2 numeric "
        "generated always as (price_czk / area_m2) stored;",
    ):
        assert [h for h in _scan("x", snippet) if h.arm == ARM_DIVISION], snippet


def test_the_vocabulary_arm_fires_on_a_correct_label_over_a_wrong_number() -> None:
    """The combination neither spelling filter can see.

    A site that imports the shared label and computes the number itself spells
    no unit (the unit arm is silent — W8 itself teaches developers to import
    rather than spell) and names no price or area identifier (the division arm
    is silent). That is the shape an adversarial probe used to walk straight
    through the two-arm census. Consuming the vocabulary is therefore itself a
    census event, and the registry entry has to say where the NUMBER came from.
    """
    probe = (
        "num = sum(x.total for x in rows)\n"
        "den = sum(x.size for x in rows)\n"
        'label = PPM2_UNIT_CS["sale_capital_czk_m2"]\n'
        "return f'{num / den} {label}'\n"
    )
    hits = _scan("x", probe)
    assert not [h for h in hits if h.arm in (ARM_DIVISION, ARM_UNIT)]
    assert [h for h in hits if h.arm == ARM_VOCAB], probe

    # One per file, not one per occurrence: a component that reads the map
    # twice instead of once has not changed the population.
    twice = "a = PPM2_UNIT.rent;\nb = PPM2_UNIT.sale;\n"
    assert len([h for h in _scan("x", twice) if h.arm == ARM_VOCAB]) == 1
    # `PPM2_UNIT_CS` must not also register as a `PPM2_UNIT` occurrence.
    assert len(_scan("x", "PPM2_UNIT_CS")) == 1


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
    permanently ignored.

    The invariant is SEMANTIC — whatever migration currently defines the view,
    it calls the measure — and deliberately NOT `startswith("425_")`. That view
    has been replaced eighteen times in this repo's history; pinning the
    filename would red the build on the nineteenth replacement, blaming the
    wrong migration, for exactly the change rule #23 asks for. A gate that reds
    on correct work gets edited out. The supersede mechanics are covered
    against synthetic text below, where no live filename can rot the assertion.
    """
    effective = effective_sql_definitions()
    assert "measure_price_per_m2" in effective["view:listings_public"][1], (
        "the live definition of listings_public no longer calls the measure"
    )
    # dropped in 425, rebuilt from browse_projection by rebuild_properties_map_mv
    assert "materialized view:properties_map_mv" not in effective


def test_a_later_drop_supersedes_a_create_but_a_same_file_drop_does_not(
    tmp_path: Path,
) -> None:
    """The drop/supersede rule, on migration text this test owns.

    Three cases, all real shapes: a view replaced by a later migration resolves
    to the LATER one; a view dropped by a later migration disappears; a
    drop-and-recreate INSIDE one file (083, 425) is a redefinition, not a
    removal, which is why the comparison is `<=` and not `<`.
    """
    (tmp_path / "001_a.sql").write_text(
        "create view v_kept as select 1 as x;\n"
        "create view v_gone as select 2 as x;\n",
        encoding="utf-8",
    )
    (tmp_path / "002_b.sql").write_text(
        "create or replace view v_kept as select price_czk / area_m2 as ppm2 from listings;\n"
        "drop view if exists v_gone;\n",
        encoding="utf-8",
    )
    (tmp_path / "003_c.sql").write_text(
        "drop view if exists v_cycled;\ncreate view v_cycled as select 3 as x;\n",
        encoding="utf-8",
    )

    effective = effective_sql_definitions(tmp_path)
    assert effective["view:v_kept"][0].name == "002_b.sql"
    assert "price_czk / area_m2" in effective["view:v_kept"][1]
    assert "view:v_gone" not in effective
    assert effective["view:v_cycled"][0].name == "003_c.sql"


def test_every_statement_is_scanned_not_only_the_create_forms(
    tmp_path: Path,
) -> None:
    """A generated column, a DML backfill, an index expression and a column
    comment are all second definitions or second labels of the measure, and
    none of them is a tracked `create`. The generated column is the worst of
    them: a persisted, unfloored, basis-blind figure that every downstream
    consumer would then legitimately read as a plain column."""
    shapes = {
        "gen": "alter table listings add column ppm2 numeric "
        "generated always as (price_czk / area_m2) stored;",
        "insert": "insert into ppm2_cache (id, ppm2) "
        "select id, price_czk / area_m2 from listings;",
        "update": "update listings set ppm2 = price_czk / area_m2 where ppm2 is null;",
        "index": "create index listings_ppm2_idx on listings ((price_czk / area_m2));",
        "comment": "comment on column listings.price_czk is 'monthly rent in CZK/m2';",
    }
    for i, (label, sql) in enumerate(sorted(shapes.items()), start=1):
        (tmp_path / f"{900 + i}_{label}.sql").write_text(sql + "\n", encoding="utf-8")

    seen: dict[str, list[str]] = {}
    for path, stmts in one_shot_sql_statements(tmp_path).items():
        for stmt in stmts:
            for hit in _scan(path.name, strip_sql_comments(stmt)):
                seen.setdefault(path.name.split("_", 1)[1][:-4], []).append(hit.arm)

    assert sorted(seen) == sorted(shapes), (
        f"these statement shapes are invisible to the census: "
        f"{sorted(set(shapes) - set(seen))}"
    )
    assert seen["comment"] == [ARM_UNIT]
    assert seen["gen"] == [ARM_DIVISION]


def test_per_m2_sql_requires_an_alias() -> None:
    """Part (a) of the rail on the Python side: there is no zero-arg variant to
    fall back to, so a unit-blind fragment cannot be requested."""
    import pytest

    from toolkit import measures

    with pytest.raises(TypeError):
        measures.per_m2_sql()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        measures.per_m2_basis_sql()  # type: ignore[call-arg]


# ------------------------------------------------- the VALUE half of the rail --
#
# The census counts occurrences. It is value-BLIND by construction, so it cannot
# tell a verbatim copy of a unit string from a wrong one: changing the
# extension's monthly suffix to the bare capital one leaves every count at its
# registered number and CI green, while re-introducing the exact factor-of-twelve
# mislabel W8 removed from RunPanel. These two tests close that: the three
# territories are compared VALUE by VALUE, in the only direction that works —
# Python reading the other two as text, since neither can be imported here.

_TS_UNIT_MAP = re.compile(
    r"export const PPM2_UNIT\s*:[^=]*=\s*\{(?P<body>[^}]*)\}", re.DOTALL
)
_TS_UNIT_ENTRY = re.compile(r"(?P<key>sale|rent|land)\s*:\s*'(?P<value>[^']*)'")
_EXT_MONTHLY_UNIT = re.compile(r"const CZK_PER_M2_MONTH\s*=\s*'(?P<value>[^']*)'")


def test_the_spa_unit_vocabulary_matches_the_python_one_value_for_value() -> None:
    """`frontend/src/lib/measure.ts` is called a twin; this is what makes it one.

    `PPM2_UNIT.land` was a byte-for-byte copy of `PPM2_UNIT.sale` while
    `PPM2_UNIT_CS` named the plot denominator — one measure with two labels,
    and the census could not see it because both spellings are legal strings.
    """
    src = (REPO / "frontend/src/lib/measure.ts").read_text(encoding="utf-8")
    body = _TS_UNIT_MAP.search(src)
    assert body, "PPM2_UNIT is no longer an object literal in measure.ts"
    spa = {m.group("key"): m.group("value") for m in _TS_UNIT_ENTRY.finditer(body.group("body"))}

    from toolkit import measures

    expected = {
        "sale": measures.PPM2_UNIT_CS[measures.SALE_CAPITAL_CZK_M2],
        "rent": measures.PPM2_UNIT_CS[measures.RENT_MONTHLY_CZK_M2],
        "land": measures.PPM2_UNIT_CS[measures.LAND_CAPITAL_CZK_M2],
    }
    assert spa == expected, (
        f"PPM2_UNIT (SPA) and PPM2_UNIT_CS (Python) disagree: {spa} vs {expected}. "
        f"One measure, one label — pick the spelling and change BOTH, or the same "
        f"number renders in two units depending on which territory drew it."
    )
    assert len(set(expected.values())) == 3, (
        "two bases share a unit string, so the surfaces cannot distinguish them"
    )


def test_the_extension_copy_is_still_verbatim() -> None:
    """The Chrome extension has no test job and no importable module, so this is
    the ONLY thing standing between its copied suffix and a silent 12x
    mislabel of the fond rate and the MF reference rent."""
    src = (REPO / "chrome-extension/src/content.ts").read_text(encoding="utf-8")
    m = _EXT_MONTHLY_UNIT.search(src)
    assert m, "CZK_PER_M2_MONTH is gone from the extension — was it renamed?"

    from toolkit import measures

    assert m.group("value") == measures.PPM2_UNIT_CS[measures.RENT_MONTHLY_CZK_M2], (
        "the extension's monthly per-m² suffix is no longer a verbatim copy of "
        "PPM2_UNIT_CS['rent_monthly_czk_m2']. The bare capital unit on a monthly "
        "charge is wrong by a factor of twelve."
    )
