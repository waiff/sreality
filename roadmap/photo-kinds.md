# Photo kinds — one label per photo, decided once

Program document (north star, decision rule, lane, waves, cost, rulings, measurements):
`docs/design/photo-kinds/PROGRAM.md`. Status: **PROPOSED 2026-10-20, awaiting rulings R1–R8.**

## Why

Users see CLIP's zero-shot label on every photo (precision 0.58 on the seven kinds it shares with the trained
DINOv3 heads, measured on the sealed holdout exam_v1); the Browse "Cover" already reads the DINOv3 heads with a
different rule, so one card can contradict itself. The trained heads are right about 9 times in 10 at a 0.80 floor
(pessimistic bound 0.80) but cover only 7.75% of active photos and nothing scores new ones. Dedup reads the CLIP tag
rows, so the CLIP lane stays; only its product read paths go.

## Waves (one draft PR each)

- [ ] **W1** `cleanup/photo-kinds-dead-paths` — dead lanes and flags out (mirror weights, render backfill, train_tag_head, drop_dinov3, FINE_TAG_KEYS). No behaviour change.
- [ ] **W2** `feature/photo-kinds-decision` — migration 646 (additive): `kind_floor` + `shown_heads` on the model version, `display_name` on the vocabulary, views `image_photo_kinds` + `photo_kinds_public`, `images_public` appended; `tag_model gate` (read-only report); `/new-dedup/tags/*` deleted (R5).
- [ ] **W3** `feature/photo-kinds-lane` — the scheduled GPU lane: identity from the registry, R2 vectors, in-pass scores, selection-side re-arm, done record, coverage health cell; then the active-listing backfill (R2).
- [ ] **W4** `feature/photo-kinds-spa` — every photo surface reads the kind; CLIP, render pill and head-score reads leave the SPA (R3).
- [ ] **W5** `cleanup/photo-kinds-contract` — migration 648 (destructive, operator window): `images_public` 15 → 8 columns, `listing_cover_public` 11 → 3, `image_dinov3_embeddings` dropped (R7).

## Next (named, not in this sprint)

- v2 as data: `promote` from R2 vectors (deletes the dedup_sim coupling), one shared split, per-head calibration, exam_v2 single-phase draw, heads ložnice / jídelna / nezařízená místnost / domovní vchod from existing labels; WC / chodba / zahrada / balkon / schodiště / situační plán / document after labelling (~12–16 operator hours).
- "Has a floor plan" Browse + Watchdog filter on `image_photo_kinds` (kind 46), one `filter_compiler` hook (rule 16).
- For the dedup owner: `clip_retag.yml` (four no-op jobs an hour) and the never-scanned `image_clip_tags_render_null_idx`.
