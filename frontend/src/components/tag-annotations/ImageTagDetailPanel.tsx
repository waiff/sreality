import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  bulkSetNewDedupImageTags,
  listNewDedupImageTags,
  setNewDedupTagAnnotation,
  type NewDedupImageTag,
  type TagExcludedReason,
  type TagState,
} from '@/lib/api';
import { pushToast } from '@/lib/toast';
import ErrorBanner from '@/components/ErrorBanner';
import {
  NEW_DEDUP_CANDIDATES_KEY,
  NEW_DEDUP_OVERVIEW_KEY,
  NEW_DEDUP_TAG_IMAGES_KEY,
  newDedupImageTagsKey,
} from '@/lib/newDedupKeys';
import TriStateControl, {
  BATCH_ACTIONS,
  EXCLUDED_REASON_META,
  STATE_META,
  isManufactured,
} from './TriStateControl';

/* One CHANGED cell, reported to whichever page opened the panel. Both pages get
 * the same object for the same write — two narrower callbacks would let them
 * disagree about what one click meant. */
export interface ImageTagChange {
  imageId: number;
  tagId: number;
  label: string;
  /* Never 'untouched': the panel writes decisions and cannot clear a cell.
   * "Absence is not a negative" cuts both ways — a human looked, and that fact
   * is not discardable. */
  state: TagState;
  excludedReason: TagExcludedReason | null;
}

/* The four outcomes an image can get on the tag the operator came FROM, in
 * TAG_STATES' order with `excluded` split into its two reasons — exactly the
 * split BATCH_ACTIONS already makes.
 *
 * Words, not the three glyphs, and only here. With ⊘ + a reason chip the
 * DANGEROUS answer (negative, which poisons a head whose subject really is in
 * the photo) costs one click and the answer the operator's own labeling rule
 * demands (excluded · pruned) costs two plus a hunt. Naming the four and
 * pricing them identically is the minimum intervention that inverts that. */
const SUBJECT_ACTIONS: ReadonlyArray<{
  key: string;
  state: TagState;
  reason: TagExcludedReason | null;
  label: string;
  title: string;
}> = [
  {
    key: 'keeps',
    state: 'positive',
    reason: null,
    label: 'keeps it',
    title: "It does belong to this tag after all — leaves it in this tag's contents.",
  },
  {
    key: 'not-this',
    state: 'negative',
    reason: null,
    label: 'not this tag',
    title: 'It simply is not this tag. A real, valuable negative the classifier learns from.',
  },
  {
    key: 'elsewhere',
    state: 'excluded',
    reason: 'pruned',
    label: 'belongs elsewhere',
    title:
      "The subject IS substantially here, but another tag fits better. Excluded and pruned — marking it negative would poison this tag's head. Pruned exclusions are kept out of the ambiguity rate entirely.",
  },
  {
    key: 'cant-tell',
    state: 'excluded',
    reason: 'ambiguous',
    label: "can't tell",
    title:
      "Nobody could decide, and a careful human probably could not either. Counts toward this tag's ambiguity rate — a high rate means the DEFINITION needs fixing, not more labeling.",
  },
];

/* Which of the four the cell is in now. An excluded cell with no reason reads
 * as ambiguous — the same fallback TriStateControl and the ambiguity rate both
 * use, because the three must not disagree about one cell. */
const pressedActionKey = (row: NewDedupImageTag): string | null => {
  if (row.state === 'positive') return 'keeps';
  if (row.state === 'negative') return 'not-this';
  if (row.state === 'excluded')
    return (row.excluded_reason ?? 'ambiguous') === 'pruned' ? 'elsewhere' : 'cant-tell';
  return null;
};

/* Image-centric detail: every active tag on ONE image, grouped by family,
 * each with the same tri-state control — the "open kitchen-living room"
 * case (kitchen positive, living_room excluded, everything else negative)
 * needs to be set in one sitting without hunting through per-tag screens.
 *
 * Opened from the definitions workbench it also carries a SUBJECT: the tag
 * whose contents the operator is reading. That tag is pulled out of its family
 * group and pinned at the top with four word-labeled outcomes, because from
 * there the act is a correction to one cell and the three ways an image can
 * leave a tag must not collapse into one. */
export default function ImageTagDetailPanel({
  imageId,
  onClose,
  onTagStateChange,
  subjectTagId,
}: {
  imageId: number;
  onClose: () => void;
  /* Fired once per CHANGED cell — once for a single write, once per id in
   * res.data.tag_ids for a bulk write. */
  onTagStateChange?: (change: ImageTagChange) => void;
  /* The tag whose contents the operator came from. null/absent renders the
   * panel exactly as the Labeling page has always rendered it: no pinned row,
   * every tag inside its family group. */
  subjectTagId?: number | null;
}) {
  const qc = useQueryClient();
  const key = newDedupImageTagsKey(imageId);
  const q = useQuery({ queryKey: key, queryFn: () => listNewDedupImageTags(imageId) });
  const rows = useMemo(() => q.data?.data ?? [], [q.data]);
  const grouped = useMemo(() => {
    const groups = new Map<string, typeof rows>();
    for (const r of rows) {
      // The subject renders ONCE, pinned at the top — never a second time
      // inside its family, which would be two controls over one cell.
      if (subjectTagId != null && r.id === subjectTagId) continue;
      const family = r.family ?? '—';
      groups.set(family, [...(groups.get(family) ?? []), r]);
    }
    return [...groups.entries()];
  }, [rows, subjectTagId]);
  const subjectRow = useMemo(
    () => (subjectTagId == null ? null : (rows.find((r) => r.id === subjectTagId) ?? null)),
    [rows, subjectTagId],
  );
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());
  const toggleSelect = (tagId: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(tagId)) next.delete(tagId);
      else next.add(tagId);
      return next;
    });
  // "Select all" targets untouched tags — the actual use case (close out
  // everything an operator hasn't looked at yet on this image) — without
  // silently overwriting tags already decided one at a time. Any row can
  // still be checked or unchecked by hand regardless. The subject is out: it
  // is by construction already decided, and closing out "the rest of the
  // image" is not a decision about the tag being read.
  const untouchedIds = useMemo(
    () =>
      rows
        .filter((r) => r.state === 'untouched' && r.id !== subjectTagId)
        .map((r) => r.id),
    [rows, subjectTagId],
  );
  const allUntouchedSelected =
    untouchedIds.length > 0 && untouchedIds.every((id) => selected.has(id));

  const setMut = useMutation({
    mutationFn: (vars: {
      tagId: number;
      label: string;
      state: TagState;
      excludedReason: TagExcludedReason | null;
    }) => setNewDedupTagAnnotation(vars.tagId, imageId, vars.state, vars.excludedReason),
    onSuccess: (res, vars) => {
      qc.setQueryData<{ data: typeof rows }>(key, (old) =>
        old
          ? {
              ...old,
              data: old.data.map((r) =>
                r.id === vars.tagId
                  ? {
                      ...r,
                      state: res.data.state,
                      source: res.data.source,
                      excluded_reason: res.data.excluded_reason,
                    }
                  : r,
              ),
            }
          : old,
      );
      onTagStateChange?.({
        imageId,
        tagId: vars.tagId,
        label: vars.label,
        state: res.data.state,
        excludedReason: res.data.excluded_reason,
      });
      // No longer untouched — drop it from the batch selection so a later
      // "Set selected" can't silently re-decide a tile already handled
      // one at a time.
      setSelected((prev) => {
        if (!prev.has(vars.tagId)) return prev;
        const next = new Set(prev);
        next.delete(vars.tagId);
        return next;
      });
      qc.invalidateQueries({ queryKey: NEW_DEDUP_OVERVIEW_KEY });
      qc.invalidateQueries({ queryKey: NEW_DEDUP_TAG_IMAGES_KEY });
      // Prefix: this panel decides tags OTHER than the one whose queue is on
      // screen, and each of those readouts has an open count that just moved.
      qc.invalidateQueries({ queryKey: NEW_DEDUP_CANDIDATES_KEY });
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  const bulkSetMut = useMutation({
    mutationFn: (vars: { state: TagState; excludedReason: TagExcludedReason | null }) =>
      bulkSetNewDedupImageTags(imageId, [...selected], vars.state, vars.excludedReason),
    onSuccess: (res) => {
      const changedIds = new Set(res.data.tag_ids);
      qc.setQueryData<{ data: typeof rows }>(key, (old) =>
        old
          ? {
              ...old,
              data: old.data.map((r) =>
                changedIds.has(r.id)
                  ? {
                      ...r,
                      state: res.data.state,
                      source: 'human' as const,
                      excluded_reason: res.data.excluded_reason,
                    }
                  : r,
              ),
            }
          : old,
      );
      for (const r of rows) {
        if (changedIds.has(r.id))
          onTagStateChange?.({
            imageId,
            tagId: r.id,
            label: r.label,
            state: res.data.state,
            excludedReason: res.data.excluded_reason,
          });
      }
      setSelected(new Set());
      pushToast('ok', `Set ${res.data.updated} to ${res.data.state}.`);
      qc.invalidateQueries({ queryKey: NEW_DEDUP_OVERVIEW_KEY });
      qc.invalidateQueries({ queryKey: NEW_DEDUP_TAG_IMAGES_KEY });
      // Prefix: this panel decides tags OTHER than the one whose queue is on
      // screen, and each of those readouts has an open count that just moved.
      qc.invalidateQueries({ queryKey: NEW_DEDUP_CANDIDATES_KEY });
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-[var(--color-ink)]/40 px-4 pt-[10vh]"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="flex max-h-[78vh] w-full max-w-lg flex-col rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)] p-5 shadow-lg"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="All tags on this image"
      >
        <div className="flex items-center justify-between">
          <h2 className="text-lg leading-tight" style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}>
            Image {imageId} — all tags
          </h2>
          <button type="button" onClick={onClose} aria-label="Close" className="text-[var(--color-ink-3)] hover:text-[var(--color-ink)]">
            ✕
          </button>
        </div>

        {q.isLoading && <p className="mt-4 text-sm text-[var(--color-ink-3)]">Loading…</p>}
        {q.error && <ErrorBanner message={(q.error as Error).message} />}

        {subjectTagId != null && rows.length > 0 && (
          <SubjectTagBlock
            row={subjectRow}
            pending={setMut.isPending && setMut.variables?.tagId === subjectTagId}
            onSet={(state, excludedReason) =>
              subjectRow &&
              setMut.mutate({
                tagId: subjectRow.id,
                label: subjectRow.label,
                state,
                excludedReason,
              })
            }
          />
        )}

        {rows.length > 0 && (
          <div className="mt-3 flex items-center gap-3 flex-wrap">
            <button
              type="button"
              onClick={() =>
                setSelected(allUntouchedSelected ? new Set() : new Set(untouchedIds))
              }
              disabled={untouchedIds.length === 0}
              className="px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] disabled:opacity-40"
            >
              {allUntouchedSelected ? 'Deselect all' : 'Select all untouched'}
            </button>
            <span className="text-xs text-[var(--color-ink-3)]">{selected.size} selected</span>
            {BATCH_ACTIONS.map((a) => (
              <button
                key={a.key}
                type="button"
                disabled={selected.size === 0 || bulkSetMut.isPending}
                onClick={() => bulkSetMut.mutate({ state: a.state, excludedReason: a.reason })}
                title={a.reason ? EXCLUDED_REASON_META[a.reason].title : undefined}
                className={[
                  'px-2.5 py-1 text-xs rounded-[var(--radius-xs)] disabled:opacity-40',
                  STATE_META[a.state].activeClass,
                  a.reason === 'pruned' ? 'opacity-80' : '',
                ].join(' ')}
              >
                Set selected: {a.label}
              </button>
            ))}
          </div>
        )}

        <div className="mt-3 flex-1 space-y-4 overflow-y-auto">
          {grouped.map(([family, tags]) => (
            <div key={family}>
              <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] mb-1.5">
                {family}
              </p>
              <div className="space-y-1">
                {tags.map((t) => (
                  <div key={t.id} className="flex items-center gap-2 py-0.5">
                    <input
                      type="checkbox"
                      checked={selected.has(t.id)}
                      onChange={() => toggleSelect(t.id)}
                      className="h-3.5 w-3.5 shrink-0"
                      aria-label={`Select ${t.label} for batch action`}
                    />
                    <span className="min-w-0 flex-1 truncate font-mono text-sm text-[var(--color-ink-2)]">
                      {t.label}
                    </span>
                    <TriStateControl
                      state={t.state}
                      onChange={(state, excludedReason) =>
                        setMut.mutate({
                          tagId: t.id,
                          label: t.label,
                          state,
                          excludedReason: excludedReason ?? null,
                        })
                      }
                      disabled={setMut.isPending && setMut.variables?.tagId === t.id}
                      excludedReason={t.excluded_reason}
                      onChangeReason={(reason) =>
                        setMut.mutate({
                          tagId: t.id,
                          label: t.label,
                          state: 'excluded',
                          excludedReason: reason,
                        })
                      }
                      source={t.source}
                    />
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* The tag the operator came FROM, pinned above everything else. Inset ground +
 * one rule border, the same "this is pinned context, not another card" treatment
 * the definition workbench's read-only version banner already uses. */
function SubjectTagBlock({
  row,
  pending,
  onSet,
}: {
  /* null when the subject tag is not in the image's active-tag list — an
   * INACTIVE tag. Says so rather than silently rendering nothing, because a
   * missing block would read as "this image is not on this tag". */
  row: NewDedupImageTag | null;
  pending: boolean;
  onSet: (state: TagState, excludedReason: TagExcludedReason | null) => void;
}) {
  const pressed = row ? pressedActionKey(row) : null;
  const manufactured = isManufactured(row?.source);

  return (
    <div className="mt-3 rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-inset)] p-2.5">
      <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        Viewing
      </p>
      {row == null ? (
        <p className="mt-1 text-[0.7rem] text-[var(--color-ink-3)]">
          This tag is not in the active list, so its state cannot be set here.
        </p>
      ) : (
        <>
          <p className="mt-0.5 truncate font-mono text-sm text-[var(--color-ink)]" title={row.label}>
            {row.label}
          </p>
          <div
            role="group"
            /* Deliberately NOT "Tag state": that name belongs to the three-glyph
             * control, and both pages' tests count those groups. This is a
             * different control over a different question. */
            aria-label="This tag's state"
            className="mt-1.5 flex flex-wrap items-center gap-1"
          >
            {SUBJECT_ACTIONS.map((a) => {
              const active = pressed === a.key;
              // A 442-manufactured positive IS the state the cell is in, but
              // nobody decided it — drawn as the default it really is.
              const fiction = active && manufactured;
              return (
                <button
                  key={a.key}
                  type="button"
                  aria-pressed={active}
                  disabled={pending}
                  onClick={() => onSet(a.state, a.reason)}
                  title={
                    fiction
                      ? `${a.title} (manufactured by migration 442's backfill — not a decision anybody made)`
                      : a.title
                  }
                  className={[
                    'px-2 py-1 text-xs rounded-[var(--radius-xs)] border transition-colors disabled:opacity-40',
                    fiction
                      ? 'border-dashed border-[var(--color-ink-3)] text-[var(--color-ink-3)]'
                      : active
                        ? [
                            STATE_META[a.state].activeClass,
                            a.reason === 'pruned' ? 'opacity-80' : '',
                          ].join(' ')
                        : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
                  ].join(' ')}
                >
                  {a.label}
                </button>
              );
            })}
          </div>
          {/* Always visible, never a tooltip: the consequence has to be readable
            * BEFORE the click, and this is the one place where picking the
            * cheap wrong answer poisons a training head. */}
          <p className="mt-1.5 text-[0.7rem] leading-snug text-[var(--color-ink-4)]">
            “Not this tag” is a real negative the classifier learns from. “Belongs elsewhere” is
            for an image that DOES show this tag's subject but fits another tag better —
            excluded, never negative, so this tag's head is not poisoned. Saved as you click.
          </p>
        </>
      )}
    </div>
  );
}
