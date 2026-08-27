import { useEffect, useMemo, useState } from 'react';
import type { NewDedupTag, TagDefinitionStatus } from '@/lib/api';
import { FAMILY_FALLBACK, tagFamily, tagShortLabel } from '@/lib/tagFamily';

/* The left column: every tag, grouped by family, with the one number that says
 * whether its definition is written (`v3`) or not (`—`). Presentational — the
 * page owns selection, the rename write and every query.
 *
 * Rename is offered on the SELECTED row only. The workflow it serves is "I am
 * reading this tag's contents and its name is wrong", so the row is already
 * picked; 51 always-on rename buttons would be noise, and a hover-reveal is
 * undiscoverable and dead on touch. Delete follows the same rule one step
 * further along the same workflow, drawn quieter than rename and opening a
 * modal — the numbers it has to state do not fit in a 22rem row. */
interface Props {
  tags: ReadonlyArray<NewDedupTag>;
  status: ReadonlyMap<number, TagDefinitionStatus>;
  selectedId: number | null;
  onSelect: (id: number) => void;
  loading: boolean;
  /* Resolves true when the server accepted the new label. The row leaves edit
   * mode only then — a rejected rename keeps the typing and the focus. */
  onRename: (tagId: number, label: string) => Promise<boolean>;
  renamePending: boolean;
  /* The server's message for the rename in flight (a duplicate label, a cap
   * breach), rendered beside the field rather than as a toast six seconds
   * away from it. */
  renameError: string | null;
  /* Must be referentially stable — see the selection effect below. */
  onRenameErrorClear: () => void;
  /* Offered on the SELECTED row only, beside rename — the same rule, one step
   * further along the same workflow. Opens the page's confirm; this component
   * never writes. */
  onRequestDelete: (tagId: number) => void;
}

/* Mirrors toolkit.tag_annotations.LABEL_MAX_CHARS / migration 442's CHECK —
 * the server's cap enforced at the input, the same rule DefinitionEditor
 * already follows for `means`. */
const LABEL_MAX_CHARS = 100;

export default function TagDefinitionList({
  tags,
  status,
  selectedId,
  onSelect,
  loading,
  onRename,
  renamePending,
  renameError,
  onRenameErrorClear,
  onRequestDelete,
}: Props) {
  const grouped = useMemo(() => {
    const byFamily = new Map<string, NewDedupTag[]>();
    for (const t of tags) {
      const family = tagFamily(t);
      const bucket = byFamily.get(family);
      if (bucket) bucket.push(t);
      else byFamily.set(family, [t]);
    }
    return [...byFamily.entries()]
      .sort((a, b) =>
        a[0] === FAMILY_FALLBACK
          ? 1
          : b[0] === FAMILY_FALLBACK
            ? -1
            : a[0].localeCompare(b[0], 'cs'),
      )
      .map(
        ([family, rows]) =>
          [family, [...rows].sort((a, b) => a.label.localeCompare(b.label, 'cs'))] as const,
      );
  }, [tags]);

  const defined = tags.reduce((n, t) => (status.has(t.id) ? n + 1 : n), 0);

  /* Which row is in edit mode, plus the text being typed. Seeded from the tag's
   * label at the moment the button is clicked — never from a useState
   * initializer, which would go stale after an external rename. */
  const [editingId, setEditingId] = useState<number | null>(null);
  const [draft, setDraft] = useState('');

  const stopEditing = () => {
    setEditingId(null);
    onRenameErrorClear();
  };

  /* Clicking a different tag only HIDES the editor — the row stops being
   * selected. Left standing, `editingId` and the abandoned text come back the
   * next time that tag is picked: the <input autoFocus> re-mounts, steals the
   * focus from the click, and one Enter commits a label the operator walked
   * away from. That is the same accident the deliberate no-commit-on-blur
   * guards against, arriving by the other door. The server's error rides along
   * for the same reason — a dead message under a field nobody opened.
   *
   * `onRenameErrorClear` must be referentially stable (the page memoizes it),
   * or this fires on every render and no rename error would ever be readable. */
  useEffect(() => {
    setEditingId(null);
    onRenameErrorClear();
  }, [selectedId, onRenameErrorClear]);

  return (
    <aside className="lg:sticky lg:top-4 lg:max-h-[calc(100dvh-6rem)] lg:overflow-y-auto">
      <p className="text-[0.7rem] font-mono tabular-nums text-[var(--color-ink-3)]">
        {tags.length} tags · {defined} defined
      </p>

      {loading && <p className="mt-3 text-sm text-[var(--color-ink-3)]">Loading…</p>}
      {!loading && tags.length === 0 && (
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">
          No tags yet — the taxonomy is managed from the Labeling page.
        </p>
      )}

      <div className="mt-3 space-y-4">
        {grouped.map(([family, rows]) => (
          <div key={family}>
            <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] mb-1.5">
              {family}
            </p>
            <div className="space-y-0.5">
              {rows.map((t) => {
                const def = status.get(t.id);
                const selected = selectedId === t.id;
                const editing = selected && editingId === t.id;
                const trimmed = draft.trim();
                const canCommit = trimmed !== '' && trimmed !== t.label && !renamePending;
                const commit = () => {
                  if (!canCommit) return;
                  void onRename(t.id, trimmed).then((ok) => {
                    if (ok) setEditingId(null);
                  });
                };
                return (
                  /* A wrapper div, because the row's own select control is a
                     <button> and a button cannot nest another one. Every child
                     of the select button is unchanged, so `within(row)` queries
                     on the label / count / version chips still resolve. */
                  <div
                    key={t.id}
                    className={[
                      'w-full flex items-baseline gap-1.5 px-1.5 py-1',
                      'rounded-[var(--radius-xs)] border',
                      selected
                        ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)]'
                        : 'border-transparent hover:bg-[var(--color-paper-2)]',
                    ].join(' ')}
                  >
                    {editing ? (
                      <div className="min-w-0 flex-1">
                        <input
                          autoFocus
                          type="text"
                          value={draft}
                          maxLength={LABEL_MAX_CHARS}
                          aria-label="New tag label"
                          onChange={(e) => setDraft(e.target.value)}
                          ref={(el) => {
                            // Cursor at the end ON MOUNT, never select-all:
                            // select-all makes wiping the family prefix a
                            // single keystroke. Guarded so clicking back into
                            // the field doesn't yank the caret.
                            if (!el || el.dataset.caretPlaced === '1') return;
                            el.dataset.caretPlaced = '1';
                            const end = el.value.length;
                            el.setSelectionRange(end, end);
                          }}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') {
                              e.preventDefault();
                              commit();
                            }
                            // Blur deliberately does NOT commit — a stray click
                            // is how a tag gets renamed by accident.
                            if (e.key === 'Escape') {
                              e.preventDefault();
                              stopEditing();
                            }
                          }}
                          className="w-full px-1.5 py-0.5 text-sm font-mono rounded-[var(--radius-xs)] border border-[var(--color-rule-strong)] bg-[var(--color-paper-2)]"
                        />
                        <p className="mt-0.5 text-[0.65rem] text-[var(--color-ink-4)]">
                          The part before " - " is the family — changing it moves the tag to
                          another group.
                        </p>
                        {renameError && (
                          <p className="mt-0.5 text-[0.68rem] text-[var(--color-brick)]">
                            {renameError}
                          </p>
                        )}
                        <div className="mt-1 flex items-center gap-2">
                          <button
                            type="button"
                            disabled={!canCommit}
                            onClick={commit}
                            className="text-xs text-[var(--color-copper)] disabled:opacity-40"
                          >
                            Save
                          </button>
                          <button
                            type="button"
                            onClick={stopEditing}
                            className="text-xs text-[var(--color-ink-3)]"
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <button
                          type="button"
                          onClick={() => onSelect(t.id)}
                          aria-current={selected ? 'true' : undefined}
                          className="min-w-0 flex-1 flex items-baseline gap-1.5 text-left"
                        >
                          <span
                            title={t.label}
                            className={[
                              'min-w-0 flex-1 truncate font-mono text-[0.78rem]',
                              selected
                                ? 'text-[var(--color-copper)]'
                                : t.priority
                                  ? 'text-[var(--color-brick)]'
                                  : 'text-[var(--color-ink-2)]',
                            ].join(' ')}
                          >
                            {tagShortLabel(t.label)}
                          </span>

                          {!t.active && (
                            <span className="shrink-0 text-[0.65rem] text-[var(--color-ink-4)]">
                              inactive
                            </span>
                          )}

                          {t.priority && (
                            <span className="shrink-0 px-1 py-px text-[0.6rem] rounded-[var(--radius-xs)] border border-[var(--color-brick)] bg-[var(--color-brick-soft)] text-[var(--color-brick)]">
                              priority
                            </span>
                          )}
                          {t.ready_for_training && (
                            <span
                              title="Ready for training"
                              className="shrink-0 px-1 py-px text-[0.6rem] rounded-[var(--radius-xs)] border border-[var(--color-sage)] bg-[var(--color-sage-soft)] text-[var(--color-sage)]"
                            >
                              ready
                            </span>
                          )}

                          <span className="shrink-0 w-9 text-right font-mono text-[0.7rem] tabular-nums text-[var(--color-ink-4)]">
                            {t.positive_count}
                          </span>

                          {def ? (
                            <span
                              title={def.means}
                              className="shrink-0 w-6 text-right font-mono text-[0.7rem] tabular-nums text-[var(--color-sage)]"
                            >
                              v{def.version}
                            </span>
                          ) : (
                            <span className="shrink-0 w-6 text-right font-mono text-[0.7rem] text-[var(--color-ink-4)]">
                              —
                            </span>
                          )}
                        </button>

                        {selected && (
                          /* aria-label must NOT carry the tag's label: the
                             page's tests find a row by `{name: /kuchyne/}`, and
                             a second matching button would make that ambiguous.
                             Exactly one row is selected at a time, so "this
                             tag" is unambiguous anyway. */
                          <button
                            type="button"
                            aria-label="Rename this tag"
                            onClick={() => {
                              setDraft(t.label);
                              setEditingId(t.id);
                              onRenameErrorClear();
                            }}
                            className="shrink-0 text-xs text-[var(--color-ink-3)] underline decoration-dotted underline-offset-2 hover:text-[var(--color-copper-2)]"
                          >
                            rename
                          </button>
                        )}

                        {selected && (
                          /* Deliberately weaker than rename (ink-4, not ink-3):
                             the destructive act must not be the loudest thing
                             on the row. The confirm carries the weight. */
                          <button
                            type="button"
                            aria-label="Delete this tag"
                            onClick={() => onRequestDelete(t.id)}
                            className="shrink-0 text-xs text-[var(--color-ink-4)] underline decoration-dotted underline-offset-2 hover:text-[var(--color-brick)]"
                          >
                            delete
                          </button>
                        )}
                      </>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </aside>
  );
}
