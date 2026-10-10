/**
 * Single frontend source of truth for image-tag labels + the render shape that
 * carries a tagged image through the photo components.
 *
 * The tags come from CLIP (image_clip_tags, exposed on images_public as
 * clip_fine_tag / clip_logical_tag / clip_confidence). We display fine_tag — the
 * raw anchor CLIP picked — so the plot-identity distinctions (aerial / cadastral
 * / situation) survive for houses + land, where CLIP is the only tagger.
 *
 * MIRRORS the backend taxonomy — keep in sync (a drift-guard test asserts this
 * map covers every value the backend can emit):
 *   - the 12 canonical logical tags == toolkit/room_taxonomy.py ROOM_TYPES
 *     (== image_room_classifications.room_type CHECK == CLIP logical_tag)
 *   - the CLIP fine-only sub-styles that collapse into site_plan / other live in
 *     data/clip_taxonomy.json ("prompts" keys / "collapse").
 */

export const IMAGE_TAG_LABELS: Record<string, string> = {
  // Canonical logical tags (toolkit/room_taxonomy.py ROOM_TYPES).
  kitchen: 'kuchyně',
  bathroom: 'koupelna',
  toilet: 'WC',
  living_room: 'obývací pokoj',
  bedroom: 'ložnice',
  hallway: 'chodba',
  staircase_interior: 'schodiště interiér',
  staircase_exterior: 'schodiště exteriér',
  exterior_facade: 'fasáda',
  balcony_terrace: 'balkon/terasa',
  garden: 'zahrada',
  floor_plan: 'půdorys',
  site_plan: 'situační plán',
  property_document: 'dokument',
  other: 'ostatní',
  // CLIP fine sub-styles (data/clip_taxonomy.json) that collapse into the above.
  situation_plan: 'situační plán',
  cadastral_map: 'katastrální mapa',
  aerial_plot: 'letecký snímek',
  location_map: 'mapa lokality',
  energy_certificate: 'energetický průkaz',
  document_text: 'dokument',
};

/** Czech display label for a CLIP/room tag; falls back to the raw tag, null for none. */
export function imageTagLabel(tag: string | null | undefined): string | null {
  if (!tag) return null;
  return IMAGE_TAG_LABELS[tag] ?? tag;
}

/** The 2 logical tags that exist ONLY as a `collapse` target of other fine tags
 * (data/clip_taxonomy.json) — they share a Czech label with their fine child
 * (site_plan/situation_plan both "situační plán"; property_document/document_text
 * both "dokument") and are never a value CLIP predicts directly. */
const COLLAPSE_ONLY_TAGS = new Set(['site_plan', 'property_document']);

/** The 19 keys CLIP can actually predict as fine_tag (data/clip_taxonomy.json's
 * `prompts` keys) — every IMAGE_TAG_LABELS entry except the 2 collapse-only logical
 * tags above, so every remaining entry has a label unique to it. This is the axis a
 * classifier trained on the frozen embeddings should learn: logical_tag is a
 * deterministic post-hoc collapse of fine_tag (see `collapse` in the taxonomy file),
 * so training on the finer class loses nothing and stays collapsible later. */
export const FINE_TAG_KEYS = Object.keys(IMAGE_TAG_LABELS).filter(
  (k) => !COLLAPSE_ONLY_TAGS.has(k),
);

/** The photos a Browse card can open on. Each option names the trained DINOv3
 * head that decides it (`headTagId`, a tag_taxonomy id) and the CLIP fine tag
 * that decides it where that head has not scored the photo. An option whose
 * head the active model lacks (ložnice, WC, …) is CLIP's until a model with that
 * head is activated — then it is the head's, with no change here. Left out:
 * tags nobody wants as a cover (chodba, schodiště, documents, technical rooms). */
export const COVER_OPTIONS = [
  { key: 'default', label: 'Default', headTagId: null, clipTag: null },
  { key: 'exterior_facade', label: 'Fasáda', headTagId: 3, clipTag: 'exterior_facade' },
  { key: 'kitchen', label: 'Kuchyně', headTagId: 25, clipTag: 'kitchen' },
  { key: 'living_room', label: 'Obývací pokoj', headTagId: 28, clipTag: 'living_room' },
  { key: 'bedroom', label: 'Ložnice', headTagId: 26, clipTag: 'bedroom' },
  { key: 'bathroom', label: 'Koupelna', headTagId: 22, clipTag: 'bathroom' },
  { key: 'toilet', label: 'WC', headTagId: 36, clipTag: 'toilet' },
  { key: 'balcony_terrace', label: 'Balkon/terasa', headTagId: 1, clipTag: 'balcony_terrace' },
  { key: 'garden', label: 'Zahrada', headTagId: 8, clipTag: 'garden' },
  { key: 'garage', label: 'Garáž', headTagId: 17, clipTag: null },
  { key: 'floor_plan', label: 'Půdorys', headTagId: 46, clipTag: 'floor_plan' },
  { key: 'plan_3d', label: '3D plán', headTagId: 39, clipTag: null },
  { key: 'site_plan', label: 'Situační plán', headTagId: null, clipTag: 'situation_plan' },
  { key: 'cadastral_map', label: 'Katastrální mapa', headTagId: 42, clipTag: 'cadastral_map' },
  { key: 'aerial_plot', label: 'Letecký snímek', headTagId: 43, clipTag: 'aerial_plot' },
] as const;
export type CoverTag = (typeof COVER_OPTIONS)[number]['key'];
export const COVER_TAGS: readonly CoverTag[] = COVER_OPTIONS.map((o) => o.key);

/** A head's yes: the threshold every v1 head was trained at. */
export const HEAD_SCORE_FLOOR = 0.5;

/** The fields of an images_public row the cover choice reads. */
export interface CoverPhoto {
  clip_fine_tag: string | null;
  clip_confidence: number | null;
  /** The active tag model's per-head scores (migration 591); null = not scored. */
  tag_head_scores?: Record<string, number> | null;
}

/** Index of the cover photo for `tag`, else 0 — an ad without that photo keeps
 * its own first photo. Per photo, the trained head decides wherever the active
 * model scored it (a yes at HEAD_SCORE_FLOOR, a no below it, whatever CLIP
 * says); CLIP decides only where the head has not. A head's yes outranks any
 * CLIP match; within each, the higher score wins, the earlier photo on a tie. */
export function coverIndex(photos: ReadonlyArray<CoverPhoto>, tag: CoverTag): number {
  const option = COVER_OPTIONS.find((o) => o.key === tag);
  if (option == null || option.key === 'default') return 0;
  let best = 0;
  let bestRank = -1;
  photos.forEach((p, i) => {
    const headScore =
      option.headTagId == null ? undefined : p.tag_head_scores?.[String(option.headTagId)];
    let rank: number;
    if (headScore != null) {
      if (headScore < HEAD_SCORE_FLOOR) return;
      rank = 1 + headScore;
    } else if (option.clipTag != null && p.clip_fine_tag === option.clipTag) {
      rank = p.clip_confidence ?? 0;
    } else {
      return;
    }
    if (rank > bestRank) {
      best = i;
      bestRank = rank;
    }
  });
  return best;
}

/** A render-ready image plus its CLIP tag — the shape the photo carousels consume. */
export interface TaggedImageUrl {
  url: string;
  /** CLIP fine_tag (the displayed label key), or null when not yet tagged. */
  tag: string | null;
  /** CLIP softmax confidence 0..1 of the winning anchor, for the tooltip. */
  confidence: number | null;
  /** CLIP render-vs-photo score 0..1 (migration 239); null until scored. */
  renderScore: number | null;
}
