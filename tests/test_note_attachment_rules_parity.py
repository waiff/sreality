"""The file picker's rules (frontend/src/lib/noteAttachments.ts) are the API's
(api/note_attachments.py): a drift would offer a file the API refuses, or hide one it takes."""

from __future__ import annotations

import re
from pathlib import Path

from api import note_attachments as na

_TS = (Path(__file__).resolve().parents[1] / "frontend/src/lib/noteAttachments.ts").read_text()


def _ts_list(name: str) -> set[str]:
    block = re.search(rf"export const {name} = \[(.*?)\] as const;", _TS, re.S)
    assert block, f"{name} not found in noteAttachments.ts"
    return set(re.findall(r"'([^']+)'", block.group(1)))


def _ts_number(name: str) -> int:
    expr = re.search(rf"export const {name} = ([0-9 *]+);", _TS)
    assert expr, f"{name} not found in noteAttachments.ts"
    total = 1
    for factor in expr.group(1).split("*"):
        total *= int(factor)
    return total


def test_the_picker_offers_exactly_the_extensions_the_api_takes():
    assert _ts_list("NOTE_ATTACHMENT_EXTENSIONS") == set(na.BY_EXTENSION)


def test_the_picker_falls_back_to_exactly_the_types_the_api_takes():
    assert _ts_list("NOTE_ATTACHMENT_MIME_TYPES") == set(na.ALLOWED)


def test_the_limits_match():
    assert _ts_number("NOTE_ATTACHMENT_MAX_BYTES") == na.MAX_BYTES
    assert _ts_number("NOTE_ATTACHMENT_MAX_FILES") == na.MAX_FILES_PER_NOTE
