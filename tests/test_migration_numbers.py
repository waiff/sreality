"""Offline guard: every new migration number is unique.

`migrations/` accumulated ~38 duplicate numbers before this gate existed — an
artifact of parallel worktree development, where two branches each grabbed the
same "next" number and then both merged. Those are harmless (a migration number
is cited by nothing; the applied schema is the source of truth) and cannot be
renumbered now without editing already-applied history (the append-only rule),
so they are grandfathered below GRANDFATHER_MAX.

Going forward the number MUST be unique, because a silent collision is not always
harmless: `299_listing_description_enrichment_batches` merged to main but never
got applied — its number had already been taken in the live DB by the
out-of-band `299_phase0_anon_hardening`, so the enrichment batch tables were
simply missing until the collision was found and the file renumbered to 305.

This is the fast, offline floor: no DB, no imports — it runs in the normal
`pytest` job and fails a PR the moment two migrations share a number above the
grandfather line.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS = _ROOT / "migrations"

# Numbers at or below this line predate the gate and contain grandfathered
# duplicates (all long applied). Every migration ABOVE it must be unique. Do NOT
# lower this to silence a fresh collision — give the newer migration a free number.
GRANDFATHER_MAX = 304

_NUM_RE = re.compile(r"^(\d+)_.*\.sql$")


def _numbered_files() -> list[tuple[int, str]]:
    files: list[tuple[int, str]] = []
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        match = _NUM_RE.match(path.name)
        assert match, f"migration {path.name!r} must be named NNN_<slug>.sql"
        files.append((int(match.group(1)), path.name))
    return files


def test_every_migration_has_a_numeric_prefix():
    # _numbered_files() asserts the shape per file; a non-empty result also proves
    # the migrations/ directory was actually found (guards a wrong _ROOT).
    assert _numbered_files(), "no migrations discovered — is _ROOT correct?"


def test_no_duplicate_migration_number_above_grandfather_line():
    files = _numbered_files()
    counts = Counter(number for number, _ in files)
    collisions = {
        number: [name for num, name in files if num == number]
        for number, count in counts.items()
        if count > 1 and number > GRANDFATHER_MAX
    }
    assert not collisions, (
        f"duplicate migration number(s) above {GRANDFATHER_MAX}: {collisions}. "
        "Two branches grabbed the same number — give the newer migration the next "
        "free number (do not lower GRANDFATHER_MAX)."
    )
