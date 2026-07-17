import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import LabelCombobox, { type LabelOption } from '@/components/LabelCombobox';
import { setTrainingExample, deleteTrainingExample, type TrainingExample } from '@/lib/api';
import type { ImagePublic } from '@/lib/types';

/* The linear-probe training-set "Train" CTA — shared by /phash-audit and /clip-audit
 * (both pages were carrying a byte-for-byte copy of this state/mutation logic before,
 * which is exactly how the two of them drifted out of sync). Defaults to the
 * CLIP-assigned fine_tag KEY (not its Czech translation — see LabelCombobox/imageTags
 * for why identity has to be the canonical key), so confirming a correct call is one
 * click; `labelOptions` renders the Czech text. */
export default function TrainControl({
  image,
  example,
  labelOptions,
  queryKeyPrefix,
}: {
  image: ImagePublic;
  example: TrainingExample | undefined;
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

  const train = useMutation({
    mutationFn: () => setTrainingExample({ image_id: image.id, label: value }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [queryKeyPrefix, 'training'] }),
  });
  const untrain = useMutation({
    mutationFn: () => deleteTrainingExample(image.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: [queryKeyPrefix, 'training'] }),
  });

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
