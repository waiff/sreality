# Summarize track

LLM-derived natural-language summaries over the data the user is
already viewing. Distinct from the `browse-*` track (UI primitives
and navigation) and from the future `agent-*` track (multi-tool
reasoning). Powered by the Claude API via the FastAPI service —
the browser never holds an Anthropic key.

## Next

### summarize-1: Annotated distribution charts (proposed)
- One- to two-sentence natural-language annotation per box plot in
  browse-2's Region page (e.g. "2+kk listings in this area cluster
  tightly around 480 Kč/m²; the long upper whisker reflects six
  premium-finish flats above 800 Kč/m²"). Generated server-side from
  the same `ppm2_box` payload that drives the chart, plus a small
  cohort sample for context.
- Cached per-region per-day so identical browser sessions don't
  re-bill the API.
- Track entry-point for Phase 6's `summarize_listing` and
  `compare_listing_images` — both fit naturally in the same family.
