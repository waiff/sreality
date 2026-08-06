/* "Modify labels" dialog for the NEW DEDUP Labeling page's Taxonomy v1 vocabulary —
 * add/rename/remove, alphabetically sorted (the bar chart on the page itself is sorted
 * by confirmed count instead, so this is the one place the operator manages the SET
 * rather than reads its progress). Modelled on PresetSaveModal for visual consistency
 * (backdrop, Escape-to-close, click-outside-to-close). */

import { useEffect, useState } from 'react';

import type { NewDedupTaxonomyLabel } from '@/lib/api';
import { TrashIcon } from '@/components/icons';
import Spinner from '@/components/Spinner';

export interface TaxonomyManageModalProps {
  labels: NewDedupTaxonomyLabel[];
  onClose: () => void;
  newLabelText: string;
  onNewLabelTextChange: (v: string) => void;
  onAdd: () => void;
  addPending: boolean;
  onRename: (id: number, oldLabel: string, label: string) => void;
  renamePending: boolean;
  onRemove: (id: number, oldLabel: string) => void;
  removePending: boolean;
}

export default function TaxonomyManageModal({
  labels,
  onClose,
  newLabelText,
  onNewLabelTextChange,
  onAdd,
  addPending,
  onRename,
  renamePending,
  onRemove,
  removePending,
}: TaxonomyManageModalProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const sorted = [...labels].sort((a, b) => a.label.localeCompare(b.label, 'cs'));

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
        aria-label="Modify Taxonomy v1 labels"
      >
        <div className="flex items-center justify-between">
          <div>
            <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
              Taxonomy v1
            </p>
            <h2
              className="mt-1 text-xl leading-tight"
              style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
            >
              Modify labels
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="text-[var(--color-ink-3)] hover:text-[var(--color-ink)]"
          >
            ✕
          </button>
        </div>

        <div className="mt-3 flex items-center gap-2">
          <input
            type="text"
            autoFocus
            value={newLabelText}
            onChange={(e) => onNewLabelTextChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && newLabelText.trim()) onAdd();
            }}
            placeholder="new label, e.g. interier - kuchyne"
            className="min-w-0 flex-1 px-2 py-1 text-sm font-mono rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] focus:outline-none focus:border-[var(--color-copper)]"
          />
          <button
            type="button"
            onClick={onAdd}
            disabled={addPending || !newLabelText.trim()}
            className="shrink-0 px-3 py-1 text-xs rounded-[var(--radius-xs)] bg-[var(--color-copper)] text-[var(--color-paper)] disabled:opacity-50"
          >
            Add label
          </button>
        </div>

        <p className="mt-3 text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
          {sorted.length} label{sorted.length === 1 ? '' : 's'}, A–Z
        </p>

        <div className="mt-1.5 flex-1 space-y-1.5 overflow-y-auto">
          {sorted.length === 0 && (
            <p className="text-sm text-[var(--color-ink-3)]">No labels yet — add the first one above.</p>
          )}
          {sorted.map((l) => (
            <ManageRow
              key={l.id}
              label={l}
              onRename={(next) => onRename(l.id, l.label, next)}
              renamePending={renamePending}
              onRemove={() => onRemove(l.id, l.label)}
              removePending={removePending}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function ManageRow({
  label,
  onRename,
  renamePending,
  onRemove,
  removePending,
}: {
  label: NewDedupTaxonomyLabel;
  onRename: (next: string) => void;
  renamePending: boolean;
  onRemove: () => void;
  removePending: boolean;
}) {
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(label.label);
  const [confirmingRemove, setConfirmingRemove] = useState(false);

  return (
    <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-2.5 py-1.5">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0 flex-1">
          {renaming ? (
            <input
              autoFocus
              type="text"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && draft.trim()) {
                  onRename(draft.trim());
                  setRenaming(false);
                }
                if (e.key === 'Escape') {
                  setDraft(label.label);
                  setRenaming(false);
                }
              }}
              className="w-full px-1.5 py-0.5 text-sm font-mono rounded-[var(--radius-xs)] border border-[var(--color-rule-strong)] bg-[var(--color-paper-2)]"
            />
          ) : (
            <span className="block truncate font-mono text-sm" title={label.label}>
              {label.label}
            </span>
          )}
          <span className="mt-0.5 block text-[0.65rem] font-mono tabular-nums text-[var(--color-ink-4)]">
            {`${label.confirmed_count} confirmed · ${label.pending_count} pending`}
          </span>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {renaming ? (
            <>
              <button
                type="button"
                disabled={renamePending || !draft.trim()}
                onClick={() => {
                  onRename(draft.trim());
                  setRenaming(false);
                }}
                className="text-xs text-[var(--color-copper)] disabled:opacity-40"
              >
                Save
              </button>
              <button
                type="button"
                onClick={() => {
                  setDraft(label.label);
                  setRenaming(false);
                }}
                className="text-xs text-[var(--color-ink-3)]"
              >
                Cancel
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => setRenaming(true)}
              className="text-xs text-[var(--color-ink-3)] underline decoration-dotted underline-offset-2 hover:text-[var(--color-copper-2)]"
            >
              rename
            </button>
          )}
          <button
            type="button"
            onClick={() => setConfirmingRemove(true)}
            aria-label={`Remove ${label.label}`}
            className="text-[var(--color-ink-4)] hover:text-[var(--color-brick)]"
          >
            <TrashIcon className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {confirmingRemove && (
        <div className="mt-2 flex items-center gap-3 border-t border-[var(--color-rule-soft)] pt-2 text-xs text-[var(--color-brick)]">
          <span>
            Remove {label.label}? {label.confirmed_count} training example
            {label.confirmed_count === 1 ? '' : 's'} and {label.pending_count + label.dismissed_count}{' '}
            proposal{label.pending_count + label.dismissed_count === 1 ? '' : 's'} go with it
            (images stay).
          </span>
          <button
            type="button"
            disabled={removePending}
            onClick={() => {
              onRemove();
              setConfirmingRemove(false);
            }}
            className="ml-auto flex shrink-0 items-center gap-1 font-medium text-[var(--color-brick)] disabled:opacity-40"
          >
            {removePending && <Spinner size={10} />}
            Remove
          </button>
          <button
            type="button"
            onClick={() => setConfirmingRemove(false)}
            className="shrink-0 text-[var(--color-ink-3)]"
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}
