import { useId, useMemo, useState } from 'react';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import {
  deleteTagLabelNote,
  setTrainingMembership,
  editTagLabelNote,
  listTrainingSet,
  listTrainingSetHeads,
  setNewDedupTagAnnotation,
  type TagState,
  type TrainingSetRow,
} from '@/lib/api';
import { imageSrc, type ImageRef } from '@/lib/imageUrl';
import Spinner from '@/components/Spinner';
import ErrorBanner from '@/components/ErrorBanner';
import ImageSizeToggle from '@/components/ImageSizeToggle';
import { pushToast } from '@/lib/toast';

/* NEW DEDUP · Training set — a head's labels, in four trays.
 *
 * MEMBERSHIP IS THE OPERATOR'S, AND IT IS STORED (migration 484). The
 * operator's correction, after two wrong models: "I asked you to keep whatever
 * was in the training positive set and I would move images from reserve to the
 * training positive set as I would deem necessary."
 *   Training · positive — what the head trains on. It changes ONLY when the
 *                         operator changes it, so a reviewed set stays the
 *                         size it was reviewed at.
 *   Training · negative — what it trains on as negative.
 *   Reserve             — positives the model proposed and the operator has
 *                         not admitted. Trains nothing. Nothing moves out of
 *                         here by itself; there is no rank and no target.
 *   Left out            — the subject is there but the photo is of something
 *                         else. Trains nothing, grades nothing.
 *
 * A tray is one server query, and the trainer reads the admitted rows through
 * machine_labeling.training_rows, so page and trainer cannot disagree. Counts
 * are counts of rows and move optimistically on every click.
 *
 * "Move to training" / "Return to reserve" is the one deliberate way membership
 * changes. A machine pass PROPOSES a label (landing in reserve) and can never
 * admit one; re-labelling never demotes an admitted row.
 *
 * THE HOLDOUT IS NOT HERE: those images grade the model.
 */

type Tray = 'positive' | 'negative' | 'excluded' | 'reserve';
const TRAYS: readonly Tray[] = ['positive', 'negative', 'reserve', 'excluded'];
const TRAY_LABEL: Record<Tray, string> = {
  positive: 'Training · positive',
  negative: 'Training · negative',
  reserve: 'Reserve',
  excluded: 'Left out',
};
const TRAY_TITLE: Record<Tray, string> = {
  positive: 'What this head trains on as positive. It only changes when you change it.',
  negative: 'What this head trains on as negative.',
  reserve: 'Positives the model proposed that you have not admitted. Nothing here trains anything until you move it in.',
  excluded: 'The subject is there but the photo is of something else. Trains nothing, grades nothing.',
};
const STATE_STYLE: Record<TagState, string> = {
  positive: 'border-[var(--color-sage)] bg-[var(--color-sage)]/10',
  negative: 'border-[var(--color-rule)]',
  excluded: 'border-dashed border-[var(--color-copper)]',
};

/* The query behind each tray. Membership is stored (migration 484), so a tray
 * is a state plus, for positives, whether the operator has admitted it. */
const trayQuery = (t: Tray) => t === 'reserve'
  ? { state: 'positive' as const, in_training: false }
  : { state: t, in_training: true };

const PAGE_SIZES = [50, 100, 500, 2000] as const;
type PageSize = (typeof PAGE_SIZES)[number];
const DEFAULT_PAGE: PageSize = 50;
const BULK_CAP = 200;

const readTray = (raw: string | null): Tray =>
  (TRAYS as readonly string[]).includes(raw ?? '') ? (raw as Tray) : 'positive';

export default function NewDedupTrainingSet() {
  const headSelectId = useId();
  const [params, setParams] = useSearchParams();
  const qc = useQueryClient();
  const [large, setLarge] = useState(false);
  /* Tiles whose mark changed in THIS session, with what they showed before —
   * the note field appears there, and from_state travels with the note. */
  const [changed, setChanged] = useState<Map<number, { from: TagState; to: TagState }>>(new Map());
  const [drafts, setDrafts] = useState<Map<number, string>>(new Map());
  const [editingNote, setEditingNote] = useState<Set<number>>(new Set());
  const [previewing, setPreviewing] = useState(false);

  const tagId = Number(params.get('tag') ?? 0) || null;
  const tray = readTray(params.get('set'));
  const offset = Math.max(0, Number(params.get('offset') ?? 0) || 0);
  const rawN = Number(params.get('n') ?? DEFAULT_PAGE);
  const pageSize: PageSize = (PAGE_SIZES as readonly number[]).includes(rawN)
    ? (rawN as PageSize) : DEFAULT_PAGE;

  const patch = (next: Record<string, string | null>) => {
    const merged = new URLSearchParams(params);
    for (const [k, v] of Object.entries(next)) {
      if (v === null) merged.delete(k);
      else merged.set(k, v);
    }
    setParams(merged, { replace: true });
  };

  const headsQ = useQuery({ queryKey: ['training-set-heads'], queryFn: () => listTrainingSetHeads() });
  const heads = headsQ.data?.data ?? [];
  const ordered = useMemo(
    () => [...heads].sort((a, b) => b.positive - a.positive || a.label.localeCompare(b.label)),
    [heads],
  );
  const activeId = tagId ?? ordered[0]?.id ?? null;
  const activeHead = ordered.find((h) => h.id === activeId) ?? null;

  const rowsKey = ['training-set', activeId, tray, offset, pageSize];
  const rowsQ = useQuery({
    queryKey: rowsKey,
    queryFn: () => listTrainingSet({ tag_id: activeId as number, ...trayQuery(tray), limit: pageSize, offset }),
    enabled: activeId != null,
  });
  const rows = rowsQ.data?.data.rows ?? [];
  const total = activeHead ? activeHead[tray] : null;
  const lastOffset = total == null ? null
    : Math.max(0, Math.floor(Math.max(0, total - 1) / pageSize) * pageSize);

  /* COUNTS MOVE ON THE CLICK, in tray terms. A mark change moves a row between
   * trays; the reserve is only ever entered or left by an explicit move. */
  const bumpTrays = (deltas: Partial<Record<Tray, number>>) =>
    qc.setQueryData(['training-set-heads'], (old: typeof headsQ.data) => old && ({
      ...old,
      data: old.data.map((h) => h.id !== activeId ? h : Object.entries(deltas).reduce(
        (acc, [k, d]) => ({ ...acc, [k]: Math.max(0, acc[k as Tray] + (d as number)) }),
        { ...h },
      )),
    }));

  const patchRow = (imageId: number, fn: (r: TrainingSetRow) => TrainingSetRow) =>
    qc.setQueryData(rowsKey, (old: typeof rowsQ.data) => old && ({
      ...old, data: { ...old.data, rows: old.data.rows.map((r) => r.image_id === imageId ? fn(r) : r) },
    }));

  const correctMut = useMutation({
    mutationFn: ({ imageId, state }: { imageId: number; state: TagState; from: TagState; fromMine: boolean }) =>
      setNewDedupTagAnnotation(activeId as number, imageId, state, state === 'excluded' ? 'pruned' : null),
    onMutate: (vars) => {
      /* Optimistic: the tile and the counts move now. A failure reverts both.
       * A mark written by the operator is admitted, so a reserve row that is
       * re-marked leaves the reserve and enters its new tray. */
      patchRow(vars.imageId, (r) => ({
        ...r, state: vars.state, source: 'human', in_training: true,
        excluded_reason: vars.state === 'excluded' ? 'pruned' : null,
      }));
      bumpTrays({ [tray]: -1, [vars.state]: +1 } as Partial<Record<Tray, number>>);
    },
    onSuccess: (_res, vars) => {
      setChanged((prev) => new Map(prev).set(vars.imageId, {
        from: prev.get(vars.imageId)?.from ?? vars.from, to: vars.state,
      }));
      qc.invalidateQueries({ queryKey: ['training-set-heads'] });
    },
    onError: (e: Error, vars) => {
      patchRow(vars.imageId, (r) => ({
        ...r, state: vars.from, source: vars.fromMine ? 'human' : 'machine',
        in_training: tray !== 'reserve',
      }));
      qc.invalidateQueries({ queryKey: ['training-set-heads'] });
      pushToast('err', e.message);
    },
  });

  /* THE MOVE. The one deliberate way membership changes: the operator admits
   * reserve photos to the training set, or returns admitted ones to reserve.
   * Chunked by the server's bulk cap so a 2,000-row page works in one press. */
  const moveMut = useMutation({
    mutationFn: async ({ imageIds, into }: { imageIds: number[]; into: boolean }) => {
      const moved: number[] = [];
      for (let i = 0; i < imageIds.length; i += BULK_CAP) {
        const res = await setTrainingMembership(activeId as number, imageIds.slice(i, i + BULK_CAP), into);
        moved.push(...res.data.moved);
      }
      return moved;
    },
    onSuccess: (moved, vars) => {
      const set = new Set(moved);
      qc.setQueryData(rowsKey, (old: typeof rowsQ.data) => old && ({
        ...old, data: { ...old.data, rows: old.data.rows.map((r) => set.has(r.image_id) ? { ...r, in_training: vars.into } : r) },
      }));
      bumpTrays(vars.into ? { reserve: -moved.length, positive: +moved.length }
                          : { positive: -moved.length, reserve: +moved.length });
      qc.invalidateQueries({ queryKey: ['training-set-heads'] });
      pushToast('ok', vars.into ? `${moved.length} moved into the training set`
                                : `${moved.length} returned to reserve`);
    },
    onError: (e: Error) => pushToast('err', e.message),
  });
  /* Rows a page-wide move would touch: everything still on the tray's side. */
  const movable = rows.filter((r) => r.in_training === (tray !== 'reserve') && !changed.has(r.image_id));
  const willMove = new Set(movable.map((r) => r.image_id));

  const patchRowNote = (imageId: number, note: { id: number; note: string } | null) =>
    patchRow(imageId, (r) => ({ ...r, note_id: note?.id ?? null, note: note?.note ?? null }));
  const noteMut = useMutation({
    mutationFn: ({ imageId, text, to, from }: { imageId: number; text: string; to: TagState; from: TagState | null }) =>
      setNewDedupTagAnnotation(activeId as number, imageId, to, to === 'excluded' ? 'pruned' : null, { text, from_state: from }),
    onSuccess: (res, vars) => {
      const data = res.data as { note_unavailable?: string; note?: { id: number; note: string } | null };
      if (data.note_unavailable) { pushToast('err', `Your mark is saved, but the note was not: ${data.note_unavailable}`); return; }
      setDrafts((prev) => { const n = new Map(prev); n.delete(vars.imageId); return n; });
      setChanged((prev) => { const n = new Map(prev); n.delete(vars.imageId); return n; });
      if (data.note) patchRowNote(vars.imageId, data.note);
      pushToast('ok', 'Note saved');
    },
    onError: (e: Error) => pushToast('err', e.message),
  });
  const editNoteMut = useMutation({
    mutationFn: ({ noteId, text }: { noteId: number; text: string; imageId: number }) => editTagLabelNote(noteId, text),
    onSuccess: (res, vars) => {
      patchRowNote(vars.imageId, { id: res.data.id, note: res.data.note });
      setDrafts((prev) => { const n = new Map(prev); n.delete(vars.imageId); return n; });
      setEditingNote((prev) => { const n = new Set(prev); n.delete(vars.imageId); return n; });
      pushToast('ok', 'Note updated');
    },
    onError: (e: Error) => pushToast('err', e.message),
  });
  const deleteNoteMut = useMutation({
    mutationFn: ({ noteId }: { noteId: number; imageId: number }) => deleteTagLabelNote(noteId),
    onSuccess: (_res, vars) => {
      patchRowNote(vars.imageId, null);
      setEditingNote((prev) => { const n = new Set(prev); n.delete(vars.imageId); return n; });
      pushToast('ok', 'Note removed');
    },
    onError: (e: Error) => pushToast('err', e.message),
  });

  if (headsQ.isLoading) return <div className="p-6"><Spinner /></div>;
  if (headsQ.error) return <div className="p-6"><ErrorBanner message={(headsQ.error as Error).message} /></div>;

  const tile = (r: TrainingSetRow) => {
    const mine = r.source !== 'machine';
    const ref: ImageRef = { storage_path: r.storage_path, sreality_url: '' };
    const ch = changed.get(r.image_id);
    return (
      <li
        key={r.image_id}
        data-testid={`training-tile-${r.image_id}`}
        data-state={r.state}
        data-previewed={previewing && willMove.has(r.image_id) ? 'true' : undefined}
        className={`rounded-[var(--radius-sm)] border p-1.5 flex flex-col gap-1.5 transition-opacity ${STATE_STYLE[r.state]} ${
          previewing ? (willMove.has(r.image_id)
            ? 'ring-2 ring-[var(--color-sage)] ring-offset-1 ring-offset-[var(--color-paper)]' : 'opacity-40') : ''
        }`}
      >
        <a href={imageSrc(ref)} target="_blank" rel="noreferrer" title="Open the full-size photo in a new tab"
           className={`block bg-[var(--color-paper-2)] rounded-[var(--radius-xs)] ${large ? 'h-56' : 'h-28'}`}>
          <img src={imageSrc(ref)} alt={`Training image ${r.image_id}`} loading="lazy"
               className="w-full h-full object-contain rounded-[var(--radius-xs)]" />
        </a>
        <div className="flex items-center gap-1 text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)]">
          <span title={mine ? 'Your label — no machine pass can overwrite it' : 'Written by the model'}>{mine ? 'yours' : 'machine'}</span>
          {r.definition_stale && (
            <span className="text-[var(--color-copper)]" title="Written under wording you have since replaced. Not necessarily wrong — but it followed a rule that has changed.">· old wording</span>
          )}
        </div>
        <div className="flex gap-1">
          {(['positive', 'negative', 'excluded'] as const).map((v) => (
            <button
              key={v}
              type="button"
              aria-label={`${v} ${r.image_id}`}
              aria-pressed={r.state === v}
              title={v === r.state
                ? (mine ? `Already ${TRAY_LABEL[v]}, yours` : 'Confirm — makes this your label and admits it')
                : `Change the mark to ${TRAY_LABEL[v]}`}
              disabled={correctMut.isPending}
              onClick={() => correctMut.mutate({ imageId: r.image_id, state: v, from: r.state, fromMine: mine })}
              className={`flex-1 py-0.5 text-[0.65rem] whitespace-nowrap rounded-[var(--radius-xs)] border transition-colors ${
                r.state === v ? 'border-[var(--color-ink-2)] text-[var(--color-ink)]'
                  : 'border-[var(--color-rule)] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)]'}`}
            >
              {v === 'positive' ? '✓ applies' : v === 'negative' ? '✕ no' : '– left out'}
            </button>
          ))}
        </div>
        {/* One photo at a time: admit it, or send it back. */}
        <button
          type="button"
          data-testid={`move-${r.image_id}`}
          disabled={moveMut.isPending}
          onClick={() => moveMut.mutate({ imageIds: [r.image_id], into: !r.in_training })}
          className={`py-0.5 text-[0.65rem] rounded-[var(--radius-xs)] border ${
            r.in_training
              ? 'border-[var(--color-rule)] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)]'
              : 'border-[var(--color-sage)] text-[var(--color-ink)] hover:bg-[var(--color-sage)]/10'}`}
        >
          {r.in_training ? '↩ return to reserve' : '→ move to training'}
        </button>
        {ch && ch.to !== tray && (
          <p className="text-[0.65rem] text-[var(--color-ink-3)] leading-snug">
            Now in <b>{TRAY_LABEL[ch.to]}</b> · <b>Yours</b>. Stays here until you change tray or leave the page.
          </p>
        )}
        {r.note && !editingNote.has(r.image_id) && (
          <div data-testid={`note-saved-${r.image_id}`} className="text-[0.7rem] leading-snug">
            <p className="text-[var(--color-ink-2)] text-pretty">“{r.note}”</p>
            <span className="flex gap-2 mt-0.5">
              <button type="button" className="text-[var(--color-copper)] hover:underline"
                onClick={() => { setDrafts((p) => new Map(p).set(r.image_id, r.note ?? '')); setEditingNote((p) => new Set(p).add(r.image_id)); }}>
                edit note
              </button>
              <button type="button" disabled={deleteNoteMut.isPending} className="text-[var(--color-ink-4)] hover:underline"
                onClick={() => deleteNoteMut.mutate({ noteId: r.note_id as number, imageId: r.image_id })}>
                remove
              </button>
            </span>
          </div>
        )}
        {!r.note && !ch && !editingNote.has(r.image_id) && (
          <button type="button" data-testid={`note-add-${r.image_id}`}
            onClick={() => setEditingNote((p) => new Set(p).add(r.image_id))}
            className="self-start text-[0.7rem] text-[var(--color-ink-4)] hover:text-[var(--color-copper)] hover:underline">
            + note
          </button>
        )}
        {(ch || editingNote.has(r.image_id)) && (
          <form
            data-testid={`note-form-${r.image_id}`}
            className="flex gap-1"
            onSubmit={(e) => {
              e.preventDefault();
              const text = (drafts.get(r.image_id) ?? '').trim();
              if (!text) return;
              if (r.note_id != null && editingNote.has(r.image_id)) {
                editNoteMut.mutate({ noteId: r.note_id, text, imageId: r.image_id });
              } else {
                noteMut.mutate({ imageId: r.image_id, text, to: ch?.to ?? r.state, from: ch?.from ?? null });
              }
            }}
          >
            <input aria-label={`why ${r.image_id}`} value={drafts.get(r.image_id) ?? ''}
              onChange={(e) => setDrafts((p) => new Map(p).set(r.image_id, e.target.value))}
              placeholder="why? (optional)" maxLength={600}
              className="min-w-0 flex-1 px-1.5 py-0.5 text-[0.75rem] rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-transparent text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)]" />
            <button type="submit" disabled={noteMut.isPending || editNoteMut.isPending || !(drafts.get(r.image_id) ?? '').trim()}
              className="px-2 py-0.5 text-[0.7rem] rounded-[var(--radius-xs)] border border-[var(--color-copper)] text-[var(--color-ink)] disabled:opacity-40">
              save
            </button>
            {editingNote.has(r.image_id) && (
              <button type="button" className="px-2 py-0.5 text-[0.7rem] text-[var(--color-ink-4)] hover:underline"
                onClick={() => { setDrafts((p) => { const n = new Map(p); n.delete(r.image_id); return n; }); setEditingNote((p) => { const n = new Set(p); n.delete(r.image_id); return n; }); }}>
                cancel
              </button>
            )}
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
            <p className="text-xs text-[var(--color-ink-3)] mt-0.5 max-w-prose">
              One head at a time, in four trays: what it trains on as positive and as negative, what waits
              in <b>reserve</b>, and what is left out. The training set changes only when you change it &mdash;
              nothing moves in or out on its own, so a set you have reviewed stays reviewed. The model&rsquo;s
              new proposals land in the reserve. The sealed exam images are excluded: they grade the model,
              so they never appear on a training surface.
            </p>
          </div>
          <ImageSizeToggle large={large} onChange={setLarge} label="Training grid image size" />
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <label htmlFor={headSelectId} className="text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)]">head</label>
          <select id={headSelectId} value={activeId ?? ''} onChange={(e) => patch({ tag: e.target.value, offset: null })}
            className="px-2 py-1 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-transparent text-[var(--color-ink)]">
            {ordered.map((h) => <option key={h.id} value={h.id}>{h.label} · {h.positive}</option>)}
          </select>

          <span className="flex gap-1" role="group" aria-label="tray">
            {TRAYS.map((t) => (
              <button key={t} type="button" title={TRAY_TITLE[t]} aria-pressed={tray === t}
                onClick={() => patch({ set: t, offset: null })}
                className={`px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border ${
                  tray === t ? 'border-[var(--color-sage)] text-[var(--color-ink)]'
                    : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]'}`}>
                {TRAY_LABEL[t]}
                {activeHead && (
                  <span data-testid={`tray-count-${t}`} className="ml-1 text-[var(--color-ink-4)] tabular-nums">{activeHead[t]}</span>
                )}
              </button>
            ))}
          </span>
        </div>

        {activeHead && (
          <p className="mt-2 text-xs text-[var(--color-ink-3)]" data-testid="head-summary">
            Trains on {activeHead.positive} positives and {activeHead.negative} negatives ·{' '}
            {activeHead.reserve} waiting in reserve · {activeHead.excluded} left out
          </p>
        )}
      </header>

      <details className="mt-3 rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-2 text-xs text-[var(--color-ink-2)]">
        <summary className="cursor-pointer select-none text-[var(--color-ink)]">How to use this page</summary>
        <div className="mt-2 grid gap-3 md:grid-cols-2">
          <div>
            <p className="font-medium text-[var(--color-ink)]">The four trays</p>
            <ul className="mt-0.5 list-disc pl-4 space-y-0.5">
              <li><b>Training · positive</b> — what this head trains on as positive. It changes only when you change it.</li>
              <li><b>Training · negative</b> — what it trains on as negative.</li>
              <li><b>Reserve</b> — positives the model proposed that you have not admitted. They train nothing, and nothing leaves here by itself.</li>
              <li><b>Left out</b> — the subject is there but the photo is of something else. Trains nothing.</li>
            </ul>
            <p className="mt-2 font-medium text-[var(--color-ink)]">Moving photos</p>
            <p className="mt-0.5">
              <b>→ move to training</b> admits one reserve photo; <b>↩ return to reserve</b> takes one out. The
              button under the grid does the whole page at once. Changing a photo&rsquo;s mark also admits it,
              because a mark you set is a decision you have made.
            </p>
            <p className="mt-2 font-medium text-[var(--color-ink)]">Growing a set</p>
            <p className="mt-0.5">
              Open <b>Reserve</b>, scan, and move in what you want. You never have to look at the rest: a
              thousand unreviewed proposals sitting in reserve cost you nothing.
            </p>
          </div>
          <div>
            <p className="font-medium text-[var(--color-ink)]">Each photo</p>
            <ul className="mt-0.5 list-disc pl-4 space-y-0.5">
              <li>Click the photo to open it full-size in a new tab.</li>
              <li><b>machine</b> / <b>yours</b> says who decided the current mark.</li>
              <li><b>→ move to training</b> / <b>↩ return to reserve</b> is membership, separate from the mark.</li>
              <li><b>old wording</b> means the label was written under a definition you have since changed.</li>
              <li><b>✓ applies</b>, <b>✕ no</b>, <b>– left out</b> change the mark. Pressing the already-pressed one on a machine tile <i>confirms</i> it as yours and admits it.</li>
              <li>A moved photo stays where it is until you change tray or reload, with a line saying where it went.</li>
            </ul>
            <p className="mt-2 font-medium text-[var(--color-ink)]">Why? field</p>
            <p className="mt-0.5">
              After a move, a small field appears; a short reason is enough. Notes are gathered per head and
              distilled into one general rule in the definition, never one line per note. A saved note stays on
              its photo: <b>edit note</b> or <b>remove</b> it before it has been absorbed. <b>+ note</b> adds one without a move.
            </p>
          </div>
        </div>
      </details>

      {rowsQ.isLoading ? (
        <div className="py-10 flex justify-center"><Spinner /></div>
      ) : rowsQ.error ? (
        <div className="mt-4"><ErrorBanner message={(rowsQ.error as Error).message} /></div>
      ) : rows.length === 0 ? (
        <p className="mt-10 text-center text-sm text-[var(--color-ink-2)]">Nothing in this tray for this head yet.</p>
      ) : (
        <>
          <ul className="mt-4 grid gap-2" style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${large ? '16rem' : '8rem'}, 1fr))` }}>
            {rows.map(tile)}
          </ul>
          {movable.length > 0 && (
            <div className="mt-4 flex flex-col items-center gap-1">
              <button type="button" data-testid="move-page" disabled={moveMut.isPending}
                onMouseEnter={() => setPreviewing(true)} onMouseLeave={() => setPreviewing(false)}
                onFocus={() => setPreviewing(true)} onBlur={() => setPreviewing(false)}
                onClick={() => { setPreviewing(false); moveMut.mutate({ imageIds: movable.map((r) => r.image_id), into: tray === 'reserve' }); }}
                className="px-3 py-1.5 text-xs rounded-[var(--radius-sm)] border border-[var(--color-sage)] text-[var(--color-ink)] hover:bg-[var(--color-sage)]/10 disabled:opacity-40">
                {tray === 'reserve'
                  ? `→ Move all ${movable.length} on this page into the training set`
                  : `↩ Return all ${movable.length} on this page to reserve`}
              </button>
              <p className="text-[0.7rem] text-[var(--color-ink-4)] text-center max-w-prose">
                {tray === 'reserve'
                  ? 'Nothing leaves the reserve on its own. Hover to see exactly which photos this admits.'
                  : 'Takes them out of what the head trains on. Hover to see which.'}
              </p>
            </div>
          )}
          <div className="mt-4 flex items-center justify-center gap-3 text-xs flex-wrap">
            <span className="flex items-center gap-1" role="group" aria-label="per page">
              <span className="text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)] mr-0.5">per page</span>
              {PAGE_SIZES.map((n) => (
                <button key={n} type="button" aria-pressed={pageSize === n} onClick={() => patch({ n: String(n), offset: null })}
                  className={`px-2 py-1 rounded-[var(--radius-sm)] border ${pageSize === n ? 'border-[var(--color-ink-2)] text-[var(--color-ink)]' : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]'}`}>
                  {n}
                </button>
              ))}
            </span>
            <button type="button" disabled={offset === 0} onClick={() => patch({ offset: String(Math.max(0, offset - pageSize)) })}
              className="px-3 py-1 rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-3)] disabled:opacity-40">
              ← previous
            </button>
            <span data-testid="page-range" className="text-[var(--color-ink-4)] tabular-nums">
              {offset + 1}–{offset + rows.length}{total != null && ` of ${total}`}
            </span>
            <button type="button" disabled={rows.length < pageSize} onClick={() => patch({ offset: String(offset + pageSize) })}
              className="px-3 py-1 rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-3)] disabled:opacity-40">
              next →
            </button>
            {lastOffset != null && lastOffset > offset && (
              <button type="button" data-testid="jump-last" onClick={() => patch({ offset: String(lastOffset) })}
                className="px-3 py-1 rounded-[var(--radius-sm)] border border-[var(--color-sage)] text-[var(--color-ink)]">
                last page ⇥
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
