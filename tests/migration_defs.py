"""Which migration currently DEFINES a Postgres function.

The two Browse aggregate RPCs are re-stated WHOLE by every migration that touches them
(504 -> 537 -> 547, three programs in two months), so a source-text rail pinned to a file
name stops guarding live behaviour the moment the next one lands — silently, because the
old file still says what it always said. Every such rail resolves the file the same way,
from here.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def latest_definition(func: str) -> Path:
    """The highest-numbered migration whose text (re)defines `func`."""
    pat = re.compile(rf"create or replace function (?:public\.)?{func}\s*\(", re.IGNORECASE)
    hits = [p for p in MIGRATIONS.glob("*.sql") if pat.search(p.read_text(encoding="utf-8"))]
    assert hits, f"no migration defines {func}"
    return max(hits, key=lambda p: int(p.name.split("_", 1)[0]))
