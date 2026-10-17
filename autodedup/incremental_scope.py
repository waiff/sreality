"""WHICH listings the real-time lane is allowed to hold — as data, and as a SQL predicate.

The batch lane is scoped by construction: it runs over an exported cohort, and the cohort is
four blocks (ruling D1). The real-time lane has no artifact, so without this module its four
feeds claim the arrivals of the WHOLE corpus — ~178,000 new listings in 30 days against a
store the operator pays for by the megabyte. `rt_scope` is therefore a REQUIRED lane setting
rather than a tuning knob: an empty, separator-only or missing scope is a hard error, a scope
with no blocks is not constructible at all (W9d-1), and a whole-corpus run has to be spelled `all`
AND fit the storage guard (`incremental_lane.storage_guard`). A PASS goes further still: its
scope is the one `rt_seed` PERSISTED and nothing else (`incremental_lane.pass_scope`, W9d-2);
moving it is a re-seed.

Three grains: the two the cohort already uses — `obec` is `listing_location.obec_kod` (a
town) and `cast_obce` is `cast_obce_kod` (a quarter) — and `okres`, `okres_kod` (a district,
E961), so a kraj is seeded as its dozen districts rather than its thousand towns. The DEFAULT is
derived from `cohort.BLOCKS` rather than retyped, which is also why the assembled Praha negative
control is absent from it: that block is a RULE evaluated at export time over address groups, it
has no natural arrival feed, and nothing in the corpus marks a new listing as belonging to it.

**Why the predicate is index-served without an index of its own.** `listing_location` carries a
btree on `(obec_kod, granularity)` and none at all on `cast_obce_kod` or `okres_kod`, and
ruling D8 forbids DDL on a shared table — so a feed that filtered the corpus BY the scope would
seq-scan 872k rows every ten minutes. It doesn't: each feed reads a bounded WINDOW off its own
cursor index and resolves the scope for that window's ids through `listing_location_pkey`
(`EXPLAIN`: Index Scan, one probe per window row). The scope is never the driving side, so the
plan cannot degrade as the scope list grows, and the cursor advances over the WINDOW rather
than over the survivors — which is what keeps a feed moving through a corpus that is 99.4% out
of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from autodedup import cohort

GRAINS: tuple[str, ...] = ("obec", "cast_obce", "okres")
ALL: str = "all"
# cohort (and apply scope) grain -> scope grain: English there, RÚIAN here (E939). `assembled` is
# deliberately unmapped (see the module docstring); `district` has no cohort block.
GRAIN_OF: dict[str, str] = {"town": "obec", "quarter": "cast_obce", "district": "okres"}


class ScopeError(ValueError):
    """A scope that cannot be honoured. The lane turns this into a non-zero exit."""


@dataclass(frozen=True, slots=True)
class ScopeBlock:
    grain: str
    code: int

    @property
    def key(self) -> str:
        """`grain:code`, the spelling the snapshot, the scan ledger and the walk key a block by."""
        return f"{self.grain}:{self.code}"

    def as_json(self) -> dict[str, Any]:
        return {"grain": self.grain, "code": self.code}


@dataclass(frozen=True, slots=True)
class Scope:
    """A scope with no blocks is not "everything" and not "nothing" — it is the NULL scope W9c
    shipped, whose drift sweep reports every row of the store as departed (W9d-1). It cannot be
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

    @property
    def okres_codes(self) -> list[int]:
        return sorted({b.code for b in self.blocks if b.grain == "okres"})

    def params(self) -> dict[str, Any]:
        """The four parameters every scoped statement takes."""
        return {
            "all_scope": bool(self.whole_corpus),
            "obec": self.obec_codes,
            "cast_obce": self.cast_obce_codes,
            "okres": self.okres_codes,
        }

    def as_json(self) -> Any:
        return ALL if self.whole_corpus else [b.as_json() for b in self.blocks]

    def label(self) -> str:
        if self.whole_corpus:
            return ALL
        return " ".join(f"{b.grain}:{b.code}" for b in self.blocks)


# The three trial blocks, derived from the cohort rather than retyped.
DEFAULT_SCOPE: Scope = Scope(tuple(
    ScopeBlock(GRAIN_OF[block.grain], int(block.code))
    for block in cohort.BLOCKS if block.grain in GRAIN_OF))

# What a whole-corpus generation would COST, on the only per-row costs this schema can actually
# be MEASURED for (W9d corrects W9c's arithmetic note, not its headline). Neither `fp_key` nor
# `rt_fp` exists yet — 539 is unapplied — so their cost is carried from the narrowest table that
# does: `autodedup.cluster_members`, 1,695,744 B over 7,162 rows = **236.8 B a row** with its
# index and its bloat in it; a pair row is measured directly at **2,869 B** (136,347,648 B /
# 47,523 rows). W9c scaled a "representative tuple" by 3.53x instead and could not reproduce its
# own two numbers — the multiplier is not a constant, it is that same measured 236.8 B over
# whichever tuple is called representative. At 17.596 index keys a listing across 872,604
# listings (15.4M `fp_key` rows) and 2.67 stored pairs a listing, seeding the corpus costs
# **9.8-10.2 GB** and its arrivals add **3.3 GB every 30 days** (112-114 MB a day). 17,000 MB is
# that seed plus about two months of it. `rt_scope = all` is refused unless `rt_max_schema_mb`
# is at least this, so "run the whole corpus" and "pay for the whole corpus" are ONE decision
# and not two. (`/home/hejtm/autodedup-artifacts/w9d/projection_correction.json`.)
CORPUS_PROJECTION_MB: int = 17000


def parse_scope(raw: Any) -> Scope:
    """`"obec:563510 cast_obce:490245 okres:3209"`, a JSON list of `{grain, code}`, or `"all"`.

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


def contains(outer: ScopeBlock, inner: ScopeBlock, parents: Mapping[int, int],
             districts: Mapping[int, Sequence[int]]) -> bool:
    """Whether every advert `inner` lists is one `outer` lists (E964), read off the RÚIAN
    register the walks ride: a block holds itself, an okres its obce (`districts`) and their
    quarters, a town its quarters (`parents`: quarter -> its obec). Nothing else holds anything:
    Praha is no okres, and there is no kraj grain."""
    if (outer.grain, outer.code) == (inner.grain, inner.code):
        return True
    if outer.grain == "okres":
        obce = {int(code) for code in districts.get(int(outer.code), ())}
        if inner.grain == "obec":
            return int(inner.code) in obce
        if inner.grain == "cast_obce":
            return parents.get(int(inner.code)) in obce
        return False
    return (outer.grain == "obec" and inner.grain == "cast_obce"
            and parents.get(int(inner.code)) == int(outer.code))


@dataclass(frozen=True, slots=True)
class Widening:
    """What a seed over a seeded generation would do to its scope (E964): the blocks it keeps,
    the blocks it adds, each old block a wider one REPLACES (`replaced`: old key -> new key) and
    the old blocks no new block holds (`dropped`). A widening drops nothing and adds something."""

    kept: tuple[ScopeBlock, ...]
    added: tuple[ScopeBlock, ...]
    replaced: dict[str, str]
    dropped: tuple[ScopeBlock, ...]


def widening(old: Scope, new: Scope, parents: Mapping[int, int],
             districts: Mapping[int, Sequence[int]]) -> Widening:
    """`new` read against `old` (E964). `parents` holds the quarters of BOTH scopes, `districts`
    the okresy of the new one. A block of `old` that `new` names again is kept; one a block of
    `new` contains is replaced by it — its adverts are that block's now, so the walk lists the
    rest of the wider block and the rows the store holds stay; one no block of `new` contains is
    dropped, which a widening never does (a dropped block is a narrower scope: a rebuild)."""
    if old.whole_corpus or new.whole_corpus:
        raise ScopeError(f"{ALL!r} widens nothing: a whole-corpus generation is a rebuild "
                         "(and a budget decision, CORPUS_PROJECTION_MB)")
    old_keys = {block.key for block in old.blocks}
    new_keys = {block.key for block in new.blocks}
    kept = tuple(block for block in new.blocks if block.key in old_keys)
    added = tuple(block for block in new.blocks if block.key not in old_keys)
    replaced: dict[str, str] = {}
    dropped: list[ScopeBlock] = []
    for block in old.blocks:
        if block.key in new_keys:
            continue
        holder = next((outer for outer in added
                       if contains(outer, block, parents, districts)), None)
        if holder is None:
            dropped.append(block)
        else:
            replaced[block.key] = holder.key
    return Widening(kept, added, replaced, tuple(dropped))


def guard_agrees(scope: Scope, max_schema_mb: float) -> bool:
    """The second half of the whole-corpus gate: `all` also needs a budget that could hold it."""
    if not scope.whole_corpus:
        return True
    return float(max_schema_mb) >= float(CORPUS_PROJECTION_MB)
