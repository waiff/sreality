/* Tag labels carry their family in the text: "interier - koupelna",
 * "podklad - letecký snímek s ohraničením subjektu".
 *
 * THE FAMILY IS PART OF THE TAG. It is rendered inline, at the same size as
 * the name, on every surface. An earlier version demoted it to a small eyebrow
 * above the name to save width — and that was measured to fail on the one pair
 * that differs ONLY by family: "exterier - domovní vchod" (from the street)
 * versus "interier - domovní vchod / chodba" (from inside). A reader, human or
 * otherwise, skimmed past the eyebrow and could not tell them apart. So this
 * helper exists only to let a surface tint the family differently; it never
 * licenses dropping or shrinking it, and the full label stays the accessible
 * name everywhere.
 */

const FAMILY = /^(interier|exterier|podklad)\s*-\s*(.+)$/i;

export function splitTagLabel(label: string): { family: string | null; name: string } {
  const m = FAMILY.exec(label.trim());
  if (!m) return { family: null, name: label };
  return { family: m[1].toLowerCase(), name: m[2] };
}
