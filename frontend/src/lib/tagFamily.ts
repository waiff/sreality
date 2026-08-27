/* Families exist only as a " - " prefix in the label text ("interier - kuchyne")
 * — tag_taxonomy.family is NULL on every live row. Prefer the column when it is
 * ever populated, fall back to the prefix, then to one catch-all bucket. */
export const FAMILY_FALLBACK = 'ostatní';

export const tagFamily = (tag: { label: string; family: string | null }): string => {
  const explicit = (tag.family ?? '').trim();
  if (explicit) return explicit;
  const idx = tag.label.indexOf(' - ');
  return idx > 0 ? tag.label.slice(0, idx).trim() : FAMILY_FALLBACK;
};

/* The part after the family prefix, for a list that already shows the family as
 * a group heading. Falls back to the whole label. */
export const tagShortLabel = (label: string): string => {
  const idx = label.indexOf(' - ');
  return idx > 0 ? label.slice(idx + 3).trim() : label;
};
