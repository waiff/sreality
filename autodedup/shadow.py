"""The wave shadow: a location wave's movers through the engine before the wave deploys.

    python3 -m location_data.what_if --ids-sql "<SELECT>" --scope apply --out shadow/w4 ...
    python3 -m autodedup.shadow shadow/w4
    python3 -m autodedup.harness shadow shadow/w4/base/cohort.jsonl.gz shadow/w4/positions.jsonl \\
        shadow/w4/labels --scorer shadow/w4/scorer.json --out shadow/w4/judge

The engine reads every position through `COHORT_LOCATION_SQL` and `export.build_location`, so the
seam is the cohort artifact (`DECISION_MODULES` untouched). `main` is step 2: the movers' blocks,
sized and capped first, then the cohort, the rulings and the live scorer, exported IN-PROCESS on a
READ ONLY connection, so `lane.run` never writes its `autodedup.iterations` row. `read_cursor` is
the only way this module and `location_data.what_if` reach SQL. `override` and `judge` are step 3's
halves (`harness shadow`). Design: `docs/design/location-quality/wave-shadow.md`.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, NamedTuple

import psycopg

from autodedup.apply_scope import SCOPE_SETTING, Member, Scope, scope_fields
from autodedup.apply_sql import SETTING_SQL
from autodedup.blocking import SPLIT_CITY_OBEC_KODS
from autodedup.cohort import ids_block, parse_blocks
from autodedup.evaluate import DIFFERENT, RULINGS_FILES, SAME, read_rulings
from autodedup.export import ARTIFACT_NAME, build_location, fetch_block_ids, run_export
from autodedup.incremental import GENERATION
from autodedup.incremental_sql import RT_CALIBRATION_SCORER_SQL
from autodedup.labels import PairKey, pair_key
from autodedup.labels_lane import run_labels

POSITIONS_FILE: str = "positions.jsonl"
MOVERS_FILE: str = "movers.jsonl"
WHAT_IF_FILE: str = "what_if.json"
SCORER_FILE: str = "scorer.json"
SHADOW_FILE: str = "shadow.json"
GROUP_FILE: str = "group.json"
D83_EDGES: tuple[str, ...] = ("merge_gained", "merge_lost", "copairs_gained", "copairs_lost")
MAX_ADS: int = 60_000
MAX_BLOCKS: int = 40
# The operator's Browse merges since 2026-10-27: 413 `same` rulings at scoping, reported apart.
RECENT_SINCE: str = "2026-10-27"
_BLOCK_TIMEOUT_MS: int = 600_000

# One advert's apply-scope facts by listing id, in `Member` order (`PROPERTY_LISTINGS_SQL`'s).
_SCOPE_FACTS_SQL = """
select l.id, l.property_id, l.category_type, l.category_main, ll.obec_kod, ll.cast_obce_kod,
       ll.okres_kod
  from public.listings l
  left join public.listing_location ll on ll.listing_id = l.id
 where l.id = any(%(ids)s::bigint[])
"""

# The ads a ruling or a property ties to the listed ones, every ruling the judge reads
# (`evaluate.read_rulings`): the other side of a pair verdict or an operator must-not-link, the
# members of a group verdict or an operator merge, the sibling ads of their property.
_TIES_SQL = """
select case when p.lo = any(%(ids)s::bigint[]) then p.hi else p.lo end
  from (select v.listing_lo, v.listing_hi from autodedup.verdicts v where v.kind = 'pair'
        union all
        select n.listing_lo, n.listing_hi from autodedup.must_not_link n
         where n.source = 'operator') p (lo, hi)
 where p.lo = any(%(ids)s::bigint[]) or p.hi = any(%(ids)s::bigint[])
union
select unnest(g.member_ids)
  from (select v.member_ids from autodedup.verdicts v where v.kind = 'cluster'
        union all
        select m.member_ids from autodedup.operator_merges m) g
 where g.member_ids && %(ids)s::bigint[]
union
select s.id from public.listings l join public.listings s on s.property_id = l.property_id
 where l.id = any(%(ids)s::bigint[])
"""


@contextmanager
def read_cursor(conn: psycopg.Connection) -> Iterator[psycopg.Cursor]:
    """The wave shadow's one way to a cursor: its own transaction, READ ONLY on that conn."""
    with conn.transaction(), conn.cursor() as cur:
        yield cur


def read(conn: psycopg.Connection, sql: str, params: Any = None, *, binary: bool | None = None
         ) -> tuple[list[str], list[tuple[Any, ...]]]:
    """One statement through `read_cursor`: its column names and rows."""
    with read_cursor(conn) as cur:
        cur.execute(sql, params, binary=binary)
        return [column.name for column in cur.description or ()], cur.fetchall()


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def in_apply_scope(conn: psycopg.Connection, listing_ids: Iterable[int]) -> list[int]:
    """The ids the live apply scope admits, by its own predicate (`apply_scope.Scope.outside`)."""
    _, rows = read(conn, SETTING_SQL, {"key": SCOPE_SETTING})
    value = rows[0][0] if rows else None
    scope = Scope(**scope_fields(json.loads(value) if isinstance(value, str) else value))
    if problems := scope.live_problems():
        raise SystemExit(f"{SCOPE_SETTING} admits nothing: {'; '.join(problems)}")
    _, rows = read(conn, _SCOPE_FACTS_SQL, {"ids": list(listing_ids)})
    return sorted(int(row[0]) for row in rows if scope.outside(Member.read(row)) is None)


def tied_ads(conn: psycopg.Connection, listing_ids: Iterable[int]) -> list[int]:
    """`_TIES_SQL` over the ids, read only."""
    _, rows = read(conn, _TIES_SQL, {"ids": sorted(set(listing_ids))})
    return sorted({int(row[0]) for row in rows if row[0] is not None})


def side_block(side: dict[str, Any] | None) -> str | None:
    """A row's block at E300's grain: its quarter in a split city, else its town; None when
    it has no town, or sits in a split city with no quarter (its candidates are the whole city)."""
    if not side or side.get("obec_kod") is None:
        return None
    if int(side["obec_kod"]) not in SPLIT_CITY_OBEC_KODS:
        return f"town:{int(side['obec_kod'])}"
    if side.get("cast_obce_kod") is not None:
        return f"quarter:{int(side['cast_obce_kod'])}"
    return None


def _unquartered(mover: dict[str, Any]) -> bool:
    """A side in a split city with no quarter: its candidates are the whole city."""
    return any(side and side.get("obec_kod") is not None and side_block(side) is None
               for side in (mover.get("old"), mover.get("new")))


def blocks_for(movers: Iterable[dict[str, Any]], ties: Callable[[list[int]], Iterable[int]]
               ) -> list[str]:
    """Each mover's old and new block; a mover with an unquartered side rides ONE `ids` block
    with every ad `ties` binds to it (`tied_ads`), so both arms hold it and its rulings."""
    keys: dict[str, None] = {}
    listed: list[int] = []
    for mover in movers:
        for side in (mover.get("old"), mover.get("new")):
            if (block := side_block(side)) is not None:
                keys[block] = None
        if _unquartered(mover):
            listed.append(int(mover["listing_id"]))
    if listed:
        keys[ids_block([*listed, *ties(sorted(set(listed)))])] = None
    return list(keys)


def parse_group(text: str) -> tuple[int, int]:
    """`i/n`, 1 <= i <= n: the i-th of n contiguous block groups."""
    index, _, count = str(text).partition("/")
    if not (index.isdigit() and count.isdigit() and 1 <= int(index) <= int(count)):
        raise argparse.ArgumentTypeError(f"--block-group must be i/n with 1 <= i <= n: {text!r}")
    return int(index), int(count)


def chunks(blocks: Iterable[str], count: int) -> list[list[str]]:
    """The blocks sorted by key and cut into `count` contiguous groups, the first ones a block
    longer when they do not divide evenly."""
    ordered = sorted(blocks)
    size, extra = divmod(len(ordered), count)
    out, at = [], 0
    for index in range(count):
        step = size + (1 if index < extra else 0)
        out.append(ordered[at:at + step])
        at += step
    return out


def groups_needed(sizes: dict[str, int], max_blocks: int, max_ads: int) -> int | None:
    """The fewest groups whose every chunk fits both caps; None when one block alone does not."""
    if any(ads > max_ads for ads in sizes.values()):
        return None
    for count in range(1, len(sizes) + 1):
        if all(len(chunk) <= max_blocks and sum(sizes[b] for b in chunk) <= max_ads
               for chunk in chunks(sizes, count)):
            return count
    return None


def outside_group(movers: Path, group: dict[str, Any]) -> set[int]:
    """The movers another group exports (by the old block, or the `ids` block an unquartered one
    rides): not judged here, and not unjudged."""
    chunk = set(group.get("blocks") or ())
    if not chunk:
        return set()
    listed_here = {i for block in parse_blocks(" ".join(sorted(chunk))) for i in block.ids}
    out: set[int] = set()
    for mover in _jsonl(movers):
        listing_id, old = int(mover["listing_id"]), side_block(mover.get("old"))
        exported_somewhere = old is not None or _unquartered(mover)
        exported_here = old in chunk or listing_id in listed_here
        if exported_somewhere and not exported_here:
            out.add(listing_id)
    return out


def block_sizes(conn: psycopg.Connection, blocks: list[str]) -> dict[str, int]:
    """Each block's ad count, selected exactly as the export will select it."""
    return {text: len(fetch_block_ids(conn, block, timeout_ms=_BLOCK_TIMEOUT_MS, negctl_max=1)[0])
            for text, block in zip(blocks, parse_blocks(" ".join(blocks)))}


def export_cohort(conn_factory: Callable[[], Any], shadow_dir: Path, blocks: list[str]
                  ) -> dict[str, Any]:
    """Step 2's export: the base cohort over `blocks`, the rulings and the live scorer."""
    cohort = run_export(conn_factory, {"blocks": " ".join(blocks)}, shadow_dir / "base")
    labels = run_labels(conn_factory, {}, shadow_dir / "labels")
    conn = conn_factory()
    try:
        _, rows = read(conn, RT_CALIBRATION_SCORER_SQL, {"generation": GENERATION})
    finally:
        conn.close()
    if not rows:
        raise SystemExit(f"generation {GENERATION!r} has no calibration row: no live scorer")
    settings = rows[0][0] if isinstance(rows[0][0], dict) else json.loads(rows[0][0] or "{}")
    (shadow_dir / SCORER_FILE).write_text(json.dumps(
        {"generation": GENERATION, "settings": settings, "model_version": rows[0][1]},
        indent=1, sort_keys=True), encoding="utf-8")
    return {"blocks": blocks, "cohort": cohort["counts"], "labels": labels["counts"]}


class Override(NamedTuple):
    """What `override` did: the ads listed, the ads in the cohort, the ads whose block changed."""

    listed: set[int]
    cohort: set[int]
    changed: set[int]


def override(base: Path, positions: Path, out: Path) -> Override:
    """`base` with the listed blocks rebuilt by the export's builder, written to `out`."""
    rows = {int(row["listing_id"]): row for row in _jsonl(positions)}
    cohort: set[int] = set()
    changed: set[int] = set()
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(base, "rt", encoding="utf-8") as src, \
            gzip.open(out, "wt", encoding="utf-8", newline="\n") as dst:
        for line in src:
            record = json.loads(line)
            if record.get("t") == "listing":
                cohort.add(record["id"])
                if record["id"] in rows:
                    block = build_location(rows[record["id"]])
                    if block != record["location"]:
                        changed.add(record["id"])
                        record["location"] = block
                        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            dst.write(line)
    return Override(set(rows), cohort, changed)


def recent_same(rulings: Path, since: str = RECENT_SINCE) -> set[PairKey]:
    """The `same` rulings written since `since` that are still their pair's newest word."""
    newest = read_rulings(rulings)
    stamped = [(row.get("decided_at"), pair_key(row["listing_lo"], row["listing_hi"]))
               for row in _jsonl(rulings / RULINGS_FILES[0]) if row.get("verdict") == SAME]
    for row in _jsonl(rulings / RULINGS_FILES[1]):
        members = sorted({int(member["listing_id"]) for member in row.get("members") or ()})
        stamped += [(row.get("merged_at"), (a, b))
                    for i, a in enumerate(members) for b in members[i + 1:]]
    return {key for stamp, key in stamped if str(stamp or "") >= since and newest.get(key) == SAME}


def judge(report: dict[str, Any], done: Override, *, base_e111: dict[str, Any],
          together: set[PairKey], recent: set[PairKey], what_if: dict[str, Any],
          elsewhere: set[int] = frozenset()) -> dict[str, Any]:
    """PASS = no NEW operator negative inside a K-B family and no ruled pair moved against its
    ruling; E111's standing state (both arms), D83 and the recent pairs are reported."""
    lists = report["d83"]["lists"]
    broken = ([row for row in lists["copairs_lost"] if row[2] == SAME]
              + [row for row in lists["copairs_gained"] if row[2] == DIFFERENT])
    honoured = ([row for row in lists["copairs_gained"] if row[2] == SAME]
                + [row for row in lists["copairs_lost"] if row[2] == DIFFERENT])
    candidate_e111 = report["revocation"]
    new = sorted({tuple(pair) for pair in candidate_e111["negatives"]}
                 - {tuple(pair) for pair in base_e111["negatives"]})
    judged = {key for key in recent if key[0] in done.cohort and key[1] in done.cohort}
    split = judged & {(int(row[0]), int(row[1])) for row in broken}
    held = judged & together
    unjudged_movers = len(done.listed - done.cohort - set(elsewhere))
    return {
        "pass": not new and not broken,
        "partial": bool(what_if.get("limit")) or unjudged_movers > 0,
        "run": what_if,
        "e111": {"base": base_e111, "candidate": candidate_e111,
                 "new_negatives": [list(pair) for pair in new]},
        "broken_rulings": broken,
        "honoured_rulings": honoured,
        "movers_listed": len(done.listed),
        "movers_in_cohort": len(done.changed),
        "movers_unjudged": unjudged_movers,
        "movers_elsewhere": len(set(elsewhere) & done.listed),
        "recent_same": {"pairs": len(recent), "judged": len(judged),
                        "held": len(held), "broken": len(split),
                        "apart_in_both": len(judged - held - split),
                        "unjudged": len(recent - judged)},
        "d83_touching_movers": {name: [row for row in lists[name]
                                       if row[0] in done.changed or row[1] in done.changed]
                                for name in D83_EDGES},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autodedup.shadow", description=__doc__.splitlines()[0])
    parser.add_argument("shadow_dir", help="the directory `what_if` wrote movers.jsonl into")
    parser.add_argument("--block-group", type=parse_group, default=(1, 1), metavar="I/N",
                        help="export and judge the i-th of n contiguous block groups")
    parser.add_argument("--max-ads", type=int, default=MAX_ADS, help="refuse a larger group")
    parser.add_argument("--max-blocks", type=int, default=MAX_BLOCKS, help="refuse more blocks")
    args = parser.parse_args(argv)
    from scraper import db

    shadow_dir = Path(args.shadow_dir)
    conn = db.connect_read_only()
    try:
        blocks = blocks_for(_jsonl(shadow_dir / MOVERS_FILE), lambda ids: tied_ads(conn, ids))
        if not blocks:
            raise SystemExit(f"no exportable block in {shadow_dir / MOVERS_FILE}")
        sizes = block_sizes(conn, blocks)
    finally:
        conn.close()
    for text, block in zip(sorted(sizes), parse_blocks(" ".join(sorted(sizes)))):
        print(f"[shadow] {block.key if block.ids else text} {sizes[text]} ads")
    index, count = args.block_group
    chunk = chunks(sizes, count)[index - 1]
    ads = sum(sizes[block] for block in chunk)
    needed = groups_needed(sizes, args.max_blocks, args.max_ads)
    print(f"[shadow] {len(sizes)} blocks, {sum(sizes.values())} ads; group {index}/{count}: "
          f"{len(chunk)} blocks, {ads} ads; "
          f"groups needed under the caps: {needed if needed else 'none (one block is over)'}",
          flush=True)
    if len(chunk) > args.max_blocks or ads > args.max_ads:
        advice = (f"dispatch --block-group i/{needed} for i in 1..{needed}" if needed
                  else "narrow the ids or raise --max-ads")
        print(f"[shadow] REFUSED: group {index}/{count} is over --max-blocks {args.max_blocks} "
              f"or --max-ads {args.max_ads}; {advice}", file=sys.stderr)
        return 2
    summary = export_cohort(db.connect_read_only, shadow_dir, chunk)
    (shadow_dir / GROUP_FILE).write_text(json.dumps(
        {"group": f"{index}/{count}", "blocks": chunk, "ads": ads,
         "all_blocks": len(sizes), "all_ads": sum(sizes.values())},
        indent=1, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=1, sort_keys=True))
    print(f"[shadow] base cohort: {shadow_dir / 'base' / ARTIFACT_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
