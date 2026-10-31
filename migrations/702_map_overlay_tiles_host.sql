-- 702_map_overlay_tiles_host.sql -- the overlay tiles' registry row follows its job to the runner (#2025 PR-3).
--
-- #2025 PR-2 moved the `map_overlays` job to the job runner and deleted ingest_map_overlays.yml. Migration 600's row still
-- read that workflow's host and cron, so Health gave the cell GitHub's 18 h delivery slack and would name the wrong host in
-- a stale message. The cadence names the registry entry, as 683 did for `rent_map_cells`. Additive (two text values);
-- idempotent. A revert of PR-2 leaves this row reading `job-runner`: 18 h stricter against a 100-day budget.
-- tests/test_derived_artifacts_stamping.py::test_no_artifact_of_a_runner_job_is_hosted_on_github is the rail.

update public.derived_artifacts
   set host    = 'job-runner',
       cadence = 'quarterly (scraper/jobs.py map_overlays)'
 where name = 'map_overlay_tiles'
   and (host, cadence) is distinct from ('job-runner', 'quarterly (scraper/jobs.py map_overlays)');
