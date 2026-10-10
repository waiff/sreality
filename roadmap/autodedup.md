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
  (`obec:554782`, 5,000 MB, E946), a 3–6-day build with merging paused everywhere → S5 the apply
  scope widens a batch of quarters at a time, `town:554782` last. Brake: interval 0, the area out
  of the apply scope, then `unapply blocks=`. Open: D905–D910 (the operator's).
- **E941 (2026-10-09, S0 rails for the widened S1, ~38k ads):** the seed re-checks storage after its
  walk and cut (+18 KiB an ad), `MAX_SCHEMA_MB` 1,500, no quarter beside its town, the rate halved
  before each pass, `peak_rss_mb` in the heartbeat, SIGTERM stops the pass and frees the lease at
  once (every lane write fenced, 30 s drain). Owed: the cgroup memory limit, the revive cap,
  `verify_pipeline`'s heartbeat check.
- **E948 (2026-10-10, a worker that died holding the lease):** its restart in place (same hostname
  and pid) releases that lease before its take and halves the claim cap in the same commit (floor
  25; only a seed raises it again); at the floor the lease waits out its TTL as before, so a death
  no claim can cure never loops the worker. The heartbeat adds `claim_cap`, `predecessor_released`
  / `predecessor_kept`, `rss_mb` and the cgroup `memory_limit_mb` (E941's owed reading). Still
  owed (E941): the revive cap, since the revived feed ignores the claim's limit.
- **Next:** three live days and checkpoint C2, then the last commit deletes the batch
  `apply`/`unapply` modes; `legacy_retire` goes at W8. Widening the scope is the operator's call.
- **Open (operator decision, before the C2 deletion):** the undo path once `mode=unapply` is gone.
  Until then the brake is interval 0, then `unapply` (E927: one `detach_listings` call per group);
  the commit that deletes the modes must name what replaces it. `legacy_retire` rides on batch
  `mode=apply` (`retire_legacy=1`), so the same commit must also say where it runs until W8.
