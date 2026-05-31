-- Data-quality observability: a live per-source completeness matrix + a
-- thin snapshot table so the operator can track field-population drift over
-- time. Long format (one row per source × field) so adding a field later is
-- a one-line view edit, never a schema change. Internal/ops only — not a
-- *_public view, not anon-granted.
--
-- Re-run live:   SELECT * FROM data_quality_by_source ORDER BY source, field;
-- Append a point-in-time snapshot (cron or manual):
--   INSERT INTO data_quality_snapshots (source, field, n_active, n_populated, pct_populated)
--   SELECT source, field, n_active, n_populated, pct_populated FROM data_quality_by_source;

CREATE OR REPLACE VIEW data_quality_by_source AS
SELECT
    l.source,
    v.field,
    count(*)                                            AS n_active,
    count(*) FILTER (WHERE v.present)                   AS n_populated,
    round(100.0 * count(*) FILTER (WHERE v.present) / count(*), 1) AS pct_populated
FROM listings l
CROSS JOIN LATERAL (VALUES
    ('price_czk',                 l.price_czk IS NOT NULL),
    ('area_m2',                   l.area_m2 IS NOT NULL),
    ('disposition',               l.disposition IS NOT NULL),
    ('category_main',             l.category_main IS NOT NULL),
    ('category_type',             l.category_type IS NOT NULL),
    ('geom',                      l.geom IS NOT NULL),
    ('locality',                  l.locality IS NOT NULL),
    ('district',                  l.district IS NOT NULL),
    ('locality_district_id',      l.locality_district_id IS NOT NULL),
    ('locality_region_id',        l.locality_region_id IS NOT NULL),
    ('street',                    l.street IS NOT NULL),
    ('house_number',              l.house_number IS NOT NULL),
    ('floor',                     l.floor IS NOT NULL),
    ('total_floors',              l.total_floors IS NOT NULL),
    ('has_balcony',               l.has_balcony IS NOT NULL),
    ('has_lift',                  l.has_lift IS NOT NULL),
    ('has_parking',               l.has_parking IS NOT NULL),
    ('terrace',                   l.terrace IS NOT NULL),
    ('cellar',                    l.cellar IS NOT NULL),
    ('garage',                    l.garage IS NOT NULL),
    ('parking_lots',              l.parking_lots IS NOT NULL),
    ('building_type',             l.building_type IS NOT NULL),
    ('condition',                 l.condition IS NOT NULL),
    ('energy_rating',             l.energy_rating IS NOT NULL),
    ('furnished',                 l.furnished IS NOT NULL),
    ('ownership',                 l.ownership IS NOT NULL),
    ('building_condition_level',  l.building_condition_level IS NOT NULL),
    ('apartment_condition_level', l.apartment_condition_level IS NOT NULL),
    ('property_grouped',          l.property_id IS NOT NULL)
) AS v(field, present)
WHERE l.is_active
GROUP BY l.source, v.field;

CREATE TABLE IF NOT EXISTS data_quality_snapshots (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    captured_at   timestamptz NOT NULL DEFAULT now(),
    source        text        NOT NULL,
    field         text        NOT NULL,
    n_active      integer     NOT NULL,
    n_populated   integer     NOT NULL,
    pct_populated numeric     NOT NULL
);

CREATE INDEX IF NOT EXISTS data_quality_snapshots_captured_idx
    ON data_quality_snapshots (captured_at);

-- Seed a baseline so drift has a starting point from the day this lands.
INSERT INTO data_quality_snapshots (source, field, n_active, n_populated, pct_populated)
SELECT source, field, n_active, n_populated, pct_populated FROM data_quality_by_source;
