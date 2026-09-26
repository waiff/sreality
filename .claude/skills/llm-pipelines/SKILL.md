---
name: llm-pipelines
description: Use when working on any LLM-backed path — the on-demand URL parser (source_dispatcher + per-source parsers), the cached analytical vision/text tools (summarize_listing, compare_listing_images, score_listing_condition, extract_building_units, read_floor_plan, discover_condition_markers, summarize_region_dispositions), the unified vision downscaling tiers, or the MF Cenová mapa nájemného reference-rent calc/ingest and gross-yield filter. Triggers on: parse_url, source_parsers, app_settings prompt/model, llm_calls, called_for, vision, max_edge downscale, reference_rent, rent map, mf_gross_yield, Gemini, provider, tool schema, additionalProperties.
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
  snapshot; `region_hash` folds in the `basis` when one is sent, so a capital and a monthly
  reading of the same cohort can never share a cached sentence. Powers the `summarize-1`
  annotated-charts feature; FACTS not opinions (toolkit rule #1) — it describes the
  distribution, never recommends a price.
  **`basis` is required in practice** (migration 425's vocabulary:
  `sale_capital_czk_m2` / `rent_monthly_czk_m2` / `land_capital_czk_m2`): the prompt payload
  names the unit AND the period from it, and without one the model narrates bare Kč/m² and
  cannot tell a ~91 535 capital figure from a ~319 monthly one. Browse skips the call
  entirely for a cohort whose basis is mixed rather than sending an unlabelled payload.
- `enrich_listing_description` — the post-publication text lane (`toolkit/description_extraction.py`),
  rebuilt by field-capture W7 on the **realtime worker** (lane `text_extract`, constant 300 s, no
  flag / setting / env var). **A field is in scope only once its `Cell.gate.passed` is true** (R7,
  a measured ≥ 95 % panel — `scripts/bakeoff_text_extraction.py`, dispatch-only): a closed gate is
  not extracted, not billed and not written, and every gate ships closed, so the lane is live and
  free until the bake-off opens one. The declared set is bazos's eight prose-only columns. The
  cache `listing_description_enrichments` is keyed `(listing_id, text_hash, extractor_version)`
  (migration 552), `extractor_version` being `'<schema>:<open-gate hash>:<model>'` — a model swap
  re-attempts, a price-only snapshot never re-bills, and **opening a gate re-opens the corpus**, so
  open every field that cleared in ONE edit. Concurrency + the one pre-call budget guard come from
  `toolkit.vision_batch.run_batch`; the write is NULL-only (`coalesce`) with the `dirty_properties`
  enqueue in the same CTE and no snapshot; a failed call is cached with an attempt count and given
  up on after 5.
  `false` needs an explicit negation in the evidence quote, every value needs a quote verbatim in
  the description, and `floor` comes back as the advert's own words for `scraper/floor.py` to
  convert. Health: `verify_pipeline`'s `text_extraction_lag`, built from the lane's OWN selector
  (R8). `app_settings.enrichment_model` is the one switch. `docs/design/field-capture/PROGRAM.md`.

**Vision image downscaling is unified in `toolkit/vision_images.py` — one helper, two
tiers.** Every image→LLM call routes R2 bytes through `image_block(r2, key, max_edge)`
(download → Pillow downscale → base64) rather than hand-rolling base64 per call. Two
semantic constants pick the tier: `COMPARISON_MAX_EDGE = 768` (also `DEFAULT_MAX_EDGE`) for
photo comparison — today `compare_listing_images` — where sub-megapixel is ample and,
crucially, *below* Anthropic's ~1.15 MP resize cap, so it actually cuts vision tokens to ~⅓
(the cost lever); and `DOCUMENT_MAX_EDGE = 1568` for reads where fine text/markers matter
(condition scoring/markers, building-extraction listing photos) — that *is* Anthropic's own
cap, so the model sees the same pixels it would have anyway (quality-neutral; just less
upload + no 200k prompt-assembly blowups). Anthropic bills tokens on the post-resize size,
so anything ≥ the cap costs the *same* tokens — the saving only appears below it. **Operator
attachments (`read_floor_plan`, building-extraction custom attachments) are deliberately
NOT routed through this** — they carry arbitrary mime (PDF/PNG line-art) where the JPEG
re-encode would corrupt PDFs and degrade crisp text; they keep their full-fidelity base64
path. `tests/test_vision_downscale_tiers.py` pins which builder passes which tier — keep a
new vision caller in that test rather than picking an edge by hand.

**The dedup vision tools are gone.** `classify_listing_images`, `compare_listings_visually`,
`compare_listing_site_plans`, `compare_listing_floor_plans` and the `validate_vision_models`
A/B harness were deleted wholesale with the legacy dedup decision engine (2026-08 cutoff —
architectural rule #15, `docs/design/new-dedup/CUTOFF.md`), along with their `app_settings`
prompt/model keys and their `llm_calls.called_for` values. Nothing routes vision at dedup any
more; the `CalledFor` literal in `api/llm_client.py` is the live list. The rebuilt engine's
vision level (manual batches, model routing, its own prompt contract) is Wave 6 of
`docs/design/new-dedup/PROGRAM.md` — build it there, don't restore the old tools. Their paid
verdict caches (`listing_visual_matches`, `listing_site_plan_matches`,
`listing_floor_plan_matches`, `image_room_classifications`) are kept **frozen**: no writes, and
the new design never reads them. `listing_image_comparisons` is unrelated — it is the
agent-facing `compare_listing_images` cache and stays live.

## Secondary rent reference (MF Cenová mapa nájemného)

The Ministry of Finance's quarterly *Cenová mapa nájemného*: a hedonic reference rent per
territory (KÚ or obec) × VK1–4, standard + novostavba columns, plus per-amenity Kč/m²
adjustments. A secondary figure shown ALONGSIDE the comparables estimate — never overrides it.

- **ONE read-time SQL measure (migration 565):** `mf_reference(facts, obec_kod, katastr_kod,
  country_status)` → `mf_reference_rent_czk`, `mf_gross_yield_pct`, `mf_reference_rent` (detail
  jsonb). LANGUAGE sql, inlined into the caller's LEFT JOIN LATERAL (no `SET`/STRICT — CI plan
  test). It reads ONLY the matview `rent_map_cells` (latest revision as `ku`/`obec`/`town` cells
  with the adjustments as columns), never geometry. Nothing WRITES MF any more (the hourly job
  and the merge/detach recompute are gone); estimations and `/estimate_yield` call the measure.
- **Serving, until PR-B:** `browse_projection` / `properties_public` / `listing_feed_public`
  still read the stored `properties.mf_*` / `listings.mf_*` — writer-less, frozen at their last
  write. PR-B's ONE view swap calls the measure with `ll.katastr_kod` (the portal lane takes the
  PROPERTY's yield from `browse_list`, Q8 b); swapping with a NULL KÚ would put every flat in a
  KÚ-priced town (79 %) on a range and out of the yield filter. PR-F drops the stored columns.
- **Rules:** flats only (else no row). VK = leading integer of the disposition clamped 1..4;
  novostavba = `condition = 'novostavba'` (NULL → older column, adjustments kept); rent =
  round((base + adjustments) × area); yield only for `prodej` with price ≥ 100 000, else the
  rent stands without one. Cell: a bound KÚ's cell → the obec's cell → (KÚ bound) `no_rent_cell`
  → the town row: one price across every current KÚ → value (`territory.basis='town_uniform'`),
  else the town's published range + note (`territory_coarse`). Until `listing_location.katastr_kod`
  lands every caller passes NULL, i.e. "location known to town level".
- **The result's SHAPE is the render contract:** value (`monthly_rent_czk`) | range
  (`range{per_m2_min/max, rent_min/max_czk, yield_min/max_pct?}` + `note`) | note (`status` +
  `note`) | none (NULL). Six codes (`ok`, `territory_coarse`, `no_rent_cell`, `not_in_cz`,
  `location_unknown`, `inputs_missing`); their Czech notes exist ONLY in migration 565 — clients
  render `detail.note`, never their own text (rail: `tests/test_mf_reference.py`), and decide the
  shape through ONE rule, `frontend/src/lib/mfReference.ts` (`mfShape`, shared with the
  extension). Ranges show on detail surfaces only; Browse/map/kanban and the yield filter see
  values only.
- **Estimations:** rent runs store `estimation_runs.reference_rent` (migration 131) from
  `toolkit.rent_map.compute_reference_rent` — a thin wrapper running the measure on the
  service-role connection, stamped `engine: 'mf_sql_v1'`. Our advert → its property's golden
  facts + stored codes (`_SUBJECT_MF_FACTS_SQL`, the views' lateral inputs); a URL-parsed or
  typed subject → obec from `api.maps.containing_obec_kod`, KÚ NULL. Stored runs keep their
  frozen breakdown (rule #12).
- **Store + ingest:** `rent_map_revisions` (`file_sha256` UNIQUE → a re-fetch no-ops) +
  `rent_map_values` + `rent_map_adjustments` (migration 132, history kept).
  `api.rent_map.insert_revision` parses (stdlib `zipfile`+`xml.etree`), COPYs, REFRESHes
  `rent_map_choropleth` (the Browse map layer, VK1–4 + Kraje overlay) and — CONCURRENTLY —
  `rent_map_cells`, stamping both in `derived_artifacts`; every caller of the measure sees the
  new revision at that REFRESH, nothing to recompute. Fed by the monthly `fetch_rent_map.yml` and
  the Settings upload / "Fetch latest now" (`POST /admin/rent-map/*`). Sheet sanity check:
  Litoměřice older 3+1, 68 m², +výtah +balkon +garáž → 291 Kč/m² → 19 788 Kč.
