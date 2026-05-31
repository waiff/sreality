-- LLM enrichment of typed attributes from a listing's free-text description.
-- Description-only portals (bazos today) carry no structured floor / amenities
-- / condition / building_type — only price, area, disposition, coords, text.
-- A cheap model extracts those typed fields from the description; this cache
-- mirrors the extraction (keyed (sreality_id, snapshot_id) so a new snapshot
-- auto-invalidates) and the enricher fills ONLY currently-NULL listings columns
-- from it. Same write-allowed-cache discipline as listing_condition_scores.
CREATE TABLE IF NOT EXISTS listing_description_enrichments (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sreality_id  bigint NOT NULL,
    snapshot_id  bigint NOT NULL,
    extracted    jsonb  NOT NULL,            -- the record_listing {value,confidence} envelope
    filled       jsonb  NOT NULL DEFAULT '{}'::jsonb,  -- columns actually written to listings
    model        text   NOT NULL,
    llm_call_id  bigint,
    cost_usd     numeric,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (sreality_id, snapshot_id)
);

CREATE INDEX IF NOT EXISTS listing_description_enrichments_sid_idx
    ON listing_description_enrichments (sreality_id);

-- Widen the llm_calls.called_for CHECK to allow the new tag (only adds a
-- value; existing rows stay valid).
ALTER TABLE llm_calls DROP CONSTRAINT IF EXISTS llm_calls_called_for_check;
ALTER TABLE llm_calls ADD CONSTRAINT llm_calls_called_for_check CHECK (
    called_for = ANY (ARRAY[
        'parse_url', 'summarize_listing', 'compare_listing_images',
        'agent_estimation', 'extract_building_units', 'read_floor_plan',
        'refine_skill', 'discover_condition_markers', 'score_listing_condition',
        'summarize_region_dispositions', 'enrich_listing_description'
    ]::text[])
);
