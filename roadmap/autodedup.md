> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

# AUTODEDUP — autonomous cross-portal dedup engine

Full plan and the standing home for wave status:
[`docs/design/autodedup/PROGRAM.md`](../docs/design/autodedup/PROGRAM.md) — rules E/D/M by number,
the progress ledger in §15 (rows A1 apply track, W5 one lane). Operator summary:
[`docs/design/autodedup/ROLLOUT.md`](../docs/design/autodedup/ROLLOUT.md).

Separate from [NEW DEDUP](new-dedup.md) (schema `dedup_sim`): neither program reads the other's
data. The legacy engine's history is [`dedup-track.md`](dedup-track.md) — never resume it.

## State (2026-10-01)
- **Trial live.** The worker's `autodedup` lane is the one production path: interval 60 s since
  migration 572 (settings `w31`), scope = sales + rentals in the three trial blocks (Jablonec nad
  Nisou, Turnov, Praha-Vysočany) since migration 570. Merges go only through the rule-15 chokepoint
  (`merge_property_set`, `source='autodedup'`) and only inside `app_settings.autodedup_apply_scope`.
- **Brake:** `realtime_autodedup_interval_seconds` = 0, then `mode=unapply`.
- **Operator split (2026-10-03):** the property page states a whole partition by letters, in one
  `POST /properties/{id}/split` (frontend only; the route is E919's). Decided 2026-10-03 and done
  (E934): `restored_outside_engine` is deleted, so a split-off property receives engine merges
  again and only the operator's rulings are bans; owed: migration 558's `undone_by` column
  comment (E934).
- **Land cross-types (2026-10-04, E935):** the operator's ruling on 38803: a `pozemek` merges with a
  `dům` or a `komerční` property (Browse merge, a `same` ruling, the engine). Blocking groups, the area
  guard and every feature are unchanged, so the engine joins such pairs only where both ads state one
  size: offline +27 / +54 / +13 merge-zone pairs on trial / c18 / c17, M1–M3 and every ruling equal; the
  fan-out cap drops 10 / 9 weak-probe rejects on c18 / c17. Release after C2 with E929's re-seed. Open:
  a plot-aware area comparison for house × land, so the engine finds a 38803 by itself.
- **Category review (2026-10-05, E937):** `/autodedup/category-splits?properties=…` shows the
  properties a link names whose ads carry categories rule 15 never joins, side by side with photos
  and text, ten per page. Every ad has a letter, one per category to start, so a side that bundles
  two flats splits into two properties; per property the operator splits by the letters (the
  property page's `splitPlan`) or keeps it as one (both `POST /properties/{id}/split`). Open with
  the operator: "keep" writes `same` across categories, the word E925 refuses on the verdict route.
- **Flat ↔ commercial (2026-10-06, E938):** the operator's ruling: a `byt` merges with a `komerční`
  ad (one studio or atelier filed both ways). Rule 15 is now four PAIRS, not classes: byt + komerční
  and komerční + dům, never byt + dům or byt + pozemek; every reader compares pairs and the category
  review's sides are pairwise. Blocking, guards and features unchanged: offline +16 / +20 / +62 /
  +84 merge-zone pairs on trial / live / c18 / c17, every one read by hand (one unit filed both
  ways), M1–M3 and every ruling equal. Release after C2 with E929's re-seed. Open: the chokepoint
  reads a property by its canonical category, so a merged byt + komerční property can still meet a
  dům by hand.
- **Quarter probe (2026-10-04, E936):** an ad in a known quarter of Praha / Brno / Ostrava whose two
  attribute keys hold over 200 ads had no attribute path (1,670 Vysočany flats live, 268863 among them);
  it now keys disposition + area band at its quarter. Offline: live +13 merge-zone pairs (268863 joins
  its 44-ad iDNES property), none on trial / c18 / c17; M1–M3 and every ruling equal. Still dark:
  1,212 ads in four Vysočany keys over 200. Release after C2 with E929's re-seed (required: keys move; `SEED_VERSION` is bumped, so nothing
  merges between the deploy and the re-seed).
- **Prague, in stages (2026-10-07, E939):** the operator wants all of Prague (157,284 located ads,
  ~48.4k online) beside Jablonec and Turnov. Storage is affordable (~2.7 GiB, +0.4 GiB a month); the
  per-pass read is not: on main a 157k build takes 27–58 days and never catches up (M913, M917).
  S0 rails (#1692, E940 the paged walk, E941) → S1 Nusle + Libeň scanned with #1709's re-seed
  (~15.2k ads, shadow) → S2 they merge under E942's brakes → S3 facts only for scored pairs, the
  re-cut out of the pass, street keys for the 36k no-quarter ads (E943–E945) → S4 one Prague block
  (the 157 quarters as blocks beside the eight towns, 6,000 MB, E946; the no-quarter ads reach a
  scope only as `obec:554782` itself, now one PAGED town block — 167,548 rows, 36,628 with no
  quarter, 9 pages, E940 done 2026-10-17 — their keys E945), a 3–6-day build with merging paused everywhere (since E964 only in the blocks it builds) → S5 the apply
  scope widens a batch of quarters at a time, `town:554782` last. Brake: interval 0, the area out
  of the apply scope, then `unapply blocks=`. Open: D905–D910 (the operator's).
- **S6 — Středočeský kraj by okres (operator, 2026-10-17; E961):** after Prague merges, the whole
  kraj 27 (130,207 located ads, 1,133 towns) is seeded as its 12 `okres:` blocks, not 1,133 towns
  (the cap is 60 walks a day); the apply scope and W8 take it a `district:` at a time. The grain is
  built (6 h walks through each okres's obce; no town or quarter beside its okres, so Beroun's
  `obec:531057` leaves for `okres:3202`). **Unblocked by E940 (done 2026-10-17, superseding draft
  #1728):** a block over the 20,000-row page is read whole a page a walk (Praha-východ, 19,812 on
  10-17 and over in ~2 days, needs nothing more), the seed walks EVERY block to its end whatever
  the 60-walk cap (the 10-16 seed stopped at 60 of 120 blocks and cut its calibration over them),
  and `MAX_SCHEMA_MB` is 10,000: 7 towns + Praha whole (paged) + the 12 okresy = 323,128 ads,
  ~7,030 MB projected (the number is the operator's, D907). That seed no longer has to be
  `fresh=true` (which stops every live area's merging until the build ends): since E964 it is a
  WIDENING of today's `rt`
  (the seven towns kept, the 112 quarters replaced by `obec:554782`, Beroun by `okres:3202`) if
  the scorer and `SEED_VERSION` stand: the seven towns keep merging through the build; the three
  live Prague quarters and Beroun wait for their wider blocks. Open (E940): the cap binds that scope's cadence 4× (240
  walks a day asked, 60 allowed), so Praha is read whole every ~3 days. **E962 (done 2026-10-17):**
  the calibration cut streams (a chunk folded and dropped, the calibration byte-identical on the
  trial and c18), ~4-10 kB an advert at its peak by block size (bounded at 12 kB) against ~58 kB,
  so the 323,128-ad seed projects ~1.9-2.3 GiB on the 16 GiB runner (it would have been
  OOM-killed at ~18 GiB) and the memory bound admits 699,050 ads (`MAX_SCHEMA_MB` binds first,
  above ~470k). Its payload is no longer jsonb (E963, below). Owed: `Calibration.build` is now only the tests' oracle — fold it
  into the stream with the next change that moves the engine digest.
- **S7 — every other region (operator, 2026-10-17: after Prague and Středočeský kraj, "expand
  beyond to all other regions when possible"), ~872k located ads.** Prerequisites, measured:
  **E963 (done 2026-10-17):** the calibration payload is zlib over its canonical JSON in a bytea
  (migration 616), not one jsonb value — jsonb stops at 268,435,455 B of BINARY, ~1.57× the text,
  so ~252k ads in small blocks (E962's "~400k" read the text) and the nation's ~591 MB of text
  (~929 MB binary) could not be written; stored it is ~164 MB (the binding bytea limit is the hex
  READ, ~537 MB compressed, ~2.87M ads), the digest unchanged (trial and c18 proven). Ships with a
  re-seed: 616 applied under the brake, merge, `rt_seed fresh=true` (or E964's widening seed)
  before the brake comes off — a row cut before 616 is refused, not read. **After the first E963
  seed commits, never a bare revert: brake, revert, re-seed** (the retired `payload` still holds
  the 10-16 calibration). Owed, scheduled right after that sitting is verified: the DROP of
  `payload` / `artifact_url` (destructive: the operator's OK + a backup; the old reader then
  fails loudly). Still binding at the nation's size: `MAX_SCHEMA_MB` 10,000 (~470k ads,
  E940/E962), the cut's memory bound 699,050 ads at 12 kB (E962); the worker's calibration is
  ~2.9 GB through every pass (resident ~1.6 + `context_for`'s copy ~1.2) and ~4.5–5 GB at a
  digest change — under its 22.9 GB (one row a block of `block_token_df` would let the read
  parse a block at a time; not built).
  **E964 (built 2026-10-17): the country by widening**, one okres batch at a time (a kraj's
  okresy, or fewer), each a WIDENING seed — `rt_seed settings=<the generation's>,model=<its
  model>,rt_scope=<the scope + the batch>`, no `fresh`: every row stays, only the batch is walked
  and built, the calibration is re-cut over the whole scope (re-keying the reject ledger: 2–4
  full-price passes; the worker re-reads the moved payload once), the forward cursors are kept,
  and every area already read keeps merging while the batch builds (the reconcile now runs through
  a build and waits block by block, A9 amended; a member property's other ads and a not-yet-walked
  ad count). Then the batch's sample (the S2 reading), and only then the apply scope widens by its
  `district:` blocks — the operator's, per batch. The adversarial review of #1847 (F2–F8 fixed in
  it; F1, the vouched-rider rule, is E965 on main) recommends the Prague + kraj step itself be a
  widening once F1–F3 are deployed. Per batch: `MAX_SCHEMA_MB` raised to its projection (~21 KiB
  a new ad; the first +130k batch past 323k projects ~9,740 MB against 10,000, the second needs
  more — D907's disk); past 699,050 ads a cut the 16 GiB runner can hold (owed; the whole-scope
  cut is ~2.2 h there, ~2.8 h at 872k, inside the 5 h job and lease); a live town replaced by its
  okres in a batch of its own (its groups wait for the okres's build). A build with a listed
  backlog ran 5–16k ads an hour (~8–26 h a 130k kraj). `recut=true` re-cuts the same scope
  (today's `rt` is cut over 39 % of its 155.8k ads).
- **E941 (2026-10-09, S0 rails for the widened S1, ~38k ads):** the seed re-checks storage after its
  walk and cut (+18 KiB an ad), `MAX_SCHEMA_MB` 1,500, no quarter beside its town, the rate halved
  before each pass, `peak_rss_mb` in the heartbeat, SIGTERM stops the pass and frees the lease at
  once (every lane write fenced, 30 s drain). Follow-up 2026-10-10: a pass whose connections a
  database restart took retries the lease's release on new ones, every 20 s for up to 180 s,
  before falling back to its TTL.
  Owed: the cgroup memory limit, the revive cap, `verify_pipeline`'s heartbeat check.
- **E948 (2026-10-10, a worker that died holding the lease):** its restart in place (same hostname
  and pid) releases that lease before its take and halves the claim cap in the same commit (floor
  25; only a seed or, E948b, a bigger container raises it); at the floor the lease waits out its
  TTL as before, so a death no claim can cure never loops the worker. The heartbeat adds
  `claim_cap`, `predecessor_released` / `predecessor_kept`, `rss_mb` and the cgroup
  `memory_limit_mb` (E941's owed reading). Still owed (E941): the revive cap, since the revived
  feed ignores the claim's limit, and the `verify_pipeline` heartbeat check.
  **E948b:** the cap row records the memory limit its deaths happened under (`limit_mb`, read
  each pass), and a container limit grown ≥ 25 % resets the cap to 500 before the next pass,
  without a seed; a boot that also finds a dead predecessor releases its lease without halving
  (it died under the smaller limit). Today's row, which records none, grows from the 8 GB limit
  (`E948_LIMIT_MB`), so the deploy and the 24 GB move may come in either order.
- **E949 (2026-10-10, the worker carried memory from pass to pass):** seven deaths in the widened build,
  each process on about its sixth pass whatever it claimed. Freed heap now goes back to the kernel as
  every pass begins and ends (`gc.collect` + glibc `malloc_trim(0)`; `rss_at_start_mb`,
  `rss_after_trim_mb`), a raised pass's when the next pass begins; the worker runs on two malloc
  arenas (`env MALLOC_ARENA_MAX=2`); the engine counts and empties its body and token memos after
  every pass (`memo_entries`; the shingle memo keyed on the body itself); and the CLIP top-up is
  encoded a slice at a time. Decisions are a fresh process's and `SEED_VERSION` is unchanged; the
  claim cap stays at 25 until a seed or, E948b, a bigger container. Part 2 is E950.
- **E950 (2026-10-16, chunked scoring):** a pass scores the pairs it decides 5,000 at a time and
  writes each chunk's rows before the next, so the rows it holds follow the chunk; a side's CLIP
  vectors stay from its first decided pair to its last (most sides in a dense pass; the heartbeat's
  `chunk_vectored` shows it). On the dense worst case (every wanted pair scored, as after a deploy
  that moves the engine digest) the pass's peak falls 37 %, its time within 3 %; at the 39.5k build's
  passes 2–5 %. Decisions identical (cohort 18 and the trial export, every pair field and group).
  Prague's memory case is the working set, E960's. It moves the engine digest: it lands with E960
  and E944 as one train in one deploy before the Prague seed.
- **E951 (2026-10-12, a build stuck at small passes):** in a build a pass's time budget is three
  quarters of the 1,050 s deadline (787.5 s, was 525 s), and a pass the clock cut that claimed its
  whole share and finished inside half the deadline (525 s) stores at least twice its starting rate
  instead of the blend; the heartbeat adds `pass_budget_s` and `rate_doubled`. At the live ~0.032/s
  and ~490 s of fixed cost a pass claimed 16 adverts and the old budget capped it near 58; simulated,
  the remaining ~11.4k adverts take ≈ 7.0 h (≈ 7.4 h by the bigger budget alone, 36–110 h before).
  Owed: `wanted_pairs` and `attempts` in the heartbeat before full claims meet the `max_pairs` 150k
  wall (the review's estimate: ~134k pairs at 500 adverts on a 38.5k store).
- **E952 (2026-10-12, the build held near 30 adverts by the pair budget):** a build's pass may want
  300,000 pairs (`BOOTSTRAP_PAIR_BUDGET`; live stays 150,000), since a 30-advert claim on the 27k
  store already wanted more; a refusal now re-claims a quarter of the claim actually made, not of
  `max_listings`, and no longer reads the claim's CLIP vectors; the heartbeat carries `attempts`,
  `wanted_pairs`, `wanted_by_attempt` and `aborted`. The build's own budget was undone by E943 below.
- **E944, the re-cut out of the pass (2026-10-15):** a pass that finds the pHash population
  drifted marks the calibration due (`rt_recut:rt`) and the next tick re-cuts instead of passing,
  under the same lease and its own 1,050 s deadline (heartbeat `recut`); a pass reads the payload
  once a digest and hashes it once. Measured at 39.5k: the read ≈1.5 s a pass (≈0.9 % of a live
  pass), the cut 633 s (read-bound), which fitted only behind a pass shorter than ~417 s and never
  behind a build pass. Review (#1819): never in a build or while the scope holds > 50k ads (the
  row says why), the attempt stamped first (a death mid-cut waits 6 h, no claim-cap halving; two
  = `died` until a seed), a shutdown takes it back, a failure is `errors: 1`. Owed before Prague re-cuts: at 157k the cut is ≈2,500 s and fits
  no tick, so Prague keeps its seed's calibration until a cut resumes across ticks.
- **E943, the reject ledger (2026-10-12, two PRs: migration 596, then the code):** a pair decided
  below `store_floor` was deleted at write and re-scored by every pass that wanted it again (≥ 95 %
  of a 35-advert build pass's 117,795 scored pairs). It is now ledgered under both adverts' decision
  digests (the whole advert and its gallery, CLIP tags and stamp included), the calibration digest,
  the feature version and an engine digest (the decision code, the model, the settings), and a pass
  skips it while all five match and neither advert was refreshed; the budget is one 150k of pairs
  to DECIDE, build and live (`BOOTSTRAP_PAIR_BUDGET` deleted). Equivalence with today's engine is
  pinned pass by pass, a deploy and a late CLIP vector included; decisions and `SEED_VERSION`
  unchanged. Rollout: apply 596, merge its PR, then the code (before 596 the lane skips
  `store_absent`). Heartbeat: `pairs_to_decide`(`_by_attempt`), `ledger_skipped`, `ledger_written`.
  Costs: a re-cut or a deploy touching the decision code re-scores every wanted reject once (2–4
  deadline-halved passes); the seed's projection is 20 KiB an advert. Residuals: a CLIP re-embed
  that leaves `clip_tagged_at` alone must empty the ledger; a sighting (`last_seen_at`) re-scores an
  advert's rejects as it does its stored pairs. Next (E943's other half, before Prague): facts only
  for the claim and the scored endpoints, the decision digest stored on `rt_fp`.
- **Next:** three live days and checkpoint C2, then the last commit deletes the batch
  `apply`/`unapply` modes; `legacy_retire` runs per area under the live generation since W8 (E953, 2026-10-14). Widening the scope is the operator's call.
- **Open (operator decision, before the C2 deletion):** the undo path once `mode=unapply` is gone.
  Until then the brake is interval 0, then `unapply` (E927: one `detach_listings` call per group);
  the commit that deletes the modes must name what replaces it. `legacy_retire` rides on
  `mode=apply` (`retire_legacy=1`), and since E953 also runs alone under `generation=rt,blocks=`
  (the per-area undo before an area goes live), so that commit must keep the `rt` form.
- **Next (W8, E953):** per area, before it enters the apply scope: brake, the area into the scope
  row, `mode=apply` `generation=rt,retire_legacy=1,blocks=town:<code>` dry run, then live, then
  the interval back. Beroun (`town:531057`) first, once the lane has read it after the 10-14
  re-seed (the step refuses an unread block).
