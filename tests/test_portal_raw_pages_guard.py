"""CI guard: nothing in the repo may delete from portal_raw_pages.

Location-data program, W0 item 0o. The archive (447,164 rows / 14 GB on 2026-08-10;
oldest surviving page per source == that portal's onboarding date, i.e. nothing has
ever been pruned) is the only surviving copy of several portals' best location
signal — remax's subject address line, ceskereality's accented street in <title>,
idnes's "no exact address" disclaimer, realitymix's breadcrumb chain. Portals do not
serve delisted pages again, so a deletion here is permanent data loss.

Migration 099's header comment ("rows are safe to delete once parsed") is SUPERSEDED
by this policy. The off-database copy lives in R2 under backups/portal-raw-pages/
(scripts/export_portal_raw_pages_archive.py, workflow export_raw_pages_archive.yml).

Scope limits (a linter, not a formal verifier): this scans migration SQL and Python
source for statements that target portal_raw_pages with DELETE / TRUNCATE / DROP
TABLE. It cannot catch dynamically assembled SQL. If a retention pruner is ever
genuinely wanted, it must land together with a verified R2 export of the rows it
prunes — remove this guard only in that same PR, with operator sign-off.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_SCAN_DIRS = ("migrations", "scraper", "toolkit", "api", "scripts")
_SCAN_SUFFIXES = {".sql", ".py"}

_FORBIDDEN = re.compile(
    r"(?is)\b(?:delete\s+from|truncate(?:\s+table)?(?:\s+only)?|"
    r"drop\s+table(?:\s+if\s+exists)?)\s+(?:public\.)?portal_raw_pages\b"
)


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for d in _SCAN_DIRS:
        root = REPO / d
        if not root.is_dir():
            continue
        files.extend(
            p
            for p in root.rglob("*")
            if p.suffix in _SCAN_SUFFIXES and p.is_file()
        )
    return files


def test_no_deletion_of_portal_raw_pages() -> None:
    offenders: list[str] = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _FORBIDDEN.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(REPO)}:{line_no}: {match.group(0)!r}")
    assert not offenders, (
        "portal_raw_pages is preservation substrate for the location re-mine wave; "
        "deleting from it destroys the only copy of delisted pages' location signal. "
        "See this test's docstring for the policy and the R2 export path.\n"
        + "\n".join(offenders)
    )
