#!/usr/bin/env python3
"""Fetch real sreality v1 API fixtures (search + detail) and scrub PII.

The live /api/v1/estates API returns snake_case JSON (id key `hash_id`,
detail wrapped under `result`). This captures one real search response and
one real detail response so the parser tests guard the true shape rather
than the HAR-derived camelCase guess. Emails and phone numbers in the
payloads are anonymized before the fixtures are committed.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import requests

BASE = "https://www.sreality.cz/api/v1/estates"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Czech phone: optional +420, then 9 digits in 3-3-3 grouping. Guarded by
# digit boundaries so it can't bite into a longer numeric string.
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+420[\s ]?)?\d{3}[\s ]?\d{3}[\s ]?\d{3}(?!\d)"
)


def _anonymize(value: Any) -> Any:
    if isinstance(value, str):
        v = _EMAIL_RE.sub("agent@example.cz", value)
        v = _PHONE_RE.sub("+420 XXX XXX XXX", v)
        return v
    if isinstance(value, list):
        return [_anonymize(x) for x in value]
    if isinstance(value, dict):
        return {k: _anonymize(x) for k, x in value.items()}
    return value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default="tests/fixtures")
    ap.add_argument("--category-main", type=int, default=1)
    ap.add_argument("--category-type", type=int, default=1)
    ap.add_argument("--search-limit", type=int, default=3)
    ap.add_argument(
        "--detail-id",
        type=int,
        default=None,
        help="Override the detail hash_id (default: first search result).",
    )
    args = ap.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    search_params = {
        "category_main_cb": args.category_main,
        "category_type_cb": args.category_type,
        "locality_country_id": 112,
        "limit": args.search_limit,
        "offset": 0,
    }
    sr = requests.get(
        f"{BASE}/search", params=search_params, headers=HEADERS, timeout=30
    )
    sr.raise_for_status()
    search = sr.json()
    results = search.get("results") or []
    if not results:
        raise SystemExit("search returned no results")

    detail_id = args.detail_id or results[0].get("hash_id") or results[0].get("id")
    if not detail_id:
        raise SystemExit("could not determine a detail id (hash_id) from search")

    dr = requests.get(f"{BASE}/{detail_id}", headers=HEADERS, timeout=30)
    dr.raise_for_status()
    detail = dr.json()
    if isinstance(detail, dict) and isinstance(detail.get("result"), dict):
        estate = detail["result"]
    else:
        estate = detail

    (out / "sample_search.json").write_text(
        json.dumps(_anonymize(search), ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    (out / "sample_listing.json").write_text(
        json.dumps(_anonymize(estate), ensure_ascii=False, indent=2) + "\n", "utf-8"
    )

    print(f"search results={len(results)} detail_id={detail_id}")
    if isinstance(estate, dict):
        print(f"detail result keys: {sorted(estate.keys())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
