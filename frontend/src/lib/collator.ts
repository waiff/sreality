/* THE Czech collator.
 *
 * Czech collation is not the default: 'Č' sorts after 'C' (not after 'Z'),
 * 'Ch' is a single letter sorting after 'H', and 'Š'/'Ř'/'Ž' each have their
 * own place. `String.prototype.localeCompare` without an explicit locale uses
 * the *runtime's* locale, so the same board sorts differently on a Czech
 * laptop and a CI box — the app had 16 ad-hoc localeCompare call sites, 8 of
 * them locale-less. Every user-visible string sort goes through here instead.
 *
 * `numeric: true` so "Praha 2" sorts before "Praha 10" rather than after it —
 * district and street labels are full of embedded numbers.
 */

export const csCollator = new Intl.Collator('cs', {
  numeric: true,
  sensitivity: 'base',
});

/** Compare two possibly-null strings, nulls last. */
export const compareCs = (
  a: string | null | undefined,
  b: string | null | undefined,
): number => {
  const av = a?.trim() || null;
  const bv = b?.trim() || null;
  if (av === bv) return 0;
  if (av == null) return 1;
  if (bv == null) return -1;
  return csCollator.compare(av, bv);
};
