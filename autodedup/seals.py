"""Committed split seals: the holdout a model was fitted on, kept in the repository.

`evaluate.split_seal` gives a group map an identity, and `harness fit` writes that map beside
the model it fits so a challenger can be scored on the INCUMBENT's holdout rather than on one
re-derived from its own merge edges (§9's feedback loop). That only works while the file
survives. W6 learned it does not: the scratch directory every earlier wave wrote its maps into
is a tmpfs, it was wiped, and the seal `ab2bd7ee…` that `w5_gold` and `w4f_gold` both name can
no longer be produced — so the shipped models cannot be re-evaluated on the split they were
measured on, only on a new one.

A seal is therefore a REPOSITORY artifact from here on: `autodedup/splits/<sha256>.json`, the
same `{listing_id: group}` object `fit` writes, named by the digest it hashes to (so the name
is checkable, not merely a label). `LOST_SEALS` records the ones that predate this rule, with
why they are gone; the census test allows a model to name a seal only when it resolves to a
committed file or is listed there.
"""

from __future__ import annotations

import json
from pathlib import Path

SPLITS_DIR: Path = Path(__file__).resolve().parent / "splits"

# The seals that were never committed and cannot be rebuilt: the maps lived only in the W4/W5
# scratch directories, which a tmpfs wipe took. Listed, not silently tolerated — a shipped
# model naming a seal nobody can produce is a real gap in the audit trail, and this is where
# the reason lives until each model is refitted on a committed seal.
LOST_SEALS: dict[str, str] = {
    "0a0186095f2cebf6a8d4cc78571c944d2c426d7852378b3064dea6aa833c9cc9": (
        "w4_gold's split (3741 listings, 859 groups). Fitted 2026-09 before split maps were "
        "committed; the map lived only under /tmp and was lost with the W4 scratch."
    ),
    "ab2bd7eee78d85da18f5ca588f86d0dce1a2f87b9efcbbaeace7a5db70f5e56f": (
        "w4f_gold's and w5_gold's shared split (3713 listings, 590 groups). Same cause: W5d "
        "wrote it beside the model in scratch, and the tmpfs wipe that opened W6 took it. W6 "
        "sealed a fresh split (37c8771f…) rather than pretend this one was recovered."
    ),
}

_HEX = set("0123456789abcdef")


def is_seal(value: str) -> bool:
    """A full sha256 in lower-case hex — the only shape a committed map is named by."""
    text = (value or "").strip().lower()
    return len(text) == 64 and set(text) <= _HEX


def path_for(seal: str) -> Path:
    return SPLITS_DIR / f"{(seal or '').strip().lower()}.json"


def committed(seal: str) -> bool:
    return is_seal(seal) and path_for(seal).is_file()


def known(seal: str) -> bool:
    """Committed, or explicitly recorded as lost."""
    return committed(seal) or (seal or "").strip().lower() in LOST_SEALS


def load(seal: str) -> dict[int, int]:
    path = path_for(seal)
    if not path.is_file():
        raise FileNotFoundError(
            f"no committed split map for seal {seal[:12]}…; commit one as {path} "
            "(the split_map.json `harness fit` writes beside the model)"
        )
    return read_map(path)


def read_map(path: Path) -> dict[int, int]:
    return {int(key): int(value)
            for key, value in json.loads(Path(path).read_text(encoding="utf-8")).items()}


def write_map(path: Path, groups: dict[int, int]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({str(key): groups[key] for key in sorted(groups)}, sort_keys=True),
        encoding="utf-8",
    )
    return path


def resolve(value: str) -> Path:
    """`--split-map` takes either a path or a committed seal, so a map that HAS been committed
    never has to be located by hand again."""
    path = Path(value)
    if path.is_file():
        return path
    if is_seal(value):
        return load_path_or_raise(value)
    raise FileNotFoundError(f"--split-map {value!r} is neither a file nor a committed seal")


def load_path_or_raise(seal: str) -> Path:
    path = path_for(seal)
    if not path.is_file():
        raise FileNotFoundError(
            f"no committed split map for seal {seal[:12]}…"
            + (f" (recorded as lost: {LOST_SEALS[seal.lower()]})" if seal.lower() in LOST_SEALS
               else "")
        )
    return path


def committed_seals() -> list[str]:
    if not SPLITS_DIR.is_dir():
        return []
    return sorted(path.stem for path in SPLITS_DIR.glob("*.json"))
