"""Top-level entry point for parsing any listing URL.

Classifies the URL by domain, dispatches to either:

  - the existing deterministic sreality flow (scraper.url_parser), or
  - an LLM-driven per-source parser (scraper.source_parsers.*) for
    bezrealitky / idnes_reality / remax / unsupported.

Wraps the LLM path with:
  - 7-day URL-hash cache (parsed_url_cache).
  - llm_calls audit (delegated to LLMClient).
  - Mapy.cz geocoding when the page didn't reveal lat/lng.

Returns a ParseResult that estimation_runs._build_target can consume
without modification (the spec dict shape matches _spec_from_parser).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

from scraper import geocoding, url_parser
from scraper.source_parsers import common

try:
    from psycopg.types.json import Jsonb as _Jsonb
except ImportError:
    def _Jsonb(value: Any) -> Any:  # type: ignore[misc]
        return value

if TYPE_CHECKING:
    import psycopg

    from api.llm_client import LLMClient
    from scraper.sreality_client import SrealityClient

LOG = logging.getLogger(__name__)


SourceKind = Literal[
    "sreality", "bezrealitky", "idnes_reality", "remax", "ceskereality",
    "unsupported",
]


_KIND_SUFFIXES: dict[SourceKind, tuple[str, ...]] = {
    "sreality": ("sreality.cz",),
    "bezrealitky": ("bezrealitky.cz", "bezrealitky.com"),
    "idnes_reality": ("reality.idnes.cz",),
    "remax": ("remax-czech.cz",),
    "ceskereality": ("ceskereality.cz",),
}

_PARSER_MODULE_BY_KIND: dict[SourceKind, str] = {
    "bezrealitky": "scraper.source_parsers.bezrealitky",
    "idnes_reality": "scraper.source_parsers.idnes_reality",
    "remax": "scraper.source_parsers.remax",
    "ceskereality": "scraper.source_parsers.ceskereality",
    "unsupported": "scraper.source_parsers.generic",
}

# Fields the dispatcher cares about for downstream estimation. The LLM
# returns more (description, has_lift, energy_rating, etc.); those are
# kept in the cached payload but not surfaced through the spec.
_REQUIRED_SPEC_FIELDS = ("area_m2", "disposition", "locality")


class ParseError(RuntimeError):
    """Raised when the URL could not be parsed into a usable spec."""


@dataclass(frozen=True)
class ParseResult:
    spec: dict[str, Any]
    source_kind: SourceKind
    parse_confidence: str
    parse_confidence_per_field: dict[str, str] | None
    source_html: str | None
    from_cache: bool
    cost_usd: float | None
    warnings: list[str]
    sreality_id: int | None
    source_url: str
    full_extraction: dict[str, Any] | None = field(default=None)
    fetched_at: str | None = field(default=None)
    wide_spec: dict[str, Any] | None = field(default=None)


# ---------------------------------------------------------------------------
# URL classification + canonicalization
# ---------------------------------------------------------------------------

def classify_url(url: str) -> SourceKind:
    if not isinstance(url, str) or not url.strip():
        return "unsupported"
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return "unsupported"
    host = (parsed.hostname or "").lower()
    if not host:
        return "unsupported"
    for kind, suffixes in _KIND_SUFFIXES.items():
        for suf in suffixes:
            if host == suf or host.endswith("." + suf):
                return kind
    return "unsupported"


def canonical_url(url: str) -> str:
    """Lowercase scheme+host, strip trailing slash, drop query and fragment.

    Two URLs that differ only by `?utm_*` or a trailing slash collapse
    to one cache key. If a real-estate portal ever puts a meaningful
    filter into the query string, we revisit; not seen on the three
    allowlisted sources today.
    """
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/") or "/"
    return f"{scheme}://{host}{port}{path}"


def url_hash(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_listing_url(
    url: str,
    *,
    sreality_client: "SrealityClient",
    llm_client: "LLMClient",
    conn: "psycopg.Connection",
    estimation_run_id: int | None = None,
    force_refresh: bool = False,
    fetch_html: Any = common.fetch_html,
    geocode: Any = geocoding.geocode,
) -> ParseResult:
    kind = classify_url(url)

    if kind == "sreality":
        return _sreality_branch(url, sreality_client, conn)

    if not force_refresh:
        cached = _cache_lookup(conn, url)
        if cached is not None:
            return _result_from_cache(cached, kind=kind, source_url=url)

    parser_module = _load_parser(kind)
    try:
        html = fetch_html(url)
    except Exception as exc:
        raise ParseError(f"failed to fetch {url}: {exc}") from exc

    extraction, cost_usd, warnings = _llm_extract(
        llm_client, parser_module, url, html,
        estimation_run_id=estimation_run_id,
    )
    extraction, warnings = parser_module.post_process(extraction, warnings)

    spec, confidence_per_field, geo_warnings = _build_spec(
        extraction, geocoder=geocode,
    )
    warnings = warnings + geo_warnings
    overall = (
        "best_effort"
        if kind == "unsupported"
        else _overall_confidence(confidence_per_field)
    )

    payload = {
        "spec": spec,
        "extraction": extraction,
        "parse_confidence": overall,
        "parse_confidence_per_field": confidence_per_field,
        "warnings": warnings,
    }
    _cache_store(
        conn, url=url, source_kind=kind, parse_result=payload,
        source_html=html, cost_usd=cost_usd,
    )
    return ParseResult(
        spec=spec,
        source_kind=kind,
        parse_confidence=overall,
        parse_confidence_per_field=confidence_per_field,
        source_html=html,
        from_cache=False,
        cost_usd=cost_usd,
        warnings=warnings,
        sreality_id=None,
        source_url=url,
        full_extraction=extraction,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

def _sreality_branch(
    url: str,
    sreality_client: "SrealityClient",
    conn: "psycopg.Connection",
) -> ParseResult:
    parsed = url_parser.parse_sreality_url(
        url, client=sreality_client, conn=conn, persist=True,
    )
    parser_spec = parsed["spec"]
    spec = {
        "lat": parser_spec.get("lat"),
        "lng": parser_spec.get("lon"),
        "area_m2": parser_spec.get("area_m2"),
        "disposition": parser_spec.get("disposition"),
        "floor": parser_spec.get("floor"),
        "exclude_ids": [],
    }
    return ParseResult(
        spec=spec,
        source_kind="sreality",
        parse_confidence="high",
        parse_confidence_per_field=None,
        source_html=None,
        from_cache=False,
        cost_usd=None,
        warnings=[],
        sreality_id=int(parsed["sreality_id"]),
        source_url=url,
        full_extraction=None,
        fetched_at=parsed.get("fetched_at"),
        wide_spec=dict(parser_spec),
    )


# ---------------------------------------------------------------------------
# LLM extraction + spec building
# ---------------------------------------------------------------------------

def _llm_extract(
    llm_client: "LLMClient",
    parser_module: ModuleType,
    url: str,
    html: str,
    *,
    estimation_run_id: int | None,
) -> tuple[dict[str, Any], float, list[str]]:
    system = llm_client.resolve_system_prompt()
    messages = parser_module.build_messages(url, html)
    response = llm_client.call(
        called_for="parse_url",
        messages=messages,
        system=system,
        tools=[common.RECORD_LISTING_TOOL],
        estimation_run_id=estimation_run_id,
    )
    record_calls = [
        tc for tc in response.tool_calls
        if tc.get("name") == "record_listing"
    ]
    if not record_calls:
        raise ParseError(
            "LLM did not invoke record_listing; refusing to guess"
        )
    extraction = dict(record_calls[0].get("input") or {})
    warnings = list(extraction.pop("warnings", None) or [])
    return extraction, response.cost_usd, warnings


def _build_spec(
    extraction: dict[str, Any],
    *,
    geocoder: Any,
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    confidence_per_field: dict[str, str] = {}
    values: dict[str, Any] = {}
    for field_name, envelope in extraction.items():
        if not isinstance(envelope, dict):
            continue
        values[field_name] = envelope.get("value")
        conf = envelope.get("confidence")
        if isinstance(conf, str):
            confidence_per_field[field_name] = conf

    spec: dict[str, Any] = {
        "lat": None,
        "lng": None,
        "area_m2": _coerce_float(values.get("area_m2")),
        "disposition": _coerce_str(values.get("disposition")),
        "floor": _coerce_int(values.get("floor")),
        "exclude_ids": [],
    }

    geocode_warnings: list[str] = []
    locality = _coerce_str(values.get("locality"))
    if locality:
        try:
            result = geocoder(locality)
            spec["lat"] = result.lat
            spec["lng"] = result.lng
            confidence_per_field["lat"] = result.confidence
            confidence_per_field["lng"] = result.confidence
            if result.confidence == "low":
                geocode_warnings.append(
                    f"geocoded '{locality}' with low confidence "
                    f"({result.matched_type}); coordinates may be off."
                )
        except geocoding.GeocodingError as exc:
            geocode_warnings.append(
                f"geocoding failed for '{locality}': {exc}"
            )
            confidence_per_field["lat"] = "low"
            confidence_per_field["lng"] = "low"
    else:
        geocode_warnings.append("no locality string extracted; cannot geocode")
        confidence_per_field["lat"] = "low"
        confidence_per_field["lng"] = "low"

    return spec, confidence_per_field, geocode_warnings


def _overall_confidence(confidence_per_field: dict[str, str]) -> str:
    """Aggregate to high / medium / low based on the spec-critical fields.

    The fields that actually drive an estimation are area_m2, disposition,
    and the geocoded coordinates (lat = lng for confidence purposes).
    """
    relevant = [
        confidence_per_field.get("area_m2"),
        confidence_per_field.get("disposition"),
        confidence_per_field.get("lat"),
    ]
    if any(c == "low" for c in relevant):
        return "low"
    if any(c == "medium" for c in relevant):
        return "medium"
    if all(c == "high" for c in relevant):
        return "high"
    return "low"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_lookup(
    conn: "psycopg.Connection",
    url: str,
) -> dict[str, Any] | None:
    sql = (
        "SELECT source_kind, parse_result, source_html, cost_usd, "
        "parsed_at, expires_at "
        "FROM parsed_url_cache "
        "WHERE url_hash = %s AND expires_at > now() "
        "ORDER BY parsed_at DESC LIMIT 1"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (url_hash(url),))
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "source_kind": row[0],
        "parse_result": row[1],
        "source_html": row[2],
        "cost_usd": float(row[3]) if row[3] is not None else None,
        "parsed_at": row[4],
        "expires_at": row[5],
    }


def _cache_store(
    conn: "psycopg.Connection",
    *,
    url: str,
    source_kind: str,
    parse_result: dict[str, Any],
    source_html: str,
    cost_usd: float,
) -> None:
    sql = (
        "INSERT INTO parsed_url_cache "
        "(url_hash, source_url, source_kind, parse_result, "
        " source_html, cost_usd, parsed_at, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, now(), now() + interval '7 days') "
        "ON CONFLICT (url_hash) DO UPDATE SET "
        " source_url = EXCLUDED.source_url, "
        " source_kind = EXCLUDED.source_kind, "
        " parse_result = EXCLUDED.parse_result, "
        " source_html = EXCLUDED.source_html, "
        " cost_usd = EXCLUDED.cost_usd, "
        " parsed_at = now(), "
        " expires_at = now() + interval '7 days'"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            sql,
            (
                url_hash(url),
                canonical_url(url),
                source_kind,
                _Jsonb(parse_result),
                source_html,
                cost_usd,
            ),
        )


def _result_from_cache(
    cached: dict[str, Any],
    *,
    kind: SourceKind,
    source_url: str,
) -> ParseResult:
    payload = cached["parse_result"] or {}
    spec = payload.get("spec") or {}
    extraction = payload.get("extraction")
    confidence_per_field = payload.get("parse_confidence_per_field")
    parsed_at = cached.get("parsed_at")
    return ParseResult(
        spec=dict(spec),
        source_kind=kind,
        parse_confidence=str(payload.get("parse_confidence") or "low"),
        parse_confidence_per_field=(
            dict(confidence_per_field)
            if isinstance(confidence_per_field, dict)
            else None
        ),
        source_html=cached.get("source_html"),
        from_cache=True,
        cost_usd=None,
        warnings=list(payload.get("warnings") or []),
        sreality_id=None,
        source_url=source_url,
        full_extraction=(
            dict(extraction) if isinstance(extraction, dict) else None
        ),
        fetched_at=parsed_at.isoformat() if hasattr(parsed_at, "isoformat") else parsed_at,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_parser(kind: SourceKind) -> ModuleType:
    name = _PARSER_MODULE_BY_KIND.get(kind)
    if name is None:
        raise ParseError(f"no parser configured for kind={kind!r}")
    return import_module(name)


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ".").strip())
        except ValueError:
            return None
    return None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str):
        v = value.strip()
        return v or None
    return None
