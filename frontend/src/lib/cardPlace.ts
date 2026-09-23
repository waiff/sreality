/* Splits the ONE server-composed place label (migration 503,
 * `location_display_label`) into the two rows a board card prints: the street /
 * part-of-town, and the town.
 *
 * The label always ends `, <Obec>` when the town is known ("Radkovská 262/8,
 * Jihlava", "Závodí, Beroun"), or is the bare town, or — foreign — a country
 * code. A single truncating line therefore clips exactly the part the operator
 * triages by, so the card gives the town a row of its own. `obec` is the same
 * RÚIAN name the label was built from, which is what makes stripping it off the
 * tail safe; when the label does not end with it the label is kept whole rather
 * than guessed at. */

export interface CardPlace {
  /* Street + house number, or the part of town; null when the label IS the town. */
  head: string | null;
  town: string | null;
}

export function splitCardPlace(
  label: string | null | undefined,
  obec: string | null | undefined,
): CardPlace {
  const shown = label?.trim() || null;
  const town = obec?.trim() || null;
  if (!shown) return { head: null, town };
  if (!town) return { head: shown, town: null };
  if (shown === town) return { head: null, town };
  const tail = `, ${town}`;
  if (shown.endsWith(tail)) {
    return { head: shown.slice(0, -tail.length).trim() || null, town };
  }
  return { head: shown, town };
}
