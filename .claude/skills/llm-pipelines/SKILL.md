---
name: llm-pipelines
description: Use when working on any LLM-backed path — the on-demand URL parser (source_dispatcher + per-source parsers), the cached analytical vision/text tools (summarize_listing, compare_listing_images, classify/compare for dedup, score_listing_condition, extract_building_units, read_floor_plan, discover_condition_markers, summarize_region_dispositions), the unified vision downscaling tiers, or the MF Cenová mapa nájemného reference-rent calc/ingest and gross-yield filter. Triggers on: parse_url, source_parsers, app_settings prompt/model, llm_calls, vision, max_edge downscale, reference_rent, rent map, mf_gross_yield.
---

# LLM pipelines

The LLM-backed parsing and analysis paths, and the MF rent reference. Each caches its
result locally and auto-invalidates; each is a write-allowed toolkit exception (see the
`toolkit-api` skill, rule #5). System prompts and model IDs are operator-tunable via
`app_settings`.

## LLM-backed parsing

`scraper.source_dispatcher.parse_listing_url` is the single entry point for any listing URL
(sreality or otherwise). It classifies the URL by domain and routes to either the
deterministic sreality flow (`scraper.url_parser`, unchanged) or an LLM-driven per-source
parser under `scraper/source_parsers/`. Today's allowlist is `bezrealitky`, `idnes_reality`
(reality.idnes.cz), and `remax` (remax-czech.cz); everything else falls through to a
best-effort generic parser that always reports `parse_confidence='best_effort'`. (Note: bazos
is ingested by its own crawler into `listings`, not through this on-demand URL parser.)

The LLM path:
1. Cache check against `parsed_url_cache`. Key is sha256 of the canonicalised URL (lowercase
   scheme/host, no query, no trailing slash). Hit → return cached spec, no LLM, no cost.
2. Fetch HTML, send to Claude with the system prompt from `app_settings.llm_parse_system_prompt`
   and the per-source user prompt from `scraper.source_parsers.<source>`. The model is
   `app_settings.llm_parse_model` (default `claude-sonnet-4-5`, from `api/llm_client.py`).
3. The LLM is required to invoke `record_listing` exactly once with every field in a
   `{value, confidence}` envelope. Any deviation raises `ParseError` and surfaces as a 502
   from `/estimations/preview` or a `failed` row from `POST /estimations`.
4. If the page didn't yield lat/lng, geocode the locality string via Mapy.cz
   (`scraper.geocoding`). The geocode confidence rolls into
   `parse_confidence_per_field['lat'/'lng']`.
5. Store the full extraction + spec + warnings in `parsed_url_cache` with a 7-day TTL.

Operator-tunable parser behaviour lives in `app_settings` (system prompt, model name) — edits
take effect on the next preview/estimation, no deploy. Every prior value is preserved in
`app_settings_history` (migration 020 trigger). Every call is recorded in `llm_calls` with
token counts (incl. cache-read/write splits), USD cost, duration, and the optional
`estimation_run_id`; `called_for='parse_url'`.

## LLM-backed analysis

Several analytical toolkit functions reach for Claude. Each caches its result locally and
auto-invalidates, logs to `llm_calls` under a distinct `called_for`, and is a write-allowed
exception per Toolkit rule #5. System prompts and model IDs are operator-tunable via
`app_settings` (model defaults `claude-sonnet-4-5`).

- `summarize_listing` (`toolkit/summaries.py`, migration 027, cache `listing_summaries`) —
  structured Czech summary of one snapshot: `headline`, `key_highlights`, `concerns`,
  `condition_assessment`, `target_audience`, plus location/building/apartment summaries.
- `compare_listing_images` (`toolkit/image_similarity.py`, migration 027, cache
  `listing_image_comparisons`) — Claude-vision pairwise comparison across six fixed dimensions
  (`exterior`, `kitchen`, `windows_and_light`, `floor_finish`, `lighting`, `styling`) plus an
  `overall_similarity`. Image bytes pulled from R2 server-side via boto3 and base64-encoded.
  Vision is materially more expensive than text (~$0.05/pair) — the cache matters most here.
- `extract_building_units` (`toolkit/building_extraction.py`, migration 036, cache
  `building_unit_extractions`) — structural decomposition of a multi-unit building into a unit
  proposal; the vision extractor behind the building-paste flow.
- `read_floor_plan` (`toolkit/floor_plan.py`, migration 044, cache
  `building_attachment_analyses`, keyed on `(attachment_id, model)`) — vision analysis of one
  operator-supplied attachment (floor plan, drawing, photo).
- `discover_condition_markers` (`toolkit/condition_markers.py`, migration 064, cache
  `listing_marker_extractions`) — mines Czech technical-state phrases ("zateplená budova", "po
  kompletní rekonstrukci") to feed the condition-scoring marker dictionary.
- `score_listing_condition` (`toolkit/condition_scoring.py`, migration 072, cache
  `listing_condition_scores`) — two-axis building/apartment condition levels (1..5) from the
  curated rubric + marker dictionary. See architectural rule #14.
- `summarize_region_dispositions` (`toolkit/region_annotations.py`, migration 104, cache
  `region_disposition_annotations`) — a one-to-two-sentence factual annotation per
  per-disposition Kč/m² box plot in Browse > Stats, from the same `ppm2_box` payload that
  drives the chart. Cached per `(region_hash, day)` — invalidates by calendar day, not by
  snapshot. Powers the `summarize-1` annotated-charts feature; FACTS not opinions (toolkit
  rule #1) — it describes the distribution, never recommends a price.

**Vision image downscaling is unified in `toolkit/vision_images.py` — one helper, two
tiers.** Every image→LLM call routes R2 bytes through `image_block(r2, key, max_edge)`
(download → Pillow downscale → base64) rather than hand-rolling base64 per call. Two
semantic constants pick the tier: `COMPARISON_MAX_EDGE = 768` for photo comparison /
classification (`classify_listing_images`, `compare_listings_visually`,
`compare_listing_images`) — sub-megapixel is ample and, crucially, *below* Anthropic's
~1.15 MP resize cap, so it actually cuts vision tokens to ~⅓ (the cost lever); and
`DOCUMENT_MAX_EDGE = 1568` for reads where fine text/markers matter (site-plan compare,
condition scoring/markers, building-extraction listing photos) — that *is* Anthropic's own
cap, so the model sees the same pixels it would have anyway (quality-neutral; just less
upload + no 200k prompt-assembly blowups). Anthropic bills tokens on the post-resize size,
so anything ≥ the cap costs the *same* tokens — the saving only appears below it. **Operator
attachments (`read_floor_plan`, building-extraction custom attachments) are deliberately
NOT routed through this** — they carry arbitrary mime (PDF/PNG line-art) where the JPEG
re-encode would corrupt PDFs and degrade crisp text; they keep their full-fidelity base64
path. The forensic `compare_listings_visually` is the one call whose verdict auto-merges, so
its tier is gated: `scripts/validate_vision_models.py` (workflow
`validate_vision_models.yml`) A/Bs a candidate `(model, max_edge)` against every historical
`High` verdict and only a green run authorizes flipping its model to Haiku / its edge to 768.

## Secondary rent reference (MF Cenová mapa nájemného)

Every **rental** estimate carries a second, independent reference figure from the Czech
Ministry of Finance's quarterly *Cenová mapa nájemného* (a hedonic-model reference rent per
territory), shown ALONGSIDE the comparables-based primary estimate — it never overrides it.
Stored on `estimation_runs.reference_rent jsonb` (migration 131; NULL = sale run / territory
miss / no revision ingested yet). Surfaced on Estimation Detail, the Chrome-extension panel,
the `/estimations` + `/estimate_yield` API payloads, and as a Browse map choropleth layer
(VK1–VK4 selectable, optional Kraje overlay — reproduces the official MF map).

- **Source store (migration 132, history-tracked):** `rent_map_revisions` (one row per ingested
  XLSX; `file_sha256` UNIQUE so re-fetching an unchanged file no-ops) + long-form
  `rent_map_values` (per RÚIAN territory × VK1–4, standard + novostavba rent) +
  `rent_map_adjustments` (per-VK amenity Kč/m², older + novostavba tables). The `*_public` views
  are latest-revision-wins (the curated-cities pattern, rule #17). The Browse map reads the
  materialized `rent_map_choropleth` (polygons + the four VK rents, REFRESHed on each ingest) so
  the anon read is a precomputed scan under the 3 s statement timeout.
- **The join:** the spreadsheet's `Kód obce` IS the ČÚZK/RÚIAN code = `admin_boundaries.id`
  (verified: all 7,630 codes match — 1,582 `ku` + 6,048 `obec` — with zero id-space collision).
  The calc resolves the subject's lat/lng to its containing `ku`/`obec` polygon (PIP, same
  pattern as `toolkit/comparables`) and looks up the rent by that code.
- **The calc:** `toolkit.rent_map.compute_reference_rent` is **READ-ONLY — NOT a new toolkit
  write exception (rule #5)**: base reference rent (VK from the disposition's leading room count:
  1→VK1 … ≥4→VK4) + per-amenity adjustments (balkón/terasa/vybavenost/garáž/výtah, + *jiný
  konstrukční materiál* for new builds), × area. New builds (`condition='novostavba'`) use the
  novostavba reference column + novostavba adjustment table; everything else uses the older-flat
  column + older adjustments. Best-effort: any miss → NULL, never fails an estimation run. It
  reproduces the MF sheet's own worked example exactly (Litoměřice older 3+1, 68 m², +výtah
  +balkon +garáž → 291 Kč/m² → 19 788 Kč).
- **Ingest (write path, out of the read-only toolkit):** `api.rent_map.ingest_bytes` →
  `insert_revision` (parse → revision INSERT → COPY values/adjustments → REFRESH the matview).
  Refreshed two ways: the monthly `fetch_rent_map.yml` workflow (`scripts.fetch_rent_map`, scrapes
  the current XLSX off the MF *infografika* page — MF updates 4×/year) AND a manual `.xlsx` upload
  / "Fetch latest now" from the Settings page (`POST /admin/rent-map/*`). The XLSX is parsed with
  stdlib `zipfile`+`xml.etree` (no `openpyxl`). No new secrets — uses `SUPABASE_DB_URL`.
- **MF gross yield Browse filter (migration 133).** Every **sale apartment** carries a derived
  `listings.mf_gross_yield_pct` (= MF reference monthly rent × 12 / asking price × 100) +
  `mf_reference_rent_czk`, computed set-based by the `recompute_mf_gross_yields()` SQL function
  (PIP-resolve territory → rent-map join → ÷ price). NULL where not computable (non-apartment,
  rental, no territory) **and** where the asking price is implausible for a sale (`< 100 000` CZK —
  excludes "cena v RK"/placeholder + rent-magnitude prices mis-tagged `prodej`, which would
  otherwise yield absurd %; genuine high-yield deals are preserved). The function runs **hourly**
  (`recompute_mf_yields.yml` → `scripts.recompute_mf_yields`) and **after each rent-map ingest**
  (inside `scripts.fetch_rent_map`); cheap + idempotent (`is distinct from` guard). Exposed on
  `listings_public` / `properties_public` and filterable in Browse **and** Watchdog via the
  `min/max_mf_gross_yield_pct` registry filter (`_UI_AGENDAS`, float range slider) — Map/Table
  auto-dispatch `.gte/.lte` on `properties_public`, the Stats RPC `browse_stats_properties` gained
  two params, and the Watchdog matcher + `_shared_filter_where`/`ComparableFilters` carry it for
  saved alerts. Real-data distribution sanity: median ~3.5%, p99 ~10%. The same recompute pass also
  stores the full formula **breakdown** as `listings.mf_reference_rent jsonb` (migration 134: territory,
  VK, novostavba flag, `base_per_m2`, per-amenity `adjustments[]`, `total_per_m2`, area,
  `monthly_rent_czk`) — exposed on `listings_public` and rendered on the sale **listing-detail header**
  so the operator sees the exact numbers behind the stored rent/yield (always consistent — one pass
  writes all three columns).

