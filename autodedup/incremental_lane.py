"""`--mode incremental`: the real-time shadow pass, and the Postgres store behind it.

Dark by default at two levels. The workflow exits before python runs unless the repository
variable `AUTODEDUP_REALTIME_ENABLED` is `true`; and this mode refuses to write unless the
`autodedup.settings` row `realtime_enabled` is true as well, so an operator can stop the lane
without touching the workflow file. Shadow mode's own switch (`autodedup_write_enabled`,
E39/D4) is untouched and nothing here reaches `public.listings` or a `property_id`.

The pass itself is `incremental.run_pass`; this module is the three adapters it needs — the
store, the read-only fact source, and the watermark — plus the lease that keeps two runs out
of each other's way (lease-row CAS, never `pg_advisory_lock`: a session lock strands over the
transaction pooler).

**Spend is structurally zero.** D19 gives no LLM judge merge authority on the band, so this
lane calls no provider; `spent_usd` is reported as a measured 0, not as a forecast.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from autodedup.dataset import Image, Listing, Location
from autodedup.export import DEFAULT_CLIP_MODEL, encode_clip
from autodedup.features import FEATURE_VERSION
from autodedup.harness import load_model, load_settings
from autodedup.hazard_context import ContextStamp, address_block_key, category_group
from autodedup.incremental import (
    GENERATION,
    Calibration,
    CellRow,
    GuardRow,
    Limits,
    PairRow,
    SET_CAP,
    key_token,
    run_pass,
)
from autodedup.incremental_sql import (
    RT_CALIBRATION_READ_SQL,
    RT_CELL_READ_SQL,
    RT_CELL_UPSERT_SQL,
    RT_CLUSTER_DROP_SQL,
    RT_CLUSTER_MEMBERS_DROP_SQL,
    RT_CLUSTERS_TOUCHING_SQL,
    RT_CURSOR_READ_SQL,
    RT_CURSOR_WRITE_SQL,
    RT_FACTS_SQL,
    RT_FLIPPED_LISTINGS_SQL,
    RT_FP_UPSERT_SQL,
    RT_GUARDS_SQL,
    RT_IMAGE_CLIP_SQL,
    RT_IMAGE_TAGS_SQL,
    RT_IMAGES_SQL,
    RT_KEY_DELETE_SQL,
    RT_KEY_INSERT_SQL,
    RT_KEYS_OF_SQL,
    RT_KNOWN_SQL,
    RT_LEASE_RELEASE_SQL,
    RT_LEASE_TAKE_SQL,
    RT_LOOKUP_SQL,
    RT_MERGE_NEIGHBOURS_SQL,
    RT_MUST_NOT_LINK_SQL,
    RT_NEW_LISTINGS_SQL,
    RT_CHANGED_LISTINGS_SQL,
    RT_PAIR_DELETE_SQL,
    RT_PAIR_UPSERT_SQL,
    RT_PAIRS_TOUCHING_SQL,
    RT_PAIRS_WITHIN_SQL,
    RT_STORE_PRESENT_SQL,
)
from autodedup.score_lane import CLUSTER_INSERT_SQL, CLUSTER_MEMBER_INSERT_SQL

LANE_NAME: str = "autodedup_realtime"
LEASE_TTL_S: int = 900
CURSOR_NEW: str = "rt_new"
CURSOR_CHANGED: str = "rt_changed"
CURSOR_FLIPPED: str = "rt_flipped"
ENV_FLAG: str = "AUTODEDUP_REALTIME_ENABLED"


def _rows(conn: Any, sql: str, params: Mapping[str, Any] | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, dict(params or {}))
        return list(cur.fetchall())


def _exec(conn: Any, sql: str, params: Mapping[str, Any] | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, dict(params or {}))


class SqlStore:
    """`incremental.Store` over Postgres. Every write is inside schema `autodedup` (D4)."""

    def __init__(self, conn: Any, generation: str = GENERATION) -> None:
        self.conn = conn
        self.generation = generation

    # ------------------------------------------------------------------ postings
    def lookup(self, probe: str, token: str) -> list[int]:
        return [int(row[0]) for row in _rows(self.conn, RT_LOOKUP_SQL, {
            "generation": self.generation, "probe": probe, "key_token": token})]

    def put_listing(self, listing_id: int, guard: GuardRow, digest: str,
                    keys: Sequence[tuple[str, str]]) -> None:
        _exec(self.conn, RT_KEY_DELETE_SQL,
              {"generation": self.generation, "listing_id": listing_id})
        for probe, token in keys:
            _exec(self.conn, RT_KEY_INSERT_SQL, {
                "generation": self.generation, "probe": probe, "key_token": token,
                "listing_id": listing_id})
        self._fp_cache.pop(listing_id, None)
        self._fp_cache[listing_id] = (guard, digest)

    def drop_listing(self, listing_id: int) -> None:
        _exec(self.conn, RT_KEY_DELETE_SQL,
              {"generation": self.generation, "listing_id": listing_id})

    def keys_of(self, listing_id: int) -> list[tuple[str, str]]:
        return [(str(row[0]), str(row[1])) for row in _rows(
            self.conn, RT_KEYS_OF_SQL,
            {"generation": self.generation, "listing_id": listing_id})]

    _fp_cache: dict[int, tuple[GuardRow, str]] = {}

    def guards(self, ids: Iterable[int]) -> dict[int, GuardRow]:
        wanted = [int(i) for i in ids]
        if not wanted:
            return {}
        out: dict[int, GuardRow] = {}
        for row in _rows(self.conn, RT_GUARDS_SQL, {"ids": wanted}):
            out[int(row[0])] = GuardRow(
                int(row[0]), row[1], row[2],
                float(row[3]) if row[3] is not None else None, row[4],
                int(row[5]) if row[5] is not None else None,
            )
        return out

    def digests(self, ids: Iterable[int]) -> dict[int, str]:
        wanted = [int(i) for i in ids]
        if not wanted:
            return {}
        return {int(row[0]): str(row[6]) for row in
                _rows(self.conn, RT_GUARDS_SQL, {"ids": wanted}) if row[6]}

    def known_listings(self) -> set[int]:
        return {int(row[0]) for row in
                _rows(self.conn, RT_KNOWN_SQL, {"generation": self.generation})}

    def write_fp(self, params: Mapping[str, Any]) -> None:
        _exec(self.conn, RT_FP_UPSERT_SQL, params)

    # ------------------------------------------------------------------ pair grain
    def _pair_row(self, row: Sequence[Any]) -> PairRow:
        evidence = row[10] if isinstance(row[10], dict) else json.loads(row[10] or "{}")
        context = row[11] if isinstance(row[11], dict) else json.loads(row[11] or "{}")
        zone = str(row[6])
        veto = row[8]
        return PairRow(
            lo=int(row[0]), hi=int(row[1]), probes=list(row[2] or ()),
            from_lo=bool(row[3]), from_hi=bool(row[4]), score=float(row[5] or 0.0),
            # A guard veto is stored as `reject` (the table's CHECK) and read back as the zone
            # the engine decided, so a round-trip through the store cannot move a decision.
            zone="veto" if veto else zone,
            families=[], certificate=None, veto=veto, reason=str(row[7] or ""),
            evidence={str(k): str(v) for k, v in evidence.items()},
            context=context, fp_lo="", fp_hi="",
        )

    def pairs_touching(self, ids: Iterable[int]) -> dict[tuple[int, int], PairRow]:
        wanted = [int(i) for i in ids]
        if not wanted:
            return {}
        rows = _rows(self.conn, RT_PAIRS_TOUCHING_SQL,
                     {"generation": self.generation, "ids": wanted})
        out: dict[tuple[int, int], PairRow] = {}
        for row in rows:
            pair = self._pair_row(row)
            features = row[12] if len(row) > 12 else None
            if isinstance(features, dict):
                pair.fp_lo = str(features.get("_fp_lo") or "")
                pair.fp_hi = str(features.get("_fp_hi") or "")
            out[(pair.lo, pair.hi)] = pair
        return out

    def pairs_within(self, members: Iterable[int]) -> list[PairRow]:
        wanted = [int(i) for i in members]
        if not wanted:
            return []
        return [self._pair_row(row) for row in _rows(
            self.conn, RT_PAIRS_WITHIN_SQL,
            {"generation": self.generation, "ids": wanted})]

    def merge_neighbours(self, ids: Iterable[int]) -> dict[int, set[int]]:
        wanted = [int(i) for i in ids]
        if not wanted:
            return {}
        out: dict[int, set[int]] = {i: set() for i in wanted}
        for row in _rows(self.conn, RT_MERGE_NEIGHBOURS_SQL,
                         {"generation": self.generation, "ids": wanted}):
            out.setdefault(int(row[0]), set()).add(int(row[1]))
        return out

    def upsert_pairs(self, rows: Sequence[PairRow]) -> None:
        for row in rows:
            _exec(self.conn, RT_PAIR_UPSERT_SQL, {
                "generation": self.generation,
                "listing_lo": row.lo, "listing_hi": row.hi, "probes": list(row.probes),
                "from_lo": row.from_lo, "from_hi": row.from_hi,
                "families": len(row.families),
                "features": json.dumps({"_fp_lo": row.fp_lo, "_fp_hi": row.fp_hi}),
                "score": float(row.score),
                # The table's CHECK knows three zones; a veto is a reject that names its rule.
                "zone": "reject" if row.zone == "veto" else row.zone,
                "decision": row.reason, "guard_veto": row.veto,
                "evidence": json.dumps(row.evidence, ensure_ascii=False),
                "context": json.dumps(row.context),
                "calibration_digest": row.evidence.get("_calibration"),
                "feature_version": FEATURE_VERSION,
                "model_version": row.evidence.get("_model"),
            })

    def delete_pairs(self, keys: Sequence[tuple[int, int]]) -> None:
        for lo, hi in keys:
            _exec(self.conn, RT_PAIR_DELETE_SQL,
                  {"generation": self.generation, "listing_lo": lo, "listing_hi": hi})

    # ------------------------------------------------------------------ cluster grain
    def clusters_touching(self, members: Iterable[int]) -> dict[int, list[int]]:
        wanted = [int(i) for i in members]
        if not wanted:
            return {}
        out: dict[int, list[int]] = {}
        for row in _rows(self.conn, RT_CLUSTERS_TOUCHING_SQL,
                         {"generation": self.generation, "ids": wanted}):
            out.setdefault(int(row[0]), []).append(int(row[1]))
        return out

    def write_clusters(self, drop_keys: Sequence[int], rows: Sequence[Mapping[str, Any]],
                       conflicts: Sequence[Mapping[str, Any]]) -> None:
        keys = [int(key) for key in drop_keys] + [int(row["cluster_key"]) for row in rows]
        if keys:
            _exec(self.conn, RT_CLUSTER_MEMBERS_DROP_SQL,
                  {"generation": self.generation, "keys": keys})
            _exec(self.conn, RT_CLUSTER_DROP_SQL,
                  {"generation": self.generation, "keys": keys})
        for row in rows:
            _exec(self.conn, CLUSTER_INSERT_SQL, _cluster_params(row, self.generation))
            for listing_id in row["members"]:
                _exec(self.conn, CLUSTER_MEMBER_INSERT_SQL, {
                    "generation": self.generation, "cluster_key": int(row["cluster_key"]),
                    "listing_id": int(listing_id), "joined_via_lo": None,
                    "joined_via_hi": None})

    def must_not_link(self) -> set[tuple[int, int]]:
        return {(int(row[0]), int(row[1]))
                for row in _rows(self.conn, RT_MUST_NOT_LINK_SQL)}

    # ------------------------------------------------------------------ live census
    def cells(self, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], CellRow]:
        wanted = sorted({key for key, _group in keys})
        if not wanted:
            return {}
        out: dict[tuple[str, str], CellRow] = {}
        for row in _rows(self.conn, RT_CELL_READ_SQL,
                         {"generation": self.generation, "keys": wanted}):
            out[(str(row[0]), str(row[1]))] = CellRow(
                str(row[0]), str(row[1]), int(row[2]), list(row[3] or ()),
                list(row[4] or ()), list(row[5] or ()), bool(row[6]))
        return out

    def bump_cell(self, listing: Listing) -> None:
        self._cell_delta(listing, +1)

    def unbump_cell(self, listing: Listing) -> None:
        self._cell_delta(listing, -1)

    def _cell_delta(self, listing: Listing, delta: int) -> None:
        key = (address_block_key(listing), category_group(listing))
        current = self.cells([key]).get(key) or CellRow(key[0], key[1])
        current.n_listings = max(0, current.n_listings + delta)
        if delta > 0:
            for bucket, value in (
                (current.shapes, key_token((listing.disposition,
                                            None if not listing.area_m2 else
                                            round(float(listing.area_m2), 1)))),
                (current.brokers, listing.broker_key),
                (current.source_ids, listing.source_id_native),
            ):
                if not value or value in bucket:
                    continue
                if len(bucket) >= SET_CAP:
                    current.capped = True
                    continue
                bucket.append(value)
        _exec(self.conn, RT_CELL_UPSERT_SQL, {
            "generation": self.generation, "cell_key": current.key,
            "category_group": current.category_group, "n_listings": current.n_listings,
            "shapes": json.dumps(current.shapes), "brokers": json.dumps(current.brokers),
            "source_ids": json.dumps(current.source_ids), "capped": current.capped})

    def stamped_merges(self) -> list[tuple[int, int, ContextStamp, str, bool]]:
        out: list[tuple[int, int, ContextStamp, str, bool]] = []
        for row in _rows(self.conn, RT_PAIRS_WITHIN_SQL,
                         {"generation": self.generation, "ids": []}):
            pair = self._pair_row(row)
            stamp = ContextStamp.from_evidence(pair.evidence)
            if stamp is None or pair.zone != "merge":
                continue
            out.append((pair.lo, pair.hi, stamp, str(pair.context.get("block") or ""),
                        bool(pair.certificate)))
        return out


def _cluster_params(row: Mapping[str, Any], generation: str) -> dict[str, Any]:
    block = (row.get("block_key") or [None])[0] if isinstance(row.get("block_key"), list) \
        else row.get("block_key")
    grain, code = (None, None)
    if isinstance(block, str) and len(block) > 1 and block[0] in "co":
        grain, code = ("cast_obce" if block[0] == "c" else "obec"), int(block[1:])
    return {
        "cluster_key": int(row["cluster_key"]), "generation": generation,
        "size": int(row["size"]), "block_key": code, "block_grain": grain,
        "cat_group": (row.get("cat_group") or [None])[0],
        "category_main": (row.get("category_main") or [None])[0],
        "category_type": (row.get("category_type") or [None])[0],
        "area_min": row.get("area_min"), "area_max": row.get("area_max"),
        "sources": list(row.get("sources") or ()),
        "medoid_listing_id": None, "min_edge_score": row.get("min_edge_score"),
        "mean_edge_score": row.get("mean_edge_score"), "n_judged_edges": 0,
        "n_certificate_edges": int(row.get("n_certificate_edges") or 0),
        "evidence_families": len(row.get("evidence_families") or ()),
        "max_gap_days": None,
        "shared_photo_warning": bool(row.get("shared_photo_warning")),
        "status": "proposed", "model_version": None, "feature_version": FEATURE_VERSION,
    }


class SqlFacts:
    """The read-only half. `public` is READ here and nowhere written (D4).

    The gallery is assembled exactly as the export lane assembles it — pHash, the corpus-wide
    population, the CLIP tag pairs (logical first, then the fine anchor when it differs) and
    the float16 vector — because a feature that reads a thinner image than the cohort pass did
    is a decision the replay proof does not cover. `broker_key` is the one field a listing row
    cannot serve: it is the export's SALTED key (E28), so the lane reads it from the
    fingerprint mirror it wrote itself rather than re-deriving an identity here."""

    def __init__(self, conn: Any, clip_model: str = DEFAULT_CLIP_MODEL) -> None:
        self.conn = conn
        self.clip_model = clip_model
        self.reads = 0

    def facts(self, ids: Iterable[int]) -> dict[int, tuple[Listing, list[Image]]]:
        wanted = [int(i) for i in ids]
        if not wanted:
            return {}
        listings: dict[int, Listing] = {}
        for row in _rows(self.conn, RT_FACTS_SQL, {"ids": wanted}):
            listings[int(row[0])] = _listing_of(row)
        galleries: dict[int, list[Image]] = {}
        image_ids: list[int] = []
        for row in _rows(self.conn, RT_IMAGES_SQL, {"ids": wanted}):
            image_id = int(row[1])
            image_ids.append(image_id)
            galleries.setdefault(int(row[0]), []).append(
                Image(image_id=image_id, listing_id=int(row[0]),
                      seq=int(row[2]) if row[2] is not None else None,
                      phash=int(row[3]) if row[3] is not None else None,
                      pop=int(row[4]) if row[4] is not None else None))
        tags: dict[int, list[tuple[str, float | None]]] = {}
        clips: dict[int, str] = {}
        if image_ids:
            for row in _rows(self.conn, RT_IMAGE_TAGS_SQL,
                             {"ids": image_ids, "model": self.clip_model}):
                bucket = tags.setdefault(int(row[0]), [])
                score = float(row[3]) if row[3] is not None else None
                logical, fine = row[2], row[1]
                if logical:
                    bucket.append((str(logical), score))
                if fine and fine != logical:
                    bucket.append((str(fine), score))
            for row in _rows(self.conn, RT_IMAGE_CLIP_SQL,
                             {"ids": image_ids, "model": self.clip_model}):
                clips[int(row[0])] = encode_clip(row[1])
        for bucket in galleries.values():
            for image in bucket:
                image.tags = tags.get(image.image_id, [])
                image.clip = clips.get(image.image_id)
        self.reads += len(listings)
        return {i: (listing, galleries.get(i, [])) for i, listing in listings.items()}


def _listing_of(row: Sequence[Any]) -> Listing:
    return Listing(
        id=int(row[0]), block="", source=row[1], source_id_native=row[2], source_url=row[3],
        category_main=row[4], category_type=row[5], subtype=row[6], disposition=row[7],
        area_m2=float(row[8]) if row[8] is not None else None,
        floor=int(row[9]) if row[9] is not None else None,
        total_floors=int(row[10]) if row[10] is not None else None,
        price=float(row[11]) if row[11] is not None else None,
        description=row[12],
        first_seen_at=row[13].isoformat() if row[13] is not None else None,
        last_seen_at=row[14].isoformat() if row[14] is not None else None,
        inactive_at=row[15].isoformat() if row[15] is not None else None,
        is_active=bool(row[16]),
        broker_key=None,
        broker_identity_id=int(row[17]) if row[17] is not None else None,
        broker_firm_id=int(row[18]) if row[18] is not None else None,
        location=Location(
            obec_kod=int(row[19]) if row[19] is not None else None,
            cast_obce_kod=int(row[20]) if row[20] is not None else None,
            granularity=row[21],
            lat=float(row[22]) if row[22] is not None else None,
            lon=float(row[23]) if row[23] is not None else None,
            street_key=row[24], house_number=row[25], house_number_cp=row[26],
            house_number_co=row[27], psc=row[28],
            ruian_adm_kod=int(row[29]) if row[29] is not None else None,
            country_code=row[30], country_status=row[31],
        ),
    )


class SqlWork:
    """The three index-served watermark cursors, bounded and committed together."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.pending: dict[str, Any] = {}
        self._claimed: dict[str, Any] = {}

    def _cursors(self) -> dict[str, tuple[int, int, Any]]:
        out = {name: (0, 0, "epoch") for name in (CURSOR_NEW, CURSOR_CHANGED, CURSOR_FLIPPED)}
        for row in _rows(self.conn, RT_CURSOR_READ_SQL, {
                "names": [CURSOR_NEW, CURSOR_CHANGED, CURSOR_FLIPPED]}):
            out[str(row[0])] = (int(row[1] or 0), int(row[2] or 0), row[3] or "epoch")
        return out

    def claim(self, limit: int) -> list[int]:
        cursors = self._cursors()
        share = max(1, limit // 3)
        ids: list[int] = []
        new_rows = _rows(self.conn, RT_NEW_LISTINGS_SQL,
                         {"after_id": cursors[CURSOR_NEW][0], "limit": share})
        ids.extend(int(row[0]) for row in new_rows)
        changed = _rows(self.conn, RT_CHANGED_LISTINGS_SQL,
                        {"after_id": cursors[CURSOR_CHANGED][1], "limit": share})
        ids.extend(int(row[1]) for row in changed)
        flipped = _rows(self.conn, RT_FLIPPED_LISTINGS_SQL,
                        {"after": cursors[CURSOR_FLIPPED][2], "limit": share})
        ids.extend(int(row[0]) for row in flipped)
        self._claimed = {
            CURSOR_NEW: max([int(row[0]) for row in new_rows],
                            default=cursors[CURSOR_NEW][0]),
            CURSOR_CHANGED: max([int(row[0]) for row in changed],
                                default=cursors[CURSOR_CHANGED][1]),
            CURSOR_FLIPPED: max([row[1] for row in flipped],
                                default=cursors[CURSOR_FLIPPED][2]),
        }
        seen: set[int] = set()
        ordered = [i for i in ids if not (i in seen or seen.add(i))]
        return ordered

    def commit(self) -> dict[str, Any]:
        _exec(self.conn, RT_CURSOR_WRITE_SQL, {
            "name": CURSOR_NEW, "last_listing_id": self._claimed.get(CURSOR_NEW),
            "last_snapshot_id": None, "watermark": None})
        _exec(self.conn, RT_CURSOR_WRITE_SQL, {
            "name": CURSOR_CHANGED, "last_listing_id": None,
            "last_snapshot_id": self._claimed.get(CURSOR_CHANGED), "watermark": None})
        _exec(self.conn, RT_CURSOR_WRITE_SQL, {
            "name": CURSOR_FLIPPED, "last_listing_id": None, "last_snapshot_id": None,
            "watermark": self._claimed.get(CURSOR_FLIPPED)})
        return {key: str(value) for key, value in self._claimed.items()}


def env_enabled(env: Mapping[str, str] | None = None) -> bool:
    """The workflow's own gate, re-read here so a dispatched run cannot bypass the variable."""
    source = os.environ if env is None else env
    return str(source.get(ENV_FLAG, "")).strip().lower() == "true"


def take_lease(conn: Any, holder: str, ttl: int = LEASE_TTL_S) -> bool:
    rows = _rows(conn, RT_LEASE_TAKE_SQL,
                 {"name": LANE_NAME, "holder": holder, "ttl": ttl})
    return bool(rows) and str(rows[0][0]) == holder


def release_lease(conn: Any, holder: str) -> None:
    _exec(conn, RT_LEASE_RELEASE_SQL, {"name": LANE_NAME, "holder": holder})


def run_incremental(
    conn_factory: Callable[[], Any], args: Mapping[str, str], out_dir: Path
) -> dict[str, Any]:
    """One bounded real-time pass. Dark unless BOTH switches are on; writes nothing else."""
    generation = (args.get("generation") or "").strip() or GENERATION
    limits = Limits(
        max_listings=int(args.get("max_listings") or 500),
        max_pairs=int(args.get("max_pairs") or 20000),
        max_component=int(args.get("max_component") or 400),
    )
    if not env_enabled():
        return {"skipped": "dark", "reason": f"{ENV_FLAG} is not true", "spent_usd": 0.0}

    settings = load_settings(args.get("settings") or None)
    model = load_model(args.get("model") or None)
    holder = f"{socket.gethostname()}:{os.getpid()}:{int(time.time())}"
    conn = conn_factory()
    try:
        present = _rows(conn, RT_STORE_PRESENT_SQL)
        if not present or not present[0][0]:
            raise SystemExit("autodedup realtime store absent — migration 539 not applied")
        if not take_lease(conn, holder):
            return {"skipped": "leased", "reason": "another pass holds the lease",
                    "spent_usd": 0.0}
        rows = _rows(conn, RT_CALIBRATION_READ_SQL, {"generation": generation})
        if not rows:
            raise SystemExit(
                f"no frozen calibration for generation {generation!r} (E65) — seed it from a "
                "cohort pass before the lane runs")
        payload = rows[0][3]
        calibration = Calibration.from_json(
            payload if isinstance(payload, dict) else json.loads(payload or "{}"))
        store = SqlStore(conn, generation)
        facts = SqlFacts(conn)
        work = SqlWork(conn)
        result = run_pass(store, facts, work, settings, model, calibration,
                          limits=limits, generation=generation, now=time.time())
        summary = result.to_json()
        summary["fact_reads"] = facts.reads
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "incremental.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        return summary
    finally:
        try:
            release_lease(conn, holder)
        finally:
            close = getattr(conn, "close", None)
            if callable(close):
                close()
