"""Rule 21 for the health checks: no string literal in scripts/checks/ names a portal."""

# A check judges every portal by the seams its walks record (DECLARED_TOTAL_KEY, total_is_count,
# declared_is_upper_bound), never by its name. Docstrings are prose and are not scanned; every
# other str constant is, f-string parts included. Word boundaries let a shared name that holds a
# portal name as a mere prefix (a synthetic 'idnes_fake') through, and catch the English
# word "maxima" (write "maximum values"). An underscore is a word character, so two names they
# pass have arms of their own: the declared-total key's value (import DECLARED_TOTAL_KEY: an
# f-string's literal parts never hold what it interpolates) and the sreality_id identifier (a
# join keys on (source, native_id)).

from __future__ import annotations

import ast
import re
from pathlib import Path

from scraper.portal_runner import DECLARED_TOTAL_KEY

_CHECKS = Path(__file__).resolve().parents[2] / "scripts" / "checks"
_PORTAL = re.compile(
    r"\b(sreality|bazos|bezrealitky|mmreality|idnes|remax|ceskereality|maxima|realitymix)\b",
    re.IGNORECASE)
_PORTAL_ID = re.compile(r"\bsreality_id\b", re.IGNORECASE)
_DOCUMENTED = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _literals(source: str) -> list[tuple[int, str]]:
    """Every str constant's (line, text), docstrings left out; f-string parts are constants."""
    tree = ast.parse(source)
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, _DOCUMENTED) and node.body
                  and isinstance(node.body[0], ast.Expr)
                  and isinstance(node.body[0].value, ast.Constant)
                  and isinstance(node.body[0].value.value, str)}
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


def _hits(source: str) -> list[tuple[int, str]]:
    return [(line, text) for line, text in _literals(source)
            if _PORTAL.search(text) or DECLARED_TOTAL_KEY in text or _PORTAL_ID.search(text)]


def test_no_check_module_spells_a_portal_in_a_literal() -> None:
    modules = sorted(_CHECKS.glob("*.py"))
    assert {p.name for p in modules} >= {"clock.py", "common.py", "platform.py", "scraping.py"}
    offenders = {p.name: _hits(p.read_text()) for p in modules}
    assert {name: hits for name, hits in offenders.items() if hits} == {}


def test_the_rail_sees_plain_and_formatted_literals_but_not_docstrings() -> None:
    source = '''"""A module about bazos."""
def check(x):
    """Reads idnes walks."""
    key = "Remax"
    sql = f"select '{x}' from walks where source = 'maxima'"
    return "idnes_fake", key, sql
'''
    assert [text for _, text in _hits(source)] == ["Remax", "' from walks where source = 'maxima'"]


def test_the_rail_catches_the_declared_total_key_and_the_sreality_id_identifier() -> None:
    """The word boundaries pass both, so each has its own arm; the imported key stays legal."""
    spelled = f"sum((e ->> '{DECLARED_TOTAL_KEY}')::numeric)"
    joined = "join listings l on l.SREALITY_ID = q.native_id::bigint"
    assert not _PORTAL.search(spelled) and not _PORTAL.search(joined)
    source = f'''def check():
    imported = f"sum((e ->> '{{DECLARED_TOTAL_KEY}}')::numeric)"
    return imported, {spelled!r}, {joined!r}
'''
    assert sorted(text for _, text in _hits(source)) == sorted([spelled, joined])


def test_the_scan_is_not_empty() -> None:
    """A rail that reads nothing passes everything: the checks hold their SQL as literals."""
    assert len(_literals((_CHECKS / "scraping.py").read_text())) > 50
