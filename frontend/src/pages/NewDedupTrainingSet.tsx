import { useEffect, useId, useMemo, useRef, useState } from 'react';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import {
  deleteTagLabelNote,
  setTrainingMembership,
  editTagLabelNote,
  drawTrainingSet,
  listTrainingSet,
  listTrainingSetHeads,
  locateTrainingImage,
  setNewDedupTagFlags,
  type ReviewState,
  setNewDedupTagAnnotation,
  type TagState,
  type TrainingSetRow,
} from '@/lib/api';
import { imageSrc, type ImageRef } from '@/lib/imageUrl';
import ImageLightbox from '@/components/ImageLightbox';
import type { ImagePublic } from '@/lib/types';
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

/* The operator's model (migration 486): membership is a fact about a LABEL, not
 * about a positive. Each sign has a training set and a reserve, and the names
 * say only that. `sample` is gone — the drawn thousand IS the training
 * negative set, so it needed neither a tray of its own nor a table. */
type Tray = 'positive' | 'positive_reserve' | 'negative' | 'negative_reserve' | 'excluded';
const TRAYS: readonly Tray[] = ['positive', 'positive_reserve', 'negative', 'negative_reserve', 'excluded'];
const TRAY_LABEL: Record<Tray, string> = {
  positive: 'Training positive',
  positive_reserve: 'Positive reserve',
  negative: 'Training negative',
  negative_reserve: 'Negative reserve',
  excluded: 'Left out',
};
const TRAY_TITLE: Record<Tray, string> = {
  positive: 'What this head trains on as positive. It only changes when you change it.',
  positive_reserve: 'Positives the model proposed that you have not admitted. Nothing here trains anything until you move it in.',
  negative: 'What this head trains on as negative — a thousand drawn at random, in the order they already had.',
  negative_reserve: 'Negatives the model proposed that are not in the training set. They train nothing.',
  excluded: 'The subject is there but the photo is of something else. Trains nothing, grades nothing.',
};
const STATE_STYLE: Record<TagState, string> = {
  positive: 'border-[var(--color-sage)] bg-[var(--color-sage)]/10',
  negative: 'border-[var(--color-rule)]',
  excluded: 'border-dashed border-[var(--color-copper)]',
};

/* The query behind each tray. Membership is stored (migration 484) and it is a
 * fact about a POSITIVE: admitted ones train, the rest wait in reserve.
 *
 * Negatives and left-outs are NOT filtered by the flag, and asking for
 * `in_training: true` on them was a real bug — every negative is admitted so
 * the filter was merely redundant there, but the backfill never admitted a
 * left-out, so "Left out" rendered 4 rows under a count of 1,064. A count and
 * its tray must be the same question; only the positive/reserve split asks
 * about membership at all. */
const trayQuery = (t: Tray) => {
  if (t === 'excluded') return { state: 'excluded' as const };
  const [state, admitted] = t.endsWith('_reserve')
    ? [t.slice(0, -'_reserve'.length), false] : [t, true];
  return { state: state as 'positive' | 'negative', in_training: admitted };
};

/* Membership is only a question for positives, so the move affordance exists
 * only where it means something. On the negative tray it did not: returning a
 * negative to "reserve" un-admitted a row that no tray counts, so it vanished
 * from the page with the counts moving the wrong column. Take a negative out by
 * re-marking it, which is what the marks are for. */
/* Membership is a question everywhere except "left out", which trains nothing
 * whichever way the flag points. */
const MEMBERSHIP_TRAYS: readonly Tray[] = ['positive', 'positive_reserve', 'negative', 'negative_reserve'];

/* Three states, and "skipped" is the reason this is not a toggle: setting a head
 * aside on purpose is a decision, and must not read as "nobody has said".
 *
 * The ORDER lives here because it is presentational, and the two Records below
 * are typed on the union — so adding a state fails the build here rather than
 * rendering a nameless button. It also must NOT come from lib/api: this page's
 * tests automock that module, which turns any runtime constant it exports into
 * undefined and renders the group empty. */
const REVIEW_ORDER: readonly ReviewState[] = ['not_ready', 'ready', 'skipped'];
const REVIEW_LABEL: Record<ReviewState, string> = {
  not_ready: 'Not ready',
  ready: '✓ Ready',
  skipped: 'Skip for now',
};
const REVIEW_ON: Record<ReviewState, string> = {
  not_ready: 'border-[var(--color-ink-2)]',
  ready: 'border-[var(--color-sage)] bg-[var(--color-sage)]/10',
  skipped: 'border-[var(--color-copper)] bg-[var(--color-copper)]/10',
};
/* The picker carries the same state, so the list itself is the progress view. */
const REVIEW_MARK: Record<ReviewState, string> = {
  not_ready: '', ready: '✓ ', skipped: '– ',
};

const PAGE_SIZES = [50, 100, 500, 2000, 10000] as const;
type PageSize = (typeof PAGE_SIZES)[number];
const DEFAULT_PAGE: PageSize = 50;
const BULK_CAP = 200;

/* Links handed out before the 486 rename keep working. */
const RENAMED: Record<string, Tray> = { reserve: 'positive_reserve', sample: 'negative' };
const readTray = (raw: string | null): Tray =>
  (TRAYS as readonly string[]).includes(raw ?? '') ? (raw as Tray)
    : RENAMED[raw ?? ''] ?? 'positive';

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
  /* Position in THIS page's rows, or null when the viewer is shut. The shared
   * ImageLightbox walks the grid from there with the arrow keys. */
  const [lightboxAt, setLightboxAt] = useState<number | null>(null);

  /* ?image=<id> — a link from outside naming one photo (a conflict list, a
   * report). The page has to answer "which tray, which page" before it can show
   * it, so the server locates it; nothing is guessed from the URL. */
  const deepLinkImage = Number(params.get('image') ?? 0) || null;
  const tagId = Number(params.get('tag') ?? 0) || null;
  const tray = readTray(params.get('set'));
  /* A reserve tray holds the labels of its sign that the head does NOT train
   * on. One flag, so the four places that used to compare against the literal
   * 'reserve' cannot drift apart. */
  const inReserve = tray.endsWith('_reserve');
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

  /* Resolve the deep link, then send the page to it: the tray it lives in and
   * the page its row falls on. Both come from the server's rank so they cannot
   * drift from what the grid will actually render. A 404 (no label for this
   * head, or a holdout image) leaves the page exactly where it was. */
  const locateQ = useQuery({
    queryKey: ['training-set-locate', activeId, deepLinkImage],
    queryFn: () => locateTrainingImage(activeId as number, deepLinkImage as number),
    enabled: activeId != null && deepLinkImage != null,
    retry: false,
  });
  const located = locateQ.data?.data ?? null;
  useEffect(() => {
    if (!located) return;
    const want = Math.floor(located.rank / pageSize) * pageSize;
    if (located.tray === tray && want === offset) return;
    patch({ set: located.tray, offset: want === 0 ? null : String(want) });
    // patch is derived from params each render; the guard above makes this idempotent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [located, pageSize, tray, offset]);

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
        (acc, [k, d]) => ({ ...acc, [k]: Math.max(0, acc[k as keyof typeof acc] as number + (d as number)) }),
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
       * re-marked leaves the reserve and enters its new tray.
       *
       * On the review lane the DRAWN COUNT MUST NOT MOVE — the thousand is a
       * list, not a tray, and shrinking it as the operator works would be the
       * churn the stored draw exists to prevent. What moves there is progress
       * and the two state trays the photo travels between. */
      patchRow(vars.imageId, (r) => ({
        ...r, state: vars.state, source: 'human', in_training: true,
        excluded_reason: vars.state === 'excluded' ? 'pruned' : null,
      }));
      /* A mark you set is admitted, so the row lands in its new sign's TRAINING
       * tray wherever it came from. */
      if (vars.state !== (tray.endsWith('_reserve') ? tray.slice(0, -'_reserve'.length) : tray)) {
        bumpTrays({ [tray]: -1, [vars.state]: +1 } as Partial<Record<Tray, number>>);
      }
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
        in_training: !inReserve,
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
      const home = tray.endsWith('_reserve') ? (tray.slice(0, -'_reserve'.length) as Tray) : tray;
      const rest = `${home}_reserve` as Tray;
      bumpTrays(vars.into ? { [rest]: -moved.length, [home]: +moved.length }
                          : { [home]: -moved.length, [rest]: +moved.length });
      qc.invalidateQueries({ queryKey: ['training-set-heads'] });
      pushToast('ok', vars.into ? `${moved.length} moved into the training set`
                                : `${moved.length} returned to reserve`);
    },
    onError: (e: Error) => pushToast('err', e.message),
  });
  /* Rows a page-wide move would touch: everything still on the tray's side.
   * Empty outside the two trays where membership is a question at all. */
  const canMove = (MEMBERSHIP_TRAYS as readonly string[]).includes(tray);
  const movable = canMove
    ? rows.filter((r) => r.in_training === !inReserve && !changed.has(r.image_id))
    : [];
  const willMove = new Set(movable.map((r) => r.image_id));

  /* The enlarged photo is the SHARED viewer (components/ImageLightbox) — the
   * same one the listing gallery and the labeling grids open, so arrow-key
   * walking, Escape layering, the focus trap and the scroll lock behave here
   * exactly as they do there. It takes ImagePublic rows; a training row already
   * carries the only two fields imageSrc reads, and the badges the viewer draws
   * self-guard on null, so the row is widened rather than re-fetched — at
   * 10,000 tiles a page, fetching a full image row per tile would be the
   * expensive way to show the same photo. `sreality_id` and `sequence` are not
   * on a training row and nothing here reads them. */
  /* Scroll the linked tile into view once, the first time it renders. Keyed on
   * the id so paging away and back does not re-yank the viewport. */
  const scrolledTo = useRef<number | null>(null);
  const highlightRef = (el: HTMLLIElement | null) => {
    if (!el || deepLinkImage == null || scrolledTo.current === deepLinkImage) return;
    scrolledTo.current = deepLinkImage;
    const still = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    // Optional call: jsdom has no scrollIntoView, and a ringed tile the operator
    // has to scroll to themselves beats a ref callback that throws mid-commit.
    el.scrollIntoView?.({ block: 'center', behavior: still ? 'auto' : 'smooth' });
  };

  const galleryImages: ImagePublic[] = useMemo(
    () => rows.map((r) => ({
      id: r.image_id,
      sreality_id: 0,
      sequence: null,
      sreality_url: '',
      storage_path: r.storage_path,
      clip_fine_tag: null,
      clip_logical_tag: null,
      clip_confidence: null,
      clip_render_score: null,
      phash: null,
    })),
    [rows],
  );

  /* THE DRAW. A head trains on a thousand negatives picked at random, not on
   * every negative it has; the rest wait in the negative reserve. Drawing again
   * replaces the whole set, discarding the review the old one had, so it asks
   * first. Membership is the record — there is no separate sample. */
  const [drawSize, setDrawSize] = useState(1000);
  const drawMut = useMutation({
    mutationFn: () => drawTrainingSet(activeId as number, { state: 'negative', size: drawSize }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['training-set-heads'] });
      qc.invalidateQueries({ queryKey: ['training-set'] });
      patch({ set: 'negative', offset: null });
      pushToast('ok', `${res.data.drawn} negatives drawn at random`);
    },
    onError: (e: Error) => pushToast('err', e.message),
  });

  /* WHERE THE OPERATOR GOT TO. Three-valued, because "set aside on purpose" is
   * a decision and must not read as "nobody has said" (migration 487). It
   * writes one column and nothing reads it — no gate, no training effect. */
  const patchHeadState = (next: ReviewState) =>
    qc.setQueryData(['training-set-heads'], (old: typeof headsQ.data) => old && ({
      ...old,
      data: old.data.map((h) => h.id === activeId ? { ...h, review_state: next } : h),
    }));
  const reviewMut = useMutation({
    mutationFn: (state: ReviewState) =>
      setNewDedupTagFlags(activeId as number, { review_state: state }),
    onMutate: (state) => {
      const was = activeHead?.review_state ?? 'not_ready';
      patchHeadState(state);
      return { was };
    },
    onError: (e: Error, _state, ctx) => {
      patchHeadState(ctx?.was ?? 'not_ready');
      pushToast('err', e.message);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['training-set-heads'] }),
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

  /* The marks, named once. The tile and the focus view render the SAME three
   * decisions; two lists would drift the moment one gained a state. */
  const MARKS = [
    { v: 'positive' as const, label: '✓ applies', key: 'a' },
    { v: 'negative' as const, label: '✕ no', key: 's' },
    { v: 'excluded' as const, label: '– left out', key: 'd' },
  ];

  /* One place that knows how a decision is made, so a keystroke and a click
   * cannot diverge. Both go through correctMut, so the optimistic patch, the
   * tray counts and the rollback are identical either way. */
  const markAt = (idx: number, state: TagState) => {
    const r = rows[idx];
    if (!r || correctMut.isPending) return;
    correctMut.mutate({ imageId: r.image_id, state, from: r.state,
                        fromMine: r.source !== 'machine' });
  };
  const moveAt = (idx: number) => {
    const r = rows[idx];
    if (!r || !canMove || moveMut.isPending) return;
    moveMut.mutate({ imageIds: [r.image_id], into: !r.in_training });
  };
  /* a / s / d / backspace — the operator's own mapping. Left hand on the marks,
   * right hand on the arrows. */
  const zoomShortcuts = {
    ...Object.fromEntries(MARKS.map(({ v, key }) => [key, (i: number) => markAt(i, v)])),
    backspace: moveAt,
  };

  /* The control bar inside the focus view. Deciding is why the photo is open —
   * closing it to reach the marks would make one judgement three clicks — and
   * the viewer STAYS open afterwards so the arrow keys walk straight to the
   * next one. The row is patched in place, so the page behind updates and the
   * position never shifts under the viewer. */
  const zoomActions = (idx: number) => {
    const r = rows[idx];
    if (!r) return null;
    return (
      <div className="flex flex-col gap-1.5">
        <div className="flex gap-1.5">
          {MARKS.map(({ v, label, key }) => (
            <button
              key={v}
              type="button"
              data-testid={`zoom-${v}`}
              aria-pressed={r.state === v}
              disabled={correctMut.isPending}
              onClick={() => markAt(idx, v)}
              className={`flex-1 py-1.5 text-sm rounded-[var(--radius-sm)] border transition-colors ${
                r.state === v
                  ? 'border-[var(--color-copper)] bg-[var(--color-paper)]/10 text-[var(--color-paper)]'
                  : 'border-[var(--color-paper)]/25 text-[var(--color-ink-4)] hover:text-[var(--color-paper)] hover:bg-[var(--color-paper)]/10'}`}
            >
              {label} <kbd className="ml-1 opacity-60">{key.toUpperCase()}</kbd>
            </button>
          ))}
        </div>
        {canMove && (
          <button
            type="button"
            data-testid="zoom-move"
            disabled={moveMut.isPending}
            onClick={() => moveAt(idx)}
            className="py-1 text-xs rounded-[var(--radius-sm)] border border-[var(--color-paper)]/25 text-[var(--color-ink-4)] hover:text-[var(--color-paper)] hover:bg-[var(--color-paper)]/10"
          >
            {r.in_training ? '↩ return to reserve' : '→ move to training'}
            <kbd className="ml-1 opacity-60">⌫</kbd>
          </button>
        )}
      </div>
    );
  };

  const tile = (r: TrainingSetRow, idx: number) => {
    const mine = r.source !== 'machine';
    const ref: ImageRef = { storage_path: r.storage_path, sreality_url: '' };
    const ch = changed.get(r.image_id);
    return (
      <li
        key={r.image_id}
        ref={r.image_id === deepLinkImage ? highlightRef : undefined}
        data-testid={`training-tile-${r.image_id}`}
        data-linked={r.image_id === deepLinkImage ? 'true' : undefined}
        data-state={r.state}
        data-previewed={previewing && willMove.has(r.image_id) ? 'true' : undefined}
        className={`rounded-[var(--radius-sm)] border p-1.5 flex flex-col gap-1.5 transition-opacity ${STATE_STYLE[r.state]} ${
          r.image_id === deepLinkImage
            ? 'ring-2 ring-[var(--color-copper)] ring-offset-2 ring-offset-[var(--color-paper)]' : ''
        } ${
          previewing ? (willMove.has(r.image_id)
            ? 'ring-2 ring-[var(--color-sage)] ring-offset-1 ring-offset-[var(--color-paper)]' : 'opacity-40') : ''
        }`}
      >
        <button
          type="button"
          data-testid={`open-${r.image_id}`}
          title="Open the photo large. Arrow keys walk the page; Escape closes."
          onClick={() => setLightboxAt(idx)}
          className={`block w-full bg-[var(--color-paper-2)] rounded-[var(--radius-xs)] ${large ? 'h-56' : 'h-28'}`}>
          <img src={imageSrc(ref)} alt={`Training image ${r.image_id}`} loading="lazy"
               className="w-full h-full object-contain rounded-[var(--radius-xs)]" />
        </button>
        {/* No "machine / yours": the operator's ruling is that a label in a tray
          * is a label, and a set of a thousand is a thousand. The human-wins
          * upsert rail still holds in the database; it is simply not a thing to
          * read on every tile. */}
        <div className="flex items-center gap-1 text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)]">
          {r.definition_stale && (
            <span className="text-[var(--color-copper)]" title="Written under wording you have since replaced. Not necessarily wrong — but it followed a rule that has changed.">· old wording</span>
          )}
        </div>
        <div className="flex gap-1">
          {MARKS.map(({ v, label }) => (
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
              {label}
            </button>
          ))}
        </div>
        {/* One photo at a time: admit it, or send it back. Only on the two
          * trays where membership means anything — see MEMBERSHIP_TRAYS. */}
        {canMove && (
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
        )}
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
            {ordered.map((h) => (
              <option key={h.id} value={h.id}>
                {REVIEW_MARK[h.review_state]}{h.label} · {h.positive}
              </option>
            ))}
          </select>

          {activeHead && (
            <span className="flex gap-1" role="radiogroup" aria-label="review state"
                  title="Where you got to on this head. It stores nothing but this — no gate, no training effect.">
              {REVIEW_ORDER.map((st) => (
                <button
                  key={st}
                  type="button"
                  role="radio"
                  data-testid={`review-${st}`}
                  aria-checked={activeHead.review_state === st}
                  disabled={reviewMut.isPending}
                  onClick={() => reviewMut.mutate(st)}
                  className={`px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border ${
                    activeHead.review_state === st
                      ? `text-[var(--color-ink)] ${REVIEW_ON[st]}`
                      : 'border-[var(--color-rule)] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)]'}`}
                >
                  {REVIEW_LABEL[st]}
                </button>
              ))}
            </span>
          )}

          <span className="flex gap-1" role="group" aria-label="tray">
            {TRAYS.map((t) => (
              <button key={t} type="button" title={TRAY_TITLE[t]} aria-pressed={tray === t}
                onClick={() => patch({ set: t, offset: null })}
                className={`px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border ${
                  tray === t ? 'border-[var(--color-sage)] text-[var(--color-ink)]'
                    : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]'}`}>
                {TRAY_LABEL[t]}
                {/* EVERY chip is a count of what is in that tray. The sample chip
                  * showed `decided/drawn`, and 35/1000 read as "35 drawn" — the
                  * operator reported exactly that. Progress belongs in the line
                  * below, which has room to name which number is which. */}
                {activeHead && (
                  <span data-testid={`tray-count-${t}`} className="ml-1 text-[var(--color-ink-4)] tabular-nums">
                    {activeHead[t]}
                  </span>
                )}
              </button>
            ))}
          </span>
        </div>

        {activeHead && (
          <p className="mt-2 text-xs text-[var(--color-ink-3)]" data-testid="head-summary">
            Trains on {activeHead.positive} positives and {activeHead.negative} negatives ·{' '}
            {activeHead.positive_reserve} + {activeHead.negative_reserve} in reserve ·{' '}
            {activeHead.excluded} left out
          </p>
        )}

        {activeHead && (
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs" data-testid="draw-controls">
            <span className="text-[var(--color-ink-3)]">
              Trains on <b className="tabular-nums">{activeHead.negative.toLocaleString()}</b> negatives,
              drawn at random from the{' '}
              <b className="tabular-nums">{(activeHead.negative + activeHead.negative_reserve).toLocaleString()}</b>{' '}
              this head has. Ten thousand is not a reviewable number; a drawn thousand is.
            </span>
            <label className="flex items-center gap-1">
              <span className="sr-only">draw size</span>
              <input type="number" min={1} max={5000} step={100} value={drawSize}
                onChange={(e) => setDrawSize(Math.max(1, Math.min(5000, Number(e.target.value) || 1)))}
                aria-label="draw size"
                className="w-20 px-1.5 py-0.5 rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-transparent text-[var(--color-ink)] tabular-nums" />
            </label>
            <button type="button" data-testid="draw-negatives" disabled={drawMut.isPending}
              onClick={() => {
                if (activeHead.negative > 0 && !window.confirm(
                  `Draw a new set of training negatives for this head? The current `
                  + `${activeHead.negative.toLocaleString()} go back to the negative reserve, including any `
                  + 'you have already been through. Your marks are kept — only what the head trains on changes.')) return;
                drawMut.mutate();
              }}
              className="px-2.5 py-1 rounded-[var(--radius-sm)] border border-[var(--color-sage)] text-[var(--color-ink)] hover:bg-[var(--color-sage)]/10 disabled:opacity-40">
              {drawMut.isPending ? 'drawing…' : activeHead.negative > 0 ? 'Draw a new set' : 'Draw training negatives'}
            </button>
          </div>
        )}
      </header>

      {deepLinkImage != null && (
        <p data-testid="deep-link-note"
           className="mt-3 flex flex-wrap items-center gap-2 rounded-[var(--radius-sm)] border border-[var(--color-copper)] px-3 py-2 text-xs text-[var(--color-ink-2)]">
          {locateQ.isError ? (
            <span>Photo {deepLinkImage} has no label on this head &mdash; nothing to show. Pick another head, or clear the link.</span>
          ) : located ? (
            <span>
              Showing photo <b>{deepLinkImage}</b> in <b>{TRAY_LABEL[located.tray]}</b>, ringed below.
            </span>
          ) : (
            <span>Finding photo {deepLinkImage}&hellip;</span>
          )}
          <button type="button" data-testid="deep-link-clear"
            onClick={() => patch({ image: null })}
            className="text-[var(--color-copper)] hover:underline">
            clear
          </button>
        </p>
      )}

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
              <li><b>Review sample</b> — a random draw from the negatives, shown in the order they already
                had, so photos of a kind still sit together. It appears once you draw one.</li>
            </ul>
            <p className="mt-2 font-medium text-[var(--color-ink)]">Moving photos</p>
            <p className="mt-0.5">
              <b>→ move to training</b> admits one reserve photo; <b>↩ return to reserve</b> takes one out. The
              button under the grid does the whole page at once. Changing a photo&rsquo;s mark also admits it,
              because a mark you set is a decision you have made. These appear only on <b>Training &middot; positive</b>
              and <b>Reserve</b>: every negative trains, and a left-out trains nothing, so there is nothing to admit
              on those two trays. Take a negative out by re-marking it.
            </p>
            <p className="mt-2 font-medium text-[var(--color-ink)]">Where you got to</p>
            <p className="mt-0.5">
              A marker for you alone, next to the head picker: <b>Not ready</b>, <b>✓ Ready</b>, or
              <b> Skip for now</b> for a head you are deliberately setting aside. It stores nothing
              but that — it does not gate training, change a count or touch a photo. The dropdown
              carries the same mark (✓ ready, – skipped), so the list of heads is itself the
              progress view.
            </p>
            <p className="mt-2 font-medium text-[var(--color-ink)]">Reviewing the negatives</p>
            <p className="mt-0.5">
              Ten thousand negatives is not a job that finishes, so <b>Draw a review sample</b> picks a
              thousand of them at random and writes that list down. It is written down on purpose: a sample
              worked out fresh on every read would quietly swap in a new photo each time you re-marked one.
              The draw is random, but the lane keeps the order the negatives already had. Nothing leaves the
              lane as you decide — a photo you re-mark stays where it is wearing its new mark, and the
              counter moves.
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
              <li>Click the photo to open it large, in the same viewer the listing pages use. The
                same marks are under it there, and it stays open after one &mdash; so arrow keys
                walk the page and you can decide without closing. <b>A</b> applies, <b>S</b> no,
                <b> D</b> left out, <b>backspace</b> moves it in or out of training. Escape closes.</li>
              <li><b>machine</b> / <b>yours</b> says who decided the current mark.</li>
              <li><b>→ move to training</b> / <b>↩ return to reserve</b> is membership, separate from the mark — on the positive and reserve trays only.</li>
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
            {rows.map((r, i) => tile(r, i))}
          </ul>
          {movable.length > 0 && (
            <div className="mt-4 flex flex-col items-center gap-1">
              <button type="button" data-testid="move-page" disabled={moveMut.isPending}
                onMouseEnter={() => setPreviewing(true)} onMouseLeave={() => setPreviewing(false)}
                onFocus={() => setPreviewing(true)} onBlur={() => setPreviewing(false)}
                onClick={() => { setPreviewing(false); moveMut.mutate({ imageIds: movable.map((r) => r.image_id), into: inReserve }); }}
                className="px-3 py-1.5 text-xs rounded-[var(--radius-sm)] border border-[var(--color-sage)] text-[var(--color-ink)] hover:bg-[var(--color-sage)]/10 disabled:opacity-40">
                {inReserve
                  ? `→ Move all ${movable.length} on this page into the training set`
                  : `↩ Return all ${movable.length} on this page to reserve`}
              </button>
              <p className="text-[0.7rem] text-[var(--color-ink-4)] text-center max-w-prose">
                {inReserve
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

      {lightboxAt != null && galleryImages.length > 0 && (
        <ImageLightbox
          images={galleryImages}
          startIndex={lightboxAt}
          onClose={() => setLightboxAt(null)}
          actionsAt={zoomActions}
          shortcuts={zoomShortcuts}
        />
      )}
    </div>
  );
}
