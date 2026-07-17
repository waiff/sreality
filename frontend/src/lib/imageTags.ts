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
