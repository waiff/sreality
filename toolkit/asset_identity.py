"""Asset links: group properties that are the SAME physical building.

The third dedup grain (migration 224). A merge collapses several `listings`
into one `properties` row (same offer/category cohort); an asset link groups
several `properties` rows that are the same physical building across DIFFERENT
category cohorts (a `dum` and a `komercni` at one address, say) WITHOUT
collapsing them — both property rows and both category facets survive, so
per-category Browse/Stats/MF-yield are untouched. This is the operator's tool
(and a future engine MatchProfile's target) for the cross-category sameness the
merge guard in `toolkit.property_identity` correctly refuses.

Reversibility is trivial (clear `asset_id`), so unlike merges there is no replay
ledger — `asset_membership_events` is a plain append-only audit. Linking is
UNION semantics: linking properties that already sit in different assets folds
those assets into one. Unlinking that leaves an asset with <2 members dissolves
it (a one-member asset is meaningless).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg

MEMBERSHIP_SOURCE = ("operator", "auto")


class AssetError(ValueError):
    """An asset link/unlink precondition failed."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_event(
    cur: psycopg.Cursor,
    *,
    asset_id: int,
    property_id: int,
    action: str,
    source: str,
    reason: str | None,
    confidence: float | None,
    created_by: str | None,
) -> None:
    cur.execute(
        """
        INSERT INTO asset_membership_events
            (asset_id, property_id, action, reason, source, confidence, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (asset_id, property_id, action, reason, source, confidence, created_by),
    )


def link_properties(
    conn: psycopg.Connection,
    *,
    property_ids: list[int],
    source: str = "operator",
    reason: str | None = None,
    confidence: float | None = None,
    note: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Link properties into one asset (same physical building). One transaction.

    UNION semantics: if the selection spans existing assets they fold into the
    lowest-id survivor asset; emptied assets are dissolved. A brand-new asset is
    created when none of the selected properties belongs to one. Returns the
    standard envelope with the surviving asset id and its full membership.
    """
    if source not in MEMBERSHIP_SOURCE:
        raise AssetError(f"invalid source {source!r}")
    ids = sorted({int(p) for p in property_ids})
    if len(ids) < 2:
        raise AssetError("need at least two distinct properties to link")

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, asset_id FROM properties "
                "WHERE id = ANY(%s) FOR UPDATE",
                (ids,),
            )
            rows = {int(r[0]): (r[1], r[2]) for r in cur.fetchall()}
            missing = [i for i in ids if i not in rows]
            if missing:
                raise AssetError(f"properties not found: {missing}")
            inactive = [i for i in ids if rows[i][0] != "active"]
            if inactive:
                raise AssetError(
                    f"properties not active (cannot link): {inactive}"
                )

            existing_assets = sorted({int(a) for _s, a in rows.values() if a is not None})
            if existing_assets:
                survivor = existing_assets[0]
                fold = existing_assets[1:]
            else:
                cur.execute(
                    "INSERT INTO assets (note, created_by) VALUES (%s, %s) RETURNING id",
                    (note, created_by),
                )
                survivor = int(cur.fetchone()[0])
                fold = []

            linked: list[int] = []

            # Fold every other existing asset's full membership into the survivor.
            for other in fold:
                cur.execute(
                    "UPDATE properties SET asset_id = %s WHERE asset_id = %s "
                    "RETURNING id",
                    (survivor, other),
                )
                for (pid,) in cur.fetchall():
                    _log_event(
                        cur, asset_id=survivor, property_id=int(pid),
                        action="linked", source=source, reason=reason,
                        confidence=confidence, created_by=created_by,
                    )
                    linked.append(int(pid))
                cur.execute(
                    "UPDATE assets SET status = 'dissolved', dissolved_at = now() "
                    "WHERE id = %s",
                    (other,),
                )

            # Attach any selected property not already on the survivor.
            cur.execute(
                "UPDATE properties SET asset_id = %s "
                "WHERE id = ANY(%s) AND asset_id IS DISTINCT FROM %s RETURNING id",
                (survivor, ids, survivor),
            )
            for (pid,) in cur.fetchall():
                _log_event(
                    cur, asset_id=survivor, property_id=int(pid),
                    action="linked", source=source, reason=reason,
                    confidence=confidence, created_by=created_by,
                )
                linked.append(int(pid))

            cur.execute(
                "SELECT id FROM properties WHERE asset_id = %s ORDER BY id",
                (survivor,),
            )
            members = [int(r[0]) for r in cur.fetchall()]

    return {
        "data": {
            "asset_id": survivor,
            "member_property_ids": members,
            "newly_linked_property_ids": sorted(set(linked)),
            "dissolved_asset_ids": fold,
        },
        "metadata": {
            "tool": "link_properties",
            "source": source,
            "reason": reason,
            "queried_at": _now_iso(),
        },
    }


def unlink_property(
    conn: psycopg.Connection,
    *,
    property_id: int,
    reason: str | None = None,
    created_by: str | None = None,
    source: str = "operator",
) -> dict[str, Any]:
    """Remove one property from its asset. One transaction.

    Clears the property's asset_id and logs an 'unlinked' event. If the asset is
    left with fewer than two members it is dissolved (the lone remaining member
    is detached too), since a single-member asset carries no information.
    """
    if source not in MEMBERSHIP_SOURCE:
        raise AssetError(f"invalid source {source!r}")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT asset_id FROM properties WHERE id = %s FOR UPDATE",
                (property_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise AssetError(f"property {property_id} not found")
            asset_id = row[0]
            if asset_id is None:
                raise AssetError(f"property {property_id} is not in an asset")
            asset_id = int(asset_id)

            cur.execute(
                "UPDATE properties SET asset_id = NULL WHERE id = %s", (property_id,)
            )
            _log_event(
                cur, asset_id=asset_id, property_id=property_id, action="unlinked",
                source=source, reason=reason, confidence=None, created_by=created_by,
            )

            cur.execute(
                "SELECT id FROM properties WHERE asset_id = %s FOR UPDATE",
                (asset_id,),
            )
            remaining = [int(r[0]) for r in cur.fetchall()]
            dissolved = False
            if len(remaining) < 2:
                for pid in remaining:
                    cur.execute(
                        "UPDATE properties SET asset_id = NULL WHERE id = %s", (pid,)
                    )
                    _log_event(
                        cur, asset_id=asset_id, property_id=pid, action="unlinked",
                        source=source, reason="asset_dissolved",
                        confidence=None, created_by=created_by,
                    )
                cur.execute(
                    "UPDATE assets SET status = 'dissolved', dissolved_at = now() "
                    "WHERE id = %s",
                    (asset_id,),
                )
                dissolved = True
                remaining = []

    return {
        "data": {
            "asset_id": asset_id,
            "unlinked_property_id": property_id,
            "remaining_member_ids": remaining,
            "asset_dissolved": dissolved,
        },
        "metadata": {
            "tool": "unlink_property",
            "source": source,
            "reason": reason,
            "queried_at": _now_iso(),
        },
    }


def get_asset(conn: psycopg.Connection, asset_id: int) -> dict[str, Any] | None:
    """Asset metadata + its member property ids, or None if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, note, created_by, created_at, dissolved_at "
            "FROM assets WHERE id = %s",
            (asset_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "SELECT id FROM properties WHERE asset_id = %s ORDER BY id", (asset_id,)
        )
        members = [int(r[0]) for r in cur.fetchall()]
    return {
        "data": {
            "asset_id": int(row[0]),
            "status": row[1],
            "note": row[2],
            "created_by": row[3],
            "created_at": row[4].isoformat() if row[4] else None,
            "dissolved_at": row[5].isoformat() if row[5] else None,
            "member_property_ids": members,
        },
        "metadata": {"tool": "get_asset", "queried_at": _now_iso()},
    }
