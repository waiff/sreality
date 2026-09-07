import { useId, useMemo, useState } from 'react';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import {
  bulkSetNewDedupTagAnnotation,
  deleteTagLabelNote,
  editTagLabelNote,
  listTrainingSet,
  listTrainingSetHeads,
  setNewDedupTagAnnotation,
  type TagState,
  type TrainingSetHead,
  type TrainingSetRow,
} from '@/lib/api';
import { imageSrc, type ImageRef } from '@/lib/imageUrl';
import Spinner from '@/components/Spinner';
import ErrorBanner from '@/components/ErrorBanner';
import ImageSizeToggle from '@/components/ImageSizeToggle';
import { pushToast } from '@/lib/toast';

/* NEW DEDUP · Training set — a head's labels, sorted into three trays.
 *
 * NO LIMITS. The operator's ruling (2026-09-07), replacing the cutoff: a
 * head's training set is every label it has. So:
 *   Training · positive — every positive, the operator's and the machine's
 *   Training · negative — every negative, likewise
 *   Left out            — every exclusion; trains nothing, grades nothing
 * A tray is one server query on one state, and the trainer reads the same
 * rows (machine_labeling.training_rows), so page and trainer cannot disagree.
 * A count on a tray is a plain count of labels, and it moves the instant a
 * mark does — patched optimistically from the click, reconciled by a refetch.
 *
 * Moving a photo between trays writes a HUMAN label, which the store's
 * human-wins rail protects from every later machine pass; that is why the tile
 * says who decided. The bounded review survives as a filter, not a cap: "the
 * machine's" inside a tray is what still deserves a look, and "confirm the
 * rest of this page" is how a page of it becomes yours in one write.
 *
 * THE HOLDOUT IS NOT HERE: those images grade the model.
 */

type Tray = TagState;
const TRAYS: readonly Tray[] = ['positive', 'negative', 'excluded'];
const TRAY_LABEL: Record<Tray, string> = {
  positive: 'Training · positive',
  negative: 'Training · negative',
  excluded: 'Left out',
};
const TRAY_TITLE: Record<Tray, string> = {
  positive: 'Every positive for this head, yours and the machine’s. This is what a probe trains on as positive.',
  negative: 'Every negative for this head, yours and the machine’s. This is what a probe trains on as negative.',
  excluded: 'The subject is there but the photo is of something else. Trains nothing, grades nothing.',
};
const TRAY_STYLE: Record<Tray, string> = {
  positive: 'border-[var(--color-sage)] bg-[var(--color-sage)]/10',
  negative: 'border-[var(--color-rule)]',
  excluded: 'border-dashed border-[var(--color-copper)]',
};

type Who = 'all' | 'machine' | 'human';
const WHO: ReadonlyArray<{ key: Who; label: string; title: string }> = [
  { key: 'all', label: 'Anyone', title: 'Every label in this tray' },
  { key: 'machine', label: 'The machine’s', title: 'Still on the model’s word alone — what deserves a look' },
  { key: 'human', label: 'Yours', title: 'Your own labels; no machine pass can overwrite these' },
];

const PAGE_SIZES = [50, 100, 500, 2000] as const;
type PageSize = (typeof PAGE_SIZES)[number];
const DEFAULT_PAGE: PageSize = 50;
const BULK_CAP = 200;

const readTray = (raw: string | null): Tray =>
  (TRAYS as readonly string[]).includes(raw ?? '') ? (raw as Tray) : 'positive';
const readWho = (raw: string | null): Who =>
  raw === 'machine' || raw === 'human' ? raw : 'all';

/* The exact size of what is on screen, from the head's counts. Left-outs are
 * not split by who decided, so that combination has no exact total. */
const trayTotal = (h: TrainingSetHead | null, tray: Tray, who: Who): number | null => {
  if (!h) return null;
  if (tray === 'excluded') return who === 'all' ? h.excluded : null;
  if (who === 'all') return h[tray];
  return who === 'machine' ? h[`machine_${tray}`] : h[`human_${tray}`];
};

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
  const who = readWho(params.get('who'));
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

  const rowsKey = ['training-set', activeId, tray, who, offset, pageSize];
  const rowsQ = useQuery({
    queryKey: rowsKey,
    queryFn: () => listTrainingSet({
      tag_id: activeId as number, state: tray,
      ...(who === 'all' ? {} : { source: who }),
      limit: pageSize, offset,
    }),
    enabled: activeId != null,
  });
  const rows = rowsQ.data?.data.rows ?? [];
  const total = trayTotal(activeHead, tray, who);
  const lastOffset = total == null ? null
    : Math.max(0, Math.floor(Math.max(0, total - 1) / pageSize) * pageSize);

  /* COUNTS MOVE ON THE CLICK. The head's counts are patched in the cache from
   * the change itself (from → to, and machine → yours), so the tray numbers
   * are right before the server answers; the heads refetch then reconciles. */
  const bumpCounts = (moves: Array<{ from: TagState; fromMine: boolean; to: TagState }>) => {
    qc.setQueryData(['training-set-heads'], (old: typeof headsQ.data) => old && ({
      ...old,
      data: old.data.map((h) => {
        if (h.id !== activeId) return h;
        const c = { ...h };
        for (const m of moves) {
          if (m.from !== m.to) {
            c[m.from] = Math.max(0, c[m.from] - 1);
            c[m.to] += 1;
          }
          if (m.from !== 'excluded') {
            const k = `${m.fromMine ? 'human' : 'machine'}_${m.from}` as const;
            c[k] = Math.max(0, c[k] - 1);
          }
          if (m.to !== 'excluded') c[`human_${m.to}` as const] += 1;
        }
        return c;
      }),
    }));
  };
  const patchRow = (imageId: number, fn: (r: TrainingSetRow) => TrainingSetRow) =>
    qc.setQueryData(rowsKey, (old: typeof rowsQ.data) => old && ({
      ...old, data: { ...old.data, rows: old.data.rows.map((r) => r.image_id === imageId ? fn(r) : r) },
    }));

  const correctMut = useMutation({
    mutationFn: ({ imageId, state }: { imageId: number; state: TagState; from: TagState; fromMine: boolean }) =>
      setNewDedupTagAnnotation(activeId as number, imageId, state, state === 'excluded' ? 'pruned' : null),
    onMutate: (vars) => {
      /* Optimistic: the tile and the counts move now. A failure reverts both. */
      patchRow(vars.imageId, (r) => ({
        ...r, state: vars.state, source: 'human',
        excluded_reason: vars.state === 'excluded' ? 'pruned' : null,
      }));
      bumpCounts([{ from: vars.from, fromMine: vars.fromMine, to: vars.state }]);
    },
    onSuccess: (_res, vars) => {
      setChanged((prev) => new Map(prev).set(vars.imageId, {
        from: prev.get(vars.imageId)?.from ?? vars.from, to: vars.state,
      }));
      qc.invalidateQueries({ queryKey: ['training-set-heads'] });
    },
    onError: (e: Error, vars) => {
      patchRow(vars.imageId, (r) => ({ ...r, state: vars.from, source: vars.fromMine ? 'human' : 'machine' }));
      qc.invalidateQueries({ queryKey: ['training-set-heads'] });
      pushToast('err', e.message);
    },
  });

  /* Confirm every machine label on this page that the operator left alone:
   * the page becomes theirs in one write (chunked by the server's bulk cap),
   * which is what takes it off "the machine's". */
  const pending = rows.filter((r) => r.source === 'machine' && !changed.has(r.image_id));
  const willConfirm = new Set(pending.map((r) => r.image_id));
  const confirmPageMut = useMutation({
    mutationFn: async (imageIds: number[]) => {
      const done: number[] = [];
      for (let i = 0; i < imageIds.length; i += BULK_CAP) {
        const res = await bulkSetNewDedupTagAnnotation(
          activeId as number, imageIds.slice(i, i + BULK_CAP), tray, tray === 'excluded' ? 'pruned' : null);
        done.push(...res.data.image_ids);
      }
      return done;
    },
    onSuccess: (done) => {
      const set = new Set(done);
      qc.setQueryData(rowsKey, (old: typeof rowsQ.data) => old && ({
        ...old, data: { ...old.data, rows: old.data.rows.map((r) => set.has(r.image_id) ? { ...r, source: 'human' as const } : r) },
      }));
      if (tray !== 'excluded') {
        const mk = `machine_${tray}` as const;
        const hk = `human_${tray}` as const;
        qc.setQueryData(['training-set-heads'], (old: typeof headsQ.data) => old && ({
          ...old,
          data: old.data.map((h) => h.id !== activeId ? h : {
            ...h, [mk]: Math.max(0, h[mk] - done.length), [hk]: h[hk] + done.length,
          }),
        }));
      }
      qc.invalidateQueries({ queryKey: ['training-set-heads'] });
      pushToast('ok', `${done.length} confirmed as yours`);
    },
    onError: (e: Error) => pushToast('err', e.message),
  });

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
        data-previewed={previewing && willConfirm.has(r.image_id) ? 'true' : undefined}
        className={`rounded-[var(--radius-sm)] border p-1.5 flex flex-col gap-1.5 transition-opacity ${TRAY_STYLE[r.state]} ${
          previewing ? (willConfirm.has(r.image_id)
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
          {TRAYS.map((v) => (
            <button
              key={v}
              type="button"
              aria-label={`${v} ${r.image_id}`}
              aria-pressed={r.state === v}
              title={v === r.state
                ? (mine ? `Already ${TRAY_LABEL[v]}, yours` : 'Confirm — makes this your label')
                : `Move to ${TRAY_LABEL[v]}`}
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
              One head at a time, in three trays: what it will train on as positive, as negative, and what is
              left out. Every label counts, yours and the machine&rsquo;s. Move a wrong photo with its buttons;
              the move is yours and final, and the counts change the moment you click. The sealed exam images
              are excluded: they grade the model, so they never appear on a training surface.
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

          {tray !== 'excluded' && (
            <span className="flex gap-1" role="group" aria-label="decided by">
              {WHO.map((w) => (
                <button key={w.key} type="button" title={w.title} aria-pressed={who === w.key}
                  onClick={() => patch({ who: w.key === 'all' ? null : w.key, offset: null })}
                  className={`px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border ${
                    who === w.key ? 'border-[var(--color-copper)] text-[var(--color-ink)]'
                      : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]'}`}>
                  {w.label}
                  {activeHead && w.key !== 'all' && (
                    <span data-testid={`who-count-${w.key}`} className="ml-1 text-[var(--color-ink-4)] tabular-nums">
                      {w.key === 'machine' ? activeHead[`machine_${tray}`] : activeHead[`human_${tray}`]}
                    </span>
                  )}
                </button>
              ))}
            </span>
          )}
        </div>
        {activeHead && (
          <p className="mt-2 text-xs text-[var(--color-ink-3)]" data-testid="head-summary">
            {activeHead.positive} positives ({activeHead.human_positive} yours, {activeHead.machine_positive} the machine&rsquo;s) ·{' '}
            {activeHead.negative} negatives ({activeHead.human_negative} yours, {activeHead.machine_negative} the machine&rsquo;s) ·{' '}
            {activeHead.excluded} left out
          </p>
        )}
      </header>

      <details className="mt-3 rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-2 text-xs text-[var(--color-ink-2)]">
        <summary className="cursor-pointer select-none text-[var(--color-ink)]">How to use this page</summary>
        <div className="mt-2 grid gap-3 md:grid-cols-2">
          <div>
            <p className="font-medium text-[var(--color-ink)]">The three trays</p>
            <ul className="mt-0.5 list-disc pl-4 space-y-0.5">
              <li><b>Training · positive</b> — every photo labeled as this head, by you or by the machine. A probe trains on all of them.</li>
              <li><b>Training · negative</b> — every photo labeled as not this head. A probe trains on all of them too.</li>
              <li><b>Left out</b> — the subject is there but the photo is of something else. Trains nothing.</li>
            </ul>
            <p className="mt-2 font-medium text-[var(--color-ink)]">Decided by</p>
            <p className="mt-0.5">
              <b>The machine&rsquo;s</b> is what still deserves a look. <b>Yours</b> is what you have decided.
              The count on each is exact and moves as you click.
            </p>
            <p className="mt-2 font-medium text-[var(--color-ink)]">The fastest way through a tray</p>
            <p className="mt-0.5">
              Show <b>the machine&rsquo;s</b>, scan the page, move the wrong ones with their buttons, then press
              <b> Confirm the other N on this page</b>: everything you left alone becomes your label in one go.
            </p>
          </div>
          <div>
            <p className="font-medium text-[var(--color-ink)]">Each photo</p>
            <ul className="mt-0.5 list-disc pl-4 space-y-0.5">
              <li>Click the photo to open it full-size in a new tab.</li>
              <li><b>machine</b> / <b>yours</b> says who decided the current mark.</li>
              <li><b>old wording</b> means the label was written under a definition you have since changed.</li>
              <li><b>✓ applies</b>, <b>✕ no</b>, <b>– left out</b> move the photo between trays. Pressing the already-pressed one on a machine tile <i>confirms</i> it as yours.</li>
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
          {pending.length > 0 && (
            <div className="mt-4 flex flex-col items-center gap-1">
              <button type="button" data-testid="confirm-page" disabled={confirmPageMut.isPending}
                onMouseEnter={() => setPreviewing(true)} onMouseLeave={() => setPreviewing(false)}
                onFocus={() => setPreviewing(true)} onBlur={() => setPreviewing(false)}
                onClick={() => { setPreviewing(false); confirmPageMut.mutate(pending.map((r) => r.image_id)); }}
                className="px-3 py-1.5 text-xs rounded-[var(--radius-sm)] border border-[var(--color-sage)] text-[var(--color-ink)] hover:bg-[var(--color-sage)]/10 disabled:opacity-40">
                ✓ Confirm the other {pending.length} on this page as correct
              </button>
              <p className="text-[0.7rem] text-[var(--color-ink-4)] text-center max-w-prose">
                Move the wrong ones first, then press this: every remaining machine label on this page becomes yours.
                Hover it to see exactly which photos it will claim.
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
