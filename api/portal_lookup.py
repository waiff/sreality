"""POST /listings/lookup — batch (source, native id) → MF facts + latest estimate.

The Chrome extension overlays the Browse-card 'Výnos MF' yield
(`mf_gross_yield_pct`) and the MF reference rent (`mf_reference_rent_czk`) on
portal detail + index pages, across every scraped portal, and deep-links each
listing to its SPA page (`/listing/{sreality_id}`). The public views expose
only `(source, sreality_id)`, so a non-sreality card — which only knows its own
native id from its href — can't map to our row from the browser. This
server-side lookup resolves uniformly on `(source, source_id_native)` (the
migration-091 unique key; for sreality `source_id_native` is the numeric id as
text) and joins any latest successful estimation. Read-only; bearer-gated like
every other non-/health route.

It also returns the listing's `property_id` + its **deal-pipeline membership**
(rule #22) so the panel's "Přidat do pipeline" toggle knows its state in the
one call it already makes — the toggle then writes through the existing
bearer-gated `POST/DELETE /pipeline/cards` (the same path the SPA uses). The
pipeline is property-grain, so membership is read off `l.property_id`. It
likewise returns the property's **collection memberships** (`collection_ids`,
rule #18) so the panel's one-click monitoring toggle knows whether the property
is already in the monitoring collection, writing through the existing
bearer-gated `POST/DELETE /collections/{id}/properties`.

Rows come back keyed by column name (dict_row) — no positional index math, so
projecting one more column is a one-line change that can't silently misalign.

TWO connections since the extension's own JWT went live (Wave 1): the shared
market facts (`listings` + `properties`) are read on the SERVICE-ROLE
connection — those tables are RLS-enabled-with-zero-policies by design (they
carry broker PII inline, so a blanket `authenticated` read policy is the exact
leak A6 walled off; see the A5 correction in
docs/design/phase-1-multitenancy-foundations.md), which means a tenant
connection sees zero rows and every lookup would report found=false. The
per-account joins (pipeline membership, collection memberships, latest
estimation) run on the TENANT connection so RLS scopes them to the caller —
including the SYSTEM-account arm of estimation_runs_tenant_read (migration
291), which is what lets the platform's pre-multi-tenancy golden estimates
still surface. Same trusted-server split POST /estimations already uses.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from psycopg.rows import dict_row

from toolkit.filter_registry import subtype_label_cs

if TYPE_CHECKING:
    import uuid

    import psycopg

    from api import schemas as s

# Listing columns projected onto each entry (dict keys === SELECT aliases).
# `listing_id` is the surrogate (always present for a found listing) and is what
# a write should carry; `sreality_id` is the legacy handle, kept for the SPA's
# legacy /listing/{sreality_id} route and NULL post-Gate-2.
_LISTING_COLS: tuple[str, ...] = (
    "sreality_id", "listing_id", "property_id",
    "category_main", "category_type", "area_m2", "price_czk", "disposition", "subtype",
    "district", "locality", "is_active", "last_seen_at",
    "mf_reference_rent_czk", "mf_gross_yield_pct",
)

_MARKET_SQL = """
WITH req(source, source_id) AS (VALUES {values})
SELECT
    req.source,
    req.source_id,
    (l.source_id_native IS NOT NULL) AS found,
    l.sreality_id, l.id AS listing_id, l.property_id, l.source_url,
    l.category_main, l.category_type, l.area_m2, l.price_czk, l.disposition, l.subtype,
    l.district, l.locality, l.is_active, l.last_seen_at,
    -- MF figures are PROPERTY-grain (the golden record), so every portal's advert
    -- of one flat shows the SAME number. coalesce to the listing's own value only
    -- for the brief pre-attach window (property_id NULL ~5 min) — a fresh listing
    -- is a singleton, whose golden record already equals its own per-listing value.
    coalesce(pr.mf_reference_rent_czk, l.mf_reference_rent_czk) AS mf_reference_rent_czk,
    coalesce(pr.mf_gross_yield_pct,    l.mf_gross_yield_pct)    AS mf_gross_yield_pct
FROM req
LEFT JOIN listings l
    ON l.source = req.source AND l.source_id_native = req.source_id
LEFT JOIN properties pr ON pr.id = l.property_id
"""

# The %s account predicates on the pipeline + collection joins are explicit
# defense-in-depth, NOT redundant with RLS: this runs on tenant_conn, whose
# legacy static-token branch is the unscoped service-role connection (RLS off) —
# the same reason api/pipeline.py scopes every statement by account. Positional
# params, so account_id is appended THREE times in the textual order the
# placeholders appear (collection subquery in the SELECT list first, then the
# two JOINs). The estimation LATERAL is deliberately NOT given an explicit
# predicate: its correct scoping needs the three-arm SYSTEM-account logic
# (estimation_runs_tenant_read / mig 341), so it stays on RLS to avoid the
# mig-316 "empties the golden estimate" regression.
_ACCOUNT_SQL = """
WITH tgt(listing_id, property_id, source_url) AS (VALUES {values})
SELECT
    tgt.listing_id,
    e.id AS estimation_id, e.estimate_kind AS estimation_kind,
    e.gross_yield_pct AS estimation_yield,
    (pp.property_id IS NOT NULL) AS in_pipeline,
    pp.stage_id AS pipeline_stage_id,
    ps.key   AS pipeline_stage_key,
    ps.label AS pipeline_stage_label,
    (SELECT coalesce(array_agg(cp.collection_id ORDER BY cp.collection_id), array[]::bigint[])
       FROM collection_properties cp
      WHERE cp.property_id = tgt.property_id
        AND cp.account_id IS NOT DISTINCT FROM %s) AS collection_ids
FROM tgt
LEFT JOIN property_pipeline pp
       ON pp.property_id = tgt.property_id AND pp.account_id IS NOT DISTINCT FROM %s
LEFT JOIN pipeline_stages   ps
       ON ps.id = pp.stage_id AND ps.account_id IS NOT DISTINCT FROM %s
LEFT JOIN LATERAL (
    SELECT er.id, er.estimate_kind, er.gross_yield_pct
    FROM estimation_runs er
    WHERE er.status = 'success'
      -- Match on the SURROGATE for every source (R2). input_listing_id is
      -- dual-written for any already-scraped listing. The URL string-equality
      -- arm stays a demoted fallback for an estimation created BEFORE its
      -- listing was scraped (input_listing_id still NULL) — note the
      -- discriminator had to move too: keyed on input_sreality_id it would,
      -- post-Gate-2, route every successfully-resolved non-sreality listing
      -- into the fragile URL arm, where any URL-normalisation difference
      -- silently drops the estimation.
      AND (
        er.input_listing_id = tgt.listing_id
        OR (er.input_listing_id IS NULL AND er.input_url = tgt.source_url)
      )
    ORDER BY er.created_at DESC
    LIMIT 1
) e ON true
"""


def _clean(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def lookup_portal_listings(
    market_conn: "psycopg.Connection",
    tenant_conn: "psycopg.Connection",
    items: "list[s.PortalLookupItem]",
    account_id: "uuid.UUID | None" = None,
) -> dict[str, Any]:
    """Resolve each (source, source_id) to its MF facts + latest estimate.

    One row per requested item, in request order; `found=false` (and null
    fields) when we have no listing for that pair. Market facts read on
    `market_conn` (service-role — see module docstring); pipeline/collections/
    estimation on `tenant_conn` (RLS-scoped to the caller's account, with
    `account_id` also predicated explicitly on the pipeline + collection joins
    for the RLS-bypassing legacy branch — see `_ACCOUNT_SQL`).
    """
    values_sql = ", ".join(["(%s::text, %s::text)"] * len(items))
    params: list[str] = []
    for it in items:
        params.extend([it.source, it.source_id])

    with market_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_MARKET_SQL.format(values=values_sql), params)
        rows = cur.fetchall()

    found_rows = [r for r in rows if r["found"] and r["listing_id"] is not None]
    account_by_listing: dict[int, dict[str, Any]] = {}
    if found_rows:
        tgt_sql = ", ".join(["(%s::bigint, %s::bigint, %s::text)"] * len(found_rows))
        tgt_params: list[Any] = []
        for r in found_rows:
            tgt_params.extend([r["listing_id"], r["property_id"], r["source_url"]])
        # account_id thrice, in the textual placeholder order of _ACCOUNT_SQL
        # (collection subquery, property_pipeline join, pipeline_stages join).
        tgt_params.extend([account_id, account_id, account_id])
        with tenant_conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_ACCOUNT_SQL.format(values=tgt_sql), tgt_params)
            for arow in cur.fetchall():
                account_by_listing[arow["listing_id"]] = arow

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        entry: dict[str, Any] = {
            "source": row["source"],
            "source_id": row["source_id"],
            "found": bool(row["found"]),
        }
        entry.update({col: _clean(row[col]) for col in _LISTING_COLS})
        # The display "kind": the subtype label (commercial/houses) else the
        # disposition (apartments). Computed server-side from the one canonical
        # label source so the extension needs no slug dictionary of its own.
        entry["kind_label"] = subtype_label_cs(row["subtype"]) or row["disposition"]
        acct = account_by_listing.get(row["listing_id"], {})
        est_id = acct.get("estimation_id")
        entry["latest_estimation"] = (
            {
                "estimation_id": est_id,
                "estimate_kind": acct.get("estimation_kind"),
                "gross_yield_pct": _clean(acct.get("estimation_yield")),
            }
            if est_id is not None
            else None
        )
        # Deal-pipeline membership is property-grain (rule #22): only present
        # once the listing is attached to a property (a freshly-scraped row is
        # property_id NULL for ~5 min — the panel hides the toggle until then).
        entry["pipeline"] = (
            {
                "in_pipeline": bool(acct.get("in_pipeline")),
                "stage_id": acct.get("pipeline_stage_id"),
                "stage_key": acct.get("pipeline_stage_key"),
                "stage_label": acct.get("pipeline_stage_label"),
            }
            if row["property_id"] is not None
            else None
        )
        # Collection memberships are property-grain (rule #18) — same NULL-until-
        # attached posture as pipeline above; the panel's monitoring toggle reads it.
        entry["collection_ids"] = (
            list(acct.get("collection_ids") or []) if row["property_id"] is not None else None
        )
        by_key[(row["source"], row["source_id"])] = entry

    fallback = lambda it: {  # noqa: E731 — tiny shape for the (rare) missing row
        "source": it.source, "source_id": it.source_id, "found": False,
        "latest_estimation": None, "pipeline": None, "collection_ids": None,
    }
    return {"data": [by_key.get((it.source, it.source_id), fallback(it)) for it in items]}
