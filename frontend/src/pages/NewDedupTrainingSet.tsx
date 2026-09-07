import { useId, useMemo, useState } from 'react';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import {
  deleteTagLabelNote,
  editTagLabelNote,
  listTrainingSet,
  listTrainingSetHeads,
  setNewDedupTagAnnotation,
  setTrainingTarget,
  type TagState,
  type TrainingSetHead,
  type TrainingSetRow,
} from '@/lib/api';
import { imageSrc, type ImageRef } from '@/lib/imageUrl';
import Spinner from '@/components/Spinner';
import ErrorBanner from '@/components/ErrorBanner';
import ImageSizeToggle from '@/components/ImageSizeToggle';
import { pushToast } from '@/lib/toast';

/* NEW DEDUP · Training set — a head's material, sorted into four trays.
 *
 * The operator's ask (2026-09-07): no filter algebra. Four trays, each the
 * exact list a probe will meet — or deliberately will not — and nothing else:
 *   positive  — in the set: the positives a head trains on, up to the target
 *   negative  — the negatives a head trains on: the operator's own, no others
 *   reserve   — positives past the target; step in when a set positive leaves
 *   left out  — excluded: the subject is there but the photo is of something
 *               else; trains nothing, grades nothing
 * A tray is not a filter combination the operator composes; it is one server
 * query each, so the tray and the trainer cannot disagree. "Training · negative"
 * is the human negatives only, because that is the only door the trainer reads
 * negatives through — the machine's negatives are in no tray at all, and the
 * page says how many there are rather than hiding them.
 *
 * A correction here is a HUMAN label and therefore final: the store's
 * human-wins rail means a later machine pass can never overwrite it. That is
 * why the tile says who decided. No bulk "confirm the rest": the operator
 * finalises a set by looking at it and moving what is wrong, then says so.
 *
 * THE HOLDOUT IS NOT HERE. Those images grade the model; correcting one from a
 * training surface would quietly train on the yardstick.
 */

type Tray = 'positive' | 'negative' | 'reserve' | 'excluded';
const TRAYS: readonly Tray[] = ['positive', 'negative', 'reserve', 'excluded'];

const TRAY_LABEL: Record<Tray, string> = {
  positive: 'Training · positive',
  negative: 'Training · negative',
  reserve: 'Reserve',
  excluded: 'Left out',
};

const TRAY_TITLE: Record<Tray, string> = {
  positive: 'What a probe trains on as positives: your positives first, then the machine’s oldest-first, up to the target',
  negative: 'What a probe trains on as negatives: your own “no” labels. The machine’s negatives are not training material',
  reserve: 'Positives past the target. One steps into the set by itself when a set positive is removed',
  excluded: 'The subject is there but the photo is of something else. Trains nothing, grades nothing',
};

/* The tray a corrected tile now belongs to, in the tray’s own words. */
const STATE_TRAY: Record<TagState, string> = {
  positive: 'Training · positive (or Reserve, past the target)',
  negative: 'Training · negative',
  excluded: 'Left out',
};

const TRAY_STYLE: Record<Tray, string> = {
  positive: 'border-[var(--color-sage)] bg-[var(--color-sage)]/10',
  negative: 'border-[var(--color-rule)]',
  reserve: 'border-[var(--color-sage)]/50',
  excluded: 'border-dashed border-[var(--color-copper)]',
};

const TILE_STYLE: Record<TagState, string> = {
  positive: TRAY_STYLE.positive,
  negative: TRAY_STYLE.negative,
  excluded: TRAY_STYLE.excluded,
};

/* Page sizes the operator asked for; 2000 is a whole set at the default
 * target. Images lazy-load, so the cost is the DOM and it is theirs to choose. */
const PAGE_SIZES = [50, 100, 500, 2000] as const;
type PageSize = (typeof PAGE_SIZES)[number];
const DEFAULT_PAGE: PageSize = 50;

/* Older links carried the review page’s cutoff presets; they all land on the
 * positive tray, which is where the set is. */
function readTray(raw: string | null): Tray {
  return (TRAYS as readonly string[]).includes(raw ?? '') ? (raw as Tray) : 'positive';
}

/* Each tray is ONE server query. The negative tray narrows to the operator’s
 * labels because that is exactly what tag_holdout.training_label_rows feeds a
 * head; the positive and reserve trays are the two halves of the ranked
 * positives, split at the target. Without a cutoff (migration 474 pending)
 * there is no boundary: the positive tray is every positive. */
function trayQuery(tray: Tray, cutoffReady: boolean) {
  switch (tray) {
    case 'positive':
      return cutoffReady
        ? { state: 'positive' as const, membership: 'set' as const }
        : { state: 'positive' as const };
    case 'negative':
      return { state: 'negative' as const, source: 'human' as const };
    case 'reserve':
      return { state: 'positive' as const, membership: 'reserve' as const };
    case 'excluded':
      return { state: 'excluded' as const };
  }
}

function trayTotal(tray: Tray, head: TrainingSetHead | null, cutoffReady: boolean): number | null {
  if (!head) return null;
  switch (tray) {
    case 'positive': return cutoffReady ? head.in_set : head.positive;
    case 'negative': return head.human_negative;
    case 'reserve': return cutoffReady ? head.reserve : null;
    case 'excluded': return head.excluded;
  }
}

export default function NewDedupTrainingSet() {
  const headId = useId();
  const [params, setParams] = useSearchParams();
  const qc = useQueryClient();
  const [large, setLarge] = useState(false);
  /* Tiles whose mark changed in THIS session, with what they showed before —
   * the note field appears only there, and from_state travels with the note
   * so the reason is recorded against the change it explains. */
  const [changed, setChanged] = useState<Map<number, { from: TagState; to: TagState }>>(new Map());
  const [drafts, setDrafts] = useState<Map<number, string>>(new Map());
  const [editingNote, setEditingNote] = useState<Set<number>>(new Set());

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

  const headsQ = useQuery({
    queryKey: ['training-set-heads'],
    queryFn: () => listTrainingSetHeads(),
  });
  const heads = headsQ.data?.data ?? [];
  /* Heads with the most material first: an alphabetical list buries the answer. */
  const ordered = useMemo(
    () => [...heads].sort((a, b) => b.positive - a.positive || a.label.localeCompare(b.label)),
    [heads],
  );
  const activeId = tagId ?? ordered[0]?.id ?? null;
  const activeHead = ordered.find((h) => h.id === activeId) ?? null;
  const cutoffReady = activeHead?.cutoff_available !== false;
  const query = trayQuery(tray, cutoffReady);

  const rowsKey = ['training-set', activeId, tray, cutoffReady, offset, pageSize];
  const rowsQ = useQuery({
    queryKey: rowsKey,
    queryFn: () => listTrainingSet({ tag_id: activeId as number, ...query, limit: pageSize, offset }),
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
  const total = trayTotal(tray, activeHead, cutoffReady);
  const lastOffset = total == null ? null
    : Math.max(0, Math.floor(Math.max(0, total - 1) / pageSize) * pageSize);

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
      /* PATCH THE LIST, NEVER REFETCH IT under a correcting hand: a refetch
       * would drop the tile from this tray before the operator could say why.
       * The tile stays, showing its new mark; the tray counts refetch. */
      qc.setQueryData(rowsKey, (old: typeof rowsQ.data) => old && ({
        ...old,
        data: {
          ...old.data,
          rows: old.data.rows.map((row) => row.image_id === vars.imageId
            ? { ...row, state: vars.state, source: 'human' as const,
                excluded_reason: vars.state === 'excluded' ? ('pruned' as const) : null }
            : row),
        },
      }));
      qc.invalidateQueries({ queryKey: ['training-set-heads'] });
    },
    onError: (e: Error) => pushToast('err', e.message),
  });

  const patchRowNote = (imageId: number, note: { id: number; note: string } | null) =>
    qc.setQueryData(rowsKey, (old: typeof rowsQ.data) => old && ({
      ...old,
      data: {
        ...old.data,
        rows: old.data.rows.map((row) => row.image_id === imageId
          ? { ...row, note_id: note?.id ?? null, note: note?.note ?? null } : row),
      },
    }));

  const noteMut = useMutation({
    /* A note is written by re-stating the CURRENT mark with the reason
     * attached — one write path for mark and reason. */
    mutationFn: ({ imageId, text, to, from }: {
      imageId: number; text: string; to: TagState; from: TagState | null;
    }) => setNewDedupTagAnnotation(
      activeId as number, imageId, to,
      to === 'excluded' ? 'pruned' : null, { text, from_state: from },
    ),
    onSuccess: (res, vars) => {
      const data = res.data as {
        note_unavailable?: string; note?: { id: number; note: string } | null;
      };
      if (data.note_unavailable) {
        pushToast('err', `Your mark is saved, but the note was not: ${data.note_unavailable}`);
        return;
      }
      setDrafts((prev) => { const n = new Map(prev); n.delete(vars.imageId); return n; });
      setChanged((prev) => { const n = new Map(prev); n.delete(vars.imageId); return n; });
      if (data.note) patchRowNote(vars.imageId, data.note);
      pushToast('ok', 'Note saved');
    },
    onError: (e: Error) => pushToast('err', e.message),
  });

  const editNoteMut = useMutation({
    mutationFn: ({ noteId, text }: { noteId: number; text: string; imageId: number }) =>
      editTagLabelNote(noteId, text),
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
        className={`rounded-[var(--radius-sm)] border p-1.5 flex flex-col gap-1.5 ${TILE_STYLE[r.state as TagState] ?? ''}`}
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
          {r.state === 'positive' && r.in_set != null && (
            <span
              data-testid={`membership-${r.image_id}`}
              className={r.in_set ? 'text-[var(--color-sage)]' : ''}
              title={r.in_set
                ? `Position ${r.set_rank} in the set. The highest positions are the most recent arrivals from the reserve.`
                : `Reserve, position ${r.set_rank} — steps into the set when a set positive is removed`}
            >
              · {r.in_set ? 'in set' : 'reserve'} #{r.set_rank}
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
          <p className="text-[0.65rem] text-[var(--color-ink-3)] leading-snug">
            Now under <b>{STATE_TRAY[changed.get(r.image_id)!.to]}</b> · <b>Yours</b>.
            Stays here until you change tray or leave the page.
          </p>
        )}
        {r.note && !editingNote.has(r.image_id) && (
          <div data-testid={`note-saved-${r.image_id}`} className="text-[0.7rem] leading-snug">
            <p className="text-[var(--color-ink-2)] text-pretty">“{r.note}”</p>
            <span className="flex gap-2 mt-0.5">
              <button
                type="button"
                onClick={() => {
                  setDrafts((prev) => new Map(prev).set(r.image_id, r.note ?? ''));
                  setEditingNote((prev) => new Set(prev).add(r.image_id));
                }}
                className="text-[var(--color-copper)] hover:underline"
              >
                edit note
              </button>
              <button
                type="button"
                disabled={deleteNoteMut.isPending}
                onClick={() => deleteNoteMut.mutate({
                  noteId: r.note_id as number, imageId: r.image_id })}
                className="text-[var(--color-ink-4)] hover:underline"
              >
                remove
              </button>
            </span>
          </div>
        )}
        {!r.note && !changed.has(r.image_id) && !editingNote.has(r.image_id) && (
          <button
            type="button"
            data-testid={`note-add-${r.image_id}`}
            onClick={() => setEditingNote((prev) => new Set(prev).add(r.image_id))}
            className="self-start text-[0.7rem] text-[var(--color-ink-4)] hover:text-[var(--color-copper)] hover:underline"
          >
            + note
          </button>
        )}
        {(changed.has(r.image_id) || editingNote.has(r.image_id)) && (
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
                noteMut.mutate({
                  imageId: r.image_id, text,
                  to: changed.get(r.image_id)?.to ?? (r.state as TagState),
                  from: changed.get(r.image_id)?.from ?? null,
                });
              }
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
              disabled={noteMut.isPending || editNoteMut.isPending
                || !(drafts.get(r.image_id) ?? '').trim()}
              className="px-2 py-0.5 text-[0.7rem] rounded-[var(--radius-xs)] border border-[var(--color-copper)] text-[var(--color-ink)] disabled:opacity-40"
            >
              save
            </button>
            {editingNote.has(r.image_id) && (
              <button
                type="button"
                onClick={() => {
                  setDrafts((prev) => { const n = new Map(prev); n.delete(r.image_id); return n; });
                  setEditingNote((prev) => { const n = new Set(prev); n.delete(r.image_id); return n; });
                }}
                className="px-2 py-0.5 text-[0.7rem] text-[var(--color-ink-4)] hover:underline"
              >
                cancel
              </button>
            )}
          </form>
        )}
      </li>
    );
  };

  /* The tray’s count, in the tray’s own terms. Positive reads against the
   * target because the set is defined by it; reserve needs the cutoff to exist. */
  const trayCount = (t: Tray) => {
    if (!activeHead) return null;
    if (t === 'positive' && cutoffReady) return `${activeHead.in_set} / ${activeHead.target}`;
    if (t === 'reserve' && !cutoffReady) return null;
    const n = trayTotal(t, activeHead, cutoffReady);
    return n == null ? null : String(n);
  };

  return (
    <div className="max-w-[112rem] mx-auto px-4 py-6">
      <header className="border-b border-[var(--color-rule)] pb-3">
        <div className="flex items-baseline justify-between flex-wrap gap-3">
          <div>
            <h1 className="text-lg font-medium text-[var(--color-ink)]">Training set</h1>
            <p className="text-xs text-[var(--color-ink-3)] mt-0.5 max-w-prose text-pretty">
              One head at a time, in four trays: what it will train on as positive and as
              negative, what waits in reserve, and what is left out. Move a wrong photo with its
              buttons; the move is yours and final. The sealed exam images are excluded: they grade
              the model, so they never appear on a training surface.
            </p>
          </div>
          <ImageSizeToggle
            large={large}
            onChange={setLarge}
            label="Training grid image size"
          />
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
          <span className="flex items-center gap-2">
            <label className="text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)]" htmlFor={headId}>
              head
            </label>
            <select
              id={headId}
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
          </span>

          {/* The four trays are the whole navigation of this page: one row,
            * each with the size of what it holds. No filter combines with it. */}
          <nav role="tablist" aria-label="tray" className="flex flex-wrap items-stretch gap-1">
            {TRAYS.map((t) => {
              const count = trayCount(t);
              const disabled = t === 'reserve' && !cutoffReady;
              return (
                <button
                  key={t}
                  type="button"
                  role="tab"
                  aria-selected={tray === t}
                  disabled={disabled}
                  title={disabled ? 'No cutoff yet (migration 474): nothing is in reserve' : TRAY_TITLE[t]}
                  onClick={() => patch({ set: t, offset: null })}
                  className={`px-3 py-1.5 text-xs rounded-[var(--radius-sm)] border flex items-baseline gap-2 transition-colors disabled:opacity-40 ${
                    tray === t
                      ? `${TRAY_STYLE[t]} text-[var(--color-ink)]`
                      : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]'
                  }`}
                >
                  <span>{TRAY_LABEL[t]}</span>
                  {count != null && (
                    <span className="tabular-nums text-[var(--color-ink-4)]">{count}</span>
                  )}
                </button>
              );
            })}
          </nav>
        </div>

        {activeHead && (
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[0.7rem] text-[var(--color-ink-4)]">
            {cutoffReady ? (
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
                <label htmlFor="target" className="text-[0.65rem] tracking-[0.1em] uppercase">
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
            ) : (
              <span data-testid="cutoff-unavailable" className="text-[var(--color-copper)]">
                No cutoff yet (migration 474 not applied): the positive tray is every positive,
                and there is no reserve.
              </span>
            )}
            <span>
              positives: {activeHead.human_positive} yours, {activeHead.machine_positive} the machine’s
              {cutoffReady && ' — yours first, then the machine’s oldest-first, up to the target'}
            </span>
            <span
              data-testid="machine-negatives"
              title="A head trains only on your negatives. The machine’s “no” is not in any tray; it neither trains nor grades."
            >
              {activeHead.machine_negative} machine negatives train nothing
            </span>
          </div>
        )}
      </header>

      {rowsQ.isLoading ? (
        <div className="py-10 flex justify-center"><Spinner /></div>
      ) : rowsQ.error ? (
        <div className="mt-4"><ErrorBanner message={(rowsQ.error as Error).message} /></div>
      ) : rows.length === 0 ? (
        <div className="mt-10 text-center text-sm text-[var(--color-ink-2)]">
          <p>Nothing in <b>{TRAY_LABEL[tray]}</b> for this head yet.</p>
          {tray === 'negative' && (
            <p className="mt-1 text-[var(--color-ink-3)]">
              Only your own “no” labels count here; the machine’s do not train.
            </p>
          )}
        </div>
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
          <div className="mt-4 flex items-center justify-center gap-3 text-xs flex-wrap">
            <span className="flex items-center gap-1" role="group" aria-label="per page">
              <span className="text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)] mr-0.5">per page</span>
              {PAGE_SIZES.map((n) => (
                <button
                  key={n}
                  type="button"
                  aria-pressed={pageSize === n}
                  onClick={() => patch({ n: String(n), offset: null })}
                  className={`px-2 py-1 rounded-[var(--radius-sm)] border ${
                    pageSize === n
                      ? 'border-[var(--color-ink-2)] text-[var(--color-ink)]'
                      : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]'
                  }`}
                >
                  {n}
                </button>
              ))}
            </span>
            <button
              type="button"
              disabled={offset === 0}
              onClick={() => patch({ offset: String(Math.max(0, offset - pageSize)) })}
              className="px-3 py-1 rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-3)] disabled:opacity-40"
            >
              ← previous
            </button>
            <span data-testid="page-range" className="text-[var(--color-ink-4)] tabular-nums">
              {offset + 1}–{offset + rows.length}{total != null && ` of ${total}`}
            </span>
            <button
              type="button"
              disabled={rows.length < pageSize}
              onClick={() => patch({ offset: String(offset + pageSize) })}
              className="px-3 py-1 rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-3)] disabled:opacity-40"
            >
              next →
            </button>
            {lastOffset != null && lastOffset > offset && (
              /* The reserve refills the set from the bottom, so the newest
               * arrivals are always on the LAST page — one click away. */
              <button
                type="button"
                data-testid="jump-last"
                title="The newest arrivals from the reserve are always at the end"
                onClick={() => patch({ offset: String(lastOffset) })}
                className="px-3 py-1 rounded-[var(--radius-sm)] border border-[var(--color-sage)] text-[var(--color-ink)]"
              >
                last page ⇥
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
