import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import BorderCaseButton from '@/components/BorderCaseButton';
import LabelCombobox, { type LabelOption } from '@/components/LabelCombobox';
import {
  setTrainingExample,
  deleteTrainingExample,
  type TrainingExample,
} from '@/lib/api';
import type { ImagePublic } from '@/lib/types';
import type { BorderCaseStore } from '@/lib/useBorderCases';

/* The linear-probe training-set "Train" CTA, used by /clip-audit (it was extracted
 * when a second labeling surface carried a byte-for-byte copy of this state/mutation
 * logic and the two drifted out of sync; `queryKeyPrefix` keeps it page-agnostic for
 * the Wave-1 Labeling page). Defaults to the
 * CLIP-assigned fine_tag KEY (not its Czech translation — see LabelCombobox/imageTags
 * for why identity has to be the canonical key), so confirming a correct call is one
 * click; `labelOptions` renders the Czech text.
 *
 * "Border case" (to Train's right) is a separate, independent flag — not a label
 * itself: even a human isn't confident how to classify this image, and it may or may
 * not also carry a best-guess training label. It lives in the shared
 * `BorderCaseButton` + `useBorderCases`, which the NEW DEDUP Labeling grid renders
 * too, so the two surfaces stay one behavior. */
export default function TrainControl({
  image,
  example,
  borderCases,
  labelOptions,
  queryKeyPrefix,
  onChanged,
}: {
  image: ImagePublic;
  example: TrainingExample | undefined;
  borderCases: BorderCaseStore;
  labelOptions: ReadonlyArray<LabelOption>;
  queryKeyPrefix: string;
  // Fires after a Train/untrain write lands, in addition to the invalidation
  // below — for a caller whose own list is scoped narrower than
  // [queryKeyPrefix,'training'] (e.g. /clip-audit's per-label browser, where a
  // relabel must drop the image from the label it just left, not only refresh
  // counts).
  onChanged?: () => void;
}) {
  const qc = useQueryClient();
  const defaultValue = image.clip_fine_tag ?? '';
  const [value, setValue] = useState(example?.label ?? defaultValue);

  // Re-sync when the saved example changes (e.g. after invalidation confirms a
  // write, or an example trained from a different session loads in).
  useEffect(() => {
    if (example?.label != null) setValue(example.label);
  }, [example?.label]);

  const trained = !!example;

  // Invalidate both this page-group's training examples AND the page-wide
  // training-labels query (LabelCombobox's suggestions, the per-label counts) — a
  // fresh Train/untrain changes both, and the latter otherwise only resyncs after
  // its 30s staleTime lapses.
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: [queryKeyPrefix, 'training'] });
    qc.invalidateQueries({ queryKey: [queryKeyPrefix, 'training-labels'] });
    onChanged?.();
  };
  const train = useMutation({
    mutationFn: () => setTrainingExample({ image_id: image.id, label: value }),
    onSuccess: invalidate,
  });
  const untrain = useMutation({
    mutationFn: () => deleteTrainingExample(image.id),
    onSuccess: invalidate,
  });

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-1.5">
        <div className="min-w-[6rem] flex-1">
          <LabelCombobox value={value} onChange={setValue} options={labelOptions} />
        </div>
        <button
          type="button"
          onClick={() => train.mutate()}
          disabled={train.isPending || value.trim().length === 0}
          title={trained ? `V trénovací sadě: „${example.label}“` : 'Přidat do trénovací sady s tímto štítkem'}
          className={[
            'shrink-0 px-2 py-1 text-[0.72rem] rounded-[var(--radius-xs)] border transition-colors disabled:opacity-50',
            trained
              ? 'border-[var(--color-sage)] bg-[var(--color-sage-soft)] text-[var(--color-sage)]'
              : 'border-[var(--color-copper)] text-[var(--color-copper)] hover:bg-[var(--color-copper-soft)]',
          ].join(' ')}
        >
          {train.isPending ? '…' : trained ? '✓ Train' : 'Train'}
        </button>
        <BorderCaseButton imageId={image.id} store={borderCases} />
      </div>
      {trained && (
        <button
          type="button"
          onClick={() => untrain.mutate()}
          disabled={untrain.isPending}
          className="self-start text-[0.68rem] text-[var(--color-ink-4)] hover:text-[var(--color-brick)] underline decoration-dotted underline-offset-2 disabled:opacity-50"
        >
          {untrain.isPending ? 'Odebírám…' : 'Odebrat z trénovací sady'}
        </button>
      )}
    </div>
  );
}
