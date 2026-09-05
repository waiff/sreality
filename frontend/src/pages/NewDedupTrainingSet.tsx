import { useMemo, useState } from 'react';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import {
  listTrainingSet,
  listTrainingSetHeads,
  setNewDedupTagAnnotation,
  type TagState,
  type TrainingSetRow,
} from '@/lib/api';
import { imageSrc, type ImageRef } from '@/lib/imageUrl';
import { splitTagLabel } from '@/lib/tagLabel';
import Spinner from '@/components/Spinner';
import ErrorBanner from '@/components/ErrorBanner';
import ImageSizeToggle from '@/components/ImageSizeToggle';
import { pushToast } from '@/lib/toast';

/* NEW DEDUP · Training set — what the model built, read head by head.
 *
 * The labeling page's tag grid could almost do this and deliberately is not
 * reused: it is built around the candidate queue (now empty), it cannot page,
 * and it cannot tell the operator's own labels from the machine's. Reviewing
 * ten thousand machine labels is a different job from working a queue, so it
 * gets a surface shaped for that job — pick a head, pick a verdict, scan.
 *
 * A correction here is a HUMAN label and therefore final: the store's
 * human-wins rail means a later machine pass can never overwrite it. That is
 * the point of reviewing at all, and it is why the tile says who decided.
 *
 * THE HOLDOUT IS NOT HERE. Those 250 images grade the model; correcting one
 * from a training surface would quietly train on the yardstick. The server
 * excludes them and this page says so, so their absence reads as a rule rather
 * than a gap.
 */

type Verdict = 'positive' | 'negative' | 'excluded';
type SourceFilter = 'all' | 'machine' | 'human';

const PAGE = 60;

const VERDICTS: ReadonlyArray<{ key: Verdict | 'all'; label: string }> = [
  { key: 'positive', label: 'Applies' },
  { key: 'negative', label: 'Does not' },
  { key: 'excluded', label: 'Left out' },
  { key: 'all', label: 'All' },
];

const SOURCES: ReadonlyArray<{ key: SourceFilter; label: string; title: string }> = [
  { key: 'all', label: 'Anyone', title: 'Every label in the training set' },
  { key: 'machine', label: 'Machine', title: 'Written by the model from your definitions' },
  { key: 'human', label: 'Yours', title: 'Your own labels — a machine pass can never overwrite these' },
];

const VERDICT_STYLE: Record<Verdict, string> = {
  positive: 'border-[var(--color-sage)] bg-[var(--color-sage)]/10',
  negative: 'border-[var(--color-rule)]',
  excluded: 'border-dashed border-[var(--color-copper)]',
};

export default function NewDedupTrainingSet() {
  const [params, setParams] = useSearchParams();
  const qc = useQueryClient();
  const [large, setLarge] = useState(false);

  const tagId = Number(params.get('tag') ?? 0) || null;
  const verdict = (params.get('verdict') ?? 'positive') as Verdict | 'all';
  const source = (params.get('source') ?? 'all') as SourceFilter;
  const offset = Math.max(0, Number(params.get('offset') ?? 0) || 0);

  const patch = (next: Record<string, string | null>) => {
    const merged = new URLSearchParams(params);
    for (const [k, v] of Object.entries(next)) {
      if (v === null) merged.delete(k);
      else merged.set(k, v);
    }
    setParams(merged, { replace: true });
  };

  const headsQ = useQuery({
    queryKey: ['training-set-heads'],
    queryFn: () => listTrainingSetHeads(),
  });
  const heads = headsQ.data?.data ?? [];
  /* Heads the model has actually produced work for come first: with eighteen
   * routing tags and twelve labeled, an alphabetical list buries the answer. */
  const ordered = useMemo(
    () => [...heads].sort((a, b) => b.positive - a.positive || a.label.localeCompare(b.label)),
    [heads],
  );
  const activeId = tagId ?? ordered[0]?.id ?? null;
  const activeHead = ordered.find((h) => h.id === activeId) ?? null;

  const rowsQ = useQuery({
    queryKey: ['training-set', activeId, verdict, source, offset],
    queryFn: () =>
      listTrainingSet({
        tag_id: activeId as number,
        ...(verdict === 'all' ? {} : { state: verdict }),
        ...(source === 'all' ? {} : { source }),
        limit: PAGE,
        offset,
      }),
    enabled: activeId != null,
  });

  const rows = rowsQ.data?.data.rows ?? [];
  const counts = rowsQ.data?.data.counts;

  const correctMut = useMutation({
    mutationFn: ({ imageId, state }: { imageId: number; state: TagState }) =>
      setNewDedupTagAnnotation(
        activeId as number, imageId, state,
        state === 'excluded' ? 'pruned' : null,
      ),
    onSuccess: () => {
      /* Refetch the page, not the whole surface: the head counts move too, and
       * a reviewer who corrects a tile expects the totals to agree. */
      qc.invalidateQueries({ queryKey: ['training-set'] });
      qc.invalidateQueries({ queryKey: ['training-set-heads'] });
    },
    onError: (e: Error) => pushToast('err', e.message),
  });

  if (headsQ.isLoading) return <div className="p-6"><Spinner /></div>;
  if (headsQ.error) {
    return <div className="p-6"><ErrorBanner message={(headsQ.error as Error).message} /></div>;
  }

  const tile = (r: TrainingSetRow) => {
    const mine = r.source !== 'machine';
    /* Every row here HAS an R2 copy (the read requires storage_path), so the
     * portal fallback is unreachable — but the shape is real, not a cast. */
    const ref: ImageRef = { storage_path: r.storage_path, sreality_url: '' };
    return (
      <li
        key={r.image_id}
        data-testid={`training-tile-${r.image_id}`}
        data-state={r.state}
        className={`rounded-[var(--radius-sm)] border p-1.5 flex flex-col gap-1.5 ${VERDICT_STYLE[r.state as Verdict] ?? ''}`}
      >
        <a href={imageSrc(ref)} target="_blank" rel="noreferrer">
          <img
            src={imageSrc(ref)}
            alt={`Training image ${r.image_id}`}
            loading="lazy"
            className={`w-full object-cover rounded-[var(--radius-xs)] ${large ? 'h-56' : 'h-28'}`}
          />
        </a>
        <div className="flex items-center gap-1 text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)]">
          <span title={mine ? 'Your label — no machine pass can overwrite it' : 'Written by the model'}>
            {mine ? 'yours' : 'machine'}
          </span>
          {r.definition_stale && (
            <span
              className="text-[var(--color-copper)]"
              title="Written under wording you have since replaced. Not necessarily wrong — but it followed a rule that has changed."
            >
              · old wording
            </span>
          )}
        </div>
        <div className="flex gap-1">
          {(['positive', 'negative', 'excluded'] as const).map((v) => (
            <button
              key={v}
              type="button"
              aria-label={`${v} ${r.image_id}`}
              aria-pressed={r.state === v}
              title={v === 'positive' ? 'Applies' : v === 'negative' ? 'Does not apply' : 'Leave out'}
              disabled={correctMut.isPending}
              onClick={() => correctMut.mutate({ imageId: r.image_id, state: v })}
              className={`flex-1 py-0.5 text-[0.7rem] rounded-[var(--radius-xs)] border transition-colors ${
                r.state === v
                  ? 'border-[var(--color-ink-2)] text-[var(--color-ink)]'
                  : 'border-[var(--color-rule)] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)]'
              }`}
            >
              {v === 'positive' ? '✓' : v === 'negative' ? '✕' : '–'}
            </button>
          ))}
        </div>
      </li>
    );
  };

  return (
    <div className="max-w-[112rem] mx-auto px-4 py-6">
      <header className="border-b border-[var(--color-rule)] pb-3">
        <div className="flex items-baseline justify-between flex-wrap gap-3">
          <div>
            <h1 className="text-lg font-medium text-[var(--color-ink)]">Training set</h1>
            <p className="text-xs text-[var(--color-ink-3)] mt-0.5">
              What the model labeled from your definitions, with your own labels beside it.
              A correction here is yours and final — no machine pass can overwrite it.
              The sealed exam images are excluded: they grade the model, so they never appear
              on a training surface.
            </p>
          </div>
          <ImageSizeToggle
            large={large}
            onChange={setLarge}
            label="Training grid image size"
          />
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <label className="text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)]" htmlFor="head">
            head
          </label>
          <select
            id="head"
            value={activeId ?? ''}
            onChange={(e) => patch({ tag: e.target.value, offset: null })}
            className="px-2 py-1 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-transparent text-[var(--color-ink)]"
          >
            {ordered.map((h) => (
              <option key={h.id} value={h.id}>
                {splitTagLabel(h.label).name} · {h.positive}
              </option>
            ))}
          </select>

          <span className="flex gap-1" role="group" aria-label="verdict">
            {VERDICTS.map((v) => (
              <button
                key={v.key}
                type="button"
                aria-pressed={verdict === v.key}
                onClick={() => patch({ verdict: v.key, offset: null })}
                className={`px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border ${
                  verdict === v.key
                    ? 'border-[var(--color-ink-2)] text-[var(--color-ink)]'
                    : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]'
                }`}
              >
                {v.label}
                {counts && v.key !== 'all' && (
                  <span className="ml-1 text-[var(--color-ink-4)]">{counts[v.key]}</span>
                )}
              </button>
            ))}
          </span>

          <span className="flex gap-1" role="group" aria-label="decided by">
            {SOURCES.map((s) => (
              <button
                key={s.key}
                type="button"
                title={s.title}
                aria-pressed={source === s.key}
                onClick={() => patch({ source: s.key, offset: null })}
                className={`px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border ${
                  source === s.key
                    ? 'border-[var(--color-copper)] text-[var(--color-ink)]'
                    : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]'
                }`}
              >
                {s.label}
              </button>
            ))}
          </span>
        </div>

        {activeHead && (
          <p className="mt-2 text-xs text-[var(--color-ink-3)]">
            {activeHead.positive} applies · {activeHead.machine_positive} by the machine,{' '}
            {activeHead.human_positive} yours
          </p>
        )}
      </header>

      {rowsQ.isLoading ? (
        <div className="py-10 flex justify-center"><Spinner /></div>
      ) : rowsQ.error ? (
        <div className="mt-4"><ErrorBanner message={(rowsQ.error as Error).message} /></div>
      ) : rows.length === 0 ? (
        <p className="mt-10 text-center text-sm text-[var(--color-ink-2)]">
          Nothing labeled for this head and filter yet.
        </p>
      ) : (
        <>
          <ul
            className="mt-4 grid gap-2"
            style={{
              gridTemplateColumns: `repeat(auto-fill, minmax(${large ? '16rem' : '8rem'}, 1fr))`,
            }}
          >
            {rows.map(tile)}
          </ul>
          <div className="mt-4 flex items-center justify-center gap-3 text-xs">
            <button
              type="button"
              disabled={offset === 0}
              onClick={() => patch({ offset: String(Math.max(0, offset - PAGE)) })}
              className="px-3 py-1 rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-3)] disabled:opacity-40"
            >
              ← previous
            </button>
            <span className="text-[var(--color-ink-4)]">
              {offset + 1}–{offset + rows.length}
            </span>
            <button
              type="button"
              disabled={rows.length < PAGE}
              onClick={() => patch({ offset: String(offset + PAGE) })}
              className="px-3 py-1 rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-3)] disabled:opacity-40"
            >
              next →
            </button>
          </div>
        </>
      )}
    </div>
  );
}
