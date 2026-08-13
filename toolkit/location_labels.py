"""Frozen labelled samples: status, labelling writes, and precision scoring
(06 6.4.0).

The sample MEMBERSHIP is frozen at draw time (scripts/location_draw_labelled_sample);
this module only reads it, accepts the operator's ground-truth labels, and
scores both systems against them:

* NEW system = `listing_location_current` (street_name / obec_name / okres_name
  / granularity), compared live so a contract-version bump re-scores against
  the same frozen labels (6.4.0 #3).
* OLD system = the legacy_* columns snapshotted at draw time (6.4.0 #4 - the
  refetch that follows a draw may rewrite listings.street, so the old system
  is scored as it stood).

Text comparison normalizes BOTH sides through migration 382's
`location_value_norm()` - the one canonical normalizer - so a diacritics or
case difference is never counted as an error (ceskereality's ASCII-folded
streets are a real error class, but the label side is typed by hand and must
not fail on accents the operator did type).

Floors (06 6.4.0 #2): street >= 95 %, obec/okres >= 98 %, precision-class
>= 95 %. Precision = matches / rows where the system asserts a value AND the
operator could determine one; yield = asserted / determinable. Both are
reported - the gate is precision, yield is context.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

FLOORS = {"street": 95.0, "obec": 98.0, "okres": 98.0, "precision_class": 95.0}

_LABEL_FIELDS = {
    "label_street": str,
    "label_street_nd": bool,
    "label_house_number": str,
    "label_house_number_nd": bool,
    "label_obec": str,
    "label_obec_nd": bool,
    "label_okres": str,
    "label_okres_nd": bool,
    "label_precision_class": str,
    "label_precision_nd": bool,
    "label_note": str,
}

_GRANULARITY_VALUES = frozenset({
    "unknown", "country", "kraj", "okres", "obec", "cast_obce_or_quarter",
    "street", "street_segment", "parcel", "building", "address_point",
})


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_sample(conn: psycopg.Connection, source: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT s.id, s.source, s.drawn_at, s.method, s.n, s.note,
                   count(m.listing_id) AS members,
                   count(m.listing_id) FILTER (WHERE m.labelled_at IS NOT NULL) AS labelled
            FROM location_labelled_samples s
            LEFT JOIN location_labelled_sample_members m ON m.sample_id = s.id
            WHERE s.source = %s AND s.is_current
            GROUP BY s.id
            """,
            (source,),
        )
        row = cur.fetchone()
    if row is not None and row.get("drawn_at"):
        row["drawn_at"] = row["drawn_at"].isoformat()
    return row


def sample_members(
    conn: psycopg.Connection,
    source: str,
    *,
    unlabelled_only: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Members for the labelling surface. Deliberately EXCLUDES the system's
    current extracted values: the operator labels against the portal page, and
    showing the stored answer would anchor the very judgement that scores it
    (the same reasoning that keeps stored columns out of LLM prompts, D7)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT m.listing_id, m.source_id_native, m.position,
                   m.label_street, m.label_street_nd,
                   m.label_house_number, m.label_house_number_nd,
                   m.label_obec, m.label_obec_nd,
                   m.label_okres, m.label_okres_nd,
                   m.label_precision_class::text, m.label_precision_nd,
                   m.label_note, m.labelled_at,
                   l.is_active
            FROM location_labelled_sample_members m
            JOIN location_labelled_samples s ON s.id = m.sample_id
            LEFT JOIN listings l ON l.id = m.listing_id
            WHERE s.source = %(source)s AND s.is_current
              AND (NOT %(unlabelled_only)s OR m.labelled_at IS NULL)
            ORDER BY m.position
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {
                "source": source,
                "unlabelled_only": unlabelled_only,
                "limit": min(limit, 500),
                "offset": max(offset, 0),
            },
        )
        rows = cur.fetchall()
    for row in rows:
        if row.get("labelled_at"):
            row["labelled_at"] = row["labelled_at"].isoformat()
    return rows


def save_labels(
    conn: psycopg.Connection,
    source: str,
    listing_id: int,
    labels: dict[str, Any],
) -> bool:
    """Upsert the operator's labels for one member of the CURRENT sample.
    Membership is frozen - an unknown listing_id is a refusal, never an insert."""
    updates: dict[str, Any] = {}
    for key, value in labels.items():
        if key not in _LABEL_FIELDS:
            raise ValueError(f"unknown label field {key!r}")
        if value is not None and not isinstance(value, _LABEL_FIELDS[key]):
            raise ValueError(f"label field {key!r} expects {_LABEL_FIELDS[key].__name__}")
        if isinstance(value, str):
            value = value.strip() or None
        updates[key] = value
    if (
        updates.get("label_precision_class") is not None
        and updates["label_precision_class"] not in _GRANULARITY_VALUES
    ):
        raise ValueError("label_precision_class must be a location_granularity value")

    set_sql = ", ".join(f"{k} = %({k})s" for k in updates)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE location_labelled_sample_members m
                SET {set_sql}, labelled_at = now()
                FROM location_labelled_samples s
                WHERE s.id = m.sample_id AND s.source = %(source)s AND s.is_current
                  AND m.listing_id = %(listing_id)s
                """,
                {**updates, "source": source, "listing_id": listing_id},
            )
            return cur.rowcount == 1


_SCORE_SQL = """
    WITH member AS (
      SELECT m.*, p.street_name, p.obec_name, p.okres_name,
             p.granularity::text AS new_granularity
      FROM location_labelled_sample_members m
      JOIN location_labelled_samples s ON s.id = m.sample_id
      LEFT JOIN listing_location_current p ON p.listing_id = m.listing_id
      WHERE s.source = %(source)s AND s.is_current AND m.labelled_at IS NOT NULL
    )
    SELECT
      count(*) AS labelled,

      -- street: determinable = a labelled value exists and is not flagged ND
      count(*) FILTER (WHERE label_street IS NOT NULL AND NOT label_street_nd)
          AS street_determinable,
      count(*) FILTER (WHERE label_street IS NOT NULL AND NOT label_street_nd
                         AND street_name IS NOT NULL) AS street_new_asserted,
      count(*) FILTER (WHERE label_street IS NOT NULL AND NOT label_street_nd
                         AND street_name IS NOT NULL
                         AND location_value_norm(street_name) = location_value_norm(label_street))
          AS street_new_match,
      count(*) FILTER (WHERE label_street IS NOT NULL AND NOT label_street_nd
                         AND legacy_street IS NOT NULL) AS street_old_asserted,
      count(*) FILTER (WHERE label_street IS NOT NULL AND NOT label_street_nd
                         AND legacy_street IS NOT NULL
                         AND location_value_norm(legacy_street) = location_value_norm(label_street))
          AS street_old_match,

      count(*) FILTER (WHERE label_obec IS NOT NULL AND NOT label_obec_nd)
          AS obec_determinable,
      count(*) FILTER (WHERE label_obec IS NOT NULL AND NOT label_obec_nd
                         AND obec_name IS NOT NULL) AS obec_new_asserted,
      count(*) FILTER (WHERE label_obec IS NOT NULL AND NOT label_obec_nd
                         AND obec_name IS NOT NULL
                         AND location_value_norm(obec_name) = location_value_norm(label_obec))
          AS obec_new_match,
      count(*) FILTER (WHERE label_obec IS NOT NULL AND NOT label_obec_nd
                         AND legacy_obec IS NOT NULL) AS obec_old_asserted,
      count(*) FILTER (WHERE label_obec IS NOT NULL AND NOT label_obec_nd
                         AND legacy_obec IS NOT NULL
                         AND location_value_norm(legacy_obec) = location_value_norm(label_obec))
          AS obec_old_match,

      count(*) FILTER (WHERE label_okres IS NOT NULL AND NOT label_okres_nd)
          AS okres_determinable,
      count(*) FILTER (WHERE label_okres IS NOT NULL AND NOT label_okres_nd
                         AND okres_name IS NOT NULL) AS okres_new_asserted,
      count(*) FILTER (WHERE label_okres IS NOT NULL AND NOT label_okres_nd
                         AND okres_name IS NOT NULL
                         AND location_value_norm(okres_name) = location_value_norm(label_okres))
          AS okres_new_match,
      count(*) FILTER (WHERE label_okres IS NOT NULL AND NOT label_okres_nd
                         AND legacy_okres IS NOT NULL) AS okres_old_asserted,
      count(*) FILTER (WHERE label_okres IS NOT NULL AND NOT label_okres_nd
                         AND legacy_okres IS NOT NULL
                         AND location_value_norm(legacy_okres) = location_value_norm(label_okres))
          AS okres_old_match,

      count(*) FILTER (WHERE label_precision_class IS NOT NULL AND NOT label_precision_nd)
          AS precision_determinable,
      count(*) FILTER (WHERE label_precision_class IS NOT NULL AND NOT label_precision_nd
                         AND new_granularity IS NOT NULL) AS precision_new_asserted,
      count(*) FILTER (WHERE label_precision_class IS NOT NULL AND NOT label_precision_nd
                         AND new_granularity = label_precision_class::text)
          AS precision_new_match
    FROM member
"""


def score_sample(conn: psycopg.Connection, source: str) -> dict[str, Any]:
    with conn.transaction():
        conn.execute("SET LOCAL statement_timeout = '20s'")
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_SCORE_SQL, {"source": source})
            raw = cur.fetchone()

    def block(prefix: str, floor_key: str, has_old: bool = True) -> dict[str, Any]:
        det = raw[f"{prefix}_determinable"] or 0
        new_asserted = raw[f"{prefix}_new_asserted"] or 0
        new_match = raw[f"{prefix}_new_match"] or 0
        out: dict[str, Any] = {
            "determinable": det,
            "new": {
                "asserted": new_asserted,
                "matches": new_match,
                "precision_pct": round(100.0 * new_match / new_asserted, 2)
                if new_asserted else None,
                "yield_pct": round(100.0 * new_asserted / det, 2) if det else None,
            },
            "floor_pct": FLOORS[floor_key],
        }
        out["new"]["floor_pass"] = (
            out["new"]["precision_pct"] is not None
            and out["new"]["precision_pct"] >= FLOORS[floor_key]
        )
        if has_old:
            old_asserted = raw[f"{prefix}_old_asserted"] or 0
            old_match = raw[f"{prefix}_old_match"] or 0
            out["old"] = {
                "asserted": old_asserted,
                "matches": old_match,
                "precision_pct": round(100.0 * old_match / old_asserted, 2)
                if old_asserted else None,
                "yield_pct": round(100.0 * old_asserted / det, 2) if det else None,
            }
        return out

    return {
        "data": {
            "source": source,
            "grain": "listing",
            "labelled": raw["labelled"],
            "street": block("street", "street"),
            "obec": block("obec", "obec"),
            "okres": block("okres", "okres"),
            "precision_class": block("precision", "precision_class", has_old=False),
        },
        "metadata": {
            "tool": "location_labels.score_sample",
            "queried_at": _utcnow(),
            "grain": "listing",
        },
    }
