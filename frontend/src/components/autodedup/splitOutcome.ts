/* AUTODEDUP · what came of one card's split statement (`POST /properties/{id}/split`,
 * E919), for every page that sends one: the proposals page and the category
 * review. A refusal is an outcome the card shows, never a throw: a statement
 * that would take back the operator's own earlier "různé" (E52) is re-sent with
 * `confirm_retract`, `stale` means the property changed since the page read it,
 * and a written split carries the undo the server issued. */

import {
  splitProperty,
  splitRefusal,
  undoSplit,
  type SplitRefusal,
  type SplitResult,
  type SplitStatement,
  type SplitUndoResult,
} from '@/lib/api';

export type SplitOutcome = {
  propertyId: number;
  statement: SplitStatement;
  /* Each ad's portal, so the outcome names it after the card is gone. */
  sources: Record<number, string>;
} & (
  | { kind: 'ok'; result: SplitResult }
  | { kind: 'undone'; result: SplitUndoResult }
  | { kind: 'reverses'; refusal: SplitRefusal }
  | { kind: 'stale' }
  | { kind: 'error'; message: string }
);

export type SplitOutcomeBase = Pick<SplitOutcome, 'propertyId' | 'statement' | 'sources'>;

/* One card's statement, never throwing. */
export async function sendSplit(base: SplitOutcomeBase): Promise<SplitOutcome> {
  try {
    return { ...base, kind: 'ok', result: await splitProperty(base.propertyId, base.statement) };
  } catch (e) {
    const refusal = splitRefusal(e);
    if (refusal?.code === 'reverses_rulings') return { ...base, kind: 'reverses', refusal };
    if (refusal?.code === 'stale') return { ...base, kind: 'stale' };
    return { ...base, kind: 'error', message: (e as Error).message };
  }
}

/* The outcome's second word: take a written split back with the server's undo,
 * or re-send one that would take back an earlier "různé". */
export async function followUpSplit(o: SplitOutcome): Promise<SplitOutcome> {
  const base: SplitOutcomeBase = { propertyId: o.propertyId, statement: o.statement, sources: o.sources };
  if (o.kind === 'reverses') return sendSplit({ ...base, statement: { ...o.statement, confirm_retract: true } });
  if (o.kind !== 'ok' || !o.result.undo) return o;
  try {
    return { ...base, kind: 'undone', result: await undoSplit(o.result.property_id, o.result.undo) };
  } catch (e) {
    return splitRefusal(e)?.code === 'stale'
      ? { ...base, kind: 'stale' }
      : { ...base, kind: 'error', message: (e as Error).message };
  }
}
