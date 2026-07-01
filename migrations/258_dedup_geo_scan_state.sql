-- 258_dedup_geo_scan_state.sql
-- Progressive geo (single-dwelling) dedup DISCOVERY cursor — a single-row table
-- holding the highest obec_id fully scanned by the geo discovery pass.
--
-- WHY: the geo path (rule #15 — houses/land/commercial matched by geo-proximity,
-- since they have no disposition) was built as ONE paid full scan (`--geo-only`,
-- cron 0 3,9,15,21). Its forensic FACADE compares are slow, so the run burns its
-- --max-seconds on the FIRST obce every time (obec_id ASC — 304 of 363 queued geo
-- candidates were Olomouc, obec_id 500496, near the front of the 500011..599999 code
-- range) and re-STARTS from obec_id order every run, so it never advanced past the
-- front and never reached Prague (obec_id 554782, ~16.3k single-dwelling listings —
-- ~9.7k un-surfaced co-located cross-source pairs). The geo-eligible universe
-- (~194k listings, 5,690 obce) is too big for one monolithic scan.
--
-- FIX (mirror the street path's free-discovery + bounded-paid-drain shape): the geo
-- DISCOVERY run is now `--free` (fast, no vision bottleneck — it only queues the
-- co-located pairs) and walks the market in obec-cursor WINDOWS. Each discovery run
-- scans a budget-sized window of WHOLE obce beyond this cursor (whole-obec so geo
-- cells — which are obec-bounded — stay intact), queues every co-located pair, then
-- ADVANCES the cursor to the window's max obec; at the market end it WRAPS to 0. A
-- separate bounded PAID geo candidate-drain (`--geo-only --candidates
-- --max-vision-calls N`) works the queued tier='geo' candidates O(queue) and
-- auto-merges the confident facades. So successive discovery runs cover
-- Olomouc → … → Prague → wrap instead of re-scanning the front forever.
--
-- Single row (id = 1, enforced by the CHECK); cursor_obec_id starts at 0 so the
-- first window begins at the smallest obec (obec ids are positive RÚIAN codes).
CREATE TABLE IF NOT EXISTS dedup_geo_scan_state (
    id             smallint    PRIMARY KEY DEFAULT 1,
    cursor_obec_id bigint      NOT NULL DEFAULT 0,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT dedup_geo_scan_state_singleton CHECK (id = 1)
);

INSERT INTO dedup_geo_scan_state (id, cursor_obec_id)
VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;

COMMENT ON TABLE dedup_geo_scan_state IS
  'Single-row cursor for progressive geo (single-dwelling) dedup DISCOVERY: the highest '
  'obec_id fully scanned. Each --geo-only --free discovery run scans a budget-sized window '
  '(dedup_geo_scan_budget) of whole obce beyond this cursor, queues the co-located pairs, '
  'and advances the cursor; wraps to 0 at the market end. See architectural rule #15 (geo path).';
