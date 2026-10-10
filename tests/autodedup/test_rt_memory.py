"""E949: no memo of a body or a token outlives the pass that read it.

2026-10-10: the worker died seven times in the S1-widened build, each process on about its sixth
pass whatever it claimed, because it carried 0.6–2 GB from pass to pass. Pinned here, the
engine's half: every process-level memo of what a pass read (the readers cached on a body, by
module, E282's shingle sets, the token hashes and SimHash lanes) is reported at the pass's end
and then emptied, whatever ended the pass, once its lease is released; every cache the engine
builds is a registered body reader or named here with its bound; a raised pass lets go of its
raise, so the lane's drop of it frees the pass at once; the shingle memo is keyed on the body
itself, so a same-length edit is never served the old body's set; and the CLIP top-up encodes
each FACT_CHUNK slice as it arrives, holding no slice's text when the next is read, with the
output the concatenating read gave. The worker's half (the hand-backs, the two arenas) is pinned
in `tests/scraper/test_realtime_worker_autodedup.py`.
"""

from __future__ import annotations

import ast
import gc
import inspect
import random
import weakref
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from autodedup import (
    body_align, incremental_lane, indistinguishable, normalize, rt_lease, text_facts,
)
from autodedup.dataset import Listing
from autodedup.export import encode_clip
from autodedup.export_sql import COHORT_CLIP_SQL
from autodedup.incremental_lane import SqlFacts, run_incremental
from autodedup.normalize import fold, normalize_folded, shingles
from tests.autodedup import lane_world
from tests.autodedup.fake_pg import FakePg

MEMOS = ("text_facts", "body_align", "shingles", "tokens")
BODIES = (
    "Prodej bytu 2+kk ve 3. patre cihloveho domu, sklep a balkon, klidna lokalita u parku. " * 3,
    "Pronajem bytu 3+1 v 5. podlazi, po rekonstrukci, garaz a terasa, vyhled na reku Nisu. " * 3,
)


@pytest.fixture(autouse=True)
def _empty_memos() -> None:
    incremental_lane.forget_bodies()


def _read(texts: tuple[str, ...]) -> None:
    """What a pass's scoring reads of a body: a text_facts reader, body_align's tokens, D43's
    shingle set (hashing its tokens) and a SimHash."""
    for text in texts:
        text_facts.printed_floors(text, True)
        body_align.tokens(text)
        indistinguishable._text_shingles(Listing(id=1, block="", description=text))
        normalize.simhash64(normalize.tokens(text))


def _seeded(tmp_path: Path) -> FakePg:
    world = lane_world.world()
    lane_world.seed_lane(world, tmp_path)
    incremental_lane.forget_bodies()
    return world


def _spy_on_the_clear(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, int]]:
    """What the memos hold at the moment the pass's end empties them."""
    at_clear: list[dict[str, int]] = []
    forget = incremental_lane.forget_bodies

    def spy() -> None:
        at_clear.append(incremental_lane.memo_entries())
        forget()

    monkeypatch.setattr(incremental_lane, "forget_bodies", spy)
    return at_clear


def _reads_then(monkeypatch: pytest.MonkeyPatch, then: Any) -> None:
    """The pass reads two bodies as it scores, then does `then`."""
    def scoring(*args: Any, **kwargs: Any) -> Any:
        _read(BODIES)
        return then(*args, **kwargs)

    monkeypatch.setattr(incremental_lane, "run_pass_bounded", scoring)


def _all_empty() -> bool:
    return (incremental_lane.memo_entries() == dict.fromkeys(MEMOS, 0)
            and not indistinguishable._SHINGLE_MEMO
            and not normalize._token_hashes and not normalize._token_lanes)


# ------------------------------------------------------------------ (c) the memos


def test_a_pass_reports_its_memos_and_then_leaves_them_empty(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _seeded(tmp_path)
    _reads_then(monkeypatch, incremental_lane.run_pass_bounded)
    at_clear = _spy_on_the_clear(monkeypatch)

    out = run_incremental(lambda: world)

    assert out["aborted"] == "" and out.get("skipped") is None
    assert at_clear == [out["memo_entries"]], "reported as they stood when they were emptied"
    assert set(out["memo_entries"]) == set(MEMOS)
    assert out["memo_entries"]["shingles"] == len(BODIES)
    assert out["memo_entries"]["body_align"] >= len(BODIES)
    assert out["memo_entries"]["text_facts"] >= len(BODIES)
    assert out["memo_entries"]["tokens"] >= len(set(normalize.tokens(BODIES[0])))
    assert _all_empty()


# What a registered reader is handed beside the body, by its parameter's annotation.
_ARGUMENTS: dict[str, Any] = {"bool": False, "str": "Kolbenova", "float | None": None,
                              "frozenset[str]": frozenset()}


def _read_with_every_reader(text: str) -> None:
    for reader in text_facts.BODY_READERS:
        _, *rest = inspect.signature(reader).parameters.values()
        reader(text, *(_ARGUMENTS[param.annotation] for param in rest))


def test_every_registered_reader_is_counted_and_then_emptied() -> None:
    """Each registered reader holds a reading of the body: the count takes in every one, by
    module, and the end of a pass empties every one, the first and the last registered too."""
    _read_with_every_reader(BODIES[0])
    held = {reader: reader.cache_info().currsize for reader in text_facts.BODY_READERS}
    assert held and all(held.values()), "every reader holds a reading"
    by_module: Counter[str] = Counter()
    for reader, size in held.items():
        by_module[reader.__module__.rsplit(".", 1)[-1]] += size

    counted = incremental_lane.memo_entries()
    incremental_lane.forget_bodies()

    assert {module: counted[module] for module in by_module} == dict(by_module)
    assert [reader.__name__ for reader in text_facts.BODY_READERS
            if reader.cache_info().currsize] == []


class _Died(Exception):
    pass


def _dies(*_args: Any, **_kwargs: Any) -> Any:
    raise _Died("the pass raised")


def test_a_pass_that_raises_leaves_no_memo_either(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _seeded(tmp_path)
    _reads_then(monkeypatch, _dies)
    at_clear = _spy_on_the_clear(monkeypatch)

    with pytest.raises(_Died):
        run_incremental(lambda: world)

    assert at_clear and at_clear[0]["shingles"] == len(BODIES) and at_clear[0]["tokens"]
    assert _all_empty()


def test_a_skipped_pass_empties_them_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever ends a pass: a seed holding the lease included."""
    from datetime import timedelta

    world = _seeded(tmp_path)
    world.lease[incremental_lane.LANE_NAME] = {
        "holder": "rt_seed:gh:7:7", "expires_at": world.now + timedelta(minutes=10)}
    _read(BODIES)

    out = run_incremental(lambda: world)

    assert out["skipped"] == "leased"
    assert _all_empty()


def test_the_lease_is_released_before_the_memos_are_emptied(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Emptying up to 56 x 65,536 readings takes time, and the next holder may be waiting on
    the lease."""
    world = _seeded(tmp_path)
    order: list[str] = []
    release, forget = rt_lease.release_after, incremental_lane.forget_bodies

    def releasing(*args: Any, **kwargs: Any) -> bool:
        order.append("release")
        return release(*args, **kwargs)

    def forgetting() -> None:
        order.append("forget")
        forget()

    monkeypatch.setattr(rt_lease, "release_after", releasing)
    monkeypatch.setattr(incremental_lane, "forget_bodies", forgetting)

    out = run_incremental(lambda: world)

    assert out["aborted"] == "" and order == ["release", "forget"]


class _WorkingSet:
    """What a pass holds when a statement of it times out: facts, fingerprints, pairs."""

    def __init__(self) -> None:
        self.rows = ["z" * 700 + str(i) for i in range(1000)]


def test_a_raised_pass_is_freed_the_moment_the_lane_drops_its_raise(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_incremental` notes the raise for its lease's release (`original`) and lets go of it
    before it leaves. Kept, the raise's traceback held the frame that held the raise: a cycle
    that kept every local of the pass alive after the lane dropped the raise, until the next
    full collection (off here, as on CPython 3.12 when none is due)."""
    world = _seeded(tmp_path)
    held: list[weakref.ref[_WorkingSet]] = []

    def times_out(*_args: Any, **_kwargs: Any) -> Any:
        working = _WorkingSet()
        held.append(weakref.ref(working))
        raise _Died("canceling statement due to statement timeout")

    monkeypatch.setattr(incremental_lane, "run_pass_bounded", times_out)
    gc.collect()
    gc.disable()
    try:
        try:
            run_incremental(lambda: world)
        except _Died:
            pass  # the lane loop logs it and drops it
        alive_after_drop = held[0]() is not None
    finally:
        gc.enable()

    assert held and not alive_after_drop


# Every cache the engine builds with functools that is not a body reader's, and its bound.
CACHES_OUTSIDE_THE_REGISTRY: dict[str, str] = {
    "text_facts.body_cached": "builds each body reader's cache and registers it (BODY_READERS)",
    "text_facts._house_number_patterns": "keyed on a street, not a body: 4,096 patterns at most",
}
_FUNCTOOLS_CACHES = frozenset({"lru_cache", "cache", "cached_property"})


def _callee(node: ast.AST) -> str | None:
    """The name a decorator or a call goes by: `lru_cache` for `@lru_cache(maxsize=9)`,
    `@functools.lru_cache` and `lru_cache(maxsize=9)(reader)` alike."""
    while isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else None


def _cache_sites(path: Path) -> set[str]:
    """`module.function` of every function a file caches with functools, by a decorator on it
    or by a call inside it."""
    sites: set[str] = set()

    def visit(node: ast.AST, owner: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = node.name
            if any(_callee(deco) in _FUNCTOOLS_CACHES for deco in node.decorator_list):
                sites.add(f"{path.stem}.{owner}")
        if isinstance(node, ast.Call) and _callee(node) in _FUNCTOOLS_CACHES:
            sites.add(f"{path.stem}.{owner}")
        for child in ast.iter_child_nodes(node):
            visit(child, owner)

    visit(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    return sites


def test_every_cache_in_the_engine_is_a_registered_reader_or_named_here() -> None:
    """A reader cached outside `body_cached` would never be emptied. The census reads every
    source file of the engine for a functools cache (`lru_cache`, `cache`, `cached_property`)
    at any size, and every cached callable of the two reader modules for the registry."""
    engine = Path(text_facts.__file__).parent
    sites = set().union(*(_cache_sites(path) for path in sorted(engine.rglob("*.py"))))
    assert sites == set(CACHES_OUTSIDE_THE_REGISTRY)
    cached = {id(value) for module in (text_facts, body_align) for value in vars(module).values()
              if callable(getattr(value, "cache_info", None))
              and value.cache_info().maxsize == text_facts.BODY_CACHE}
    registered = [id(reader) for reader in text_facts.BODY_READERS]
    assert cached == set(registered) and len(registered) == len(set(registered))
    assert {id(body_align.tokens), id(body_align.body_numbers)} <= cached


# ------------------------------------------------------------------ (c) the shingle memo's key


def _fresh(text: str) -> frozenset[int]:
    """E282's reading of `text`, computed with no memo at all."""
    tokens = normalize_folded(fold(text)).split()
    return frozenset(shingles(tokens)) if tokens else frozenset()


def test_the_shingle_memo_is_keyed_on_the_body_never_on_the_advert() -> None:
    """Keyed (id, text length), an advert whose body was edited to a text of the same length
    was served its old body's set for as long as the process lived."""
    first = BODIES[0]
    edited = first.replace("sklep", "garaz")
    assert len(edited) == len(first) and edited != first

    before = indistinguishable._text_shingles(Listing(id=7, block="b", description=first))
    after = indistinguishable._text_shingles(Listing(id=7, block="b", description=edited))

    assert before == _fresh(first) and after == _fresh(edited) and after != before
    assert len(indistinguishable._SHINGLE_MEMO) == 2, "two bodies, two entries"
    again = indistinguishable._text_shingles(Listing(id=8, block="b", description=first))
    assert again is before, "the same text hits, whichever advert carries it"
    assert len(indistinguishable._SHINGLE_MEMO) == 2


# ------------------------------------------------------------------ (d) the vectors, streamed


class _Text(str):
    """An embedding as the pooler hands it back, as text, traceable once dropped."""


def _vector(seed: int) -> str:
    rng = random.Random(seed)
    return "[" + ",".join(f"{rng.uniform(-0.1, 0.1):.4f}" for _ in range(512)) + "]"


class _SlicedClip:
    """A connection that serves COHORT_CLIP_SQL one slice at a time and, before it serves the
    next, counts the texts of the slices before that something still holds."""

    def __init__(self, texts: dict[int, str]) -> None:
        self.texts = texts
        self.slices: list[list[int]] = []
        self.served: list[weakref.ref[_Text]] = []
        self.alive_at_next: list[int] = []

    def cursor(self) -> "_SlicedCursor":
        return _SlicedCursor(self)


class _SlicedCursor:
    description = (("image_id",), ("embedding",))

    def __init__(self, conn: _SlicedClip) -> None:
        self.conn = conn
        self.rows: list[tuple[int, _Text]] = []

    def __enter__(self) -> "_SlicedCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        assert sql == COHORT_CLIP_SQL
        self.conn.alive_at_next.append(sum(ref() is not None for ref in self.conn.served))
        self.conn.slices.append(list(params["ids"]))
        for image_id in params["ids"]:
            if image_id in self.conn.texts:
                text = _Text(self.conn.texts[image_id])
                self.conn.served.append(weakref.ref(text))
                self.rows.append((image_id, text))

    def fetchall(self) -> list[tuple[int, _Text]]:
        rows, self.rows = self.rows, []
        return rows


def test_the_vector_read_holds_one_slice_and_gives_what_the_concatenating_read_gave(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Three FACT_CHUNK slices: one image with no vector, one whose text is not 512 numbers (no
    vector, as before). The output is the old read's to the byte and in its order, and each
    slice's text is gone before the next slice is read."""
    monkeypatch.setattr(incremental_lane, "FACT_CHUNK", 2)
    texts = {1: _vector(1), 2: _vector(2), 3: "[0.1,0.2]", 4: _vector(4), 6: _vector(6)}
    wanted = [6, 2, 5, 1, 3, 4, 2]

    old_conn = _SlicedClip(texts)
    old = SqlFacts(old_conn)
    encoded = {int(row["image_id"]): encode_clip(row["embedding"])
               for row in old._dicts_over(COHORT_CLIP_SQL, sorted(set(wanted)),
                                          model=old.clip_model)}
    reference = {image_id: clip for image_id, clip in encoded.items() if clip is not None}
    assert old_conn.alive_at_next == [0, 2, 4], "the sentinel sees the concatenation"

    conn = _SlicedClip(texts)
    facts = SqlFacts(conn)
    got = facts.vectors(wanted)

    assert got == reference and list(got) == list(reference) == [1, 2, 4, 6]
    assert conn.slices == [[1, 2], [3, 4], [5, 6]] and facts.statements == 3
    assert conn.alive_at_next == [0, 0, 0], "no slice's text outlives its encoding"
