import { useEffect, useRef } from 'react';
import type {
  NewDedupTag,
  TagDefinitionConfusable,
  TagDefinitionDoesNotCount,
} from '@/lib/api';
import TagPicker from './TagPicker';

/* The whole definition document as the operator is editing it. There are no
 * drafts server-side — one Save is one version — so every keystroke, every
 * picker and every gallery click lands HERE and nowhere else until Save. */
export interface Draft {
  means: string;
  counts: string[];
  does_not_count: TagDefinitionDoesNotCount[];
  confusable_with: TagDefinitionConfusable[];
  leave_out_when: string;
  example_image_ids: number[];
}

export const EMPTY_DRAFT: Draft = {
  means: '',
  counts: [],
  does_not_count: [],
  confusable_with: [],
  leave_out_when: '',
  example_image_ids: [],
};

interface Props {
  draft: Draft;
  onChange: (patch: Partial<Draft>) => void;
  tags: ReadonlyArray<NewDedupTag>;
  subjectTagId: number;
  /* Set by "Add to confusable" on a neighbour row so the operator lands in the
   * one field that click can't fill in for them. */
  focusConfusableIndex: number | null;
  onConfusableFocused: () => void;
}

/* Mirrors toolkit.tag_definitions.MEANS_MAX_CHARS / LINE_MAX_CHARS. The server
 * rejects the whole document over these, and this page's contract is "one
 * sitting, one write" — so a cap the input does not enforce turns a finished
 * sitting into a 422 at the end of it. */
const MEANS_MAX = 500;
const LINE_MAX = 300;

const INPUT =
  'w-full min-w-0 px-2 py-1 text-[0.82rem] rounded-[var(--radius-xs)] border ' +
  'border-[var(--color-rule)] bg-[var(--color-inset)] text-[var(--color-ink)] ' +
  'placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-copper)]';

const REMOVE =
  'shrink-0 px-1.5 py-1 text-[0.75rem] leading-none rounded-[var(--radius-xs)] ' +
  'text-[var(--color-ink-4)] hover:text-[var(--color-brick)]';

const ADD =
  'mt-1.5 px-2 py-1 text-[0.72rem] rounded-[var(--radius-xs)] border ' +
  'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] ' +
  'hover:border-[var(--color-rule-strong)]';

function Field({
  label,
  help,
  children,
}: {
  label: string;
  help: string;
  children: React.ReactNode;
}) {
  return (
    <div className="border-t border-[var(--color-rule-soft)] pt-3 first:border-t-0 first:pt-0">
      <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        {label}
      </p>
      <p className="mb-1.5 text-[0.7rem] text-[var(--color-ink-4)]">{help}</p>
      {children}
    </div>
  );
}

export default function DefinitionEditor({
  draft,
  onChange,
  tags,
  subjectTagId,
  focusConfusableIndex,
  onConfusableFocused,
}: Props) {
  const tellRefs = useRef<Array<HTMLInputElement | null>>([]);

  useEffect(() => {
    if (focusConfusableIndex == null) return;
    tellRefs.current[focusConfusableIndex]?.focus();
    onConfusableFocused();
  }, [focusConfusableIndex, onConfusableFocused]);

  const patchList = <T,>(list: T[], index: number, value: T): T[] =>
    list.map((row, i) => (i === index ? value : row));
  const dropAt = <T,>(list: T[], index: number): T[] => list.filter((_, i) => i !== index);

  const pickedConfusable = draft.confusable_with.map((c) => c.tag_id);

  return (
    <div className="space-y-3">
      <Field label="means" help="one sentence — the meaning a second person must land on too">
        <input
          type="text"
          aria-label="means"
          value={draft.means}
          maxLength={MEANS_MAX}
          placeholder="one sentence: what this tag means"
          onChange={(e) => onChange({ means: e.target.value })}
          className={INPUT}
        />
      </Field>

      <Field label="counts" help="the cases that clearly belong to this tag">
        <div className="space-y-1">
          {draft.counts.map((line, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <input
                type="text"
                aria-label={`counts ${i + 1}`}
                value={line}
                maxLength={LINE_MAX}
                placeholder="lobby of an apartment building"
                onChange={(e) => onChange({ counts: patchList(draft.counts, i, e.target.value) })}
                className={INPUT}
              />
              <button
                type="button"
                aria-label={`Remove counts ${i + 1}`}
                onClick={() => onChange({ counts: dropAt(draft.counts, i) })}
                className={REMOVE}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
        <button
          type="button"
          onClick={() => onChange({ counts: [...draft.counts, ''] })}
          className={ADD}
        >
          + Add
        </button>
      </Field>

      <Field
        label="does not count"
        help="the near-miss cases — and, where there is one, the tag each belongs to instead"
      >
        <div className="space-y-1">
          {draft.does_not_count.map((row, i) => (
            <div key={i}>
              <div className="flex items-center gap-1.5">
                <input
                  type="text"
                  aria-label={`does not count ${i + 1}`}
                  value={row.case}
                  maxLength={LINE_MAX}
                  placeholder="a hallway inside one flat"
                  onChange={(e) =>
                    onChange({
                      does_not_count: patchList(draft.does_not_count, i, {
                        ...row,
                        case: e.target.value,
                      }),
                    })
                  }
                  className={INPUT}
                />
                <span className="shrink-0 text-[0.7rem] text-[var(--color-ink-4)]">goes to</span>
                <TagPicker
                  ariaLabel={`does not count ${i + 1} goes to tag`}
                  value={row.goes_to_tag_id}
                  allowEmpty
                  excludeIds={[subjectTagId]}
                  tags={tags}
                  onChange={(id) =>
                    onChange({
                      does_not_count: patchList(draft.does_not_count, i, {
                        ...row,
                        goes_to_tag_id: id,
                      }),
                    })
                  }
                  className="w-40"
                />
                <button
                  type="button"
                  aria-label={`Remove does not count ${i + 1}`}
                  onClick={() => onChange({ does_not_count: dropAt(draft.does_not_count, i) })}
                  className={REMOVE}
                >
                  ✕
                </button>
              </div>
              {row.case.trim() === '' && row.goes_to_tag_id != null && (
                <p className="mt-0.5 text-[0.68rem] text-[var(--color-brick)]">
                  Needs the case this tag takes instead — a row with no case is not saved.
                </p>
              )}
            </div>
          ))}
        </div>
        <button
          type="button"
          onClick={() =>
            onChange({
              does_not_count: [...draft.does_not_count, { case: '', goes_to_tag_id: null }],
            })
          }
          className={ADD}
        >
          + Add
        </button>
      </Field>

      <Field
        label="confusable with"
        help="the tag you keep mixing this one up with, and the visual tell that separates them — if you cannot write the tell, they are one tag"
      >
        <div className="space-y-1">
          {draft.confusable_with.map((row, i) => (
            <div key={i}>
              <div className="flex items-center gap-1.5">
                <TagPicker
                  ariaLabel={`confusable with ${i + 1} tag`}
                  value={row.tag_id === 0 ? null : row.tag_id}
                  excludeIds={[
                    subjectTagId,
                    ...pickedConfusable.filter((id) => id !== row.tag_id),
                  ]}
                  tags={tags}
                  onChange={(id) =>
                    onChange({
                      confusable_with: patchList(draft.confusable_with, i, {
                        ...row,
                        tag_id: id ?? 0,
                      }),
                    })
                  }
                  className="w-40"
                />
                <input
                  type="text"
                  ref={(el) => {
                    tellRefs.current[i] = el;
                  }}
                  aria-label={`confusable with ${i + 1} tell`}
                  value={row.tell}
                  maxLength={LINE_MAX}
                  placeholder="mailboxes/intercom = shared"
                  onChange={(e) =>
                    onChange({
                      confusable_with: patchList(draft.confusable_with, i, {
                        ...row,
                        tell: e.target.value,
                      }),
                    })
                  }
                  className={INPUT}
                />
                <button
                  type="button"
                  aria-label={`Remove confusable with ${i + 1}`}
                  onClick={() =>
                    onChange({ confusable_with: dropAt(draft.confusable_with, i) })
                  }
                  className={REMOVE}
                >
                  ✕
                </button>
              </div>
              {(row.tag_id === 0 || row.tell.trim() === '') && (
                <p className="mt-0.5 text-[0.68rem] text-[var(--color-brick)]">
                  Needs both a tag and the tell that separates them.
                </p>
              )}
            </div>
          ))}
        </div>
        <button
          type="button"
          onClick={() =>
            onChange({ confusable_with: [...draft.confusable_with, { tag_id: 0, tell: '' }] })
          }
          className={ADD}
        >
          + Add
        </button>
      </Field>

      <Field
        label="leave out when"
        help="when to leave the image OUT of this head rather than decide — one line; line breaks are collapsed on save"
      >
        <textarea
          rows={2}
          aria-label="leave out when"
          value={draft.leave_out_when}
          maxLength={LINE_MAX}
          placeholder="the room is half-demolished and could be read either way"
          onChange={(e) => onChange({ leave_out_when: e.target.value })}
          className={INPUT}
        />
      </Field>
    </div>
  );
}
