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
  `POST /properties/{id}/split` (frontend only; the route is E919's). Open with the operator: lift
  E905's property-grain freeze (`restored_outside_engine`) so a split-off property can receive
  engine merges again; only his pair rulings would stay as bans.
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
- **Next:** three live days and checkpoint C2, then the last commit deletes the batch
  `apply`/`unapply` modes; `legacy_retire` goes at W8. Widening the scope is the operator's call.
- **Open (operator decision, before the C2 deletion):** the undo path once `mode=unapply` is gone.
  Until then the brake is interval 0, then `unapply` (E927: one `detach_listings` call per group);
  the commit that deletes the modes must name what replaces it. `legacy_retire` rides on batch
  `mode=apply` (`retire_legacy=1`), so the same commit must also say where it runs until W8.
