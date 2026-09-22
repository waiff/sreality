/* Collection membership as a COHORT — one definition, rendered for whichever
 * surface asks: a property-id allowlist for Browse, an in-memory predicate for
 * the pipeline board. Null = no constraint, [] = a selection that matched
 * nothing; collapsing the two shows the whole market to an empty selection. */

/** The member map reduced to the property ids the selection admits. */
export const propertyIdsInCollections = (
  members: ReadonlyMap<number, number[]>,
  selected: readonly number[],
): number[] | null => {
  if (!selected.length) return null;
  const wanted = new Set(selected);
  const ids: number[] = [];
  for (const [property_id, collectionIds] of members) {
    if (collectionIds.some((id) => wanted.has(id))) ids.push(property_id);
  }
  return ids;
};

/** The same OR rule per property: `undefined` memberIds = in no collection. */
export const matchesCollections = (
  memberIds: readonly number[] | undefined,
  selected: readonly number[],
): boolean => {
  if (!selected.length) return true;
  if (!memberIds) return false;
  return memberIds.some((id) => selected.includes(id));
};
