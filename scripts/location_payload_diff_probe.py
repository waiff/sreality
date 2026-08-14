"""What moves in a portal's body when NOTHING about the listing moved — W2a-3b.

The passive instrument (migration 402) says how often a normalised body changes.
It cannot say WHY. This probe answers the why: fetch the same detail page two or
three times seconds apart, and every byte that differs is volatile by
construction — the property cannot have changed in eight seconds.

The output is deliberately shaped to what `VolatileProfile` can express — a CSS
selector, an attribute name, a JSON pointer — because a diff naming something the
normaliser cannot strip teaches nobody anything. Three divergence kinds come out:

  * `attribute` — same element path, different attribute value  -> strip_attributes
    (when the name recurs across many paths) or a css_selector (when it is one node)
  * `text` / `element` — a node's text moved, or the node exists in one fetch only
    -> css_selectors
  * `json_pointer` — a JSON body's value moved -> json_pointers, with array indices
    collapsed onto the `-` wildcard once two indices agree

Element identity is a CLASS/ID PATH, not a sibling index: `body > div.wrap >
span.views`. Insertion of one ad slot would renumber every following sibling and
drown the report, whereas a path key survives it — an inserted node simply shows
up as an `element` divergence at its own path.

Rails, same three as the 200x3 confirmation probe next door:
  * every fetch goes through the portal's own client (politeness, 429/403
    penalisation, ListingGoneError, SCRAPER_PROXY_URL egress), reusing that
    probe's `build_client` / `fetch_body` rather than a second front door;
  * round-major and paced, so one listing is never hammered back-to-back and the
    spacing between a key's own fetches is the length of a round;
  * SMALL — a handful of listings, two or three rounds. It writes NOTHING to the
    database; bodies land on disk so `--replay` can re-diff them offline while a
    profile is iterated, with no further traffic.

TIME IS NOT THE ONLY AXIS — `--fresh-session-per-round` (W2a-3c). remax answers
three fetches seconds apart with BYTE-IDENTICAL bodies and still measured 100%
normalised change in production. The difference is the HTTP session: a Symfony
CSRF token is minted per session, so it is a constant inside one `requests.Session`
and re-rolls for the next one. The live drain is a fresh process per run, hours
apart, so every production refetch is a cross-session one — and a probe that keeps
one session measures a strictly weaker thing than the instrument it is explaining.
The flag builds a NEW client per round (sharing the rate limiter, so politeness and
the 429/403 penalty do not reset with the session) and is the default for that
reason. `--no-fresh-session-per-round` isolates the other half when a portal's
churn needs splitting into per-response and per-session parts.

Usage:
  python -m scripts.location_payload_diff_probe --source idnes --out-dir /tmp/w2a3b
  python -m scripts.location_payload_diff_probe --replay /tmp/w2a3b/idnes
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from selectolax.parser import HTMLParser

from location_data.payload_norm import (
    DEFAULT_VOLATILE_PROFILES,
    VolatileProfile,
    normalise,
    sniff_content_type,
)
from scraper.portal import default_config
from scraper.rate_limit import RateLimiter
from scripts.location_payload_refetch_probe import (
    JSON_CONTENT_TYPE,
    KIND_GONE,
    KIND_OK,
    Pacer,
    SampleKey,
    build_client,
    fetch_body,
    proxy_unavailable,
)

LOG = logging.getLogger("location_payload_diff_probe")

DEFAULT_LISTINGS = 5
DEFAULT_FETCHES = 2
# Long enough that a per-second counter or a clock-derived token has ticked,
# short enough that no human edited the listing in between.
DEFAULT_SPACING_S = 8.0

# How many differing values to carry into the report per divergence. Two is
# enough to see the SHAPE of the churn (`sig=a1b2` vs `sig=c3d4`); more just
# makes a long report longer.
_SAMPLES = 2
_SAMPLE_CHARS = 110
# A Nette/Tailwind detail page nests ~20 levels deep, and the top of that chain is
# the same page frame on every listing. Only the tail identifies the node.
_PATH_TAIL = 4

# Discovery: which index scope to pull sample detail refs from. One page per
# portal, the largest category, so the probe costs one extra request per source.
# The kwargs are that portal's OWN `fetch_index` signature, which is why mmreality
# (one mixed index, `page`) and remax (one mixed search, `sale=1` = prodej) look
# nothing like the three category-split portals.
_DISCOVERY_SCOPE: dict[str, dict[str, Any]] = {
    "idnes": {"sale_type": "prodej", "category": "byty"},
    "realitymix": {"sale_type": "prodej", "category": "byty"},
    "ceskereality": {"sale_type": "prodej", "category": "byty"},
    "mmreality": {},
    "remax": {"sale": 1},
}

_INDEX_PARSERS: dict[str, str] = {
    "idnes": "scraper.idnes_parser",
    "realitymix": "scraper.realitymix_parser",
    "ceskereality": "scraper.ceskereality_parser",
    "mmreality": "scraper.mmreality_parser",
    "remax": "scraper.remax_parser",
}


@dataclass(frozen=True)
class Divergence:
    """One thing that moved between fetches, named the way a profile names it."""

    kind: str
    path: str
    detail: str = ""
    samples: tuple[str, ...] = ()
    nodes: int = 1

    def render(self, *, tail: int = _PATH_TAIL) -> str:
        segments = self.path.split(" > ")
        shown = self.path if len(segments) <= tail else "... > " + " > ".join(
            segments[-tail:])
        head = f"[{self.kind}] {shown}"
        if self.detail:
            head += f"  @{self.detail}"
        if self.nodes > 1:
            head += f"  (x{self.nodes})"
        body = "".join(f"\n      {i + 1}: {s}" for i, s in enumerate(self.samples))
        return head + body


@dataclass
class KeyResult:
    key: str
    bodies: list[bytes] = field(default_factory=list)
    content_type: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return len(self.bodies) >= 2


# --- the differ (pure: no network, no clock) ---


def diff_bodies(bodies: Sequence[bytes], *, content_type: str) -> list[Divergence]:
    """Everything that differs across `bodies`, as profile-expressible names."""
    if len(bodies) < 2:
        return []
    if "json" in content_type.lower():
        return _diff_json(bodies)
    return _diff_html(bodies)


def _diff_html(bodies: Sequence[bytes]) -> list[Divergence]:
    docs = [_html_features(body) for body in bodies]
    out: list[Divergence] = []

    attr_keys = _union(d.attrs for d in docs)
    for path, name in sorted(attr_keys):
        values = [d.attrs.get((path, name), ()) for d in docs]
        if _all_equal(values):
            continue
        out.append(Divergence(
            kind="attribute", path=path, detail=name,
            samples=_samples(values), nodes=max(len(v) for v in values),
        ))

    for path in sorted(_union(d.texts for d in docs)):
        values = [d.texts.get(path, ()) for d in docs]
        if _all_equal(values):
            continue
        out.append(Divergence(
            kind="text", path=path,
            samples=_samples(values), nodes=max(len(v) for v in values),
        ))

    for path in sorted(_union(d.paths for d in docs)):
        counts = [d.paths.get(path, 0) for d in docs]
        if len(set(counts)) == 1:
            continue
        out.append(Divergence(
            kind="element", path=path,
            samples=tuple(f"count={c}" for c in counts[:_SAMPLES]),
            nodes=max(counts),
        ))
    return out


@dataclass
class _Features:
    paths: dict[str, int]
    attrs: dict[tuple[str, str], tuple[str, ...]]
    texts: dict[str, tuple[str, ...]]


def _html_features(body: bytes) -> _Features:
    tree = HTMLParser(body)
    paths: Counter[str] = Counter()
    attrs: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    texts: defaultdict[str, list[str]] = defaultdict(list)
    root = tree.root
    if root is None:
        return _Features({}, {}, {})
    for node in root.traverse(include_text=False):
        if node.tag in (None, "-undef", "-text", "-comment"):
            continue
        path = _node_path(node)
        paths[path] += 1
        for name, value in (node.attributes or {}).items():
            attrs[(path, name)].append(value or "")
        own = _own_text(node)
        if own:
            texts[path].append(own)
    return _Features(
        paths=dict(paths),
        # Sorted so a reordering of equal siblings is not read as a change: the
        # question is WHICH values a path carries, never in what order.
        attrs={k: tuple(sorted(v)) for k, v in attrs.items()},
        texts={k: tuple(sorted(v)) for k, v in texts.items()},
    )


_WS = re.compile(r"\s+")


def _node_path(node: Any) -> str:
    segments: list[str] = []
    cur: Any = node
    while cur is not None and cur.tag not in (None, "-undef"):
        segments.append(_node_sig(cur))
        cur = cur.parent
    return " > ".join(reversed(segments))


def _node_sig(node: Any) -> str:
    sig = node.tag
    attributes = node.attributes or {}
    node_id = attributes.get("id")
    if node_id:
        sig += f"#{node_id}"
    classes = (attributes.get("class") or "").split()
    if classes:
        sig += "." + ".".join(sorted(classes))
    return sig


def _own_text(node: Any) -> str:
    """This node's DIRECT text, so a change is reported at the node that owns it.

    `node.text(deep=True)` would repeat one moved counter at every ancestor up to
    <html>, which buries the selector the profile actually needs.
    """
    parts: list[str] = []
    child = node.child
    while child is not None:
        if child.tag == "-text":
            parts.append(child.text_content or "")
        child = child.next
    return _WS.sub(" ", "".join(parts)).strip()


def _diff_json(bodies: Sequence[bytes]) -> list[Divergence]:
    docs: list[Any] = []
    for body in bodies:
        try:
            docs.append(json.loads(body.decode("utf-8")))
        except Exception:
            return [Divergence(kind="json_pointer", path="/", detail="unparseable")]
    flat = [dict(_flatten(doc, "")) for doc in docs]
    diverged: dict[str, list[str]] = {}
    for pointer in sorted(_union(f for f in flat)):
        values = [f.get(pointer, "<absent>") for f in flat]
        if len(set(values)) == 1:
            continue
        diverged[pointer] = values
    return _collapse_array_pointers(diverged)


def _flatten(node: Any, prefix: str) -> Iterable[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            token = str(key).replace("~", "~0").replace("/", "~1")
            yield from _flatten(value, f"{prefix}/{token}")
        return
    if isinstance(node, list):
        for index, value in enumerate(node):
            yield from _flatten(value, f"{prefix}/{index}")
        return
    yield prefix or "/", json.dumps(node, ensure_ascii=False)


_INDEX_TOKEN = re.compile(r"/\d+(?=/|$)")


def _collapse_array_pointers(diverged: dict[str, list[str]]) -> list[Divergence]:
    """`/images/0/url` + `/images/1/url` -> the profile's `/images/-/url`.

    Only when at least two distinct indices moved: one moved element of an array
    is a single pointer, and widening it to the wildcard on that evidence would
    strip the other elements without ever having seen them move.
    """
    groups: defaultdict[str, list[str]] = defaultdict(list)
    for pointer in diverged:
        groups[_INDEX_TOKEN.sub("/-", pointer)].append(pointer)
    out: list[Divergence] = []
    for wildcard, members in sorted(groups.items()):
        if len(members) >= 2 and wildcard != members[0]:
            values = diverged[sorted(members)[0]]
            out.append(Divergence(
                kind="json_pointer", path=wildcard,
                samples=_samples([(v,) for v in values]), nodes=len(members),
            ))
            continue
        for pointer in sorted(members):
            out.append(Divergence(
                kind="json_pointer", path=pointer,
                samples=_samples([(v,) for v in diverged[pointer]]),
            ))
    return out


def _union(maps: Iterable[dict[Any, Any]]) -> set[Any]:
    keys: set[Any] = set()
    for mapping in maps:
        keys |= set(mapping)
    return keys


def _all_equal(values: Sequence[tuple[str, ...]]) -> bool:
    return len(set(values)) == 1


def _samples(values: Sequence[Sequence[str]]) -> tuple[str, ...]:
    out: list[str] = []
    for group in values[:_SAMPLES]:
        joined = " | ".join(group) if group else "<absent>"
        if len(joined) > _SAMPLE_CHARS:
            joined = joined[:_SAMPLE_CHARS] + "..."
        out.append(joined)
    return tuple(out)


# --- profile evaluation ---


def profile_residue(
    result: KeyResult, profile: VolatileProfile,
) -> tuple[bool, list[Divergence]]:
    """Do the fetches hash alike under `profile`, and if not, what is left moving?

    The residue is diffed over the NORMALISED bodies, so it names only what the
    profile failed to remove — the exact list a next iteration has to cover.
    """
    normed = [
        normalise(body, content_type=result.content_type, volatile=profile)
        for body in result.bodies
    ]
    hashes = {n.norm_sha256 for n in normed}
    if len(hashes) == 1:
        return True, []
    return False, diff_bodies(
        [n.norm_bytes for n in normed], content_type=result.content_type,
    )


def raw_matches(result: KeyResult) -> bool:
    return len({normalise(
        b, content_type=result.content_type, volatile=VolatileProfile(),
    ).raw_sha256 for b in result.bodies}) == 1


# --- fetching ---


def discover_refs(source: str, client: Any, limit: int) -> list[SampleKey]:
    """Sample detail refs off ONE live index page, via the portal's own parser.

    The refetch probe next door samples from `listings`; this one runs wherever an
    operator is (no SUPABASE_DB_URL in a dev shell), so it pays one index request
    instead of a database round trip.
    """
    if source == "sreality":
        estates = client.fetch_index_page(0)
        return [
            SampleKey(source=source, native_id=str(e.get("hash_id")), detail_ref=None)
            for e in estates[:limit] if e.get("hash_id")
        ]
    parser = importlib.import_module(_INDEX_PARSERS[source])
    html, _status = client.fetch_index(**_DISCOVERY_SCOPE[source])
    page = parser.parse_index(html)
    return [
        SampleKey(source=source, native_id=item.source_id_native,
                  detail_ref=item.detail_path)
        for item in page.items[:limit]
    ]


def session_factory(
    source: str, rate: float, *, fresh_per_round: bool,
) -> Callable[[int], Any]:
    """A client per round, or one client for all of them.

    The RateLimiter is deliberately SHARED across the fresh clients: it carries the
    pacing and the adaptive 429/403 penalty, and re-creating it per round would hand
    a portal that just throttled us a clean slate three times in a row. Only the
    HTTP session (cookies, and therefore any session-minted CSRF token) is new.
    """
    limiter = RateLimiter(rate)
    first = build_client(source, limiter)

    def factory(round_index: int) -> Any:
        if round_index == 0 or not fresh_per_round:
            return first
        return build_client(source, limiter)

    return factory


def fetch_rounds(
    source: str, client_for_round: Callable[[int], Any], keys: Sequence[SampleKey], *,
    fetches: int, pacer: Pacer,
) -> dict[str, KeyResult]:
    """Round-major passes: all keys, then all keys again. Never back-to-back.

    A `gone` key leaves the sample — it will 404 for the rest of the run. An
    ERRORED one stays: a transient 502 on round 2 still leaves rounds 1 and 3
    comparable, and two bodies is all the differ needs.
    """
    results = {k.native_id: KeyResult(key=k.native_id) for k in keys}
    live = list(keys)
    for round_index in range(fetches):
        gone: set[str] = set()
        client = client_for_round(round_index)
        for key in live:
            pacer.wait()
            result = fetch_body(source, client, key)
            record = results[key.native_id]
            if result.kind == KIND_OK and result.body is not None:
                record.bodies.append(result.body)
                record.content_type = result.content_type or JSON_CONTENT_TYPE
                continue
            record.errors.append(result.error or result.kind)
            if result.kind == KIND_GONE:
                gone.add(key.native_id)
        LOG.info("DIFF round %d/%d done source=%s", round_index + 1, fetches, source)
        if gone:
            live = [k for k in live if k.native_id not in gone]
    return results


def probe_source(
    source: str, *, listings: int, fetches: int, spacing_s: float, out_dir: Path | None,
    fresh_session: bool = True,
) -> dict[str, KeyResult]:
    config = default_config(source)
    configured = config.limits.detail_rate
    # Spacing between a key's own fetches is a ROUND, so the per-request interval
    # only has to be polite: take the politer of the portal's configured rate and
    # this lane's 1/s ceiling.
    rate = min(1.0, configured) if configured and configured > 0 else 1.0
    client_for_round = session_factory(source, rate, fresh_per_round=fresh_session)
    keys = discover_refs(source, client_for_round(0), listings)
    if not keys:
        LOG.warning("DIFF no index items discovered for source=%s", source)
        return {}
    LOG.info("DIFF start source=%s keys=%d fetches=%d rate=%.2f/s fresh_session=%s",
             source, len(keys), fetches, rate, fresh_session)
    # A round of N keys at `rate` already spaces a key's own fetches by N/rate;
    # the floor only matters for a very small sample.
    per_request = max(1.0 / rate, spacing_s / max(len(keys), 1))
    results = fetch_rounds(
        source, client_for_round, keys, fetches=fetches,
        pacer=Pacer(min_interval_s=per_request),
    )
    if out_dir is not None:
        save_bodies(out_dir / source, results)
    return results


def save_bodies(directory: Path, results: dict[str, KeyResult]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for native_id, result in results.items():
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", native_id)
        suffix = "json" if "json" in result.content_type else "html"
        for index, body in enumerate(result.bodies):
            (directory / f"{safe}.r{index + 1}.{suffix}").write_bytes(body)


def load_bodies(directory: Path) -> dict[str, KeyResult]:
    results: dict[str, KeyResult] = {}
    for path in sorted(directory.glob("*.r*.*")):
        native_id = path.name.split(".r")[0]
        body = path.read_bytes()
        record = results.setdefault(native_id, KeyResult(key=native_id))
        record.bodies.append(body)
        record.content_type = sniff_content_type(body)
    return results


# --- reporting ---


def report(source: str, results: dict[str, KeyResult], *, top: int) -> None:
    profile = DEFAULT_VOLATILE_PROFILES.get(source, VolatileProfile())
    usable = {k: v for k, v in results.items() if v.usable}
    print(f"\n{'=' * 78}\n{source}: {len(usable)}/{len(results)} keys with >=2 fetches")
    for record in results.values():
        for error in record.errors:
            print(f"  ERROR {record.key}: {error}")
    if not usable:
        return

    raw_stable = sum(1 for r in usable.values() if raw_matches(r))
    passing = 0
    raw_group = _Group()
    residue_group = _Group()
    for record in usable.values():
        raw_group.add(diff_bodies(record.bodies, content_type=record.content_type))
        ok, residue = profile_residue(record, profile)
        passing += ok
        residue_group.add(residue)

    print(f"  raw bytes identical across fetches: {raw_stable}/{len(usable)}")
    print(f"  CURRENT profile normalises alike:   {passing}/{len(usable)}")
    raw_group.render("RAW DIVERGENCES (all of them volatile by construction)",
                     top=top, keys=len(usable))
    # Its own examples, diffed over the NORMALISED bodies: sharing the raw pass's
    # samples would show a value the profile has already stripped as still moving.
    residue_group.render("RESIDUE AFTER THE CURRENT PROFILE (still missing)",
                         top=top, keys=len(usable))


@dataclass
class _Group:
    counts: Counter[tuple[str, str, str]] = field(default_factory=Counter)
    examples: dict[tuple[str, str, str], Divergence] = field(default_factory=dict)

    def add(self, divergences: Sequence[Divergence]) -> None:
        for div in divergences:
            token = (div.kind, div.path, div.detail)
            self.counts[token] += 1
            self.examples.setdefault(token, div)

    def render(self, title: str, *, top: int, keys: int) -> None:
        print(f"\n  --- {title}: {len(self.counts)} distinct ---")
        for token, hits in self.counts.most_common(top):
            print(f"  [{hits}/{keys} keys] {self.examples[token].render()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="structural refetch diff probe")
    parser.add_argument("--source", default="",
                        help="comma-separated portals to fetch live")
    parser.add_argument("--replay", default="",
                        help="re-diff saved bodies (a directory of <source>/ dirs, "
                             "or one source dir) — no network")
    parser.add_argument("--listings", type=int, default=DEFAULT_LISTINGS)
    parser.add_argument("--fetches", type=int, default=DEFAULT_FETCHES)
    parser.add_argument("--spacing-s", type=float, default=DEFAULT_SPACING_S)
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--top", type=int, default=40)
    parser.add_argument(
        "--fresh-session-per-round", dest="fresh_session",
        action=argparse.BooleanOptionalAction, default=True,
        help="new HTTP session per round (default): what the live drain does, and "
             "the only way to see session-minted CSRF material move",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if args.replay:
        root = Path(args.replay)
        directories = (
            [root] if any(root.glob("*.r*.*"))
            else sorted(p for p in root.iterdir() if p.is_dir())
        )
        for directory in directories:
            report(directory.name, load_bodies(directory), top=args.top)
        return 0

    sources = [s.strip() for s in args.source.split(",") if s.strip()]
    if not sources:
        print("ERROR: --source or --replay is required.", file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir) if args.out_dir else None
    for source in sources:
        if proxy_unavailable(source):
            LOG.warning("DIFF %s rides SCRAPER_PROXY_URL and it is unset — "
                        "fetching direct; a WAF page is not evidence", source)
        try:
            results = probe_source(
                source, listings=args.listings, fetches=args.fetches,
                spacing_s=args.spacing_s, out_dir=out_dir,
                fresh_session=args.fresh_session,
            )
        except Exception as exc:  # noqa: BLE001 - one portal must not end the sweep
            LOG.error("DIFF source=%s failed: %s", source, exc)
            continue
        report(source, results, top=args.top)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
