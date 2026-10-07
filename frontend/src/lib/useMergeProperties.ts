/* Browse's merge of the ticked properties (MS15): one click, then the one
 * receipt with "Otevřít #S" (`pushMergeReceipt`), the read-your-writes refresh
 * (the merge txn patched browse_list and moved the curation onto the survivor)
 * and `onMerged`. It declares no onError, so a refusal, rule 15's Czech
 * sentence included, is the app MutationCache's one toast (lib/mutationCache).
 * Before the click, `useMergePreview` reads how many "Různé" rulings the merge
 * would take back (MS12). */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { getMergePreview, mergePropertySet, type MergePreview } from '@/lib/api';
import { mergedAdvertsKeys, pushMergeReceipt, refreshAfterSplit } from '@/lib/mergedAdverts';
import { ROUTES } from '@/lib/routes';

export function useMergeProperties(onMerged: () => void) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  return useMutation({
    mutationFn: (propertyIds: number[]) => mergePropertySet(propertyIds),
    onSuccess: (res) => {
      pushMergeReceipt(res, (id) => navigate(ROUTES.property.build({ propertyId: id })));
      refreshAfterSplit(qc);
      onMerged();
    },
  });
}

/* MS12's count before the click, read once two or more properties are ticked. */
export function useMergePreview(propertyIds: Iterable<number>) {
  const ids = [...new Set(propertyIds)].sort((a, b) => a - b);
  return useQuery<MergePreview, Error>({
    queryKey: mergedAdvertsKeys.mergePreview(ids),
    queryFn: () => getMergePreview(ids),
    enabled: ids.length >= 2,
    staleTime: 30_000,
  });
}
