import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import LabelCombobox, { type LabelOption } from '@/components/LabelCombobox';
import {
  setTrainingExample,
  deleteTrainingExample,
  setBorderCase,
  deleteBorderCase,
  type TrainingExample,
} from '@/lib/api';
import type { ImagePublic } from '@/lib/types';

/* The linear-probe training-set "Train" CTA — shared by /phash-audit and /clip-audit
 * (both pages were carrying a byte-for-byte copy of this state/mutation logic before,
 * which is exactly how the two of them drifted out of sync). Defaults to the
 * CLIP-assigned fine_tag KEY (not its Czech translation — see LabelCombobox/imageTags
 * for why identity has to be the canonical key), so confirming a correct call is one
 * click; `labelOptions` renders the Czech text.
 *
 * "Border case" (to Train's right) is a separate, independent flag — not a label
 * itself: even a human isn't confident how to classify this image, and it may or may
 * not also carry a best-guess training label. A one-click toggle (unlike Train, it
 * has no value of its own to edit), so it needs no combobox and no separate
 * remove-link — clicking it again just unflags. */
export default function TrainControl({
  image,
  example,
  borderCase,
  labelOptions,
  queryKeyPrefix,
}: {
  image: ImagePublic;
  example: TrainingExample | undefined;
  borderCase: boolean;
  labelOptions: ReadonlyArray<LabelOption>;
  queryKeyPrefix: string;
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
  // training-labels query (LabelCombobox's suggestions, and PhashAudit's per-label
  // counts) — a fresh Train/untrain changes both, and the latter otherwise only
  // resyncs after its 30s staleTime lapses.
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: [queryKeyPrefix, 'training'] });
    qc.invalidateQueries({ queryKey: [queryKeyPrefix, 'training-labels'] });
  };
  const train = useMutation({
    mutationFn: () => setTrainingExample({ image_id: image.id, label: value }),
    onSuccess: invalidate,
  });
  const untrain = useMutation({
    mutationFn: () => deleteTrainingExample(image.id),
    onSuccess: invalidate,
  });

  const flagBorderCase = useMutation({
    mutationFn: () => setBorderCase(image.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: [queryKeyPrefix, 'border-cases'] }),
  });
  const unflagBorderCase = useMutation({
    mutationFn: () => deleteBorderCase(image.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: [queryKeyPrefix, 'border-cases'] }),
  });
  const borderCasePending = flagBorderCase.isPending || unflagBorderCase.isPending;

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-1.5">
        <div className="min-w-0 flex-1">
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
        <button
          type="button"
          onClick={() => (borderCase ? unflagBorderCase.mutate() : flagBorderCase.mutate())}
          disabled={borderCasePending}
          title={
            borderCase
              ? 'Sporný případ — klikni pro odebrání'
              : 'Nejasné i pro člověka — přidat do sporných případů'
          }
          className={[
            'shrink-0 px-2 py-1 text-[0.72rem] rounded-[var(--radius-xs)] border transition-colors disabled:opacity-50',
            borderCase
              ? 'border-[var(--color-brick)] bg-[var(--color-brick-soft)] text-[var(--color-brick)]'
              : 'border-[var(--color-brick)] text-[var(--color-brick)] hover:bg-[var(--color-brick-soft)]',
          ].join(' ')}
        >
          {borderCasePending ? '…' : borderCase ? '✓ Border case' : 'Border case'}
        </button>
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
