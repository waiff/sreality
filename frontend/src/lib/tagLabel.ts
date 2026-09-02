/* Tag labels carry their family in the text: "interier - koupelna",
 * "podklad - letecký snímek s ohraničením subjektu". On a button that prefix
 * costs the width the actual name needs, and it repeats on nearly every button
 * in the set — so surfaces render it as a small eyebrow above the name instead
 * of inline.
 *
 * NOT a truncation and NOT a rename: the family is still shown, because two
 * tags differ ONLY by it — "exterier - domovní vchod" (photographed from the
 * street) versus "interier - domovní vchod / chodba" (from inside). Dropping
 * the prefix would make those two buttons read identically, which is exactly
 * the mislabel this whole programme exists to avoid. The full label stays the
 * button's accessible name.
 */

const FAMILY = /^(interier|exterier|podklad)\s*-\s*(.+)$/i;

export function splitTagLabel(label: string): { family: string | null; name: string } {
  const m = FAMILY.exec(label.trim());
  if (!m) return { family: null, name: label };
  return { family: m[1].toLowerCase(), name: m[2] };
}
