"""WHICH listings the real-time lane is allowed to hold — as data, and as a SQL predicate.

The batch lane is scoped by construction: it runs over an exported cohort, and the cohort is
four blocks (ruling D1). The real-time lane has no artifact, so without this module its four
feeds claim the arrivals of the WHOLE corpus — ~178,000 new listings in 30 days against a
store the operator pays for by the megabyte. `rt_scope` is therefore a REQUIRED lane setting
rather than a tuning knob: an empty, separator-only or missing scope is a hard error, a scope
with no blocks is not constructible at all (D1), and a whole-corpus run has to be spelled `all`
AND fit the storage guard (`incremental_lane.storage_guard`). A PASS goes further still: its
scope is the one `rt_seed` PERSISTED, and a dispatch argument that differs is a re-scope the
operator has to ask for by name (`resolve_pass_scope`, D2).

Two grains, and they are the two the cohort already uses: `obec` is
`listing_location.obec_kod` (a town) and `cast_obce` is `cast_obce_kod` (a quarter). The
DEFAULT is derived from `cohort.BLOCKS` rather than retyped, which is also why the assembled
Praha negative control is absent from it: that block is a RULE evaluated at export time over
address groups, it has no natural arrival feed, and nothing in the corpus marks a new listing
as belonging to it.

**Why the predicate is index-served without an index of its own.** `listing_location` carries
a btree on `(obec_kod, granularity)` and none at all on `cast_obce_kod`, and ruling D8 forbids
DDL on a shared table — so a feed that filtered the corpus BY the scope would seq-scan 872k
rows every ten minutes. It doesn't: each feed reads a bounded WINDOW off its own cursor index
and resolves the scope for that window's ids through `listing_location_pkey` (`EXPLAIN`: Index
Scan, one probe per window row). The scope is never the driving side, so the plan cannot
degrade as the scope list grows, and the cursor advances over the WINDOW rather than over the
survivors — which is what keeps a feed moving through a corpus that is 99.4% out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from autodedup import cohort

GRAINS: tuple[str, ...] = ("obec", "cast_obce")
ALL: str = "all"

# cohort grain -> scope grain. `assembled` is deliberately unmapped (see the module docstring).
_GRAIN_OF: dict[str, str] = {"town": "obec", "quarter": "cast_obce"}


class ScopeError(ValueError):
    """A scope that cannot be honoured. The lane turns this into a non-zero exit."""


@dataclass(frozen=True, slots=True)
class ScopeBlock:
    grain: str
    code: int

    def as_json(self) -> dict[str, Any]:
        return {"grain": self.grain, "code": self.code}


@dataclass(frozen=True, slots=True)
class Scope:
    """A scope with no blocks is not "everything" and not "nothing" — it is the NULL scope W9c
    shipped, whose drift sweep reports every row of the store as departed (D1). It cannot be
    built: `all` is spelled `whole_corpus`, and everything else needs at least one block."""

    blocks: tuple[ScopeBlock, ...] = ()
    whole_corpus: bool = False

    def __post_init__(self) -> None:
        if not self.whole_corpus and not self.blocks:
            raise ScopeError(
                "an empty scope holds nothing and would retire the whole store — spell "
                f"{ALL!r} for the corpus, or name at least one block")

    def key(self) -> tuple[bool, tuple[tuple[str, int], ...]]:
        """Identity, independent of the order the blocks were spelled in."""
        return (bool(self.whole_corpus),
                tuple(sorted((b.grain, b.code) for b in self.blocks)))

    @property
    def obec_codes(self) -> list[int]:
        return sorted({b.code for b in self.blocks if b.grain == "obec"})

    @property
    def cast_obce_codes(self) -> list[int]:
        return sorted({b.code for b in self.blocks if b.grain == "cast_obce"})

    def params(self) -> dict[str, Any]:
        """The three parameters every scoped statement takes."""
        return {
            "all_scope": bool(self.whole_corpus),
            "obec": self.obec_codes,
            "cast_obce": self.cast_obce_codes,
        }

    def as_json(self) -> Any:
        return ALL if self.whole_corpus else [b.as_json() for b in self.blocks]

    def label(self) -> str:
        if self.whole_corpus:
            return ALL
        return " ".join(f"{b.grain}:{b.code}" for b in self.blocks)

    def contains(self, obec_kod: Any, cast_obce_kod: Any) -> bool:
        """The same predicate the SQL spells, for the seed backfill and the replay."""
        if self.whole_corpus:
            return True
        try:
            obec = int(obec_kod) if obec_kod is not None else None
        except (TypeError, ValueError):
            obec = None
        try:
            cast_obce = int(cast_obce_kod) if cast_obce_kod is not None else None
        except (TypeError, ValueError):
            cast_obce = None
        return ((obec is not None and obec in set(self.obec_codes))
                or (cast_obce is not None and cast_obce in set(self.cast_obce_codes)))

    def holds(self, listing: Any) -> bool:
        location = getattr(listing, "location", None)
        return self.contains(getattr(location, "obec_kod", None),
                             getattr(location, "cast_obce_kod", None))


# The three trial blocks, derived from the cohort rather than retyped.
DEFAULT_SCOPE: Scope = Scope(tuple(
    ScopeBlock(_GRAIN_OF[block.grain], int(block.code))
    for block in cohort.BLOCKS if block.grain in _GRAIN_OF))

# What a whole-corpus generation would COST, measured rather than feared (W9c). 17.596 index
# keys a listing (measured over the scoped cohort) across 872,604 listings is 15.4M `fp_key`
# rows; at the per-row on-disk costs this schema itself shows — 210 B for a narrow keyed row,
# 2,869 B for a pair row (136 MB / 47,523 live rows) — seeding the corpus costs ~9.6 GB and its
# arrivals add ~3.4 GB every 30 days (5,940 new listings a day x 17.6 keys x 5.34 stored
# pairs). 17,000 MB is that seed plus two months. `rt_scope = all` is refused unless
# `rt_max_schema_mb` is at least this, so "run the whole corpus" and "pay for the whole corpus"
# are ONE decision and not two.
CORPUS_PROJECTION_MB: int = 17000


def parse_scope(raw: Any) -> Scope:
    """`"obec:563510 cast_obce:490245"`, a JSON list of `{grain, code}`, or `"all"`.

    Never a default: a caller that has nothing to pass calls `resolve_scope`, which is where
    the default and the hard error live."""
    if raw is None:
        raise ScopeError("rt_scope is required (an empty scope would claim the whole corpus)")
    if isinstance(raw, Mapping):
        raw = raw.get("blocks", raw.get("value", raw.get("scope")))
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ScopeError("rt_scope is empty (an empty scope would claim the whole corpus)")
        if text.lower() == ALL:
            return Scope((), whole_corpus=True)
        blocks = _dedupe(_parse_token(token) for token in text.replace(",", " ").split())
        if not blocks:
            raise ScopeError(
                f"rt_scope {raw!r} names no block (separators only) — an empty scope would "
                "retire the whole store")
        return Scope(tuple(blocks))
    if isinstance(raw, Sequence):
        entries = list(raw)
        if not entries:
            raise ScopeError("rt_scope is empty (an empty scope would claim the whole corpus)")
        blocks: list[ScopeBlock] = []
        for entry in entries:
            if isinstance(entry, str):
                if not entry.strip():
                    raise ScopeError("rt_scope entry is empty")
                blocks.append(_parse_token(entry))
                continue
            if not isinstance(entry, Mapping):
                raise ScopeError(f"rt_scope entry must be an object or a token: {entry!r}")
            blocks.append(_block(entry.get("grain"), entry.get("code")))
        kept = _dedupe(blocks)
        if not kept:
            raise ScopeError(f"rt_scope {raw!r} names no block")
        return Scope(tuple(kept))
    raise ScopeError(f"rt_scope must be a list, a token string or {ALL!r}: {raw!r}")


def _parse_token(token: str) -> ScopeBlock:
    grain, _, code = token.partition(":")
    if not _:
        raise ScopeError(f"rt_scope token must be grain:code, got {token!r}")
    return _block(grain, code)


def _block(grain: Any, code: Any) -> ScopeBlock:
    name = str(grain or "").strip()
    if name not in GRAINS:
        raise ScopeError(f"unknown scope grain {name!r}; known: {', '.join(GRAINS)}")
    try:
        value = int(str(code).strip())
    except (TypeError, ValueError):
        raise ScopeError(f"scope code must be an integer, got {code!r}") from None
    return ScopeBlock(name, value)


def _dedupe(blocks: Iterable[ScopeBlock]) -> list[ScopeBlock]:
    seen: set[tuple[str, int]] = set()
    out: list[ScopeBlock] = []
    for block in blocks:
        key = (block.grain, block.code)
        if key in seen:
            continue
        seen.add(key)
        out.append(block)
    return out


def resolve_scope(arg: Any = None, setting: Any = None) -> Scope:
    """The lane's resolution order: the dispatch argument, then the `rt_scope` settings row,
    then the three trial blocks. A value that is PRESENT and empty is an error, never a
    silent whole-corpus run."""
    if arg is not None and str(arg).strip() != "":
        return parse_scope(arg)
    if setting is not None:
        return parse_scope(setting)
    return DEFAULT_SCOPE


def resolve_pass_scope(arg: Any = None, setting: Any = None,
                      rescope: bool = False) -> tuple[Scope, bool]:
    """A PASS's scope: the persisted one, and a dispatch argument only as an explicit RE-SCOPE.

    `resolve_scope` is the SEED's resolution order, where an argument chooses the scope the
    generation is cut for. A pass is the other way round (D2): the `rt_scope` row `rt_seed`
    wrote is what the store was built under, and a narrower argument is DESTRUCTIVE — the drift
    sweep retires everything the argument leaves out, one dispatch, no undo, and the next
    scheduled pass reads the wider row again with nothing to re-add the retired listings. So a
    differing argument is refused unless `rt_rescope=true` says so, and a rescope is PERSISTED
    rather than applied for one pass. A missing row under a seeded generation is a hard error:
    falling back to the default would be the same destruction with no argument at all."""
    if setting is None:
        raise ScopeError(
            "this generation has a frozen calibration but no rt_scope row — the row IS the "
            "scope its store was built under. Re-seed it (`--mode rt_seed`) or write the row; "
            "refusing to fall back to the default scope")
    persisted = parse_scope(setting)
    if arg is None or str(arg).strip() == "":
        return persisted, False
    asked = parse_scope(arg)
    if asked.key() == persisted.key():
        return persisted, False
    if not rescope:
        raise ScopeError(
            f"the dispatch argument {asked.label()!r} is not the scope this generation was "
            f"seeded with ({persisted.label()!r}); everything outside it would be RETIRED. "
            "Pass rt_rescope=true to move the generation's scope, or re-seed")
    return asked, True


def guard_agrees(scope: Scope, max_schema_mb: float) -> bool:
    """The second half of the whole-corpus gate: `all` also needs a budget that could hold it."""
    if not scope.whole_corpus:
        return True
    return float(max_schema_mb) >= float(CORPUS_PROJECTION_MB)
