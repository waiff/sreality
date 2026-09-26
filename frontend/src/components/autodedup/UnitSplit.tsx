/* AUTODEDUP · the unit split, shared by every surface that rules on a set of adverts.
 *
 * A GROUP IS NOT ALWAYS ONE ANSWER. The engine proposes a set, and the operator
 * regularly finds two of its adverts are one flat and the other two are not.
 * Whole-set buttons cannot say that, so every member carries a UNIT LETTER:
 * members sharing a letter are one property, members in different letters are
 * different properties — and every pair across letters becomes a permanent
 * must-not-link.
 *
 * LETTERS ONLY, SINCE D39. The split used to ask, per unit pair, WHICH kind of
 * different — same building, same development project, unrelated. The engine
 * consumes all three identically (one permanent must-not-link, one negative
 * calibration label), so the question bought no decision and cost a select per
 * unit pair. The client names no relation at all now; the server defaults a
 * missing one to `different` and still obeys an older client that sends one.
 *
 * THE STORED RULING IS READ BACK, NOT REMEMBERED. A split lives in the store as
 * pair verdicts, and `deriveSplit` rebuilds the assignment from them before the
 * operator can act on it — including from the finer values a ruling taken
 * before D39 carries, which are negatives like any other. Page state that a
 * reload throws away would show "A" over every member of a group that was
 * partitioned last week — and the next save would re-derive all pairs as one
 * unit and retract the permanent must-not-links the first save wrote.
 *
 * TWO CALLERS, ONE MACHINE. The proposed-cluster card sends `cluster_key`; the
 * candidate card sends `candidate_key` and no reason chips (there is no cluster
 * row to carry them — §9). Everything else — the letters, the summary line, the
 * 409 — is identical, and lives here once.
 */

import type {
  AutodedupCandidateSplitInput,
  AutodedupSplitInput,
  AutodedupSplitResult,
  AutodedupSplitUnit,
  AutodedupVerdictRow,
  AutodedupVerdictValue,
} from '@/lib/api';
import ErrorBanner from '@/components/ErrorBanner';
import VerdictNotes, {
  EMPTY_ANNOTATION,
  annotationInput,
  type VerdictAnnotation,
} from '@/components/autodedup/VerdictNotes';
import { NEGATIVE_VERDICTS } from '@/components/autodedup/VerdictButtons';

/* A unit is a LETTER, not a free-text label: the operator is partitioning the
 * members of one small group, and a typed name would only add a way to spell the
 * same unit two ways. The map is keyed by `listings.id` and lives at page level
 * so the card and the dialog edit ONE assignment — a dialog opened over a card
 * that already separated two adverts must not start from a blank slate. */
export type UnitMap = Record<number, string>;

export const UNIT_LETTERS: readonly string[] = Array.from({ length: 26 }, (_, i) =>
  String.fromCharCode(65 + i),
);

export interface SplitState {
  units: UnitMap;
}

export const EMPTY_SPLIT: SplitState = { units: {} };

/* An unassigned member is in unit A — the whole group is one property until the
 * operator says otherwise, which is exactly what the engine proposed. (The
 * candidate card seeds its own map instead: the engine proposed NOTHING there,
 * so its adverts start on separate letters.) */
export const unitOf = (units: UnitMap, listingId: number): string => units[listingId] ?? 'A';

export function distinctUnits(
  members: ReadonlyArray<{ listing_id: number }>,
  units: UnitMap,
): string[] {
  return Array.from(new Set(members.map((m) => unitOf(units, m.listing_id)))).sort();
}

/* `A: 101,202 · B: 303` — the assignment in one line, so the card says what it is
 * about to store rather than leaving it to be read off four selects. */
export function splitSummary(
  members: ReadonlyArray<{ listing_id: number }>,
  units: UnitMap,
): string {
  const byUnit = new Map<string, number[]>();
  for (const m of members) {
    const unit = unitOf(units, m.listing_id);
    byUnit.set(unit, [...(byUnit.get(unit) ?? []), m.listing_id]);
  }
  return [...byUnit.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([unit, ids]) => `${unit}: ${ids.join(',')}`)
    .join(' · ');
}

/* The same line, read off a SUBMITTED assignment rather than off the live
 * selects. The receipt has to say what was stored, and live state stops being
 * that the moment the operator touches a select after saving. */
export function unitsSummary(units: ReadonlyArray<AutodedupSplitUnit>): string {
  return splitSummary(
    units.map((u) => ({ listing_id: u.listing_id })),
    Object.fromEntries(units.map((u) => [u.listing_id, u.unit])),
  );
}

/* THE STORED RULING, READ BACK. A split lives in the store as pair verdicts —
 * members ruled `same` are one unit, a NEGATIVE pair verdict separates two —
 * so the assignment is DERIVED from them rather than kept in page state that a
 * reload throws away. Without this the card shows "A" over every member of a
 * group it knows was split, and the next save quietly retracts the permanent
 * must-not-links the first one wrote.
 *
 * Null means nobody has ruled on any pair of this group: there is nothing stored
 * to show, which is a different statement from "everything is one unit". */
export function deriveSplit(
  members: ReadonlyArray<{ listing_id: number }>,
  verdicts: ReadonlyArray<AutodedupVerdictRow>,
): SplitState | null {
  const inside = new Set(members.map((m) => m.listing_id));
  /* The server sends newest first, so the FIRST row for a pair is its live word
   * — taken BEFORE anything is filtered: a withdrawal is a newer `unsure` row
   * (migration 574), and skipping it first would read the older `same` it
   * withdrew as still standing. */
  const newest = new Map<string, AutodedupVerdictValue>();
  for (const v of verdicts) {
    if (v.kind && v.kind !== 'pair') continue;
    const lo = v.listing_lo;
    const hi = v.listing_hi;
    if (lo == null || hi == null || !inside.has(lo) || !inside.has(hi)) continue;
    const key = `${lo}:${hi}`;
    if (!newest.has(key)) newest.set(key, v.verdict);
  }
  /* `unsure` says nothing about the partition; the two finer values a ruling
   * taken before D39 carries separate two units exactly as `different` does. */
  const latest = new Map(
    [...newest].filter(([, verdict]) => verdict === 'same' || NEGATIVE_VERDICTS.includes(verdict)),
  );
  if (latest.size === 0) return null;

  /* Members joined by a `same` verdict are one unit — the transitive closure, so
   * a chain of pair rulings lands in one letter rather than three. */
  const parent = new Map<number, number>();
  const find = (x: number): number => {
    const up = parent.get(x);
    if (up == null || up === x) return x;
    const root = find(up);
    parent.set(x, root);
    return root;
  };
  const union = (a: number, b: number) => {
    const [ra, rb] = [find(a), find(b)];
    if (ra !== rb) parent.set(rb, ra);
  };
  for (const m of members) parent.set(m.listing_id, m.listing_id);
  for (const [key, verdict] of latest) {
    if (verdict !== 'same') continue;
    const [lo, hi] = key.split(':').map(Number);
    union(lo, hi);
  }

  /* Letters follow the member order, so the first advert on the card is A. */
  const letterOf = new Map<number, string>();
  const units: UnitMap = {};
  for (const m of members) {
    const root = find(m.listing_id);
    if (!letterOf.has(root)) letterOf.set(root, UNIT_LETTERS[letterOf.size] ?? 'A');
    units[m.listing_id] = letterOf.get(root)!;
  }
  return { units };
}

/* The half of the body both surfaces send: EVERY member travels, including the
 * ones a card counted rather than showed (the server requires the assignment to
 * name the whole set, and a card that sent only its visible members would be
 * refused — rightly). NO relation travels: two letters mean two properties, and
 * the server's default says the same thing (D39). */
export function splitBody(
  members: ReadonlyArray<{ listing_id: number }>,
  state: SplitState,
  confirmRetract = false,
): {
  units: AutodedupSplitUnit[];
  confirm_retract?: true;
} {
  return {
    units: members.map((m) => ({
      listing_id: m.listing_id,
      unit: unitOf(state.units, m.listing_id),
    })),
    ...(confirmRetract ? { confirm_retract: true as const } : {}),
  };
}

export function splitInput(
  clusterKey: number,
  generation: string,
  members: ReadonlyArray<{ listing_id: number }>,
  state: SplitState,
  confirmRetract = false,
  annotation: VerdictAnnotation = EMPTY_ANNOTATION,
): AutodedupSplitInput {
  return {
    cluster_key: clusterKey,
    generation,
    ...splitBody(members, state, confirmRetract),
    /* ONE set for the whole split: it is one ruling, and the server stamps it on
     * every pair row and on the cluster row. Empty is ordinary — the chips and
     * the note are optional. */
    ...annotationInput(annotation),
  };
}

/* The candidate card's body. The NOTE travels and the reason chips do not: a
 * split stamps its chips on the cluster row, and a candidate group has none —
 * the server answers 400 rather than dropping them, so this must not send them. */
export function candidateSplitInput(
  candidateKey: string,
  generation: string,
  members: ReadonlyArray<{ listing_id: number }>,
  state: SplitState,
  confirmRetract = false,
  annotation: VerdictAnnotation = EMPTY_ANNOTATION,
): AutodedupCandidateSplitInput {
  return {
    candidate_key: candidateKey,
    generation,
    ...splitBody(members, state, confirmRetract),
    note: annotationInput(annotation).note,
  };
}

export interface SplitError {
  message: string;
  /* The server refused because the split takes back an earlier ruling (409). */
  needsConfirm: boolean;
}

export interface SplitReceipt {
  input: { units: AutodedupSplitUnit[] };
  result: AutodedupSplitResult;
}

/* What the CLUSTER row will say: one letter is one property, two letters are
 * two, and the page has no finer word for the second (D39) — which is what the
 * server is about to store, so the optimistic badge agrees with the row that
 * lands. */
export function clusterVerdictOf(input: {
  units: ReadonlyArray<AutodedupSplitUnit>;
}): AutodedupVerdictValue {
  return new Set(input.units.map((u) => u.unit)).size < 2 ? 'same' : 'different';
}

/* What a card or a dialog needs to edit ONE assignment. Bundled rather than
 * passed as seven props, because both surfaces take exactly the same set and a
 * split edited in the dialog has to be the split the card is holding. */
export interface SplitControls {
  state: SplitState;
  setUnit: (listingId: number, unit: string) => void;
  save: (members: ReadonlyArray<{ listing_id: number }>, confirmRetract?: boolean) => void;
  pending: boolean;
  /* WHY this split — the chips and the note, sent with the save, and OPTIONAL:
   * nothing blocks a save that carries neither. NOT hydrated from the stored
   * cluster verdict the way a plain verdict's is: the server writes the
   * ASSIGNMENT into that note (`A: 11,12 | B: 13`), with the operator's own
   * words merely in front of it, so reading it back into the input and
   * re-posting would store the summary twice. */
  annotation: VerdictAnnotation;
  setAnnotation: (next: VerdictAnnotation) => void;
  error?: SplitError;
  receipt?: SplitReceipt;
  /* The ruling that is STORED, derived from the group's pair verdicts. Present
   * means this group already carries a split — which is what keeps the row on
   * screen when the operator merges every member back into one unit. */
  stored: SplitState | null;
}

/* One member's unit. Letters up to the member count: a group of three cannot
 * hold four units, and offering 26 where 3 are possible is a control that mostly
 * misleads. */
export function UnitSelect({
  listingId,
  units,
  count,
  onChange,
  /* What the control is ABOUT, when it is not one advert: the candidate card
   * gives one select to a whole locked group, and "Jednotka #11" over four
   * adverts would name the wrong thing. */
  label,
  disabled = false,
}: {
  listingId: number;
  units: UnitMap;
  count: number;
  onChange: (unit: string) => void;
  label?: string;
  disabled?: boolean;
}) {
  const letters = UNIT_LETTERS.slice(0, Math.min(Math.max(count, 2), UNIT_LETTERS.length));
  return (
    <label className="flex items-center gap-1.5 text-[0.62rem] text-[var(--color-ink-3)]">
      <span>{label ?? `Jednotka #${listingId}`}</span>
      <select
        className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-1.5 py-0.5 text-[0.68rem] text-[var(--color-ink)] disabled:opacity-60"
        value={unitOf(units, listingId)}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        {letters.map((letter) => (
          <option key={letter} value={letter}>
            {letter}
          </option>
        ))}
      </select>
    </label>
  );
}

/* THE SPLIT. It appears once the operator has actually separated something — a
 * split row over a group nobody has split is a statement with no subject — AND
 * it stays once the group carries a stored split, however the operator then
 * assigns the letters. That second half is not a detail: putting every member
 * back into one unit is the UNDO, the one assignment the server implements as
 * "every pair same, every veto retracted", and a row that vanished at one unit
 * made it unreachable from this page. The whole-group "Stejné" is not that
 * undo either — it writes `same` on the cluster and leaves the pair vetoes
 * standing, a contradiction visible on no surface at all.
 *
 * Every pair across two letters becomes a permanent must-not-link, so the row
 * says so in words before the click rather than in a toast after it. */
export function SplitRow({
  members,
  split,
  generation,
  notesOpen = false,
  /* The candidate card always shows this row (its whole purpose is the split)
   * and it saves under its own words — "Uložit rozhodnutí" rather than the
   * proposed group's "Save split". */
  alwaysOpen = false,
  saveLabel,
  mergeBackLabel,
  /* The candidate split writes no cluster row, so it carries no reason chips. */
  showReasons = true,
}: {
  members: ReadonlyArray<{ listing_id: number }>;
  split: SplitControls;
  generation: string;
  /* Open where one group is the whole screen (the dialog); collapsed on a queue
   * card, which is a scroll. */
  notesOpen?: boolean;
  alwaysOpen?: boolean;
  saveLabel?: string;
  mergeBackLabel?: string;
  showReasons?: boolean;
}) {
  const units = distinctUnits(members, split.state.units);
  const one = units.length < 2;
  if (one && !split.stored && !split.receipt && !alwaysOpen) return null;
  const confirm = split.error?.needsConfirm ?? false;
  return (
    <div className="rounded-[var(--radius-sm)] border border-dashed border-[var(--color-rule-strong)] bg-[var(--color-paper)] px-3 py-2 space-y-2">
      <p className="text-[0.7rem] text-[var(--color-ink-2)]">
        {one ? (
          <>
            Sloučit zpět: všech {members.length} inzerátů jako jedna jednotka —{' '}
            <span className="font-mono">{splitSummary(members, split.state.units)}</span>.
            Uložením se zruší dřívější zákazy spojení mezi nimi.
          </>
        ) : (
          <>
            Rozdělit na {units.length} jednotky —{' '}
            <span className="font-mono">{splitSummary(members, split.state.units)}</span>. Stejné
            písmeno znamená <strong>stejnou jednotku</strong>, různá písmena{' '}
            <strong>různé jednotky</strong>: každá dvojice napříč písmeny se uloží jako „různé" a
            dostane trvalý zákaz spojení.
          </>
        )}
      </p>
      {split.stored && (
        /* WHAT IS STORED, not what is on the selects. Without it a reload shows a
         * blank assignment over a group that was ruled on last week. */
        <p className="text-[0.66rem] text-[var(--color-ink-3)]">
          Uloženo dříve:{' '}
          <span className="font-mono">{splitSummary(members, split.stored.units)}</span>
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          /* Not disabled while in flight — the review-queue rule: disabling the
           * button that was just clicked drops focus onto <body>. */
          aria-busy={split.pending}
          onClick={() => split.save(members, confirm)}
          className={`rounded-[var(--radius-sm)] border px-3 py-1.5 text-[0.72rem] hover:bg-[var(--color-paper-3)] ${
            confirm
              ? 'border-[var(--color-brick)] bg-[var(--color-paper-2)] text-[var(--color-brick)]'
              : 'border-[var(--color-rule-strong)] bg-[var(--color-paper-2)] text-[var(--color-ink)]'
          } ${split.pending ? 'opacity-60' : ''}`}
        >
          {split.pending
            ? 'Ukládám…'
            : confirm
              ? 'Přepsat a uložit'
              : one
                ? (mergeBackLabel ?? 'Sloučit zpět')
                : (saveLabel ?? 'Save split')}
        </button>
        <span className="text-[0.62rem] text-[var(--color-ink-4)]">generace {generation}</span>
      </div>
      {/* The split's own reasons — one set for the whole ruling, and optional
        * like every annotation on this program's surfaces. There is no "Uložit
        * poznámku" here: the split IS the save button above, and a second one
        * would write a second, different ruling. The toggle NAMES its
        * destination: a card can show this picker and the cluster verdict's at
        * once, and two identical labels over two different drafts silently drop
        * whichever set the operator did not then save. */}
      <VerdictNotes
        defaultOpen={notesOpen}
        value={split.annotation}
        onChange={split.setAnnotation}
        pending={split.pending}
        label={showReasons ? 'důvod rozdělení' : 'poznámka k rozhodnutí'}
        showReasons={showReasons}
      />
      {split.receipt && (
        <p className="text-[0.68rem] text-[var(--color-ink-2)]">
          Uloženo: {split.receipt.result.n_pairs_same} dvojic jako stejná jednotka,{' '}
          {split.receipt.result.n_pairs_negative} jako různé ·{' '}
          <span className="font-mono">{unitsSummary(split.receipt.input.units)}</span>
        </p>
      )}
      {split.error && <ErrorBanner message={split.error.message} />}
    </div>
  );
}
