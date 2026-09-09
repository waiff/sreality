"""`location_v2.<feature>` runtime flags — the W6 serving-flip precondition (MASTER.md §2.2,
roadmap location-data.md W6 row).

W6 is a per-feature cutover, not a wave-wide switch: each downstream consumer of the new
location stack (`listing_location_current` / `property_location_current`) flips onto it
independently, in MASTER.md §2.2's ascending-blast-radius order — dashboards → dedup blocking
→ filters and statistics → map rendering → estimation comparables — spelled here with the
roadmap's shorthand. R12 is why they are runtime flags at all: "so every non-estimation flip
is reversible without a deploy". A flag lives in `app_settings` (migration 020), the same mechanism
already used for `location_payload_shadow_hash` and `gate2_null_sreality_id_enabled`
(`scraper/db.py`); it is intentionally NOT seeded by a migration — a feature is unflipped
until an operator writes the row, and a missing row reads as OFF, i.e. "keep reading
`listings.geom` and the geo-derived admin columns", which is the safe direction.

No consumer reads one of these flags yet: `dashboards` (the admin-gated Location Quality
page) already reads the projection unconditionally, wired directly in W1v before this
flag mechanism existed, and the other four features have not flipped at all. This module
is the flag surface those flips will gate on; it does not itself change what any consumer
reads.

To flip a feature on, an operator runs (no migration, no deploy):
    INSERT INTO app_settings (key, value, description, updated_by)
    VALUES ('location_v2.dedup', 'true'::jsonb, 'W6 serving flip: dedup', 'operator')
    ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by;
"""

from __future__ import annotations

import psycopg

FEATURES: tuple[str, ...] = ("dashboards", "dedup", "filters", "map", "estimation")

_FLAG_KEY_PREFIX = "location_v2."


def flag_key(feature: str) -> str:
    """The `app_settings.key` for one W6 feature. Raises on an undeclared feature —
    a typo here must fail loud, never silently read a nonexistent row as OFF-by-accident-
    when-it-meant-something-else."""
    if feature not in FEATURES:
        raise ValueError(f"unknown location_v2 feature {feature!r}; add it to FEATURES first")
    return f"{_FLAG_KEY_PREFIX}{feature}"


def is_enabled(conn: psycopg.Connection, feature: str) -> bool:
    """Live read of one `location_v2.<feature>` flag. Missing row or NULL -> False
    (fresh-deploy-safe, mirrors `scraper.db._app_settings_flag`: a flag that isn't
    seeded yet reads as OFF, so an unflipped feature keeps reading the old path)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = %s",
            (flag_key(feature),),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return False
    value = row[0]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)
