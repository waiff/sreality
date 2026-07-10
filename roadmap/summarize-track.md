> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

## Summarize track (parallel)

LLM-derived natural-language summaries over the data the user is
already viewing. Distinct from the `browse-*` track (UI primitives
and navigation) and from the future `agent-*` track (multi-tool
reasoning). Powered by the Claude API via the FastAPI service —
the browser never holds an Anthropic key.

### summarize-1: Annotated distribution charts (done)
- One- to two-sentence natural-language annotation per per-disposition
  Kč/m² box plot in Browse > Stats (the box plots browse-2 shipped
  under the `DispositionBoxPlots` component). Generated server-side by
  `toolkit.region_annotations.summarize_region_dispositions` from the
  same `ppm2_box` payload that drives the chart, with the cohort-wide
  percentiles + all per-disposition box stats as cross-disposition
  context. Annotations are facts about the distribution — never a price
  recommendation (toolkit rule #1).
- Cached per `(region, calendar day)` in `region_disposition_annotations`
  (migration 104) so repeat browser sessions don't re-bill: the first
  viewer of a region today pays for the Claude call, everyone else hits
  the cache; the next day's first view regenerates. `region_key` is the
  SPA's deterministic serialization of the active filter set.
- Wired the same LLM-tool way as `summarize_listing`: operator-tunable
  system prompt + model in `app_settings`
  (`llm_region_annotation_system_prompt` / `_model`, default
  `claude-sonnet-4-5`), audited in `llm_calls` under
  `called_for='summarize_region_dispositions'`, exposed via the
  bearer-gated `POST /tools/summarize_region_dispositions`. The SPA
  renders the annotations under the box plots; the browser never holds
  an Anthropic key.
- Was the track entry-point for Phase 6's `summarize_listing` and
  `compare_listing_images` — all three now sit in the same family.

