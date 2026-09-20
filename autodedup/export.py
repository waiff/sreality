"""`mode=export` — the whole trial cohort as one gzipped JSONL artifact (W1).

The point of this file is that every later wave runs **locally at $0**: blocking, features,
model fitting, calibration and clustering never touch the database again, because everything
they need is in `out/cohort.jsonl.gz`. One dispatch, one artifact, then offline iteration.

The artifact is a stream of JSON objects discriminated by `t`, in a fixed order — `meta`,
then every `listing`, then every `image` — so a reader can size itself from the first line
and never needs to hold the file in memory. Its shape is a contract shared with the dataset
builder; `tests/autodedup/test_export.py` pins it.

Nothing here writes to any `public.*` table (ruling D4) and nothing reads a legacy dedup
relation (ruling D7). The only write this lane makes anywhere is its own
`autodedup.iterations` row, and that happens in `autodedup.lane`, not here.

PII (E28). Broker identity leaves as a salted sha256 `broker_key` plus the two numeric ids;
names, phones and e-mails never reach the file, and descriptions are scrubbed of Czech
contact shapes before they are written. The scrub is deliberately conservative about digits:
a nine-digit run followed by a currency marker is a price, not a phone.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
import os
import re
import struct
import time
import unicodedata
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from autodedup import cohort
from autodedup.cohort import Block
from autodedup.export_sql import (
    ATTR_COLUMNS,
    COHORT_CLIP_COUNT_SQL,
    COHORT_CLIP_SQL,
    COHORT_CLIP_TAGS_SQL,
    COHORT_IMAGES_SQL,
    COHORT_LISTINGS_SQL,
    COHORT_LOCATION_SQL,
    COHORT_NEGCTL_GROUPS_SQL,
    COHORT_PHASH_POP_SQL,
    COHORT_PRICE_HISTORY_SQL,
    COHORT_QUARTER_IDS_SQL,
    COHORT_TOWN_IDS_SQL,
)

GENERATOR_VERSION = "w1"
ARTIFACT_NAME = "cohort.jsonl.gz"

# The CLIP model whose 512-d vectors the corpus actually carries (migration 226;
# scripts/tagging_bakeoff_arms.STORED_CLIP_MODEL). A `/` is outside the workflow input's
# charset, so an override is a local-run affordance, not a dispatch one.
DEFAULT_CLIP_MODEL = "openai/clip-vit-base-patch32"

# Pseudonymisation salt (E28). A constant, not a secret: it must be STABLE across exports or
# the same broker would get a different key in every artifact and `same_broker` would stop
# being a feature. It is not a defence against a determined re-identification attempt — the
# defence is that no name, phone or e-mail is ever written.
AUTODEDUP_BROKER_SALT = "autodedup-v1"

MAX_PRICE_EVENTS = 12
CLIP_DIMS = 512
_FLOAT16_MAX = 65504.0

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Czech phone: an optional country code in any of its three spellings (+420, 00420, bare
# 420), then 9 digits in 3-3-3 with the usual separators. The leading digit may not be 0/1
# (no Czech subscriber number starts there) and a run followed by a currency marker is a
# price — both guards keep "4 500 000 Kč" out of the phone class.
_PHONE_RE = re.compile(
    r"(?<![\d])(?:(?:\+|00)?\s?420[\s./-]?)?[2-9]\d{2}[\s./-]?\d{3}[\s./-]?\d{3}"
    r"(?![\d])(?!\s*(?:Kč|kč|KČ|CZK|czk|,-))"
)
_TITLE_NAME_RE = re.compile(
    r"\b(?:Ing|Bc|BcA|Mgr|MgA|MUDr|MVDr|JUDr|PhDr|RNDr|PaedDr|Dr)\.\s*"
    r"[A-ZÁ-Ž][a-zá-ž]+(?:\s+[A-ZÁ-Ž][a-zá-ž]+)?"
)
_MAKLER_NAME_RE = re.compile(
    r"(?:makléř(?:ka)?|maklér(?:ka)?|realitní\s+makléř(?:ka)?|realitní\s+specialist(?:a|ka)?)"
    r"\s*[:\-–]?\s*[A-ZÁ-Ž][a-zá-ž]+\s+[A-ZÁ-Ž][a-zá-ž]+",
    re.IGNORECASE,
)

EMAIL_TOKEN = "[email]"
PHONE_TOKEN = "[telefon]"
NAME_TOKEN = "[jmeno]"


# --- value coercion ---------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """JSON numbers for Decimal, ISO-8601 for timestamps, everything else untouched."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, memoryview):
        return value.tobytes().hex()
    return value


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- PII (E28) --------------------------------------------------------------------------


_CZ_TRUNK_PREFIXES: tuple[str, ...] = ("00420", "420")


def normalize_phone(raw: str | None) -> str | None:
    """Digits only, a CZECH country code dropped, exactly 9 digits kept — so `+420 777 123
    456`, `00420777123456` and `777123456` hash to one broker.

    Only `420`/`00420` is stripped: a blind "keep the last nine" would fold every foreign
    number onto a Czech-shaped key, and `broker_key` is the identity behind the BRK evidence
    family (E11), so a collision there manufactures a `same_broker` that never existed."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    for prefix in _CZ_TRUNK_PREFIXES:
        if len(digits) > 9 and digits.startswith(prefix):
            digits = digits[len(prefix) :]
            break
    return digits if len(digits) == 9 else None


def broker_key(
    *,
    broker_identity_id: Any = None,
    phone: str | None = None,
    email: str | None = None,
    salt: str = AUTODEDUP_BROKER_SALT,
) -> str | None:
    """A salted sha256 over the first identity that exists: the resolved broker id, else the
    normalized phone, else the e-mail DOMAIN (never the local part — that is a person)."""
    identity = _int_or_none(broker_identity_id)
    if identity is not None:
        material = f"id:{identity}"
    elif (digits := normalize_phone(phone)) is not None:
        material = f"tel:{digits}"
    elif email and "@" in email:
        domain = email.rsplit("@", 1)[1].strip().lower()
        material = f"dom:{domain}" if domain else ""
    else:
        material = ""
    if not material:
        return None
    return hashlib.sha256(f"{salt}:{material}".encode("utf-8")).hexdigest()


def scrub_description(text: str | None) -> str | None:
    """Replace Czech contact patterns with tokens, keeping the description's full length
    otherwise — the text features (E20) need the whole body, only not the contacts."""
    if not text:
        return text
    out = _EMAIL_RE.sub(EMAIL_TOKEN, text)
    out = _PHONE_RE.sub(PHONE_TOKEN, out)
    out = _TITLE_NAME_RE.sub(NAME_TOKEN, out)
    out = _MAKLER_NAME_RE.sub(NAME_TOKEN, out)
    return out


# --- derived fields ---------------------------------------------------------------------


def street_key(name: str | None) -> str | None:
    """A comparable key off `listing_location.street_name` — deaccented, lowercased,
    punctuation collapsed. The store carries no `street_key` column of its own (the
    listing-level one went with migration 508), so the artifact derives it once here rather
    than letting every consumer invent its own spelling."""
    if not name:
        return None
    folded = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in folded if not unicodedata.combining(ch))
    key = re.sub(r"[^0-9a-z]+", " ", ascii_only.lower()).strip()
    return key or None


def house_number(cp: str | None, co: str | None) -> str | None:
    """`čp/čo` as one string, the Czech way; either half alone when the other is missing."""
    cp = (cp or "").strip() or None
    co = (co or "").strip() or None
    if cp and co:
        return f"{cp}/{co}"
    return cp or co


def price_events(rows: Sequence[dict[str, Any]], *, limit: int = MAX_PRICE_EVENTS) -> list[list[Any]]:
    """Distinct consecutive price CHANGES, oldest first, at most `limit`.

    The first snapshot counts as an event (it is where the path starts); a repeated price is
    not an event. When a path is longer than the cap the OLDEST events are dropped, because
    E19 matches change events between two adverts that overlap in time — the recent end is
    the end that can overlap."""
    events: list[list[Any]] = []
    last: int | None = None
    for row in rows:
        price = _int_or_none(row.get("price_czk"))
        if price is None or price == last:
            continue
        stamp = _jsonable(row.get("scraped_at"))
        events.append([stamp, price])
        last = price
    return events[-limit:]


def encode_clip(vector: Any) -> str | None:
    """512 float16s, little-endian, base64 — 1,024 bytes on the wire per image.

    psycopg hands a pgvector column back as the literal text `[0.1,0.2,…]` unless a vector
    adapter is registered in the process, in which case it is already a sequence; both are
    accepted, and anything that is not exactly 512 finite numbers is dropped rather than
    written short."""
    values: Sequence[Any]
    if vector is None:
        return None
    if isinstance(vector, str):
        text = vector.strip().strip("[]")
        if not text:
            return None
        try:
            values = [float(part) for part in text.split(",")]
        except ValueError:
            return None
    elif isinstance(vector, (list, tuple)):
        try:
            values = [float(part) for part in vector]
        except (TypeError, ValueError):
            return None
    elif hasattr(vector, "tolist"):
        try:
            values = [float(part) for part in vector.tolist()]
        except (TypeError, ValueError):
            return None
    else:
        return None
    if len(values) != CLIP_DIMS:
        return None
    # float16 tops out at 65504 and NaN would poison every cosine downstream, so a vector
    # that cannot be written exactly is dropped rather than packed (or raised on).
    if not all(math.isfinite(v) and abs(v) <= _FLOAT16_MAX for v in values):
        return None
    return base64.b64encode(struct.pack(f"<{CLIP_DIMS}e", *values)).decode("ascii")


def decode_clip(encoded: str | None) -> list[float] | None:
    """The reader half of `encode_clip`, so the round trip is testable in one place."""
    if not encoded:
        return None
    raw = base64.b64decode(encoded)
    return list(struct.unpack(f"<{CLIP_DIMS}e", raw))


# --- records ----------------------------------------------------------------------------


def build_location(row: dict[str, Any] | None) -> dict[str, Any]:
    """The location block of a listing record. Every key is always present (null when the
    store does not carry it) so a consumer never has to test for absence."""
    row = row or {}
    return {
        "obec_kod": _int_or_none(row.get("obec_kod")),
        "obec_name": row.get("obec_name"),
        "cast_obce_kod": _int_or_none(row.get("cast_obce_kod")),
        "cast_obce_name": row.get("cast_obce_name"),
        "granularity": row.get("granularity"),
        "granularity_rank": _int_or_none(row.get("granularity_rank")),
        "is_address_grain": row.get("is_address_grain"),
        "lat": _jsonable(row.get("lat")),
        "lon": _jsonable(row.get("lon")),
        "uncertainty_radius_m": _jsonable(row.get("uncertainty_radius_m")),
        "street_key": street_key(row.get("street_name")),
        "house_number": house_number(row.get("house_number_cp"), row.get("house_number_co")),
        "psc": row.get("psc"),
        "ruian_adm_kod": _int_or_none(row.get("ruian_adm_kod")),
        "country_status": row.get("country_status"),
    }


def build_listing_record(
    row: dict[str, Any],
    *,
    block: str,
    location: dict[str, Any] | None,
    history: Sequence[dict[str, Any]] = (),
    salt: str = AUTODEDUP_BROKER_SALT,
) -> dict[str, Any]:
    attrs = {
        name: _jsonable(row[name])
        for name in ATTR_COLUMNS
        if row.get(name) is not None
    }
    return {
        "t": "listing",
        "id": int(row["id"]),
        "block": block,
        "source": row.get("source"),
        "source_id_native": row.get("source_id_native"),
        "source_url": row.get("source_url"),
        "category_main": row.get("category_main"),
        "category_type": row.get("category_type"),
        "subtype": row.get("subtype"),
        "disposition": row.get("disposition"),
        "area_m2": _jsonable(row.get("area_m2")),
        "floor": _int_or_none(row.get("floor")),
        "total_floors": _int_or_none(row.get("total_floors")),
        "price": _jsonable(row.get("price_czk")),
        "attrs": attrs,
        "description": scrub_description(row.get("description")),
        "first_seen_at": _jsonable(row.get("first_seen_at")),
        "last_seen_at": _jsonable(row.get("last_seen_at")),
        "inactive_at": _jsonable(row.get("inactive_at")),
        "is_active": bool(row.get("is_active")),
        "broker_key": broker_key(
            broker_identity_id=row.get("broker_identity_id"),
            phone=row.get("broker_phone"),
            email=row.get("broker_email"),
            salt=salt,
        ),
        "broker_identity_id": _int_or_none(row.get("broker_identity_id")),
        "broker_firm_id": _int_or_none(row.get("broker_firm_id")),
        "location": build_location(location),
        "price_history": price_events(history),
    }


def build_image_record(
    row: dict[str, Any],
    *,
    clip: str | None,
    tags: Sequence[Sequence[Any]] = (),
    pop: dict[int, int] | None = None,
) -> dict[str, Any]:
    """`pop=None` means the corpus-wide population is UNKNOWN (the probe did not run or
    timed out), and the record says so with a null — never a 0, which a consumer would read
    as "this photo is unique" and E9's catalog subtraction would silently become a no-op.

    A hash MISSING from a `pop` that did run is the same unknown, not a zero. The export's own
    probe counts `count(DISTINCT listing_id)` over `public.images` and so can never answer less
    than 1 for a hash it was handed, which is why this never fires here; the real-time lane
    joins the same builder against the FROZEN population instead, where a hash the calibration
    never saw is exactly an unknown — and reading it as 0 was W9f's missing K-C certificates
    (E91)."""
    phash = _int_or_none(row.get("phash"))
    return {
        "t": "image",
        "listing_id": int(row["listing_id"]),
        "image_id": int(row["image_id"]),
        "seq": _int_or_none(row.get("sequence")),
        "storage_path": row.get("storage_path"),
        "phash": phash,
        "pop": None if (phash is None or pop is None or phash not in pop)
               else int(pop[phash]),
        "clip": clip,
        "tags": [list(tag) for tag in tags],
    }


def tag_pairs(rows: Sequence[dict[str, Any]]) -> list[list[Any]]:
    """`image_clip_tags` rows -> `[[tag, score], …]`: the LOGICAL tag (the room type, e.g.
    `site_plan`) first, then the fine CLIP anchor when the two differ.

    Neither is a family. E10's families come from `toolkit.room_taxonomy.family_of()` over
    the logical tag — `ROOM_FAMILIES` maps room type -> family — so a consumer that wants
    interior/exterior/plan resolves it there rather than reading the first pair as one."""
    out: list[list[Any]] = []
    for row in rows:
        score = _jsonable(row.get("confidence"))
        logical = row.get("logical_tag")
        fine = row.get("fine_tag")
        if logical:
            out.append([logical, score])
        if fine and fine != logical:
            out.append([fine, score])
    return out


# --- DB layer ---------------------------------------------------------------------------


def _rows_as_dicts(cur: Any) -> list[dict[str, Any]]:
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _run(conn: Any, sql: str, params: dict[str, Any], timeout_ms: int) -> list[dict[str, Any]]:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            cur.execute(sql, params)
            return _rows_as_dicts(cur)


def batched(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def fetch_block_ids(
    conn: Any, block: Block, *, timeout_ms: int, negctl_max: int
) -> tuple[list[int], dict[str, Any]]:
    """The listing ids of one block, plus whatever detail the block's rule produced."""
    if block.grain == "town":
        rows = _run(conn, COHORT_TOWN_IDS_SQL, {"code": block.code}, timeout_ms)
        return [int(r["listing_id"]) for r in rows], {}
    if block.grain == "quarter":
        rows = _run(conn, COHORT_QUARTER_IDS_SQL, {"code": block.code}, timeout_ms)
        return [int(r["listing_id"]) for r in rows], {}
    rule = cohort.NEGATIVE_CONTROL
    groups = _run(
        conn,
        COHORT_NEGCTL_GROUPS_SQL,
        {
            "obec_kod": block.code,
            "exclude_cast_obce_kod": rule.exclude_cast_obce_kod,
            "min_group_size": rule.min_group_size,
            "min_distinct_floors": rule.min_distinct_floors,
        },
        timeout_ms,
    )
    ids, taken = cohort.select_negative_control(groups, max_listings=negctl_max)
    return ids, {"groups_available": len(groups), "groups_taken": len(taken)}


def _by_id(rows: Iterable[dict[str, Any]], key: str) -> dict[int, dict[str, Any]]:
    return {int(row[key]): row for row in rows}


# --- mode entry -------------------------------------------------------------------------

TIMEOUT_S_MAX = 3600
NEGCTL_MAX_CEILING = 5 * cohort.NEGATIVE_CONTROL.max_listings

ARG_DEFAULTS: dict[str, Any] = {
    "blocks": "",
    "timeout_s": 600,
    # The corpus-wide phash population is one unindexed scan of `images` (ruling D8 forbids
    # the index), so it gets its own, larger budget.
    "phash_timeout_s": 1800,
    "batch": 1000,
    "clip_model": DEFAULT_CLIP_MODEL,
    "negctl_max": cohort.NEGATIVE_CONTROL.max_listings,
}


def parse_export_args(args: dict[str, str]) -> dict[str, Any]:
    """Coerce the lane's k=v strings into export parameters. Unknown keys are fatal."""
    unknown = sorted(set(args) - set(ARG_DEFAULTS))
    if unknown:
        raise ValueError(
            f"unknown export arg(s) {', '.join(unknown)}; known: {', '.join(sorted(ARG_DEFAULTS))}"
        )
    out: dict[str, Any] = dict(ARG_DEFAULTS)
    out.update(args)
    for key in ("timeout_s", "phash_timeout_s", "batch", "negctl_max"):
        try:
            out[key] = int(out[key])
        except (TypeError, ValueError):
            raise ValueError(f"export arg {key} must be an integer, got {out[key]!r}") from None
    # Both timeouts reach SQL as text (SET LOCAL takes no parameter), so they are clamped
    # here as well as coerced.
    for key in ("timeout_s", "phash_timeout_s"):
        if not 1 <= out[key] <= TIMEOUT_S_MAX:
            raise ValueError(f"export arg {key} must be between 1 and {TIMEOUT_S_MAX}")
    if not 1 <= out["batch"] <= 10000:
        raise ValueError("export arg batch must be between 1 and 10000")
    # Ruling D1 caps this stratum at 800 listings; the clamp leaves headroom for a
    # deliberate sensitivity run and stops a dispatched typo from dragging tens of thousands
    # of production listings (and their images, vectors and snapshots) into one gzip.
    if not 1 <= out["negctl_max"] <= NEGCTL_MAX_CEILING:
        raise ValueError(f"export arg negctl_max must be between 1 and {NEGCTL_MAX_CEILING}")
    if not str(out["clip_model"]).strip():
        raise ValueError("export arg clip_model must not be empty")
    out["blocks"] = str(out["blocks"])
    out["clip_model"] = str(out["clip_model"])
    return out


def run_export(
    conn_factory: Callable[[], Any], args: dict[str, str], out_dir: Any
) -> dict[str, Any]:
    params = parse_export_args(args)
    blocks = cohort.parse_blocks(params["blocks"])
    timeout_ms = int(params["timeout_s"]) * 1000
    phash_timeout_ms = int(params["phash_timeout_s"]) * 1000
    batch = int(params["batch"])
    model = params["clip_model"]

    timings: dict[str, float] = {}
    # `snapshots` counts the rows READ from listing_snapshots, so the number reconciles
    # against that table; `price_events` counts what the artifact actually carries after the
    # MAX_PRICE_EVENTS truncation.
    counts: dict[str, int] = {
        "listings": 0, "images": 0, "clip_vectors": 0, "snapshots": 0, "price_events": 0
    }
    block_stats: list[dict[str, Any]] = []
    errors: list[str] = []

    out_path = Path(out_dir) / ARTIFACT_NAME
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def say(message: str) -> None:
        print(f"[export] {message}", flush=True)

    conn = conn_factory()
    try:
        started = time.monotonic()
        block_of: dict[int, str] = {}
        for block in blocks:
            block_started = time.monotonic()
            ids, detail = fetch_block_ids(
                conn, block, timeout_ms=timeout_ms, negctl_max=int(params["negctl_max"])
            )
            fresh = [i for i in ids if i not in block_of]
            for listing_id in fresh:
                block_of[listing_id] = block.key
            stat = {**block.as_meta(), "n_listings": len(fresh), **detail}
            block_stats.append(stat)
            timings[f"ids_{block.key}_s"] = round(time.monotonic() - block_started, 3)
            say(f"block {block.key}: {len(fresh)} listings")
        listing_ids = sorted(block_of)
        timings["ids_s"] = round(time.monotonic() - started, 3)

        started = time.monotonic()
        listing_rows: list[dict[str, Any]] = []
        for chunk in batched(listing_ids, batch):
            listing_rows.extend(_run(conn, COHORT_LISTINGS_SQL, {"ids": chunk}, timeout_ms))
        timings["listings_s"] = round(time.monotonic() - started, 3)
        say(f"listings: {len(listing_rows)} rows")

        started = time.monotonic()
        locations: dict[int, dict[str, Any]] = {}
        for chunk in batched(listing_ids, batch):
            locations.update(
                _by_id(_run(conn, COHORT_LOCATION_SQL, {"ids": chunk}, timeout_ms), "listing_id")
            )
        timings["location_s"] = round(time.monotonic() - started, 3)

        started = time.monotonic()
        history: dict[int, list[dict[str, Any]]] = {}
        for chunk in batched(listing_ids, batch):
            for row in _run(conn, COHORT_PRICE_HISTORY_SQL, {"ids": chunk}, timeout_ms):
                history.setdefault(int(row["listing_id"]), []).append(row)
                counts["snapshots"] += 1
        timings["price_history_s"] = round(time.monotonic() - started, 3)

        started = time.monotonic()
        image_rows: list[dict[str, Any]] = []
        for chunk in batched(listing_ids, batch):
            image_rows.extend(_run(conn, COHORT_IMAGES_SQL, {"ids": chunk}, timeout_ms))
        image_ids = [int(row["image_id"]) for row in image_rows]
        timings["images_s"] = round(time.monotonic() - started, 3)
        say(f"images: {len(image_rows)} rows")

        started = time.monotonic()
        hashes = sorted({h for row in image_rows if (h := _int_or_none(row.get("phash"))) is not None})
        pop: dict[int, int] | None = {}
        if hashes:
            # The one statement in this export that can plausibly blow its budget (no index
            # on images.phash, and D8 forbids adding one). A timeout here costs the catalog
            # ratio, not the artifact: every `pop` is written null-or-zero and the run says so.
            try:
                for row in _run(conn, COHORT_PHASH_POP_SQL, {"hashes": hashes}, phash_timeout_ms):
                    pop[int(row["phash"])] = int(row["n_listings"])
            except Exception as exc:  # noqa: BLE001 — one probe must not void the export
                errors.append(f"phash_pop: {type(exc).__name__}: {exc}")
                # UNKNOWN, not zero: every `pop` goes out null so a reader can see that E9's
                # catalog subtraction has no input, instead of reading "every photo unique".
                pop = None
        timings["phash_pop_s"] = round(time.monotonic() - started, 3)
        phash_pop_ok = pop is not None
        say(
            f"phash population: {len(hashes)} hashes in {timings['phash_pop_s']}s "
            f"(unindexed scan, ok={phash_pop_ok})"
        )

        started = time.monotonic()
        for chunk in batched(image_ids, batch):
            rows = _run(conn, COHORT_CLIP_COUNT_SQL, {"ids": chunk, "model": model}, timeout_ms)
            counts["clip_vectors"] += int(rows[0]["n"]) if rows else 0
        timings["clip_count_s"] = round(time.monotonic() - started, 3)

        # Records first, counts second, file third: `meta` leads the stream and carries the
        # exact counts, so every number in it is known before a byte is written.
        listing_records: list[dict[str, Any]] = []
        for row in listing_rows:
            listing_id = int(row["id"])
            record = build_listing_record(
                row,
                block=block_of.get(listing_id, ""),
                location=locations.get(listing_id),
                history=history.get(listing_id, ()),
            )
            counts["price_events"] += len(record["price_history"])
            listing_records.append(record)
        counts["listings"] = len(listing_records)
        counts["images"] = len(image_rows)

        started = time.monotonic()
        # A truncated artifact is worse than none: the workflow uploads `out/` with
        # `if: always()`, so a mid-stream failure would ship a valid gzip whose record stream
        # simply stops. The final name appears only after the stream closes cleanly.
        part_path = out_path.with_name(out_path.name + ".part")
        with gzip.open(part_path, "wt", encoding="utf-8", newline="\n") as fh:
            def emit(record: dict[str, Any]) -> None:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

            emit(
                {
                    "t": "meta",
                    "exported_at": datetime.now().astimezone().isoformat(),
                    "generator_version": GENERATOR_VERSION,
                    "blocks": [b.as_meta() for b in blocks],
                    "counts": dict(counts),
                    "phash_pop_ok": phash_pop_ok,
                    "errors": list(errors),
                    "params": {**params, "salt": AUTODEDUP_BROKER_SALT},
                }
            )
            for record in listing_records:
                emit(record)
            written = 0
            for chunk in batched(image_rows, batch):
                chunk_ids = [int(row["image_id"]) for row in chunk]
                clips = {
                    int(row["image_id"]): encode_clip(row.get("embedding"))
                    for row in _run(
                        conn, COHORT_CLIP_SQL, {"ids": chunk_ids, "model": model}, timeout_ms
                    )
                }
                tags: dict[int, list[dict[str, Any]]] = {}
                for row in _run(
                    conn, COHORT_CLIP_TAGS_SQL, {"ids": chunk_ids, "model": model}, timeout_ms
                ):
                    tags.setdefault(int(row["image_id"]), []).append(row)
                for row in chunk:
                    image_id = int(row["image_id"])
                    emit(
                        build_image_record(
                            row,
                            clip=clips.get(image_id),
                            tags=tag_pairs(tags.get(image_id, [])),
                            pop=pop,
                        )
                    )
                written += len(chunk)
                say(f"images written: {written}/{len(image_rows)}")
        os.replace(part_path, out_path)
        timings["write_s"] = round(time.monotonic() - started, 3)
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()

    size = out_path.stat().st_size if out_path.exists() else 0
    say(f"done: {counts} -> {out_path} ({size} bytes)")
    return {
        "artifact": str(out_path),
        "bytes": size,
        "counts": counts,
        "blocks": block_stats,
        "params": params,
        "timings": timings,
        "phash_hashes": len(hashes),
        "phash_pop_ok": phash_pop_ok,
        "errors": errors,
    }
