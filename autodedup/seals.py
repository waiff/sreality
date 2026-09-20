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

A seal is also SPENT once a search has read its test side. W8a ran a 1,476-candidate rule
search on `37c8771f…`, so g6's sealed column confirms a rule chosen elsewhere and cannot
adjudicate between rules (D23 ii). `SPENT_SEALS` records that, with what spent it — the file
stays, because an incumbent must still be re-measurable on the split it was fitted on; what is
gone is its power to DECIDE. A new choice needs a new seal.

The map alone does not fix the holdout: `evaluate.split_of` hashes `<seed>:<group>`, so two
seeds over one map are two different partitions under one name. A committed map therefore
carries the seed it was partitioned with, in the object form `{"seed": n, "groups": {...}}`;
the bare `{listing_id: group}` form predates that and reads as the legacy `SPLIT_SEED`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

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

# A seal whose TEST side a search has already read. The map is still committed — the incumbent
# it sealed must stay re-measurable — but a number taken on it can only ever confirm a choice
# made elsewhere, so the next choice needs a fresh seal. Entries are append-only.
SPENT_SEALS: dict[str, str] = {
    "37c8771fda6b06db2ead790fcf7728e0ccb0e80c60cad5358c2905ed39be52cc": (
        "w6_gold's split (4569 listings, 691 groups), sealed 2026-09 with SPLIT_SEED "
        "20260916. SPENT by W8a's 1,476-candidate rule search, which read the test side while "
        "choosing E63 (D23 ii, C5); W8's own verification then read it again for the g6 "
        "columns. Every g6 sealed number CONFIRMS a rule chosen on dev — none adjudicates. "
        "W9 sealed fb9df2ea… over the same cohort with seed 20260922 to make a choice again."
    ),
    "00e2cb2fe4fe1e8ee9886729ef3420ebaa6aec059934b20e30630751500032b4": (
        "W11's fresh seal (4456 listings, 664 groups), seed 20260923, cut with the honest "
        "arm's K-B families unioned in. SPENT by W11's verification, which opened it ONCE to "
        "read four arms and rule D30: the honest clock gains 47 sealed labelled duplicates "
        "over g6 and loses 0, and the E85 family guard is inert against the same arm without "
        "it (gained 0, lost 0). A later number on this split confirms; it cannot decide. The "
        "next rule read at family grain owes a fresh seal cut the same way. W12 then found "
        "that this map is a FIXPOINT — the same cohort, the same two arms and the same "
        "construction reproduce it to the listing — so its fresh holdout had to be a fresh "
        "SEED, and `seal_id` now names a seed-bearing seal by map AND seed rather than by the "
        "map alone (the W12 seal ebc141fa… is this map under seed 20260925)."
    ),
    "fb9df2ea9fd773bf0eda256d00894924ba4b8491cc7559181f2c48da75e59884": (
        "W9's fresh seal (4456 listings, 663 groups), seed 20260922. SPENT because it was read "
        "TWICE: once for the refused g7 refit (D25) and once for W10's verification, which is "
        "what refuted the honest-clock + carrier candidate (D28 v). Every later number on it "
        "confirms a choice made elsewhere. W11 sealed a fresh split over the same cohort, with "
        "seed 20260923 and the honest arm's K-B FAMILIES unioned in — a family that straddles "
        "the split puts one developer chain on both sides of a holdout that exists to judge a "
        "family-grain guard."
    ),
}

_HEX = set("0123456789abcdef")


def seal_id(groups: "dict[int, int] | Mapping[int, int]", seed: int | None = None) -> str:
    """The name a split map is filed under — the map alone, or the map AND its seed.

    W12 found the hole in "a seal is named by its map". Its cohort, its arms and its
    construction reproduce W11's map to the listing, so the only fresh holdout available over
    that cohort is a fresh SEED — and under a map-only name that seal could be committed only
    by overwriting a file the program has registered as SPENT. `split_of` hashes
    `<seed>:<group>`, so a map under two seeds is two holdouts; a seed-bearing seal is
    therefore named by both, and a map-only name stays exactly what it was for the four seals
    cut before this rule."""
    import hashlib

    digest = hashlib.sha256()
    if seed is not None:
        digest.update(f"seed:{int(seed)}\n".encode("utf-8"))
    for item in sorted(groups):
        digest.update(f"{item}:{groups[item]}\n".encode("utf-8"))
    return digest.hexdigest()


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


def spent(seal: str) -> str | None:
    """Why this seal can no longer DECIDE, or None. A spent seal still measures an incumbent."""
    return SPENT_SEALS.get((seal or "").strip().lower())


def load(seal: str) -> dict[int, int]:
    path = path_for(seal)
    if not path.is_file():
        raise FileNotFoundError(
            f"no committed split map for seal {seal[:12]}…; commit one as {path} "
            "(the split_map.json `harness fit` writes beside the model)"
        )
    return read_map(path)


def _raw(path: Path) -> dict[str, object]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _groups_of(raw: dict[str, object]) -> dict[str, object]:
    """Both shapes: the legacy bare map, and `{"seed": n, "groups": {...}}`."""
    inner = raw.get("groups") if isinstance(raw.get("groups"), dict) else None
    return inner if inner is not None else raw  # type: ignore[return-value]


def read_map(path: Path) -> dict[int, int]:
    return {int(key): int(value) for key, value in _groups_of(_raw(Path(path))).items()}


def read_seed(path: Path) -> int | None:
    """The seed this map was partitioned with, or None for a legacy bare map."""
    raw = _raw(Path(path))
    value = raw.get("seed") if isinstance(raw.get("groups"), dict) else None
    return None if value is None else int(value)  # type: ignore[arg-type]


def seed_for(seal: str) -> int | None:
    path = path_for(seal)
    return read_seed(path) if path.is_file() else None


def write_map(path: Path, groups: dict[int, int], seed: int | None = None) -> Path:
    """The map, and the seed that partitions it — a map without one names no holdout."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, object] = {str(key): groups[key] for key in sorted(groups)}
    payload: dict[str, object] = body if seed is None else {"seed": int(seed), "groups": body}
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
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
