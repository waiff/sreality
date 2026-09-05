import { useMemo, useState } from 'react';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import {
  listTrainingSet,
  listTrainingSetHeads,
  setNewDedupTagAnnotation,
  setTrainingTarget,
  type TagState,
  type TrainingSetRow,
} from '@/lib/api';
import { imageSrc, type ImageRef } from '@/lib/imageUrl';
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
/* The cutoff view. 'review' is the bounded job: in-set positives that are still
 * the machine's word alone. 'set' is everything a probe trains on; 'reserve' is
 * what steps in when a set positive is removed. */
type Membership = 'review' | 'set' | 'reserve' | 'all';

const MEMBERSHIPS: ReadonlyArray<{ key: Membership; label: string; title: string }> = [
  { key: 'review', label: 'To review', title: 'In the set, still on the machine’s word alone — the bounded job' },
  { key: 'set', label: 'In set', title: 'What a probe trains on: your positives first, then the machine’s oldest-first, up to the target' },
  { key: 'reserve', label: 'Reserve', title: 'Past the target. Steps into the set automatically when a set positive is removed' },
  { key: 'all', label: 'Everything', title: 'No cutoff' },
];

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
  /* Tiles whose mark changed in THIS session, with what they showed before —
   * the note field appears only there, and from_state travels with the note
   * so the reason is recorded against the change it explains. */
  const [changed, setChanged] = useState<Map<number, { from: TagState; to: TagState }>>(new Map());
  const [drafts, setDrafts] = useState<Map<number, string>>(new Map());

  const tagId = Number(params.get('tag') ?? 0) || null;
  const verdict = (params.get('verdict') ?? 'positive') as Verdict | 'all';
  const source = (params.get('source') ?? 'all') as SourceFilter;
  /* Opens on the bounded review by default: that is the work worth doing. */
  const membership = (params.get('set') ?? 'review') as Membership;
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

  /* 'review' = set membership + machine source + positive verdict, composed
   * from the three server filters so it can never disagree with them. */
  const effective = membership === 'review'
    ? { verdict: 'positive' as const, source: 'machine' as const, member: 'set' as const }
    : { verdict, source, member: membership === 'all' ? null : membership };

  const rowsQ = useQuery({
    queryKey: ['training-set', activeId, effective.verdict, effective.source, effective.member, offset],
    queryFn: () =>
      listTrainingSet({
        tag_id: activeId as number,
        ...(effective.verdict === 'all' ? {} : { state: effective.verdict }),
        ...(effective.source === 'all' ? {} : { source: effective.source }),
        ...(effective.member ? { membership: effective.member } : {}),
        limit: PAGE,
        offset,
      }),
    enabled: activeId != null,
  });

  const targetMut = useMutation({
    mutationFn: ({ tagId, target }: { tagId: number; target: number | null }) =>
      setTrainingTarget(tagId, target),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['training-set'] });
      qc.invalidateQueries({ queryKey: ['training-set-heads'] });
      pushToast('ok', res.data.is_default
        ? `Target back to the default (${res.data.target})`
        : `Target ${res.data.target}`);
    },
    onError: (e: Error) => pushToast('err', e.message),
  });

  const rows = rowsQ.data?.data.rows ?? [];
  const counts = rowsQ.data?.data.counts;

  const correctMut = useMutation({
    mutationFn: ({ imageId, state }: { imageId: number; state: TagState; from: TagState }) =>
      setNewDedupTagAnnotation(
        activeId as number, imageId, state,
        state === 'excluded' ? 'pruned' : null,
      ),
    onSuccess: (_res, vars) => {
      setChanged((prev) => new Map(prev).set(vars.imageId, {
        from: prev.get(vars.imageId)?.from ?? vars.from, to: vars.state,
      }));
      /* Refetch the page, not the whole surface: the head counts move too, and
       * a reviewer who corrects a tile expects the totals to agree. */
      qc.invalidateQueries({ queryKey: ['training-set'] });
      qc.invalidateQueries({ queryKey: ['training-set-heads'] });
    },
    onError: (e: Error) => pushToast('err', e.message),
  });

  /* The reason, sent as a re-statement of the SAME mark with the note attached
   * — one write path for mark and reason, so they cannot drift apart. */
  const noteMut = useMutation({
    mutationFn: ({ imageId, text }: { imageId: number; text: string }) => {
      const ch = changed.get(imageId);
      if (!ch) throw new Error('note without a change');
      return setNewDedupTagAnnotation(
        activeId as number, imageId, ch.to,
        ch.to === 'excluded' ? 'pruned' : null,
        { text, from_state: ch.from },
      );
    },
    onSuccess: (_res, vars) => {
      setDrafts((prev) => { const n = new Map(prev); n.delete(vars.imageId); return n; });
      setChanged((prev) => { const n = new Map(prev); n.delete(vars.imageId); return n; });
      pushToast('ok', 'Note saved');
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
        <a
          href={imageSrc(ref)}
          target="_blank"
          rel="noreferrer"
          title="Open the full-size photo in a new tab"
          className={`block bg-[var(--color-paper-2)] rounded-[var(--radius-xs)] ${large ? 'h-56' : 'h-28'}`}
        >
          {/* The WHOLE photo, letterboxed on a quiet ground — a review of what
            * a photo is OF cannot be done on a crop that hides the edges. */}
          <img
            src={imageSrc(ref)}
            alt={`Training image ${r.image_id}`}
            loading="lazy"
            className="w-full h-full object-contain rounded-[var(--radius-xs)]"
          />
        </a>
        <div className="flex items-center gap-1 text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)]">
          <span title={mine ? 'Your label — no machine pass can overwrite it' : 'Written by the model'}>
            {mine ? 'yours' : 'machine'}
          </span>
          {r.state === 'positive' && (
            <span
              data-testid={`membership-${r.image_id}`}
              className={r.in_set ? 'text-[var(--color-sage)]' : ''}
              title={r.in_set
                ? `In the set at position ${r.set_rank}`
                : `Reserve, position ${r.set_rank} — steps in when a set positive is removed`}
            >
              · {r.in_set ? 'in set' : 'reserve'}
            </span>
          )}
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
              title={v === 'positive'
                ? (r.state === 'positive' && !mine ? 'Confirm — makes this your label' : 'Applies')
                : v === 'negative' ? 'Does not apply' : 'Leave out'}
              disabled={correctMut.isPending}
              onClick={() => correctMut.mutate({ imageId: r.image_id, state: v, from: r.state })}
              className={`flex-1 py-0.5 text-[0.65rem] whitespace-nowrap rounded-[var(--radius-xs)] border transition-colors ${
                r.state === v
                  ? 'border-[var(--color-ink-2)] text-[var(--color-ink)]'
                  : 'border-[var(--color-rule)] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)]'
              }`}
            >
              {v === 'positive' ? '✓ applies' : v === 'negative' ? '✕ no' : '– left out'}
            </button>
          ))}
        </div>
        {changed.has(r.image_id) && (
          <form
            data-testid={`note-form-${r.image_id}`}
            className="flex gap-1"
            onSubmit={(e) => {
              e.preventDefault();
              const text = (drafts.get(r.image_id) ?? '').trim();
              if (text) noteMut.mutate({ imageId: r.image_id, text });
            }}
          >
            <input
              aria-label={`why ${r.image_id}`}
              value={drafts.get(r.image_id) ?? ''}
              onChange={(e) => setDrafts((prev) => new Map(prev).set(r.image_id, e.target.value))}
              placeholder="why? (optional)"
              maxLength={600}
              className="min-w-0 flex-1 px-1.5 py-0.5 text-[0.75rem] rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-transparent text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)]"
            />
            <button
              type="submit"
              disabled={noteMut.isPending || !(drafts.get(r.image_id) ?? '').trim()}
              className="px-2 py-0.5 text-[0.7rem] rounded-[var(--radius-xs)] border border-[var(--color-copper)] text-[var(--color-ink)] disabled:opacity-40"
            >
              save
            </button>
          </form>
        )}
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
              After you change a mark, a small field appears to say why; those reasons are
              distilled into the head&rsquo;s definition, as one general rule, not one line per note.
              Only the positives inside a head&rsquo;s cutoff need your eyes: &ldquo;To review&rdquo; is that list.
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
                {h.label} · {h.positive}
              </option>
            ))}
          </select>

          <span className="flex gap-1" role="group" aria-label="cutoff">
            {MEMBERSHIPS.map((m) => (
              <button
                key={m.key}
                type="button"
                title={m.title}
                aria-pressed={membership === m.key}
                onClick={() => patch({ set: m.key, offset: null })}
                className={`px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border ${
                  membership === m.key
                    ? 'border-[var(--color-sage)] text-[var(--color-ink)]'
                    : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]'
                }`}
              >
                {m.label}
                {activeHead && m.key === 'review' && (
                  <span className="ml-1 text-[var(--color-ink-4)]">{activeHead.in_set_unreviewed}</span>
                )}
                {activeHead && m.key === 'set' && (
                  <span className="ml-1 text-[var(--color-ink-4)]">{activeHead.in_set}/{activeHead.target}</span>
                )}
                {activeHead && m.key === 'reserve' && (
                  <span className="ml-1 text-[var(--color-ink-4)]">{activeHead.reserve}</span>
                )}
              </button>
            ))}
          </span>

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
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-[var(--color-ink-3)]">
            <span>
              {activeHead.positive} applies · {activeHead.machine_positive} by the machine,{' '}
              {activeHead.human_positive} yours
            </span>
            <form
              className="flex items-center gap-1"
              onSubmit={(e) => {
                e.preventDefault();
                const raw = (new FormData(e.currentTarget).get('target') as string).trim();
                const n = Number(raw);
                if (!raw) targetMut.mutate({ tagId: activeHead.id, target: null });
                else if (Number.isInteger(n) && n >= 1) targetMut.mutate({ tagId: activeHead.id, target: n });
              }}
            >
              <label htmlFor="target" className="text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)]">
                target
              </label>
              <input
                id="target"
                name="target"
                key={`${activeHead.id}-${activeHead.target}`}
                defaultValue={activeHead.target}
                inputMode="numeric"
                className="w-16 px-1.5 py-0.5 text-xs rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-transparent text-[var(--color-ink)]"
                title="How many positives make up this head’s set. Empty = the default. Changing it moves a boundary; nothing is copied."
              />
              <button
                type="submit"
                disabled={targetMut.isPending}
                className="px-2 py-0.5 text-[0.7rem] rounded-[var(--radius-xs)] border border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]"
              >
                set
              </button>
            </form>
            <span className="text-[var(--color-ink-4)]">
              set = your positives, then the machine’s oldest-first, up to the target; the
              reserve refills it when you remove one
            </span>
          </div>
        )}
      </header>

      <details className="mt-3 rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-2 text-xs text-[var(--color-ink-2)]">
        <summary className="cursor-pointer select-none text-[var(--color-ink)]">
          How to use this page
        </summary>
        <div className="mt-2 grid gap-3 md:grid-cols-2">
          <div>
            <p className="font-medium text-[var(--color-ink)]">What you are looking at</p>
            <p className="mt-0.5">
              The model read your definitions and labeled thousands of photos. Each head (a tag such as
              <i> kuchyně</i>) has its own training set. You do not need to check all of it, only the
              part inside the cutoff, and only the photos the machine decided alone.
            </p>
            <p className="mt-2 font-medium text-[var(--color-ink)]">The cutoff chips</p>
            <ul className="mt-0.5 list-disc pl-4 space-y-0.5">
              <li><b>To review</b> — photos in the set that only the machine has judged. This is your job; the number is how many remain.</li>
              <li><b>In set</b> — everything a classifier will train on: your positives first, then the machine’s oldest-first, up to the target.</li>
              <li><b>Reserve</b> — positives past the target. When you remove one from the set, the first reserve photo steps in automatically.</li>
              <li><b>Everything</b> — no cutoff; use the verdict and “decided by” filters freely.</li>
            </ul>
            <p className="mt-2 font-medium text-[var(--color-ink)]">Target</p>
            <p className="mt-0.5">
              How many positives make up a head’s set (default 300, about where a classifier stops
              improving). Change the number and press <b>set</b>; clear it to return to the default.
              Nothing is copied or relabeled, only the boundary moves.
            </p>
          </div>
          <div>
            <p className="font-medium text-[var(--color-ink)]">Each photo</p>
            <ul className="mt-0.5 list-disc pl-4 space-y-0.5">
              <li>Click the photo to open it full-size in a new tab.</li>
              <li><b>machine</b> / <b>yours</b> says who decided the current mark. <b>in set</b> / <b>reserve</b> says which side of the cutoff it is on.</li>
              <li><b>old wording</b> means the label was written under a definition you have since changed. Not necessarily wrong, worth a look.</li>
              <li><b>✓ applies</b> — the photo is of this head. On a machine tile, pressing the already-pressed ✓ <i>confirms</i> it as your own label.</li>
              <li><b>✕ no</b> — the head does not apply. The photo leaves the set and the reserve refills it.</li>
              <li><b>– left out</b> — the subject is there but the photo is of something else. Trains nothing, grades nothing.</li>
            </ul>
            <p className="mt-2 font-medium text-[var(--color-ink)]">Why? field</p>
            <p className="mt-0.5">
              After you change a mark, a small field appears. A short reason (“entrance door, facade is
              just the backdrop”) is enough. Notes are gathered per head and distilled into one general
              rule in the definition, never one line per note, so keep them short.
            </p>
            <p className="mt-2 font-medium text-[var(--color-ink)]">Verdict and “decided by”</p>
            <p className="mt-0.5">
              <b>Applies / Does not / Left out / All</b> filter by the current mark. <b>Anyone / Machine / Yours</b>
              filter by who made it. Both are ignored under <b>To review</b>, which is always machine positives in the set.
            </p>
          </div>
        </div>
      </details>

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
