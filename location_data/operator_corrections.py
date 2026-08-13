"""Location W1v: the OPERATOR claim producer (03 S7 rank 1, 05 5.5.5).

One of the four claim producers in the design (portal contracts, the LLM lane,
operator input, the migration loader). A correction is an appended
`location_claims` row - `surface='operator_input'`,
`extraction_method='operator_manual'`, `licence_class='operator'`,
`claim_confidence='exact'` - never an UPDATE of anything: a wrong correction
is superseded by a newer one (S7 breaks operator-vs-operator ties by recency),
and the claim spine stays append-only.

Two deliberate choices, both learned from the intake lane:

* The `dirty_locations` enqueue is UNCONDITIONAL, not gated on the insert CTE.
  `claim_fingerprint` is time-free, so a correction that RESTATES an earlier
  operator value (A -> B -> A) collides with the original claim's fingerprint
  and inserts nothing - an `ins`-gated enqueue would never fire and the
  operator would see a dead button. A restated claim still gets a re-sight
  observation row, so the restatement is on the record either way.
* `value_norm` and the fingerprint are computed in SQL by the named migration
  functions (`location_value_norm`, `location_claim_fingerprint`), exactly as
  the intake does - a Python mirror drifts on the foreign-address cohort and a
  drifted fingerprint does not conflict, it inserts.

snapshot_anchor is 'unanchored_latest_fetch': an operator correction is a
statement about the listing as currently served, not about a stored payload
(and not registry-derived even when it asserts a registry key - the OPERATOR
chose the key; the registry only validated it).

The synchronous projection refresh (05 5.5.5: operator-initiated changes are
visible on the next read) is `resolve_now()` = `drain.run(only_listing_id=)`;
`submit_correction` callers invoke it after the claim transaction commits. If
it fails, the unconditional enqueue guarantees the */15 drain converges.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row

LOG = logging.getLogger("location.operator_corrections")

EXTRACTOR_ID = "operator.correction"
EXTRACTOR_VERSION = "1"

# Text-valued claim types an operator can assert in W1v. `coordinate` (a pin
# drag) is deliberately absent: it needs a geometry input surface and its own
# licence reasoning; the policy row for it already exists (migration 400), so
# adding it later is UI work, not schema work.
ALLOWED_CLAIM_TYPES: frozenset[str] = frozenset({
    "address_point_id",
    "street_name",
    "house_number_cp",
    "house_number_co",
    "psc",
    "obec_name",
    "cast_obce_name",
    "okres_name",
})


class CorrectionError(ValueError):
    """Invalid correction input (maps to HTTP 422 at the API layer)."""


class UnknownListingError(CorrectionError):
    pass


class UnknownKodAdmError(CorrectionError):
    pass


# Single-row variant of claims_intake._CLAIM_WRITE_SQL. Same CTE shape, same
# named functions, same ON CONFLICT target - but the enqueue reads the INPUT
# row, not `ins` (see module docstring). Kept inline rather than imported so
# the two lanes can diverge without hidden coupling; the fingerprint CALL is
# what must never be re-transcribed, and both statements use the migration 386
# function for it.
_OPERATOR_CLAIM_SQL = """
    WITH input AS (
        SELECT %(listing_id)s::bigint AS listing_id, %(source)s::text AS source,
               %(source_id_native)s::text AS source_id_native,
               'unanchored_latest_fetch'::text AS snapshot_anchor,
               now() AS first_observed_at,
               %(claim_type)s::text AS claim_type,
               'operator_input'::text AS surface, 'none'::text AS page_kind,
               'operator_manual'::text AS extraction_method,
               %(extractor_id)s::text AS extractor_id,
               %(extractor_version)s::text AS extractor_version,
               NULL::bigint AS contract_entry_id,
               %(value_text)s::text AS value_text,
               NULL::numeric AS value_num, NULL::text AS value_geom_wkt,
               NULL::text AS value_shape_wkt, NULL::jsonb AS value_jsonb,
               NULL::integer AS distance_m, NULL::text AS travel_mode,
               NULL::text AS target_text,
               NULL::text AS declared_precision_label,
               NULL::text AS declared_confidence, NULL::numeric AS declared_radius_m,
               'exact'::text AS claim_confidence,
               'none'::text AS blur_evidence, 'operator'::text AS licence_class,
               NULL::text AS legacy_source_column, false AS legacy_write_path_unknown,
               NULL::text AS history_completeness, true AS subject_scoped
    ), typed AS (
        SELECT i.*,
               location_value_norm(i.value_text) AS value_norm,
               NULL::geometry AS geom, NULL::geometry AS shape
        FROM input i
    ), fingerprinted AS (
        SELECT t.*,
               location_claim_fingerprint(
                   t.listing_id, t.source, t.source_id_native,
                   t.claim_type, t.surface, t.page_kind, t.extraction_method,
                   t.extractor_id, t.extractor_version, t.contract_entry_id,
                   t.value_norm, t.value_text,
                   t.value_num, t.geom, t.shape,
                   t.value_jsonb, t.distance_m, t.travel_mode, t.target_text,
                   t.declared_precision_label, t.declared_confidence, t.declared_radius_m,
                   t.legacy_source_column) AS claim_fingerprint
        FROM typed t
    ), ins AS (
        INSERT INTO location_claims (
            listing_id, source, source_id_native, snapshot_anchor, first_observed_at,
            claim_type, surface, page_kind, extraction_method, extractor_id,
            extractor_version, contract_entry_id, batch_id, value_text, value_norm,
            value_num, value_geom, value_shape, value_jsonb, distance_m, travel_mode,
            target_text, declared_precision_label, declared_confidence, declared_radius_m,
            claim_confidence, blur_evidence, licence_class, legacy_source_column,
            legacy_write_path_unknown, history_completeness, subject_scoped,
            claim_fingerprint)
        SELECT f.listing_id, f.source, f.source_id_native, f.snapshot_anchor,
               f.first_observed_at, f.claim_type::location_claim_type,
               f.surface::location_claim_surface, f.page_kind::location_page_kind,
               f.extraction_method::location_extraction_method, f.extractor_id,
               f.extractor_version, f.contract_entry_id, %(batch_id)s, f.value_text,
               f.value_norm, f.value_num, f.geom, f.shape, f.value_jsonb, f.distance_m,
               f.travel_mode, f.target_text, f.declared_precision_label,
               f.declared_confidence, f.declared_radius_m,
               f.claim_confidence::match_confidence,
               f.blur_evidence::blur_evidence, f.licence_class::licence_class,
               f.legacy_source_column, f.legacy_write_path_unknown,
               f.history_completeness, f.subject_scoped, f.claim_fingerprint
        FROM fingerprinted f
        ON CONFLICT (claim_fingerprint) DO NOTHING
        RETURNING id
    ), resighted AS (
        SELECT c.id, f.first_observed_at, f.extractor_version
        FROM fingerprinted f
        JOIN location_claims c ON c.claim_fingerprint = f.claim_fingerprint
    ), obs AS (
        INSERT INTO location_claim_observations
            (claim_id, observed_at, extractor_version)
        SELECT r.id, r.first_observed_at, r.extractor_version
        FROM resighted r
        WHERE NOT EXISTS (
            SELECT 1 FROM location_claim_observations o
            WHERE o.claim_id = r.id AND o.observed_at = r.first_observed_at)
        RETURNING claim_id
    ), enqueued AS (
        INSERT INTO dirty_locations (listing_id, reason)
        VALUES (%(listing_id)s, 'operator_edit')
        ON CONFLICT (listing_id) DO NOTHING
        RETURNING listing_id
    )
    SELECT (SELECT count(*) FROM ins)      AS inserted,
           (SELECT count(*) FROM obs)      AS observations,
           (SELECT count(*) FROM enqueued) AS enqueued
"""

_KOD_ADM_SQL = """
    SELECT ap.kod_adm,
           u_obec.name AS obec,
           s.name      AS street,
           ap.cislo_domovni::text AS cislo_domovni,
           ap.cislo_orientacni    AS cislo_orientacni,
           ap.psc::text           AS psc
    FROM ruian_address_points ap
    LEFT JOIN ruian_streets s
           ON s.id = ap.street_id
    LEFT JOIN ruian_admin_units u_obec
           ON u_obec.id = ap.obec_unit_id
    WHERE ap.kod_adm = %s AND ap.valid_to IS NULL
"""

_PROJECTION_SQL = """
    SELECT listing_id, granularity::text, position_source::text,
           match_confidence::text, admin_assignment_method::text,
           uncertainty_radius_m, ruian_adm_kod, street_name,
           house_number_cp, house_number_co, psc, obec_name, okres_name,
           display_label, location_disputed, resolver_version, built_at
    FROM listing_location_current WHERE listing_id = %s
"""


def submit_correction(
    conn: psycopg.Connection,
    *,
    listing_id: int,
    claim_type: str,
    value_text: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Append one operator claim + unconditional dirty enqueue. One transaction."""
    if claim_type not in ALLOWED_CLAIM_TYPES:
        raise CorrectionError(
            f"claim_type {claim_type!r} not correctable; allowed: "
            + ", ".join(sorted(ALLOWED_CLAIM_TYPES))
        )
    value_text = (value_text or "").strip()
    if not value_text:
        raise CorrectionError("value_text must be non-empty")

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, source, source_id_native FROM listings WHERE id = %s",
            (listing_id,),
        )
        listing = cur.fetchone()
    if listing is None:
        raise UnknownListingError(f"listing {listing_id} does not exist")

    registry_echo: dict[str, Any] | None = None
    if claim_type == "address_point_id":
        if not value_text.isdigit():
            raise CorrectionError("address_point_id must be a bare kod ADM (digits only)")
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_KOD_ADM_SQL, (int(value_text),))
            registry_echo = cur.fetchone()
        if registry_echo is None:
            raise UnknownKodAdmError(
                f"kod ADM {value_text} is not a current address point in the RUIAN mirror"
            )

    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO location_claim_batches
                    (lane, source, extractor_version, finished_at, row_count, outcome, note)
                VALUES ('operator_correction', %s, %s, now(), 1, 'ok', %s)
                RETURNING id
                """,
                (listing["source"], EXTRACTOR_VERSION, note),
            )
            batch_id = cur.fetchone()["id"]
            cur.execute(
                _OPERATOR_CLAIM_SQL,
                {
                    "listing_id": listing_id,
                    "source": listing["source"],
                    "source_id_native": listing["source_id_native"],
                    "claim_type": claim_type,
                    "value_text": value_text,
                    "extractor_id": EXTRACTOR_ID,
                    "extractor_version": EXTRACTOR_VERSION,
                    "batch_id": batch_id,
                },
            )
            counts = cur.fetchone()

    result: dict[str, Any] = {
        "listing_id": listing_id,
        "source": listing["source"],
        "claim_type": claim_type,
        "value_text": value_text,
        "inserted": counts["inserted"] == 1,
        "restatement": counts["inserted"] == 0,
        "enqueued": counts["enqueued"] == 1,
        "registry_echo": registry_echo,
        "resolved": False,
        "projection": None,
    }
    return result


def resolve_now(conn: psycopg.Connection, listing_id: int) -> bool:
    """Synchronous single-listing resolve (05 5.5.5). Best-effort: on failure the
    unconditional enqueue guarantees the */15 drain converges."""
    try:
        from location_data.resolver import drain

        drain.run(conn, only_listing_id=listing_id, batch_size=1)
        return True
    except Exception:
        LOG.exception("synchronous resolve failed for listing %s; drain will pick it up",
                      listing_id)
        return False


def read_projection(conn: psycopg.Connection, listing_id: int) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_PROJECTION_SQL, (listing_id,))
        row = cur.fetchone()
    if row is not None and row.get("built_at") is not None:
        row["built_at"] = row["built_at"].isoformat()
    return row
