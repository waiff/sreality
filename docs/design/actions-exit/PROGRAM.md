# Actions exit: make the repo private, run recurring work where it is billed by use

Status: APPROVED 2026-10-25 (rulings R1–R27 below); building: W0 and W1 PRs landing, W2's schema (#2042) and
registry (#2023) landed, the runner (#2024) landed, its job cells and form (#2026) landed, its service and the move of its first three jobs (#2025) landed, their 7-day gate #2123 running. Audit: 84 workflow files, 14 days of run data, two review panels (42 agents on the audit and first design; 20 agents on the from-scratch
design: four architects, three judges, twelve refuters). Per-file verdicts: [dispositions.md](dispositions.md).
Decision record: [ADR 0001](../../adr/0001-scheduled-jobs-on-a-railway-job-runner.md).

## 1. Short answer

The repo goes private, after the recurring jobs have left GitHub Actions.

- Public repos get free Actions. A private one is billed per job-minute above the plan's pool. This repo
  runs about 112,000 billable minutes a month; private with nothing moved it would cost about $650/month.
- The repo moves into a company organisation (`propscout-cz`) on **GitHub Enterprise Cloud** ($21/month,
  50,000 included minutes). CI, the canary and the click-to-run tools stay on GitHub inside that pool.
- Every scheduled production job moves to one Railway service, the **job runner**. The flip happens when
  GitHub's billed run-rate has stayed under 50,000 minutes a month for seven days, so the GitHub bill is
  never more than the seat.
- Going private removes secret scanning and push protection on GitHub's side; gitleaks replaces them.
  Code cloned while public cannot be recalled (0 forks, 0 stars today).
- Steady cost after the sprint: Enterprise $21 + Railway $12–25. The 2026-06-11 "keep public" ruling rested
  on free Actions; this program supersedes it.

## 2. North star

**Every recurring production job has one definition in code (the `scraper/jobs.py` registry), one database
row that says when it is due and who holds it (`scheduled_jobs`), and one usage-billed host (the Railway
job runner). GitHub keeps only work that needs a branch, a human click, or independence from Railway. No
step of the move leaves a job without a working host or a Health cell.**

Tested against every wave: a change that adds a second scheduler, a second definition of a job, a routing
flag, a settings knob that means "stop", or a portal name in shared scheduler code is off the path.

## 3. What is true today (verified 2026-10-25)

| Fact | Evidence |
| --- | --- |
| 84 workflow files, 34 with a `schedule:`; 50 dispatch/CI only | `ls .github/workflows` |
| ~89,000 wall-clock run-minutes/month; ~112,000 billable (per-job round-up) | 5,535 runs, 11–24 Oct |
| CI: 24,544 billable min/month (test 16,119; migrations 4,139; frontend 2,969; docs-budget 1,082) | jobs API; 200 PRs + 797 commits to `main` in 14 days |
| Price-stats self-chain: 138 dispatches in 14 days (~14,700 min/month) on a weekly cron | `scrape_price_stats.yml` |
| GitHub fires sub-hourly crons about 6×/day; the worker drains 96–99 % of every portal's queue | run counts; `detail_queue_completions` |
| Worker memory: avg 7.4 GB, peak 21.5 of 22.9 GB (an autodedup pass is 11.4 GB) | Railway metrics, heartbeat |
| Merges redeploy the image ~8–11×/day; `drainingSeconds` 30; a cut walk leaves `ended_at NULL` and does not resume | `.railway/railway.ts` (`WORKER_DRAINING_SECONDS`), `portal_runner.py` |
| No SIGTERM handler in `portal_runner`, `portal` or any `*_main.py`; the worker cannot kill a pass (a thread) | code read |
| `location_jobs` (mig 384) is already a CAS lease table: holder, expiry, last start/success, outcome, failures | `location_data/resolver/lease.py` |
| Required checks: pytest, schema-and-sql, vitest, build, docs-budget; `strict=false` | branch protection API |
| 13.2 GB of Actions artifacts downloadable by any signed-in user | `/actions/artifacts` |
| Railway: Pro, builds free, usage $20/vCPU-mo + $10/GB-mo; cron services: 5-min minimum, skip-if-running, never terminated, mid-run deploy undocumented, `railway cron run` by hand | docs.railway.com |
| GitHub: Linux 2-core $0.006/min; Enterprise Cloud self-serve by card, metered per member, 50,000 min; Secret Protection $19/committer on Team or Enterprise; self-hosted runners free, fee postponed; private hosted runners are 2 vCPU / 7 GB | docs.github.com, github.com/pricing |
| Repo transfer keeps issues, PRs, secrets, webhooks, deploy keys; redirects old URLs; retires the old name; fine-grained PATs do not follow; Railway's GitHub App must be reinstalled on the organisation | docs.github.com, Railway forum |

## 4. Target architecture

### 4.1 Where each class runs (decided by the shape of the job, never job by job)

| Class | Host after | Why |
| --- | --- | --- |
| WALKS (10 portal index walks), SWEEPS (property, broker, full location resolve), INTAKE (location claims), UPKEEP (stale image URLs, rent map, map overlays, the RÚIAN registry load), ML-CPU (CLIP tag+retag) | **Railway job runner** | process-shaped work that needs an on-time slot, a kill at its budget, one lock, a Health cell, and a by-hand run from the same machine, secrets and IP |
| SQL (`recompute_city_proximity(false)` hourly) | Supabase pg_cron | one plpgsql call on the database's own clock |
| GPU (autodedup seeds, DINOv3, bake-offs) | RunPod pods, dispatched from attended GitHub forms | each run is a spending decision a human makes; the pod fetches a presigned R2 archive, never the repo |
| ATTENDED (migrations, backups, reparse, reextract, labelling, exams, boundaries, population, cities, retire, export, parked condition tools) | GitHub `workflow_dispatch` forms, unchanged | branch ref before merge, psql/pg_dump, artifacts, a typed confirm |
| CI (`test` incl. frontend + docs-budget, `migrations`, `build-extension`) | GitHub-hosted runners | PR heads; schema-and-sql needs Docker |
| CANARY (`verify_pipeline`, hourly) | GitHub-hosted, unchanged | the one alarm independent of Railway and pg_cron; its red run is the operator's e-mail |
| Price stats (weekly, needs Playwright) | GitHub schedule, by ruling; ticks through the same claim path so it has a row and a cell | browser toolchain |
| LATENCY lanes | Realtime worker, 17 → 16 lanes (`count_probe` deleted; images lane absorbs the inactive-image residue) | warm loops, not jobs |

The placement rules an agent applies to any new recurring job, in order: PR/branch code or Docker → CI;
raises the alarm when Railway or Postgres is down → the canary only; a human decides each run → an
attended form; one SQL call under ~5 min with no egress → pg_cron; needs a GPU → a RunPod pod; a
continuous loop under ~5 min cadence with warm state → a worker lane; everything else → one registry line
on the job runner. A missing toolchain is fixed in the runner image (one Dockerfile ARG), never by a
different host; only a browser or an operator ruling keeps a scheduled job on GitHub. Self-hosted GitHub
runners are a CI-only contingency (trigger: CI above the Enterprise pool two months running), never for
production.

### 4.2 The job runner (the textbook distributed-cron pattern, stdlib Python; sizes in § 7)

- **One table**: `scheduled_jobs` = `location_jobs` renamed (migration, with a temporary alias view). Added:
  `argv`, `offset_s`, `timeout_s`, `memory_mb`, `cpus`, `due_at`, `paused_until` (finite, with a
  note and author), `requested_at/args/by`, `registry_at`, `retired_at`, a `resource` column for jobs that
  must never overlap (intake, the RÚIAN load, the retire tool share `location-heavy`; it replaces the GitHub
  `location-batch` group). `last_outcome` gains `timeout`, `interrupted`, `lost`. The dead columns
  (`enabled`, `concurrency_group`, `alert_route`, `note`) drop under R6b behind a backup receipt.
- **One history table**: `job_runs` (closed in place; pruned daily by the runner after 90 days): holder, version
  (`RAILWAY_GIT_COMMIT_SHA`),
  args, requester, started/ended, outcome, exit code, peak RSS, a 64-line log tail, and
  `UNIQUE (job_name) WHERE ended_at IS NULL`: one open run per job, guaranteed by the database.
- **One registry in code** (`scraper/jobs.py`, ~100 lines, W2 #2023): `Job(name, argv, every_s, offset_s,
  timeout_s, memory_mb, cpus, host, resource, db_heavy)`; `every_s` is upserted as 384's `cadence`. Walk
  entries are generated from `portal_factory.PORTAL_CLASSES` with two code-only per-portal values, the
  Portal class attributes `walk_every_s` and `walk_timeout_s` (never in the DB-overridable limits layer), so
  shared scheduler code names no portal (rule 21). Every job's entry exists from W2 with a `host`:
  `job-runner`, `github-actions` (the canary and price stats), or `github-actions-until-moved` (a workflow
  still schedules it; its values are the move's targets). **A scheduler reads `runner_jobs()` only**
  (`host == job-runner`; `declared()` is the censuses'); it upserts, schedules, claims and accepts a request
  only for those names, adopts a `registry_at IS NULL` row one of them names (lease state and history kept),
  logs and skips a row it cannot reconcile instead of aborting at boot, and retires only rows with
  `registry_at IS NOT NULL AND runner = 'job-runner'` that its registry no longer names. Whole-day grids
  are phased so a slot falls at, or up to 3 days after, the calendar run it replaces. CI pins
  (`tests/scraper/test_job_registry.py`): every argv parses; walk set = `PORTAL_CLASSES`; static co-tenancy
  (the largest `RUNNER_MAX_CONCURRENT` memory declarations, one per resource, ≤ 81 % of a replica, 70 % until #2155); no two
  DB-heavy jobs in one hour (a wait for a shared resource and the kill grace counted; scheduled runs and their
  retries, not a deploy's at-once re-run of an interrupted one); the API's stuck-run
  cutoff (`STUCK_RUN_CUTOFF_MINUTES`) exceeds the longest walk timeout (the walks are the `scrape_runs`
  writers) plus `STUCK_RUN_GRACE_S`; the GitHub schedule set is exactly {verify_pipeline,
  scrape_price_stats} once the files still running a waiting job are set aside, so it tightens by itself
  when the last one moves; and, computed from the workflow files with no hand-kept ledger, every job runs on
  exactly one host.
- **One service** (`python -m scraper.job_runner run`, exec form: a shell as PID 1 forwards no SIGTERM), **two
  identical replicas**, 8 GB / 4 vCPU each,
  `restartPolicy ALWAYS`, built from the same Dockerfile, whose `ARG EXTRAS` shipped with
  #2060 (no default; unset installs `api`): the runner sets `EXTRAS: "api,clip,geo"` in its `.railway/railway.ts`
  env, so CPU torch, the pinned CLIP weights (revision 3d74acf9) and the geo wheels live in this image only.
  `watchPatterns` cover the trees jobs import. **As built (#2025):** service `job-runner` in the realtime-worker
  project (no new project); `drainingSeconds` 250, outlasting the runner's own 180 s drain by its last tick and
  forget (243 s); 8 GB is 8e9 B = 7,629 MiB (`jobs.REPLICA_MEMORY_MB`); the watch patterns are every tree the
  Dockerfile copies (a census); the torch and CLIP layer is keyed on the checkpoint pin alone and proven offline in
  the build and in `CI: runner image`. Its one secret is a reference to the worker service's `SUPABASE_DB_URL`
  (ruling #2025-R3's default, not a shared variable): one value to rotate, and a worker rotation redeploys it.
- **Scheduling**: anchored, staggered slots on the database clock (`due_at` from `every_s` + `offset_s`);
  missed slots are skipped, never replayed; a new row's first slot is no earlier than `now() + timeout_s`
  so it never overlaps a GitHub run still in flight at cutover. Claim-if-due is one compare-and-set, tried
  oldest `due_at` first so a job waits at most one sibling's run for its resource; the lease TTL is 180 s,
  renewed every 60 s, with self-fencing before expiry, and each claim is its own holder (a late release of a
  lapsed run never touches the job's next run). The due list holds free, registered rows with no open run on
  the job or its resource (a lapsed run is the reaper's: it closes it `lost`, never waiting on a claim's locks).
  Every runner call is one transaction under a 5 s statement timeout. The boot upserts the registry, retrying each
  tick until it lands; the runner execs argv from its in-memory entry, never from the row.
- **Execution**: each job is a subprocess in its own session with its exact argv and `JOB_CPUS` exported (CLIP
  threads and the intake pool read it instead of `os.cpu_count()`); a guard thread sends every signal, with no
  database call and no log write of its own (a forwarder thread writes its lines), so neither a stalled database
  nor a stalled log reader delays a stop: at `timeout_s` SIGTERM to the leader only (a walk reads it
  as its deadline; a pool finishes the item in hand), SIGKILL to the whole group `GRACE_S` (90 s) later, under
  `jobs.STUCK_RUN_GRACE_S` (600 s; the stuck-run cutoff and DB-heavy hour censuses count it); at the lease's fence,
  or a refused renewal, the group is SIGKILLed at once. A run the runner stopped at `timeout_s` is recorded
  `timeout` whatever it exited (0, 75 and 143 included), never `interrupted` (an interrupted run is due again at
  once, so a walk cut by its own timeout would restart at once); the lease is released only after the child's
  whole group has exited; the exit code is stored signed (−9, −15: killed by that signal), the peak RSS is the
  larger of the leader's `VmHWM` and the group's summed `VmRSS` over 1 s samples (never `ru_maxrss`, which
  inherits the runner's own peak at exec; shared pages count twice, the safe direction; a job shorter than one
  sample records none), and the tail is the last 64 lines. Children carry `oom_score_adj +500`, set by an exec
  shim in the same pid, so an OOM kills the job, not the runner. **Residual:** a fenced child's statement already
  sent keeps running on the server and can commit (autocommit; the pooler keeps the backend's client alive). The
  database commit fence, `job_lease.fence` (#2114), serves a holder of its own `Hold`; a runner child holds none.
- **Retries**: an interrupted run (deploy, kill) is due again at once and is claimed by the new deployment
  within seconds; exit 75 ("more remains") re-dues after 60 s and replaces every self-chain and PAT; failed
  runs back off exponentially from 60 s, capped at the cadence and the next slot; a timeout retries on that
  backoff floored at the job's own `timeout_s`; a lost run (lease expired) is re-run once, then backs off. A
  DB-heavy job's failure, timeout and lost retries wait for its own hour (the next day at most); its interrupted
  run is due at once like any other (#2024 R-a). A SIGKILL the runner did not send (likely the OOM killer) is
  `failed`, with the peak in its error. A pure request run never moves the schedule (its exit 75 only pulls it to
  60 s); a request that also took a due slot advances it; an interrupted or lost request is not re-filed.
- **Manual runs and pauses**: `python -m scraper.job_runner request <job> [--by WHO] -- <args>` writes a request
  row the runner executes on the same host (args appended to the registry's, so they override its flags
  unchecked; a second is refused while one waits; one filed during a run waits for it); one request-only GitHub
  form "Jobs: run now" (`run_job.yml`: a typed job name, refused unless it is one, R-2 of #2026) does the same for
  the operator; `pause <job> --until <date|now>
  --note` sets `paused_until` on any row, a lane's and a retired one's included (`--until now` lifts it); `once <job>` runs a due
  or requested runner entry in the foreground (GitHub entries from #2033; a closed terminal stops it like Ctrl-C,
  and it exits 1 when the database does not answer within 60 s); `list` shows every row. Nothing is a
  flag; a pause always ends.
- **Health** (as built, #2026, migration 682): a `job/<name>` cell per registered job-runner row, live or retired
  under an hour (a blink, judged whole), computed in SQL by the pg_cron watcher (so it survives a Railway outage) through
  `public.health_job_cells()`, a seam a later wave replaces alone. A job cell is never `unknown`: not run yet,
  paused or held reads ok and says why. Against the row's own cadence C and `timeout_s` T: in flight is Watch past
  ⅔·T and "being stopped" past T; a run the runner recorded `timeout` is a Problem until a later run closes (warn
  at 2/3, problem at 1×, read from the runner's own record, never a sample of the 90 s kill window: R-3); in
  flight past T + `STUCK_RUN_GRACE_S` is a Problem (the kill did not land). A failure streak is Watch at 1 and a
  Problem at 3 once its first failure is min(C, 1 h) old (backoff reaches 3 in minutes). Late is Watch past
  C + T + W and a Problem a further min(C, 1 d) on, W the longest sibling run seen on its resource, anchored on the
  last ok run, else the first scheduled run, else the first slot, and on a lapsed pause; every row is judged on
  its runs since it joined the runner (`registry_at`, which `register` stamps for a new or adopted row, or one
  un-retired an hour or more after its retirement, and later boots and 682's trigger keep; a shorter retirement,
  an older image's blink or a revert re-landed within the hour, keeps its `due_at` too), from the first slot of
  that registration, the streak and success it carried from before a revert unjudged. `held by sibling` is
  ok, shown only while the job waits; a due job or a request unstarted for 10 min is Watch while a replica beats
  and a Problem when none has for 10 min; a refused registry entry is a Problem; a pause longer than
  greater(C, 7 d) is Watch. The cause (outcome, signed exit code, error, requester and args) comes from
  `job_runs`, never its `log_tail`. A `job-runner` fleet cell is ok or Watch, never a Problem: a cleanly stopped
  runner reads Watch until a job comes due + 10 min. If the arm raises, one `job-cells` Problem and a listed
  `unknown` per open job incident keep those incidents open. One heartbeat row per replica for liveness
  (`job-runner/<RAILWAY_REPLICA_ID[:8]>`, carrying its deployment; it beats only after a claim pass completes,
  so a replica that cannot register or list due jobs goes red while its heartbeat row exists), each worker with
  its own liveness subject (migration 675); a clean stop deletes its row, and each beat forgets another
  deployment's rows and its own deployment's after an hour of silence. A replica that never registers never
  beats, so it has no row: a job main's registry puts on the runner with no live row is the `jobs` check's
  `job-runner/registry` Problem an hour after the worker's boot (`jobs_runner_register_grace_s`), once the previous
  measurement read it missing too. The two global in-flight thresholds are replaced by a test-pinned mirror of the
  worker's one pass budget (`LANE_PASS_BUDGET_S` = `LANE_PASS_TIMEOUT_SECONDS`): a lane is Watch at ⅔ and a
  Problem at 1×, a job Watch at ⅔ of its `timeout_s` and a Problem at the runner's recorded stop; one shape, two
  mechanisms.
- **Deploys**: `portal_runner.run_phase` installs a SIGTERM/SIGINT handler that sets one shared stop event;
  `portal.deadline_reached()` reads it; the rate limiters wait on it; `BasePortalClient._request` checks it
  before every attempt and backoff and caps each request at the drain budget left. `by_category` persists
  per category in the same transaction as `bump_index_pages`. A cut walk resumes as part of **one cycle**: the
  resumed run skips scope groups whose reached-end is newer than the cycle's anchor (the first fragment's
  start), siblings stay adjacent, and the orphan-nomination step runs only in the fragment that closes the
  cycle over all of the portal's scopes with `last_seen_at >= anchor` as "seen" (rule 3 intact). A run that
  walks its whole plan ends the cycle, short when a group failed; groups go least recently walked first (idnes's
  rule) and a cycle resumes only within the walk timeout of its anchor, so no group can starve the rest.
  Source of truth (operator ruling 2026-10-29, #2020): `scrape_runs.by_category`, whose entries carry each
  unit's scope and cycle anchor; idnes's `_slice_order` and `portal_index_slices` are deleted (migration 689).
- **Secrets**: Railway's service variables are the store of record for runtime (the runner references the
  worker's: `SUPABASE_DB_URL`, ruling #2025-R3, and the R2 set, by reference (#2032); a shared variable would be per
  project, `ctx.shared.NAME`, a second place to rotate);
  GitHub keeps what CI, the canary and the attended forms read; the duplicated set is listed once in the
  toolkit-api skill. Deleted:
  `SCRAPE_CHAIN_TOKEN` (both hosts), `WORKER_DISPATCH_TOKEN`, `GEMINI_API_KEY`. Added: a new fine-grained
  token under the organisation for price-stats "Run now". No `RAILWAY_TOKEN`: M0 (#2060) applies
  `.railway/railway.ts` by hand in merge windows (operator ruling R2, 2026-10-26), and #2025 keeps it so, now and
  after W6: plan/apply in CI would need one project token per Railway project and an apply restarts services.

### 4.3 Honest limits (from the refuters)

- The runner scales to 30 portals with no per-portal code and no new Railway object, but "adding a portal"
  still means a client, a parser, an adapter and config rows elsewhere; and the **realtime worker's serial
  probe/drain loops** must become per-portal passes before about 15 portals. That is a named follow-up
  wave on the scraper track, not this sprint.
- Railway adds failure modes GitHub did not have: deploy cuts (bounded by the cycle resume; the retried
  tail is measured per walk in W4 as proxy GB/day and orphan-nomination cadence) and one egress IP for every
  walk (gated per walk by the 403/429 share; the remedy is a per-portal proxy or static outbound IPs).
- A merge still redeploys the runner ~8–11 times a day; `watchPatterns` narrow it, and batching merges is
  the operator's lever.
- The per-failure table (hung job, OOM, deploy mid-run, portal block, Railway outage, DB stall), today vs
  this design, is kept in the W2 PR body; against today the design ties or wins on all but Railway-outage,
  where batch work waits as the latency lanes already do, and the canary alarms.

## 5. Waves and gates

Each wave is one or more small PRs with a one-click revert; every PR records its pre-check SELECTs and
regenerates `frontend/public/workflow-docs.json`. PRs touching the realtime worker merge only on the
merge-window light. The repo stays public until W6, so every run during the move is free.

### W0 — Organisation first, then prune while Actions are free
Operator steps (guided in chat, ruling 2026-10-27): create the `propscout-cz` organisation on Enterprise Cloud
("personal accounts" type, metered, no trial), transfer the repo, reinstall Railway's GitHub App on the
organisation and re-link the three services, one merge posts green statuses; recreate the price-stats token
under the organisation; every peer session is told before and after. Coordination: after the propscout.cz
domain cutover has deployed (sreality-f3) and PR #2002 has merged (sreality-e6/e9).
Code: delete 27 workflows (16 dead/one-shot incl. the six R2 one-offs, 8 redundant backstops, 3 folds) and
11 one-shot scripts with their tests; fold `images.yml` into the images lane (EXPLAIN-gated), city
proximity onto pg_cron (migration 672 + a weekly `cron.job_run_details` cleanup), `clip_retag` into
`clip_tag`; stop the price-stats runaway (stalest-first ordering + a progress-gated chain on the job's own
`GITHUB_TOKEN`); delete the `count_probe` lane and its GitHub dispatch; retention-days on every artifact,
the retire backup to R2, artifacts purged to ≤ 1 GB; `SEED_MEMORY_MB` from the cgroup; the RunPod pod
fetches a presigned R2 `git archive` tarball; User-Agent contact
`propscout/1.0 (+https://app.propscout.cz; info@propscout.cz)`.
**Gate (72 h):** deploys green from the new owner; per-source pending p95 ≤ pre-W0 + 10 %; image freshness
unchanged ± 10 % and the inactive residue falling; settled-by-first-photo ± 2 pp; `cron/recompute-city-
proximity` ok 24 h; no new non-ok cell; 57 workflow files; artifacts ≤ 1 GB; 0 hits for the deleted tokens.

### W1 — Walks that survive a cut, proven on GitHub first
The § 4.2 deploy seams and the cycle resume; `bezrealitky_main` gains `--index-only/--drain-only`; the four
combined scrapers (bezrealitky, maxima, mmreality, remax) become walk-only; the `JOB_CPUS` helper.
**Gate:** unit tests (SIGTERM mid-category → rc 143, `ended_at` set, the cut group nominates nothing, the
next fragment starts at the stalest group and only the closing fragment nominates orphans, SIGTERM during a
hung request finalises under the drain budget); one cancelled realitymix walk finalises within 15 s; 7 days
with 0 index runs `ended_at NULL` > 6 h and nominations per cycle ± 10 %.

### W2 — The runner, live with the smallest jobs
One migration (rename + alias view, the new columns, `resource`, `job_runs` with its unique open-run index,
per-worker liveness subjects); `scraper/job_lease.py` (moved + claim/renew/fence/release), `scraper/jobs.py`,
`scraper/job_runner.py`, the `job/<name>` SQL arm, the request-only `run_job.yml`; the `job-runner` service
added to `.railway/railway.ts`. **Scope change 2026-10-25 (operator ruling Q24, agent-platform grill):** the
move of the three existing services to that file (operator-applied in windows, no CI recipe: ruling R2, #2060), and the
deletion of the three `railway*.json`, is the agent-platform programme's M0 (PR #2004, `MVP.md` § M0, due
within the week, hard cutoff 2026-12-01); #2025 reuses the file and lands it for all four only if M0 has not
landed by then. First jobs: stale_images, rent_map, map_overlays. Each later PR flips its entries' host to
`job-runner` and deletes the matching YAMLs; the pairing census fails either half alone. A move PR folds or
drops each other step of the file it deletes, and says which in its body. A move never lowers `timeout_s`. A
long-cadence entry's slot falls at, or up to 3 days after, the calendar run it replaces (the 30-day grids
drift ~5 days a year earlier), else the PR re-phases `offset_s`. After merge, `gh run list --workflow=<file>
--json status,databaseId` shows any queued or running run of the deleted file; if one exists, the new row
is paused (`job_runner pause --until`) past that run's timeout, since a queued run in a non-cancelling group
can start after the merge. A new row's first slot comes after `now() + timeout_s`; rollback is a revert. A
re-land within an hour of its revert's deploy keeps the old `due_at` and claims at its boot, so its retired row
is paused before that deploy.
The PR that moves the last waiting job (#2031, #2032 or #2033) also deletes the `github-actions-until-moved`
host, its ratchet and the gone leavers in the registry test.
**As built (#2025), three PRs, each merged on the merge light:** (1) the `job-runner` service, dark, applied at its
head inside one window (`scripts/railway_iac_wizard.sh window worker 1 0 job-runner`), then merged at once, with the
runner image, migration 683 (`rent_map_cells` names its scripts, not a workflow) and the wizard's general modes;
(2) the move of stale_images, rent_map and map_overlays, after #2026 deploys, with no `.railway/` change and no
migration, so `git revert -m 1` undoes it (pause first); fetch_rent_map.yml's companion re-render is folded into
`scripts/fetch_rent_map.py` (a refused render exits 1; the override is a `map_overlays` request `--sections rent_map
--accept-shrink`, then a `rent_map` request clears that job's cell); (3) migration 702, `map_overlay_tiles` re-hosted to `job-runner` once (2) deployed; each later move PR re-hosts its own row (`test_no_artifact_of_a_runner_job_is_hosted_on_github`).
**Gate (7 days):** every job cell green; ≥ 3 deploys survived with interrupted runs re-claimed within 1 min;
one request run, one pause with expiry and one self-fence drill exercised; 0 unique-index violations, 0
double holders; resolver and intake lanes green 48 h after the rename; `railway config plan` = 0 changes.

### W3 — Sweeps and intake
property_sweep (keeping its 56 h fail line), broker_sweep, location_intake (exit 75
for backlogs; the pool width from `JOB_CPUS`); the intake self-chain, its yield bash, the `location-batch`
GitHub group and the last `SCRAPE_CHAIN_TOKEN` users deleted. (The full location resolve and the
`location_resolve` `*/15` tick left GitHub in the location programme's W2, 2026-10-31: the worker
sweeps itself; nothing to move.)
**Gate (7 days):** derived artifacts fresh; `location_town_coverage` not worse; sweep durations ± 20 %; a
backlog drains at least as fast as the chain did; no two DB-heavy jobs overlap; intake reads `held by
sibling` during a registry load instead of red.

### W4 — The walks
In ascending duration, 2–3 per PR: web, maxima, bezrealitky, remax, mmreality, sreality (hourly, R24),
realitymix, ceskereality, bazos, idnes last. Walk argv drops `--max-seconds`; the runner's SIGTERM at the
walk timeout is the one budget (this is #2029's "budget derived from the timeout"; the registry's argv census
refuses a walk `--max-seconds`). A batch flips its walks' host without a portal name in `jobs.py`; a
per-portal host attribute would be a third code-only per-portal value and needs a ruling.
**Gate per walk (7 days):** pages, reached-end and nominations per cycle ± 10 %; 403/429 share from
Railway ≤ the GitHub baseline; 0 `ended_at NULL` after ≥ 5 mid-walk deploys; proxy GB/day within its band;
delisting and coverage cells unchanged; aggregate peak ≤ 70 % of a replica.

### W5 — CLIP, the registry load, the GitHub tick, the leftovers
`clip` every 15 min in the runner image (one unsharded run, exit 75 for backlogs; `clip_tag.yml`, the shard
plumbing and the weights cache deleted); `ruian_load` moves to the runner (geo wheels in the build);
price_stats ticks daily through `job_runner once` on GitHub so it has a row and a cell; `location_refetch`
reads its due time from its row (`first_delay_seconds` deleted). The test pinning the GitHub schedule set
exists from W2 (#2023) and tightens by itself when the last waiting job moves; W5 adds its one workflow-reader
rule, `scraper.job_runner once <job>` read as running `<job>`, without which the pairing census reads
`scrape_price_stats.yml` as running no registry job and the timeout floor stops checking price_stats.
**As built (#2032), one PR on the merge light**, its first commit (the worker's four `R2_*` on job-runner, by
reference) applied at its head in one runner-only window (no autodedup brake: #2025-R2), then merged at once:
`clip` every 900 s at offset 300 s, `timeout_s` kept at 3300 with its own `--max-seconds 1500` budget, a
re-select every 300 s and whenever the snapshot drains, the tag phase ending on a re-select under one chunk
(64 a chunk: Pillow holds RGB at 4 B a pixel; children run with
`MALLOC_ARENA_MAX=2`); exit 75 only when the budget cut a run that was making progress or a re-tag campaign stays open, exit 1
when its downloads failed and it marked nothing (a whole chunk's worth), exit 2 without R2; cpus 4 (torch threads from `JOB_CPUS`), memory 3072 MB (the first run peaked at 2,386; #2155),
no resource. The order is the last 45 min first, then the priority kraje, then newest first. The next chunk's
GETs download while this one encodes, each chunk waits at most 45 s for them, and the job's R2 client gives up
a stalled GET after 2 × (5 + 20) s; a GET that failed is retried by the next run, not the same one. A batch that
fails to embed falls back to one image at a time, and an image that fails to decode, or to embed alone, while a
sibling does is marked terminal (at most 2 a chunk, 5 a run, each; one chunk past a decode cap is skipped, its
photos that decode tagged, and a second in a row exits 1). It tags, then re-tags for at most 300 s with
the same model load (`retag_from_embeddings` is a library now). The runner image pins transformers,
Pillow and numpy in `data/requirements-clip-runner.txt`, asserted by the offline proof, and `CI: runner image`'s
`api-image` job proves the API image clip-free. Health's `fleet/photo_tags` judges the 45-min line (tagged =
stamped and vectored; Watch at most while `clip` is paused, then judged on the photos stored since the pause ended). `--verify-reembed` (`scripts/clip_reembed.py`)
re-embeds a frozen 100-image sample read-only; it runs while `clip` is paused "AC2 pending" from its
registration, and the pause lifts on a pass. No hold for a GitHub run in flight (both writers are idempotent and
stamp last), and no migration.
**Gate:** CLIP lag p90 ≤ 45 min; a 100-image re-embed identical within fp32 tolerance; both GitHub-hosted
jobs show cells; `location_refetch` runs once a day across ≥ 3 deploys; **the flip gate: 7 days of billed
GitHub run-rate under 50,000 min/month.**

### W6 — Consolidate CI, prove the private paths, flip
`test.yml` absorbs `frontend-build` and `docs-budget` as one `frontend` job (vitest `--maxWorkers=2`, timeout
sized for 2-vCPU runners; the budget step keeps `fetch-depth: 0`, its `HEAD^1` base argument and a PR-only
`if:`); PR-only `cancel-in-progress` on test + migrations; `build-extension` job renamed `extension`; required
contexts → pytest, schema-and-sql, frontend, secrets (ruling 2026-10-28); gitleaks: one history scan, a PR job
over the PR's commit range, a commit/push hook; the two fixture map keys replaced by placeholders; LICENSE
notice (all rights reserved); Dependabot alerts on; docs lose "PUBLIC" (CLAUDE.md, issue-tracker, scraper-ops);
R6a deletions; R6b drops behind receipts; the Actions budget set at $100 with alerts.
**Gate before the flip:** the W5 flip gate holds; artifacts ≤ 1 GB; one pod logs `step=fetch ok` from the
tarball; history scan clean or every finding rotated/fingerprinted; one autodedup `rt_seed` at the current
widest scope finishes on a 2-vCPU/7 GB runner, else R5 decides; a deliberately failing draft PR is blocked.
**After the flip (72 h):** `private:true`; exactly 4 required contexts; Railway statuses for all four
services on the next merge; canary green; billing page run-rate inside the pool; RunPod and autodedup
dispatches succeed; the canary and `realtime-scrapers.md` glossary entries updated.

## 6. Cost (USD per month)

| Line | After | Basis |
| --- | --- | --- |
| GitHub Enterprise Cloud, one seat | 21 | 50,000 included minutes; branch protection; organisation ownership |
| GitHub Actions overage | 0 | CI 23–42k + attended 1.3–2.5k + canary ≤ 0.7k + ticks ~0.5k = 25–46k, inside the pool |
| Railway job runner (walks, sweeps, intake, upkeep, CLIP, two idle replicas) | 12–25 | time-weighted at $0.0278/vCPU-h and $0.0139/GB-h; measured from `job_runs` peak RSS in W2–W5 |
| pg_cron, artifacts, secret scanning (gitleaks) | 0 | |
| Residential proxy | unchanged + a 5–10 % retried tail on proxied walks | measured in W4 |
| **Total** | **~35–50** | versus ~$650 private with nothing moved |

The flip never precedes the W5 gate, so no month is billed above the seat.

## 7. Subtraction ledger

Deleted: 46 workflow files (27 in W0, 17 across W2–W5, `clip_tag` in W5, 2 in W6; ~4,400 YAML lines);
~45 concurrency groups; every self-chain and PAT; the `count_probe` lane and its dispatch (~200 lines +
tests); 11 one-shot scripts (~2,850 lines) and their tests (~1,400); the image shard flags; four duplicate
drain phases; the `location-batch` GitHub group, the intake chain and yield; the `location_resolve` tick;
`railway*.json` ×3; the two global in-flight thresholds (replaced by a pinned mirror of the worker's one pass
budget) and the hand-kept stuck-run cutoff; the per-walk
`--max-seconds` duplicate; idnes's own slice ordering; the `LOCATION_INTAKE_WORKERS` override; the 4-way
CLIP shard matrix and weights cache; `location_refetch`'s first-delay machinery; the 18 h GitHub slack on
artifact rows, re-hosted row by row in each move PR; the anonymous GitHub clone in RunPod pods; 13.2 GB of public artifacts. By ruling:
up to 9 more files (R6a) and the dead lease columns, alias view and index (R6b).
Never built (dropped from the first design): the shared `lanes.py` move, 17 interval knobs, the due-gated
GitHub matrix, the overdue-only fallback rule, the disarm wave, runtime memory admission.

Added: the job-runner service (two replicas) and the Dockerfile's runner lines (#2025: the `clip-ref` stage, the
model layer and the offline proof, with no ARG default); columns on the reused
`scheduled_jobs`; `job_runs`; `scraper/jobs.py` (~100), `scraper/job_runner.py` (~220), `scraper/job_lease.py`
(moved, ~+60); the `job/<name>` SQL arm (~60); the W1 seams (~60); `run_job.yml` (~30, request-only);
its service in `.railway/railway.ts` (~15: M0, #2060, ships the file and the one ARG, `EXTRAS`, which the runner sets);
two code-only per-portal walk values; ~250 lines of tests; one `job-runner` heartbeat row per replica; the runner's
daily `job_runs` prune; a LICENSE notice (no `RAILWAY_TOKEN` in M0; CI tokens are #2025's call). **As built
(#2024):** `scraper/job_runner.py` ~725 lines (~595 of code), `scraper/job_process.py` ~410 (~300), `job_lease.py`
+~375 (the runner's statements), `stop.py` +4/−3 (an opt-in hangup), ~2,200 test lines; the guard thread, the mixed
kill mode, the straggler wait, atomic spawn and the per-call stopping budget are the growth over the estimate.
**As built (#2026):** the `job/<name>` arm is migration 682's `health_job_cells()` (~220 SQL lines; the watcher
re-created around one ~20-line block), the `job-runner/registry` census ~50 lines, `run_job.yml` ~60 lines; the
fleet cell, the (re)registered first slot, the refused entries and the arm-raised placeholders are the growth.
**As built (#2032):** deleted `clip_tag.yml` (127 lines: its 4-shard matrix, its weights cache step and its job
clock), the `--shard/--shards` flags and their SQL in both CLIP scripts, the ignored `--categories`, the retag
CLI (`retag_from_embeddings.main`) and, after the merge, GitHub's two weights cache entries
(`hf-clip-vit-base-patch32-v1`, the orphan `hf-clip-vit-b32`); added `scripts/clip_reembed.py` (the re-embed
proof), `data/requirements-clip-runner.txt`, the `fleet/photo_tags` cell and the `api-image` CI job.

Net: workflow files 84 → ~38 (31 if R6a passes in full); live GitHub schedules 34 → 2; production schedulers
2 → 1; Railway services 3 → 4; Railway config files 3 → 1; worker lanes 17 → 16; roughly −8,500 to −9,000 lines.

## 8. Rulings (operator, 2026-10-25)

R1 shape approved · R2 organisation `propscout-cz` on Enterprise Cloud (`propscout` is another firm's account; ruling 2026-10-27), transfer in W0 after the domain cutover ·
R3 flip only after the move, gated on 7 days under 50,000 billed min/month · R4 CLIP moves (R23: torch only
in the runner's build) · R5 operator UX of moved jobs: one request form, Railway logs, pause by row ·
R6a six parked/one-off workflows + `compute_image_phash` (bounded refill in the images lane) +
`backfill_idnes_brokers` (after re-measuring the 637-row gap) deleted; R6b drops behind receipts ·
R7 price stats: one weekly run · R8 glossary terms adopted · R9 User-Agent contact
`propscout/1.0 (+https://app.propscout.cz; info@propscout.cz)` · R10 LICENSE notice + Dependabot alerts ·
R11 Enterprise, bought directly · R12 no walk throttling · R13 gitleaks, Secret Protection left open ·
R14 Actions budget $100 at the flip · R18 CI volume cuts out of scope · R19 ADR 0001 · R20–R21 job runner
replaces shape B; flip gate as above · R22 approved · R23 CLIP in the runner build only · R24 sreality walk
hourly · R25 RÚIAN load moves to the runner · R26 CLI + request-only form; Health buttons when asked ·
R27 two replicas.
**Still open, decided when measured:** autodedup `rt_seed` on a private 2-vCPU/7 GB runner (W6 gate): a
4-core larger runner (~$3 per seed), the E943/E944 memory work, or a request-only job on a heavy runner pool.

## 9. Rejected alternatives

- **One Railway cron service per job**: 18 services today, ~38 at 30 portals, each rebuilding on every
  matching merge (~150 builds/day); Railway never stops a hung run and skips every later tick until a human
  redeploys; no Health reporting; mid-run deploy behaviour undocumented; runs with arguments would still
  need a lease row, a Health arm and a form, which is most of a runner spread over 18 objects.
- **Self-hosted GitHub runners on Railway**: GitHub's throttled cron stays the scheduler; every YAML and
  group stays; no Docker, so no schema-and-sql; PR code beside production secrets; an admin registration
  credential; the postponed fee may return; Enterprise's pool already covers CI. CI-only contingency.
- **Shape B batch-worker as first written**: one container, a supervisor shared with the realtime worker
  (coupling batch PRs to the worker's merge window), 17 interval knobs (flags in disguise), manual runs
  executing on GitHub (a second host and egress), a cutover flag.
- **Walks as realtime-worker lanes**: a thread cannot be killed; the worker peaks at 94 % of memory; one OOM
  takes every latency lane down.
- **Established orchestrators** (APScheduler, Celery, Prefect/Dagster, Temporal, Kubernetes CronJobs,
  Railway Functions): each fails at least one of: deletes more than it adds, no new always-on service or
  SaaS, pooler-safe, can hard-kill, Health-readable, operable by agents (rule 7).
- **pg_cron as the clock for Python jobs**; runtime aggregate memory admission; a GitHub break-glass
  executor for scheduled jobs; a 4-hour `drainingSeconds` as the deploy answer; path-gated or skip-if-green
  CI checks; strict up-to-date merges; pytest-xdist; flipping before the move.

## 10. What the two adversarial passes changed

First panel (shape B): the bill restated upward; the stop event pushed into the client's retry loop and the
lease released only after the child exits; resume from the one existing slice ledger; aggregate memory gating;
`runpod_reap` kept and two deletions turned into rulings; per-worker liveness subjects; push protection's
loss stated plainly with a push-time replacement; the cutover flag removed; the ledger's temporary items
listed. Second panel (from scratch): the cycle-based resume so a cut walk still reaches the portal's end
before nominating; the per-failure table instead of a bare interventions count; job cells computed by the
pg_cron watcher so Health survives a Railway outage; scaling claims scoped to the runner with the worker's
serial drain loop named as the next limit; walk cadence kept code-only; the `location-heavy` resource
column restored in place of the deleted GitHub group; `location_refetch` and `sold_comps` moved toward the
registry; runtime memory admission replaced by a static co-tenancy test plus `oom_score_adj`.
