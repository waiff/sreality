/* Browse's merge of the ticked properties (MS15): one click, then the one
 * receipt with "Otevřít #S" (`pushMergeReceipt`), the read-your-writes refresh
 * (the merge txn patched browse_list and moved the curation onto the survivor)
 * and `onMerged`. It declares no onError, so a refusal, rule 15's Czech
 * sentence included, is the app MutationCache's one toast (lib/mutationCache). */

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { mergePropertySet } from '@/lib/api';
import { pushMergeReceipt, refreshAfterSplit } from '@/lib/mergedAdverts';
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
