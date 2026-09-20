"""The real-time SHADOW lane: the batch engine's decisions, made one arrival at a time.

The batch lane proves accuracy over a whole cohort; production has to reach the SAME state
incrementally. Nothing here re-implements a decision: fingerprints come from
`fingerprint.build_fingerprint`, features from `features.pair_features`, the zone from
`decide.decide_pair` and the groups from `cluster.cluster_pairs`. What this module adds is
the three mechanisms a cohort pass gets for free and an arrival does not.

**E70 — the calibration is FROZEN per generation.** Six of the engine's inputs are read off
the cohort rather than off the pair: the price deciles K3 blocks on, the exploded-key set,
the block and corpus document frequencies behind `tfidf`/`rare_token_overlap`, the in-block
attribute frequencies behind `attr_rarity`, the exact-pin population, and E60's certifying
code set. Left live they would make a pair's score a function of WHEN it was scored, so a
re-run would silently re-decide yesterday's pairs. They are computed once, stored, and a
generation runs under them until an operator cuts a new one — which is a new generation.

**E71 — an arrival re-probes its whole probe-key NEIGHBOURHOOD, so the final state is a pure
function of the corpus.** `BlockIndex.candidates` caps fan-out at `max_candidates_per_listing`
(297 of g6's 5,687 listings sit at the cap), and a cap makes retrieval depend on what was
present when it ran. The fix is not a bigger cap: when a listing's keys change, every listing
that PROBES one of those keys has its retrieval recomputed too, and a pair is kept when either
side retrieves the other (`from_lo` / `from_hi`). The neighbourhood is the ±1 band/decile
relation, which is symmetric, so it is exactly the set `probe_keys` reaches.

**E72 — clustering is recomputed per CONNECTED COMPONENT, not per edge.** Union-find over an
edge stream is order-dependent; `cluster_pairs` over a component's whole edge set is not,
because every rule it applies — the certificate-first order, `cluster_invariants_ok` on the
merged member set, E37's bridge refusal and E57's second offer — reads only members of the two
clusters being joined, and those never leave the component. So the component is the unit of
work, and its result is identical to the cohort pass's.

Everything else is what W9's verification found missing, and each of those is a rule too:
a watermark over FOUR bounded feeds, none of which is monotone on its own (E73); a pass bounded
in statements and not only in listings (E74); a pass that REFUSES a claim it cannot fit rather
than truncating it, and writes nothing until it has agreed to it (E75); a generation seeded at
the present (E76); a store that reads a decision back exactly as it was taken (E77); and an
operator refusal reconciled on every pass, idle ones included (E78). Nothing written leaves
schema `autodedup` (D4).
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Protocol, Sequence

from autodedup.blocking import PROBE_PRIORITY, BlockIndex
from autodedup.cluster import cluster_pairs, cluster_rows
from autodedup.dataset import Image, Listing
from autodedup.decide import Decision, decide_pair
from autodedup.features import (
    FEATURE_VERSION,
    FeatureContext,
    Feats,
    _block_of,
    _tfidf_vector,
    MIN_RARE_BLOCK_DOCS,
    pair_features,
    vocabulary_attr_keys,
)
from autodedup.features import _attr_map as _feature_attr_map
from autodedup.fingerprint import Fingerprint, build_fingerprint
from autodedup.guards import UNIT_DESIGNATOR_VETO, pair_veto
from autodedup.hazard_context import BlockCell, ContextIndex, ContextStamp, rail_plan
from autodedup.hazard_context import address_block_key, category_group
from autodedup.model import LogisticModel
from autodedup.settings import Settings
from autodedup.text_facts import reference_codes, states_from_price

GENERATION: str = "rt"

# A `(probe, key)` tuple flattened to one text token, so the posting list is a two-column
# index lookup in SQL and the same string in the in-memory twin. Unit separator, because a
# street key may hold anything a portal prints but never a control character.
_SEP: str = "\x1f"


def key_token(key: Sequence[Any]) -> str:
    return _SEP.join("" if part is None else str(part) for part in key)


def _cut_key(cat_group: str | None, category_type: str | None) -> str:
    return key_token((cat_group, category_type))


@dataclass(slots=True, frozen=True)
class GuardRow:
    """The five columns `pair_veto` reads, served from `listing_fp` without a gallery fetch.

    Satisfies `guards.GuardSide` structurally, which is why the rule floor can refuse a
    candidate before the fan-out cap spends a slot on it (E17) without building its
    fingerprint — the whole point of keeping retrieval cheap."""

    listing_id: int
    category_main: str | None
    category_type: str | None
    area_m2: float | None
    disposition: str | None
    floor: int | None


def guard_row(fp: Fingerprint) -> GuardRow:
    return GuardRow(fp.listing_id, fp.category_main, fp.category_type, fp.area_m2,
                    fp.disposition, fp.floor)


@dataclass(slots=True, frozen=True)
class FpRow:
    """What the store keeps about ONE listing: the guard columns, the re-score digest, the
    census cell it is counted in and the activity flag.

    The cell is stored rather than re-derived because a listing that MOVES (a resolved location,
    a category correction) must unbump the cell it LEFT — derive it from today's facts and the
    old cell keeps a phantom member forever, and `n_listings` is the exact counter
    `fungible_catalogue` and E64's rail read. `is_active` is stored for the same reason in the
    other direction: it is what the revive sweep anti-joins the live flag against."""

    guard: GuardRow
    digest: str
    cell_key: str
    cell_group: str
    is_active: bool

    @property
    def cell(self) -> tuple[str, str]:
        return (self.cell_key, self.cell_group)


@dataclass(slots=True)
class PairRow:
    """One stored pair of the rolling generation, plus the two bookkeeping bits E71 needs."""

    lo: int
    hi: int
    probes: list[str]
    from_lo: bool
    from_hi: bool
    zone: str
    score: float
    families: list[str]
    certificate: str | None
    veto: str | None
    reason: str
    evidence: dict[str, str]
    context: dict[str, Any]
    fp_lo: str
    fp_hi: str
    # The feature vector this decision was taken on, carried only on the way OUT: the store
    # writes it in the score lane's own `{name: [value, present]}` shape (E12) so the pair
    # page reads one thing whichever lane wrote it, and a row re-read from the store carries
    # none, which is why the upsert coalesces rather than overwrites.
    feats: Feats | None = None

    def decision(self) -> Decision:
        return Decision(self.lo, self.hi, self.zone, self.score, set(self.families),
                        self.certificate, self.veto, self.reason, dict(self.evidence))


@dataclass(slots=True)
class CellRow:
    """The live half of the census (E64): a counter plus three capped sets.

    `n_listings` is exact — it is the only member `fungible_catalogue` and the rail read. The
    three cardinalities are REPORTED only (`is_shape_stack` is inert by W8's verification), so
    their sets are capped and the row says when a cap bound rather than pretending precision."""

    key: str
    category_group: str
    n_listings: int = 0
    shapes: list[str] = field(default_factory=list)
    brokers: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    capped: bool = False

    def cell(self) -> BlockCell:
        return BlockCell(self.key, self.category_group, self.n_listings, len(self.shapes),
                         len(self.brokers), len(self.source_ids))


SET_CAP: int = 256


# ---------------------------------------------------------------------------- calibration


@dataclass(slots=True)
class Calibration:
    """E70: every cohort-relative input of a decision, frozen for one generation."""

    generation: str
    feature_version: int
    built_at: str
    n_listings: int
    price_cuts: dict[str, list[float]] = field(default_factory=dict)
    exploded: dict[str, list[str]] = field(default_factory=dict)
    block_docs: dict[str, int] = field(default_factory=dict)
    block_token_df: dict[str, dict[str, int]] = field(default_factory=dict)
    corpus_docs: int = 0
    corpus_token_df: dict[str, int] = field(default_factory=dict)
    block_attr_df: dict[str, dict[str, int]] = field(default_factory=dict)
    pin_pop: dict[str, int] = field(default_factory=dict)
    certifying_codes: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        fps: Mapping[int, Fingerprint],
        listings: Mapping[int, Listing],
        settings: Settings,
        generation: str = GENERATION,
    ) -> "Calibration":
        """Read the six cohort statistics off a seed cohort — the batch pass's own inputs."""
        index = BlockIndex(settings)
        for listing_id in sorted(fps):
            index.add(fps[listing_id])
        index.finalize()
        ctx = FeatureContext.build(fps, settings, None)
        ctx.index_reference_codes(listings)
        ctx.index_attrs(fps, listings)
        certifying: set[str] = set()
        for codes in ctx.codes.values():
            certifying |= set(codes)
        return cls(
            generation=generation,
            feature_version=FEATURE_VERSION,
            built_at=_now(),
            n_listings=len(fps),
            price_cuts={_cut_key(*key): list(cuts) for key, cuts in index.price_cuts.items()},
            exploded={probe: sorted(key_token(key) for key in keys)
                      for probe, keys in index.exploded.items()},
            block_docs=dict(ctx.block_docs),
            block_token_df={block: dict(df) for block, df in ctx.block_token_df.items()},
            corpus_docs=ctx.corpus_docs,
            corpus_token_df=dict(ctx.corpus_token_df),
            block_attr_df={
                block: {key_token(pair): count for pair, count in bucket.items()}
                for block, bucket in ctx.block_attr_df.items()
            },
            pin_pop=dict(ctx.pin_pop),
            certifying_codes=sorted(certifying),
        )

    def digest(self) -> str:
        """A CONTENT hash of the frozen inputs — a pass stamps it so a decision can never be
        read back under a calibration it was not taken under.

        `built_at` is excluded deliberately: it says WHEN the calibration was cut, not what it
        contains, and a digest that moved every time the same cohort was re-cut would certify
        nothing. Two identical calibrations must hash identically or E70's stamp is decoration."""
        payload = dict(self.to_json())
        payload.pop("built_at", None)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]

    def to_json(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "feature_version": self.feature_version,
            "built_at": self.built_at,
            "n_listings": self.n_listings,
            "price_cuts": self.price_cuts,
            "exploded": self.exploded,
            "block_docs": self.block_docs,
            "block_token_df": self.block_token_df,
            "corpus_docs": self.corpus_docs,
            "corpus_token_df": self.corpus_token_df,
            "block_attr_df": self.block_attr_df,
            "pin_pop": self.pin_pop,
            "certifying_codes": self.certifying_codes,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "Calibration":
        return cls(
            generation=str(raw.get("generation") or GENERATION),
            feature_version=int(raw.get("feature_version") or FEATURE_VERSION),
            built_at=str(raw.get("built_at") or ""),
            n_listings=int(raw.get("n_listings") or 0),
            price_cuts={k: [float(v) for v in vals]
                        for k, vals in (raw.get("price_cuts") or {}).items()},
            exploded={k: list(vals) for k, vals in (raw.get("exploded") or {}).items()},
            block_docs=dict(raw.get("block_docs") or {}),
            block_token_df={k: dict(v) for k, v in (raw.get("block_token_df") or {}).items()},
            corpus_docs=int(raw.get("corpus_docs") or 0),
            corpus_token_df=dict(raw.get("corpus_token_df") or {}),
            block_attr_df={k: dict(v) for k, v in (raw.get("block_attr_df") or {}).items()},
            pin_pop=dict(raw.get("pin_pop") or {}),
            certifying_codes=list(raw.get("certifying_codes") or []),
        )


class Keyer:
    """`index_keys` / `probe_keys` under a frozen calibration.

    It IS `BlockIndex` — the six probes are never spelled twice — with the two cohort-derived
    structures injected instead of computed: `price_cuts` (K3's decile) and `exploded`."""

    def __init__(self, settings: Settings, calibration: Calibration) -> None:
        index = BlockIndex(settings)
        cuts: dict[tuple[str | None, str | None], list[float]] = {}
        for token, values in calibration.price_cuts.items():
            left, _, right = token.partition(_SEP)
            cuts[(left or None, right or None)] = list(values)
        index.price_cuts = cuts
        index.exploded = {probe: set(calibration.exploded.get(probe, ()))
                          for probe in PROBE_PRIORITY}
        index._keys_ready = True  # the cuts are injected, so key derivation is ready
        self.index = index
        self.settings = settings

    def index_keys(self, fp: Fingerprint) -> list[tuple[str, str]]:
        return [(probe, key_token(key)) for probe, key in self.index.index_keys(fp)]

    def probe_keys(self, fp: Fingerprint) -> list[tuple[str, str]]:
        return [(probe, key_token(key)) for probe, key in self.index.probe_keys(fp)]

    def is_exploded(self, probe: str, token: str) -> bool:
        return token in self.index.exploded[probe]

    def exploded_probes(self, fp: Fingerprint) -> set[str]:
        return {probe for probe, token in self.index_keys(fp) if self.is_exploded(probe, token)}


def context_for(
    calibration: Calibration,
    settings: Settings,
    fps: Mapping[int, Fingerprint],
    listings: Mapping[int, Listing],
) -> FeatureContext:
    """A `FeatureContext` over the WORKING SET whose cohort statistics are the frozen ones.

    The per-listing halves (`tfidf`, `rare`, `codes`) are derived here because they are cheap
    and local; the cohort halves are never recomputed, which is E70."""
    ctx = FeatureContext(settings=settings)
    ctx.block_docs = dict(calibration.block_docs)
    ctx.block_token_df = {block: dict(df) for block, df in calibration.block_token_df.items()}
    ctx.corpus_docs = calibration.corpus_docs
    ctx.corpus_token_df = dict(calibration.corpus_token_df)
    ctx.pin_pop = dict(calibration.pin_pop)
    ctx.block_attr_df = {
        block: {tuple(token.split(_SEP, 1)): count for token, count in bucket.items()}  # type: ignore[misc]
        for block, bucket in calibration.block_attr_df.items()
    }
    certifying = set(calibration.certifying_codes)
    for listing_id, fp in fps.items():
        block = _block_of(fp)
        df = ctx.block_token_df.setdefault(block, {})
        n_docs = max(1, ctx.block_docs.get(block, 1))
        counts: dict[str, int] = {}
        for token in fp.desc_tokens or ():
            counts[token] = counts.get(token, 0) + 1
        ctx.tfidf[listing_id] = _tfidf_vector(counts, df, n_docs)
        ctx.tfidf_corpus[listing_id] = _tfidf_vector(
            counts, ctx.corpus_token_df, max(1, ctx.corpus_docs)
        )
        ctx.rare[listing_id] = (
            {token for token in counts if df.get(token, 0) <= settings.rare_token_df}
            if n_docs >= MIN_RARE_BLOCK_DOCS
            else set()
        )
        listing = listings.get(listing_id)
        if listing is not None:
            kept = frozenset(code for code in reference_codes(listing.description)
                             if code in certifying)
            if kept:
                ctx.codes[listing_id] = kept
    return ctx


def attr_deltas(fp: Fingerprint, listing: Listing, settings: Settings) -> list[tuple[str, str]]:
    """The `(block, attr-token)` pairs one listing contributes to `block_attr_df`."""
    block = _block_of(fp)
    return [(block, key_token(pair))
            for pair in _feature_attr_map(listing, vocabulary_attr_keys(settings))]


# ------------------------------------------------------------------------------- the store


class Store(Protocol):
    """Everything the lane persists, in the shape SQL serves it.

    The replay drives the identical interface through an in-memory twin, so a divergence
    between the two paths is a bug in one implementation, never in the proof."""

    def lookup(self, probe: str, token: str) -> list[int]: ...
    def lookup_many(self, keys: Sequence[tuple[str, str]]
                    ) -> dict[tuple[str, str], list[int]]: ...
    def put_listing(self, listing_id: int, row: FpRow,
                    keys: Sequence[tuple[str, str]]) -> None: ...
    def drop_listing(self, listing_id: int) -> None: ...
    def keys_many(self, ids: Iterable[int]) -> dict[int, list[tuple[str, str]]]: ...
    def rows(self, ids: Iterable[int]) -> dict[int, FpRow]: ...
    def known(self, ids: Iterable[int]) -> set[int]: ...
    def pairs_touching(self, ids: Iterable[int]) -> dict[tuple[int, int], PairRow]: ...
    def pairs_within(self, members: Iterable[int]) -> list[PairRow]: ...
    def merge_neighbours(self, ids: Iterable[int]) -> dict[int, set[int]]: ...
    def upsert_pairs(self, rows: Sequence[PairRow]) -> None: ...
    def delete_pairs(self, keys: Sequence[tuple[int, int]]) -> None: ...
    def clusters_touching(self, members: Iterable[int]) -> dict[int, list[int]]: ...
    def write_clusters(self, drop_keys: Sequence[int], rows: Sequence[dict[str, Any]],
                       conflicts: Sequence[dict[str, Any]]) -> None: ...
    def must_not_link(self) -> set[tuple[int, int]]: ...
    def cells(self, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], CellRow]: ...
    def bump_cell(self, listing: Listing) -> None: ...
    def unbump_cell(self, cell: tuple[str, str]) -> None: ...
    def stamped_merges(self, blocks: Sequence[str]
                       ) -> list[tuple[int, int, ContextStamp, str, bool]]: ...
    def flush(self) -> None: ...


class FactSource(Protocol):
    """The read-only half: listing facts and galleries out of `public` (D4 — never written)."""

    def facts(self, ids: Iterable[int]) -> dict[int, tuple[Listing, list[Image]]]: ...


@dataclass(slots=True, frozen=True)
class WorkItem:
    """One claimed listing and WHEN it arrived, per feed.

    The arrival stamp is the feed's own event time — a new row's `first_seen_at`, a content
    change's `scraped_at`, a flip's `inactive_at` — and never the listing's age, so
    "arrival to decision" measures the lane's latency rather than how old the advert is."""

    listing_id: int
    feed: str
    arrived_at: float | None = None
    # The feed's own cursor value for this row — a listing id, a snapshot id, an
    # `(inactive_at, id)` pair. The watermark is the MAXIMUM over the items a pass committed,
    # so a claim that was refused advances nothing.
    cursor: Any = None
    # The scope no longer holds this listing (E79). It is RETIRED rather than refreshed: its
    # postings, its fingerprint row and its pairs go, and the neighbours it linked re-cluster
    # without it. Half-indexed is worse than unindexed — every neighbour would go on
    # retrieving it through postings nothing maintains.
    retire: bool = False


class WorkSource(Protocol):
    """The watermark feeds, bounded. Five index-served cursors in production; an arrival
    schedule in the replay."""

    def claim(self, limit: int) -> list[WorkItem]: ...
    def commit(self, done: Sequence[WorkItem]) -> dict[str, Any]: ...


# ------------------------------------------------------------------------------ retrieval


def retrieve(
    fp: Fingerprint,
    keyer: Keyer,
    store: Store,
    guards: Mapping[int, GuardRow],
    settings: Settings,
    vetoed: dict[tuple[int, int], str],
) -> dict[int, set[str]]:
    """`BlockIndex.candidates` over SQL postings — the same order, the same cap, the same guards.

    Fill order is `PROBE_PRIORITY` then the listing's own `probe_keys` order then ascending
    listing id, exactly as the cohort pass fills it, so the cap can only ever discard the
    weakest evidence class (E17). `tests/autodedup/test_incremental.py` asserts this against
    `BlockIndex.candidates` over the whole cohort rather than trusting the restatement."""
    by_probe: dict[str, list[str]] = {probe: [] for probe in PROBE_PRIORITY}
    wanted: list[tuple[str, str]] = []
    for probe, token in keyer.probe_keys(fp):
        by_probe[probe].append(token)
        if not keyer.is_exploded(probe, token):
            wanted.append((probe, token))
    # ONE statement for the whole listing (E74), not one per key: the posting lists are read
    # in `PROBE_PRIORITY` order below, so batching the fetch cannot move the fill order.
    postings = store.lookup_many(wanted)
    out: dict[int, set[str]] = {}
    cap = settings.max_candidates_per_listing
    for probe in PROBE_PRIORITY:
        for token in by_probe[probe]:
            if keyer.is_exploded(probe, token):
                continue
            for other in postings.get((probe, token), ()):
                if other == fp.listing_id:
                    continue
                hit = out.get(other)
                if hit is not None:
                    hit.add(probe)
                    continue
                if len(out) >= cap:
                    continue
                pair = (fp.listing_id, other) if fp.listing_id < other else (other, fp.listing_id)
                if pair in vetoed:
                    continue
                row = guards.get(other)
                if row is None:
                    continue
                veto = pair_veto(fp, row, settings)
                if veto is not None:
                    vetoed[pair] = veto
                    continue
                out[other] = {probe}
    return out


def neighbourhood(
    keyer: Keyer, store: Store, keys: Iterable[tuple[str, str]]
) -> set[int]:
    """Every listing whose own retrieval could move because a listing under `keys` changed.

    The ±1 relation on area band and price decile is symmetric, so "the listings that PROBE
    one of these keys" is exactly "the listings these keys' probes reach" — one lookup set,
    not a second widening."""
    wanted = sorted({key for key in keys if not keyer.is_exploded(key[0], key[1])})
    out: set[int] = set()
    for ids in store.lookup_many(wanted).values():
        out.update(ids)
    return out


# -------------------------------------------------------------------------------- the pass


@dataclass(slots=True)
class Limits:
    """Everything one pass is allowed to spend, and what it does when it cannot fit.

    `max_pairs` is a SCHEDULING bound, never a correctness one: a pass that scored the first
    20,000 of a wanted set and wrote them would leave the rest of the neighbourhood's pairs
    undecided, re-cluster from a partial edge set and then advance the watermark over the
    difference — measured on g6 at a cap of 500, that manufactures 38 member sets the cohort
    pass never produces. So the pass REFUSES instead (E75) and `run_pass_bounded` re-claims a
    smaller slice, which is the same work at the same answer."""

    max_listings: int = 500
    # Sized from the measurement rather than from a round number: the densest pass over the g6
    # cohort wants {{WANTED_MAX}} pairs at a 200-listing claim, and a block seen for the FIRST
    # time has to score its whole pair set however small the claim is (which is what the seed
    # backfill, E76, exists to pay once).
    max_pairs: int = 150_000
    max_component: int = 400


@dataclass(slots=True)
class PassResult:
    generation: str
    calibration_digest: str
    claimed: list[int] = field(default_factory=list)
    dirty: int = 0
    pairs_scored: int = 0
    pairs_written: int = 0
    pairs_deleted: int = 0
    components: int = 0
    clusters_written: int = 0
    clusters_dropped: int = 0
    retired: int = 0
    rail: dict[str, int] = field(default_factory=dict)
    zones: dict[str, int] = field(default_factory=dict)
    certificates: dict[str, int] = field(default_factory=dict)
    latency_s: list[float] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    spent_usd: float = 0.0
    oversized_components: list[int] = field(default_factory=list)
    cursors: dict[str, Any] = field(default_factory=dict)
    feeds: dict[str, int] = field(default_factory=dict)
    # Set when the wanted pair set did not fit `max_pairs`. The pass then wrote no pair, no
    # cluster and no cursor: `aborted` is the reason a caller shrinks the claim and retries.
    aborted: str = ""
    wanted_pairs: int = 0
    attempts: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "calibration_digest": self.calibration_digest,
            "counts": {
                "claimed": len(self.claimed),
                "dirty": self.dirty,
                "pairs_scored": self.pairs_scored,
                "pairs_written": self.pairs_written,
                "pairs_deleted": self.pairs_deleted,
                "components": self.components,
                "clusters_written": self.clusters_written,
                "clusters_dropped": self.clusters_dropped,
                "retired": self.retired,
                "wanted_pairs": self.wanted_pairs,
                "attempts": self.attempts,
            },
            "feeds": dict(self.feeds),
            "aborted": self.aborted,
            "zones": dict(self.zones),
            "certificates": dict(self.certificates),
            "rail": dict(self.rail),
            "latency_s": _latency(self.latency_s),
            "timings": {k: round(v, 4) for k, v in self.timings.items()},
            # D19: no LLM judge has merge authority on the band, and this lane runs none.
            "spent_usd": self.spent_usd,
            "oversized_components": self.oversized_components,
            "cursors": self.cursors,
        }


def _latency(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50": round(_percentile(ordered, 0.50), 3),
        "p95": round(_percentile(ordered, 0.95), 3),
        "max": round(ordered[-1], 3),
        "mean": round(sum(ordered) / len(ordered), 3),
    }


def _percentile(ordered: Sequence[float], q: float) -> float:
    rank = max(1, min(len(ordered), math.ceil(q * len(ordered))))
    return float(ordered[rank - 1])


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def fp_digest(fp: Fingerprint, images: Sequence[Image]) -> str:
    """What a re-score depends on: if this moves, every pair touching the listing is re-decided.

    Cheap and total — the whole fingerprint plus the gallery's hashes — because a digest that
    misses a field is a stale decision nobody can see."""
    payload = json.dumps(
        [
            fp.source, fp.block_key, fp.cat_group, fp.category_main, fp.category_type,
            fp.area_m2, fp.area_band, fp.disposition, fp.floor, fp.total_floors,
            fp.broker_key, fp.street_key, fp.house_number, fp.ruian_adm_kod, fp.psc,
            fp.lat, fp.lon, fp.pin_key, fp.price, sorted(fp.price_events),
            fp.desc_simhash, len(fp.desc_tokens), sorted(fp.all_hashes),
            sorted(fp.image_hashes), fp.n_images, fp.country_status,
            fp.first_seen_at, fp.last_seen_at, fp.inactive_at, fp.is_active,
            sorted((img.image_id, img.phash, img.pop, img.seq) for img in images),
        ],
        sort_keys=True, default=str,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


class _Working:
    """Fingerprints, listings and galleries of one pass's working set, fetched once."""

    def __init__(self, facts: FactSource, settings: Settings) -> None:
        self.facts = facts
        self.settings = settings
        self.listings: dict[int, Listing] = {}
        self.images: dict[int, list[Image]] = {}
        self.fps: dict[int, Fingerprint] = {}

    def ensure(self, ids: Iterable[int]) -> None:
        missing = [i for i in ids if i not in self.fps]
        if not missing:
            return
        for listing_id, (listing, images) in self.facts.facts(missing).items():
            self.listings[listing_id] = listing
            self.images[listing_id] = list(images)
            self.fps[listing_id] = build_fingerprint(listing, images, self.settings)


class _Overlay:
    """The pass's OWN writes, held back until the budget has agreed to them (E75).

    A pass refuses a claim it cannot fit, and the refusal has to leave nothing behind: a store
    that already held the new postings would tell the next, smaller attempt that those listings
    are unchanged, and their pairs would never be scored at all. So steps 1-4 read and write
    through this view — postings, fingerprint rows and census cells — and `apply` is the single
    moment it all lands. Everything else delegates, because nothing else is written before the
    budget check."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.fp: dict[int, FpRow] = {}
        self.keys: dict[int, list[tuple[str, str]]] = {}
        # Buffered postings, indexed BY KEY rather than by listing: retrieval asks for a
        # listing's keys thousands of times a pass, and a scan of the whole buffer per ask
        # would make the overlay cost more than the pass it protects.
        self.added: dict[tuple[str, str], set[int]] = {}
        self.dropped: dict[tuple[str, str], set[int]] = {}
        self.cell_rows: dict[tuple[str, str], CellRow] = {}
        self.ops: list[tuple[str, Any]] = []
        # Retired listings (E79), held back like everything else until the budget agrees.
        self.gone: set[int] = set()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    # -------------------------------------------------------------------- buffered writes
    def put_listing(self, listing_id: int, row: FpRow, keys: Sequence[tuple[str, str]],
                    old_keys: Sequence[tuple[str, str]] = ()) -> None:
        self.fp[listing_id] = row
        self.keys[listing_id] = list(keys)
        fresh = set(keys)
        for key in old_keys:
            if key not in fresh:
                self.dropped.setdefault(key, set()).add(listing_id)
            self.added.get(key, set()).discard(listing_id)
        for key in fresh:
            self.added.setdefault(key, set()).add(listing_id)
            self.dropped.get(key, set()).discard(listing_id)

    def drop_listing(self, listing_id: int, old_keys: Sequence[tuple[str, str]] = ()) -> None:
        """Retire a listing the scope no longer holds. The caller has already read its keys,
        so they are passed in rather than re-read."""
        listing_id = int(listing_id)
        for key in self.keys.pop(listing_id, ()):
            self.added.get(key, set()).discard(listing_id)
        self.fp.pop(listing_id, None)
        for key in old_keys:
            self.dropped.setdefault(key, set()).add(listing_id)
            self.added.get(key, set()).discard(listing_id)
        self.gone.add(listing_id)

    def bump_cell(self, listing: Listing) -> None:
        cell = (address_block_key(listing), category_group(listing))
        row = self._cell(cell)
        row.n_listings += 1
        self.ops.append(("bump", listing))

    def unbump_cell(self, cell: tuple[str, str]) -> None:
        row = self._cell((str(cell[0]), str(cell[1])))
        row.n_listings = max(0, row.n_listings - 1)
        self.ops.append(("unbump", (str(cell[0]), str(cell[1]))))

    def _cell(self, cell: tuple[str, str]) -> CellRow:
        if cell not in self.cell_rows:
            live = self.store.cells([cell]).get(cell)
            self.cell_rows[cell] = CellRow(
                cell[0], cell[1], live.n_listings if live else 0,
                list(live.shapes) if live else [], list(live.brokers) if live else [],
                list(live.source_ids) if live else [], bool(live.capped) if live else False)
        return self.cell_rows[cell]

    # --------------------------------------------------------------------- buffered reads
    def lookup_many(self, keys: Sequence[tuple[str, str]]
                    ) -> dict[tuple[str, str], list[int]]:
        out = {key: list(ids) for key, ids in self.store.lookup_many(keys).items()}
        for key in keys:
            for listing_id in self.dropped.get(key, ()):
                if listing_id in out.get(key, ()):
                    out[key].remove(listing_id)
            for listing_id in self.added.get(key, ()):
                bucket = out.setdefault(key, [])
                if listing_id not in bucket:
                    # Postings stay ascending: the cap fills in listing-id order (E17).
                    bisect.insort(bucket, listing_id)
        return {key: ids for key, ids in out.items() if ids}

    def rows(self, ids: Iterable[int]) -> dict[int, FpRow]:
        wanted = list(ids)
        out = dict(self.store.rows([i for i in wanted if i not in self.fp]))
        out.update({i: self.fp[i] for i in wanted if i in self.fp})
        return {i: row for i, row in out.items() if i not in self.gone}

    def known(self, ids: Iterable[int]) -> set[int]:
        wanted = set(ids)
        return (self.store.known(wanted) | (wanted & set(self.fp))) - self.gone

    def keys_many(self, ids: Iterable[int]) -> dict[int, list[tuple[str, str]]]:
        wanted = list(ids)
        out = dict(self.store.keys_many(wanted))
        out.update({i: list(self.keys[i]) for i in wanted if i in self.keys})
        return {i: keys for i, keys in out.items() if i not in self.gone}

    def cells(self, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], CellRow]:
        wanted = list(keys)
        out = dict(self.store.cells(wanted))
        out.update({key: self.cell_rows[key] for key in wanted if key in self.cell_rows})
        return out

    # --------------------------------------------------------------------------- the gate
    def apply(self) -> None:
        # Retirements first: a store that dropped a listing AFTER re-writing its postings
        # would have written rows it then has to find again.
        for listing_id in sorted(self.gone):
            self.store.drop_listing(listing_id)
        for listing_id in sorted(self.keys):
            self.store.put_listing(listing_id, self.fp[listing_id], self.keys[listing_id])
        for verb, payload in self.ops:
            if verb == "bump":
                self.store.bump_cell(payload)
            else:
                self.store.unbump_cell(payload)
        self.fp.clear()
        self.keys.clear()
        self.added.clear()
        self.dropped.clear()
        self.cell_rows.clear()
        self.ops.clear()
        self.gone.clear()


def run_pass(
    store: Store,
    facts: FactSource,
    work: WorkSource,
    settings: Settings,
    model: LogisticModel,
    calibration: Calibration,
    limits: Limits | None = None,
    generation: str = GENERATION,
    now: float | None = None,
) -> PassResult:
    """One bounded, idempotent incremental pass. Re-running it on an unchanged corpus is a no-op.

    The order is the cohort pass's order, restricted: refresh the fingerprints that moved,
    widen to the probe-key neighbourhood (E71), retrieve, score what is new or stale, write the
    pair rows, re-cluster the touched components (E72), then run E64's rail over the census.

    Every write lands before the watermark moves, and the watermark moves only when the whole
    claim was decided — a pass that cannot fit its pair set writes nothing at all (E75)."""
    # E83 is a COHORT-wide index (who else carries each frame) and this pass sees one claim at
    # a time, so it would silently subtract what the batch pass keeps and break E70's replay
    # equivalence. An incremental path that wants it owes the index first.
    if settings.catalog_carrier_aware:
        raise NotImplementedError(
            "catalog_carrier_aware (E83) has no incremental carrier index yet: a pass that "
            "cannot see a frame's other carriers would decide it differently from the cohort "
            "pass, and E70's replay equivalence is what says the two agree"
        )
    # E85 is decided over the whole K-B FAMILY, and a family is a property of the pair set this
    # pass sees only a slice of. The batch side is built (`family.refusals`); the incremental
    # side owes a stored family index plus E64's re-evaluation rail extended to K-B — a pair
    # certified yesterday has to fall back to the BAND when tomorrow's arrival makes its family
    # impure. Until that exists a pass would certify what the cohort pass refuses, which is
    # exactly the divergence E70's replay equivalence is there to catch, so it fails loudly.
    if settings.family_guard_mode != "off":
        raise NotImplementedError(
            "family_guard_mode (E85) has no incremental family index or re-evaluation rail "
            "yet: a pass that sees one claim cannot read the whole K-B family, and a family "
            "only ever grows — the rail owed is E64's, triggered by a family gaining a member "
            "rather than by a block census growing, with K-B the one certificate E64's "
            "certificate exemption must not cover"
        )
    caps = limits or Limits()
    result = PassResult(generation=generation, calibration_digest=calibration.digest())
    clock = time.perf_counter()

    items = list(work.claim(caps.max_listings))
    seen_ids: set[int] = set()
    # A listing claimed for RETIREMENT is never also refreshed, whichever feed saw it: the
    # scope no longer holds it, so there is nothing to refresh it into.
    retire_ids = {item.listing_id for item in items if item.retire}
    claimed = [item.listing_id for item in items
               if item.listing_id not in retire_ids
               and not (item.listing_id in seen_ids or seen_ids.add(item.listing_id))]
    result.claimed = list(claimed)
    for item in items:
        result.feeds[item.feed] = result.feeds.get(item.feed, 0) + 1
    if not claimed and not retire_ids:
        # An idle pass is still the only moment an operator's must-not-link row can be
        # honoured: it moves no pair, so nothing else would ever seed its component (E78).
        operator_mnl = store.must_not_link()
        seeds = _mnl_seeds(store, operator_mnl)
        if seeds:
            _recluster(store, facts, settings, _Working(facts, settings), seeds, caps,
                       result, operator_mnl)
        store.flush()
        result.cursors = work.commit(items)
        result.timings["total_s"] = time.perf_counter() - clock
        return result

    keyer = Keyer(settings, calibration)
    working = _Working(facts, settings)
    working.ensure(claimed)
    # Everything this pass writes before the budget check goes through the overlay, so a
    # refusal leaves the store exactly as it found it (E75).
    view = _Overlay(store)

    # --- 1. refresh the changed listings' fingerprints and postings ------------------------
    # Two batched reads for the whole claim, not two statements per listing (E74).
    stored_rows = view.rows(claimed)
    stored_keys = view.keys_many(claimed)
    changed: set[int] = set()
    touched_keys: list[tuple[str, str]] = []
    touched_blocks: set[str] = set()

    # --- 1a. E79: retire what the scope no longer holds ------------------------------------
    # A retired listing's keys are TOUCHED keys, so step 2 widens to everything that could
    # retrieve it; its stored pairs are then absent from the wanted set, which is what deletes
    # them, and step 6 re-clusters the components those edges held together.
    retired: set[int] = set()
    if retire_ids:
        departed = sorted(view.known(retire_ids))
        gone_rows = view.rows(departed)
        gone_keys = view.keys_many(departed)
        for listing_id in departed:
            old = gone_rows.get(listing_id)
            old_keys = sorted(set(gone_keys.get(listing_id, ())))
            if old is not None:
                view.unbump_cell(old.cell)
                touched_blocks.add(old.cell_key)
            touched_keys.extend(old_keys)
            view.drop_listing(listing_id, old_keys)
            retired.add(listing_id)
    result.retired = len(retired)

    for listing_id in claimed:
        fp = working.fps.get(listing_id)
        if fp is None:
            continue
        listing = working.listings[listing_id]
        digest = fp_digest(fp, working.images.get(listing_id, ()))
        old = stored_rows.get(listing_id)
        old_keys = sorted(set(stored_keys.get(listing_id, ())))
        # DISTINCT and sorted on both sides: `index_keys` repeats a key whenever two images
        # share a pHash band, and the posting list is a set — a pass that read that difference
        # as a change would re-score the whole corpus on every drain.
        new_keys = sorted(set(keyer.index_keys(fp)))
        if old is not None and old.digest == digest and old_keys == new_keys:
            continue  # idempotence: an unchanged listing costs a digest, not a pass
        cell = (address_block_key(listing), category_group(listing))
        if old is not None:
            # The cell it LEFT, read off the stored row — never re-derived from today's facts,
            # which would leave a phantom member in the old cell for ever (E64 reads that count).
            view.unbump_cell(old.cell)
            touched_blocks.add(old.cell_key)
        view.put_listing(
            listing_id,
            FpRow(guard_row(fp), digest, cell[0], cell[1], bool(listing.is_active)),
            new_keys, old_keys,
        )
        view.bump_cell(listing)
        touched_keys.extend(old_keys)
        touched_keys.extend(new_keys)
        touched_blocks.add(cell[0])
        changed.add(listing_id)
    result.timings["refresh_s"] = time.perf_counter() - clock

    # --- 2. E71: the dirty set is the probe-key neighbourhood ------------------------------
    clock = time.perf_counter()
    dirty = set(changed) | neighbourhood(keyer, view, set(touched_keys))
    dirty &= view.known(dirty)
    result.dirty = len(dirty)
    working.ensure(dirty)

    # --- 3. retrieval for every dirty listing, against the CURRENT corpus ------------------
    # The postings and the candidates' guard rows are prefetched in two statements, so the
    # retrieval loop below is pure computation over what is already in hand.
    local_guards = {i: guard_row(working.fps[i]) for i in dirty if i in working.fps}
    probe_keys: set[tuple[str, str]] = set()
    for listing_id in sorted(dirty):
        fp = working.fps.get(listing_id)
        if fp is None:
            continue
        probe_keys.update(key for key in keyer.probe_keys(fp)
                          if not keyer.is_exploded(key[0], key[1]))
    reachable: set[int] = set()
    for ids in view.lookup_many(sorted(probe_keys)).values():
        reachable.update(ids)
    fetched = view.rows(sorted(reachable - set(local_guards)))
    lookup = _GuardLookup(view, local_guards, {i: row.guard for i, row in fetched.items()})
    vetoed: dict[tuple[int, int], str] = {}
    cand: dict[int, dict[int, set[str]]] = {}
    for listing_id in sorted(dirty):
        fp = working.fps.get(listing_id)
        if fp is None:
            continue
        cand[listing_id] = retrieve(fp, keyer, view, lookup, settings, vetoed)
    result.timings["retrieve_s"] = time.perf_counter() - clock

    # --- 4. the pair set: either side retrieving the other keeps it ------------------------
    clock = time.perf_counter()
    stored = store.pairs_touching(set(dirty) | retired)
    wanted: dict[tuple[int, int], dict[str, Any]] = {}
    for listing_id, found in cand.items():
        for other, probes in found.items():
            lo, hi = (listing_id, other) if listing_id < other else (other, listing_id)
            entry = wanted.setdefault(
                (lo, hi), {"probes": set(), "from_lo": False, "from_hi": False})
            entry["probes"] |= probes
            if listing_id == lo:
                entry["from_lo"] = True
            else:
                entry["from_hi"] = True
    # A pair with one clean side keeps the clean side's own direction, which did not move —
    # unless that side was RETIRED, in which case the clean direction is the stale one and
    # keeping it would resurrect the pair the retirement exists to drop.
    for (lo, hi), row in stored.items():
        if lo in retired or hi in retired:
            continue
        entry = wanted.get((lo, hi))
        if lo not in cand and row.from_lo:
            entry = entry or {"probes": set(), "from_lo": False, "from_hi": False}
            entry["from_lo"] = True
            entry["probes"] |= set(row.probes)
            wanted[(lo, hi)] = entry
        if hi not in cand and row.from_hi:
            entry = wanted.get((lo, hi)) or {"probes": set(), "from_lo": False,
                                             "from_hi": False}
            entry["from_hi"] = True
            entry["probes"] |= set(row.probes)
            wanted[(lo, hi)] = entry

    result.wanted_pairs = len(wanted)
    # E75: the budget is refused BEFORE the first write, because a partial pair set re-clusters
    # from a partial edge set and the watermark would then step over the difference for ever.
    if len(wanted) > caps.max_pairs:
        # Nothing of this pass has reached the store yet, so the refusal IS the rollback.
        result.aborted = "pair_budget"
        result.timings["total_s"] = sum(
            value for key, value in result.timings.items() if key.endswith("_s"))
        return result

    view.apply()
    dropped = [key for key in stored if key not in wanted]
    if dropped:
        store.delete_pairs(dropped)
        result.pairs_deleted = len(dropped)

    # --- 5. score what is new or stale -----------------------------------------------------
    working.ensure({i for pair in wanted for i in pair})
    endpoints = {i for pair in wanted for i in pair}
    digests = {i: fp_digest(working.fps[i], working.images.get(i, ()))
               for i in endpoints if i in working.fps}
    ctx = context_for(calibration, settings, working.fps, working.listings)
    census = _census(store, working.listings, working.images)
    rows: list[PairRow] = []
    for (lo, hi) in sorted(wanted):
        entry = wanted[(lo, hi)]
        if lo not in working.fps or hi not in working.fps:
            continue
        previous = stored.get((lo, hi))
        dlo = digests.get(lo, "")
        dhi = digests.get(hi, "")
        if (previous is not None and previous.fp_lo == dlo and previous.fp_hi == dhi
                and lo not in changed and hi not in changed):
            if (set(previous.probes) != entry["probes"] or previous.from_lo != entry["from_lo"]
                    or previous.from_hi != entry["from_hi"]):
                previous.probes = sorted(entry["probes"])
                previous.from_lo = bool(entry["from_lo"])
                previous.from_hi = bool(entry["from_hi"])
                rows.append(previous)
            continue
        fa, fb = working.fps[lo], working.fps[hi]
        la, lb = working.listings[lo], working.listings[hi]
        feats: Feats = pair_features(fa, fb, la, lb, working.images.get(lo, ()),
                                     working.images.get(hi, ()), ctx, settings)
        decision = decide_pair(fa, fb, la, lb, feats, entry["probes"], model, settings, census)
        result.pairs_scored += 1
        result.zones[decision.zone] = result.zones.get(decision.zone, 0) + 1
        if decision.certificate:
            result.certificates[decision.certificate] = (
                result.certificates.get(decision.certificate, 0) + 1)
        evidence = dict(decision.evidence)
        if decision.certificate == "K-R":
            evidence["ref_codes"] = ",".join(ctx.shared_codes(lo, hi))
        rows.append(PairRow(
            lo=lo, hi=hi, probes=sorted(entry["probes"]),
            from_lo=bool(entry["from_lo"]), from_hi=bool(entry["from_hi"]),
            zone=decision.zone, score=decision.score, families=sorted(decision.families),
            certificate=decision.certificate, veto=decision.veto, reason=decision.reason,
            evidence=evidence,
            # `block` is not part of PairContext: it is the per-block cap E64 counts on,
            # and the rail needs it off the stored row rather than off a live re-read.
            context={**census.pair_context(la, lb).to_json(),
                     "block": address_block_key(la)},
            fp_lo=dlo, fp_hi=dhi, feats=feats,
        ))
    store.upsert_pairs(rows)
    result.pairs_written = len(rows)
    result.timings["score_s"] = time.perf_counter() - clock

    # --- 6. E72: re-cluster the touched components ----------------------------------------
    # Only a component whose MERGE edges or vetoes moved can cluster differently, so a pass
    # that scores a thousand rejects re-clusters nothing. A pair that merely changed its
    # probes or its band score leaves the component's edge set alone.
    clock = time.perf_counter()
    seeds: set[int] = set()
    for row in rows:
        previous = stored.get((row.lo, row.hi))
        was = previous.zone if previous is not None else None
        if row.zone == "merge" or was == "merge" or row.veto == UNIT_DESIGNATOR_VETO:
            seeds |= {row.lo, row.hi}
    for key in dropped:
        previous = stored.get(key)
        if previous is not None and previous.zone == "merge":
            seeds |= set(key)
    operator_mnl = store.must_not_link()
    seeds |= _mnl_seeds(store, operator_mnl)
    # A retired listing is not a seed: it has no postings, no pairs and no cell any more. Its
    # ex-partners are already seeds (they are the other side of every dropped edge), and they
    # are the component that has to re-cluster without it.
    seeds -= retired
    _recluster(store, facts, settings, working, seeds, caps, result, operator_mnl)
    result.timings["cluster_s"] = time.perf_counter() - clock

    # --- 7. E64: a census that has overtaken a stamped promotion re-opens it to the band ---
    clock = time.perf_counter()
    result.rail = _run_rail(store, facts, settings, working, result, sorted(touched_blocks),
                            operator_mnl)
    result.timings["rail_s"] = time.perf_counter() - clock

    store.flush()
    if now is not None:
        for item in items:
            if item.arrived_at is not None:
                result.latency_s.append(max(0.0, now - item.arrived_at))
    result.cursors = work.commit(items)
    result.timings["total_s"] = sum(
        value for key, value in result.timings.items() if key.endswith("_s")
    )
    return result


def run_pass_bounded(
    store: Store,
    facts: FactSource,
    work: WorkSource,
    settings: Settings,
    model: LogisticModel,
    calibration: Calibration,
    limits: Limits | None = None,
    generation: str = GENERATION,
    now: float | None = None,
    shrink: int = 4,
    attempts: int = 5,
) -> PassResult:
    """`run_pass`, re-claiming a SMALLER slice when the pair budget refused the last one.

    An aborted pass has written nothing and advanced no cursor (E75), so a retry is the same
    work over fewer arrivals rather than a resumption of a half-written one. When even a
    single-listing claim will not fit, the lane STOPS: a neighbourhood that alone exceeds the
    budget is a block worth an operator's eye, not a number to quietly truncate."""
    caps = limits or Limits()
    result = run_pass(store, facts, work, settings, model, calibration, caps, generation, now)
    tries = 1
    while result.aborted and tries < attempts and caps.max_listings > 1:
        caps = replace(caps, max_listings=max(1, caps.max_listings // shrink))
        result = run_pass(store, facts, work, settings, model, calibration, caps, generation,
                          now)
        tries += 1
    result.attempts = tries
    return result


class _GuardLookup:
    """`guards.get(other)` over the working set first, the prefetched rows second, the store
    last — so retrieval never builds a fingerprint for a candidate the rule floor is about to
    refuse, and never spends a statement on one the pass already read."""

    def __init__(self, store: Store, local: Mapping[int, GuardRow],
                 prefetched: Mapping[int, GuardRow] | None = None) -> None:
        self.store = store
        self.local = local
        self.cache: dict[int, GuardRow | None] = dict(prefetched or {})

    def get(self, listing_id: int) -> GuardRow | None:
        row = self.local.get(listing_id)
        if row is not None:
            return row
        if listing_id in self.cache:
            return self.cache[listing_id]
        fetched = self.store.rows([listing_id]).get(listing_id)
        guard = fetched.guard if fetched is not None else None
        self.cache[listing_id] = guard
        return guard


def _mnl_seeds(store: Store, must_not_link: Iterable[tuple[int, int]]) -> set[int]:
    """Operator must-not-link rows that TODAY'S clusters contradict.

    A refusal added after the merge it refuses would otherwise never be honoured: re-clustering
    is seeded by pairs whose zone moved, and an operator row moves no pair. The set is
    operator-curated and small, so one membership read per pass settles it."""
    pairs = [(lo, hi) for lo, hi in must_not_link]
    if not pairs:
        return set()
    members: dict[int, int] = {}
    for key, ids in store.clusters_touching({i for pair in pairs for i in pair}).items():
        for listing_id in ids:
            members[listing_id] = key
    seeds: set[int] = set()
    for lo, hi in pairs:
        if lo in members and members[lo] == members.get(hi):
            seeds |= {lo, hi}
    return seeds


def _census(
    store: Store, listings: Mapping[int, Listing], images: Mapping[int, Sequence[Image]]
) -> ContextIndex:
    """The live census (E64), served from the store's counters for the working set's cells."""
    keys = {(address_block_key(listing), category_group(listing))
            for listing in listings.values()}
    cells = {key: row.cell() for key, row in store.cells(keys).items()}
    phashes = {
        listing_id: frozenset(img.phash for img in bucket if img.phash is not None)
        for listing_id, bucket in images.items()
        if any(img.phash is not None for img in bucket)
    }
    # The carrier count is read off the IMAGE, never recounted: `public.images.phash` carries
    # no index and D8 forbids adding one, so the corpus-wide count is one sequential scan the
    # cohort lane runs per pass into `autodedup.phash_pop` and this lane joins per image. That
    # makes it a cohort statistic like the other five, and E70 freezes it with them — a pass
    # that recounted it from what it happened to have seen would move `catalog_ratio`,
    # `anchor_bands` and with them the K4 probe and every certificate reading a catalogue ratio.
    population: dict[int, int] = {}
    for bucket in images.values():
        for image in bucket:
            if image.phash is None or image.pop is None:
                continue
            population[image.phash] = max(population.get(image.phash, 0), int(image.pop))
    return ContextIndex(
        cells=cells,
        listing_phashes=phashes,
        phash_population=population,
        from_price=frozenset(listing_id for listing_id, listing in listings.items()
                             if states_from_price(listing.description)),
    )


def _components(
    store: Store, seeds: Iterable[int], max_size: int
) -> tuple[list[list[int]], list[int]]:
    """Connected components of the merge-edge graph reachable from the seeds, BFS-bounded.

    A component larger than the cap is NOT silently clustered from a partial member set — it is
    named on the pass summary and left to the cohort lane, because a partial component is the
    one input that would make E72's result differ from the batch pass's."""
    seen: set[int] = set()
    out: list[list[int]] = []
    oversized: list[int] = []
    for seed in sorted(set(seeds)):
        if seed in seen:
            continue
        frontier = {seed}
        members: set[int] = set()
        overflow = False
        while frontier:
            members |= frontier
            if len(members) > max_size:
                overflow = True
                break
            nxt: set[int] = set()
            for listing_id, others in store.merge_neighbours(frontier).items():
                nxt |= others
            frontier = nxt - members
        seen |= members
        if overflow:
            oversized.append(seed)
            continue
        out.append(sorted(members))
    return out, oversized


def _recluster(
    store: Store,
    facts: FactSource,
    settings: Settings,
    working: _Working,
    seeds: Iterable[int],
    caps: Limits,
    result: PassResult,
    operator_mnl: frozenset[tuple[int, int]] | set[tuple[int, int]] | None = None,
) -> None:
    components, oversized = _components(store, seeds, caps.max_component)
    result.oversized_components = sorted(set(result.oversized_components) | set(oversized))
    result.components += len(components)
    operator_mnl = store.must_not_link() if operator_mnl is None else operator_mnl
    for members in components:
        working.ensure(members)
        pairs = store.pairs_within(members)
        decisions = [row.decision() for row in pairs]
        vetoed = {(row.lo, row.hi) for row in pairs if row.veto == UNIT_DESIGNATOR_VETO}
        inside = set(members)
        mnl = frozenset(
            {pair for pair in operator_mnl if pair[0] in inside and pair[1] in inside} | vetoed
        )
        fps = {i: working.fps[i] for i in members if i in working.fps}
        listings = {i: working.listings[i] for i in members if i in working.listings}
        clustered = cluster_pairs(decisions, listings, fps, settings, mnl)
        rows = cluster_rows(clustered, decisions, fps)
        existing = store.clusters_touching(members)
        keep = set(clustered.clusters)
        drop = [key for key in existing if key not in keep]
        # Refused unions AND refused bridges: §8 calls these the highest-value rows in the UI,
        # because a conflict is evidence in two directions. An APPLIED bridge is a union and
        # has nothing left to show.
        conflicts = [{**conflict, "kind": "invariant", "generation": result.generation}
                     for conflict in clustered.conflicts]
        conflicts += [{**bridge, "kind": "bridge", "generation": result.generation}
                      for bridge in clustered.bridges if not bridge.get("applied")]
        store.write_clusters(drop, rows, conflicts)
        result.clusters_written += len(rows)
        result.clusters_dropped += len(drop)


def _run_rail(
    store: Store,
    facts: FactSource,
    settings: Settings,
    working: _Working,
    result: PassResult,
    blocks: Sequence[str],
    operator_mnl: frozenset[tuple[int, int]] | set[tuple[int, int]] | None = None,
) -> dict[str, int]:
    """E64: every stamped E63 promotion a census limb has since overtaken goes back to the band.

    Certificates are exempt by construction — a shared order code or a disjoint-window re-post
    is a per-pair FACT no later census can decay — and the re-opening is capped per block, so a
    block that doubles overnight re-opens a few merges rather than all of them. With every
    census limb off (the configuration W8's verification preferred) no census enters a warrant
    and this returns an empty plan, which is the rail costing nothing rather than not existing."""
    if (settings.context_rule_block_min is None
            and settings.context_rule_image_population_min is None) or not blocks:
        return {"reopened": 0, "deferred_by_cap": 0, "blocks_at_cap": 0, "blocks_touched": 0,
                "owed": 0}
    # Only the blocks whose census MOVED this pass: under a frozen calibration (E70) the image
    # population cannot change at all and a block counter changes only where a listing was
    # bumped, so a generation-wide scan would re-confirm what cannot have moved.
    stamped = store.stamped_merges(list(blocks))
    if not stamped:
        return {"reopened": 0, "deferred_by_cap": 0, "blocks_at_cap": 0, "blocks_touched": 0,
                "owed": 0}
    working.ensure({i for lo, hi, _stamp, _block, _cert in stamped for i in (lo, hi)})
    merges = [(lo, hi, stamp, block) for lo, hi, stamp, block, _cert in stamped]
    exempt = frozenset((lo, hi) for lo, hi, _s, _b, cert in stamped if cert)
    live = _census(store, working.listings, working.images)
    actions, counters = rail_plan(
        merges, live, working.listings,
        block_min=settings.context_rule_block_min,
        image_population_min=settings.context_rule_image_population_min,
        max_per_block=settings.context_rail_max_reopen_per_block,
        exempt=exempt,
    )
    demoted: list[PairRow] = []
    live_rows = (store.pairs_touching({action.lo for action in actions}) if actions else {})
    for action in actions:
        row = live_rows.get((action.lo, action.hi))
        if row is None or row.zone != "merge":
            continue
        row.zone = "band"
        row.reason = f"{row.reason}:rail_reopen:{action.limb}"
        demoted.append(row)
    if demoted:
        store.upsert_pairs(demoted)
        _recluster(store, facts, settings, working,
                   {i for row in demoted for i in (row.lo, row.hi)},
                   Limits(), result, operator_mnl)
    counters["owed"] = len(actions)
    return counters


def _parse_epoch(value: str | None) -> float | None:
    if not value:
        return None
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
