import { TAG_STATES, type TagExcludedReason, type TagSource, type TagState } from '@/lib/api';

/* The tag-annotation matrix's one cell control, shared by the Labeling page's
 * two grids and by the all-tags panel that both pages open. Its vocabulary —
 * the three states, the two exclusion reasons, the four batch actions — moves
 * with it, because they are one concept and splitting them costs two imports
 * for nothing. */

export const STATE_META: Record<
  TagState,
  { label: string; icon: string; activeClass: string; hotkey: string }
> = {
  positive: {
    label: 'Positive — this tag applies',
    icon: '✓',
    activeClass: 'bg-[var(--color-sage)] text-[var(--color-paper)] border-[var(--color-sage)]',
    hotkey: '1',
  },
  negative: {
    label: 'Negative — this tag does not apply',
    icon: '–',
    activeClass: 'bg-[var(--color-ink-3)] text-[var(--color-paper)] border-[var(--color-ink-3)]',
    hotkey: '2',
  },
  excluded: {
    label: "Excluded — ambiguous (dropped from this tag's training set)",
    icon: '⊘',
    activeClass: 'bg-[var(--color-copper)] text-[var(--color-paper)] border-[var(--color-copper)]',
    hotkey: '3',
  },
};

/* WHY a cell is excluded. Identical effect on training, opposite meaning for
 * diagnostics — only 'ambiguous' counts toward a tag's ambiguity rate, and
 * 'pruned' is kept out of that rate's DENOMINATOR too, so pruning can never
 * dilute the signal.
 *
 * Colour follows that difference rather than decorating it: ambiguous inherits
 * COPPER, the same hue the ⊘ button already wears, because the reason refines
 * the excluded state rather than introducing a new one; pruned is deliberately
 * colourless, because it is bookkeeping and carries no diagnostic weight. */
export const EXCLUDED_REASON_META: Record<
  TagExcludedReason,
  { label: string; title: string; className: string }
> = {
  ambiguous: {
    label: 'ambiguous',
    title:
      "Nobody could decide. Counts toward this tag's ambiguity rate — a high rate means the DEFINITION needs fixing, not more labeling. Click to mark it pruned instead.",
    className: 'bg-[var(--color-copper-soft)] text-[var(--color-copper)]',
  },
  pruned: {
    label: 'pruned',
    title:
      'Deliberately removed from the training set. Same effect on training as ambiguous, opposite meaning — pruned cells are excluded from the ambiguity rate entirely. Click to mark it ambiguous instead.',
    // Neutral, not a hue: pruning carries no diagnostic weight, and the
    // absence of colour beside a copper "ambiguous" IS the difference. ink-2
    // rather than the quieter ink-3 because 0.6rem type on a 5%-tint ground
    // still has to be readable.
    className: 'bg-[var(--color-rule-soft)] text-[var(--color-ink-2)]',
  },
};

/* A cell manufactured by migration 442's one-hot backfill is NOT a decision
 * anybody made, and on screen it is otherwise indistinguishable from an
 * operator's negative — precisely the confusion the provenance work exists to
 * end. Rendered with the same dashed treatment an untouched cell already uses
 * for "defaulted, not decided", so it needs no new visual vocabulary and no
 * room in the tile. */
export const isManufactured = (source: TagSource | null | undefined) => source === 'backfill_442';

/* Three visually distinct states at a glance (colour + icon), plus a fourth
 * "untouched" rendering of the same three buttons — a dashed outline on the
 * negative slot, since untouched defaults to negative — so an explicit
 * decision is never confused with the unreviewed default. One control, never
 * two widgets for "is it positive" and "is it excluded".
 *
 * Two things ride alongside the three buttons without joining them, because
 * neither is a fourth state: the exclusion-reason chip (why, once the cell IS
 * excluded) and the dashed treatment for a 442-manufactured cell (which state
 * it is in, and that nobody put it there). The chip sits OUTSIDE the
 * `role="group"` deliberately — the group means "which of the three states",
 * the chip means "why excluded". */
export default function TriStateControl({
  state,
  onChange,
  disabled,
  focused,
  excludedReason,
  onChangeReason,
  source,
}: {
  state: TagState | 'untouched';
  /* ⊘ ALONE always means ambiguous — one click, no prompt, no modifier. The
   * fast path is a sweep of positives and negatives; exclusions are the rare
   * case and ambiguity is overwhelmingly the common one among them, so the
   * default is also the safe default. Pruning gets its own visible affordance
   * (the chip below, the batch bar, the 4 key) rather than a hidden one. */
  onChange: (state: TagState, excludedReason?: TagExcludedReason | null) => void;
  disabled?: boolean;
  focused?: boolean;
  excludedReason?: TagExcludedReason | null;
  onChangeReason?: (reason: TagExcludedReason) => void;
  source?: TagSource | null;
}) {
  const manufactured = isManufactured(source);
  /* An excluded row with no reason — a legacy row predating the column, or any
   * non-SPA writer — reads as ambiguous. A deliberate prune always names itself,
   * so an unexplained exclusion is "nobody could decide"; that matches what ⊘
   * means everywhere else on this page AND how the ambiguity rate counts it
   * (tag_annotations._OVERVIEW_SQL), which is the point — the two must not
   * disagree about the same cell. */
  const reason: TagExcludedReason = excludedReason ?? 'ambiguous';
  const reasonMeta = EXCLUDED_REASON_META[reason];
  const other: TagExcludedReason = reason === 'ambiguous' ? 'pruned' : 'ambiguous';

  return (
    <div className="flex items-center gap-1.5 shrink-0">
      <div role="group" aria-label="Tag state" className="flex items-center gap-0.5 shrink-0">
        {TAG_STATES.map((s) => {
          const meta = STATE_META[s];
          const active = state === s;
          const implied = state === 'untouched' && s === 'negative';
          // A manufactured cell IS in this state, so it stays pressed — but it
          // is drawn as the default it really is, not as a decision.
          const fiction = active && manufactured;
          return (
            <button
              key={s}
              type="button"
              aria-label={meta.label}
              aria-pressed={active}
              disabled={disabled}
              onClick={() => onChange(s, s === 'excluded' ? 'ambiguous' : null)}
              title={[
                meta.label,
                implied ? ' (defaulted — not yet reviewed)' : '',
                fiction
                  ? " (manufactured by migration 442's backfill — not a decision anybody made)"
                  : '',
                ` [${meta.hotkey}]`,
              ].join('')}
              className={[
                'flex h-6 w-6 items-center justify-center rounded-[var(--radius-xs)] border text-[0.8rem] leading-none transition-colors disabled:opacity-40',
                focused ? 'ring-2 ring-[var(--color-copper)] ring-offset-1' : '',
                fiction
                  ? 'border-dashed border-[var(--color-ink-3)] text-[var(--color-ink-3)]'
                  : active
                    ? meta.activeClass
                    : implied
                      ? 'border-dashed border-[var(--color-ink-3)] text-[var(--color-ink-3)]'
                      : 'border-[var(--color-rule)] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)]',
              ].join(' ')}
            >
              <span aria-hidden="true">{meta.icon}</span>
            </button>
          );
        })}
      </div>
      {/* Only exists once the cell IS excluded: nothing new on screen on the
        * cold path, no layout shift, and the reason is never a state you set
        * and then cannot see. */}
      {state === 'excluded' && onChangeReason && (
        <button
          type="button"
          disabled={disabled}
          onClick={() => onChangeReason(other)}
          aria-label={`Exclusion reason: ${reasonMeta.label}. Change to ${other}.`}
          title={reasonMeta.title}
          className={[
            'shrink-0 rounded-[var(--radius-xs)] px-1.5 py-0.5 text-[0.6rem] leading-none transition-colors disabled:opacity-40',
            reasonMeta.className,
          ].join(' ')}
        >
          {reasonMeta.label}
        </button>
      )}
    </div>
  );
}

/* The batch bar's buttons. "All of these are fine" stays ONE click — the two
 * exclusion reasons are two one-click buttons, not one button plus a follow-up
 * prompt. A prompt would tax the path the operator uses most on a hard tag,
 * which is exactly what a batch bar exists to avoid. */
export const BATCH_ACTIONS: ReadonlyArray<{
  key: string;
  state: TagState;
  reason: TagExcludedReason | null;
  label: string;
}> = [
  { key: 'positive', state: 'positive', reason: null, label: 'positive' },
  { key: 'negative', state: 'negative', reason: null, label: 'negative' },
  { key: 'excl-amb', state: 'excluded', reason: 'ambiguous', label: 'excluded · ambiguous' },
  { key: 'excl-prn', state: 'excluded', reason: 'pruned', label: 'excluded · pruned' },
];
