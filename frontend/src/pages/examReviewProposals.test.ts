import { describe, expect, it } from 'vitest';
import { applyProposal, humanVerdictOf, proposalsOf } from './NewDedupExamReview';

const TAGS = [{ id: 22, label: 'a' }, { id: 25, label: 'b' }];
const st = { picked: new Set([22]), skipped: new Set<number>(), cantTell: false };

describe('proposalsOf', () => {
  it('speaks the machine vocabulary for the human side', () => {
    expect(humanVerdictOf(st, 22)).toBe('yes');
    expect(humanVerdictOf(st, 25)).toBe('no');
    expect(humanVerdictOf({ ...st, cantTell: true }, 22)).toBe('skip');
  });
  it('on a can\'t-tell row only the machine\'s yes is raised', () => {
    const ct = { picked: new Set<number>(), skipped: new Set<number>(), cantTell: true };
    const out = proposalsOf(TAGS, ct, { '22': 'no', '25': 'yes' }, new Set());
    expect(out.map((p) => p.tag.id)).toEqual([25]);
  });
  it('applying a skip moves the cell to left-out and keeps the rest', () => {
    const next = applyProposal(st, 22, 'skip');
    expect([...next.picked]).toEqual([]);
    expect([...next.skipped]).toEqual([22]);
    const yes = applyProposal(next, 25, 'yes');
    expect([...yes.picked]).toEqual([25]);
    expect([...yes.skipped]).toEqual([22]);
  });
  it('applying on a can\'t-tell row starts over from the accepted pick', () => {
    const ct = { picked: new Set<number>(), skipped: new Set<number>(), cantTell: true };
    const next = applyProposal(ct, 25, 'yes');
    expect(next).toEqual({ picked: new Set([25]), skipped: new Set(), cantTell: false });
  });
});
